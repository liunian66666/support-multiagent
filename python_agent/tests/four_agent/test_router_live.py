# 中文文案必然包含全角标点
"""真实模型的路由验收：只在配了 OpenAI 兼容端点时才跑。

脚本化 fake 只能证明「拿到 tool_calls 之后图怎么走」，证不了「真实模型看到 ROUTER_PROMPT
会不会派发」。这一步必须用真模型跑一次，否则「提示词没发出去」这类退化只能在线上发现：
``plan_turn`` 会读到模型的直答文本，整张图悄悄退化成单模型，而离线用例全绿。

判据刻意宽松——只要求「派对了关键角色」且「没有退化成直答」，不要求角色集完全相等：
模型多派一路（例如政策问题同时派 general 澄清）是可接受的浪费，不是契约违背。

运行：``AGENT_CHAT_BASE_URL=... AGENT_CHAT_MODEL=... pytest tests/four_agent/test_router_live.py``
"""

from __future__ import annotations

import os

import pytest
from langchain_core.messages import HumanMessage

from app.agent.support.plan import plan_turn

pytestmark = [
    # 必须 session 级事件循环：真实模型底下是 httpx 连接池，按用例新建 loop 时
    # 上一个 loop 关闭后连接池仍被 SDK 复用，第二个用例会抛 ``Event loop is closed``，
    # 表现为「0、2 过，1、3 挂」的交替失败——那是测试基础设施问题，不是路由问题。
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(not os.getenv("AGENT_CHAT_BASE_URL"),
                       reason="需要 AGENT_CHAT_BASE_URL（OpenAI 兼容端点）才能跑真实路由"),
]

CASES = [
    ("退货期限是多久？", {"faq"}),
    # 口语化、没有订单号的政策判定：规则判不出来，路由必须派 FAQ。
    ("我买的那个东西有点问题想退，这种情况能退吗？", {"faq"}),
    ("ORD-1001 到哪了？", {"order"}),
    # 复合问：两路都要到，缺一路这一轮就没做完。
    ("我的订单 ORD-1001 到哪了？另外退货运费谁承担？", {"faq", "order"}),
]


def _live_model() -> object:
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:  # pragma: no cover  只有精简镜像才缺
        pytest.skip("langchain_openai 未安装")
    return ChatOpenAI(model=os.getenv("AGENT_CHAT_MODEL", "mock-support-model"),
                      base_url=os.environ["AGENT_CHAT_BASE_URL"],
                      api_key=os.getenv("AGENT_CHAT_API_KEY", "not-needed"),
                      temperature=0)


@pytest.mark.parametrize("question,expected", CASES)
async def test_live_router_dispatches_the_expected_role(question: str,
                                                        expected: set[str]) -> None:
    decision, _used, error = await plan_turn(_live_model(), [HumanMessage(content=question)],
                                             question=question, timeout=40.0)

    assert not error, error
    assert decision.direct == "", f"路由退化成直答（ROUTER_PROMPT 可能没发出去）：{decision.direct[:80]}"
    assert expected <= set(decision.roles), decision.items
