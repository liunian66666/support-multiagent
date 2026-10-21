"""对话端点：/agent/chat、/agent/chat/stream、/agent/resume、/agent/checkpoint/{thread_id}。"""
import logging

from fastapi import APIRouter, Request
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from app.agent import tools
from app.agent.builder import get_tenant_agent
from app.agent.faq_agent import faq_agent as get_faq_specialist
from app.agent.faq_agent import recent_messages
from app.api.deps import _require_trusted_chat
from app.chat.faq import has_order_reference
from app.chat.intent import Route, route_message
from app.chat.order import (
    _awaiting_order_id,
    _last_order_id_from_history,
    looks_like_bare_order_id,
    order_handoff_reason,
    should_auto_handoff,
)
from app.chat.plan import plan_compound
from app.chat.reply import _friendly_llm_error
from app.infra.config import PROD_MODE, SERVICE_FOOTER
from app.infra.context import pending_ticket_var, tenant_var, thread_var, trace_id_var
from app.infra.db.quota import check_concurrent_sessions
from app.infra.db.session import touch_session_retry
from app.infra.db.tenant import tenant_from_thread
from app.infra.db_pool import _pool_conn
from app.schemas import AgentResp, ChatRequest, ResumeRequest
from app.services.compound import execute_compound
from app.services.dialog import _record_to_checkpoint, _record_usage_after_chat

logger = logging.getLogger("agent")
router = APIRouter()


async def _compound_plan(app, thread_id: str, message: str, route: Route):
    """Resume an unfinished compound question only within the same trusted thread."""
    if route.specialist == "handoff":
        return None, message
    if looks_like_bare_order_id(message) and await _awaiting_order_id(app, thread_id):
        for previous in reversed(await recent_messages(app, thread_id)):
            if isinstance(previous, HumanMessage):
                old = plan_compound(str(previous.content))
                if old and old.needs_order_id:
                    return plan_compound(message, resumed_order_id=message,
                                         resumed_question=str(previous.content)), str(previous.content)
                break
    plan = plan_compound(message)
    if plan and plan.needs_order_id and route.order_id:
        plan = plan_compound(message, resumed_order_id=route.order_id)
    return plan, message


@router.post("/agent/chat", response_model=AgentResp)
async def agent_chat(req: ChatRequest, request: Request):
    trace_id_var.set(request.headers.get("X-Trace-Id", "-"))
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
    route = route_message(user_message, tenant_id, pending_ticket=req.has_pending_ticket)
    if route.specialist not in {"handoff", "order", "order_list"} and has_order_reference(user_message):
        previous_order = await _last_order_id_from_history(request.app, thread_id)
        if previous_order:
            route = Route("order", order_id=previous_order)
    # [ours] 上一轮已在索要订单号时，允许用户直接回裸单号（「请提供订单号」→「10110」）
    if (route.specialist in {"general", "faq"}
            and looks_like_bare_order_id(user_message)
            and await _awaiting_order_id(request.app, thread_id)):
        route = Route("order", order_id=user_message.strip())
    logger.info("support specialist=%s tenant=%s", route.specialist, tenant_id)
    plan, full_question = await _compound_plan(request.app, thread_id, user_message, route)
    if plan:
        try:
            result = await execute_compound(request.app, tenant_id, thread_id,
                                            full_question, plan,
                                            has_pending_ticket=req.has_pending_ticket)
            if result.interrupt_data:
                return AgentResp(success=True, is_interrupt=True,
                                 interrupt_data=result.interrupt_data)
            if not req.has_pending_ticket:
                await _record_to_checkpoint(request.app, thread_id, user_message, result.content)
            if result.model_messages:
                async with _pool_conn(request.app) as conn:
                    await _record_usage_after_chat(request, conn, tenant_id, thread_id,
                                                   result.model_messages)
            return AgentResp(success=True, is_interrupt=False,
                             content=result.content + SERVICE_FOOTER)
        except Exception as e:
            logger.exception("/agent/chat compound error")
            return AgentResp(success=False, is_interrupt=False,
                             content=_friendly_llm_error(e))
    if route.specialist == "handoff":
        reason = route.reason or "用户请求转人工"
        await _record_to_checkpoint(
            request.app, thread_id, user_message,
            f"已为您转接人工客服（原因：{reason}），请稍候。",
        )
        return AgentResp(success=True, is_interrupt=True, interrupt_data={
            "reason": reason, "direct_handoff": True,
        })
    if route.specialist == "order_list":
        result = await tools.list_owned_orders()
        if not req.has_pending_ticket:
            await _record_to_checkpoint(request.app, thread_id, user_message, result.text)
            if should_auto_handoff(result.code):
                return AgentResp(success=True, is_interrupt=True, interrupt_data={
                    "reason": order_handoff_reason(result.code, "订单列表"),
                    "direct_handoff": True, "code": result.code,
                })
        return AgentResp(success=True, is_interrupt=False,
                         content=result.text + SERVICE_FOOTER)
    if route.specialist == "order":
        oid = route.order_id
        result = await tools.lookup_order(oid)
        if not req.has_pending_ticket:
            await _record_to_checkpoint(request.app, thread_id, user_message, result.text)
            # 只对用户无法自助纠正的失败自动转人工（归属不符/系统不可用/身份上下文缺失）；
            # 单号不存在、未登录留在自助链路，由用户自己纠正，避免一字打错就静默建单。
            if should_auto_handoff(result.code):
                reason = order_handoff_reason(result.code, oid)
                return AgentResp(success=True, is_interrupt=True, interrupt_data={
                    "reason": reason, "direct_handoff": True,
                    "order_id": oid, "code": result.code,
                })
            return AgentResp(success=True, is_interrupt=False, content=result.text + SERVICE_FOOTER)
        content = result.text
        if should_auto_handoff(result.code):
            content += "您已有待处理工单，客服接入后会继续核实。"
        return AgentResp(success=True, is_interrupt=False, content=content + SERVICE_FOOTER)
    if route.specialist == "faq_fast":
        answer = route.answer
        if not req.has_pending_ticket:
            await _record_to_checkpoint(request.app, thread_id, user_message, answer)
        return AgentResp(success=True, is_interrupt=False, content=answer + SERVICE_FOOTER)
    specialist_faq = route.specialist == "faq"
    agent = (await get_faq_specialist(request.app, tenant_id) if specialist_faq
             else await get_tenant_agent(request.app, tenant_id))
    input_messages = ([*await recent_messages(request.app, thread_id), HumanMessage(content=user_message)]
                      if specialist_faq else [HumanMessage(content=user_message)])

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

        state = None if specialist_faq else await agent.aget_state(config)
        if state and state.tasks and state.tasks[0].interrupts:
            interrupt_info = state.tasks[0].interrupts[0].value
            return AgentResp(
                success=True,
                is_interrupt=True,
                interrupt_data=interrupt_info,
                content=None
            )
        last_msg = messages[-1]
        content = last_msg.content if hasattr(last_msg, "content") else ""
        if specialist_faq and not req.has_pending_ticket:
            await _record_to_checkpoint(request.app, thread_id, user_message, content)
        # The FAQ specialist receives a snapshot of earlier messages; bill only
        # messages produced by this turn, never the copied historical context.
        usage_messages = messages[len(input_messages):] if specialist_faq else messages
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

    trace_id_var.set(request.headers.get("X-Trace-Id", "-"))
    thread_id = req.thread_id.strip()
    user_message = req.user_message.strip()
    _require_trusted_chat(request, thread_id)  # [ours] 新增：流式入口同样校验可信来源

    async def event_generator():
        if not thread_id or not user_message:
            yield f"data: {_json.dumps({'type':'error','msg':'thread_id 和 user_message 不能为空'}, ensure_ascii=False)}\n\n"
            return

        tenant_id = tenant_from_thread(thread_id)
        tenant_var.set(tenant_id)
        thread_var.set(thread_id)
        pending_ticket_var.set(req.has_pending_ticket)
        route = route_message(user_message, tenant_id, pending_ticket=req.has_pending_ticket)
        if route.specialist not in {"handoff", "order", "order_list"} and has_order_reference(user_message):
            previous_order = await _last_order_id_from_history(request.app, thread_id)
            if previous_order:
                route = Route("order", order_id=previous_order)
        # [ours] 同 /agent/chat：索要订单号后允许直接回裸单号
        if (route.specialist in {"general", "faq"}
                and looks_like_bare_order_id(user_message)
                and await _awaiting_order_id(request.app, thread_id)):
            route = Route("order", order_id=user_message.strip())
        logger.info("support stream specialist=%s tenant=%s", route.specialist, tenant_id)
        plan, full_question = await _compound_plan(request.app, thread_id, user_message, route)
        if plan:
            try:
                result = await execute_compound(request.app, tenant_id, thread_id,
                                                full_question, plan,
                                                has_pending_ticket=req.has_pending_ticket)
                if result.interrupt_data:
                    payload = {"type": "interrupt", "data": result.interrupt_data}
                    yield f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n"
                    return
                if not req.has_pending_ticket:
                    await _record_to_checkpoint(request.app, thread_id, user_message, result.content)
                if result.model_messages:
                    async with _pool_conn(request.app) as conn:
                        await _record_usage_after_chat(request, conn, tenant_id, thread_id,
                                                       result.model_messages)
                payload = {"type": "end", "content": result.content + SERVICE_FOOTER}
                yield f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n"
            except Exception as e:
                logger.exception("/agent/chat/stream compound error")
                yield f"data: {_json.dumps({'type':'error','msg':_friendly_llm_error(e)}, ensure_ascii=False)}\n\n"
            return
        if route.specialist == "handoff":
            reason = route.reason or "用户请求转人工"
            info = {"reason": reason, "direct_handoff": True}
            await _record_to_checkpoint(
                request.app, thread_id, user_message,
                f"已为您转接人工客服（原因：{reason}），请稍候。",
            )
            yield f"data: {_json.dumps({'type':'interrupt','data':info}, ensure_ascii=False)}\n\n"
            return
        if route.specialist == "order_list":
            result = await tools.list_owned_orders()
            if not req.has_pending_ticket:
                await _record_to_checkpoint(request.app, thread_id, user_message, result.text)
                if should_auto_handoff(result.code):
                    info = {"reason": order_handoff_reason(result.code, "订单列表"),
                            "direct_handoff": True, "code": result.code}
                    yield f"data: {_json.dumps({'type':'interrupt','data':info}, ensure_ascii=False)}\n\n"
                    return
            yield f"data: {_json.dumps({'type':'end','content':result.text + SERVICE_FOOTER}, ensure_ascii=False)}\n\n"
            return
        if route.specialist == "order":
            oid = route.order_id
            result = await tools.lookup_order(oid)
            if not req.has_pending_ticket:
                await _record_to_checkpoint(request.app, thread_id, user_message, result.text)
                # 同上：结构化 code 判定，只有归属不符/系统不可用/身份缺失才自动转人工
                if should_auto_handoff(result.code):
                    reason = order_handoff_reason(result.code, oid)
                    info = {"reason": reason, "direct_handoff": True,
                            "order_id": oid, "code": result.code}
                    yield f"data: {_json.dumps({'type':'interrupt','data':info}, ensure_ascii=False)}\n\n"
                    return
                yield f"data: {_json.dumps({'type':'end','content':result.text + SERVICE_FOOTER}, ensure_ascii=False)}\n\n"
                return
            content = result.text
            if should_auto_handoff(result.code):
                content += "您已有待处理工单，客服接入后会继续核实。"
            yield f"data: {_json.dumps({'type':'end','content':content + SERVICE_FOOTER}, ensure_ascii=False)}\n\n"
            return
        if route.specialist == "faq_fast":
            answer = route.answer
            if not req.has_pending_ticket:
                await _record_to_checkpoint(request.app, thread_id, user_message, answer)
            yield f"data: {_json.dumps({'type':'end','content':answer + SERVICE_FOOTER}, ensure_ascii=False)}\n\n"
            return

        # P1-2：并发活跃会话配额校验
        async with _pool_conn(request.app) as conn:
            quota_err = await check_concurrent_sessions(conn, request.app, tenant_id)
        if quota_err:
            yield f"data: {_json.dumps({'type':'error','msg':quota_err['msg']}, ensure_ascii=False)}\n\n"
            return

        try:
            specialist_faq = route.specialist == "faq"
            agent = (await get_faq_specialist(request.app, tenant_id) if specialist_faq
                     else await get_tenant_agent(request.app, tenant_id))
            input_messages = ([*await recent_messages(request.app, thread_id), HumanMessage(content=user_message)]
                              if specialist_faq else [HumanMessage(content=user_message)])
            config = {"configurable": {"thread_id": thread_id}}
            async with _pool_conn(request.app) as conn:
                await touch_session_retry(request.app, thread_id)

            full_content = ""
            is_interrupt = False
            interrupt_data = None
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
                        full_content += chunk
                        yield f"data: {_json.dumps({'type':'chunk','content':chunk}, ensure_ascii=False)}\n\n"

            # 检查是否有中断（人工接管）
            try:
                state = None if specialist_faq else await agent.aget_state(config)
                if state and state.tasks and state.tasks[0].interrupts:
                    is_interrupt = True
                    interrupt_data = state.tasks[0].interrupts[0].value
            except Exception:
                pass

            if is_interrupt:
                yield f"data: {_json.dumps({'type':'interrupt','data':interrupt_data}, ensure_ascii=False)}\n\n"
            else:
                if specialist_faq and not req.has_pending_ticket:
                    await _record_to_checkpoint(request.app, thread_id, user_message, full_content)
                yield f"data: {_json.dumps({'type':'end','content':full_content}, ensure_ascii=False)}\n\n"
            # P1-1：记录 token 用量（从流式消息中提取 usage_metadata）
            async with _pool_conn(request.app) as conn:
                await _record_usage_after_chat(request, conn, tenant_id, thread_id, stream_msgs)

        except Exception as e:
            logger.exception("/agent/chat/stream error")
            err_msg = _friendly_llm_error(e)
            yield f"data: {_json.dumps({'type':'error','msg':err_msg}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


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
