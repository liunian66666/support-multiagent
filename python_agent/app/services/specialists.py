"""Bounded specialist execution; business writes remain in FastAPI."""

from dataclasses import dataclass

from app.agent import tools
from app.chat.order import order_handoff_reason, should_auto_handoff
from app.services import trace
from app.services.compound import execute_compound
from app.services.dialog import _record_to_checkpoint


@dataclass(frozen=True)
class SpecialistResult:
    role: str
    content: str = ""
    interrupt_data: dict | None = None
    status: str = "answered"
    code: str = ""
    model_messages: tuple = ()


async def execute_shortcut(app, tenant_id: str, thread_id: str, message: str,
                           decision, *, pending_ticket: bool = False) -> SpecialistResult | None:
    """Execute non-generative specialists and a bounded read-only compound plan.

    Returns None only when the Knowledge or General model specialist must run.
    This function never creates a ticket or grants an order identity.
    """
    route = decision.route
    trace.record("delegate", "specialist", role=decision.specialist)
    if decision.plan:
        result = await execute_compound(app, tenant_id, thread_id,
                                        decision.question, decision.plan,
                                        has_pending_ticket=pending_ticket)
        if result.interrupt_data:
            trace.record("delegate", "composer_result", status="handoff")
            return SpecialistResult("composer", interrupt_data=result.interrupt_data,
                                    status="handoff")
        if not pending_ticket:
            await _record_to_checkpoint(app, thread_id, message, result.content)
        trace.record("delegate", "composer_result", status="answered")
        return SpecialistResult("composer", content=result.content,
                                model_messages=result.model_messages)
    if route.specialist == "handoff":
        reason = route.reason or "用户请求转人工"
        await _record_to_checkpoint(app, thread_id, message,
                                    f"已为您转接人工客服（原因：{reason}），请稍候。")
        trace.record("delegate", "case_request", status="handoff")
        return SpecialistResult("case", interrupt_data={
            "reason": reason, "direct_handoff": True,
        }, status="handoff")
    if route.specialist in {"order", "order_list"}:
        oid = route.order_id or ""
        result = (await tools.lookup_order(oid) if route.specialist == "order"
                  else await tools.list_owned_orders())
        trace.record("tool", "order_read", code=result.code,
                     mode="lookup" if oid else "list")
        if not pending_ticket:
            await _record_to_checkpoint(app, thread_id, message, result.text)
            if should_auto_handoff(result.code):
                data = {"reason": order_handoff_reason(result.code,
                         oid or "订单列表"), "direct_handoff": True,
                        "code": result.code}
                if oid:
                    data["order_id"] = oid
                return SpecialistResult("order", interrupt_data=data,
                                        status="handoff", code=result.code)
        content = result.text
        if pending_ticket and should_auto_handoff(result.code):
            content += "您已有待处理工单，客服接入后会继续核实。"
        return SpecialistResult("order", content=content, code=result.code)
    if route.specialist == "faq_fast":
        answer = route.answer or ""
        if not pending_ticket:
            await _record_to_checkpoint(app, thread_id, message, answer)
        trace.record("delegate", "faq_rule", status="answered")
        return SpecialistResult("faq_rule", content=answer)
    return None
