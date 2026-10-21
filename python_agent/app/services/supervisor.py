"""One routing decision for both JSON and streaming customer chat."""

from dataclasses import dataclass

from langchain_core.messages import HumanMessage

from app.agent.faq_agent import recent_messages
from app.chat.faq import has_order_reference
from app.chat.intent import Route, route_message
from app.chat.order import (_awaiting_order_id, _last_order_id_from_history,
                            looks_like_bare_order_id)
from app.chat.plan import TaskPlan, plan_compound
from app.services import trace


@dataclass(frozen=True)
class TurnDecision:
    route: Route
    plan: TaskPlan | None
    question: str
    source: str

    @property
    def specialist(self) -> str:
        if self.plan:
            return "composer"
        return "case" if self.route.specialist == "handoff" else self.route.specialist


async def _compound_plan(app, thread_id: str, message: str, route: Route):
    """Resume an unfinished compound question only within the same thread."""
    if route.specialist == "handoff":
        return None, message
    if looks_like_bare_order_id(message) and await _awaiting_order_id(app, thread_id):
        for previous in reversed(await recent_messages(app, thread_id)):
            if isinstance(previous, HumanMessage):
                old = plan_compound(str(previous.content))
                if old and old.needs_order_id:
                    return (plan_compound(message, resumed_order_id=message,
                                          resumed_question=str(previous.content)),
                            str(previous.content))
                break
    plan = plan_compound(message)
    if plan and plan.needs_order_id and route.order_id:
        plan = plan_compound(message, resumed_order_id=route.order_id)
    return plan, message


async def decide_turn(app, tenant_id: str, thread_id: str, message: str,
                      *, pending_ticket: bool = False) -> TurnDecision:
    """Plan once using only the verified tenant and this thread's history."""
    route = route_message(message, tenant_id, pending_ticket=pending_ticket)
    source = "rule"
    if route.specialist not in {"handoff", "order", "order_list"} and has_order_reference(message):
        previous_order = await _last_order_id_from_history(app, thread_id)
        if previous_order:
            route = Route("order", order_id=previous_order)
            source = "history"
    if (route.specialist in {"general", "faq"}
            and looks_like_bare_order_id(message)
            and await _awaiting_order_id(app, thread_id)):
        route = Route("order", order_id=message.strip())
        source = "history"
    plan, question = await _compound_plan(app, thread_id, message, route)
    if plan:
        source = "compound_plan"
    decision = TurnDecision(route=route, plan=plan, question=question, source=source)
    run = trace.current()
    if run is not None:
        run.route = route.specialist
        run.specialist = decision.specialist
    trace.record("route", "decision", route=route.specialist,
                 specialist=decision.specialist, source=source,
                 tasks=[task.kind for task in plan.tasks] if plan else [])
    return decision
