"""阶段 2：聊天记录 / 评价 / 工单状态 的边界测试。

核心约束与订单一致：thread_id 只能来自服务端会话，请求参数无法指定别人的会话；
访客与商家预览各自只能读到自己的会话内容。
"""

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main_compat as main
from patch_business import patch_business
from business.customer import routes as customer_routes


class FakeRedis:
    def __init__(self):
        self.values = {}

    def exists(self, _key):
        return int(_key in self.values)

    def eval(self, _script, _keys, _key):
        return 1

    def set(self, key, value, ex=None):
        self.values[key] = value


class Stage2ContractTest(unittest.TestCase):
    def setUp(self):
        self.queries = []
        self.writes = []
        self.redis = FakeRedis()
        self.session = {
            "tenant_id": "t_demo", "customer_id": "t_demo:buyer1",
            "thread_id": "t_demo:buyer1:session", "token": "session-id",
            "is_guest": 0, "is_preview": 0,
        }
        self.ticket = None
        self.rating_row = None
        self.message_rows = [
            {"id": 3, "thread_id": "t_demo:buyer1:session", "role": "ai",
             "content": "第三个", "msg_type": "text", "tenant_id": "t_demo",
             "ticket_id": None, "is_read": 1, "created_at": None},
            {"id": 2, "thread_id": "t_demo:buyer1:session", "role": "user",
             "content": "第二个", "msg_type": "text", "tenant_id": "t_demo",
             "ticket_id": None, "is_read": 1, "created_at": None},
            {"id": 1, "thread_id": "t_demo:buyer1:session", "role": "user",
             "content": "第一个", "msg_type": "text", "tenant_id": "t_demo",
             "ticket_id": None, "is_read": 1, "created_at": None},
        ]

        def one(sql, args=()):
            self.queries.append((sql, args))
            if "FROM tenants" in sql:
                return {"tenant_id": "t_demo", "app_secret_hash": "hash"}
            if "FROM customer_sessions" in sql:
                return self.session
            if "FROM satisfaction_ratings" in sql:
                return self.rating_row
            if "FROM tickets" in sql:
                return self.ticket
            return None

        def many(sql, args=()):
            self.queries.append((sql, args))
            return list(self.message_rows)

        self.patches = (
            patch_business(main, "one", side_effect=one),
            patch_business(main, "many", side_effect=many),
            patch_business(main, "write", side_effect=lambda sql, args: self.writes.append((sql, args))),
            patch_business(main, "cache", return_value=self.redis),
            patch.object(customer_routes, "one", side_effect=one),
            patch.object(customer_routes, "many", side_effect=many),
            patch.object(customer_routes, "write", side_effect=lambda sql, args: self.writes.append((sql, args))),
            patch.object(customer_routes, "cache", return_value=self.redis),
        )
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        self.client = TestClient(main.app)

    def token(self, **overrides):
        payload = {"tid": "t_demo", "exp": int(time.time()) + 3600,
                   "kind": "customer", "cid": "t_demo:buyer1", "sess": "session-id"}
        payload.update(overrides)
        return main.signed_token(payload, "hash")

    def auth(self, **overrides):
        return {"Authorization": "Bearer " + self.token(**overrides)}

    def message_query(self):
        return [(sql, args) for sql, args in self.queries if "FROM messages" in sql]

    # ---------- 聊天记录 ----------

    def test_messages_are_scoped_to_session_thread(self):
        response = self.client.get("/api/customer/messages", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual([m["id"] for m in response.json()["data"]], [1, 2, 3])
        sql, args = self.message_query()[0]
        self.assertIn("tenant_id = %s AND thread_id = %s", sql)
        self.assertEqual(args[:2], ("t_demo", "t_demo:buyer1:session"))
        # 返回字段需与历史实现的 /api/user/messages 对齐，前端才能直接替换
        self.assertEqual(set(response.json()["data"][0]), {
            "id", "thread_id", "role", "content", "msg_type", "tenant_id",
            "ticket_id", "is_read", "created_at",
        })

    def test_messages_ignore_client_supplied_thread(self):
        # 客户端塞 thread_id 也不会改变作用域：端点根本不读这个参数
        response = self.client.get(
            "/api/customer/messages",
            params={"thread_id": "t_demo:buyer2:session", "tenant_id": "t_other"},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200)
        _sql, args = self.message_query()[0]
        self.assertEqual(args[:2], ("t_demo", "t_demo:buyer1:session"))

    def test_messages_pagination_is_capped_and_ordered(self):
        self.client.get("/api/customer/messages", params={"limit": 9999},
                        headers=self.auth())
        _sql, args = self.message_query()[0]
        self.assertEqual(args[-1], main.MESSAGE_PAGE_MAX)

        self.client.get("/api/customer/messages", params={"before_id": 100, "limit": 2},
                        headers=self.auth())
        sql, args = self.message_query()[-1]
        self.assertIn("id < %s", sql)
        self.assertEqual(args, ("t_demo", "t_demo:buyer1:session", 100, 2))

    def test_guest_and_preview_read_only_their_own_thread(self):
        for flag in ("is_guest", "is_preview"):
            with self.subTest(flag=flag):
                self.session[flag] = 1
                response = self.client.get("/api/customer/messages", headers=self.auth())
                self.assertEqual(response.status_code, 200)
                _sql, args = self.message_query()[-1]
                self.assertEqual(args[:2], ("t_demo", "t_demo:buyer1:session"))
                self.session[flag] = 0

    def test_messages_require_valid_token(self):
        self.assertEqual(self.client.get("/api/customer/messages").status_code, 401)
        self.assertEqual(self.client.get(
            "/api/customer/messages", headers=self.auth(exp=int(time.time()) - 1)
        ).status_code, 401)
        self.assertEqual(self.message_query(), [])

    def test_legacy_messages_path_uses_signed_session(self):
        response = self.client.get(
            "/api/user/messages", params={"user_id": "t_demo:buyer2:other"},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.message_query()[-1][1][:2],
                         ("t_demo", "t_demo:buyer1:session"))
        self.assertEqual(self.client.get("/api/user/messages").status_code, 401)

    # ---------- 评价 ----------

    def test_rating_rejects_out_of_range_score(self):
        for score in (0, 6, -1):
            with self.subTest(score=score):
                response = self.client.post("/api/customer/rating",
                                            json={"score": score}, headers=self.auth())
                self.assertEqual(response.status_code, 400)
        self.assertFalse(any("satisfaction_ratings" in sql for sql, _ in self.writes))

    def test_rating_inserts_then_overwrites(self):
        headers = self.auth()
        created = self.client.post("/api/customer/rating", headers=headers,
                                   json={"score": 5, "tags": "快", "comment": "很好"})
        self.assertEqual(created.status_code, 200)
        insert_sql, insert_args = self.writes[-1]
        self.assertIn("INSERT INTO satisfaction_ratings", insert_sql)
        self.assertEqual(insert_args[:2], ("t_demo", "t_demo:buyer1:session"))

        self.rating_row = {"id": 7}
        updated = self.client.post("/api/customer/rating", headers=headers,
                                   json={"score": 3, "tags": "一般", "comment": ""})
        self.assertEqual(updated.status_code, 200)
        update_sql, update_args = self.writes[-1]
        self.assertIn("UPDATE satisfaction_ratings", update_sql)
        self.assertEqual(update_args, (3, "一般", "", 7))

    def test_rating_truncates_long_text(self):
        self.client.post("/api/customer/rating", headers=self.auth(),
                         json={"score": 4, "tags": "x" * 300, "comment": "y" * 800})
        _sql, args = self.writes[-1]
        # 插入参数顺序：(tenant_id, thread_id, user_key, score, tags, comment)
        self.assertEqual(len(args[4]), 255)
        self.assertEqual(len(args[5]), 500)

    def test_legacy_rating_path_ignores_supplied_user_id(self):
        response = self.client.post(
            "/api/user/rating", headers=self.auth(),
            json={"user_id": "t_demo:buyer2:other", "score": 5},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.writes[-1][1][:2],
                         ("t_demo", "t_demo:buyer1:session"))

    def test_customer_upload_uses_authenticated_tenant(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch_business(main, "MSG_FILE_ROOT", directory):
            response = self.client.post(
                "/api/user/upload", headers=self.auth(),
                data={"tenant_id": "t_other", "user_id": "t_demo:buyer2:other"},
                files={"file": ("picture.png", b"image-bytes", "image/png")},
            )
            self.assertEqual(response.status_code, 200)
            url = response.json()["data"]["url"]
            self.assertTrue(url.startswith("/msg_files/t_demo/"))
            self.assertEqual(response.json()["data"]["msg_type"], "image")
            self.assertTrue(os.path.isfile(os.path.join(directory, *url.split("/")[2:])))
        self.assertEqual(self.client.post(
            "/api/user/upload", files={"file": ("picture.png", b"x")}
        ).status_code, 401)

    # ---------- 工单 ----------

    def test_ticket_absent_returns_null(self):
        response = self.client.get("/api/customer/ticket", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["data"])

    def test_ticket_status_is_mapped_and_scoped(self):
        self.ticket = {"ticket_no": "TK-1", "status": 1, "priority": 1,
                       "reason": "归属校验未通过", "handler_name": "小王",
                       "created_at": None, "closed_at": None}
        response = self.client.get("/api/customer/ticket", headers=self.auth())
        data = response.json()["data"]
        self.assertEqual(data["status_text"], "handling")
        self.assertEqual(data["ticket_no"], "TK-1")
        sql, args = [(s, a) for s, a in self.queries if "FROM tickets" in s][-1]
        self.assertIn("deleted_at = 0", sql)
        self.assertEqual(args, ("t_demo", "t_demo:buyer1:session"))


if __name__ == "__main__":
    unittest.main()
