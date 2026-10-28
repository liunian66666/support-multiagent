# 中文文案必然包含全角标点
"""Injected capabilities at the trusted boundaries of the graph.

The graph itself never talks to Business, Qdrant or a model SDK directly: it calls
these hooks. That keeps the parent graph testable with fakes and keeps the security
property the project depends on — order ownership is decided by the Business
service inside ``lookup_order`` / ``list_orders``, so no prompt can widen access.

``evidence_cursor`` / ``evidence_ids_since`` let the FAQ delegate report which
knowledge-base chunks actually backed its answer, which is what makes the Reviewer's
"no evidence, no policy number" rule checkable instead of rhetorical.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .state import Evidence, OrderFact

ModelProvider = Callable[[], Awaitable[Any]]
AgentProvider = Callable[[], Awaitable[Any]]
OrderReader = Callable[[str], Awaitable[OrderFact]]
OrderLister = Callable[[], Awaitable[OrderFact]]
TraceSink = Callable[..., None]
Cursor = Callable[[], int]
EvidenceSince = Callable[[int], "list[str | Evidence]"]


def _noop_trace(*_args: Any, **_kwargs: Any) -> None:
    return None


@dataclass(frozen=True)
class SupportDeps:
    """Capabilities the graph needs. Implementations must already hold a verified
    tenant/customer session; a browser-supplied customer id is never accepted.

    A thread's pre-existing conversation is not a dependency: the service passes it
    into the graph once as input, so the checkpoint stays the only writer.
    """

    model: ModelProvider
    """Chat model for the supervisor, the order delegate and summarisation."""

    faq_agent: AgentProvider
    """Existing FAQ agent (tenant knowledge-base tool only, no write tools)."""

    general_agent: AgentProvider
    """Existing tool-free intake agent (greetings, capability, clarification)."""

    lookup_order: OrderReader
    list_orders: OrderLister

    evidence_cursor: Cursor = field(default=lambda: 0)
    evidence_ids_since: EvidenceSince = field(default=lambda _cursor: [])
    trace: TraceSink = field(default=_noop_trace)
