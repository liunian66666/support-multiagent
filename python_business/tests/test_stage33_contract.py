"""Stage 3-3 contract tests: ticket domain (list/detail/messages/logs/read + 状态流转).

重点锁三件事：
1. 错误分支的 code 与文案（Go 大量用 body code 表达业务失败）
2. WS 推送与 Agent resume 的调用点、调用参数（跨进程后最容易漏掉的部分）
3. 事务外可观察的副作用序列：工单状态、处理量、操作日志、系统消息
"""

import hashlib
import os
import time
import unittest
from datetime import datetime, timedelta
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


def ticket_row(**overrides):
    row = {"id": 7, "thread_id": THREAD, "ticket_no": "TK-abc12345",
           "user_id": THREAD, "reason": "转人工", "status": 0, "priority": 1,
           "tenant_id": "t_demo", "handler_uid": 0, "handler_name": "",
           "closed_at": None,
           "created_at": datetime(2026, 10, 20, 12, 58, 32, 477000)}
    row.update(overrides)
    return row


class Stage33Test(unittest.TestCase):
    def setUp(self):
        self.queries = []
        self.writes = []
        self.redis = FakeRedis()
        self.tenant_platform = 0
        self.user = None
        self.perms = set()
        self.ticket = ticket_row()
        self.target_user = {"id": 9, "tenant_id": "t_demo", "nickname": "小李",
                            "username": "li", "status": 1}
        self.agent_row = {"id": 3, "status": 1, "handling_count": 1, "max_concurrent": 3}
        self.ticket_rows = []
        self.messages = []
        self.archived = []
        self.logs = []
        self.posts = []
        self.ws_messages = []
        self.resume_reply = "已接管，您好"

        def one(sql, args=()):
            self.queries.append((sql, args))
            if "FROM tenants" in sql:
                return {"tenant_id": "t_demo", "app_secret_hash": SECRET_HASH,
                        "is_platform_admin": self.tenant_platform}
            if "SELECT id, tenant_id, status FROM users" in sql:
                return self.user
            if "SELECT username" in sql:
                return {"username": "agent01"}
            if "SELECT nickname, username FROM users" in sql:
                return {"nickname": "小王", "username": "wang"}
            if "FROM tickets WHERE id = %s" in sql:
                return self.ticket if args[0] == self.ticket["id"] else None
            if "FROM tickets WHERE ticket_no" in sql:
                return self.ticket if args[0] == self.ticket["ticket_no"] else None
            if "FROM users WHERE id = %s" in sql:
                return self.target_user if args[0] == self.target_user["id"] else None
            if "COUNT(*) AS n FROM messages" in sql:
                return {"n": 2}
            if "COUNT(*) AS n FROM tickets" in sql:
                return {"n": len(self.ticket_rows)}
            if "FROM agent_status" in sql:
                return self.agent_row
            return None

        def many(sql, args=()):
            self.queries.append((sql, args))
            if "FROM user_roles" in sql:
                return [{"perm": p} for p in sorted(self.perms)]
            if "FROM tickets WHERE" in sql:
                return list(self.ticket_rows)
            if "FROM messages WHERE thread_id = %s AND created_at" in sql:
                return list(self.messages)
            if "FROM messages_archive" in sql:
                return list(self.archived)
            if "FROM ticket_logs" in sql:
                return list(self.logs)
            if "FROM users WHERE tenant_id" in sql:
                return [{"id": 9, "nickname": "小李", "username": "li"}]
            return []

        def post(url, json=None, headers=None, timeout=None):
            self.posts.append((url, json or {}, headers or {}))
            response = type("R", (), {})()
            if url.endswith("/agent/resume"):
                if isinstance(self.resume_reply, Exception):
                    raise self.resume_reply
                response.json = lambda: {"content": self.resume_reply}
            else:
                response.json = lambda: {"code": 0}
            return response

        self.patches = (
            patch.object(main, "one", side_effect=one),
            patch.object(main, "many", side_effect=many),
            patch.object(main, "write",
                         side_effect=lambda sql, args: self.writes.append((sql, args))),
            patch.object(main, "cache", return_value=self.redis),
            patch.object(main.httpx, "post", side_effect=post),
            patch.object(main.ws_hub, "broadcast_from_thread",
                         side_effect=lambda target, payload, key="", tenant_id="":
                         self.ws_messages.append(("python", {
                             "target": target, "payload": payload,
                             "key": key, "tenant_id": tenant_id}))),
            patch.dict(os.environ, {"INTERNAL_API_TOKEN": "internal-test",
                                         "USE_PYTHON_WS_HUB": "true"}),
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

    def business_writes(self):
        """排除审计日志：Go 的 AuditMiddleware 挂在 StatusCode 内层，
        读到的 status 恒为 200，所以软失败（body code 报错）的写请求照样落审计。
        断言「没有业务写」时必须把 audit_logs 摘出去。"""
        return [w for w in self.writes if "INSERT INTO audit_logs" not in w[0]]

    def broadcasts(self):
        return self.ws_messages

    def resumes(self):
        return [p for p in self.posts if p[0].endswith("/agent/resume")]

    # ---------- 列表 ----------

    def test_list_filters_and_pagination(self):
        self.ticket_rows = [ticket_row()]
        response = self.client.get(
            "/api/admin/tickets?status=1&priority=2&q=TK-&page=0&size=999",
            headers=self.auth())
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual((data["page"], data["size"]), (1, 20))
        sql, args = self.writes and (None, None) or (None, None)
        clause_sql, clause_args = [q for q in self.queries if "COUNT(*) AS n FROM tickets" in q[0]][0]
        self.assertIn("deleted_at = 0", clause_sql)
        self.assertIn("status = %s", clause_sql)
        self.assertIn("priority = %s", clause_sql)
        self.assertIn("(ticket_no LIKE %s OR user_id LIKE %s OR handler_name LIKE %s)", clause_sql)
        self.assertEqual(clause_args, ("t_demo", 1, "%TK-%", "%TK-%", "%TK-%", 2))
        list_sql, list_args = [q for q in self.queries
                               if "ORDER BY created_at DESC, id DESC" in q[0]][0]
        self.assertIn("FROM tickets WHERE", list_sql)
        self.assertIn("ORDER BY created_at DESC, id DESC", list_sql)
        self.assertEqual(list_args[-2:], (20, 0))

    def test_list_ignores_invalid_status_and_priority(self):
        self.client.get("/api/admin/tickets?status=abc&priority=9", headers=self.auth())
        sql, args = [q for q in self.queries if "COUNT(*) AS n FROM tickets" in q[0]][0]
        self.assertNotIn("status = %s", sql)
        self.assertNotIn("priority = %s", sql)
        self.assertEqual(args, ("t_demo",))

    def test_list_scope_mine_filters_by_handler(self):
        self.user = {"id": 5, "tenant_id": "t_demo", "status": 1}
        self.perms = {"ticket:view"}
        self.client.get("/api/admin/tickets?scope=mine", headers=self.auth(uid=5))
        sql, args = [q for q in self.queries if "COUNT(*) AS n FROM tickets" in q[0]][0]
        self.assertIn("handler_uid = %s", sql)
        self.assertEqual(args, ("t_demo", 5))

    def test_ticket_json_shape(self):
        self.ticket_rows = [ticket_row()]
        row = self.client.get("/api/admin/tickets", headers=self.auth()).json()["data"]["list"][0]
        self.assertEqual(set(row), {"id", "thread_id", "ticket_no", "user_id", "reason",
                                    "status", "priority", "tenant_id", "handler_uid",
                                    "handler_name", "created_at", "unread_count"})
        self.assertNotIn("closed_at", row)     # Go: *time.Time + omitempty
        self.assertEqual(row["unread_count"], 2)
        self.assertEqual(row["created_at"], "2026-10-20T12:58:32.477+08:00")

    # ---------- 详情 / 错误分支 ----------

    def test_detail_errors(self):
        bad = self.client.get("/api/admin/tickets/abc", headers=self.auth())
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["msg"], "参数错误")
        missing = self.client.get("/api/admin/tickets/999", headers=self.auth())
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["msg"], "工单不存在")
        self.ticket = ticket_row(tenant_id="t_other")
        foreign = self.client.get("/api/admin/tickets/7", headers=self.auth())
        self.assertEqual(foreign.status_code, 403)
        self.assertEqual(foreign.json()["msg"], "无权访问该租户工单")

    def test_detail_includes_zero_unread(self):
        data = self.client.get("/api/admin/tickets/7", headers=self.auth()).json()["data"]
        self.assertEqual(data["unread_count"], 0)

    # ---------- 消息窗口 ----------

    def test_messages_window_upper_bound_per_status(self):
        self.messages = [ticket_row() and {
            "id": 1, "thread_id": THREAD, "role": "user", "content": "hi",
            "msg_type": "text", "tenant_id": "t_demo", "ticket_id": None,
            "is_read": 1, "created_at": datetime(2026, 10, 20, 12, 58, 32, 477000)}]
        self.client.get("/api/admin/tickets/7/messages", headers=self.auth())
        sql, args = [q for q in self.queries
                     if "FROM messages WHERE thread_id = %s AND created_at" in q[0]][0]
        self.assertEqual(args[0], THREAD)
        # 待处理：创建时刻 +2s
        self.assertEqual(args[1], datetime(2026, 10, 20, 12, 58, 34, 477000))

    def test_messages_merge_archive_drops_msg_type(self):
        self.ticket = ticket_row(status=1, handler_uid=0)
        self.messages = [{"id": 12, "thread_id": THREAD, "role": "ai", "content": "后",
                          "msg_type": "text", "tenant_id": "t_demo", "ticket_id": 7,
                          "is_read": 0, "created_at": datetime(2026, 10, 20, 13, 0, 0)}]
        self.archived = [{"id": 3, "thread_id": THREAD, "role": "user", "content": "前",
                          "tenant_id": "t_demo", "ticket_id": None, "is_read": 1,
                          "created_at": datetime(2026, 10, 20, 12, 0, 0)}]
        data = self.client.get("/api/admin/tickets/7/messages", headers=self.auth()).json()["data"]
        self.assertEqual([m["content"] for m in data], ["前", "后"])
        self.assertEqual(data[0]["msg_type"], "")     # Go 合并时未带 MsgType
        self.assertEqual(data[1]["msg_type"], "text")
        self.assertNotIn("ticket_id", data[0])

    def test_read_marks_non_user_only(self):
        self.client.post("/api/admin/tickets/7/read", headers=self.auth())
        sql, args = self.writes_matching("UPDATE messages SET is_read = 1")[0]
        self.assertIn("role != %s", sql)
        self.assertEqual(args, (THREAD, "user"))

    # ---------- 接管 ----------

    def test_takeover_pending_runs_full_sequence(self):
        # uid=0 是 app_key 管理员：Go 的 AdjustHandlingCount 对 uid==0 直接 return，
        # 所以这里必须用子账号令牌才能真正覆盖「处理量 +1 并联动状态」
        self.user = {"id": 5, "tenant_id": "t_demo", "status": 1}
        self.perms = {"ticket:handle"}
        response = self.client.post("/api/admin/tickets/7/takeover",
                                    headers=self.auth(uid=5), json={"wait_time": 30})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["msg"], "已接管，您好")
        resume = self.resumes()[0]
        self.assertEqual(resume[1], {"thread_id": THREAD, "confirmed": True,
                                     "wait_time": 30, "ticket_id": "TK-abc12345"})
        # handler_name 走 currentHandlerName：子账号昵称
        self.assertEqual(self.writes_matching("UPDATE tickets SET handler_uid")[0][1],
                         (5, "小王", 7))
        # 处理量 1 + 1 = 2，未达上限 3 → 保持在线(1)
        status_update = self.writes_matching("UPDATE agent_status")[0]
        self.assertIn("UPDATE agent_status", status_update[0])
        self.assertEqual(status_update[1], (2, 1, 3))
        self.assertEqual(self.writes_matching("UPDATE tickets SET status")[0][1], (1, 7))
        log = self.writes_matching("INSERT INTO ticket_logs")[0][1]
        self.assertEqual(log[3:8], ("takeover", 0, 1, 1, 1))
        self.assertEqual(log[8], "小王")
        system = self.writes_matching("INSERT INTO messages")[0][1]
        self.assertEqual(system[1:3], ("system", "人工客服已接管对话，工单号：TK-abc12345"))
        self.assertEqual(system[5], 7)
        targets = [b[1]["target"] for b in self.broadcasts()]
        self.assertEqual(targets, ["user", "admins_tenant"])
        self.assertEqual(self.broadcasts()[0][1]["payload"]["type"], "human_notice")
        self.assertEqual(self.broadcasts()[1][1]["payload"]["ticket"]["status"], 1)

    def test_takeover_app_key_admin_skips_handling_count(self):
        # uid=0：记录 handler_uid=0、handler_name=租户ID，处理量不动
        response = self.client.post("/api/admin/tickets/7/takeover",
                                    headers=self.auth(), json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.writes_matching("UPDATE tickets SET handler_uid")[0][1],
                         (0, "t_demo", 7))
        self.assertEqual(self.writes_matching("UPDATE agent_status"), [])
        self.assertEqual(self.writes_matching("INSERT INTO ticket_logs")[0][1][8], "t_demo")

    def test_takeover_resume_failure_still_takes_over(self):
        self.resume_reply = RuntimeError("agent down")
        response = self.client.post("/api/admin/tickets/7/takeover",
                                    headers=self.auth(), json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["msg"].startswith("接管成功，但恢复Agent会话失败"))
        self.assertTrue(self.writes_matching("UPDATE tickets SET status"))

    def test_takeover_rejects_closed_and_canceled(self):
        for status, msg in ((3, "工单已关闭"), (2, "工单已取消")):
            with self.subTest(status=status):
                self.ticket = ticket_row(status=status)
                response = self.client.post("/api/admin/tickets/7/takeover",
                                            headers=self.auth(), json={})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["msg"], msg)

    def test_takeover_by_other_handler_is_forbidden(self):
        self.user = {"id": 5, "tenant_id": "t_demo", "status": 1}
        self.perms = {"ticket:handle"}
        self.ticket = ticket_row(status=1, handler_uid=8, handler_name="老王")
        response = self.client.post("/api/admin/tickets/7/takeover",
                                    headers=self.auth(uid=5), json={})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["msg"], "该工单由 老王 接管中，无权接管")

    def test_takeover_idempotent_for_same_handler(self):
        self.user = {"id": 5, "tenant_id": "t_demo", "status": 1}
        self.perms = {"ticket:handle"}
        self.ticket = ticket_row(status=1, handler_uid=5, handler_name="小王")
        response = self.client.post("/api/admin/tickets/7/takeover",
                                    headers=self.auth(uid=5), json={})
        self.assertEqual(response.json()["msg"], "该工单已在人工接管中")
        self.assertEqual(self.resumes(), [])
        self.assertEqual(self.business_writes(), [])   # Go 在这个分支直接返回，不落业务写

    # ---------- 回复 ----------

    def test_reply_requires_content_and_handling_state(self):
        empty = self.client.post("/api/admin/tickets/7/reply", headers=self.auth(),
                                 json={"content": ""})
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(empty.json()["msg"], "content 不能为空")
        not_handling = self.client.post("/api/admin/tickets/7/reply", headers=self.auth(),
                                        json={"content": "在的"})
        self.assertEqual(not_handling.status_code, 400)
        self.assertEqual(not_handling.json()["msg"], "该工单未处于人工接管中，无法回复")

    def test_reply_writes_and_broadcasts(self):
        self.ticket = ticket_row(status=1, handler_uid=0)
        response = self.client.post("/api/admin/tickets/7/reply", headers=self.auth(),
                                    json={"content": "已为您处理", "msg_type": "image"})
        self.assertEqual(response.json()["msg"], "ok")
        saved = self.writes_matching("INSERT INTO messages")[0][1]
        self.assertEqual(saved[1:3], ("human", "已为您处理"))
        self.assertEqual(saved[3], "image")
        user_push = [b for b in self.broadcasts() if b[1]["target"] == "user"][0][1]
        self.assertEqual(user_push["payload"],
                         {"type": "human_reply", "msg": "已为您处理",
                          "msg_type": "image", "ticket_no": "TK-abc12345"})

    def test_reply_by_other_handler_is_forbidden(self):
        self.user = {"id": 5, "tenant_id": "t_demo", "status": 1}
        self.perms = {"ticket:handle"}
        self.ticket = ticket_row(status=1, handler_uid=8, handler_name="老王")
        response = self.client.post("/api/admin/tickets/7/reply", headers=self.auth(uid=5),
                                    json={"content": "hi"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["msg"], "该工单由 老王 接管中，无权回复")

    # ---------- 关闭 ----------

    def test_close_is_idempotent_and_scoped(self):
        self.ticket = ticket_row(status=3, closed_at=datetime(2026, 10, 20, 13, 0, 0))
        response = self.client.post("/api/admin/tickets/7/close", headers=self.auth())
        self.assertEqual(response.json()["msg"], "工单已关闭")
        self.assertEqual(self.business_writes(), [])

    def test_close_writes_and_releases_handling_count(self):
        self.ticket = ticket_row(status=1, handler_uid=9)
        response = self.client.post("/api/admin/tickets/7/close", headers=self.auth())
        self.assertEqual(response.json()["msg"], "已关闭")
        self.assertEqual(self.writes_matching("UPDATE tickets SET status")[0][1], (3, 7))
        log = self.writes_matching("INSERT INTO ticket_logs")[0][1]
        self.assertEqual(log[3:5], ("close", 1))     # Go 这里 old_status 硬编码 1
        self.assertEqual(self.broadcasts()[0][1]["payload"]["type"], "human_notice")

    def test_close_by_other_handler_is_forbidden(self):
        self.user = {"id": 5, "tenant_id": "t_demo", "status": 1}
        self.perms = {"ticket:handle"}
        self.ticket = ticket_row(status=1, handler_uid=8, handler_name="老王")
        response = self.client.post("/api/admin/tickets/7/close", headers=self.auth(uid=5))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["msg"], "该工单由 老王 接管中，无权关闭")

    # ---------- 优先级 ----------

    def test_priority_rejects_zero_because_go_binding_required(self):
        for body in ({}, {"priority": 0}, {"priority": "2"}, {"priority": True}):
            with self.subTest(body=body):
                response = self.client.post("/api/admin/tickets/7/priority",
                                            headers=self.auth(), json=body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["msg"], "参数错误：priority 必填")
        self.assertEqual(self.business_writes(), [])
        # 但审计照写：AuditMiddleware 在 StatusCode 内层，读到的是 200
        self.assertEqual(len([w for w in self.writes if "INSERT INTO audit_logs" in w[0]]), 4)

    def test_priority_updates_and_logs(self):
        response = self.client.post("/api/admin/tickets/7/priority", headers=self.auth(),
                                    json={"priority": 3, "remark": "客户催单"})
        self.assertEqual(response.json(),
                         {"code": 0, "msg": "优先级设置成功", "data": {"priority": 3}})
        self.assertEqual(self.writes_matching("UPDATE tickets SET priority")[0][1], (3, 7))
        log = self.writes_matching("INSERT INTO ticket_logs")[0][1]
        self.assertEqual(log[3:8], ("priority", 0, 0, 1, 3))
        self.assertEqual(log[9], "客户催单")

    # ---------- 转交 ----------

    def test_transfer_validation_chain(self):
        cases = [
            ({"to_user_id": 0}, 400, "to_user_id 不能为空"),
            ({"to_user_id": "9"}, 400, "to_user_id 不能为空"),
        ]
        for body, code, msg in cases:
            with self.subTest(body=body):
                response = self.client.post("/api/admin/tickets/7/transfer",
                                            headers=self.auth(), json=body)
                self.assertEqual(response.status_code, code)
                self.assertEqual(response.json()["msg"], msg)
        self.ticket = ticket_row(status=3, closed_at=datetime(2026, 10, 20, 13, 0, 0))
        closed = self.client.post("/api/admin/tickets/7/transfer", headers=self.auth(),
                                  json={"to_user_id": 9})
        self.assertEqual(closed.json()["msg"], "仅待处理或接管中的工单可转交")

    def test_transfer_target_must_be_active_same_tenant(self):
        self.ticket = ticket_row(status=0)
        self.target_user = {"id": 9, "tenant_id": "t_other", "nickname": "x",
                            "username": "x", "status": 1}
        foreign = self.client.post("/api/admin/tickets/7/transfer", headers=self.auth(),
                                   json={"to_user_id": 9})
        self.assertEqual(foreign.json()["msg"], "目标客服不属于当前租户")
        self.target_user = {"id": 9, "tenant_id": "t_demo", "nickname": "x",
                            "username": "x", "status": 0}
        disabled = self.client.post("/api/admin/tickets/7/transfer", headers=self.auth(),
                                    json={"to_user_id": 9})
        self.assertEqual(disabled.json()["msg"], "目标客服已禁用，不能转交")

    def test_transfer_promotes_pending_and_double_audits(self):
        self.ticket = ticket_row(status=0)
        response = self.client.post("/api/admin/tickets/7/transfer", headers=self.auth(),
                                    json={"to_user_id": 9})
        self.assertEqual(response.json()["msg"], "已转交")
        update = self.writes_matching("UPDATE tickets SET handler_uid = %s, handler_name = %s, "
                                      "status = %s")[0][1]
        self.assertEqual(update, (9, "小李", 1, 7))
        log = self.writes_matching("INSERT INTO ticket_logs")[0][1]
        self.assertEqual(log[3:5], ("transfer", 0))
        # Go 除中间件自动审计外还手写一条带 detail 的审计
        audits = [w for w in self.writes if "INSERT INTO audit_logs" in w[0]]
        explicit = [a for a in audits if a[1][6]]
        self.assertEqual(len(explicit), 1)
        self.assertEqual(explicit[0][1][3], "update")
        self.assertEqual(explicit[0][1][4], "ticket")
        self.assertEqual(explicit[0][1][5], "7")
        self.assertEqual(explicit[0][1][6], "转交工单：TK-abc12345 → 小李")

    # ---------- 审批 ----------

    def test_approve_requires_ticket_no(self):
        response = self.client.post("/api/admin/ticket/approve", headers=self.auth(), json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "参数错误")

    def test_approve_cancel_path(self):
        self.ticket = ticket_row(status=0)
        self.resume_reply = "已取消转接"
        response = self.client.post("/api/admin/ticket/approve", headers=self.auth(),
                                    json={"ticket_no": "TK-abc12345", "confirmed": False})
        self.assertEqual(response.json()["msg"], "已取消转接")
        self.assertEqual(self.resumes()[0][1]["confirmed"], False)
        self.assertEqual(self.writes_matching("UPDATE tickets SET status")[0][1], (2, 7))
        log = self.writes_matching("INSERT INTO ticket_logs")[0][1]
        self.assertEqual(log[3:7], ("cancel", 0, 2, 1))
        system = self.writes_matching("INSERT INTO messages")[0][1]
        self.assertEqual(system[2], "转接申请已取消，智能客服继续服务")

    def test_approve_confirm_path_delegates_to_takeover(self):
        self.ticket = ticket_row(status=0)
        response = self.client.post("/api/admin/ticket/approve", headers=self.auth(),
                                    json={"ticket_no": "TK-abc12345", "confirmed": True,
                                          "wait_time": 5})
        self.assertEqual(response.json()["msg"], "已接管，您好")
        self.assertEqual(self.resumes()[0][1]["confirmed"], True)
        log = self.writes_matching("INSERT INTO ticket_logs")[0][1]
        self.assertEqual(log[3], "takeover")

    # ---------- 平台隔离 ----------

    def test_platform_admin_is_rejected(self):
        self.tenant_platform = 1
        for method, path, body in (
            ("get", "/api/admin/tickets", None),
            ("get", "/api/admin/tickets/7", None),
            ("get", "/api/admin/tickets/7/messages", None),
            ("get", "/api/admin/tickets/7/logs", None),
            ("get", "/api/admin/tickets/transfer-targets", None),
            ("post", "/api/admin/tickets/7/read", None),
            ("post", "/api/admin/tickets/7/takeover", {}),
            ("post", "/api/admin/tickets/7/reply", {"content": "x"}),
            ("post", "/api/admin/tickets/7/close", None),
            ("post", "/api/admin/tickets/7/priority", {"priority": 2}),
            ("post", "/api/admin/tickets/7/transfer", {"to_user_id": 9}),
            ("post", "/api/admin/ticket/approve", {"ticket_no": "TK-x", "confirmed": True}),
        ):
            with self.subTest(path=path, method=method):
                kwargs = {"json": body} if body is not None else {}
                response = getattr(self.client, method)(path, headers=self.auth(), **kwargs)
                self.assertEqual(response.status_code, 403)
                self.assertIn("平台管理员不可访问租户业务数据", response.json()["msg"])


if __name__ == "__main__":
    unittest.main()
