"""Stage 3-6 contract tests: 平台域（租户 / 模型提供商 / 菜单）。

锁的重点：
1. 两套错误语义不能混：租户与模型提供商是「软失败」（HTTP 200 + body code，审计照记），
   菜单是「真失败」（真实 4xx/500，审计跳过）
2. 租户列表的三段用量口径（MySQL 今日消息 → Agent → ws Hub 连接数）与
   `time.Truncate(24h)` 的 UTC 午夜语义
3. 模型提供商被引用时 403、required 校验文案、菜单重置的 50 条内置数据与 admin 补授权
"""

import hashlib
import os
import time
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

import main

SECRET_HASH = hashlib.sha256(b"demo-secret").hexdigest()


def tenant_row(**overrides):
    row = {"id": 2, "tenant_id": "t_demo", "name": "演示租户", "app_key": "tk_demo",
           "is_platform_admin": 0, "status": 1,
           "created_at": datetime(2026, 9, 1, 10, 30, 0),
           "model_provider": "platform", "model_provider_key": "", "llm_provider_id": 0,
           "model_name": "", "model_api_base": "",
           "chat_input_cache_price": 0, "chat_input_price": 0, "chat_output_price": 0,
           "embedding_price": 0, "embedding_provider": "platform",
           "embedding_provider_key": "", "embedding_provider_id": 0,
           "embedding_model_name": "", "embedding_api_base": "",
           "max_concurrent_sessions": 100, "daily_message_limit": 1000,
           "max_kb_docs": 100, "max_kb_size_mb": 100}
    row.update(overrides)
    return row


def provider_row(**overrides):
    row = {"id": 3, "provider_key": "deepseek", "provider_name": "DeepSeek",
           "default_model": "deepseek-chat", "default_api_base": "https://api.deepseek.com",
           "provider_type": "chat", "sort_order": 1, "status": 1,
           "created_at": datetime(2026, 9, 1, 10, 0, 0),
           "updated_at": datetime(2026, 9, 1, 10, 0, 0)}
    row.update(overrides)
    return row


def menu_row(**overrides):
    row = {"id": 1, "parent_id": 0, "group_name": "业务中心", "name": "会话管理", "type": 1,
           "path": "", "component": "", "perm": None, "icon": "", "sort": 1,
           "visible": 1, "status": 1, "is_builtin": 1, "created_at": None,
           "updated_at": datetime(2026, 10, 18, 13, 5, 0)}
    row.update(overrides)
    return row


class FakeCursor:
    def __init__(self, harness):
        self.harness = harness
        self.lastrowid = None
        self._rows = None

    def execute(self, sql, args=()):
        self.harness.statements.append((sql, args))
        if sql.startswith("INSERT INTO tenants"):
            self.lastrowid = 31
        elif sql.startswith("INSERT INTO model_providers"):
            self.lastrowid = 44
        elif sql.startswith("INSERT INTO menus"):
            self.lastrowid = 88
        self._rows = self.harness.cursor_result(sql, args)

    def fetchone(self):
        if isinstance(self._rows, list):
            return self._rows[0] if self._rows else None
        return self._rows

    def fetchall(self):
        if self._rows is None:
            return []
        return self._rows if isinstance(self._rows, list) else [self._rows]


class Stage36Test(unittest.TestCase):
    def setUp(self):
        self.queries = []
        self.writes = []
        self.statements = []
        self.tenant_platform = 0
        self.tenant_row = tenant_row()
        self.messages_count_rows = []
        self.agent_usage = None
        self.ws_conns = {}
        self.tenants = []
        self.providers = []
        self.provider_by_id = {}
        self.menus = []
        self.menu_by_id = {}
        self.menu_children = {}
        self.removed_menus = []
        self.builtin_ids = []
        self.admin_role_ids = []
        self.biz_menu_ids = []
        self.dup_on_insert = False
        self.audit_rows_written = []
        self.cursor_hook = None
        self.published = []

        def one(sql, args=()):
            self.queries.append((sql, args))
            if "app_secret_hash" in sql:
                return {"tenant_id": "t_demo", "app_secret_hash": SECRET_HASH,
                        "is_platform_admin": self.tenant_platform}
            if sql.startswith(f"SELECT {main.TENANT_COLUMNS} FROM tenants"):
                return self.tenant_row
            if sql.startswith("SELECT status FROM tenants"):
                return self.tenant_row
            if sql.startswith("SELECT id FROM tenants WHERE app_key"):
                return {"id": 9} if args[0] == "tk_taken" else None
            if sql.startswith("SELECT id FROM tenants WHERE tenant_id"):
                return {"id": 2} if self.tenant_row else None
            if sql.startswith("SELECT id FROM model_providers"):
                return self.provider_by_id.get(args[0])
            if sql.startswith(f"SELECT {main.MODEL_PROVIDER_COLUMNS} FROM model_providers"):
                return provider_row(id=args[0])
            if sql.startswith("SELECT COUNT(*) AS cnt FROM tenants"):
                return {"cnt": self.provider_ref_count if hasattr(
                    self, "provider_ref_count") else 0}
            if sql.startswith(f"SELECT {main.MENU_COLUMNS} FROM menus WHERE id = %s"):
                return self.menu_by_id.get(args[0])
            if sql.startswith("SELECT id FROM menus WHERE id"):
                return {"id": args[0]} if args[0] in self.menu_by_id else None
            return None

        def many(sql, args=()):
            self.queries.append((sql, args))
            if sql.startswith("SELECT tenant_id, COUNT(*) AS cnt FROM messages"):
                return list(self.messages_count_rows)
            if sql.startswith(f"SELECT {main.TENANT_COLUMNS} FROM tenants"):
                return list(self.tenants)
            if sql.startswith(f"SELECT {main.MODEL_PROVIDER_COLUMNS} FROM model_providers"):
                return list(self.providers)
            if sql.startswith(f"SELECT {main.MENU_COLUMNS} FROM menus"):
                return list(self.menus)
            if sql.startswith("SELECT id FROM menus WHERE parent_id"):
                return [{"id": i} for i in self.menu_children.get(args[0], [])]
            return []

        def write(sql, args):
            self.writes.append((sql, args))
            if "INSERT INTO audit_logs" in sql:
                self.audit_rows_written.append(args)
            if sql.startswith("INSERT INTO tenants") and self.dup_on_insert:
                raise main.pymysql.MySQLError(1062, "Duplicate entry")

        @contextmanager
        def transaction():
            yield FakeCursor(self)

        published = self.published

        class FakeCache:
            def exists(self, key):
                return 0

            def publish(self, channel, message):
                published.append((channel, message))

        fake_cache = FakeCache()

        def cache():
            return fake_cache

        self.patches = (
            patch.object(main, "one", side_effect=one),
            patch.object(main, "many", side_effect=many),
            patch.object(main, "write", side_effect=write),
            patch.object(main, "transaction", side_effect=transaction),
            patch.object(main, "cache", side_effect=cache),
            patch.object(main, "agent_call", side_effect=lambda *a, **k: (None, "down")),
            patch.object(main, "go_internal", side_effect=self.fake_go_internal),
            patch.object(main.ws_hub, "count_all_tenants",
                         side_effect=lambda: self.ws_conns),
            patch.object(main, "prewarm_tenant_async", side_effect=lambda tid: None),
            patch.object(main, "cleanup_tenant_async", side_effect=lambda tid: None),
            patch.object(main, "reload_go_menu_caches", side_effect=lambda: None),
            patch.dict(os.environ, {"INTERNAL_API_TOKEN": "internal-test",
                                         "USE_PYTHON_WS_HUB": "true"}),
        )
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        self.client = TestClient(main.app)

    # ---------- 工具 ----------

    def fake_go_internal(self, method, path, body=None, timeout=5):
        if path == "/api/agent/ws/conns":
            return {"code": 0, "data": self.ws_conns}
        return {"code": 0, "msg": "ok"}

    def cursor_result(self, sql, args):
        if self.cursor_hook:
            return self.cursor_hook(sql, args)
        if sql.startswith("SELECT id FROM menus WHERE is_builtin = 1"):
            return [{"id": i} for i in self.builtin_ids]
        if sql.startswith(f"SELECT {main.MENU_COLUMNS} FROM menus WHERE id IN"):
            return list(self.removed_menus)
        if sql.startswith("SELECT id FROM roles WHERE code = 'admin'"):
            return [{"id": i} for i in self.admin_role_ids]
        if sql.startswith("SELECT id FROM menus WHERE group_name = '业务中心'"):
            return [{"id": i} for i in self.biz_menu_ids]
        return None

    def token(self, **overrides):
        payload = {"tid": "t_demo", "exp": int(time.time()) + 3600, "nonce": "n1", "uid": 0}
        payload.update(overrides)
        return main.signed_token(payload, SECRET_HASH)

    def auth(self, **overrides):
        return {"Authorization": "Bearer " + self.token(**overrides)}

    def platform(self):
        self.tenant_platform = 1
        return self.auth()

    def writes_matching(self, needle):
        return [w for w in self.writes if needle in w[0]]

    def business_writes(self):
        return [w for w in self.writes if "INSERT INTO audit_logs" not in w[0]]

    # ---------- 权限边界 ----------

    def test_platform_endpoints_reject_non_platform_admin(self):
        for path in ("/api/admin/tenants", "/api/admin/model-providers",
                     "/api/admin/menus"):
            response = self.client.get(path, headers=self.auth())
            self.assertEqual(response.status_code, 403, path)
            self.assertEqual(response.json()["msg"], "仅平台管理员可操作")

    def test_available_model_providers_is_admin_group_not_platform_only(self):
        """Go 把它挂在 admin 组（只要求登录），租户管理员也能读公共配置。"""
        self.providers = [provider_row()]
        response = self.client.get("/api/admin/available-model-providers",
                                   headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)

    def test_platform_rejects_subaccount_too(self):
        response = self.client.get("/api/admin/tenants", headers=self.auth(uid=5))
        self.assertEqual(response.status_code, 403)

    # ---------- 租户列表 ----------

    def test_tenant_list_excludes_platform_and_orders_desc(self):
        self.tenants = [tenant_row()]
        payload = self.client.get("/api/admin/tenants", headers=self.platform()).json()
        self.assertEqual(payload["data"][0]["tenant_id"], "t_demo")
        sql, args = [q for q in self.queries
                     if q[0].startswith(f"SELECT {main.TENANT_COLUMNS} FROM tenants")][0]
        self.assertIn("is_platform_admin = 0", sql)
        self.assertIn("ORDER BY created_at DESC", sql)
        self.assertEqual(args, ())

    def test_tenant_list_status_only_accepts_exact_zero_one(self):
        self.tenants = [tenant_row()]
        self.client.get("/api/admin/tenants?status=01", headers=self.platform())
        sql = [q for q in self.queries
               if q[0].startswith(f"SELECT {main.TENANT_COLUMNS} FROM tenants")][0][0]
        self.assertNotIn("status =", sql)
        self.queries.clear()
        self.client.get("/api/admin/tenants?status=0", headers=self.platform())
        sql, args = [q for q in self.queries
                     if q[0].startswith(f"SELECT {main.TENANT_COLUMNS} FROM tenants")][0]
        self.assertIn("status = %s", sql)
        self.assertEqual(args, (0,))

    def test_tenant_list_like_filters_and_sort_variants(self):
        self.tenants = [tenant_row()]
        self.client.get("/api/admin/tenants?name=%E6%BC%94&app_key=tk&sort=name",
                        headers=self.platform())
        sql, args = [q for q in self.queries
                     if q[0].startswith(f"SELECT {main.TENANT_COLUMNS} FROM tenants")][0]
        self.assertIn("name LIKE %s", sql)
        self.assertIn("app_key LIKE %s", sql)
        self.assertIn("ORDER BY name ASC", sql)
        self.assertEqual(args, ("%演%", "%tk%"))

    def test_tenant_list_created_at_uses_space_format(self):
        self.tenants = [tenant_row(created_at=datetime(2026, 9, 1, 10, 30, 0))]
        item = self.client.get("/api/admin/tenants", headers=self.platform()).json()["data"][0]
        self.assertEqual(item["created_at"], "2026-09-01 10:30:00")

    def test_tenant_list_usage_three_sources_override_in_order(self):
        self.tenants = [tenant_row()]
        self.messages_count_rows = [{"tenant_id": "t_demo", "cnt": 7}]
        self.ws_conns = {"t_demo": 4}
        with patch.object(main, "agent_call", side_effect=lambda *a, **k: (
                b'{"code":0,"data":{"t_demo":{"active_sessions":99,"kb_docs":3,'
                b'"kb_size_bytes":2097152}}}', None)):
            item = self.client.get("/api/admin/tenants",
                                   headers=self.platform()).json()["data"][0]
        self.assertEqual(item["today_messages"], 7)
        self.assertEqual(item["kb_docs"], 3)
        self.assertEqual(item["kb_size_mb"], 2.0)
        # ws Hub 连接数最后覆盖 Agent 的 active_sessions
        self.assertEqual(item["active_sessions"], 4)

    def test_utc_day_start_matches_go_truncate_semantics(self):
        start = main.utc_day_start_local()
        # Go 的 Truncate(24h) 按 Unix 纪元取整 = UTC 午夜；本地时区偏移 8h 时是本地 08:00
        self.assertEqual(start.hour, 8)
        self.assertEqual(start.minute, 0)

    def test_tenant_list_empty_is_empty_list(self):
        payload = self.client.get("/api/admin/tenants", headers=self.platform()).json()
        self.assertEqual(payload["data"], [])

    # ---------- 租户详情 ----------

    def test_tenant_detail_hides_secret_and_soft_delete_fields(self):
        data = self.client.get("/api/admin/tenants/t_demo",
                               headers=self.platform()).json()["data"]
        for hidden in ("app_secret_hash", "deleted_at", "rbac_seeded"):
            self.assertNotIn(hidden, data)
        self.assertEqual(data["created_at"], "2026-09-01T10:30:00+08:00")
        self.assertIs(data["is_platform_admin"], False)

    def test_tenant_detail_not_found_is_soft_404(self):
        self.tenant_row = None
        response = self.client.get("/api/admin/tenants/t_none", headers=self.platform())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"code": 404, "msg": "租户不存在"})

    # ---------- 创建租户 ----------

    def test_tenant_create_generates_key_and_hashes_secret(self):
        response = self.client.post("/api/admin/tenants", json={"name": "新租户"},
                                    headers=self.platform())
        payload = response.json()
        self.assertEqual(payload["code"], 0)
        data = payload["data"]
        self.assertTrue(data["app_key"].startswith("tk_"))
        self.assertEqual(len(data["app_key"]), 19)          # tk_ + 16 hex
        self.assertEqual(len(data["app_secret"]), 32)       # 16 字节 hex
        self.assertEqual(data["tenant_id"], "t_" + data["app_key"])
        self.assertEqual(data["tip"], "请立即保存 app_secret，仅此一次显示")
        _sql, args = self.writes_matching("INSERT INTO tenants")[0]
        self.assertEqual(args[3], hashlib.sha256(data["app_secret"].encode()).hexdigest())
        self.assertEqual(_sql.count("'platform'"), 2)      # chat 与 embedding 都跟随平台
        # 所有 GORM 会写零值的列都要显式写（表里默认是 NULL，漏写会查不到）
        for column in ("is_platform_admin", "rbac_seeded", "created_at", "deleted_at",
                       "model_api_key", "embedding_api_key"):
            self.assertIn(column, _sql)
        self.assertIn("0, 1, 0,", _sql)
        self.assertTrue(_sql.rstrip().endswith("NOW(3), 0)"))

    def test_tenant_create_requires_name(self):
        for body in ({}, {"name": ""}, {"name": 1}):
            response = self.client.post("/api/admin/tenants", json=body,
                                        headers=self.platform())
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["msg"], "name 不能为空")
        self.assertEqual(self.business_writes(), [])

    def test_tenant_create_rejects_used_app_key(self):
        response = self.client.post("/api/admin/tenants",
                                    json={"name": "x", "app_key": "tk_taken"},
                                    headers=self.platform())
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["msg"], "app_key 已被使用（含已删除租户）")

    def test_tenant_create_duplicate_race_returns_db_error(self):
        self.dup_on_insert = True
        response = self.client.post("/api/admin/tenants", json={"name": "x"},
                                    headers=self.platform())
        self.assertEqual(response.status_code, 500)
        self.assertIn("Duplicate entry", response.json()["msg"])

    # ---------- 租户更新 ----------

    def test_tenant_update_rename(self):
        payload = self.client.put("/api/admin/tenants/t_demo", json={"name": "改名"},
                                  headers=self.platform()).json()
        self.assertEqual(payload, {"code": 0, "msg": "租户已更新",
                                   "data": {"tenant_id": "t_demo", "name": "改名"}})
        self.assertEqual(self.writes_matching("UPDATE tenants SET name")[0][1],
                         ("改名", "t_demo"))

    def test_tenant_update_requires_name(self):
        response = self.client.put("/api/admin/tenants/t_demo", json={"name": ""},
                                   headers=self.platform())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "name 不能为空")

    def test_tenant_update_missing_tenant_is_404(self):
        self.tenant_row = None
        response = self.client.put("/api/admin/tenants/t_none", json={"name": "x"},
                                   headers=self.platform())
        self.assertEqual(response.json()["msg"], "租户不存在")

    def test_tenant_quota_clamps_negative_to_zero(self):
        payload = self.client.put("/api/admin/tenants/t_demo/quota",
                                  json={"max_concurrent_sessions": -5,
                                        "daily_message_limit": 200,
                                        "max_kb_docs": -1, "max_kb_size_mb": 50},
                                  headers=self.platform()).json()
        self.assertEqual(payload["msg"], "租户配额已更新")
        self.assertEqual(self.writes_matching("UPDATE tenants SET max_concurrent_sessions")[0][1],
                         (0, 200, 0, 50, "t_demo"))

    def test_tenant_quota_rejects_non_integer(self):
        response = self.client.put("/api/admin/tenants/t_demo/quota",
                                   json={"max_kb_docs": "many"}, headers=self.platform())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "参数错误")

    def test_tenant_reset_secret_rotates_hash(self):
        payload = self.client.post("/api/admin/tenants/t_demo/reset-secret",
                                   headers=self.platform()).json()
        self.assertEqual(len(payload["data"]["app_secret"]), 32)
        self.assertIn("新的 app_secret", payload["data"]["tip"])
        self.assertEqual(self.writes_matching("UPDATE tenants SET app_secret_hash")[0][1][1],
                         "t_demo")

    def test_tenant_toggle_flips_status_and_message(self):
        self.tenant_row = tenant_row(status=1)
        payload = self.client.post("/api/admin/tenants/t_demo/toggle",
                                   headers=self.platform()).json()
        self.assertEqual(payload["msg"], "租户已禁用")
        self.assertEqual(payload["data"]["status"], 0)
        self.tenant_row = tenant_row(status=0)
        payload = self.client.post("/api/admin/tenants/t_demo/toggle",
                                   headers=self.platform()).json()
        self.assertEqual(payload["msg"], "租户已启用")
        self.assertEqual(payload["data"]["status"], 1)

    # ---------- 删除租户 ----------

    def test_tenant_delete_blocks_platform_tenant_but_still_audits(self):
        response = self.client.delete("/api/admin/tenants/t_admin", headers=self.platform())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["msg"], "不能删除平台管理员租户")
        # 软失败：审计中间件读到的是 200，仍写一条
        self.assertEqual(len(self.audit_rows_written), 1)
        self.assertEqual(self.business_writes(), [])

    def test_tenant_delete_soft_deletes_tickets_and_hard_deletes_messages(self):
        payload = self.client.delete("/api/admin/tenants/t_demo",
                                     headers=self.platform()).json()
        self.assertEqual(payload["msg"], "已删除租户 t_demo（含工单/消息/Prompt/知识库数据）")
        # tickets / tenants 走软删（unix 秒）；messages 表没有 deleted_at 列 → 硬删除
        soft_deletes = [s for s in self.statements if "SET deleted_at" in s[0]]
        self.assertEqual(len(soft_deletes), 2)
        self.assertTrue(any("tickets" in s[0] for s in soft_deletes))
        self.assertTrue(any("tenants" in s[0] for s in soft_deletes))
        hard_deletes = [s for s in self.statements
                        if s[0].startswith("DELETE FROM messages")]
        self.assertEqual(hard_deletes[0][1], ("t_demo",))
        stamp = soft_deletes[0][1][0]
        self.assertIsInstance(stamp, int)                   # 软删存 unix 秒
        self.assertEqual(soft_deletes[0][1][1], "t_demo")

    def test_tenant_delete_missing_is_404(self):
        self.tenant_row = None
        response = self.client.delete("/api/admin/tenants/t_none", headers=self.platform())
        self.assertEqual(response.json()["msg"], "租户不存在")

    # ---------- 租户模型配置 ----------

    def test_tenant_model_config_not_found(self):
        self.tenant_row = None
        response = self.client.get("/api/admin/tenants/t_none/model-config",
                                   headers=self.platform())
        self.assertEqual(response.json()["msg"], "租户不存在")

    def test_tenant_model_config_update_missing_tenant_is_500_not_404(self):
        self.tenant_row = None
        response = self.client.put("/api/admin/tenants/t_none/model-config",
                                   json={"model_config": {"model_provider": "platform"}},
                                   headers=self.platform())
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["msg"], "record not found")

    # ---------- 模型提供商 ----------

    def test_model_provider_list_orders_by_sort_then_id(self):
        self.providers = [provider_row()]
        data = self.client.get("/api/admin/model-providers",
                               headers=self.platform()).json()["data"]
        self.assertEqual(data[0]["provider_key"], "deepseek")
        sql = [q[0] for q in self.queries if "FROM model_providers" in q[0]][0]
        self.assertIn("ORDER BY sort_order ASC, id ASC", sql)

    def test_available_model_providers_filters_enabled_and_type(self):
        self.providers = [provider_row()]
        self.client.get("/api/admin/available-model-providers?type=chat",
                        headers=self.platform())
        sql, args = [q for q in self.queries if "FROM model_providers" in q[0]][0]
        self.assertIn("status = 1", sql)
        self.assertIn("provider_type = 'both'", sql)
        self.assertEqual(args, ("chat",))

    def test_model_provider_create_defaults_type_both_and_status_one(self):
        payload = self.client.post("/api/admin/model-providers",
                                   json={"provider_key": "qwen",
                                         "provider_name": "通义千问"},
                                   headers=self.platform()).json()
        self.assertEqual(payload["msg"], "模型提供商已创建")
        self.assertEqual(payload["data"]["id"], 44)
        sql, args = self.statements[0]
        self.assertEqual(args[4], "both")                  # provider_type 缺省 → both
        self.assertIn(", 1, NOW(), NOW())", sql)

    def test_model_provider_create_requires_key_then_name(self):
        response = self.client.post("/api/admin/model-providers",
                                    json={"provider_name": "X"}, headers=self.platform())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"],
                         "Key: 'ProviderKey' Error:Field validation for 'ProviderKey' "
                         "failed on the 'required' tag")
        response = self.client.post("/api/admin/model-providers",
                                    json={"provider_key": "x"}, headers=self.platform())
        self.assertEqual(response.json()["msg"],
                         "Key: 'ProviderName' Error:Field validation for 'ProviderName' "
                         "failed on the 'required' tag")
        # 两个必填都缺失时，validator 会把两条用 \n 串起来
        response = self.client.post("/api/admin/model-providers", json={},
                                    headers=self.platform())
        lines = response.json()["msg"].split("\n")
        self.assertEqual(len(lines), 2)
        self.assertIn("ProviderKey", lines[0])
        self.assertIn("ProviderName", lines[1])

    def test_model_provider_update_requires_existing_and_fields(self):
        self.provider_by_id = {}
        response = self.client.put("/api/admin/model-providers/3",
                                   json={"provider_name": "x"}, headers=self.platform())
        self.assertEqual(response.json()["msg"], "模型提供商不存在")
        self.provider_by_id = {3: provider_row()}
        response = self.client.put("/api/admin/model-providers/3", json={},
                                   headers=self.platform())
        self.assertEqual(response.json()["msg"], "没有需要更新的字段")

    def test_model_provider_update_bad_id(self):
        response = self.client.put("/api/admin/model-providers/abc",
                                   json={"provider_name": "x"}, headers=self.platform())
        self.assertEqual(response.json()["msg"], "无效的 ID")

    def test_model_provider_delete_blocked_when_referenced(self):
        self.provider_by_id = {3: provider_row()}
        self.provider_ref_count = 2
        response = self.client.delete("/api/admin/model-providers/3",
                                      headers=self.platform())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["msg"],
                         "有 2 个租户正在使用该提供商，请先调整租户配置")
        self.assertEqual(self.business_writes(), [])

    def test_model_provider_delete_success(self):
        self.provider_by_id = {3: provider_row()}
        self.provider_ref_count = 0
        payload = self.client.delete("/api/admin/model-providers/3",
                                     headers=self.platform()).json()
        self.assertEqual(payload["msg"], "模型提供商已删除")
        self.assertEqual(self.business_writes()[0][1], (3,))

    # ---------- 菜单 ----------

    def test_menu_list_zero_time_and_null_perm(self):
        self.menus = [menu_row(created_at=None, perm=None)]
        data = self.client.get("/api/admin/menus", headers=self.platform()).json()["data"]
        self.assertEqual(data[0]["created_at"], "0001-01-01T00:00:00Z")
        self.assertIsNone(data[0]["perm"])

    def test_menu_list_orders_by_group_sort_id(self):
        self.menus = [menu_row()]
        self.client.get("/api/admin/menus", headers=self.platform())
        sql = [q[0] for q in self.queries if "FROM menus" in q[0]][0]
        self.assertIn("ORDER BY group_name ASC, sort ASC, id ASC", sql)

    def test_menu_create_rejects_blank_name_with_real_400(self):
        response = self.client.post("/api/admin/menus", json={"name": "  "},
                                    headers=self.platform())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "参数错误")
        # 真失败 → 审计中间件跳过
        self.assertEqual(self.audit_rows_written, [])

    def test_menu_create_defaults_type_to_two(self):
        payload = self.client.post("/api/admin/menus", json={"name": "新页面"},
                                   headers=self.platform()).json()
        self.assertEqual(payload["data"]["id"], 88)
        _sql, args = self.statements[0]
        self.assertEqual(args[3], 2)                        # type 0 → 2
        # handler 审计 + 中间件审计各一条（object_type 前者 menu、后者 other）
        handler_rows = [w[1] for w in self.writes if "INSERT INTO audit_logs" in w[0]]
        self.assertEqual(len(handler_rows), 2)
        self.assertEqual(handler_rows[0][3:7], ("create", "menu", "新页面", "新增菜单"))
        self.assertEqual(handler_rows[1][4], "other")

    def test_menu_create_missing_parent(self):
        response = self.client.post("/api/admin/menus",
                                    json={"name": "x", "parent_id": 999},
                                    headers=self.platform())
        self.assertEqual(response.json()["msg"], "父菜单不存在")

    def test_menu_create_duplicate_perm(self):
        def boom(sql, args):
            raise main.pymysql.MySQLError(1062, "Duplicate entry 'x' for key 'perm'")
        self.cursor_hook = boom
        response = self.client.post("/api/admin/menus",
                                    json={"name": "x", "perm": "dashboard:view"},
                                    headers=self.platform())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "权限码已存在，请更换")

    def test_menu_update_missing_menu_is_400(self):
        response = self.client.put("/api/admin/menus/999", json={"name": "x"},
                                   headers=self.platform())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "菜单不存在")

    def test_menu_update_rejects_bad_id(self):
        response = self.client.put("/api/admin/menus/abc", json={"name": "x"},
                                   headers=self.platform())
        self.assertEqual(response.json()["msg"], "参数错误")

    def test_menu_update_only_writes_changed_fields(self):
        self.menu_by_id = {1: menu_row()}
        payload = self.client.put("/api/admin/menus/1",
                                  json={"name": "新名字", "sort": 9},
                                  headers=self.platform()).json()
        self.assertEqual(payload["msg"], "菜单已更新")
        sql, args = self.business_writes()[0]
        self.assertIn("name = %s", sql)
        self.assertIn("sort = %s", sql)
        self.assertNotIn("path = %s", sql)
        self.assertEqual(args, ("新名字", 9, 1))
        handler = [w[1] for w in self.writes if "INSERT INTO audit_logs" in w[0]][0]
        self.assertEqual(handler[5], "会话管理")             # object_id 用旧名字

    def test_menu_delete_cascades_and_returns_perms(self):
        self.menu_by_id = {1: menu_row()}
        self.menu_children = {1: [10], 10: [11]}
        self.removed_menus = [menu_row(id=1),
                              menu_row(id=10, parent_id=1, perm="session:view"),
                              menu_row(id=11, parent_id=10, perm="session:profile:edit")]
        payload = self.client.delete("/api/admin/menus/1",
                                     headers=self.platform()).json()
        self.assertEqual(payload["data"]["removed_perms"],
                         ["session:view", "session:profile:edit"])
        deletes = [s for s in self.statements if s[0].startswith("DELETE")]
        self.assertTrue(any("sys_role_menu" in s[0] for s in deletes))
        self.assertTrue(any(s[0].startswith("DELETE FROM menus") for s in deletes))

    def test_menu_batch_update_validation_matrix(self):
        cases = (
            ({"ids": []}, "参数错误"),
            ({"ids": [1], "status": 2}, "状态取值非法"),
            ({"ids": [1], "visible": 3}, "可见性取值非法"),
            ({"ids": [1]}, "缺少更新字段"),
        )
        for body, message in cases:
            response = self.client.post("/api/admin/menus/batch-update", json=body,
                                        headers=self.platform())
            self.assertEqual(response.status_code, 400, body)
            self.assertEqual(response.json()["msg"], message, body)

    def test_menu_batch_update_applies_and_counts(self):
        payload = self.client.post("/api/admin/menus/batch-update",
                                   json={"ids": [1, 2, 3], "status": 0, "sort": 5},
                                   headers=self.platform()).json()
        self.assertEqual(payload["msg"], "已批量更新 3 条菜单")
        sql, args = self.business_writes()[0]
        self.assertIn("WHERE id IN (%s,%s,%s)", sql)
        self.assertEqual(args, (0, 5, 1, 2, 3))

    def test_menu_reset_builtin_reseeds_and_grants_admin(self):
        self.builtin_ids = [1, 2, 110]
        self.admin_role_ids = [2, 8]
        self.biz_menu_ids = [1, 10]
        payload = self.client.post("/api/admin/menus/reset-builtin",
                                   headers=self.platform()).json()
        self.assertEqual(payload["msg"], "内置菜单已重置为初始状态")
        inserts = [s for s in self.statements if s[0].startswith("INSERT INTO menus")]
        self.assertEqual(len(inserts), len(main.BUILTIN_MENUS))
        self.assertEqual(len(inserts), 50)
        grants = [s for s in self.statements
                  if s[0].startswith("INSERT INTO sys_role_menu")]
        self.assertEqual(len(grants), 4)                    # 2 个 admin 角色 × 2 个业务菜单

    def test_builtin_menu_table_matches_go(self):
        ids = [m[0] for m in main.BUILTIN_MENUS]
        self.assertEqual(len(ids), len(set(ids)))           # 主键不重复
        self.assertIn(107, ids)                             # 菜单权限页
        self.assertEqual(sum(1 for m in main.BUILTIN_MENUS if m[6] == "业务中心"), 38)
        self.assertEqual(sum(1 for m in main.BUILTIN_MENUS if m[6] == "系统管理"), 12)


if __name__ == "__main__":
    unittest.main()
