"""The progress transport reports graph phases without exposing a draft."""

import asyncio
import unittest
from dataclasses import replace

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.support.fakes import ScriptedSupportModel, offline_deps
from app.agent.support.service import SupportService
from app.services.experience_recall import match_experiences


class ProgressTest(unittest.TestCase):
    def test_supervisor_can_match_approved_route_specific_experience(self):
        items = [{"id": 1, "route": "faq", "content": "先核对政策再回答",
                  "applicability": {"keywords": ["退款"]}, "confidence": 1}]
        self.assertEqual(match_experiences(items, "", "退款政策")[0]["id"], 1)

    def test_graph_calls_optional_experience_recall(self):
        async def run():
            seen = []

            async def deps_for_tenant(_tenant):
                deps, *_ = offline_deps(model=ScriptedSupportModel(
                    supervisor_script=[AIMessage(content="已核实的答复。")]))
                async def recall(question):
                    seen.append(question)
                    return [{"content": "先核对资料再回答"}]
                return replace(deps, recall_experience=recall)

            service = SupportService(deps_for_tenant, checkpointer=InMemorySaver())
            result = await service.run_turn("t_demo", "t_demo:experience-test", "退款政策？")
            self.assertTrue(result.answer)
            self.assertTrue(seen)
            self.assertTrue(all(question == "退款政策？" for question in seen))

        asyncio.run(run())

    def test_progress_keeps_the_reviewed_checkpoint_answer(self):
        async def run():
            events = []

            async def deps_for_tenant(_tenant):
                deps, *_ = offline_deps(model=ScriptedSupportModel(
                    supervisor_script=[AIMessage(content="已核实的答复。")]))
                return deps

            service = SupportService(deps_for_tenant, checkpointer=InMemorySaver())
            result = await service.run_turn(
                "t_demo", "t_demo:progress-test", "你好", turn_id="progress-1",
                on_progress=lambda node: record(events, node))
            self.assertEqual(result.answer, "已核实的答复。")
            self.assertEqual(events[0], "open_turn")
            self.assertIn("review", events)
            self.assertIn("finalize", events)
            self.assertEqual((await service.history("t_demo", "t_demo:progress-test"))[-1]["content"],
                             result.answer)

        asyncio.run(run())


async def record(events, node):
    events.append(node)


if __name__ == "__main__":
    unittest.main()
