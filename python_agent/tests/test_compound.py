"""Compound requests fan out read-only tasks and preserve identity boundaries."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.tools import OrderLookup
from app.api.chat import _compound_plan
from app.chat.intent import Route, route_message
from app.chat.plan import plan_compound
from app.services.compound import execute_compound


class PlanTest(unittest.TestCase):
    def test_order_and_policy(self):
        plan = plan_compound("查订单 ORD-2024-001 的状态，再结合退货政策告诉我下一步")
        self.assertEqual([task.kind for task in plan.tasks], ["order", "kb"])
        self.assertEqual(plan.order_id, "ORD-2024-001")
        self.assertNotIn("ORD-2024-001", plan.tasks[1].query)

    def test_order_id_adjacent_to_chinese_is_kept_for_order_and_removed_from_kb(self):
        question = "ORD-2024-003订单的状态，以及怎么退货？"
        plan = plan_compound(question)
        self.assertEqual(route_message(question, "t_demo").order_id, "ORD-2024-003")
        self.assertEqual(plan.order_id, "ORD-2024-003")
        self.assertEqual([task.kind for task in plan.tasks], ["order", "kb"])
        self.assertNotIn("ORD-2024-003", plan.tasks[1].query)
        self.assertNotIn("003", plan.tasks[1].query)

    def test_missing_order_id_keeps_policy_work(self):
        question = "核对我的订单，再结合店铺退货政策告诉我下一步"
        self.assertNotEqual(route_message(question, "t_demo").specialist, "handoff")
        plan = plan_compound(question)
        self.assertTrue(plan.needs_order_id)
        self.assertEqual([task.kind for task in plan.tasks], ["kb"])

    def test_two_public_topics(self):
        plan = plan_compound("退货运费和发票规则分别是什么？")
        self.assertEqual([task.kind for task in plan.tasks], ["kb", "kb"])

    def test_order_eligibility_question_requires_order_and_policy(self):
        plan = plan_compound("我的订单 ORD-2024-001 能退吗？")
        self.assertEqual([task.kind for task in plan.tasks], ["order", "kb"])

    def test_single_question_stays_on_existing_route(self):
        self.assertIsNone(plan_compound("查订单 ORD-2024-001 的状态"))
        self.assertIsNone(plan_compound("退货运费谁承担？"))

    def test_write_action_remains_handoff(self):
        self.assertEqual(route_message("请给我退款，订单号 ORD-2024-001", "t_demo").specialist,
                         "handoff")


class ExecutionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.retriever = SimpleNamespace(ainvoke=AsyncMock(return_value=[
            Document(page_content="签收后七天内可申请退货。", metadata={
                "doc_id": "policy-1", "title": "退货政策"})
        ]))
        self.app = SimpleNamespace(state=SimpleNamespace(
            tenant_retrievers={"t_demo": self.retriever}))

    async def test_parallel_reads_and_one_synthesis(self):
        seen = []

        async def order_read(_):
            seen.append("order_started")
            await asyncio.sleep(0)
            self.assertIn("kb_started", seen)
            return OrderLookup("OK", "订单 ORD-2024-001 状态：已签收。")

        async def kb_read(_):
            seen.append("kb_started")
            await asyncio.sleep(0)
            self.assertIn("order_started", seen)
            return self.retriever.ainvoke.return_value

        self.retriever.ainvoke.side_effect = kb_read
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(
            content="请说明退货原因，客服会核对商品情况和适用规则。")))
        with patch("app.services.compound.tools.lookup_order", side_effect=order_read), \
                patch("app.services.compound.tools._record_kb_hits", new_callable=AsyncMock), \
                patch("app.services.compound.get_tenant_model", new_callable=AsyncMock,
                      return_value=model):
            plan = plan_compound("查订单 ORD-2024-001 状态并结合退货政策说明下一步")
            result = await execute_compound(self.app, "t_demo", "t_demo:buyer1",
                                            "查订单 ORD-2024-001 状态并结合退货政策说明下一步", plan)
        self.assertEqual(model.ainvoke.await_count, 1)
        self.assertIn("订单核验：订单 ORD-2024-001 状态：已签收", result.content)
        self.assertIn("请说明退货原因", result.content)
        self.assertNotIn("知识来源", result.content)
        self.assertNotIn("[K1]", result.content)
        self.assertNotIn("ORD-2024-001", self.retriever.ainvoke.call_args.args[0])

    async def test_empty_model_reply_keeps_order_without_exposing_evidence(self):
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content="")))
        with patch("app.services.compound.tools.lookup_order", new_callable=AsyncMock,
                   return_value=OrderLookup("OK", "订单 ORD-2024-001 状态：已发货。")), \
                patch("app.services.compound.tools._record_kb_hits", new_callable=AsyncMock), \
                patch("app.services.compound.get_tenant_model", new_callable=AsyncMock,
                      return_value=model):
            plan = plan_compound("查订单 ORD-2024-001 状态并结合退货政策说明下一步")
            result = await execute_compound(self.app, "t_demo", "t_demo:buyer1", "问题", plan)
        self.assertIn("订单核验：订单 ORD-2024-001 状态：已发货", result.content)
        self.assertIn("请说明退货原因", result.content)
        self.assertNotIn("签收后七天内", result.content)
        self.assertNotIn("[K1]", result.content)
        self.assertNotIn("知识来源", result.content)

    async def test_stale_process_chunk_never_reaches_model_or_customer(self):
        self.retriever.ainvoke.return_value = [Document(
            page_content="订单查询：当前主项目尚未接通真实订单业务接口和客户归属映射。",
            metadata={"doc_id": "old-process", "title": "客服通用流程"})]
        with patch("app.services.compound.tools.lookup_order", new_callable=AsyncMock,
                   return_value=OrderLookup("OK", "订单 ORD-2024-003 状态：待发货。")), \
                patch("app.services.compound.tools._record_kb_hits", new_callable=AsyncMock), \
                patch("app.services.compound.get_tenant_model", new_callable=AsyncMock) as model:
            plan = plan_compound("查订单 ORD-2024-003 状态并说明怎么退货")
            result = await execute_compound(self.app, "t_demo", "t_demo:buyer1", "问题", plan)
        model.assert_not_awaited()
        self.assertIn("ORD-2024-003 状态：待发货", result.content)
        self.assertNotIn("当前主项目尚未接通", result.content)

    async def test_qwen_composer_uses_non_thinking_mode(self):
        model = SimpleNamespace(model_name="qwen3.8-flash")
        model.ainvoke = AsyncMock(return_value=AIMessage(content="请说明退货原因，我们会核对课程交付情况。"))
        model.bind = lambda **kwargs: (self.assertEqual(
            kwargs, {"extra_body": {"enable_thinking": False}}) or model)
        with patch("app.services.compound.tools.lookup_order", new_callable=AsyncMock,
                   return_value=OrderLookup("OK", "订单 ORD-2024-003 状态：待发货。")), \
                patch("app.services.compound.tools._record_kb_hits", new_callable=AsyncMock), \
                patch("app.services.compound.get_tenant_model", new_callable=AsyncMock,
                      return_value=model):
            plan = plan_compound("查订单 ORD-2024-003 状态并说明怎么退货")
            result = await execute_compound(self.app, "t_demo", "t_demo:buyer1", "问题", plan)
        self.assertEqual(model.ainvoke.await_count, 1)
        self.assertIn("核对课程交付情况", result.content)
        self.assertNotIn("知识来源", result.content)

    async def test_missing_order_id_never_calls_order(self):
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content="政策见 [K1]。")))
        with patch("app.services.compound.tools.lookup_order", new_callable=AsyncMock) as lookup, \
                patch("app.services.compound.tools._record_kb_hits", new_callable=AsyncMock), \
                patch("app.services.compound.get_tenant_model", new_callable=AsyncMock,
                      return_value=model):
            plan = plan_compound("核对我的订单，再结合退货政策告诉我下一步")
            result = await execute_compound(self.app, "t_demo", "t_demo:guest1", "问题", plan)
        lookup.assert_not_awaited()
        self.assertIn("请提供订单号", result.content)

    async def test_empty_kb_never_invents_policy_or_calls_model(self):
        self.retriever.ainvoke.return_value = []
        with patch("app.services.compound.tools.lookup_order", new_callable=AsyncMock,
                   return_value=OrderLookup("OK", "订单已签收。")), \
                patch("app.services.compound.tools._record_kb_hits", new_callable=AsyncMock), \
                patch("app.services.compound.get_tenant_model", new_callable=AsyncMock) as model:
            plan = plan_compound("查订单 ORD-2024-001 状态并结合退货政策说明下一步")
            result = await execute_compound(self.app, "t_demo", "t_demo:buyer1", "问题", plan)
        model.assert_not_awaited()
        self.assertIn("无法判断具体政策", result.content)

    async def test_failed_ownership_still_escalates(self):
        with patch("app.services.compound.tools.lookup_order", new_callable=AsyncMock,
                   return_value=OrderLookup("FORBIDDEN", "无法核验")), \
                patch("app.services.compound.tools._record_kb_hits", new_callable=AsyncMock), \
                patch("app.services.compound.get_tenant_model", new_callable=AsyncMock) as model:
            plan = plan_compound("查订单 ORD-2024-001 状态并结合退货政策说明下一步")
            result = await execute_compound(self.app, "t_demo", "t_demo:buyer2", "问题", plan)
        model.assert_not_awaited()
        self.assertEqual(result.interrupt_data["code"], "FORBIDDEN")

    async def test_bare_order_id_resumes_prior_compound_question(self):
        state = SimpleNamespace(values={"messages": [
            HumanMessage(content="核对我的订单，再结合退货政策告诉我下一步"),
            AIMessage(content="请提供订单号并登录客户账号。"),
        ]})
        bridge = SimpleNamespace(aget_state=AsyncMock(return_value=state))
        app = SimpleNamespace(state=SimpleNamespace(faq_bridge=bridge))
        route = route_message("ORD-2024-001", "t_demo")
        plan, question = await _compound_plan(app, "t_demo:buyer1", "ORD-2024-001", route)
        self.assertEqual(question, "核对我的订单，再结合退货政策告诉我下一步")
        self.assertEqual([task.kind for task in plan.tasks], ["order", "kb"])

    async def test_direct_handoff_bypasses_compound_planner(self):
        route = route_message("请给我退款，订单号 ORD-2024-001", "t_demo")
        plan, _ = await _compound_plan(self.app, "t_demo:buyer1", "请给我退款，订单号 ORD-2024-001", route)
        self.assertIsNone(plan)

    async def test_order_reference_uses_verified_thread_order_id(self):
        plan, _ = await _compound_plan(self.app, "t_demo:buyer1", "刚才那单能退吗？",
                                       Route("order", order_id="ORD-2024-001"))
        self.assertEqual([task.kind for task in plan.tasks], ["order", "kb"])
        self.assertEqual(plan.order_id, "ORD-2024-001")


if __name__ == "__main__":
    unittest.main()
