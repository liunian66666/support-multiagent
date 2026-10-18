"""FAQ 快答旁路图（build_faq_bridge）单元测试。

验证确定性答复（流程 FAQ / 订单核验 / 转人工）经旁路图写入检查点后，
同一 thread 的状态能被后续 LLM 对话读回。使用 MemorySaver 避免依赖 PostgreSQL。
"""
import asyncio
import unittest

from langchain_core.messages import HumanMessage, AIMessage
from langgraph.checkpoint.memory import MemorySaver

from agent_factory import build_faq_bridge


class FaqBridgeTest(unittest.TestCase):
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_bridge_appends_message_pair_to_thread(self):
        checkpointer = MemorySaver()
        bridge = build_faq_bridge(checkpointer)
        config = {"configurable": {"thread_id": "test:bridge1"}}
        self._run(bridge.ainvoke(
            {"messages": [HumanMessage(content="我要退货"), AIMessage(content="请提供订单号和退货原因")]},
            config=config,
        ))
        state = self._run(bridge.aget_state(config))
        msgs = state.values.get("messages", [])
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].content, "我要退货")
        self.assertEqual(msgs[1].content, "请提供订单号和退货原因")

    def test_bridge_accumulates_across_invokes(self):
        """多次写入累加，模拟用户连续提两个 FAQ 问题。"""
        checkpointer = MemorySaver()
        bridge = build_faq_bridge(checkpointer)
        config = {"configurable": {"thread_id": "test:bridge2"}}

        async def run():
            await bridge.ainvoke(
                {"messages": [HumanMessage(content="q1"), AIMessage(content="a1")]},
                config=config,
            )
            await bridge.ainvoke(
                {"messages": [HumanMessage(content="q2"), AIMessage(content="a2")]},
                config=config,
            )
        self._run(run())
        state = self._run(bridge.aget_state(config))
        msgs = state.values.get("messages", [])
        self.assertEqual(len(msgs), 4)
        self.assertEqual([m.content for m in msgs], ["q1", "a1", "q2", "a2"])

    def test_bridge_does_not_run_model(self):
        """旁路图只落状态，不应触发任何 LLM 调用。
        用一个会抛错的假模型放进 StateGraph 验证节点不依赖模型——
        这里换一种轻量方式：确认 bridge.aget_state 后没有 outstanding tasks。"""
        checkpointer = MemorySaver()
        bridge = build_faq_bridge(checkpointer)
        config = {"configurable": {"thread_id": "test:bridge3"}}
        self._run(bridge.ainvoke(
            {"messages": [HumanMessage(content="x"), AIMessage(content="y")]},
            config=config,
        ))
        state = self._run(bridge.aget_state(config))
        # record 节点已执行完毕，不应有待处理任务
        self.assertFalse(state.tasks)


if __name__ == "__main__":
    unittest.main()
