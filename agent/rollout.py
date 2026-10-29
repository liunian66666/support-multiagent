"""Choose the four-agent graph for every ordinary chat turn.

Unfinished approvals retain their original checkpoint until resolved. They are the
only reason to run the historical graph.
"""

from __future__ import annotations


def graph_enabled(*, pending_ticket: bool = False) -> bool:
    """Return whether this turn belongs to the current four-agent graph."""
    return not pending_ticket
