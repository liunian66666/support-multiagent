# 中文文案必然包含全角标点
"""生产入口对四 Agent 主图的适配层。

这里是**唯一**的执行入口：普通聊天固定走主图，与"有没有待处理工单"无关。

旧图只剩一条独立公开入口——``/agent/resume``，由 :mod:`app.agent.legacy_approval`
在**服务端确认存在未完成审批**时驱动。``/agent/chat`` 系列遇到未完成审批时不会再顺手
让旧图回答新问题，而是返回结构化的 ``approval_pending`` 冲突。

关于探测结果的三态请直接看 :func:`approval_state`：``UNKNOWN``（检查点读不到）不是
"没有审批"，调用方必须把它当可重试的基础设施错误。
"""

from __future__ import annotations

from app.agent.legacy_approval.approval import ApprovalState, approval_state
from app.agent.legacy_approval.approval import (
    pending_approval_payload as _pending_approval_payload,
)
from app.agent.support.cutover import clear_services
from app.agent.support.cutover import run_turn as graph_run_turn

__all__ = ["ApprovalState", "approval_state", "clear_cached", "pending_approval_payload",
           "run_turn"]


async def pending_approval_payload(app, thread_id: str):
    """中断载荷（只用于展示冲突细节）。"""
    return await _pending_approval_payload(app, thread_id)


async def run_turn(app, tenant_id: str, thread_id: str, user_message: str,
                   turn_id: str = "", *, on_progress=None) -> dict:
    return await graph_run_turn(app, tenant_id, thread_id, user_message, turn_id,
                                on_progress=on_progress)


def clear_cached(tenant_id: str | None = None) -> None:
    clear_services(tenant_id)
