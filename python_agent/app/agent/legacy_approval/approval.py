# 中文文案必然包含全角标点
"""旧图未完成审批的判定与恢复。

判定不建图、不调模型：直接读检查点里那条 ``__interrupt__`` 待写记录。这一点很关键——
如果为了判定而 ``get_tenant_agent()``，每个租户进程启动后第一次聊天都要额外构造一次
模型客户端，而这条判定在每个请求上都要跑。

判定结果是**三态**（:class:`ApprovalState`），不是布尔值：检查点读取失败时既不能
说"有旧审批"，也不能说"没有"——把数据库抖动当成"没有"会让新图与旧审批在同一会话里
并行跑（旧审批恢复后客户看到两套事实），把它当成"有"则会把每一次聊天都推回旧图。
第三种状态 ``UNKNOWN`` 交给调用方走可重试的基础设施错误。

恢复只在**原检查点**（旧图的 ``thread_id``）上做，绝不碰 ``support_v2:`` 前缀的键：
两张图的 state schema 不同，交叉恢复等于用新 schema 读旧状态。
"""

from __future__ import annotations

import logging
import os
from enum import Enum
from typing import Any

from langgraph.types import Command

from app.agent.support.service import NAMESPACE

logger = logging.getLogger("agent")

LEGACY_CHECKPOINT_NS = ""
"""旧图（HITL 转人工）检查点的命名空间：空串，即 ``thread_id`` 原样。"""

# [ours] S6 治理：图执行步数预算。AGENT_RECURSION_LIMIT=0（默认）保持 LangGraph 缺省 25，
# 设 >0 显式收紧。收敛后**只剩旧图 resume 会跑图**，所以这个预算跟着本模块走。
try:
    _RECURSION_LIMIT = max(0, int(os.getenv("AGENT_RECURSION_LIMIT", "0")))
except ValueError:
    _RECURSION_LIMIT = 0


class ApprovalState(str, Enum):
    """一条线程相对旧图审批的三种状态。"""

    PENDING = "pending"
    """确实停在旧图的中断上：新问题不能被回答，只能走 ``/agent/resume``。"""

    CLEAR = "clear"
    """确认没有未完成审批：新问题进四 Agent 主图。"""

    UNKNOWN = "unknown"
    """探测失败（检查点读不到）。**不等于**没有任何一方可以据此声称"没有审批"。"""


class ApprovalStateUnknown(RuntimeError):
    """检查点探测失败 —— 调用方必须按可重试的基础设施错误处理。"""


class NoPendingApproval(RuntimeError):
    """服务端检查点里没有未完成审批 —— resume 不该被调用。"""


def legacy_config(thread_id: str) -> dict[str, Any]:
    """旧图的运行配置（探活与 resume 共用同一把键）。

    ``AGENT_RECURSION_LIMIT>0`` 时注入步数预算；默认不注入，沿用框架缺省。
    """
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id,
                                               "checkpoint_ns": LEGACY_CHECKPOINT_NS}}
    if _RECURSION_LIMIT:
        config["recursion_limit"] = _RECURSION_LIMIT
    return config


def support_config(thread_id: str) -> dict[str, Any]:
    """四 Agent 主图的检查点键；与 ``SupportService`` 用的是同一个键。"""
    return {"configurable": {"thread_id": f"{NAMESPACE}:{thread_id}"}}


def _interrupt_writes(tuple_or_none: Any) -> list[Any]:
    if tuple_or_none is None:
        return []
    return [value for (_task_id, channel, value) in (tuple_or_none.pending_writes or [])
            if channel == "__interrupt__"]


def requires_checkpointer(app: Any) -> bool:
    """这个进程是否**必须**有检查点后端才能回答"有没有旧审批"。

    生产（配了 ``POSTGRES_DSN``）必须在启动时把 ``app.state.require_checkpointer`` 置真：
    否则一次装配意外（DSN 写错、saver 初始化失败）会让所有线程都被判成"没有旧审批"，
    而旧审批其实还躺在数据库里——那是新旧两条链路同会话并行的入口。离线替身与本机
    测试没有这个标记，仍然按 ``CLEAR`` 处理。
    """
    return bool(getattr(getattr(app, "state", None), "require_checkpointer", False))


async def approval_state(app: Any, thread_id: str) -> ApprovalState:
    """服务端事实：这条线程相对旧图审批处于哪一种状态。

    三态而不是布尔：``aget_tuple`` 抛异常时返回 :attr:`ApprovalState.UNKNOWN`，
    由调用方决定"暂停本轮并让业务重试"，而不是在此处猜一个答案。

    ``checkpointer`` 为 ``None`` 时分两种情形（:func:`requires_checkpointer`）：

    * 生产（必须要有检查点）→ ``UNKNOWN``：无法确认，宁可让业务重试也不能放行；
    * 离线替身/本机测试 → ``CLEAR``：本进程没有持久后端，这条线程在这个进程里
      本来就不可能有旧审批。
    """
    checkpointer = getattr(getattr(app, "state", None), "checkpointer", None)
    if checkpointer is None:
        if requires_checkpointer(app):
            logger.error("approval state unavailable: checkpointer missing thread=%s",
                         thread_id)
            return ApprovalState.UNKNOWN
        return ApprovalState.CLEAR
    try:
        record = await checkpointer.aget_tuple(legacy_config(thread_id))
    except Exception:
        logger.warning("approval state probe failed thread=%s", thread_id, exc_info=True)
        return ApprovalState.UNKNOWN
    return ApprovalState.PENDING if _interrupt_writes(record) else ApprovalState.CLEAR


async def has_unfinished_approval(app: Any, thread_id: str) -> bool:
    """严格版：``True`` 当且仅当确认停在旧图中断上。

    状态未知时抛 :class:`ApprovalStateUnknown` 而不是返回 ``False``——返回值只有两个
    取值时，"读失败"必然被误读成"没有审批"，那正是新旧两条链路同会话并行的入口。
    需要自己处理第三种状态的调用方请直接用 :func:`approval_state`。
    """
    state = await approval_state(app, thread_id)
    if state is ApprovalState.UNKNOWN:
        raise ApprovalStateUnknown(f"审批状态未知（检查点读取失败）：{thread_id}")
    return state is ApprovalState.PENDING


async def pending_approval_payload(app: Any, thread_id: str) -> dict[str, Any] | None:
    """中断载荷（前端弹窗用）。没有未完成审批、或探测失败时返回 None。

    只用于**展示**冲突细节；判定一律用 :func:`approval_state`，不靠这个函数的返回值。
    """
    checkpointer = getattr(getattr(app, "state", None), "checkpointer", None)
    if checkpointer is None:
        return None
    try:
        record = await checkpointer.aget_tuple(legacy_config(thread_id))
    except Exception:
        return None
    for group in _interrupt_writes(record):
        for item in group or []:
            value = getattr(item, "value", None)
            if isinstance(value, dict):
                return value
    return None


async def resume_approval(app: Any, tenant_id: str, thread_id: str, *,
                          confirmed: bool, wait_time: int, ticket_id: str) -> str:
    """在旧图原检查点上恢复一次审批，返回最终答复文本。

    只在服务端**确认**存在未完成审批时才允许调用；否则抛 :class:`NoPendingApproval`，
    避免把一次普通 resume 变成"在空检查点上跑一遍旧图、按默认分支凭空回答"。
    状态未知时抛 :class:`ApprovalStateUnknown`，调用方应让业务重试而不是当作已恢复。
    """
    if not await has_unfinished_approval(app, thread_id):
        raise NoPendingApproval("该会话没有未完成的转人工审批")
    from app.agent.builder import get_tenant_agent  # 延迟 import：避免请求期外的模型装配

    agent = await get_tenant_agent(app, tenant_id)
    result = await agent.ainvoke(
        Command(resume={"confirmed": confirmed, "wait_time": wait_time,
                        "ticket_id": ticket_id}),
        config=legacy_config(thread_id),
    )
    messages = result.get("messages", []) if isinstance(result, dict) else []
    if not messages:
        raise NoPendingApproval("resume 执行完成，但旧图没有返回消息")
    last = messages[-1]
    return last.content if isinstance(getattr(last, "content", ""), str) else str(last.content)
