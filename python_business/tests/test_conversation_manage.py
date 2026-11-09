"""会话重命名与删除的边界。

删除有三条 409 不能省，任何一条漏掉都会造成"看起来删掉了但 Agent 还在写"：
当前正在用的会话、有 queued/running 轮次的会话、有未完成工单的会话。
这里直接调函数（不经 HTTP），只钉判定顺序与落库动作。
"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import HTTPException

from business.customer import routes as cr
from business.customer.routes import ConversationRenameReq, Customer

CUSTOMER = Customer("t_demo", "t_demo:buyer1", "t_demo:buyer1:current", "sess-1",
                    False, False)


def _conversation(**overrides):
    row = {
        "thread_id": "t_demo:buyer1:old",
        "tenant_id": "t_demo",
        "customer_id": "t_demo:buyer1",
        "title": "",
        "is_guest": 0,
        "is_preview": 0,
        "status": "active",
        "last_active_at": datetime.now(),
    }
    row.update(overrides)
    return row


class _Stub:
    """按 SQL 内容分派 one()，并记录 write() 与 agent 调用。"""

    def __init__(self, conversation=None, busy=False, ticket=False):
        self.conversation = conversation
        self.busy = busy
        self.ticket = ticket
        self.writes = []
        self.purged = []

    def one(self, sql, args=()):
        if "FROM chat_conversations" in sql:
            return self.conversation
        if "FROM chat_turns" in sql:
            return {"x": 1} if self.busy else None
        if "FROM tickets" in sql:
            return {"x": 1} if self.ticket else None
        return None

    def write(self, sql, args):
        self.writes.append((sql, args))
        return 1

    def post(self, url, **kwargs):
        self.purged.append((url, kwargs))
        return None


class RenameConversationTest(unittest.TestCase):
    def _run(self, title, stub=None):
        stub = stub or _Stub(conversation=_conversation())
        with patch.object(cr, "one", side_effect=stub.one), \
             patch.object(cr, "write", side_effect=stub.write):
            result = cr.rename_conversation(
                "t_demo:buyer1:old", ConversationRenameReq(title=title), CUSTOMER)
        return result, stub

    def test_rename_trims_and_persists(self):
        result, stub = self._run("  订单发货进度  ")
        self.assertEqual(result["data"]["title"], "订单发货进度")
        self.assertEqual(stub.writes[0][1], ("订单发货进度", "t_demo:buyer1:old"))

    def test_empty_title_restores_default(self):
        result, stub = self._run("   ")
        self.assertEqual(result["data"]["title"], "")
        self.assertEqual(stub.writes[0][1][0], "")

    def test_over_long_title_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            self._run("x" * 61)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_foreign_conversation_is_not_renameable(self):
        stub = _Stub(conversation=_conversation(customer_id="t_demo:buyer2"))
        with self.assertRaises(HTTPException) as ctx:
            self._run("偷改别人", stub)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(stub.writes, [])


class DeleteConversationTest(unittest.TestCase):
    def _delete(self, stub, thread_id="t_demo:buyer1:old"):
        with patch.object(cr, "one", side_effect=stub.one), \
             patch.object(cr, "write", side_effect=stub.write), \
             patch.object(cr.httpx, "post", side_effect=stub.post):
            return cr.delete_conversation(thread_id, CUSTOMER)

    def test_delete_marks_row_and_purges_checkpoint(self):
        stub = _Stub(conversation=_conversation())
        self._delete(stub)
        self.assertIn("deleted_at = NOW(3)", stub.writes[0][0])
        self.assertEqual(len(stub.purged), 1)
        self.assertIn("/agent/session/cleanup/batch", stub.purged[0][0])

    def test_current_conversation_cannot_be_deleted(self):
        stub = _Stub(conversation=_conversation(thread_id="t_demo:buyer1:current"))
        with self.assertRaises(HTTPException) as ctx:
            self._delete(stub, thread_id="t_demo:buyer1:current")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(stub.writes, [])

    def test_in_flight_turn_blocks_delete(self):
        stub = _Stub(conversation=_conversation(), busy=True)
        with self.assertRaises(HTTPException) as ctx:
            self._delete(stub)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(stub.writes, [])

    def test_unfinished_ticket_blocks_delete(self):
        stub = _Stub(conversation=_conversation(), ticket=True)
        with self.assertRaises(HTTPException) as ctx:
            self._delete(stub)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(stub.writes, [])

    def test_foreign_conversation_is_not_deletable(self):
        stub = _Stub(conversation=_conversation(customer_id="t_demo:buyer2"))
        with self.assertRaises(HTTPException) as ctx:
            self._delete(stub)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(stub.writes, [])

    def test_guest_cannot_delete_even_own_current_conversation(self):
        """游客删自己的当前会话必须 403，不能落到 409（"换一个再删"是错误暗示）。"""
        guest = Customer("t_demo", "t_demo:guest_abc", "t_demo:guest_abc",
                         "sess-g", True, False)
        stub = _Stub(conversation=_conversation(
            thread_id="t_demo:guest_abc", customer_id="t_demo:guest_abc",
            is_guest=1))
        with patch.object(cr, "one", side_effect=stub.one), \
             patch.object(cr, "write", side_effect=stub.write), \
             patch.object(cr.httpx, "post", side_effect=stub.post):
            with self.assertRaises(HTTPException) as ctx:
                cr.delete_conversation("t_demo:guest_abc", guest)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(stub.writes, [])


if __name__ == "__main__":
    unittest.main()
