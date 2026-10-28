# ruff: noqa: RUF001
"""Drop-in replacement for ``app.services.four_agent``, same two functions.

``python_agent/app/api/chat.py`` already runs the old and new paths in parallel: both
the JSON branch and the SSE branch call ``four_agent.enabled(...)`` and then
``four_agent.run_turn(app, tenant_id, thread_id, user_message)``, and read exactly two
fields out of the result — ``state["answer"]`` and ``state.get("model_messages", [])``.
So switching the default entry over is one import alias, not a rewrite:

    - from app.services import four_agent
    + from agent import cutover as four_agent

See ``agent/cutover.patch`` for that diff, prepared but deliberately **not applied**.

This module keeps the legacy contract in full, including the parts chat.py does not
read but the console does: the same ``FOUR_AGENT_GRAPH`` env flag, the same
``support_v2:`` checkpoint namespace, the per-thread serialising lock, the one-time
legacy-history import on a thread's first turn, and the ``trace`` route/specialist
attribution. Dropping any of those would silently change behaviour the acceptance
criteria depend on (HITL threads staying put, trace showing which agents ran).
"""

from __future__ import annotations

from typing import Any

from .legacy_bindings import deps_for_app, legacy_history
from .service import SupportService, thread_key

# The rollout flag chat.py already uses. Keeping the name means operators do not have to
# learn a second switch, and a rollback is the same env change that always was.
ENV_FLAG = "FOUR_AGENT_GRAPH"

_services: dict[str, SupportService] = {}


def enabled(*, pending_ticket: bool = False) -> bool:
    """Same rule as the legacy adapter: an unfinished approval stays on its old graph."""
    import os

    return (not pending_ticket
            and os.getenv(ENV_FLAG, "off").strip().lower() in {"on", "true", "1"})


async def _service_for(app, tenant_id: str) -> SupportService:
    """One cached service per tenant, bound to the live app state and checkpointer.

    ``deps_for_app(..., reuse_faq_agent=True)`` keeps the production FAQ agent, so the
    knowledge-base path, its in-process cache and its ``kb_hits`` tracing behave exactly
    as they do on the old graph — the new graph changes orchestration, not retrieval.
    """
    service = _services.get(tenant_id)
    if service is not None:
        return service

    async def deps_for_tenant(target: str) -> Any:
        return await deps_for_app(app, target, reuse_faq_agent=True)

    service = SupportService(deps_for_tenant, checkpointer=getattr(app.state, "checkpointer", None))
    _services[tenant_id] = service
    return service


async def run_turn(app, tenant_id: str, thread_id: str, user_message: str) -> dict[str, Any]:
    """Run one turn and return the final, reviewed state in the legacy dict shape."""
    # Identity first, service second. Building a service is what later builds the graph,
    # and a graph build reaches the model and the knowledge base: an unverifiable thread
    # must fail closed before any of that happens (and before it is billed).
    thread_key(tenant_id, thread_id)
    service = await _service_for(app, tenant_id)

    history = await service.history(tenant_id, thread_id)
    imports = None if history else await legacy_history(app, thread_id)
    result = await service.run_turn(tenant_id, thread_id, user_message, import_history=imports)

    _attribute_trace(result)
    return {"answer": result.answer,
            "outcome": result.outcome,
            "model_messages": result.model_messages,
            "evidence_ids": result.evidence_ids,
            "review_issues": result.review_issues,
            "attempts": result.attempts,
            "delegations": [{"specialist": role} for role in result.roles]}


def _attribute_trace(result: Any) -> None:
    """Preserve the console's route/specialist columns, which chat.py does not render."""
    try:
        from app.services import trace
    except Exception:  # noqa: BLE001 - attribution is best-effort, never a turn failure
        return
    current = trace.current()
    if not current:
        return
    current.route = "multi_agent"
    current.specialist = ",".join(dict.fromkeys(result.roles))[:120]


async def memory_state(app, tenant_id: str, thread_id: str) -> dict[str, int]:
    """Watermark ledger for the ``/agent/checkpoint`` view, when a host wants it."""
    return (await (await _service_for(app, tenant_id)).memory_state(tenant_id, thread_id))
