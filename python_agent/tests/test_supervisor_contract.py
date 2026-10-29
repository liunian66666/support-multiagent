"""JSON/SSE parity and specialist boundaries after moving routing out of API."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.api.chat import agent_chat, agent_chat_stream
from app.chat.intent import Route, route_message
from app.schemas import ChatRequest
from app.services.specialists import SpecialistResult, execute_shortcut
from app.services.supervisor import TurnDecision, decide_turn


class SupervisorTest(unittest.IsolatedAsyncioTestCase):
    async def test_shipping_threshold_question_routes_to_knowledge(self):
        self.assertEqual(route_message("满99元包邮吗？", "t_demo").specialist, "faq")

    async def test_store_fact_phrasings_reach_knowledge_not_general(self):
        """留出集off两轮共同暴露：口语化政策问法被规则路由丢给 general，
        由无知识库能力的 intake 直接拒答——这些问法的正确归宿是 faq。"""
        for msg in ("我买了120块的东西还要不要付邮费？",
                    "一单买了88元，邮费具体是几块？",
                    "东西有质量问题要退，寄回去的钱谁掏？",
                    "无理由退货来回邮费谁承担？",
                    "签收之后最晚几天内能申请退货？",
                    "退货打到银行卡大概几个工作日？",
                    "预售的包写的是11月20日发货，会按这个时间发吗？",
                    "晚上十点在线客服还有人吗，几点有？"):
            with self.subTest(msg=msg):
                self.assertNotEqual(route_message(msg, "t_demo").specialist, "general")

    async def test_demo_order_id_without_context_still_routes_to_order(self):
        """「ORD-2024-001现在到哪一步了？」无上下文词，旧正则漏提取→落general。
        ORD-数字-数字 特征足够强，无需上下文词。"""
        route = route_message("ORD-2024-001现在到哪一步了？", "t_demo")
        self.assertEqual(route.specialist, "order")
        self.assertEqual(route.order_id, "ORD-2024-001")

    async def test_general_chat_not_swept_into_faq(self):
        """扩词不得把寒暄/闲聊吸进 faq（否则知识库检索空转、时延上升）。"""
        for msg in ("你好", "谢谢", "再见", "好的", "哈哈", "是的", "嗯嗯", "辛苦了"):
            with self.subTest(msg=msg):
                self.assertEqual(route_message(msg, "t_demo").specialist, "general")

    async def test_owned_order_history_is_scoped_to_same_thread(self):
        app = SimpleNamespace()
        with patch("app.services.supervisor._last_order_id_from_history",
                   new_callable=AsyncMock, return_value="ORD-2024-001") as history:
            decision = await decide_turn(app, "t_demo", "t_demo:buyer1",
                                         "刚才那个订单到哪了？")
        self.assertEqual(decision.route.specialist, "order")
        self.assertEqual(decision.route.order_id, "ORD-2024-001")
        history.assert_awaited_once_with(app, "t_demo:buyer1")

    async def test_refund_action_cannot_become_compound_order_read(self):
        decision = await decide_turn(SimpleNamespace(), "t_demo", "t_demo:buyer1",
                                     "请给订单 ORD-2024-001 退款并解释政策")
        self.assertEqual(decision.specialist, "case")
        self.assertIsNone(decision.plan)

    async def test_order_specialist_returns_handoff_request_without_creating_ticket(self):
        decision = TurnDecision(Route("order", order_id="ORD-2024-001"), None,
                                "查订单", "rule")
        with patch("app.services.specialists.tools.lookup_order", new_callable=AsyncMock,
                   return_value=SimpleNamespace(code="FORBIDDEN", text="无法核验")), \
                patch("app.services.specialists._record_to_checkpoint", new_callable=AsyncMock):
            result = await execute_shortcut(SimpleNamespace(), "t_demo", "t_demo:buyer2",
                                            "查订单", decision)
        self.assertEqual(result.role, "order")
        self.assertEqual(result.status, "handoff")
        self.assertEqual(result.interrupt_data["code"], "FORBIDDEN")
        self.assertNotIn("ticket_id", result.interrupt_data)


class EntryParityTest(unittest.IsolatedAsyncioTestCase):
    async def test_json_and_sse_use_same_supervisor_and_shortcut(self):
        req = ChatRequest(thread_id="t_demo:guest1", user_message="包邮规则", has_pending_ticket=True)
        request = SimpleNamespace(headers={"X-Trace-Id": "trace-test"},
                                  app=SimpleNamespace())
        decision = TurnDecision(Route("faq_fast", answer="满 99 元包邮"), None,
                                req.user_message, "rule")
        shortcut = SpecialistResult("faq_rule", content="满 99 元包邮")
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_pool(app):  # 配额/触点在本用例被 patch 掉，只需空连接占位
            yield SimpleNamespace()
        with patch("app.api.chat._require_trusted_chat", return_value="t_demo"), \
                patch("app.api.chat._pool_conn", fake_pool), \
                patch("app.api.chat.check_concurrent_sessions", new_callable=AsyncMock,
                      return_value=None), \
                patch("app.api.chat.decide_turn", new_callable=AsyncMock,
                      return_value=decision) as decide, \
                patch("app.api.chat.execute_shortcut", new_callable=AsyncMock,
                      return_value=shortcut) as execute:
            direct = await agent_chat(req, request)
            streamed = await agent_chat_stream(req, request)
            frames = [json.loads(line[6:]) async for chunk in streamed.body_iterator
                      for line in chunk.splitlines() if line.startswith("data: ")]
        self.assertEqual(decide.await_count, 2)
        self.assertEqual(execute.await_count, 2)
        self.assertEqual(direct.content, frames[-1]["content"])
        self.assertEqual(frames[-1]["type"], "end")


if __name__ == "__main__":
    unittest.main()
