"""Stage 3-2 contract tests: agent status / sessions / customer profiles / customers.

这些用例锁的是「与 Go 逐字段对齐」的那部分行为：
- 失败分支的 HTTP 状态与 body code（Go 大量使用 200 + code）
- 审计中间件的 object_type / object_id 推断（Go auditObjectType 的怪异去尾 s）
- 空列表的 null 与 [] 差异（会话列表 []、客户列表 null）
- 时间序列化带时区偏移
"""

import hashlib
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

import main

SECRET_HASH = hashlib.sha256(b"demo-secret").hexdigest()
THREAD = "t_demo:buyer1:20261020125832_4c57acdd"


class FakeRedis:
    def __init__(self):
        self.values = {}

    def exists(self, key):
        return int(key in self.values)


class Stage32Test(unittest.TestCase):
    def setUp(self):
        self.queries = []
        self.writes = []
        self.redis = FakeRedis()
        self.tenant_platform = 0
        self.user = None
        self.perms = set()
        self.profiles = {}
        self.tags = {}
        self.last_contents = {}
        self.session_rows = []
        self.session_total = 0
        self.agent_rows = [
            {"id": 1, "tenant_id": "t_demo", "user_id": 7, "status": 1,
             "handling_count": 2, "max_concurrent": 3,
             "updated_at": datetime(2026, 10, 20, 9, 0, 0), "nickname": None,
             "username": None},
        ]
        self.messages = [
            {"id": 11, "thread_id": THREAD, "role": "user", "content": "13800138000",
             "msg_type": "text", "tenant_id": "t_demo", "ticket_id": None,
             "is_read": 1, "created_at": datetime(2026, 10, 20, 12, 58, 32, 477000),
             "archived": 0},
            {"id": 12, "thread_id": THREAD, "role": "ai", "content": "已收到",
             "msg_type": "text", "tenant_id": "t_demo", "ticket_id": 9,
             "is_read": 0, "created_at": datetime(2026, 10, 20, 12, 58, 33),
             "archived": 1},
        ]
        self.user_messages = [{"content": "我的电话是 13800138000"}]
        self.customer_rows = []
        self.platform_rules = []

        def one(sql, args=()):
            self.queries.append((sql, args))
            if "FROM tenants" in sql:
                return {"tenant_id": "t_demo", "app_secret_hash": SECRET_HASH,
                        "is_platform_admin": self.tenant_platform}
            if "SELECT id, tenant_id, status FROM users" in sql:
                return self.user
            if "SELECT username" in sql:
                return {"username": "agent01"}
            if "COUNT(DISTINCT m.thread_id)" in sql:
                return {"n": self.session_total}
            # 阶段 3-8：total 包进子查询以共用 handoff 的 HAVING
            if "COUNT(*) AS n FROM (SELECT m.thread_id FROM messages m" in sql:
                return {"n": self.session_total}
            if "COUNT(*) AS n FROM customer_profiles p" in sql:
                return {"n": len(self.customer_rows)}
            if "FROM customer_profiles WHERE thread_id = %s AND deleted_at = 0" in sql:
                return self.profiles.get(args[0])
            if "SELECT id, phone, email FROM customer_profiles" in sql:
                return self.profiles.get(args[0])
            if "SELECT content FROM messages WHERE thread_id = %s ORDER BY id DESC" in sql:
                content = self.last_contents.get(args[0])
                return {"content": content} if content is not None else None
            if "SELECT tags FROM session_tags" in sql:
                value = self.tags.get(args[1])
                return {"tags": value} if value is not None else None
            return None

        def many(sql, args=()):
            self.queries.append((sql, args))
            if "FROM user_roles" in sql:
                return [{"perm": p} for p in sorted(self.perms)]
            if "FROM agent_status AS a" in sql:
                return list(self.agent_rows)
            if "FROM platform_config" in sql:
                return list(self.platform_rules)
            if "FROM customer_profiles p" in sql:
                rows = []
                for row in self.customer_rows:
                    item = dict(row)
                    item.setdefault("last_active_at", None)
                    item.setdefault("message_count", 0)
                    rows.append(item)
                return rows
            if "SELECT thread_id, name, phone, address, email, remark, extract_status" in sql:
                return [{"thread_id": k, **v} for k, v in self.profiles.items()
                        if isinstance(v, dict) and "id" in v]
            if "SELECT thread_id, tags FROM session_tags" in sql:
                return [{"thread_id": k, "tags": v} for k, v in self.tags.items()]
            if "UNION ALL" in sql:
                return [dict(row) for row in self.messages]
            if "SELECT content FROM messages WHERE thread_id = %s AND role = %s" in sql:
                return list(self.user_messages)
            if "GROUP BY m.thread_id" in sql:
                return list(self.session_rows)
            return []

        self.patches = (
            patch.object(main, "one", side_effect=one),
            patch.object(main, "many", side_effect=many),
            patch.object(main, "write",
                         side_effect=lambda sql, args: self.writes.append((sql, args))),
            patch.object(main, "cache", return_value=self.redis),
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

    def first_write(self, needle):
        return [w for w in self.writes if needle in w[0]][0]

    def audited(self):
        return [w for w in self.writes if "INSERT INTO audit_logs" in w[0]]

    # ---------- 时间序列化 ----------

    def test_iso_matches_go_rfc3339(self):
        aware = datetime(2026, 10, 20, 21, 3, 17, 123000,
                         tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(main.iso(aware), "2026-10-20T21:03:17.123+08:00")
        whole = datetime(2026, 10, 20, 21, 3, 17, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(main.iso(whole), "2026-10-20T21:03:17+08:00")  # 微秒为 0 省略
        self.assertIsNone(main.iso(None))

    # ---------- 在线客服状态 ----------

    def test_agent_status_list_scopes_and_orders(self):
        response = self.client.get("/api/admin/agent-status", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        sql, args = [q for q in self.queries if "FROM agent_status AS a" in q[0]][0]
        self.assertIn("WHERE a.tenant_id = %s", sql)
        self.assertIn("ORDER BY a.status DESC, a.handling_count ASC, a.id ASC", sql)
        self.assertIn("LEFT JOIN users u ON u.id = a.user_id AND u.deleted_at = 0", sql)
        self.assertEqual(args, ("t_demo",))
        row = response.json()["data"][0]
        self.assertEqual(row["nickname"], "")       # LEFT JOIN 未命中 → 空串而非 null
        self.assertTrue(row["updated_at"].endswith("+08:00"))

    def test_agent_status_update_rejects_app_key_admin(self):
        response = self.client.put("/api/admin/agent-status", headers=self.auth(),
                                   json={"status": 1})
        self.assertEqual(response.status_code, 403)
        self.assertIn("app_key 管理员无需设置状态", response.json()["msg"])
        self.assertFalse(any("UPDATE agent_status" in s for s, _ in self.writes))

    def test_agent_status_update_validates_body_in_go_shape(self):
        self.user = {"id": 7, "tenant_id": "t_demo", "status": 1}
        self.perms = {"ticket:handle"}
        for body in ({}, {"status": 9}, {"status": "1"}, {"status": True}):
            with self.subTest(body=body):
                response = self.client.put("/api/admin/agent-status",
                                           headers=self.auth(uid=7), json=body)
                # Go 写 200 + code 400，但全局 StatusCode 中间件按 body.code 改写为 400
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], 400)
        self.assertFalse(any("UPDATE agent_status" in s for s, _ in self.writes))

    def test_agent_status_update_writes_only_self(self):
        self.user = {"id": 7, "tenant_id": "t_demo", "status": 1}
        self.perms = {"ticket:handle"}
        response = self.client.put("/api/admin/agent-status", headers=self.auth(uid=7),
                                   json={"status": 2})
        self.assertEqual(response.status_code, 200)
        sql, args = self.first_write("UPDATE agent_status")
        self.assertIn("updated_at = NOW()", sql)
        self.assertEqual(args, (2, "t_demo", 7))
        audit = self.audited()
        self.assertEqual(audit[0][1][3], "update")
        self.assertEqual(audit[0][1][4], "other")   # agent-status 去掉尾 s 后不命中白名单

    # ---------- 会话列表 ----------

    def test_session_list_pagination_is_clamped(self):
        response = self.client.get("/api/admin/sessions?page=0&size=500",
                                   headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["page"], 1)
        self.assertEqual(response.json()["data"]["size"], 20)

    def test_session_list_filters_compose_in_go_order(self):
        self.client.get("/api/admin/sessions?q=张三&start=2026-10-01&end=2026-10-20"
                        "&handoff=1&tag=投诉", headers=self.auth())
        sql, args = [q for q in self.queries
                     if "COUNT(*) AS n FROM (SELECT m.thread_id FROM messages m" in q[0]][0]
        self.assertIn("(m.thread_id LIKE %s OR cp.name LIKE %s OR cp.phone LIKE %s "
                      "OR cp.email LIKE %s OR cp.address LIKE %s)", sql)
        self.assertIn("m.created_at >= %s", sql)
        self.assertIn("m.created_at < %s", sql)
        self.assertIn("m.thread_id IN (SELECT thread_id FROM session_tags", sql)
        self.assertEqual(args, ("t_demo", "%张三%", "%张三%", "%张三%", "%张三%", "%张三%",
                                "2026-10-01 00:00:00", "2026-10-20 23:59:59",
                                "t_demo", "%投诉%"))
        main_sql, main_args = [q for q in self.queries
                               if "GROUP BY m.thread_id" in q[0]
                               and not q[0].startswith("SELECT COUNT(*) AS n FROM (")][0]
        self.assertIn("HAVING COALESCE(MAX(CASE WHEN t.status IN (0,1) THEN 1 ELSE 0 END), 0) = 1",
                      main_sql)
        self.assertEqual(main_args[-2:], (20, 0))
        # 阶段 3-8 修复：Go 的 total 不带 handoffFilter，导致筛选后 total 虚高
        self.assertIn("HAVING", sql)

    def test_session_list_item_shape_and_profile_merge(self):
        self.session_rows = [{"thread_id": THREAD, "tenant_id": "t_demo",
                              "message_count": 2, "user_count": 1, "unread_count": 0,
                              "last_active_at": datetime(2026, 10, 20, 20, 58, 32, 477000),
                              "has_open_ticket": 1, "summary": None}]
        self.session_total = 1
        self.last_contents[THREAD] = "已提交人工客服"
        self.profiles[THREAD] = {"id": 3, "thread_id": THREAD, "tenant_id": "t_demo",
                                 "name": "张三", "phone": "13800138000", "address": "",
                                 "email": "", "remark": "", "extract_status": 2,
                                 "created_at": None, "updated_at": None}
        self.tags[THREAD] = "投诉,发票"
        data = self.client.get("/api/admin/sessions", headers=self.auth()).json()["data"]
        item = data["list"][0]
        self.assertEqual(set(item), {"thread_id", "tenant_id", "message_count", "user_count",
                                    "unread_count", "last_content", "last_active_at",
                                    "has_open_ticket", "name", "phone", "address", "email",
                                    "remark", "extract_status", "tags", "summary"})
        self.assertTrue(item["has_open_ticket"])         # 0/1 → true/false
        self.assertEqual(item["last_content"], "已提交人工客服")
        self.assertEqual(item["name"], "张三")
        self.assertEqual(item["tags"], "投诉,发票")
        self.assertEqual(item["summary"], "")            # NULL → 空串
        self.assertEqual(item["last_active_at"], "2026-10-20T20:58:32.477+08:00")

    def test_session_list_returns_empty_array_not_null(self):
        data = self.client.get("/api/admin/sessions", headers=self.auth()).json()["data"]
        self.assertEqual(data["list"], [])

    # ---------- 会话聊天记录 ----------

    def test_session_messages_merges_archive_and_omits_null_ticket(self):
        response = self.client.get(f"/api/admin/sessions/{THREAD}/messages",
                                   headers=self.auth())
        self.assertEqual(response.status_code, 200)
        messages = response.json()["data"]["messages"]
        self.assertEqual([m["id"] for m in messages], [11, 12])
        self.assertNotIn("ticket_id", messages[0])        # Go omitempty：NULL 时省略
        self.assertEqual(messages[1]["ticket_id"], 9)
        self.assertEqual(messages[1]["archived"], True)
        self.assertEqual(messages[0]["archived"], False)
        sql, args = [q for q in self.queries if "UNION ALL" in q[0]][0]
        self.assertEqual(args, (THREAD, THREAD))
        self.assertIsNone(response.json()["data"]["profile"])

    def test_session_messages_rejects_foreign_thread(self):
        response = self.client.get("/api/admin/sessions/t_other:buyer1:x/messages",
                                   headers=self.auth())
        self.assertEqual(response.status_code, 403)
        self.assertIn("无权访问该租户会话", response.json()["msg"])
        self.assertFalse(any("UNION ALL" in s for s, _ in self.queries))

    def test_platform_admin_is_rejected_on_every_session_endpoint(self):
        self.tenant_platform = 1
        for method, path, body in (
            ("get", "/api/admin/sessions", None),
            ("get", f"/api/admin/sessions/{THREAD}/messages", None),
            ("get", f"/api/admin/sessions/{THREAD}/tags", None),
            ("put", f"/api/admin/sessions/{THREAD}/tags", {"tags": "x"}),
            ("put", f"/api/admin/sessions/{THREAD}/profile", {"name": "x"}),
            ("post", f"/api/admin/sessions/{THREAD}/reextract", None),
            ("get", "/api/admin/customers", None),
            ("get", "/api/admin/agent-status", None),
        ):
            with self.subTest(path=path, method=method):
                kwargs = {"json": body} if body is not None else {}
                response = getattr(self.client, method)(path, headers=self.auth(),
                                                        **kwargs)
                self.assertEqual(response.status_code, 403)
                self.assertIn("平台管理员不可访问租户业务数据", response.json()["msg"])

    # ---------- 客户档案 ----------

    def test_profile_upsert_marks_manual_and_audits(self):
        response = self.client.put(f"/api/admin/sessions/{THREAD}/profile",
                                   headers=self.auth(),
                                   json={"name": "张三", "phone": "13800138000",
                                         "address": "北京", "email": "a@b.com",
                                         "remark": "VIP"})
        self.assertEqual(response.status_code, 200)
        sql, args = self.first_write("INSERT INTO customer_profiles")
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertIn("extract_status = 2", sql)
        self.assertEqual(args[:7], (THREAD, "t_demo", "张三", "13800138000", "北京",
                                    "a@b.com", "VIP"))
        audit = self.audited()[0][1]
        self.assertEqual(audit[3:6], ("update", "session", "profile"))

    def test_profile_rejects_malformed_body(self):
        response = self.client.put(f"/api/admin/sessions/{THREAD}/profile",
                                   headers=self.auth(), content=b"not-json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], 400)
        self.assertFalse(any("INSERT INTO customer_profiles" in s for s, _ in self.writes))

    # ---------- 重新提取 ----------

    def test_reextract_extracts_phone_and_keeps_manual_fields(self):
        self.user_messages = [{"content": "我的电话是 13800138000"}]
        self.profiles[THREAD] = {"id": 3, "phone": "", "email": ""}
        response = self.client.post(f"/api/admin/sessions/{THREAD}/reextract",
                                    headers=self.auth())
        self.assertEqual(response.status_code, 200)
        sql, args = self.first_write("UPDATE customer_profiles SET phone")
        self.assertEqual(args, ("13800138000", "", 3))

    def test_reextract_ignores_long_digit_runs(self):
        # Go 侧手工校验数字边界：从 unixNano 时间戳里不能截出 11 位手机号
        self.user_messages = [{"content": "https://x/1759999999999999999.png"}]
        self.profiles.pop(THREAD, None)
        self.client.post(f"/api/admin/sessions/{THREAD}/reextract", headers=self.auth())
        self.assertFalse(any("INSERT INTO customer_profiles" in s for s, _ in self.writes))

    def test_extract_rules_fall_back_to_defaults(self):
        self.assertEqual(main.extract_rules(), main.DEFAULT_EXTRACT_RULES)
        self.platform_rules = [{"config_key": "cs_extract_rule_phone",
                                "config_value": r"1[3-9]\d{9}"}]
        self.assertEqual(main.extract_rules(), (("phone", r"1[3-9]\d{9}"),))

    # ---------- 会话标签 ----------

    def test_tags_get_returns_empty_when_absent(self):
        data = self.client.get(f"/api/admin/sessions/{THREAD}/tags",
                               headers=self.auth()).json()["data"]
        self.assertEqual(data, {"thread_id": THREAD, "tags": ""})

    def test_tags_put_upserts_and_strips(self):
        response = self.client.put(f"/api/admin/sessions/{THREAD}/tags",
                                   headers=self.auth(), json={"tags": "  投诉,发票  "})
        self.assertEqual(response.status_code, 200)
        sql, args = self.first_write("INSERT INTO session_tags")
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertEqual(args, ("t_demo", THREAD, "投诉,发票", "投诉,发票"))

    # ---------- 客户名册 ----------

    def test_customers_returns_null_list_when_empty(self):
        data = self.client.get("/api/admin/customers?q=张", headers=self.auth()).json()["data"]
        self.assertIsNone(data["list"])          # Go: var items []T → null
        self.assertEqual(data["q"], "张")

    def test_customers_rows_include_activity_and_q(self):
        self.customer_rows = [{"thread_id": THREAD, "tenant_id": "t_demo", "name": "张三",
                               "phone": "13800138000", "address": "北京",
                               "email": "a@b.com", "remark": "", "extract_status": 2,
                               "created_at": datetime(2026, 10, 1, 8, 0, 0),
                               "updated_at": datetime(2026, 10, 2, 8, 0, 0)}]
        data = self.client.get("/api/admin/customers?page=2&size=5&q=张",
                               headers=self.auth()).json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["page"], 2)
        self.assertEqual(data["size"], 5)
        row = data["list"][0]
        # 阶段 3-8 修复：Go 侧 msg_count→MessageCount 映射不上恒为 0，这里按 thread
        # 统计 messages + messages_archive；本用例未提供计数 → 0
        self.assertEqual(row["message_count"], 0)
        self.assertIsNone(row["last_active_at"])
        self.assertEqual(row["updated_at"], "2026-10-02T08:00:00+08:00")
        sql, args = [q for q in self.queries if "FROM customer_profiles p" in q[0]
                     and "LIMIT %s OFFSET %s" in q[0]][0]
        self.assertEqual(args[:2], ("t_demo", "t_demo"))   # 子查询两个 tenant_id 在前
        self.assertEqual(args[-2:], (5, 5))
        # 阶段 3-8 修复：message_count 按 thread 统计 messages + messages_archive
        self.assertIn("COUNT(*) FROM messages WHERE thread_id = p.thread_id", sql)
        self.assertIn("COUNT(*) FROM messages_archive WHERE thread_id = p.thread_id", sql)

    # ---------- 审计中间件 ----------

    def test_audit_middleware_skips_reads_and_real_failures(self):
        self.client.get("/api/admin/sessions", headers=self.auth())
        self.client.put("/api/admin/sessions/t_other:x/y/tags", headers=self.auth(),
                        json={"tags": "x"})                        # HTTP 403 → 不记
        self.assertEqual(self.audited(), [])

    def test_audit_middleware_records_soft_failures_like_go(self):
        """Go 的 200 + body code 会被审计中间件计入（Writer.Status() < 400）。"""
        self.client.put(f"/api/admin/sessions/{THREAD}/tags", headers=self.auth(),
                        content=b"not-json")
        audit = self.audited()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0][1][:6], ("t_demo", "t_demo", 0, "update", "session", "tags"))

    def test_audit_object_type_matches_go_segment_trimming(self):
        self.assertEqual(main.audit_object_type("/api/admin/sessions/abc/profile"), "session")
        self.assertEqual(main.audit_object_type("/api/admin/tickets/12/reply"), "ticket")
        self.assertEqual(main.audit_object_type("/api/admin/tenants/t_demo"), "tenant")
        self.assertEqual(main.audit_object_type("/api/admin/model-config"), "model_config")
        self.assertEqual(main.audit_object_type("/api/admin/quick-replies"), "other")
        # kb 路径首段是 kb，Go 只取首段 → other（knowledge 分支实际只在 /knowledge/... 命中）
        self.assertEqual(main.audit_object_type("/api/admin/kb/documents"), "other")
        self.assertEqual(main.audit_object_type("/api/admin/knowledge/doc"), "knowledge")


if __name__ == "__main__":
    unittest.main()
