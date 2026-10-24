"""对话端点：/agent/chat、/agent/chat/stream、/agent/resume、/agent/checkpoint/{thread_id}。"""
import logging

from fastapi import APIRouter, Request
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from app.agent.builder import get_tenant_agent
from app.agent.faq_agent import faq_agent as get_faq_specialist
from app.agent.faq_agent import recent_messages
from app.agent.intake_agent import intake_agent as get_intake_specialist
from app.api.deps import _require_trusted_chat
from app.chat.reply import finalize_faq_reply, _friendly_llm_error, demo_faq_prefix, mark_demo_faq_reply
from app.infra.config import PROD_MODE, SERVICE_FOOTER
from app.infra.context import pending_ticket_var, tenant_var, thread_var, trace_id_var
from app.infra.db.quota import check_concurrent_sessions
from app.infra.db.session import touch_session_retry
from app.infra.db_pool import _pool_conn
from app.schemas import AgentResp, ChatRequest, ResumeRequest
from app.services.dialog import _record_to_checkpoint, _record_usage_after_chat
from app.services.specialists import execute_shortcut
from app.services.supervisor import _compound_plan as _compound_plan
from app.services.supervisor import decide_turn
from app.services import trace as agent_trace

logger = logging.getLogger("agent")
router = APIRouter()


@router.post("/agent/chat", response_model=AgentResp)
async def agent_chat(req: ChatRequest, request: Request):
    thread_id = req.thread_id.strip()
    if not thread_id or not req.user_message.strip():
        return await _agent_chat_impl(req, request)
    tenant_id = _require_trusted_chat(request, thread_id)
    run = agent_trace.new_trace(request.headers.get("X-Trace-Id", ""),
                                tenant_id, thread_id)
    token = agent_trace.bind(run)
    status = "failed"
    try:
        result = await _agent_chat_impl(req, request)
        result.trace_id = run.trace_id
        status = ("interrupted" if result.is_interrupt else
                  "completed" if result.success else "failed")
        return result
    finally:
        await run.finish(request.app, status)
        agent_trace.reset(token)


async def _agent_chat_impl(req: ChatRequest, request: Request):
    active_run = agent_trace.current()
    trace_id_var.set(active_run.trace_id if active_run else
                     request.headers.get("X-Trace-Id", "-"))
    thread_id = req.thread_id.strip()
    user_message = req.user_message.strip()
    if not thread_id or not user_message:
        return AgentResp(
            success=False,
            is_interrupt=False,
            content="thread_id 和 user_message 不能为空"
        )

    tenant_id = _require_trusted_chat(request, thread_id)  # [ours] 原为 tenant_from_thread，改为可信入口校验
    tenant_var.set(tenant_id)
    thread_var.set(thread_id)
    pending_ticket_var.set(req.has_pending_ticket)
    decision = await decide_turn(request.app, tenant_id, thread_id, user_message,
                                 pending_ticket=req.has_pending_ticket)
    logger.info("support specialist=%s source=%s tenant=%s",
                decision.specialist, decision.source, tenant_id)
    try:
        shortcut = await execute_shortcut(request.app, tenant_id, thread_id,
                                          user_message, decision,
                                          pending_ticket=req.has_pending_ticket)
    except Exception as e:
        logger.exception("/agent/chat specialist error")
        return AgentResp(success=False, is_interrupt=False,
                         content=_friendly_llm_error(e))
    if shortcut is not None:
        if shortcut.model_messages:
            async with _pool_conn(request.app) as conn:
                await _record_usage_after_chat(request, conn, tenant_id, thread_id,
                                               shortcut.model_messages)
        if shortcut.interrupt_data:
            return AgentResp(success=True, is_interrupt=True,
                             interrupt_data=shortcut.interrupt_data)
        return AgentResp(success=True, is_interrupt=False,
                         content=shortcut.content + SERVICE_FOOTER)
    route = decision.route
    specialist_faq = route.specialist == "faq"
    agent = (await get_faq_specialist(request.app, tenant_id) if specialist_faq
             else await get_intake_specialist(request.app, tenant_id))
    input_messages = [*await recent_messages(request.app, thread_id),
                      HumanMessage(content=user_message)]

    config = {"configurable": {"thread_id": thread_id}}
    try:
        async with _pool_conn(request.app) as conn:
            await touch_session_retry(request.app, thread_id)
        result = await agent.ainvoke(
            {"messages": input_messages},
            config=config
        )
        messages = result.get("messages", [])
        if not messages:
            return AgentResp(success=False, is_interrupt=False, content="Agent执行完成，但未返回消息")

        last_msg = messages[-1]
        content = last_msg.content if hasattr(last_msg, "content") else ""
        if specialist_faq:
            content = finalize_faq_reply(user_message, content, messages)
            content = mark_demo_faq_reply(tenant_id, content)
        if not req.has_pending_ticket:
            await _record_to_checkpoint(request.app, thread_id, user_message, content)
        agent_trace.record("delegate", "model_result",
                           role="knowledge" if specialist_faq else "intake",
                           status="answered")
        # Stateless specialists receive history snapshots; bill only new messages.
        usage_messages = messages[len(input_messages):]
        async with _pool_conn(request.app) as conn:
            await _record_usage_after_chat(request, conn, tenant_id, thread_id, usage_messages)
        return AgentResp(
            success=True,
            is_interrupt=False,
            content=content
        )
    except Exception as e:
        logger.exception("/agent/chat error")
        err_msg = _friendly_llm_error(e)
        return AgentResp(success=False, is_interrupt=False, content=err_msg)


@router.post("/agent/chat/stream")
async def agent_chat_stream(req: ChatRequest, request: Request):
    """P2-1：Agent 对话流式输出（SSE）
    使用 LangGraph astream(stream_mode="messages") 逐 token 推送 LLM 回复
    SSE 事件格式：
      data: {"type":"chunk","content":"token片段"}\n\n
      data: {"type":"end","content":"完整内容"}\n\n
      data: {"type":"error","msg":"错误信息"}\n\n
      data: {"type":"interrupt","data":中断数据}\n\n
    """
    import json as _json

    from fastapi.responses import StreamingResponse

    thread_id = req.thread_id.strip()
    user_message = req.user_message.strip()
    verified_tenant = _require_trusted_chat(request, thread_id)
    run = agent_trace.new_trace(request.headers.get("X-Trace-Id", ""),
                                verified_tenant, thread_id)
    trace_id_var.set(run.trace_id)

    async def event_generator():
        if not thread_id or not user_message:
            yield f"data: {_json.dumps({'type':'error','msg':'thread_id 和 user_message 不能为空'}, ensure_ascii=False)}\n\n"
            return

        tenant_id = verified_tenant
        tenant_var.set(tenant_id)
        thread_var.set(thread_id)
        pending_ticket_var.set(req.has_pending_ticket)
        decision = await decide_turn(request.app, tenant_id, thread_id, user_message,
                                     pending_ticket=req.has_pending_ticket)
        logger.info("support stream specialist=%s source=%s tenant=%s",
                    decision.specialist, decision.source, tenant_id)
        try:
            shortcut = await execute_shortcut(request.app, tenant_id, thread_id,
                                              user_message, decision,
                                              pending_ticket=req.has_pending_ticket)
            if shortcut is not None:
                if shortcut.model_messages:
                    async with _pool_conn(request.app) as conn:
                        await _record_usage_after_chat(request, conn, tenant_id, thread_id,
                                                       shortcut.model_messages)
                payload = ({"type": "interrupt", "data": shortcut.interrupt_data}
                           if shortcut.interrupt_data else
                           {"type": "end", "content": shortcut.content + SERVICE_FOOTER})
                yield f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n"
                return
        except Exception as e:
            logger.exception("/agent/chat/stream specialist error")
            yield f"data: {_json.dumps({'type':'error','msg':_friendly_llm_error(e)}, ensure_ascii=False)}\n\n"
            return
        route = decision.route

        # P1-2：并发活跃会话配额校验
        async with _pool_conn(request.app) as conn:
            quota_err = await check_concurrent_sessions(conn, request.app, tenant_id)
        if quota_err:
            yield f"data: {_json.dumps({'type':'error','msg':quota_err['msg']}, ensure_ascii=False)}\n\n"
            return

        try:
            specialist_faq = route.specialist == "faq"
            agent = (await get_faq_specialist(request.app, tenant_id) if specialist_faq
                     else await get_intake_specialist(request.app, tenant_id))
            input_messages = [*await recent_messages(request.app, thread_id),
                              HumanMessage(content=user_message)]
            config = {"configurable": {"thread_id": thread_id}}
            async with _pool_conn(request.app) as conn:
                await touch_session_retry(request.app, thread_id)

            full_content = ""
            demo_prefix_sent = False
            stream_msgs: list = []

            # 使用 LangGraph astream(stream_mode="messages") 逐 token 输出
            async for msg, metadata in agent.astream(
                {"messages": input_messages},
                config=config,
                stream_mode="messages"
            ):
                stream_msgs.append(msg)
                # [ours] 修正：原条件只排除 HumanMessage，工具结果会混入回复；改为仅推送 AIMessage
                # 只推送 AIMessage 的内容（过滤 HumanMessage / ToolMessage / 系统消息）。
                # ToolMessage 是 search_kb 等工具的返回结果，原始文档文本不应混进用户看到的回复。
                if hasattr(msg, "content") and msg.content and isinstance(msg, AIMessage):
                    chunk = msg.content if isinstance(msg.content, str) else str(msg.content)
                    if chunk:
                        if specialist_faq and not demo_prefix_sent:
                            prefix = demo_faq_prefix(tenant_id)
                            if prefix:
                                full_content += prefix
                            demo_prefix_sent = True
                        full_content += chunk
                        if not specialist_faq:
                            yield f"data: {_json.dumps({'type':'chunk','content':chunk}, ensure_ascii=False)}\n\n"

            if specialist_faq:
                full_content = finalize_faq_reply(user_message, full_content, stream_msgs)
                full_content = mark_demo_faq_reply(tenant_id, full_content)
                yield f"data: {_json.dumps({'type':'chunk','content':full_content}, ensure_ascii=False)}\n\n"
            if not req.has_pending_ticket:
                await _record_to_checkpoint(request.app, thread_id, user_message, full_content)
            agent_trace.record("delegate", "model_result",
                               role="knowledge" if specialist_faq else "intake",
                               status="answered")
            # Persist usage before the terminal frame: the business client
            # stops reading as soon as it receives `end`.
            async with _pool_conn(request.app) as conn:
                await _record_usage_after_chat(request, conn, tenant_id, thread_id, stream_msgs)
            yield f"data: {_json.dumps({'type':'end','content':full_content}, ensure_ascii=False)}\n\n"

        except Exception as e:
            logger.exception("/agent/chat/stream error")
            err_msg = _friendly_llm_error(e)
            yield f"data: {_json.dumps({'type':'error','msg':err_msg}, ensure_ascii=False)}\n\n"

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


@router.post("/agent/resume", response_model=AgentResp)
async def agent_resume(req: ResumeRequest, request: Request):
    trace_id_var.set(request.headers.get("X-Trace-Id", "-"))
    thread_id = req.thread_id.strip()
    tenant_id = _require_trusted_chat(request, thread_id)  # [ours] 原为 tenant_from_thread
    tenant_var.set(tenant_id)
    thread_var.set(thread_id)

    # P1-2：并发活跃会话配额校验
    async with _pool_conn(request.app) as conn:
        quota_err = await check_concurrent_sessions(conn, request.app, tenant_id)
    if quota_err:
        return AgentResp(success=False, is_interrupt=False, content=quota_err["msg"])

    agent = await get_tenant_agent(request.app, tenant_id)

    config = {"configurable": {"thread_id": thread_id}}
    try:
        async with _pool_conn(request.app) as conn:
            await touch_session_retry(request.app, thread_id)
        result = await agent.ainvoke(
            Command(resume={
                "confirmed": req.confirmed,
                "wait_time": req.wait_time,
                "ticket_id": req.ticket_id,
            }),
            config=config
        )
        messages = result.get("messages", [])
        if not messages:
            return AgentResp(success=False, is_interrupt=False, content="resume执行完成无返回消息")
        last_msg = messages[-1]
        content = last_msg.content if hasattr(last_msg, "content") else ""
        async with _pool_conn(request.app) as conn:
            await _record_usage_after_chat(request, conn, tenant_id, thread_id, messages)
        return AgentResp(
            success=True,
            is_interrupt=False,
            content=content
        )
    except Exception as e:
        logger.exception("/agent/resume error")
        err_msg = "resume服务内部异常" if PROD_MODE else f"resume error:{str(e)}"
        return AgentResp(success=False, is_interrupt=False, content=err_msg)


# 调试接口，查询checkpoint数据表（异步版本）
@router.get("/agent/checkpoint/{thread_id}")
async def get_checkpoint(thread_id: str, request: Request):
    _require_trusted_chat(request, thread_id)
    async with _pool_conn(request.app) as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, metadata
                FROM checkpoints
                WHERE thread_id = %s AND checkpoint_ns = '';
            """, (thread_id,))
            rows = await cur.fetchall()
            result = []
            for row in rows:
                result.append({
                    "thread_id": row[0],
                    "checkpoint_ns": row[1],
                    "checkpoint_id": row[2],
                    "parent_checkpoint_id": row[3],
                    "metadata": row[4]
                })
        return {"data": result}
