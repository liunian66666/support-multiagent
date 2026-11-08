# 中文文案必然包含全角标点
"""未完成审批的唯一闸门：``/agent/chat``、``/agent/chat/stream``、``/agent/chat/v2``
三个生产入口共用这一份语义。

规则只有一条：**只要服务端检查点里还停着旧图的转人工审批，本轮就不执行任何 Agent**
（既不进四 Agent，也不让旧 FAQ/Intake 回答新问题），而是返回结构化冲突，让业务层先
把这条审批走完（``/agent/resume``）。恢复之后，新问题自然回到四 Agent。

三种结果：

===================  ========  ====================================================
审批状态               HTTP      业务层该做什么
===================  ========  ====================================================
``clear``             放行       正常提问
``pending``           409        有工单就提示等待；没工单就建/复用工单 + resume
``unknown``           503        可重试的基础设施错误，**不要**建工单、不要当作已回答
===================  ========  ====================================================

``unknown`` 单独成一种状态，是因为"检查点读不到"既不能证明有旧审批、也不能证明没有；
折成 409 会让客户无谓地排队，折成放行则会让新图与旧审批在同一会话里并行。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from starlette.requests import Request

from app.agent.legacy_approval.approval import ApprovalState
from app.agent.support.service import NAMESPACE
from app.services import four_agent

ERROR_APPROVAL_PENDING = "approval_pending"
ERROR_APPROVAL_UNKNOWN = "approval_state_unknown"

RESUME_ENDPOINT = "/agent/resume"

APPROVAL_PENDING_DETAIL = (
    "该会话有未完成的转人工审批：本轮不回答新问题（旧 Agent 不会被调用）。"
    f"请先调用 {RESUME_ENDPOINT} 完成该审批，恢复后新问题会自动回到四 Agent。"
)
APPROVAL_UNKNOWN_DETAIL = (
    "无法确认该会话是否还有未完成的转人工审批（检查点读取失败），本轮未执行。"
)

__all__ = [
    "APPROVAL_PENDING_DETAIL",
    "APPROVAL_UNKNOWN_DETAIL",
    "ERROR_APPROVAL_PENDING",
    "ERROR_APPROVAL_UNKNOWN",
    "RESUME_ENDPOINT",
    "checkpoint_keys",
    "conflict_payload",
    "require_clear",
    "unknown_payload",
]


def checkpoint_keys(thread_id: str) -> list[str]:
    """两张图的两把键，按"主图, 旧审批"顺序给出（排障时一眼看出查过哪两处）。"""
    return [f"{NAMESPACE}:{thread_id}", thread_id]


def conflict_payload(thread_id: str, turn_id: str = "", *,
                     pending_approval: dict[str, Any] | None = None) -> dict[str, Any]:
    """409 的 ``detail``：机器可读字段在前，给客户看的文案在后。"""
    return {
        "error_code": ERROR_APPROVAL_PENDING,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "retryable": False,
        "resume_endpoint": RESUME_ENDPOINT,
        "checkpoint_keys": checkpoint_keys(thread_id),
        "pending_approval": pending_approval,
        "detail": APPROVAL_PENDING_DETAIL,
    }


def unknown_payload(thread_id: str, turn_id: str = "") -> dict[str, Any]:
    return {
        "error_code": ERROR_APPROVAL_UNKNOWN,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "retryable": True,
        "detail": APPROVAL_UNKNOWN_DETAIL,
    }


async def require_clear(request: Request, thread_id: str,
                        turn_id: str = "") -> ApprovalState:
    """闸门：只有确认没有未完成审批时返回；否则抛 409 / 503。

    返回值恒为 :attr:`ApprovalState.CLEAR`，保留返回类型只是为了让调用点显式地
    "我知道这里查过一次状态"。
    """
    state = await four_agent.approval_state(request.app, thread_id)
    if state is ApprovalState.UNKNOWN:
        raise HTTPException(status_code=503, detail=unknown_payload(thread_id, turn_id))
    if state is ApprovalState.PENDING:
        pending = await four_agent.pending_approval_payload(request.app, thread_id)
        raise HTTPException(status_code=409,
                            detail=conflict_payload(thread_id, turn_id,
                                                    pending_approval=pending))
    return state
