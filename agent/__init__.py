# 中文文案必然包含全角标点
"""Four-agent LangGraph customer service, isolated from the legacy agent package.

Public surface: build the graph, run a turn, and the state contracts.
Nothing in ``python_agent`` imports this package; the legacy path stays intact until
the comparison phase is accepted.
"""

from .deps import SupportDeps
from .graph import DEFAULT_MAX_PASSES, build_support_graph
from .memory import SUMMARY_BANNER, compact, model_context
from .service import SupportService, TurnResult, enabled, sse_events, thread_key
from .state import Delegation, Evidence, OrderFact, SupportState, TurnRecord

__all__ = [
    "DEFAULT_MAX_PASSES",
    "SUMMARY_BANNER",
    "Delegation",
    "Evidence",
    "OrderFact",
    "SupportDeps",
    "SupportService",
    "SupportState",
    "TurnRecord",
    "TurnResult",
    "build_support_graph",
    "compact",
    "enabled",
    "model_context",
    "sse_events",
    "thread_key",
]
