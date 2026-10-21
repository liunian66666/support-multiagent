"""[ours] 本文件为 fork 新增：订单号追问链路单元测试。

覆盖「客服回复『请提供订单号』→ 用户直接回裸单号」这一真实路径：
- looks_like_bare_order_id 只认形如订单号的整条消息；
- _awaiting_order_id 只在上一轮回复确实索要订单号时成立。
"""
import asyncio
import unittest

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from app.agent.faq_bridge import build_faq_bridge
from app.chat.intent import route_message
from app.chat.order import _awaiting_order_id, looks_like_bare_order_id


class _AppStub:
    """只暴露 _awaiting_order_id 需要的 app.state.faq_bridge。"""

    def __init__(self, bridge):
        self.state = type("State", (), {"faq_bridge": bridge})()


class BareOrderIdTest(unittest.TestCase):
    def test_bare_numeric_id(self):
        self.assertTrue(looks_like_bare_order_id("10110"))
        self.assertTrue(looks_like_bare_order_id("TB202410180001"))

    def test_prefixed_demo_id(self):
        self.assertTrue(looks_like_bare_order_id("ORD-2024-001"))

    def test_rejects_non_id_like(self):
        for text in ("1", "怎么退货", "你好", "", "   ", "我的订单呢"):
            with self.subTest(text=text):
                self.assertFalse(looks_like_bare_order_id(text))


class AwaitingOrderIdTest(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def _app_with_history(self, thread_id, messages):
        bridge = build_faq_bridge(MemorySaver())
        self._run(bridge.ainvoke({"messages": messages},
                                 config={"configurable": {"thread_id": thread_id}}))
        return _AppStub(bridge)

    def test_true_when_last_ai_asked_for_order_id(self):
        app = self._app_with_history("test:ask1", [
            HumanMessage(content="查询一下我的订单"),
            AIMessage(content="请提供订单号。核对订单归属后，客服才能查询状态。"),
        ])
        self.assertTrue(self._run(_awaiting_order_id(app, "test:ask1")))

    def test_false_when_last_ai_is_greeting(self):
        app = self._app_with_history("test:ask2", [
            HumanMessage(content="你好"),
            AIMessage(content="您好，有什么可以帮您？"),
        ])
        self.assertFalse(self._run(_awaiting_order_id(app, "test:ask2")))

    def test_false_without_bridge(self):
        self.assertFalse(self._run(_awaiting_order_id(_AppStub(None), "test:ask3")))

    def test_false_on_empty_history(self):
        bridge = build_faq_bridge(MemorySaver())
        self.assertFalse(self._run(_awaiting_order_id(_AppStub(bridge), "test:empty")))


class RouteOrderQueryTest(unittest.TestCase):
    """「查询订单10001」这类消息必须直接走订单查询，而不是回流程 FAQ。"""

    def test_inline_order_id_routes_to_order_lookup(self):
        route = route_message("查询订单10001", "t_demo")
        self.assertEqual(route.specialist, "order")
        self.assertEqual(route.order_id, "10001")

    def test_order_query_without_id_lists_owned_orders(self):
        route = route_message("查询一下我的订单", "t_demo")
        self.assertEqual(route.specialist, "order_list")


if __name__ == "__main__":
    unittest.main()
