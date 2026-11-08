"""Account questions must never inherit an unrelated order fallback."""

import asyncio
import unittest

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.support import guards
from app.agent.support.fakes import ScriptedSupportModel, offline_deps
from app.agent.support.service import SupportService

QUESTION = "我是谁？我的密码是什么？"


class AccountFallbackTest(unittest.TestCase):
    def test_order_review_reason_does_not_change_account_reply(self):
        answer = guards.safe_fallback(
            ["引用了本轮未经业务接口核验归属的订单号"], [], [], QUESTION)
        self.assertIn("身份", answer)
        self.assertIn("密码", answer)
        self.assertNotIn("这笔订单", answer)

    def test_no_result_fallback_uses_question_type(self):
        self.assertIn("政策", guards.safe_fallback([], [], [], "店铺退款政策是什么？"))
        self.assertIn("订单", guards.safe_fallback([], [], [], "我的订单在哪里？"))
        self.assertNotIn("订单", guards.safe_fallback([], [], [], "你能做什么？"))

    def test_account_reply_is_reviewed_without_model_guessing(self):
        async def run():
            model = ScriptedSupportModel(fail_supervisor=True)

            async def deps_for_tenant(_tenant):
                deps, *_ = offline_deps(model=model)
                return deps

            service = SupportService(deps_for_tenant, checkpointer=InMemorySaver())
            result = await service.run_turn("t_demo", "t_demo:account-question", QUESTION)
            self.assertEqual(result.outcome, "answered")
            self.assertIn("无法仅凭聊天确认您的真实身份", result.answer)
            self.assertIn("无法查看或提供账号密码", result.answer)
            self.assertNotIn("订单", result.answer)
            self.assertEqual(model.recorded, [])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
