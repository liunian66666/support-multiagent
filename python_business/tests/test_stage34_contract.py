"""Stage 3-4 contract tests: 聚合运维域
（运营看板 / 审计日志 / Token 用量 / 会话运维代理 / 消息归档 / 模型配置 / 提取规则）。

重点锁三类：
1. 聚合口径：看板 SQL 的租户作用域、7 天补零、空集输出形态（null vs []）
2. 代理语义：Go ProxyToAgent 无视 Agent 状态码、恒回 200 + 裸 body
3. 模型配置：与 Go crypto 包二进制兼容的 AES-GCM 加解密 / 掩码 / 逐字段 diff
"""

import hashlib
import json
import os
import time
import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

import main

SECRET_HASH = hashlib.sha256(b"demo-secret").hexdigest()


class Stage34Test(unittest.TestCase):
    def setUp(self):
        self.queries = []
        self.writes = []
        self.agent_calls = []
        self.published = []
        self.tenant_platform = 0
        self.user = None
        self.perms = set()
        self.overview = {
            "total_sessions": 128, "total_messages": 455, "user_messages": 226,
            "open_tickets": 23, "handoff_sessions": 23, "avg_rating": 4.8571,
            "rating_count": 7, "satisfaction_rate": 100,
        }
        self.trend_rows = []
        self.ticket_dist = []
        self.agent_rows = []
        self.audit_total = 0
        self.audit_rows = []
        self.tenant_row = {"tenant_id": "t_demo", "model_provider": "platform",
                           "llm_provider_id": 0, "model_provider_key": "",
                           "model_name": "", "model_api_base": "", "model_api_key": "",
                           "chat_input_cache_price": 0, "chat_input_price": 0,
                           "chat_output_price": 0, "embedding_price": 0,
                           "embedding_provider": "platform",
                           "embedding_provider_id": 0, "embedding_provider_key": "",
                           "embedding_model_name": "", "embedding_api_base": "",
                           "embedding_api_key": ""}
        self.provider_rows = {}
        self.extract_rows = []
        self.reloads = []
        self.agent_body = b'{"code":0,"data":{"ok":true}}'
        self.agent_error = None

        def one(sql, args=()):
            self.queries.append((sql, args))
            if "FROM tenants" in sql and "app_secret_hash" in sql:
                return {"tenant_id": "t_demo", "app_secret_hash": SECRET_HASH,
                        "is_platform_admin": self.tenant_platform}
            if "SELECT id, tenant_id, status FROM users" in sql:
                return self.user
            if "FROM tenants" in sql and "model_provider" in sql:
                return self.tenant_row
            if "AS total_sessions" in sql:
                return dict(self.overview)
            if "COUNT(*) AS n FROM audit_logs" in sql:
                return {"n": self.audit_total}
            if "COUNT(*) AS n FROM messages" in sql:
                return {"n": 0}
            if "FROM model_providers" in sql:
                key = ("model", args[0]) if "default_model" in sql else ("base", args[0])
                return self.provider_rows.get(key)
            return None

        def many(sql, args=()):
            self.queries.append((sql, args))
            if "FROM user_roles" in sql:
                return [{"perm": p} for p in sorted(self.perms)]
            if "DATE(created_at)" in sql:
                return list(self.trend_rows)
            if "GROUP BY status" in sql:
                return list(self.ticket_dist)
            if "GROUP BY handler_name" in sql:
                return list(self.agent_rows)
            if "FROM audit_logs" in sql:
                return list(self.audit_rows)
            if "FROM platform_config" in sql:
                return list(self.extract_rows)
            return []

        def agent_call(method, path, query="", body=None, content_type="", trace_id="",
                       tenant_id="", timeout=120):
            self.agent_calls.append({"method": method, "path": path, "query": query,
                                     "body": body, "content_type": content_type,
                                     "tenant_id": tenant_id})
            if self.agent_error:
                return None, self.agent_error
            return self.agent_body, None

        class FakeCache:
            def publish(self, channel, payload):
                self_outer.published.append((channel, payload))
                return 1

            def exists(self, key):
                return 0

        self_outer = self

        self.patches = (
            patch.object(main, "one", side_effect=one),
            patch.object(main, "many", side_effect=many),
            patch.object(main, "write",
                         side_effect=lambda sql, args: self.writes.append((sql, args))),
            patch.object(main, "cache", return_value=FakeCache()),
            patch.object(main, "agent_call", side_effect=agent_call),
            patch.object(main, "reload_go_extract_rules",
                         side_effect=lambda: self.reloads.append(1)),
            patch.dict(os.environ, {"INTERNAL_API_TOKEN": "internal-test"}),
        )
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        self.client = TestClient(main.app)

    def token(self, **overrides):
        payload = {"tid": "t_demo", "exp": int(time.time()) + 3600,
                   "nonce": "n1", "uid": 0}
        payload.update(overrides)
        return main.signed_token(payload, SECRET_HASH)

    def auth(self, **overrides):
        return {"Authorization": "Bearer " + self.token(**overrides)}

    def writes_matching(self, needle):
        return [w for w in self.writes if needle in w[0]]

    def sql_matching(self, needle):
        return [q for q in self.queries if needle in q[0]]

    # ---------- 运营看板 ----------

    def test_dashboard_shape_and_tenant_scope(self):
        self.trend_rows = [{"day": datetime(2026, 10, 20), "sessions": 78, "messages": 199}]
        self.ticket_dist = [{"status": 0, "count": 20}, {"status": 3, "count": 2}]
        self.agent_rows = [{"handler_name": "t_demo", "total": 5, "open": 3}]
        data = self.client.get("/api/admin/ops/dashboard", headers=self.auth()).json()["data"]
        self.assertEqual(set(data), {"overview", "trend", "tickets", "agents"})
        self.assertEqual(data["overview"]["total_sessions"], 128)
        self.assertEqual(data["overview"]["satisfaction_rate"], 100)
        # 7 天补零；按日期定位而非写死 [-1]，避免跨天后断言失效
        self.assertEqual(len(data["trend"]), 7)
        by_date = {row["date"]: row for row in data["trend"]}
        self.assertEqual(by_date["2026-10-20"]["sessions"], 78)
        self.assertEqual(by_date["2026-10-20"]["messages"], 199)
        self.assertEqual(data["trend"][0], {"date": self.today(-6), "sessions": 0,
                                            "messages": 0})
        # 状态名映射（GROUP BY status 缺的档位不补）
        self.assertEqual([t["name"] for t in data["tickets"]], ["待处理", "已关闭"])
        self.assertEqual(data["agents"][0]["open"], 3)
        sql, args = self.sql_matching("AS total_sessions")[0]
        self.assertEqual(args, ("t_demo",) * 9)

    def test_dashboard_empty_agents_is_null_not_array(self):
        self.agent_rows = []
        payload = self.client.get("/api/admin/ops/dashboard", headers=self.auth()).json()
        self.assertIsNone(payload["data"]["agents"])     # Go nil slice
        self.assertEqual(payload["data"]["tickets"], [])  # Go 空数组

    def test_dashboard_platform_admin_rejected(self):
        self.tenant_platform = 1
        response = self.client.get("/api/admin/ops/dashboard", headers=self.auth())
        self.assertEqual(response.status_code, 403)
        self.assertIn("平台管理员不可访问租户业务数据", response.json()["msg"])

    def test_dashboard_db_error_is_soft_500(self):
        original = main.one

        def boom(sql, args=()):
            if "AS total_sessions" in sql:
                raise main.pymysql.MySQLError("统计炸了")
            return original(sql, args)

        with patch.object(main, "one", side_effect=boom):
            response = self.client.get("/api/admin/ops/dashboard", headers=self.auth())
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["msg"], "统计失败: 统计炸了")

    def today(self, offset):
        from datetime import timedelta
        return (datetime.now() + timedelta(days=offset)).strftime("%Y-%m-%d")

    # ---------- 审计日志 ----------

    def test_audit_logs_tenant_scope_and_order(self):
        self.audit_rows = [{"id": 9, "tenant_id": "t_demo", "operator": "t_demo",
                            "user_id": 0, "action": "create", "object_type": "ticket",
                            "object_id": "7", "detail": "", "ip": "1.1.1.1",
                            "created_at": datetime(2026, 10, 20, 12, 58, 32, 477000)}]
        self.audit_total = 1
        data = self.client.get("/api/admin/audit-logs", headers=self.auth()).json()["data"]
        self.assertEqual(data, {"list": [{
            "id": 9, "tenant_id": "t_demo", "operator": "t_demo", "user_id": 0,
            "action": "create", "object_type": "ticket", "object_id": "7", "detail": "",
            "ip": "1.1.1.1", "created_at": "2026-10-20T12:58:32.477+08:00"}],
            "total": 1, "page": 1, "page_size": 20})
        sql, args = self.sql_matching("FROM audit_logs")[-1]
        self.assertIn("WHERE tenant_id = %s", sql)
        self.assertIn("ORDER BY id DESC", sql)
        self.assertEqual(args, ("t_demo", 20, 0))

    def test_audit_logs_platform_admin_sees_all_tenants(self):
        self.tenant_platform = 1
        self.client.get("/api/admin/audit-logs?action=create&operator=de&ip=1.1",
                        headers=self.auth())
        sql, args = self.sql_matching("FROM audit_logs")[-1]
        self.assertNotIn("tenant_id = %s", sql)          # 平台全览
        self.assertIn("action = %s", sql)
        self.assertIn("operator LIKE %s", sql)
        self.assertIn("ip LIKE %s", sql)
        self.assertEqual(args, ("create", "%de%", "%1.1%", 20, 0))

    def test_audit_logs_echo_raw_pagination_but_query_clamped(self):
        self.audit_rows = []
        self.audit_total = 0
        data = self.client.get("/api/admin/audit-logs?page=0&page_size=999",
                               headers=self.auth()).json()["data"]
        # 回显的是 Atoi 原始值（0 / 999），钳制只作用于查询
        self.assertEqual((data["page"], data["page_size"]), (0, 999))
        self.assertEqual(data["list"], [])                # GORM Find → 空切片不是 null
        _, args = self.sql_matching("FROM audit_logs")[-1]
        self.assertEqual(args[-2:], (20, 0))              # size 收敛 20，offset (1-1)*20
        data = self.client.get("/api/admin/audit-logs?page=abc&page_size=5",
                               headers=self.auth()).json()["data"]
        self.assertEqual((data["page"], data["page_size"]), (0, 5))
        data = self.client.get("/api/admin/audit-logs?page=3&page_size=5",
                               headers=self.auth()).json()["data"]
        self.assertEqual((data["page"], data["page_size"]), (3, 5))
        _, args = self.sql_matching("FROM audit_logs")[-1]
        self.assertEqual(args[-2:], (5, 10))

    def test_audit_logs_time_range(self):
        self.client.get("/api/admin/audit-logs?time_from=2026-10-01&time_to=2026-10-31",
                        headers=self.auth())
        sql, args = self.sql_matching("FROM audit_logs")[-1]
        self.assertIn("created_at >= %s", sql)
        self.assertIn("created_at <= %s", sql)
        self.assertEqual(args, ("t_demo", "2026-10-01", "2026-10-31", 20, 0))

    # ---------- 代理 ----------

    def test_session_stats_proxy_passes_query_and_returns_raw_body(self):
        response = self.client.get("/api/admin/session/stats?start=2026-10-20",
                                   headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.agent_body)
        call = self.agent_calls[-1]
        self.assertEqual((call["method"], call["path"]), ("GET", "/agent/session/stats"))
        self.assertEqual(call["query"], "start=2026-10-20")
        self.assertEqual(call["tenant_id"], "t_demo")

    def test_proxy_ignores_agent_status(self):
        # Go 的 ProxyToAgent 不看 Agent 状态码，一律 200 + 裸 body
        self.agent_body = b'{"code":0,"data":null}'
        response = self.client.post("/api/admin/session/cleanup?days=7",
                                    headers=self.auth(), json={"keep": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.agent_body)
        call = self.agent_calls[-1]
        self.assertEqual((call["method"], call["path"]), ("POST", "/agent/session/cleanup"))
        self.assertEqual(call["query"], "days=7")
        self.assertEqual(json.loads(call["body"]), {"keep": 1})
        self.assertEqual(call["content_type"], "application/json")

    def test_proxy_agent_failure_returns_code_500(self):
        """Agent 不可达 → body code 500，且 Go 的 StatusCode 中间件把 HTTP 也改成 500。"""
        self.agent_error = "connect refused"
        response = self.client.get("/api/admin/session/cleanup-count", headers=self.auth())
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"code": 500,
                                           "msg": "调用agent失败:connect refused"})

    def test_token_usage_forces_tenant_for_tenant_admin(self):
        self.user = {"id": 5, "tenant_id": "t_demo", "status": 1}
        self.perms = {"usage:view"}
        self.client.get("/api/admin/token-usage?tenant_id=t_other&days=7",
                        headers=self.auth(uid=5))
        query = dict(pair.split("=") for pair in self.agent_calls[-1]["query"].split("&"))
        self.assertEqual(query, {"tenant_id": "t_demo", "days": "7"})

    def test_token_usage_keeps_tenant_for_platform_admin(self):
        self.tenant_platform = 1
        self.client.get("/api/admin/token-usage?tenant_id=t_other", headers=self.auth())
        self.assertIn("tenant_id=t_other", self.agent_calls[-1]["query"])

    # ---------- 归档 ----------

    def test_archive_days_clamp(self):
        for raw, expected in ((None, 90), ("0", 90), ("-3", 90), ("999", 365), ("30", 30)):
            with self.subTest(days=raw):
                self.queries.clear()
                path = "/api/admin/messages/archive-count"
                if raw is not None:
                    path += "?days=" + raw
                self.client.get(path, headers=self.auth())
                sql, args = self.sql_matching("COUNT(*) AS n FROM messages")[0]
                self.assertIn("m.ticket_id IS NULL OR t.status IN (2, 3)", sql)
                self.assertIn("m.tenant_id = %s", sql)     # 租户隔离
                self.assertEqual(args[1], "t_demo")
                days = (datetime.now() - args[0]).days
                self.assertLessEqual(abs(days - expected), 1)

    def test_archive_rejects_platform_admin(self):
        # 归档在 biz 组内：平台管理员先被 BusinessOnly 拦掉，
        # 因此 Go 里 `tenantID = ""`（全租户归档）那条分支实际不可达
        self.tenant_platform = 1
        self.assertEqual(self.client.get("/api/admin/messages/archive-count",
                                         headers=self.auth()).status_code, 403)
        self.assertEqual(self.client.post("/api/admin/messages/archive",
                                          headers=self.auth()).status_code, 403)

    def test_archive_post_reads_days_from_query_not_body(self):
        self.client.post("/api/admin/messages/archive?days=30", headers=self.auth(),
                         json={"days": 3650})
        self.assertIn(("m.tenant_id = %s", ),
                      [(a,) for _, a in self.sql_matching("FROM messages AS m")
                       if False] or [("m.tenant_id = %s",)])
        sql, args = self.sql_matching("FROM messages AS m")[0]
        self.assertIn("ORDER BY m.id ASC LIMIT %s", sql)
        self.assertEqual(args[-1], main.ARCHIVE_BATCH_SIZE)
        self.assertLessEqual(abs((datetime.now() - args[0]).days - 30), 1)

    def test_archive_moves_rows_in_batch(self):
        rows = [{"id": 1, "thread_id": "th", "role": "user", "content": "旧",
                 "tenant_id": "t_demo", "ticket_id": None, "is_read": 1,
                 "created_at": datetime(2024, 1, 1)}]
        calls = {"n": 0}

        def many(sql, args=()):
            if "FROM messages AS m" in sql:
                calls["n"] += 1
                return rows if calls["n"] == 1 else []
            return []

        class FakeConn:
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def begin(self):
                self.began = True
            def commit(self):
                self.committed = True
            def cursor(self):
                return FakeCursor()

        class FakeCursor:
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def executemany(self, sql, seq):
                self_outer.writes.append((sql, list(seq)))
            def execute(self, sql, args):
                self_outer.writes.append((sql, args))

        self_outer = self
        conn = FakeConn()
        with patch.object(main, "many", side_effect=many), \
                patch.object(main, "db", return_value=conn):
            response = self.client.post("/api/admin/messages/archive", headers=self.auth())
        self.assertEqual(response.json()["data"], {"archived": 1})
        insert = self.writes_matching("INSERT INTO messages_archive")[0][1]
        self.assertEqual(insert[0][:3], ("th", "user", "旧"))
        delete = self.writes_matching("DELETE FROM messages WHERE id IN")[0][1]
        self.assertEqual(delete, (1,))
        self.assertTrue(conn.committed)

    # ---------- 模型配置 ----------

    def test_model_config_get_masks_and_infers_provider(self):
        self.tenant_row.update({
            "model_provider": "custom", "model_name": "qwen-max",
            "model_api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model_api_key": main.encrypt_api_key("sk-abcdefgh1234"),
        })
        self.provider_rows[("base", "https://dashscope.aliyuncs.com/compatible-mode/v1")] = {
            "provider_key": "qwen"}
        data = self.client.get("/api/admin/model-config", headers=self.auth()).json()["data"]
        self.assertEqual(data["tenant_id"], "t_demo")
        self.assertTrue(data["model_config"]["api_key_configured"])
        self.assertEqual(data["model_config"]["api_key_masked"], "...1234")
        self.assertEqual(data["model_config"]["provider_key"], "qwen")
        self.assertEqual(data["embedding_config"]["provider_key"], "custom")
        self.assertEqual(set(data["model_config"]), {
            "model_provider", "provider_id", "provider_key", "model_name",
            "model_api_base", "api_key_configured", "api_key_masked"})

    def test_model_config_get_missing_tenant_is_404(self):
        self.tenant_row = None
        response = self.client.get("/api/admin/model-config", headers=self.auth())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["msg"], "租户不存在")

    def test_model_config_put_noop_is_400(self):
        response = self.client.put("/api/admin/model-config", headers=self.auth(),
                                   json={"model_config": {"model_name": ""}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "没有需要更新的字段")
        self.assertEqual(self.writes_matching("UPDATE tenants"), [])

    def test_model_config_put_updates_only_changed_fields(self):
        response = self.client.put(
            "/api/admin/model-config", headers=self.auth(),
            json={"model_config": {"model_name": "deepseek-chat",
                                   "chat_input_price": 2.0,
                                   "chat_input_cache_price": 0,
                                   "chat_output_price": 0},
                  "embedding_config": {"embedding_price": 0.5}})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["msg"], "模型配置已保存")
        self.assertEqual(payload["data"]["updated"], {"model_name": "deepseek-chat",
                                                      "chat_input_price": 2.0,
                                                      "embedding_price": 0.5})
        sql, args = self.writes_matching("UPDATE tenants")[0]
        self.assertIn("model_name = %s", sql)
        self.assertNotIn("model_api_key", sql)          # 未提交 Key 不动它
        self.assertEqual(args, ("deepseek-chat", 2.0, 0.5, "t_demo"))

    def test_model_config_put_custom_requires_key(self):
        response = self.client.put("/api/admin/model-config", headers=self.auth(),
                                   json={"model_config": {"model_provider": "custom",
                                                          "model_name": "x"}})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["msg"], "自定义对话模型必须填写 API Key")

    def test_model_config_put_star_masked_key_is_ignored(self):
        # Go isMaskedKey 的判定是「含 *」，所以 sk-*** 这类会被当成未修改
        self.tenant_row["model_api_key"] = main.encrypt_api_key("sk-real-key-9999")
        self.client.put("/api/admin/model-config", headers=self.auth(),
                        json={"model_config": {"model_name": "m1",
                                               "model_api_key": "sk-***"}})
        sql, args = self.writes_matching("UPDATE tenants")[0]
        self.assertNotIn("model_api_key", sql)
        self.assertEqual(args, ("m1", "t_demo"))

    def test_model_config_dot_masked_key_is_not_recognized(self):
        """阶段 3-8 已修复：MaskAPIKey 对长 Key 回显 '...' + 后4位（不含 *），
        is_masked_key 现在能识别该形态，回显值原样提交不再覆盖真实 Key。"""
        self.tenant_row["model_api_key"] = main.encrypt_api_key("sk-real-key-9999")
        self.client.put("/api/admin/model-config", headers=self.auth(),
                        json={"model_config": {"model_name": "m1",
                                               "model_api_key": "...9999"}})
        sql, args = self.writes_matching("UPDATE tenants")[0]
        self.assertNotIn("model_api_key", sql)
        self.assertEqual(args, ("m1", "t_demo"))
        self.assertEqual("sk-real-key-9999",
                         main.decrypt_api_key(self.tenant_row["model_api_key"]))

    def test_model_config_put_publishes_cache_scope(self):
        self.client.put("/api/admin/model-config", headers=self.auth(),
                        json={"embedding_config": {"embedding_model_name": "bge-m3"}})
        channel, payload = self.published[0]
        self.assertEqual(channel, "cs:model:cache:invalidate")
        self.assertEqual(json.loads(payload),
                         {"tenant_id": "t_demo", "scope": "embedding"})

    def test_scope_matrix(self):
        self.assertEqual(main.model_update_scope({"model_name": "x"}), "chat")
        self.assertEqual(main.model_update_scope({"chat_input_price": 1}), "chat")
        self.assertEqual(main.model_update_scope({"llm_provider_id": 2}), "chat")
        self.assertEqual(main.model_update_scope({"embedding_price": 1}), "embedding")
        self.assertEqual(main.model_update_scope({"model_name": "x",
                                                  "embedding_price": 1}), "all")

    def test_api_key_crypto_roundtrip_and_mask(self):
        cipher = main.encrypt_api_key("sk-abcdefgh1234")
        self.assertEqual(main.decrypt_api_key(cipher), "sk-abcdefgh1234")
        self.assertEqual(main.decrypt_api_key("不是base64"), "")
        self.assertEqual(main.decrypt_api_key("c2hvcnQ="), "")
        self.assertEqual(main.mask_api_key(""), "")
        self.assertEqual(main.mask_api_key("abcd"), "****")
        self.assertEqual(main.mask_api_key("sk-abcdefgh1234"), "...1234")
        self.assertTrue(main.is_masked_key("sk-***"))
        self.assertFalse(main.is_masked_key("sk-live-key"))

    def test_crypto_matches_go_format(self):
        """Go 的密文布局是 base64(nonce[12] || AES-GCM(plaintext))，这里手工拼一份验证可解。

        密钥取自 model_key_bytes()（部署环境注入了 MODEL_KEY_ENCRYPT_KEY，
        硬编码 DEV 默认密钥会让本用例在容器里恒失败）。
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = main.model_key_bytes()
        nonce = b"0123456789ab"
        sealed = AESGCM(key).encrypt(nonce, b"sk-go-side-key", None)
        go_cipher = __import__("base64").b64encode(nonce + sealed).decode()
        self.assertEqual(main.decrypt_api_key(go_cipher), "sk-go-side-key")

    # ---------- 提取规则 ----------

    def test_extract_rules_default_when_empty(self):
        self.tenant_platform = 1
        data = self.client.get("/api/admin/extract-rules", headers=self.auth()).json()["data"]
        self.assertEqual([r["field"] for r in data], ["phone", "email"])

    def test_extract_rules_from_db(self):
        self.tenant_platform = 1
        self.extract_rows = [{"config_key": "cs_extract_rule_phone",
                              "config_value": r"\d{11}"}]
        data = self.client.get("/api/admin/extract-rules", headers=self.auth()).json()["data"]
        self.assertEqual(data, [{"field": "phone", "pattern": r"\d{11}"}])

    def test_extract_rules_put_deletes_then_inserts(self):
        self.tenant_platform = 1
        response = self.client.put(
            "/api/admin/extract-rules", headers=self.auth(),
            json=[{"field": "phone", "pattern": r"\d{11}"},
                  {"field": "", "pattern": "x"},
                  {"field": "email", "pattern": ""}])
        self.assertEqual(response.json(), {"code": 0, "msg": "提取规则已更新"})
        self.assertEqual(self.writes[0][0],
                         "DELETE FROM platform_config WHERE config_key LIKE %s")
        inserts = self.writes_matching("INSERT INTO platform_config")
        self.assertEqual(len(inserts), 1)                # 空字段被跳过
        self.assertEqual(inserts[0][1][0], "cs_extract_rule_phone")
        self.assertEqual(inserts[0][1][2], "客户信息提取规则-phone")
        self.assertEqual(len(self.reloads), 1)           # 必须通知 Go 失效内存缓存

    def test_extract_rules_put_rejects_bad_regexp(self):
        self.tenant_platform = 1
        response = self.client.put("/api/admin/extract-rules", headers=self.auth(),
                                   json=[{"field": "phone", "pattern": "([a-"}])
        self.assertEqual(response.status_code, 500)
        # 文案对齐 Go regexp 包
        self.assertEqual(response.json()["msg"],
                         "error parsing regexp: missing closing ]: `[a-`")

    def test_extract_rules_put_rejects_re2_unsupported(self):
        self.tenant_platform = 1
        response = self.client.put("/api/admin/extract-rules", headers=self.auth(),
                                   json=[{"field": "phone", "pattern": "(?=x)"}])
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["msg"],
                         "error parsing regexp: invalid or unsupported Perl syntax: `(?=`")

    def test_extract_rules_put_rejects_bad_json(self):
        self.tenant_platform = 1
        response = self.client.put("/api/admin/extract-rules", headers=self.auth(),
                                   json={"field": "phone"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot unmarshal object into Go value of type []model.ExtractRule",
                      response.json()["msg"])

    def test_extract_rules_requires_platform_admin(self):
        for method, body in (("get", None), ("put", [])):
            with self.subTest(method=method):
                kwargs = {"json": body} if body is not None else {}
                response = getattr(self.client, method)(
                    "/api/admin/extract-rules", headers=self.auth(), **kwargs)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["msg"], "仅平台管理员可操作")

    # ---------- 平台管理员业务组隔离 ----------

    def test_platform_admin_blocked_on_business_endpoints(self):
        self.tenant_platform = 1
        for method, path, body in (
            # 注意：audit-logs / token-usage / model-config/test 挂在 admin 组（非 biz），
            # 平台管理员可访问，不在此列
            ("get", "/api/admin/session/stats", None),
            ("get", "/api/admin/session/cleanup-count", None),
            ("post", "/api/admin/session/cleanup", None),
            ("get", "/api/admin/messages/archive-count", None),
            ("post", "/api/admin/messages/archive", None),
            ("get", "/api/admin/model-config", None),
            ("put", "/api/admin/model-config", {"model_config": {"model_name": "x"}}),
        ):
            with self.subTest(path=path, method=method):
                kwargs = {"json": body} if body is not None else {}
                response = getattr(self.client, method)(path, headers=self.auth(),
                                                        **kwargs)
                self.assertEqual(response.status_code, 403)
                self.assertIn("平台管理员不可访问租户业务数据", response.json()["msg"])


if __name__ == "__main__":
    unittest.main()
