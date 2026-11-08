"""Routing contract for the integrated customer support specialists."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage

from app.agent import tools
from app.agent.faq_agent import faq_agent, recent_messages
from app.agent.intake_agent import intake_agent
from app.chat.intent import route_message


class RoutingTest(unittest.TestCase):
    def test_process_faq(self):
        route = route_message("怎么样申请退货？", "t_demo")
        self.assertEqual(route.specialist, "faq_fast")

    def test_tenant_policy_faq(self):
        route = route_message("退货运费谁承担？", "t_demo")
        self.assertEqual(route.specialist, "faq")

    def test_owned_order_stays_in_order_specialist(self):
        route = route_message("查订单 ORD-2024-001 的状态", "t_demo")
        self.assertEqual((route.specialist, route.order_id), ("order", "ORD-2024-001"))

    def test_unknown_order_number_lists_owned_orders(self):
        for query in ("我的订单", "查询一下我的订单", "我不记得订单号"):
            with self.subTest(query=query):
                self.assertEqual(route_message(query, "t_demo").specialist, "order_list")

    def test_refund_policy_never_becomes_mutation(self):
        for query in ("退款政策怎么规定", "帮我查询退款政策"):
            with self.subTest(query=query):
                self.assertEqual(route_message(query, "t_demo").specialist, "faq")

    def test_refund_action_with_order_never_becomes_order_lookup(self):
        route = route_message("请给我退款，订单号 ORD-2024-001", "t_demo")
        self.assertEqual(route.specialist, "handoff")

    def test_direct_handoff(self):
        self.assertEqual(route_message("转人工", "t_demo").specialist, "handoff")

    def test_pending_ticket_does_not_open_second_ticket(self):
        route = route_message("请给我退款", "t_demo", pending_ticket=True)
        self.assertEqual(route.specialist, "faq_fast")

    def test_other_chat_uses_general_agent(self):
        self.assertEqual(route_message("你好", "t_demo").specialist, "general")

    def test_refund_process_stays_in_faq(self):
        self.assertEqual(route_message("如何申请退款？", "t_demo").specialist, "faq_fast")


class SpecialistTest(unittest.IsolatedAsyncioTestCase):
    async def test_intake_agent_has_no_business_tools(self):
        app = SimpleNamespace(state=SimpleNamespace(intake_agents={}))
        with patch("app.agent.intake_agent._check_config_version", new_callable=AsyncMock), \
                patch("app.agent.intake_agent.get_tenant_model", new_callable=AsyncMock,
                      return_value=object()), \
                patch("app.agent.intake_agent.create_agent") as create:
            first = await intake_agent(app, "t_demo")
            second = await intake_agent(app, "t_demo")
        self.assertIs(first, second)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(create.call_args.kwargs["tools"], [])
        self.assertNotIn("checkpointer", create.call_args.kwargs)

    async def test_faq_agent_has_only_kb_tool(self):
        app = SimpleNamespace()
        with patch("app.agent.faq_agent.get_tenant_model", new_callable=AsyncMock) as model, patch("app.agent.faq_agent.create_agent") as create:
            model.return_value = object()
            await faq_agent(app, "t_demo")
        self.assertEqual(create.call_args.kwargs["tools"], [tools.search_kb])
        self.assertNotIn("checkpointer", create.call_args.kwargs)

    async def test_history_omits_unpaired_tool_calls(self):
        state = SimpleNamespace(values={"messages": [
            HumanMessage(content="问题"),
            AIMessage(content="", tool_calls=[{"name": "search_kb", "args": {"query": "x"}, "id": "call1"}]),
            AIMessage(content="回答"),
        ]})
        bridge = SimpleNamespace(aget_state=AsyncMock(return_value=state))
        app = SimpleNamespace(state=SimpleNamespace(faq_bridge=bridge))
        messages = await recent_messages(app, "t_demo:guest")
        self.assertEqual([m.content for m in messages], ["问题", "回答"])


if __name__ == "__main__":
    unittest.main()
