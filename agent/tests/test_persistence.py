# 中文文案必然包含全角标点
"""Acceptance for phase 3: the checkpoint is the only durable store."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.fakes import ScriptedSupportModel, ai_with_tools, tool_call
from agent.fakes import offline_deps as make_deps
from agent.service import SupportService, enabled, sse_events, thread_key

pytestmark = pytest.mark.asyncio

TENANT = "t_demo"
THREAD = "t_demo:sess-1"
OUT_OF_GRAPH_WRITERS = ("_record_to_checkpoint", "faq_bridge", "app.services.dialog")


def answered(text: str) -> ScriptedSupportModel:
    return ScriptedSupportModel(supervisor_script=[
        AIMessage(content=text, usage_metadata={"input_tokens": 10, "output_tokens": 5,
                                               "total_tokens": 15})])


def build_service(saver=None, **model_kwargs):
    saver = saver or InMemorySaver()

    async def deps_for_tenant(_tenant_id: str):
        deps, _model, _events, _captured = make_deps(**model_kwargs)
        return deps

    return SupportService(deps_for_tenant, checkpointer=saver), saver


async def test_repeated_request_with_same_turn_id_does_not_duplicate() -> None:
    service, _saver = build_service(model=answered("您的订单 ORD-1001 已发货。"))
    first = await service.run_turn(TENANT, THREAD, "ORD-1001 到哪了？", turn_id="idem-1")
    history_after_one = await service.history(TENANT, THREAD)
    replay = await service.run_turn(TENANT, THREAD, "ORD-1001 到哪了？", turn_id="idem-1")
    history_after_replay = await service.history(TENANT, THREAD)

    assert first.answer == replay.answer
    assert history_after_replay == history_after_one
    assert [m["message_id"] for m in history_after_replay] == ["idem-1:u", "idem-1:a"]
    graph = await service.graph_for(TENANT)
    state = (await graph.aget_state({"configurable": {"thread_id": thread_key(TENANT, THREAD)}})).values
    assert len(state["ledger"]) == 1
    assert len(state["delegations"]) == 0


async def test_replay_skips_model_and_returns_original_turn_after_later_turn() -> None:
    model = ScriptedSupportModel(supervisor_script=[
        AIMessage(content="您好。"), AIMessage(content="再见。")])
    service, _saver = build_service(model=model)
    first = await service.run_turn(TENANT, THREAD, "你好", turn_id="first")
    await service.run_turn(TENANT, THREAD, "谢谢", turn_id="second")
    calls_before = len(model.recorded)

    replay = await service.run_turn(TENANT, THREAD, "你好", turn_id="first")

    assert replay.replayed is True
    assert replay.answer == first.answer
    assert len(model.recorded) == calls_before
    assert len(await service.history(TENANT, THREAD)) == 4


async def test_reusing_turn_id_for_another_question_fails_closed() -> None:
    service, _saver = build_service(model=answered("您好。"))
    await service.run_turn(TENANT, THREAD, "你好", turn_id="same")
    with pytest.raises(ValueError, match="turn_id was already used"):
        await service.run_turn(TENANT, THREAD, "查另一笔订单", turn_id="same")
    assert len(await service.history(TENANT, THREAD)) == 2


async def test_restart_continues_the_same_thread() -> None:
    saver = InMemorySaver()
    service_one, _ = build_service(saver=saver, model=answered("签收后 7 天内可申请退货。"))
    before = await service_one.run_turn(TENANT, THREAD, "退货期限是多久？", turn_id="p1")

    # A new process: fresh service object, same checkpointer behind it.
    service_two, _ = build_service(saver=saver, model=answered("订单 ORD-1001 已发货。"))
    after = await service_two.run_turn(TENANT, THREAD, "ORD-1001 到哪了？", turn_id="p2")

    history = await service_two.history(TENANT, THREAD)
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert history[1]["content"] == before.answer and history[3]["content"] == after.answer
    assert not any(m["message_id"] is None for m in history)


async def test_legacy_thread_history_is_imported_once() -> None:
    service, _ = build_service(model=answered("签收后 7 天内可申请退货。"))
    legacy = [HumanMessage(content="你好"), AIMessage(content="您好，有什么可以帮您？")]
    await service.run_turn(TENANT, THREAD, "退货期限是多久？", turn_id="n1", import_history=legacy)
    await service.run_turn(TENANT, THREAD, "运费谁承担？", turn_id="n2", import_history=legacy)
    history = await service.history(TENANT, THREAD)
    assert len(history) == 6  # 2 imported + 2 turns
    assert history[0]["content"] == "你好" and history[1]["content"].startswith("您好")


async def test_unverified_thread_identity_is_refused() -> None:
    service, _ = build_service(model=answered("ok"))
    with pytest.raises(PermissionError):
        await service.run_turn("t_demo", "t_other:sess-9", "你好")
    with pytest.raises(PermissionError):
        await service.run_turn("", "sess-9", "你好")


async def test_json_and_sse_carry_the_same_answer() -> None:
    service, _ = build_service(model=answered("签收后 7 天内可申请退货。"))
    result = await service.run_turn(TENANT, THREAD, "退货期限是多久？", turn_id="sse-1")
    payload = result.to_payload()
    events = sse_events(result, answer_id="a-1", footer="以上为系统答复")
    assert payload["content"] + "以上为系统答复" in events[0]
    assert events[1].startswith('data: {"type": "end"') and '"answer_id": "a-1"' in events[1]
    assert payload["outcome"] == "answered"


async def test_new_path_never_uses_the_out_of_graph_checkpoint_bridge() -> None:
    """The side writer that the phase-3 requirement removes must not be reachable.

    Scope is the shipped new chat path (`agent/*.py`). `agent/evaluation/` holds measurement
    scripts, and `probe_mounted_graph.py` deliberately drives the legacy bridge to measure the
    app-side rollback gap: naming those writers there is the subject being measured, not a write
    from the new path. Narrowing is only safe if the scan cannot go vacuous, hence the floor.
    """
    scanned = {path.name for path in Path("agent").glob("*.py")}
    assert {"graph.py", "service.py", "entrypoint.py", "memory.py", "cutover.py"} <= scanned, scanned
    sources = {path.name: path.read_text(encoding="utf-8") for path in Path("agent").glob("*.py")}
    offenders = {name: [needle for needle in OUT_OF_GRAPH_WRITERS if needle in text]
                 for name, text in sources.items() if any(needle in text for needle in OUT_OF_GRAPH_WRITERS)}
    assert offenders == {}


async def test_roles_are_attributed_per_turn_not_accumulated() -> None:
    """Regression: a later turn must not inherit the previous turn's specialist roles."""
    saver = InMemorySaver()
    delegating = ScriptedSupportModel(supervisor_script=[
        ai_with_tools("核实中", [tool_call("consult_faq", {"question": "退货期限"}, "c1")]),
        AIMessage(content="签收后 7 天内可申请退货。", usage_metadata={"input_tokens": 10,
                                                                    "output_tokens": 5,
                                                                    "total_tokens": 15})])

    async def deps_delegating(_tenant_id):
        deps, _m, _e, _c = make_deps(model=delegating)
        return deps

    async def deps_plain(_tenant_id):
        deps, _m, _e, _c = make_deps(model=ScriptedSupportModel())
        return deps

    first = await SupportService(deps_delegating, checkpointer=saver).run_turn(
        TENANT, THREAD, "退货期限是多久？", turn_id="a1")
    second = await SupportService(deps_plain, checkpointer=saver).run_turn(
        TENANT, THREAD, "谢谢", turn_id="a2")
    assert first.roles == ["faq"] and first.evidence_ids == ["chunk-7d"]
    assert second.roles == [] and second.evidence_ids == []


async def test_earlier_turn_evidence_cannot_license_a_later_number() -> None:
    """Per-turn scoping is a guard, not a nicety: stale receipts must not vouch."""
    saver = InMemorySaver()
    tokens = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    delegating = ScriptedSupportModel(supervisor_script=[
        ai_with_tools("核实", [tool_call("consult_faq", {"question": "退货期限"}, "c1")]),
        AIMessage(content="签收后 7 天内可申请退货。", usage_metadata=tokens)])
    inventing = ScriptedSupportModel(supervisor_script=[
        AIMessage(content="本店退货期为 15 天。", usage_metadata=tokens)])

    async def deps_delegating(_tenant_id):
        deps, _m, _e, _c = make_deps(model=delegating)
        return deps

    async def deps_inventing(_tenant_id):
        deps, _m, _e, _c = make_deps(model=inventing)
        return deps

    first = await SupportService(deps_delegating, checkpointer=saver,
                                 graph_kwargs={"max_passes": 1}).run_turn(
        TENANT, THREAD, "退货期限是多久？", turn_id="ev1")
    second = await SupportService(deps_inventing, checkpointer=saver,
                                  graph_kwargs={"max_passes": 1}).run_turn(
        TENANT, THREAD, "那到底几天内能退？", turn_id="ev2")
    assert first.outcome == "answered" and first.evidence_ids
    assert second.outcome == "review_blocked"
    assert "15 天" not in second.answer


async def test_unfinished_approval_thread_stays_with_the_old_graph() -> None:
    """Compatibility, proven against a real interrupt rather than a flag.

    A legacy thread can sit paused inside ``interrupt()`` awaiting human approval. The
    new graph must (a) never read that checkpoint as its own schema, and (b) leave it
    resumable by the old graph afterwards.
    """
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.types import Command, interrupt

    saver = InMemorySaver()

    async def ask_human(state):
        decision = interrupt({"action": "transfer_to_human", "reason": "等待人工确认"})
        return {"messages": [AIMessage(content=f"工单 {decision.get('ticket_id')} 已创建。")]}

    graph = StateGraph(MessagesState)
    graph.add_node("ask", ask_human)
    graph.add_edge(START, "ask")
    graph.add_edge("ask", END)
    legacy = graph.compile(checkpointer=saver)
    thread = "t_demo:sess-approval"
    config = {"configurable": {"thread_id": thread}}

    await legacy.ainvoke({"messages": [HumanMessage(content="我要转人工")]}, config)
    paused = await legacy.aget_state(config)
    assert paused.next == ("ask",), "期望这是一个真实的中断态线程"
    assert enabled(pending_ticket=True) is False  # 路由层因此不会切到新图

    service, _ = build_service(saver=saver, model=answered("签收后 7 天内可申请退货。"))
    assert await service.history(TENANT, thread) == []
    assert await service.memory_state(TENANT, thread) == {
        "summary": "", "watermark": 0, "total": 0, "summarized": 0, "verbatim": 0}

    resumed = await legacy.ainvoke(Command(resume={"confirmed": True, "ticket_id": "T-1"}), config)
    assert "T-1" in resumed["messages"][-1].content  # 新图没碰坏旧线程


async def test_pending_approval_stays_on_the_legacy_graph() -> None:
    assert enabled(pending_ticket=True) is False
    assert enabled() is False  # rollout flag defaults to off; no traffic switches by accident
