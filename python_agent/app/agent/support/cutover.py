# ruff: noqa: RUF001
"""四 Agent 主图的生产桥接：身份 → 服务 → 一轮结果。

这个模块不再持有自己的服务缓存（那曾是"生产路径与调试路径各一份图"的来源）。服务
注册表在 :mod:`app.services.support_runtime`，这里只保留三个**可替换的缝**，它们同时
是既有测试的注入点：

* ``_service_for`` —— 取该租户的服务（默认走共享注册表）
* ``legacy_history`` —— 首次切到主图时一次性导入旧会话
* ``run_turn`` —— 返回 ``chat.py`` 读的契约字段（``answer`` / ``model_messages`` / …）

轮次执行体本身只有一份：``support_runtime.run_turn_result``。
"""

from __future__ import annotations

from typing import Any

from app.services.support_runtime import billing_messages, run_turn_result, runtime_for


def enabled(*, pending_ticket: bool = False) -> bool:
    """**已移除的开关的墓碑**：调用即报错，不返回任何"该走哪张图"的答案。

    收敛前这里返回 ``not pending_ticket``，聊天入口据此把未完成审批的会话送进旧图。
    现在普通聊天固定走主图，未完成审批由 ``/agent/chat`` 返回 409 ``approval_pending``、
    由 ``/agent/resume`` 恢复——没有任何调用方应该再问这个问题。保留函数只为让残留调用
    在第一次执行时就炸出明确原因，而不是悄悄回答一个已经不存在的问题。
    """
    raise RuntimeError(
        "four_agent.enabled 已退役：普通聊天固定走主图，未完成审批由 "
        "/agent/chat 返回 409 approval_pending、由 /agent/resume 恢复。"
    )


async def _service_for(app, tenant_id: str):
    """一个租户一个服务，来自**全进程唯一**的注册表。"""
    return await runtime_for(app).service_for(tenant_id)


def clear_services(tenant_id: str | None = None) -> None:
    """模型配置变更后丢弃按模型装配的图。"""
    from app.services.support_runtime import clear_all

    clear_all(tenant_id)


async def run_turn(app, tenant_id: str, thread_id: str, user_message: str,
                   turn_id: str = "", *, on_progress=None) -> dict[str, Any]:
    """跑一轮并返回 ``chat.py`` 需要的字段（JSON 与 SSE 共用同一份）。"""
    # 身份先于服务：建服务会建图，建图会触达模型与知识库，不可核验的线程必须在此之前失败。
    from .service import thread_key

    thread_key(tenant_id, thread_id)
    service = await _service_for(app, tenant_id)
    result = await run_turn_result(app, tenant_id, thread_id, user_message,
                                   turn_id=turn_id or None,
                                   on_progress=on_progress, service=service)
    return {"answer": result.answer,
            "turn_id": result.turn_id,
            "outcome": result.outcome,
            "usage": result.usage,
            "model_messages": billing_messages(result),
            "_replayed": result.replayed,
            "evidence_ids": result.evidence_ids,
            "review_issues": result.review_issues,
            "attempts": result.attempts,
            "delegations": [{"specialist": role} for role in result.roles]}


async def memory_state(app, tenant_id: str, thread_id: str) -> dict[str, int]:
    """``/agent/checkpoint`` 视图用的水位账本（宿主需要时才调）。"""
    return (await (await _service_for(app, tenant_id)).memory_state(tenant_id, thread_id))
