"""FAQ 能力：知识库专职 Agent 与历史读取的稳定入口。

两条性质必须由这一层保证（它们曾被两处实现各写一遍，然后各自漂移）：

* **只读**：FAQ Agent 只有一个 ``search_kb`` 工具，没有工单、退款、支付能力；
* **无第二份记忆**：专职 Agent 是无状态的，历史由调用方按快照喂入。
"""

from __future__ import annotations

from typing import Any


async def faq_agent(app: Any, tenant_id: str, model_override: Any = None) -> Any:
    """生产用 FAQ Agent（沿用现有实现的检索缓存与 kb_hits 埋点）。"""
    from app.agent.faq_agent import faq_agent as existing

    if model_override is None:
        return await existing(app, tenant_id)
    return await existing(app, tenant_id, model_override=model_override)


async def history(app: Any, thread_id: str, limit: int = 12) -> list[Any]:
    """客户可见的历史快照（只取人与 AI 消息，不带工具轨迹）。"""
    from app.agent.faq_agent import recent_messages

    return list(await recent_messages(app, thread_id, limit=limit))


__all__ = ["faq_agent", "history"]
