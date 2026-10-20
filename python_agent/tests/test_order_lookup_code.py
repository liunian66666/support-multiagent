"""[ours] 本文件为 fork 新增：订单查询结果码与自动转人工判定。

背景：早期实现用「未找到订单/不属于您/暂不可用」等**面向用户的文案**做子串匹配来决定是否转人工，
导致用户把单号打错（404）也直接建单、且改一句措辞就改流程。现在判定只认结构化 code：
- 单号不存在（NOT_FOUND）→ 留在自助链路，提示用户核对；
- 未登录（UNAUTHENTICATED）→ 提示登录；
- 归属不符（FORBIDDEN）/系统不可用（UNAVAILABLE）/身份上下文缺失（UNVERIFIED）→ 自动转人工。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.agent import tools
from app.chat.order import (ORDER_FORBIDDEN, ORDER_NOT_FOUND, ORDER_OK,
                            ORDER_UNAUTHENTICATED, ORDER_UNAVAILABLE,
                            ORDER_UNVERIFIED, order_handoff_reason,
                            should_auto_handoff)
from app.infra import context


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def _app(resp=None, boom=False):
    async def get(*args, **kwargs):
        if boom:
            raise RuntimeError("connect timeout")
        return resp

    return SimpleNamespace(state=SimpleNamespace(http_client=SimpleNamespace(get=get)))


class AutoHandoffPolicyTest(unittest.TestCase):
    def test_user_fixable_failures_stay_self_service(self):
        for code in (ORDER_OK, ORDER_NOT_FOUND, ORDER_UNAUTHENTICATED, None):
            with self.subTest(code=code):
                self.assertFalse(should_auto_handoff(code))

    def test_unrecoverable_failures_handoff(self):
        for code in (ORDER_FORBIDDEN, ORDER_UNAVAILABLE, ORDER_UNVERIFIED):
            with self.subTest(code=code):
                self.assertTrue(should_auto_handoff(code))

    def test_reason_is_user_readable_and_names_the_order(self):
        for code in (ORDER_FORBIDDEN, ORDER_UNAVAILABLE, ORDER_UNVERIFIED):
            with self.subTest(code=code):
                reason = order_handoff_reason(code, "10001")
                self.assertIn("10001", reason)
                self.assertNotIn("{oid}", reason)


class OrderLookupCodeTest(unittest.IsolatedAsyncioTestCase):
    """HTTP 状态/业务码 → 结果码映射。"""

    def setUp(self):
        self._orig = (tools._app, tools._tenant_var, tools._thread_var)
        tools._tenant_var = context.tenant_var
        tools._thread_var = context.thread_var
        context.tenant_var.set("t_demo")
        context.thread_var.set("t_demo:guest")

    def tearDown(self):
        tools._app, tools._tenant_var, tools._thread_var = self._orig

    async def _lookup(self, resp=None, boom=False, token="tok"):
        tools._app = _app(resp, boom)
        with patch.object(tools, "INTERNAL_API_TOKEN", token):
            return await tools.lookup_order("10001")

    async def test_ok(self):
        r = await self._lookup(_Resp(200, {"code": 0, "data": {
            "order_id": "ORD-2024-001", "status": "已发货", "item": "VIP会员套餐",
            "amount": "799", "create_date": "2024-01-15"}}))
        self.assertEqual(r.code, ORDER_OK)
        self.assertIn("已发货", r.text)

    async def test_not_found_does_not_become_handoff(self):
        r = await self._lookup(_Resp(404, {"code": 404}))
        self.assertEqual(r.code, ORDER_NOT_FOUND)
        self.assertFalse(should_auto_handoff(r.code))
        self.assertIn("核对订单号", r.text)
        self.assertNotIn("请转人工客服", r.text)

    async def test_unauthenticated(self):
        r = await self._lookup(_Resp(401, {"code": 401}))
        self.assertEqual(r.code, ORDER_UNAUTHENTICATED)
        self.assertFalse(should_auto_handoff(r.code))
        self.assertIn("登录", r.text)

    async def test_forbidden(self):
        r = await self._lookup(_Resp(403, {"code": 403}))
        self.assertEqual(r.code, ORDER_FORBIDDEN)
        self.assertTrue(should_auto_handoff(r.code))

    async def test_server_error(self):
        r = await self._lookup(_Resp(503, {"code": 503}))
        self.assertEqual(r.code, ORDER_UNAVAILABLE)
        self.assertTrue(should_auto_handoff(r.code))

    async def test_network_exception(self):
        r = await self._lookup(boom=True)
        self.assertEqual(r.code, ORDER_UNAVAILABLE)
        self.assertTrue(should_auto_handoff(r.code))

    async def test_missing_identity_context(self):
        r = await self._lookup(_Resp(200, {"code": 0}), token="")
        self.assertEqual(r.code, ORDER_UNVERIFIED)
        self.assertTrue(should_auto_handoff(r.code))

    async def test_text_wrapper_matches_lookup(self):
        tools._app = _app(_Resp(404, {"code": 404}))
        with patch.object(tools, "INTERNAL_API_TOKEN", "tok"):
            text = await tools.query_order_readonly("10001")
        self.assertEqual(text, (await tools.lookup_order("10001")).text)


if __name__ == "__main__":
    unittest.main()
