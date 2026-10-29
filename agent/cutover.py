# ruff: noqa: RUF001
"""Production bridge from ``app.services.four_agent`` into the durable graph.

The app adapter calls this module for a reviewed turn. It preserves
the ``support_v2:`` checkpoint namespace, legacy history import, trace attribution,
and a stable turn ID used by both JSON and SSE replies.
"""

from __future__ import annotations

import os
from typing import Any

from .legacy_bindings import deps_for_app, legacy_history
from .rollout import graph_enabled
from .service import SupportService, thread_key

_services: dict[str, SupportService] = {}


def enabled(*, pending_ticket: bool = False) -> bool:
    """Use the new graph except while resolving an unfinished approval."""
    return graph_enabled(pending_ticket=pending_ticket)


async def _service_for(app, tenant_id: str) -> SupportService:
    """One cached service per tenant, bound to the live app state and checkpointer.

    ``deps_for_app(..., reuse_faq_agent=True)`` keeps the production FAQ agent, so the
    knowledge-base path, its in-process cache and its ``kb_hits`` tracing behave exactly
    as they do on the old graph — the new graph changes orchestration, not retrieval.
    """
    service = _services.get(tenant_id)
    if service is not None:
        return service

    model_name = os.getenv("FOUR_AGENT_MODEL_NAME", "").strip()

    async def deps_for_tenant(target: str) -> Any:
        return await deps_for_app(app, target, reuse_faq_agent=True,
                                  model_source="platform" if model_name else "tenant",
                                  model_name=model_name)

    from .entrypoint import _guard_from_env

    service = SupportService(deps_for_tenant,
                             checkpointer=getattr(app.state, "checkpointer", None),
                             thread_guard=_guard_from_env())
    _services[tenant_id] = service
    return service


def clear_services(tenant_id: str | None = None) -> None:
    """Drop model-bound graphs after a tenant or platform model configuration change."""
    if tenant_id is None:
        _services.clear()
    else:
        _services.pop(tenant_id, None)


async def run_turn(app, tenant_id: str, thread_id: str, user_message: str,
                   turn_id: str = "") -> dict[str, Any]:
    """Run one turn and return the final, reviewed state in the legacy dict shape."""
    # Identity first, service second. Building a service is what later builds the graph,
    # and a graph build reaches the model and the knowledge base: an unverifiable thread
    # must fail closed before any of that happens (and before it is billed).
    thread_key(tenant_id, thread_id)
    service = await _service_for(app, tenant_id)

    history = await service.history(tenant_id, thread_id)
    imports = None if history else await legacy_history(app, thread_id)
    result = await service.run_turn(tenant_id, thread_id, user_message,
                                    turn_id=turn_id or None, import_history=imports)

    _attribute_trace(result)
    from langchain_core.messages import AIMessage

    usage = result.usage
    model_name = os.getenv("FOUR_AGENT_MODEL_NAME", "").strip()
    billed = ([AIMessage(content="", response_metadata={
        "model_name": model_name, "model_source": "platform" if model_name else "",
        "model_override": bool(model_name),
    }, usage_metadata={
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "total_tokens": int(usage.get("total_tokens", 0)),
    })] if usage.get("total_tokens") else [])
    return {"answer": result.answer,
            "turn_id": result.turn_id,
            "outcome": result.outcome,
            "usage": usage,
            "model_messages": billed,
            "_replayed": result.replayed,
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
