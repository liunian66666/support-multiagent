# 中文文案必然包含全角标点
"""对话端点：/agent/chat、/agent/chat/stream、/agent/resume、/agent/checkpoint/{thread_id}。

生产契约的四条线在这里被固定下来：

1. **一个轮次 ID**：``turn_id`` 来自请求体；旧调用方没带时按 ``X-Trace-Id`` 兼容。
   ``answer_id`` 恒等于它，JSON 与 SSE 因此可以按同一个锚点归因。
2. **一个执行体**：普通聊天只经过 :func:`_primary_turn`（四 Agent 主图）。旧图**不再**
   由聊天入口触发——未完成审批时返回结构化 409，旧图只能从 ``/agent/resume`` 进入。
   这样"待处理工单"不再被任何一层当成"选旧 Agent"的开关。
3. **一个结果映射**：两种传输都只读同一份轮次结果（``answer`` / ``outcome`` /
   ``turn_id`` / ``model_messages``），差别仅在信封与分帧。
4. **一种未知状态**：检查点探测失败返回 503 且 ``retryable=true``，既不放行也不冒充
   "没有审批"（见 :mod:`app.api.approval_gate`）。
"""
import json as _json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.agent.legacy_approval.approval import ApprovalState, ApprovalStateUnknown, NoPendingApproval, resume_approval
from app.agent.support.service import NAMESPACE, TurnConflictError
from app.api.approval_gate import ERROR_APPROVAL_UNKNOWN, require_clear
from app.api.deps import _require_trusted_chat
from app.chat.reply import _friendly_llm_error
from app.infra.config import SERVICE_FOOTER
from app.infra.context import pending_ticket_var, tenant_var, thread_var, trace_id_var
from app.infra.db.quota import check_concurrent_sessions
from app.infra.db.session import touch_session_retry
from app.infra.db_pool import _pool_conn
from app.schemas import AgentResp, ChatRequest, ResumeRequest
from app.services import four_agent
from app.services import trace as agent_trace
from app.services.dialog import _record_usage_after_chat
from app.services.supervisor import _compound_plan as _compound_plan  # noqa: F401  向后兼容的再导出

logger = logging.getLogger("agent")
router = APIRouter()

ERROR_TURN_CONFLICT = "turn_conflict"
ERROR_INTERNAL = "agent_error"
ERROR_NO_PENDING_APPROVAL = "no_pending_approval"
TURN_CONFLICT_DETAIL = "同一个 turn_id 已用于另一个问题；重试请复用原问题，新问题请生成新的 turn_id"

_PHASES = {"open_turn": "searching", "supervisor": "checking",
           "review": "reviewing", "finalize": "finalizing"}


def _resolve_turn_id(req: ChatRequest, run) -> str:
    """显式 ``turn_id`` 优先；旧调用方（业务层尚未升级）按 ``X-Trace-Id`` 兼容。

    兼容规则只写在这里：一旦 JSON 用 trace、SSE 用 turn_id，同一轮的两个响应就会拿到
    不同的 ``answer_id``，评价归因立刻错位。
    """
    explicit = (req.turn_id or "").strip()
    return explicit or run.trace_id


def _frame(payload: dict) -> str:
    return f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n"


def _invalid_request() -> AgentResp:
    return AgentResp(success=False, is_interrupt=False, error_code="invalid_request",
                     content="thread_id 和 user_message 不能为空")


async def _quota_error(request, tenant_id: str) -> str:
    """并发活跃会话配额（JSON 与 SSE 同一处判定）。"""
    async with _pool_conn(request.app) as conn:
        quota = await check_concurrent_sessions(conn, request.app, tenant_id)
    return quota["msg"] if quota else ""


async def _gate(request, thread_id: str, turn_id: str):
    """审批闸门 + trace 归因：放行前先记下"这一轮为什么能/不能执行"。"""
    try:
        approval = await require_clear(request, thread_id, turn_id)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        agent_trace.record("route", "approval_blocked",
                           error_code=detail.get("error_code", ""),
                           status=exc.status_code)
        raise
    agent_trace.record("route", "four_agent", approval_state=approval.value)
    return approval


async def _primary_turn(request, tenant_id: str, thread_id: str, user_message: str,
                        turn_id: str, *, on_progress=None) -> dict:
    """四 Agent 主图的一轮（JSON 与 SSE 共用的唯一入口）。"""
    await touch_session_retry(request.app, thread_id)
    return await four_agent.run_turn(request.app, tenant_id, thread_id, user_message,
                                     turn_id, on_progress=on_progress)


async def _bill_once(request, tenant_id: str, thread_id: str, state: dict) -> None:
    """重放的那一轮不再计费（模型根本没被调用）。"""
    if state.get("_replayed"):
        return
    messages = state.get("model_messages", [])
    if not messages:
        return
    async with _pool_conn(request.app) as conn:
        await _record_usage_after_chat(request, conn, tenant_id, thread_id, messages)


def _primary_resp(state: dict) -> AgentResp:
    """主图轮次 → 响应。锚点、结果状态、专职 Agent 列表都取自同一份 state。"""
    turn_id = state.get("turn_id") or ""
    run = agent_trace.current()
    if run is not None and turn_id:
        run.answer_id = turn_id  # trace 与响应同一个锚点
    roles = list(dict.fromkeys(item["specialist"] for item in state.get("delegations") or []))
    return AgentResp(success=True, is_interrupt=False,
                     content=state["answer"] + SERVICE_FOOTER,
                     turn_id=turn_id or None, answer_id=turn_id or None,
                     outcome=state.get("outcome") or None,
                     agents=roles or None,
                     evidence_ids=list(state.get("evidence_ids") or []) or None)


@router.post("/agent/chat", response_model=AgentResp)
async def agent_chat(req: ChatRequest, request: Request):
    thread_id = req.thread_id.strip()
    if not thread_id or not req.user_message.strip():
        return _invalid_request()
    tenant_id = _require_trusted_chat(request, thread_id)
    run = agent_trace.new_trace(request.headers.get("X-Trace-Id", ""),
                                tenant_id, thread_id)
    turn_id = _resolve_turn_id(req, run)
    run.answer_id = turn_id  # 失败轮次也带着同一个锚点进 trace
    token = agent_trace.bind(run)
    status = "failed"
    try:
        result = await _chat_json(req, request, tenant_id, thread_id, turn_id)
        result.trace_id = run.trace_id
        if result.answer_id:
            run.answer_id = result.answer_id
        status = ("interrupted" if result.is_interrupt else
                  "completed" if result.success else "failed")
        return result
    finally:
        await run.finish(request.app, status)
        agent_trace.reset(token)


async def _chat_json(req: ChatRequest, request: Request, tenant_id: str,
                     thread_id: str, turn_id: str) -> AgentResp:
    run = agent_trace.current()
    trace_id_var.set(run.trace_id if run is not None else turn_id)
    tenant_var.set(tenant_id)
    thread_var.set(thread_id)
    quota_msg = await _quota_error(request, tenant_id)
    if quota_msg:
        return AgentResp(success=False, is_interrupt=False, error_code="quota_exceeded",
                         turn_id=turn_id, answer_id=turn_id, content=quota_msg)

    # 服务端事实先行：未完成审批 ⇒ 409（旧图只从 /agent/resume 恢复），探测失败 ⇒ 503。
    # 请求体的 has_pending_ticket 只表示业务层在排队，不参与判定。
    await _gate(request, thread_id, turn_id)
    pending_ticket_var.set(bool(req.has_pending_ticket))
    try:
        state = await _primary_turn(request, tenant_id, thread_id,
                                    req.user_message.strip(), turn_id)
    except TurnConflictError as exc:
        agent_trace.record("finish", "turn_conflict")
        raise HTTPException(status_code=409, detail=TURN_CONFLICT_DETAIL) from exc
    except Exception as exc:
        logger.exception("/agent/chat four-agent graph error")
        return AgentResp(success=False, is_interrupt=False, error_code=ERROR_INTERNAL,
                         turn_id=turn_id, answer_id=turn_id,
                         content=_friendly_llm_error(exc))
    await _bill_once(request, tenant_id, thread_id, state)
    return _primary_resp(state)


@router.post("/agent/chat/stream")
async def agent_chat_stream(req: ChatRequest, request: Request):
    """Agent 对话流式输出（SSE）。

    事件格式（与 JSON 的字段同名，便于业务层用一套解析）：

      data: {"type":"progress","phase":"searching|checking|reviewing|finalizing"}
      data: {"type":"chunk","content":"..."}                      # 最终答复（一次性）
      data: {"type":"end","content":"...","answer_id":"...","turn_id":"...","outcome":"..."}
      data: {"type":"error","msg":"...","code":"turn_conflict|..."}

    未完成审批与审批状态未知**不**用错误帧表达：它们在建立流之前就以 409 / 503 返回，
    ``detail.error_code`` 分别是 ``approval_pending`` 与 ``approval_state_unknown``。
    这样业务层可以按状态码分流，而不是把一个 HTTP 200 的流读到一半才发现没有答复。
    """
    thread_id = req.thread_id.strip()
    user_message = req.user_message.strip()
    verified_tenant = _require_trusted_chat(request, thread_id)
    run = agent_trace.new_trace(request.headers.get("X-Trace-Id", ""),
                                verified_tenant, thread_id)
    turn_id = _resolve_turn_id(req, run)
    run.answer_id = turn_id
    trace_id_var.set(run.trace_id)
    tenant_var.set(verified_tenant)
    thread_var.set(thread_id)

    async def invalid_stream():
        yield _frame({"type": "error", "code": "invalid_request",
                      "msg": "thread_id 和 user_message 不能为空"})

    if not thread_id or not user_message:
        return StreamingResponse(invalid_stream(), media_type="text/event-stream")

    token = agent_trace.bind(run)
    try:
        await _gate(request, thread_id, turn_id)
    except HTTPException:
        await run.finish(request.app, "failed")
        raise
    finally:
        agent_trace.reset(token)

    async def event_generator():
        pending_ticket_var.set(bool(req.has_pending_ticket))
        async for frame in _primary_stream(request, verified_tenant, thread_id,
                                           user_message, turn_id):
            yield frame

    async def traced_generator():
        token = agent_trace.bind(run)
        status = "failed"
        try:
            async for frame in event_generator():
                if frame.startswith("data: "):
                    try:
                        kind = _json.loads(frame[6:].strip()).get("type")
                    except (ValueError, AttributeError):
                        kind = None
                    if kind == "end":
                        status = "completed"
                    elif kind == "interrupt":
                        status = "interrupted"
                    elif kind == "error":
                        status = "failed"
                yield frame
        finally:
            await run.finish(request.app, status)
            agent_trace.reset(token)

    return StreamingResponse(traced_generator(), media_type="text/event-stream")


async def _primary_stream(request, tenant_id: str, thread_id: str, user_message: str,
                          turn_id: str):
    """主图轮次的 SSE 分帧。

    刻意**不**逐 token 透传：review_guard 与检查点都在 finalize 之后才完成，把未过审的
    草稿推给客户等于绕过评审。这里只在进度上给反馈，正文一次性发出。
    """
    import asyncio

    progress: asyncio.Queue = asyncio.Queue()

    async def on_progress(node: str) -> None:
        # Only public phase names leave Agent; never drafts, tools or evidence.
        phase = _PHASES.get(node)
        if phase:
            progress.put_nowait(phase)

    task = asyncio.create_task(_primary_turn(request, tenant_id, thread_id,
                                             user_message, turn_id,
                                             on_progress=on_progress))
    try:
        last_phase = ""
        while not task.done() or not progress.empty():
            try:
                phase = await asyncio.wait_for(progress.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if phase != last_phase:
                yield _frame({"type": "progress", "phase": phase})
                last_phase = phase
        state = await task
    except TurnConflictError:
        agent_trace.record("finish", "turn_conflict")
        yield _frame({"type": "error", "code": ERROR_TURN_CONFLICT,
                      "msg": TURN_CONFLICT_DETAIL})
        return
    except Exception as exc:
        logger.exception("/agent/chat/stream four-agent graph error")
        yield _frame({"type": "error", "code": ERROR_INTERNAL,
                      "msg": _friendly_llm_error(exc)})
        return
    finally:
        if not task.done():
            task.cancel()

    await _bill_once(request, tenant_id, thread_id, state)
    content = state["answer"] + SERVICE_FOOTER
    answer_id = state.get("turn_id") or turn_id
    run = agent_trace.current()
    if run is not None:
        run.answer_id = answer_id
    # 正文与 end 帧同一份文本：业务层若先收到 chunk 又收到 end，拼接不会重复。
    yield _frame({"type": "chunk", "content": content})
    yield _frame({"type": "end", "content": content, "answer_id": answer_id,
                  "turn_id": answer_id, "outcome": state.get("outcome", ""),
                  "agents": list(dict.fromkeys(item["specialist"]
                                               for item in state.get("delegations") or [])),
                  "evidence_ids": list(state.get("evidence_ids") or [])})


@router.post("/agent/resume", response_model=AgentResp)
async def agent_resume(req: ResumeRequest, request: Request):
    """恢复一次未完成的转人工审批 —— **旧图唯一的公开入口**。

    只在**服务端检查点**里确实存在未完成审批时执行；没有就明确报错，而不是在空检查点
    上把旧图按默认分支跑一遍、凭空给客户一句答复。审批状态未知时返回
    ``approval_state_unknown``（可重试），业务层据此重试而不是当作"已恢复"。
    恢复完成后，新提问自然回到主图。
    """
    trace_id_var.set(request.headers.get("X-Trace-Id", "-"))
    thread_id = req.thread_id.strip()
    tenant_id = _require_trusted_chat(request, thread_id)  # [ours] 原为 tenant_from_thread
    tenant_var.set(tenant_id)
    thread_var.set(thread_id)

    # P1-2：并发活跃会话配额校验
    quota_msg = await _quota_error(request, tenant_id)
    if quota_msg:
        return AgentResp(success=False, is_interrupt=False, error_code="quota_exceeded",
                         content=quota_msg)

    try:
        await touch_session_retry(request.app, thread_id)
        content = await resume_approval(request.app, tenant_id, thread_id,
                                        confirmed=req.confirmed,
                                        wait_time=req.wait_time,
                                        ticket_id=req.ticket_id)
        return AgentResp(success=True, is_interrupt=False, content=content)
    except NoPendingApproval as exc:
        return AgentResp(success=False, is_interrupt=False,
                         error_code=ERROR_NO_PENDING_APPROVAL, content=str(exc))
    except ApprovalStateUnknown:
        logger.warning("/agent/resume approval state unknown thread=%s", thread_id)
        return AgentResp(success=False, is_interrupt=False,
                         error_code=ERROR_APPROVAL_UNKNOWN,
                         content="无法确认该会话的审批状态（检查点读取失败），请稍后重试")
    except Exception:
        logger.exception("/agent/resume error")
        # [ours] S6 治理：统一错误文案，不回吐 str(e)（内部异常细节不外泄给业务/前端）
        return AgentResp(success=False, is_interrupt=False, error_code=ERROR_INTERNAL,
                         content="resume服务内部异常")


@router.get("/agent/approval/{thread_id}")
async def get_approval_state(thread_id: str, request: Request):
    """轻量探测：只回三态，不查检查点表、不需要数据库连接池。

    业务层在**重试 resume 之前**必须问一次这里：超时之后服务端可能已经恢复完了，
    再 resume 一次虽然幂等，但会白跑一轮旧图；而"读不到"也不能被当成"已恢复"。
    """
    _require_trusted_chat(request, thread_id)
    state = await four_agent.approval_state(request.app, thread_id)
    return {"thread_id": thread_id, "approval_state": state.value,
            "checkpointer": bool(getattr(request.app.state, "checkpointer", None))}


# 调试接口：查询两条检查点键（主图 + 旧审批），并标明图版本
@router.get("/agent/checkpoint/{thread_id}")
async def get_checkpoint(thread_id: str, request: Request):
    """列出这条会话在两个命名空间下的检查点。

    两张图用两把键：主图 ``support_v2:{thread_id}``，旧审批 ``{thread_id}``。只查后者会让
    重启后的新会话看起来"没有检查点"，只查前者则会把一个仍需恢复的旧审批藏起来。
    """
    _require_trusted_chat(request, thread_id)
    keys = ((f"{NAMESPACE}:{thread_id}", "four_agent"),
            (thread_id, "legacy_approval"))
    result = []
    async with _pool_conn(request.app) as conn:
        async with conn.cursor() as cur:
            for key, graph in keys:
                await cur.execute("""
                    SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, metadata
                    FROM checkpoints
                    WHERE thread_id = %s AND checkpoint_ns = ''
                    ORDER BY checkpoint_id DESC
                """, (key,))
                for row in await cur.fetchall():
                    result.append({
                        "graph": graph,
                        "checkpoint_key": key,
                        "thread_id": row[0],
                        "checkpoint_ns": row[1],
                        "checkpoint_id": row[2],
                        "parent_checkpoint_id": row[3],
                        "metadata": row[4],
                    })
    approval = await four_agent.approval_state(request.app, thread_id)
    return {"data": result,
            "turn_key": f"{NAMESPACE}:{thread_id}",
            # 布尔是历史字段（只有 true/false 两种读法），三态看 approval_state。
            "pending_approval": approval is ApprovalState.PENDING,
            "approval_state": approval.value}


__all__ = ["agent_chat", "agent_chat_stream", "agent_resume", "get_approval_state",
           "get_checkpoint", "router"]
