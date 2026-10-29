# 中文文案必然包含全角标点
"""The parent LangGraph: one durable turn of four-agent collaboration.

Officially this is the *custom workflow* shape — a ``StateGraph`` whose nodes call
``create_agent`` agents plus deterministic logic — because the turn needs its own
review, finalize and compaction steps, not only model delegation.

    START → open_turn → supervisor → review → ┐ (should_continue)
                ↑                              ├→ supervisor   (bounded retry)
                └──────────────────────────────┘
                                             └→ finalize → compact → END

Bounds, from the inside out: middleware caps each agent's model/tool calls, the
node-level ``attempts`` cap bounds the review-retry loop, and ``recursion_limit``
caps graph steps. Every bound is observable in ``ledger`` and ``usage``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Overwrite

from . import guards, memory
from .agents import add_usage, build_supervisor, collect_delegations, collect_usage, last_ai_text
from .deps import SupportDeps
from .state import Delegation, SupportState, TurnRecord

DEFAULT_MAX_PASSES = 3
"""Supervisor passes per turn: one plan plus two evidence-backed retries."""

SUPERVISOR_STEP_BUDGET = 32
"""Steps one supervisor pass may take (its own model/tool rounds, plus the delegates it runs).

Measured, not guessed (bisect on the offline fakes, report §8e): the most expensive *legal*
shape — three passes, each delegating to FAQ and then to General before closing — needs 24
steps. At 16 and at 20 the pass raises ``GraphRecursionError`` and the turn answers
``no_result`` even though every model call succeeded; at 24 it answers (``review_blocked``,
i.e. the safe refusal, because the draft stated a number nothing evidenced). 32 keeps a
third of headroom for a fourth delegate.
"""

TURN_STEP_BUDGET = 48
"""Steps the whole turn may take — every caller must use this number, tests included.

LangGraph charges a subgraph's steps against the *parent's* remaining budget, so the parent
has to be able to hand the supervisor's budget over more than once: ``max_passes`` passes of
``SUPERVISOR_STEP_BUDGET`` plus the outer nodes. The value shipped before this measurement
was 24, which is why a compound question could exhaust the turn on its second retry.
"""


def _question(state: SupportState) -> str:
    turn_id = state.get("turn_id", "")
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage) and (not turn_id or message.id == f"{turn_id}:u"):
            return memory.message_text(message)
    return memory.message_text(state["messages"][-1]) if state.get("messages") else ""


def _roles(delegations: list[Delegation]) -> list[str]:
    return list(dict.fromkeys(item["specialist"] for item in delegations))


async def build_support_graph(deps: SupportDeps, *, checkpointer: Any = None,
                              max_passes: int = DEFAULT_MAX_PASSES, context_window: int = 12,
                              compact_trigger: int = memory.DEFAULT_TRIGGER,
                              compact_keep: int = memory.DEFAULT_KEEP,
                              compact_repair: bool = True,
                              timeout_seconds: float = 60.0,
                              **supervisor_kwargs: Any):
    """Compile one tenant-scoped graph. Only this outer graph gets a checkpointer."""
    if max_passes < 1:
        raise ValueError("max_passes must be at least 1")
    supervisor = await build_supervisor(deps, timeout_seconds=min(timeout_seconds, 30.0),
                                        **supervisor_kwargs)

    async def open_turn(state: SupportState) -> dict[str, Any]:
        """Clear the previous turn's scratch channels; history is untouched.

        ``delegations`` must be cleared too, and via ``Overwrite`` because every one of
        these channels has an appending/merging reducer — returning an empty list would
        merge into the old value. Without the reset the Reviewer would happily treat an
        earlier turn's retrieval as this turn's evidence, which is exactly the
        "no evidence, no promise" boundary buying off with stale receipts.
        """
        return {"draft": "", "answer": "", "review_feedback": "", "last_error": "",
                "review_issues": Overwrite([]), "delegations": Overwrite([]),
                "review_hard": False, "attempts": 0, "outcome": "",
                # Only *this* turn's usage entry is dropped: passes add to it, so a replay of
                # the same turn recomputes the same total instead of stacking a second copy.
                "usage": Overwrite({k: v for k, v in (state.get("usage") or {}).items()
                                    if k != state.get("turn_id", "")}),
                "turn_id": state.get("turn_id", "")}

    async def supervisor_node(state: SupportState) -> dict[str, Any]:
        turn_id = state.get("turn_id", "")
        prompt = memory.model_context(state, window=context_window)
        if state.get("review_feedback"):
            prompt = [*prompt, HumanMessage(content=f"【校验反馈】{state['review_feedback']}")]
        size = len(prompt)
        fresh: list[Any] = []
        try:
            result = await asyncio.wait_for(
                supervisor.ainvoke({"messages": prompt},
                                   config={"recursion_limit": SUPERVISOR_STEP_BUDGET}),
                timeout_seconds)
            fresh = (result.get("messages") or [])[size:]
            draft = last_ai_text(fresh)
            findings = collect_delegations(fresh, turn_id)
            failed = not fresh
        except Exception as exc:
            # A customer-facing graph must not propagate, but it must not erase the
            # reason either: without this the only observable outcome is `no_result`
            # and the cause is invisible to whoever has to fix it.
            draft, findings, failed = "", [], True
            last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
        else:
            last_error = ""
        deps.trace("delegate", "supervisor", roles=_roles(findings), attempt=state.get("attempts", 0) + 1,
                   status="failed" if failed else "ok", draft_chars=len(draft), error=last_error)
        this_pass = collect_usage(fresh)
        return {"draft": draft, "delegations": findings, "attempts": state.get("attempts", 0) + 1,
                "last_error": last_error,
                # Summed across this turn's passes: a retry is real spend. `open_turn` drops
                # this turn's earlier entry, so replaying the same turn still cannot double-bill.
                "usage": {turn_id: add_usage((state.get("usage") or {}).get(turn_id), this_pass)}}

    def review(state: SupportState) -> dict[str, Any]:
        """The Reviewer: deterministic rules, no model call, not a fifth agent."""
        customer_order_ids = set().union(*(guards._ids(memory.message_text(message))
                                           for message in state.get("messages", [])
                                           if isinstance(message, HumanMessage)))
        hard, soft = guards.review(_question(state), state.get("draft", ""),
                                   state.get("delegations", []),
                                   customer_order_ids=customer_order_ids)
        issues = [*hard, *soft]
        deps.trace("review", "result", passed=not issues, hard=hard[:4], reasons=soft[:4],
                   attempts=state.get("attempts", 0))
        return {"review_feedback": "；".join(issues), "review_issues": issues, "review_hard": bool(hard)}

    def should_continue(state: SupportState) -> str:
        """Explicit bounded edge: retry once the reviewer found a fixable gap."""
        if state.get("review_feedback") and state.get("attempts", 0) < max_passes:
            return "supervisor"
        return "finalize"

    def finalize(state: SupportState) -> dict[str, Any]:
        turn_id = state.get("turn_id", "")
        delegations = state.get("delegations", [])
        issues = state.get("review_issues", [])
        # Wording keys off the last pass; the ledger keeps everything found along the way.
        latest = [item for item in state.get("review_feedback", "").split("；") if item]
        if state.get("review_hard"):
            answer = guards.safe_fallback(latest, [], delegations, _question(state))
            outcome = "review_blocked"
        elif not state.get("draft"):
            # The customer always gets a sentence: an empty answer here would surface
            # as a blank bubble in the chat UI.
            answer = "我暂时没能核实到可靠信息，请补充更具体的问题；需要人工协助请回复「转人工」。"
            outcome = "no_result"
        elif state.get("review_feedback"):
            answer, outcome = state["draft"], "answered_unchecked"
        else:
            answer, outcome = state["draft"], "answered"
        deps.trace("finish", outcome, roles=_roles(delegations), attempts=state.get("attempts", 0),
                   answer_chars=len(answer))
        record: TurnRecord = {"turn_id": turn_id, "question": _question(state)[:500],
                              "answer": answer[:2000], "outcome": outcome, "roles": _roles(delegations),
                              "evidence_ids": [item["id"] for d in delegations for item in d["evidence"]][:12],
                              "review_issues": issues[:8], "attempts": state.get("attempts", 0)}
        updates: dict[str, Any] = {"answer": answer, "outcome": outcome, "ledger": [record]}
        if answer:
            updates["messages"] = [AIMessage(content=answer, id=f"{turn_id}:a")]
        return updates

    async def compact(state: SupportState) -> dict[str, Any]:
        """Fold the oldest messages above the watermark into the rolling summary."""
        try:
            model = await deps.model()
            update = await memory.compact(state, model=model, trigger=compact_trigger,
                                          keep=compact_keep, repair=compact_repair)
        except Exception:
            update = {"_compacted": False, "_reason": "compactor_error"}
        compacted = bool(update.pop("_compacted", False))
        reason = update.pop("_reason", "")
        absorbed = update.pop("_absorbed", 0)
        restored = update.pop("_restored", 0)
        deps.trace("memory", "compacted" if compacted else "skipped", absorbed=absorbed,
                   restored=restored, reason=reason, watermark=update.get("summary_watermark",
                                                                          state.get("summary_watermark", 0)))
        return update

    graph = StateGraph(SupportState)
    for name, node in (("open_turn", open_turn), ("supervisor", supervisor_node), ("review", review),
                       ("finalize", finalize), ("compact", compact)):
        graph.add_node(name, node)
    graph.add_edge(START, "open_turn")
    graph.add_edge("open_turn", "supervisor")
    graph.add_edge("supervisor", "review")
    graph.add_conditional_edges("review", should_continue, {"supervisor": "supervisor",
                                                            "finalize": "finalize"})
    graph.add_edge("finalize", "compact")
    graph.add_edge("compact", END)
    return graph.compile(checkpointer=checkpointer)
