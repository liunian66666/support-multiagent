"""Application adapter for the authoritative four-agent graph in ``agent/``.

Ordinary chat always uses this graph. An unfinished approval stays on its original
checkpoint until it is resolved.
"""

from __future__ import annotations

from agent.cutover import clear_services, run_turn as graph_run_turn
from agent.rollout import graph_enabled


def enabled(*, pending_ticket: bool = False) -> bool:
    """Only an unfinished approval uses the historical graph."""
    return graph_enabled(pending_ticket=pending_ticket)


async def run_turn(app, tenant_id: str, thread_id: str, user_message: str,
                   turn_id: str = "") -> dict:
    return await graph_run_turn(app, tenant_id, thread_id, user_message, turn_id)


def clear_cached(tenant_id: str | None = None) -> None:
    clear_services(tenant_id)
