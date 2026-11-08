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


class RouteItem(TypedDict):
    """One dispatch decision: which specialist, and the sub-question it should answer.

    The router writes these; the fan-out edge reads them. Keeping the sub-question in
    state — instead of only inside a tool call — is what makes an explicit node path
    replayable: a retry can re-run one role without asking the model to plan again.
    """

    role: str  # faq / order / general
    question: str


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
    """Keyed by turn_id; the counters of one turn are **summed**.

    并行扇出之后必须累加而不是覆盖：三个专家节点在同一 super-step 各写一份 usage，
    覆盖式合并只留下最后完成的那一路，等于把另外两路的 token 记成没花。回放仍然幂等，
    因为 ``open_turn`` 会先清掉本轮的条目，累加从零开始。
    """
    merged = dict(left)
    for turn, values in right.items():
        if isinstance(values, dict) and isinstance(merged.get(turn), dict):
            merged[turn] = {key: int(merged[turn].get(key) or 0) + int(values.get(key) or 0)
                            for key in set(merged[turn]) | set(values)}
        else:
            merged[turn] = values
    return merged


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

    plan: list[RouteItem]
    """This pass's dispatch plan. Plain last-value channel: a retry replaces it, and
    ``open_turn`` clears it so a previous turn's plan can never steer this one."""

    missing_roles: list[str]
    """Roles the Reviewer says must be dispatched again: never called, or called without
    leaving usable evidence. Only these are re-run — a retry never repeats work that
    already produced a finding.

    只有「可补派」的评审结论才会填这里：没有 hard，或 hard 全部是「缺当轮证据」
    （``guards.refillable_hard``）。越权陈述、暴露内部编号、引用未核验订单号一律留空，
    那条路径由 ``should_end`` 直接收尾，不走补派。"""

    review_gaps: list[str]
    """Answer-shape findings (``guards.answer_gaps``). They are fixed by rewriting the
    draft in ``compose``, not by dispatching a specialist again."""

    redraft: bool
    """「材料已在手、只是草稿编了数值」这条窄路：让 ``compose`` 不看旧稿重写一次。

    Reviewer 置位，``compose`` 用掉后清零。重写时必须丢掉 ``【上一版答复】``：把含错
    数值的旧稿再喂回去，模型最常见的做法是照抄那个数、只换措辞。
    """

    redraft_used: bool
    """这条窄重写每轮只给一次。第二次仍编数值就交回 Reviewer 收尾——换个说法的重写
    不会变出证据，多给几轮只是把预算烧在同一个结论上。"""
