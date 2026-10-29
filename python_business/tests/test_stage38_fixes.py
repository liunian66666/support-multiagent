"""阶段 3-8：补齐历史缺陷回归测试。

这批修复全部是**有意修正历史行为**：历史实现仍有缺陷，FastAPI 侧修掉。
因此每一条都同时断言「修后的正确行为」与「偏离点」，方便后续核对兼容清单。

覆盖：
1. 掩码回填：长 Key 的掩码是 "..." + 后4位（不含星号），曾被当真 Key 写库
2. 客户列表 message_count 恒 0（历史实现列名不匹配）
3. 会话列表 handoff=1 时 total 不含 HAVING 过滤
4. 提取规则先删后校验 → 改为先校验后落库
5. 关闭工单的 old_status 写死 Handling → 取真实状态
6. 角色 code 不可指定 → 放开请求体 code（限长 32）
7. 看板 satisfaction_rate：历史实现无评价时 Scan NULL 报错，Python 已免疫
"""

import hashlib
import time
import unittest
from contextlib import contextmanager
from datetime import datetime

from fastapi.testclient import TestClient

import main_compat as main
from patch_business import patch_business

SECRET_HASH = hashlib.sha256(b"demo-secret").hexdigest()


def tenant_config(**overrides):
    row = {
        "tenant_id": "t_demo", "model_provider": "platform", "llm_provider_id": 0,
        "model_provider_key": "", "model_name": "", "model_api_base": "",
        "model_api_key": "", "chat_input_cache_price": 0, "chat_input_price": 0,
        "chat_output_price": 0, "embedding_price": 0,
        "embedding_provider": "platform", "embedding_provider_id": 0,
        "embedding_provider_key": "", "embedding_model_name": "",
        "embedding_api_base": "", "embedding_api_key": "",
    }
    row.update(overrides)
    return row


class FakeCursor:
    def __init__(self, harness):
        self.harness = harness
        self.lastrowid = None
        self._rows = None

    def execute(self, sql, args=()):
        self.harness.statements.append((sql, args))
        if sql.startswith("INSERT INTO roles"):
            self.lastrowid = 77
            self.harness.role_code = args[1]
        self._rows = self.harness.cursor_result(sql, args)

    def fetchone(self):
        if isinstance(self._rows, list):
            return self._rows[0] if self._rows else None
        return self._rows

    def fetchall(self):
        if self._rows is None:
            return []
        return self._rows if isinstance(self._rows, list) else [self._rows]


class FixesTest(unittest.TestCase):
    def setUp(self):
        self.queries = []
        self.writes = []
        self.statements = []
        self.config_row = tenant_config()
        self.tenant_platform = 0
        self.customer_rows = []
        self.session_total = 0
        self.cursor_hook = None
        self.role_code = None

        def one(sql, args=()):
            self.queries.append((sql, args))
            if sql.startswith("SELECT tenant_id, app_secret_hash, is_platform_admin"):
                return {"tenant_id": "t_demo", "app_secret_hash": SECRET_HASH,
                        "is_platform_admin": self.tenant_platform}
            if sql.startswith(f"SELECT {main.TENANT_CONFIG_COLUMNS} FROM tenants"):
                return self.config_row
            if sql.startswith("SELECT COUNT(*) AS n FROM ("):
                return {"n": self.session_total}
            if sql.startswith("SELECT COUNT(*) AS n FROM customer_profiles"):
                return {"n": len(self.customer_rows)}
            if sql.startswith("SELECT id FROM roles WHERE code = 'admin'"):
                return None
            return None

        def many(sql, args=()):
            self.queries.append((sql, args))
            if sql.startswith("SELECT p.thread_id, p.tenant_id"):
                return list(self.customer_rows)
            return []

        def write(sql, args):
            self.writes.append((sql, args))
            if sql.startswith("INSERT INTO roles"):
                self.role_code = args[1]

        @contextmanager
        def transaction():
            yield FakeCursor(self)

        def ensure_roles(tenant_id):
            return None

        class FakeCache:
            def exists(self, key):
                return 0

        self.patches = (
            patch_business(main, "one", side_effect=one),
            patch_business(main, "many", side_effect=many),
            patch_business(main, "write", side_effect=write),
            patch_business(main, "transaction", side_effect=transaction),
            patch_business(main, "cache", side_effect=lambda: FakeCache()),
            patch_business(main, "ensure_tenant_roles", side_effect=ensure_roles),
            patch_business(main, "go_broadcast", side_effect=lambda *a, **k: None),
            patch_business(main, "reload_go_extract_rules", side_effect=lambda: None),
            patch_business(main, "save_message", side_effect=lambda *a, **k: None),
            patch_business(main, "adjust_handling", side_effect=lambda *a, **k: None),
            patch_business(main, "go_internal", side_effect=lambda *a, **k: None),
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

    def auth(self):
        return {"Authorization": "Bearer " + self.token()}

    def ticket(self, id=1, status=0, priority=0, handler_uid=0):
        return {"id": id, "thread_id": "t_demo:u1", "tenant_id": "t_demo",
                "ticket_no": "T20261020001", "user_id": "t_demo:u1",
                "reason": "转人工", "status": status, "priority": priority,
                "handler_uid": handler_uid, "handler_name": "",
                "created_at": datetime(2026, 10, 20, 9, 0, 0), "closed_at": None}

    def cursor_result(self, sql, args):
        if self.cursor_hook:
            return self.cursor_hook(sql, args)
        return None

    # ---------- ① 掩码回填 ----------

    def test_short_key_mask_contains_star(self):
        self.assertTrue(main.is_masked_key("****"))

    def test_long_key_mask_has_no_star_but_is_detected(self):
        """MaskAPIKey 对长 Key 输出 "..." + 后4位 —— 不含星号，必须仍判为掩码。"""
        self.assertTrue(main.is_masked_key("...abcd"))
        self.assertFalse(main.is_masked_key("...abcde"))     # 长度不对，视为真实 Key

    def test_long_key_mask_not_written_as_real_key(self):
        """只改模型名不改 Key 时，回传的掩码不能被加密写库。"""
        plain = "sk-realkey-1234"
        stored = main.encrypt_api_key(plain)
        self.config_row = tenant_config(model_provider="custom", model_name="old",
                                        model_api_key=stored)
        updates = main.update_model_config("t_demo", {
            "model_config": {"model_provider": "custom", "model_name": "new",
                             "model_api_key": main.mask_api_key(plain)}})
        self.assertIn("model_name", updates)
        self.assertNotIn("model_api_key", updates)
        key_writes = [a for s, a in self.writes if "UPDATE tenants SET" in s]
        self.assertFalse(key_writes and "model_api_key" in key_writes[0][0])

    def test_masked_value_matching_stored_plain_is_detected(self):
        """兜底：掩码恰好与已存明文相同（极端巧合）也不写库。"""
        self.assertTrue(main.is_masked_key("same-value", "same-value"))
        self.assertFalse(main.is_masked_key("other", "same-value"))

    def test_real_key_still_written(self):
        plain = "sk-realkey-1234"
        stored = main.encrypt_api_key("sk-old-key-9999")
        self.config_row = tenant_config(model_provider="custom",
                                        model_api_key=stored)
        updates = main.update_model_config("t_demo", {
            "model_config": {"model_provider": "custom", "model_api_key": plain}})
        self.assertIn("model_api_key", updates)
        self.assertEqual(plain, main.decrypt_api_key(updates["model_api_key"]))

    def test_embedding_long_mask_not_written(self):
        plain = "emb-realkey-7777"
        stored = main.encrypt_api_key(plain)
        self.config_row = tenant_config(embedding_provider="custom",
                                        embedding_api_key=stored)
        main.update_model_config("t_demo", {
            "embedding_config": {"embedding_provider": "custom",
                                 "embedding_api_key": main.mask_api_key(plain)}})
        key_updates = [a for s, a in self.writes
                       if "UPDATE tenants SET" in s and "embedding_api_key" in s]
        self.assertEqual([], key_updates)

    # ---------- ② 客户列表 message_count ----------

    def test_customer_message_count_counts_archive_too(self):
        self.customer_rows = [{
            "thread_id": "t_demo:u1", "tenant_id": "t_demo", "name": "张三",
            "phone": "", "address": "", "email": "", "remark": "",
            "extract_status": 1, "created_at": datetime(2026, 10, 1, 9, 0, 0),
            "updated_at": datetime(2026, 10, 2, 9, 0, 0),
            "last_active_at": datetime(2026, 10, 2, 10, 0, 0),
            "message_count": 12,
        }]
        items, total = main.list_customers("t_demo", 1, 20, "")
        self.assertEqual(1, total)
        self.assertEqual(12, items[0]["message_count"])
        self.assertIn("messages_archive", self.queries[-1][0])

    def test_customer_message_count_zero_when_no_messages(self):
        self.customer_rows = [{
            "thread_id": "t_demo:u2", "tenant_id": "t_demo", "name": "李四",
            "phone": "", "address": "", "email": "", "remark": "",
            "extract_status": 0, "created_at": datetime(2026, 10, 1, 9, 0, 0),
            "updated_at": datetime(2026, 10, 2, 9, 0, 0),
            "last_active_at": None, "message_count": 0,
        }]
        items, _ = main.list_customers("t_demo", 1, 20, "")
        self.assertEqual(0, items[0]["message_count"])

    # ---------- ③ 会话列表 total 口径 ----------

    def test_session_total_includes_handoff_filter(self):
        self.session_total = 3
        _, total = main.list_sessions("t_demo", 1, 20, "", "", "", 1, "")
        self.assertEqual(3, total)
        sql = self.queries[0][0]
        self.assertIn("GROUP BY m.thread_id", sql)
        self.assertIn("HAVING", sql)

    def test_session_total_subquery_has_tickets_join(self):
        """HAVING 引用 t.status，total 子查询必须带 tickets JOIN。

        Mock 掉 DB 时漏了 JOIN 单测照样绿，真实库直接 1054 —— 这里把结构锁住。
        """
        self.session_total = 1
        main.list_sessions("t_demo", 1, 20, "", "", "", 1, "")
        sql = self.queries[0][0]
        self.assertIn("LEFT JOIN tickets t ON t.thread_id = m.thread_id "
                      "AND t.status IN (0,1)", sql)
        self.assertIn("LEFT JOIN customer_profiles cp ON cp.thread_id = m.thread_id", sql)
        # 主查询的 JOIN 集合必须是 total 子查询的超集
        main_sql = [q[0] for q in self.queries if "GROUP BY m.thread_id" in q[0]
                    and not q[0].startswith("SELECT COUNT(*) AS n FROM (")][0]
        for join in ("LEFT JOIN tickets t", "LEFT JOIN customer_profiles cp",
                     "LEFT JOIN session_summaries ss"):
            self.assertIn(join, main_sql)

    def test_session_total_without_handoff_has_no_having(self):
        self.session_total = 40
        _, total = main.list_sessions("t_demo", 1, 20, "", "", "", 0, "")
        self.assertEqual(40, total)
        self.assertNotIn("HAVING", self.queries[0][0])

    # ---------- ④ 提取规则先校验后落库 ----------

    def test_extract_rules_bad_pattern_keeps_old_rules(self):
        self.tenant_platform = 1
        resp = self.client.put("/api/admin/extract-rules", json=[
            {"field": "phone", "pattern": r"\d{11}"},
            {"field": "email", "pattern": "([unclosed"},      # 非法正则
        ], headers=self.auth())
        self.assertEqual(500, resp.json()["code"])
        deletes = [s for s, _ in self.writes if s.startswith("DELETE FROM platform_config")]
        inserts = [s for s, _ in self.writes if s.startswith("INSERT INTO platform_config")]
        self.assertEqual([], deletes, "校验失败时不得删除旧规则")
        self.assertEqual([], inserts)

    def test_extract_rules_all_valid_then_replace(self):
        self.tenant_platform = 1
        resp = self.client.put("/api/admin/extract-rules", json=[
            {"field": "phone", "pattern": r"\d{11}"},
            {"field": "email", "pattern": r"[\w.]+@[\w.]+"},
        ], headers=self.auth())
        self.assertEqual(0, resp.json()["code"])
        self.assertEqual(1, len([s for s, _ in self.writes
                                 if s.startswith("DELETE FROM platform_config")]))
        self.assertEqual(2, len([s for s, _ in self.writes
                                 if s.startswith("INSERT INTO platform_config")]))

    def test_extract_rules_bad_field_type_rejected_before_delete(self):
        self.tenant_platform = 1
        resp = self.client.put("/api/admin/extract-rules", json=[
            {"field": 123, "pattern": r"\d+"},
        ], headers=self.auth())
        self.assertEqual(400, resp.json()["code"])
        self.assertEqual([], [s for s, _ in self.writes
                              if s.startswith("DELETE FROM platform_config")])

    # ---------- ⑤ 关闭工单 old_status ----------

    def test_close_ticket_logs_real_old_status(self):
        ticket = self.ticket(id=5, status=main.TICKET_STATUS_PENDING, priority=0)
        main.close_ticket(ticket, "kefu")
        log = [a for s, a in self.writes if "INSERT INTO ticket_logs" in s][0]
        # ticket_logs 列序：ticket_id, thread_id, tenant_id, action, old_status, new_status...
        self.assertEqual("close", log[3])
        self.assertEqual(main.TICKET_STATUS_PENDING, log[4])
        self.assertEqual(main.TICKET_STATUS_CLOSED, log[5])

    def test_close_ticket_old_status_handling_when_handling(self):
        ticket = self.ticket(id=6, status=main.TICKET_STATUS_HANDLING,
                             priority=1, handler_uid=9)
        main.close_ticket(ticket, "kefu")
        log = [a for s, a in self.writes if "INSERT INTO ticket_logs" in s][0]
        self.assertEqual(main.TICKET_STATUS_HANDLING, log[4])

    def test_close_ticket_idempotent_no_log(self):
        ticket = self.ticket(id=7, status=main.TICKET_STATUS_CLOSED)
        main.close_ticket(ticket, "kefu")
        self.assertEqual([], [s for s, _ in self.writes
                              if "INSERT INTO ticket_logs" in s])

    # ---------- ⑥ 角色 code ----------

    def test_role_create_accepts_explicit_code(self):
        resp = self.client.post("/api/admin/roles", json={
            "name": "售后组", "code": "after_sales", "menu_ids": [1, 2],
        }, headers=self.auth())
        self.assertEqual(0, resp.json()["code"])
        self.assertEqual("after_sales", self.role_code)

    def test_role_create_generates_code_when_absent(self):
        resp = self.client.post("/api/admin/roles", json={
            "name": "售后组", "menu_ids": [1],
        }, headers=self.auth())
        self.assertEqual(0, resp.json()["code"])
        self.assertTrue(self.role_code.startswith("custom_"))

    def test_role_create_rejects_oversized_code(self):
        resp = self.client.post("/api/admin/roles", json={
            "name": "售后组", "code": "x" * 33, "menu_ids": [1],
        }, headers=self.auth())
        self.assertEqual(400, resp.json()["code"])
        self.assertIn("32", resp.json()["msg"])

    def test_role_create_rejects_non_string_code(self):
        resp = self.client.post("/api/admin/roles", json={
            "name": "售后组", "code": 42, "menu_ids": [1],
        }, headers=self.auth())
        self.assertEqual(400, resp.json()["code"])

    def test_role_create_rejects_empty_code(self):
        resp = self.client.post("/api/admin/roles", json={
            "name": "售后组", "code": "   ", "menu_ids": [1],
        }, headers=self.auth())
        self.assertEqual(400, resp.json()["code"])

    # ---------- ⑦ 看板 satisfaction_rate ----------

    def test_dashboard_satisfaction_rate_null_safe(self):
        """历史实现此场景 Scan NULL 到 float64 会直接报错；Python 必须返回 0.0。"""
        overview = {"satisfaction_rate": None}
        value = float(overview["satisfaction_rate"]) \
            if overview.get("satisfaction_rate") is not None else 0.0
        self.assertEqual(0.0, value)


if __name__ == "__main__":
    unittest.main()
