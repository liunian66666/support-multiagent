# 中文文案必然包含全角标点
"""State channels of the four-agent support graph.

Every value that a turn produces — the user message, each specialist finding, the
evidence IDs behind it, and the final answer — is a channel of this one state, so
the parent checkpoint is the only durable store. Nothing is written from outside
the graph.

Official primitives: ``langgraph.graph.MessagesState`` supplies the ``messages``
reducer; the remaining channels declare their own reducers, which is the documented
way to make an update idempotent (verified locally against langgraph 1.2.11:
``add_messages`` replaces a message that carries an existing id, and a keyed upsert
reducer keeps a replayed turn from duplicating records).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, NotRequired, TypedDict

from langgraph.graph import MessagesState

MAX_DELEGATIONS = 40
"""Per-thread bound; older turns stay auditable through the compact ``ledger``."""


class OrderFact(TypedDict):
    """Read-only result of a business-enforced ownership check.

    ``code`` is decided by the Business service, never by a prompt: ``ok`` /
    ``unauthenticated`` / ``forbidden`` / ``not_found`` / ``unavailable`` /
    ``unverified``.
    """

    code: str
    text: str
    order_id: NotRequired[str]


class Evidence(TypedDict):
    id: str
    title: str
    text: str


class Delegation(TypedDict):
    """One specialist outcome, merged into the parent turn by the supervisor node."""

    turn_id: str
    specialist: str  # faq / order / general
    answer: str
    tool_calls: list[str]
    evidence: list[Evidence]
    verified_results: list[OrderFact]
    used_search: bool
    failed: bool


class TurnRecord(TypedDict):
    """Durable, bounded audit trail: what ran, on what evidence, with what result."""

    turn_id: str
    question: str
    answer: str
    outcome: str
    roles: list[str]
    evidence_ids: list[str]
    review_issues: list[str]
    attempts: int


def _upsert(records: list[dict[str, Any]], incoming: list[dict[str, Any]],
            key: Callable[[dict[str, Any]], str], limit: int | None = None) -> list[dict[str, Any]]:
    """Replace same-key records instead of appending; keeps first-seen order, trims oldest."""
    merged: dict[str, dict[str, Any]] = {key(item): item for item in records}
    for item in incoming:
        merged[key(item)] = item
    values = list(merged.values())
    return values[-limit:] if limit else values


def merge_delegations(left: list[Delegation], right: list[Delegation]) -> list[Delegation]:
    """Idempotent per (turn, specialist): a replayed turn rewrites its own finding."""
    return _upsert(left, right, lambda item: f"{item['turn_id']}|{item['specialist']}",
                   MAX_DELEGATIONS)


def merge_ledger(left: list[TurnRecord], right: list[TurnRecord]) -> list[TurnRecord]:
    return _upsert(left, right, lambda item: item["turn_id"])


def merge_usage(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Keyed by turn_id so a client retry rewrites its own usage, never adds twice."""
    return {**left, **right}


def merge_issues(left: list[str], right: list[str]) -> list[str]:
    """Append review findings in order, dropping repeats.

    Accumulated on purpose: a turn that needed a retry has to stay visible in the
    ledger, otherwise the comparison report cannot tell "answered first try" from
    "answered after the Reviewer pushed back".
    """
    merged = list(dict.fromkeys([*left, *right]))
    return merged[-8:]


class SupportState(MessagesState):
    """Only this parent graph is checkpointed; specialists receive a message snapshot."""

    summary: str
    """Rolling summary of everything at or before ``summary_watermark``."""

    summary_watermark: int
    """Count of ``messages`` already absorbed into ``summary``. Monotonic, never
    advances past a message that is dropped: the transcript itself is append-only."""

    turn_id: str
    draft: str
    answer: str
    last_error: str
    review_feedback: str
    review_issues: Annotated[list[str], merge_issues]
    review_hard: bool
    attempts: int
    outcome: str
    delegations: Annotated[list[Delegation], merge_delegations]
    ledger: Annotated[list[TurnRecord], merge_ledger]
    usage: Annotated[dict[str, Any], merge_usage]
