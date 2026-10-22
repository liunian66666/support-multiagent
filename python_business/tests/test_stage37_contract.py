"""Stage 3-7 contract tests: 代理域（Prompt / 知识库 / 平台设置 / 模型测试）。

这一批 18 条是「Go 只做鉴权+审计+转发」的路由，因此锁的是**转发语义**而不是业务：

1. 请求侧必须原样透传 query 与 body，并补齐 X-Trace-Id / X-Tenant-Id / X-Internal-Token
2. 响应侧不看 Agent 的 HTTP 状态码，拿到什么字节都回 200 + application/json
3. 连不上 Agent 时是 HTTP 200 + body code 500（不是 5xx），文案前缀「调用agent失败:」
4. `BusinessOnly` 的 prompts 白名单：平台管理员在 `/api/admin/prompts*` 放行、其余 biz 路由 403
5. `ProxyPrompt` 的 `?scope=platform`：非平台管理员软 403，平台管理员换 `__platform__` 租户
6. `ProxyKbUpload` 的 multipart 重组（只带 file/title/category）与无文件时的 JSON 透传
7. `platform-settings` 的 GET(admin) / PUT(plat) 分组不对称
8. `POST /api/admin/model-config/test` 是本批唯一有本地逻辑的一条：
   留空回退已存配置、use_custom 取「对话侧」口径、各种失败文案

测试不 patch `agent_call`：它自己就是被测对象。用替身 `httpx.Client` 拦住出口，
这样 header 拼装、query 透传、状态码忽略、异常转文案全部走真实代码路径。
"""

import hashlib
import json
import os
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import main_compat as main
from patch_business import patch_business

SECRET_HASH = hashlib.sha256(b"demo-secret").hexdigest()


def tenant_config(**overrides):
    row = {
        "tenant_id": "t_demo", "model_provider": "platform",
        "llm_provider_id": 0, "model_provider_key": "", "model_name": "",
        "model_api_base": "", "model_api_key": "",
        "chat_input_cache_price": 0, "chat_input_price": 0, "chat_output_price": 0,
        "embedding_price": 0, "embedding_provider": "platform",
        "embedding_provider_id": 0, "embedding_provider_key": "",
        "embedding_model_name": "", "embedding_api_base": "", "embedding_api_key": "",
    }
    row.update(overrides)
    return row


class FakeResponse:
    def __init__(self, status_code=200, content=b'{"code":0}', text=None):
        self.status_code = status_code
        self.content = content
        self.text = text if text is not None else content.decode()


class FakeClient:
    """替身 httpx.Client：同时服务 agent 转发(request) 与 model-config/test(post)。"""

    instances = []
    next_response = FakeResponse()
    raise_on_request = None

    def __init__(self, timeout=None):
        self.timeout = timeout
        self.request_calls = []
        self.post_calls = []
        FakeClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, url, content=None, headers=None, files=None, data=None):
        if FakeClient.raise_on_request:
            raise FakeClient.raise_on_request
        self.request_calls.append({"method": method, "url": url, "content": content,
                                   "headers": dict(headers or {}),
                                   "files": files, "data": data})
        return FakeClient.next_response

    def post(self, url, content=None, headers=None):
        if FakeClient.raise_on_request:
            raise FakeClient.raise_on_request
        self.post_calls.append({"url": url, "content": content,
                                "headers": dict(headers or {})})
        return FakeClient.next_response


class Stage37Test(unittest.TestCase):
    def setUp(self):
        FakeClient.instances = []
        FakeClient.next_response = FakeResponse()
        FakeClient.raise_on_request = None
        self.queries = []
        self.audit_rows = []
        self.platform_admin = 0
        self.config_rows = {"t_demo": tenant_config()}
        self.providers = []
        self.decrypt_map = {}

        def one(sql, args=()):
            self.queries.append((sql, args))
            if "app_secret_hash" in sql:
                return {"tenant_id": "t_demo", "app_secret_hash": SECRET_HASH,
                        "is_platform_admin": self.platform_admin}
            if "FROM model_providers WHERE id = %s" in sql:
                return next((p for p in self.providers
                             if p["id"] == args[0] and p["status"] == 1), None)
            if "FROM model_providers WHERE status = 1" in sql:
                for p in self.providers:
                    if p["status"] != 1:
                        continue
                    if p["provider_type"] in (args[0], "both"):
                        return p
                return None
            return None

        def write(sql, args):
            if "INSERT INTO audit_logs" in sql:
                self.audit_rows.append(args)

        def decrypt(value):
            return self.decrypt_map.get(value, "")

        def tenant_config_row(tid):
            return self.config_rows.get(tid)

        class FakeCache:
            def exists(self, key):
                return 0

            def publish(self, channel, message):
                return 1

        self.patches = (
            patch_business(main, "one", side_effect=one),
            patch_business(main, "write", side_effect=write),
            patch_business(main, "cache", side_effect=lambda: FakeCache()),
            patch_business(main, "decrypt_api_key", side_effect=decrypt),
            patch_business(main, "tenant_config_row", side_effect=tenant_config_row),
            patch.object(main.httpx, "Client", FakeClient),
            patch.dict(os.environ,
                       {"INTERNAL_API_TOKEN": "internal-test",
                        "CS_AGENT_URL": "http://agent:8000"}),
        )
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        self.client = TestClient(main.app)

    # ---------- 工具 ----------

    def last_request(self):
        return FakeClient.instances[-1].request_calls[-1]

    def token(self, **overrides):
        payload = {"tid": "t_demo", "exp": int(time.time()) + 3600, "nonce": "n1", "uid": 0}
        payload.update(overrides)
        return main.signed_token(payload, SECRET_HASH)

    def auth(self, **overrides):
        return {"Authorization": "Bearer " + self.token(**overrides)}

    # ---------- ① 代理透传语义 ----------

    def test_proxy_forwards_query_body_and_headers(self):
        resp = self.client.post("/api/admin/kb/batch?x=1&y=2",
                                content=b'{"ids":[1]}',
                                headers={**self.auth(),
                                         "Content-Type": "application/json",
                                         "X-Trace-Id": "trace-9"})
        self.assertEqual(200, resp.status_code)
        call = self.last_request()
        self.assertEqual("POST", call["method"])
        self.assertEqual("http://agent:8000/agent/kb/batch?x=1&y=2", call["url"])
        self.assertEqual(b'{"ids":[1]}', call["content"])
        self.assertEqual("application/json", call["headers"]["Content-Type"])
        self.assertEqual("trace-9", call["headers"]["X-Trace-Id"])
        self.assertEqual("t_demo", call["headers"]["X-Tenant-Id"])
        self.assertEqual("internal-test", call["headers"]["X-Internal-Token"])
        self.assertEqual(120, FakeClient.instances[-1].timeout)   # Go adminHTTPClient

    def test_proxy_ignores_agent_status_code(self):
        """Agent 自己的 HTTP 状态被丢弃，只看 body：Agent 回 500 但 body code=0 → 对外 200。"""
        FakeClient.next_response = FakeResponse(
            status_code=500, content=b'{"code":0,"data":{"ok":true}}')
        resp = self.client.get("/api/admin/kb/documents", headers=self.auth())
        self.assertEqual(200, resp.status_code)
        self.assertEqual(b'{"code":0,"data":{"ok":true}}', resp.content)
        self.assertEqual("application/json", resp.headers["content-type"])

    def test_proxy_rewrites_status_from_body_code(self):
        """Go 的 StatusCode 中间件按 body.code 改写状态码：Agent 回 400 → 对外 400。"""
        FakeClient.next_response = FakeResponse(
            status_code=200, content=b'{"code":400,"msg":"doc_ids \xe4\xb8\x8d\xe8\x83\xbd\xe4\xb8\xba\xe7\xa9\xba"}')
        resp = self.client.post("/api/admin/kb/batch", content=b"{}",
                                headers=self.auth())
        self.assertEqual(400, resp.status_code)
        self.assertEqual(400, resp.json()["code"])

    def test_proxy_non_json_body_keeps_200(self):
        """非 JSON 响应体不改写（Go 拿到 "Internal Server Error" 也是 200）。"""
        FakeClient.next_response = FakeResponse(
            status_code=500, content=b"Internal Server Error")
        resp = self.client.get("/api/admin/kb/documents", headers=self.auth())
        self.assertEqual(200, resp.status_code)
        self.assertEqual(b"Internal Server Error", resp.content)

    def test_proxy_agent_down_is_http_500_with_body_code(self):
        FakeClient.raise_on_request = httpx.ConnectError("connection refused")
        resp = self.client.get("/api/admin/kb/categories", headers=self.auth())
        self.assertEqual(500, resp.status_code)
        payload = resp.json()
        self.assertEqual(500, payload["code"])
        self.assertTrue(payload["msg"].startswith("调用agent失败:"))

    def test_agent_call_omits_tenant_header_when_empty(self):
        """Go：tenantID 为空时**不设** X-Tenant-Id（不是设成空串）。"""
        main.agent_call("GET", "/agent/kb/categories", tenant_id="")
        self.assertNotIn("X-Tenant-Id", self.last_request()["headers"])
        main.agent_call("GET", "/agent/kb/categories", tenant_id="t_demo")
        self.assertEqual("t_demo", self.last_request()["headers"]["X-Tenant-Id"])

    # ---------- ② BusinessOnly 的 prompts 白名单 ----------

    def test_platform_admin_allowed_on_prompts_only(self):
        """平台管理员在 /api/admin/prompts* 放行，在 /api/admin/kb/* 一律 403。"""
        self.platform_admin = 1
        self.assertEqual(200, self.client.get("/api/admin/prompts",
                                              headers=self.auth()).status_code)
        self.assertEqual(200, self.client.get("/api/admin/prompts/active",
                                              headers=self.auth()).status_code)
        for path in ("/api/admin/kb/documents", "/api/admin/kb/categories"):
            with self.subTest(path=path):
                resp = self.client.get(path, headers=self.auth())
                self.assertEqual(403, resp.status_code)
                self.assertEqual({"code": 403,
                                  "msg": "平台管理员不可访问租户业务数据，请使用对应租户账号"},
                                 resp.json())

    def test_tenant_admin_blocked_on_all_kb_routes(self):
        """租户 app_key 管理员（uid=0）隐式全权限，15 条 biz 代理路由全部放行。"""
        for method, path, body in (
                ("get", "/api/admin/prompts", None),
                ("get", "/api/admin/prompts/active", None),
                ("post", "/api/admin/prompts", b"{}"),
                ("delete", "/api/admin/prompts/faq", None),
                ("post", "/api/admin/prompts/faq/activate", b"{}"),
                ("get", "/api/admin/prompts/faq/versions", None),
                ("post", "/api/admin/prompts/faq/versions/3/rollback", b"{}"),
                ("post", "/api/admin/prompts/faq/versions", b"{}"),
                ("get", "/api/admin/kb/documents", None),
                ("delete", "/api/admin/kb/documents/d1", None),
                ("post", "/api/admin/kb/documents/d1/category", b"{}"),
                ("post", "/api/admin/kb/documents/d1/reindex", b"{}"),
                ("post", "/api/admin/kb/index", b"{}"),
                ("post", "/api/admin/kb/batch", b"{}"),
                ("get", "/api/admin/kb/categories", None)):
            with self.subTest(path=path):
                kwargs = {"headers": self.auth()}
                if body is not None:
                    kwargs["content"] = body
                resp = getattr(self.client, method)(path, **kwargs)
                self.assertEqual(200, resp.status_code)

    def test_subaccount_without_perm_is_forbidden(self):
        """子账号（uid>0）没有 prompt:view → 真实 403 无权限执行该操作。"""
        with patch_business(main, "one", side_effect=self._subaccount_one), \
             patch_business(main, "user_perms", return_value=set()):
            resp = self.client.get("/api/admin/prompts", headers=self.auth(uid=7))
        self.assertEqual(403, resp.status_code)
        self.assertEqual("无权限执行该操作", resp.json()["msg"])

    def _subaccount_one(self, sql, args=()):
        if "app_secret_hash" in sql:
            return {"tenant_id": "t_demo", "app_secret_hash": SECRET_HASH,
                    "is_platform_admin": 0}
        if sql.startswith("SELECT id, tenant_id, status FROM users"):
            return {"id": 7, "tenant_id": "t_demo", "status": 1}
        return None

    # ---------- ③ ProxyPrompt 的 platform 作用域 ----------

    def test_prompt_platform_scope_rejects_tenant_admin(self):
        resp = self.client.get("/api/admin/prompts?scope=platform", headers=self.auth())
        self.assertEqual(403, resp.status_code)
        self.assertEqual({"code": 403,
                          "msg": "仅平台管理员可管理平台级（全局）Prompt"}, resp.json())
        self.assertEqual([], FakeClient.instances)     # 根本没发出请求

    def test_prompt_platform_scope_uses_platform_tenant(self):
        self.platform_admin = 1
        resp = self.client.get("/api/admin/prompts?scope=platform", headers=self.auth())
        self.assertEqual(200, resp.status_code)
        self.assertEqual("__platform__", self.last_request()["headers"]["X-Tenant-Id"])

    def test_prompt_default_scope_uses_real_tenant(self):
        self.platform_admin = 1
        self.client.get("/api/admin/prompts", headers=self.auth())
        self.assertEqual("t_demo", self.last_request()["headers"]["X-Tenant-Id"])

    # ---------- ④ 平台设置 ----------

    def test_platform_settings_get_is_admin_group_put_is_plat_group(self):
        """GET 挂 admin 组（登录即可），PUT 挂 plat 组（租户管理员 403）——不对称。"""
        self.platform_admin = 1
        self.assertEqual(200, self.client.get("/api/admin/platform-settings",
                                              headers=self.auth()).status_code)
        self.assertEqual(200, self.client.put("/api/admin/platform-settings",
                                              content=b"{}",
                                              headers=self.auth()).status_code)
        self.platform_admin = 0
        self.assertEqual(200, self.client.get("/api/admin/platform-settings",
                                              headers=self.auth()).status_code)
        resp = self.client.put("/api/admin/platform-settings", content=b"{}",
                               headers=self.auth())
        self.assertEqual(403, resp.status_code)
        self.assertEqual({"code": 403, "msg": "仅平台管理员可操作"}, resp.json())

    def test_platform_settings_get_forwards_query(self):
        self.client.get("/api/admin/platform-settings?k=v", headers=self.auth())
        self.assertEqual("http://agent:8000/agent/platform-settings?k=v",
                         self.last_request()["url"])

    # ---------- ⑤ kb upload 的 multipart 重组 ----------

    def test_kb_upload_rebuilds_multipart_with_three_parts(self):
        resp = self.client.post(
            "/api/admin/kb/upload",
            files={"file": ("faq.txt", b"hello", "text/plain")},
            data={"title": "FAQ", "category": "售后"},
            headers=self.auth())
        self.assertEqual(200, resp.status_code)
        call = self.last_request()
        self.assertEqual("http://agent:8000/agent/kb/upload", call["url"])
        self.assertEqual(("faq.txt", b"hello"), call["files"]["file"])
        self.assertEqual({"title": "FAQ", "category": "售后"}, call["data"])

    def test_kb_upload_keeps_empty_fields(self):
        """Go 侧 title/category 为空也照写空串，不是省略。"""
        self.client.post("/api/admin/kb/upload",
                         files={"file": ("a.txt", b"x", "text/plain")},
                         headers=self.auth())
        self.assertEqual({"title": "", "category": ""}, self.last_request()["data"])

    def test_kb_upload_without_file_falls_back_to_json(self):
        self.client.post("/api/admin/kb/upload", json={"title": "t", "content": "c"},
                         headers={**self.auth(),
                                  "Content-Type": "application/json"})
        call = self.last_request()
        self.assertIsNone(call["files"])              # 走的是普通 proxy_to_agent
        self.assertEqual(b'{"title":"t","content":"c"}', call["content"])

    # ---------- ⑥ model-config/test 的本地逻辑 ----------

    def test_model_test_requires_type(self):
        resp = self.client.post("/api/admin/model-config/test", json={},
                                headers=self.auth())
        self.assertEqual({"code": 400, "msg": main.required_field_error("Type")},
                         resp.json())

    def test_model_test_checks_api_key_before_name_and_base(self):
        """Go 的校验顺序是 api_key → (model_name/api_base)，全空时报的是 API Key。"""
        resp = self.client.post("/api/admin/model-config/test", json={"type": "chat"},
                                headers=self.auth())
        self.assertEqual(400, resp.json()["code"])
        self.assertIn("请填写 API Key 后再测试", resp.json()["msg"])

    def test_model_test_missing_name_and_base_with_key(self):
        resp = self.client.post("/api/admin/model-config/test",
                                json={"type": "chat", "api_key": "k"},
                                headers=self.auth())
        self.assertEqual(400, resp.json()["code"])
        self.assertIn("模型名称与 API Base 不能为空", resp.json()["msg"])

    def test_model_test_rejects_unknown_type(self):
        resp = self.client.post("/api/admin/model-config/test",
                                json={"type": "rerank", "model_name": "m",
                                      "api_base": "https://x", "api_key": "k"},
                                headers=self.auth())
        self.assertEqual(400, resp.json()["code"])
        self.assertEqual("type 必须是 chat 或 embedding", resp.json()["msg"])

    def test_model_test_falls_back_to_stored_custom_config(self):
        """表单留空（前端只回显掩码）+ 已存自有配置 → 回退读库。

        注意 Go 的坑：`tenant_id` 缺省时回退读的是 **t_admin**，不是调用者自己的租户。
        所以这里把配置放在 t_admin 上；app_key 管理员恰好属于 t_admin，才「碰巧」对。
        """
        self.config_rows["t_admin"] = tenant_config(
            tenant_id="t_admin", model_provider="custom", model_name="stored-model",
            model_api_base="https://stored", model_api_key="enc")
        self.decrypt_map["enc"] = "plain-key"
        FakeClient.next_response = FakeResponse(
            text=json.dumps({"choices": [{"message": {"content": "pong"}}]}))
        resp = self.client.post("/api/admin/model-config/test", json={"type": "chat"},
                                headers=self.auth())
        self.assertTrue(resp.json()["data"]["ok"])
        call = FakeClient.instances[-1].post_calls[-1]
        self.assertEqual("https://stored/chat/completions", call["url"])
        self.assertEqual("Bearer plain-key", call["headers"]["Authorization"])
        self.assertIn('"model": "stored-model"', call["content"])

    def test_model_test_does_not_fall_back_when_following_platform(self):
        """use_custom=False（跟随平台）→ 不回退，缺 Key 直接 400。"""
        self.config_rows["t_admin"] = tenant_config(tenant_id="t_admin")   # 全空 = 跟随平台
        resp = self.client.post("/api/admin/model-config/test", json={"type": "chat"},
                                headers=self.auth())
        self.assertEqual(400, resp.json()["code"])
        self.assertIn("请填写 API Key 后再测试", resp.json()["msg"])

    def test_model_test_embedding_uses_chat_use_custom_gate(self):
        """Go 的 quirk：embedding 回填也走「对话侧是否自有」这同一个开关。

        这里让 embedding 有 Key 但对话侧未配置 → use_custom=False → 不回填。
        """
        self.config_rows["t_admin"] = tenant_config(
            tenant_id="t_admin",
            embedding_provider="custom", embedding_model_name="emb",
            embedding_api_base="https://emb", embedding_api_key="enc")
        self.decrypt_map["enc"] = "emb-key"
        resp = self.client.post("/api/admin/model-config/test", json={"type": "embedding"},
                                headers=self.auth())
        self.assertEqual(400, resp.json()["code"])
        self.assertIn("请填写 API Key 后再测试", resp.json()["msg"])

    def test_model_test_embedding_returns_dim(self):
        FakeClient.next_response = FakeResponse(
            text=json.dumps({"data": [{"embedding": [0.1, 0.2, 0.3]}]}))
        resp = self.client.post("/api/admin/model-config/test",
                                json={"type": "embedding", "model_name": "e",
                                      "api_base": "https://x/", "api_key": "k"},
                                headers=self.auth())
        data = resp.json()["data"]
        self.assertEqual({"ok": True, "dim": 3},
                         {k: v for k, v in data.items() if k != "latency_ms"})
        self.assertEqual("https://x/embeddings",
                         FakeClient.instances[-1].post_calls[-1]["url"])

    def test_model_test_http_error_is_soft_failure(self):
        FakeClient.next_response = FakeResponse(status_code=401,
                                                text="unauthorized-body")
        resp = self.client.post("/api/admin/model-config/test",
                                json={"type": "chat", "model_name": "m",
                                      "api_base": "https://x", "api_key": "k"},
                                headers=self.auth())
        self.assertEqual(0, resp.json()["code"])       # 外层 code=0，内层 ok=false
        self.assertEqual({"ok": False, "reason": "HTTP 401: unauthorized-body"},
                         resp.json()["data"])

    def test_model_test_chat_without_choices_is_soft_failure(self):
        FakeClient.next_response = FakeResponse(text=json.dumps({"object": "x"}))
        resp = self.client.post("/api/admin/model-config/test",
                                json={"type": "chat", "model_name": "m",
                                      "api_base": "https://x", "api_key": "k"},
                                headers=self.auth())
        self.assertEqual({"ok": False,
                          "reason": "响应无 choices 字段，模型可能不支持该接口"},
                         resp.json()["data"])

    def test_model_test_unparsable_body_is_soft_failure(self):
        FakeClient.next_response = FakeResponse(status_code=200, text="<html>oops</html>")
        resp = self.client.post("/api/admin/model-config/test",
                                json={"type": "chat", "model_name": "m",
                                      "api_base": "https://x", "api_key": "k"},
                                headers=self.auth())
        self.assertEqual({"ok": False, "reason": "响应解析失败: <html>oops</html>"},
                         resp.json()["data"])

    def test_model_test_connection_failure_is_soft_failure(self):
        FakeClient.raise_on_request = httpx.ConnectTimeout("connect timeout")
        resp = self.client.post("/api/admin/model-config/test",
                                json={"type": "chat", "model_name": "m",
                                      "api_base": "https://x", "api_key": "k"},
                                headers=self.auth())
        self.assertTrue(resp.json()["data"]["reason"].startswith("连接失败:"))
        self.assertFalse(resp.json()["data"]["ok"])

    def test_model_test_uses_tenant_id_query_for_fallback(self):
        """`?tenant_id=` 指定回退来源；缺省是 t_admin。"""
        self.config_rows["t_admin"] = tenant_config(
            model_provider="custom", model_name="admin-model",
            model_api_base="https://admin", model_api_key="enc")
        self.decrypt_map["enc"] = "admin-key"
        FakeClient.next_response = FakeResponse(
            text=json.dumps({"choices": [{"message": {"content": "pong"}}]}))
        resp = self.client.post("/api/admin/model-config/test?tenant_id=t_admin",
                                json={"type": "chat"}, headers=self.auth())
        self.assertTrue(resp.json()["data"]["ok"])
        self.assertEqual("https://admin/chat/completions",
                         FakeClient.instances[-1].post_calls[-1]["url"])


if __name__ == "__main__":
    unittest.main()
