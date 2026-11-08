# 中文文案必然包含全角标点
"""Four-agent LangGraph customer service: the orchestration layer of ``app.agent``.

Public surface: build the graph, run a turn, and the state contracts.

This package owns the graph, the four specialists, the Reviewer and the memory
watermark. It sits *above* the capability layer in the parent package
(``app.agent.tools`` / ``retrieval`` / ``llm_config`` / ``faq_agent`` /
``intake_agent``) and reaches it through :mod:`app.agent.support.legacy_bindings`,
the single seam between the two.

``app.services.four_agent`` is the production entry point: ordinary chat always runs
this graph, and only an unfinished approval stays on the historical checkpoint.
"""

from .deps import SupportDeps
from .graph import DEFAULT_MAX_PASSES, build_support_graph
from .memory import SUMMARY_BANNER, compact, model_context
from .service import SupportService, TurnResult, sse_events, thread_key
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
    "model_context",
    "sse_events",
    "thread_key",
]
