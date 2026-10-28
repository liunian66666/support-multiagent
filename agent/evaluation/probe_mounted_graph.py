"""Measured ledger for the MOUNTED app-side graph, driven through its production entry.

Why this exists: `python_agent/app/api/chat.py` now carries a `FOUR_AGENT_GRAPH` switch (default off)
that routes a turn into `app.services.four_agent.run_turn` -> `app.multi_agent.graph`. The app-side
test (`python_agent/tests/test_four_agent_graph.py`, 2 cases) calls `build_graph` directly, so the
adapter that actually runs in production -- `run_turn`: the `support_v2:` thread key, the one-time
legacy import, the per-tenant graph cache, the trace fields -- plus the persistence/replay/memory/
retry/failure edges are unmeasured.

Zero model calls, zero real data: scripted delegates, `InMemorySaver`, and a faithful in-memory
stand-in for `app.state.faq_bridge` (the legacy canonical history -- `aget_state` to read, `ainvoke`
to append, exactly the two calls `recent_messages` / `_record_to_checkpoint` make). The scripted
supervisor invokes the REAL `consult_faq` / `consult_order` / `consult_general` delegate tools, and
the order delegate invokes the REAL `lookup_my_order` tool with only the business HTTP adapter
swapped, so evidence extraction, `verified_results` and the ownership boundary stay on the path.

  M1 compound   does the real entry delegate order+faq in one turn and checkpoint the answer?
  M2 replay     does a repeated identical turn duplicate messages and billing rows?
  M3 rollback   after N turns on the new path, how many of them can the OLD path still see?
  M4 bounded    is the review -> supervisor retry capped, and does the cap end in a safe answer?
  M5 failure    when the model dies (403 / refused): what stays persisted, what does trace say?
  M6 summary    does the watermark/RemoveMessage stay in bounds, and on summary failure?
  M7 security   does an order claim with no OK verification behind it leak?
  M9  concurrency are 4 simultaneous turns on one thread all persisted (same process)?
  M10/M11       the same thread written by two independently compiled graphs -- the two-worker-case,
                measured on the mounted graph and on agent/graph.py so the loss can be attributed.

Run (repo root, isolated test image, read-only mount):
  MSYS_NO_PATHCONV=1 docker run --rm -v "$PWD:/work:ro" \
    -e PYTHONPATH=/work/python_agent:/work -e PYTHONPYCACHEPREFIX=/tmp/pyc \
    -w /work/python_agent cs-test:agent python /work/agent/evaluation/probe_mounted_graph.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.tools import OrderLookup
from app.multi_agent.graph import GraphDependencies, build_graph
from app.services import four_agent, trace

ORD_OK = OrderLookup("OK", "订单 ORD-2024-003 状态：待发货；金额：199元。")
ORD_DENIED = OrderLookup("NOT_FOUND", "订单不存在或不属于您。")
POLICY_Q = "ORD-2024-003 的状态，以及怎么退货？"
FOLLOW_Q = "ORD-2024-003 到哪了？"
FRIEND_Q = "帮我查我朋友订单 ORD-9999 的状态"
UNEVIDENCED = "退货运费由我们承担，30天内都能退。"
CLAIMING = "订单 ORD-9999 状态：已发货，请放心。"
V2 = "support_v2:"
TENANT = "t_demo"


class FakeModel:
    """Merge model for the summarize node: answers with a stub, or dies like the gateway."""

    def __init__(self, text="摘要：客户此前咨询退货，未解决的问题是运费承担。", error=None):
        self.text, self.error, self.calls = text, error, 0

    async def ainvoke(self, _messages):
        self.calls += 1
        if self.error:
            raise self.error
        return AIMessage(content=self.text)


class FakeBridge:
    """In-memory stand-in for app.state.faq_bridge, the legacy canonical chat history."""

    def __init__(self):
        self.threads: dict[str, list] = {}

    def seed(self, thread_id: str, pairs: int):
        self.threads[thread_id] = [m for i in range(pairs)
                                   for m in (HumanMessage(content=f"历史提问{i}"),
                                             AIMessage(content=f"历史回答{i}"))]

    def length(self, thread_id: str) -> int:
        return len(self.threads.get(thread_id, []))

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": self.threads.get(config["configurable"]["thread_id"], [])})

    async def ainvoke(self, payload, config):
        self.threads.setdefault(config["configurable"]["thread_id"], []).extend(payload["messages"])
        return {"messages": payload["messages"]}


def last_human(messages):
    return next((m.content for m in reversed(messages) if isinstance(m, HumanMessage)), "")


class Supervisor:
    """The parent agent: delegates through the REAL consult_* tools, then returns `draft`."""

    def __init__(self, pick, draft, *, fail=None):
        self.pick, self.draft, self.fail, self.calls = pick, draft, fail, 0
        self.tools: dict[str, object] = {}
        self.last_prompt: list = []      # exactly what the parent agent is fed this turn

    async def ainvoke(self, payload, config=None):
        self.calls += 1
        messages = payload["messages"]
        self.last_prompt = list(messages)
        if self.fail:
            raise self.fail
        question = last_human(messages)
        fresh = []
        for name in self.pick(question):
            body = await self.tools[name].ainvoke({"question": question})
            fresh.append(ToolMessage(content=body, name=name, tool_call_id=f"c-{self.calls}-{name}"))
        fresh.append(AIMessage(content=self.draft))
        return {"messages": [*messages, *fresh]}


class FaqDelegate:
    """Stands in for the existing FAQ agent: one search_kb call, optional evidence ids."""

    def __init__(self, answer="签收后七天内可申请退货。", evidence=("chunk-7",), search=True):
        self.answer, self.evidence, self.search = answer, evidence, search

    async def ainvoke(self, payload, config=None):
        messages = payload["messages"]
        if not self.search:
            return {"messages": [*messages, AIMessage(content=self.answer)]}
        trace.record("evidence", "search_kb", evidence_ids=list(self.evidence))
        return {"messages": [*messages,
                             AIMessage(content="", tool_calls=[{"name": "search_kb",
                                                                "args": {"query": last_human(messages)},
                                                                "id": "k1"}]),
                             ToolMessage(content="原文：签收后七天内可申请退货，运费承担见细则。",
                                         name="search_kb", tool_call_id="k1"),
                             AIMessage(content=self.answer)]}


class OrderDelegateTools:
    """The order subagent, driven through the real `lookup_my_order` tool object."""

    def __init__(self, tools, order_id="ORD-9999"):
        self.tools, self.order_id = {t.name: t for t in tools}, order_id

    async def ainvoke(self, payload, config=None):
        messages = payload["messages"]
        raw = await self.tools["lookup_my_order"].ainvoke({"order_id": self.order_id})
        return {"messages": [*messages,
                             AIMessage(content="", tool_calls=[{"name": "lookup_my_order",
                                                                "args": {"order_id": self.order_id},
                                                                "id": "o1"}]),
                             ToolMessage(content=raw, name="lookup_my_order", tool_call_id="o1"),
                             AIMessage(content=json.loads(raw)["text"])]}


def create_agent_patch(supervisor, *, order_id="ORD-9999", faq=None):
    """Map the tool-name set create_agent receives onto the scripted stand-ins."""
    def fake_create_agent(model, tools, **kwargs):
        names = {t.name for t in tools}
        if "consult_faq" in names:
            supervisor.tools = {t.name: t for t in tools}
            return supervisor
        if "lookup_my_order" in names:
            return OrderDelegateTools(tools, order_id)
        return SimpleNamespace(ainvoke=AsyncMock(
            return_value={"messages": []}))

    return fake_create_agent


def env_patches(model, supervisor, *, faq=None, order_id="ORD-9999", order_result=ORD_OK):
    """Everything that must stay patched while the graph is COMPILED and RUN.

    It has to cover the run, not just the build: `four_agent.get_graph` caches per tenant, so a
    graph built outside the patched window would be rebuilt by `run_turn` with the real
    `get_tenant_model` (which needs a live model gateway).
    """
    general = SimpleNamespace(ainvoke=AsyncMock(return_value={
        "messages": [AIMessage(content="您好，有什么可以帮您？")]}))
    return [patch("app.multi_agent.graph.get_tenant_model", AsyncMock(return_value=model)),
            patch("app.multi_agent.graph.faq_agent", AsyncMock(return_value=faq or FaqDelegate())),
            patch("app.multi_agent.graph.intake_agent", AsyncMock(return_value=general)),
            patch("app.multi_agent.graph.create_agent",
                  side_effect=create_agent_patch(supervisor, order_id=order_id)),
            patch("app.multi_agent.graph.business_tools.lookup_order",
                  AsyncMock(return_value=order_result)),
            patch("app.multi_agent.graph.business_tools.list_owned_orders",
                  AsyncMock(return_value=order_result))]


def app_with(seed_pairs=0, thread=None, model=None):
    app = SimpleNamespace(state=SimpleNamespace(checkpointer=InMemorySaver(), faq_bridge=FakeBridge()))
    if thread and seed_pairs:
        app.state.faq_bridge.seed(thread, seed_pairs)
    return app, (model or FakeModel())


async def compile_graph(app, model, supervisor, *, faq=None, order_id="ORD-9999",
                        order_result=ORD_OK):
    """Build only (for the direct-graph memory case); the returned graph keeps the fake objects."""
    with ExitStack() as stack:
        for ctx in env_patches(model, supervisor, faq=faq, order_id=order_id,
                               order_result=order_result):
            stack.enter_context(ctx)
        return await build_graph(GraphDependencies(app, TENANT), app.state.checkpointer)


async def turn(app, model, supervisor, question, *, thread=f"{TENANT}:buyer1",
               order_result=ORD_OK, faq=None, order_id="ORD-2024-003", turn_id="",
               event_sink=None):
    """One turn through the production entry, with run_turn doing the per-tenant graph build.

    `event_sink`: pass a list to keep **this** turn's trace events even when the turn raises.
    Without it the raise throws the event stream away, and the only numbers left are the ones
    sampled from the *next* (successful) turn — which read as "no error event was recorded"
    when in fact the failing turn's reason is right there in that discarded stream.
    """
    with ExitStack() as stack:
        for ctx in env_patches(model, supervisor, faq=faq, order_id=order_id,
                               order_result=order_result):
            stack.enter_context(ctx)
        token = trace.bind(trace.new_trace("mounted-probe", TENANT, thread))
        stack.callback(trace.reset, token)
        # 幂等键是候选补丁（deploy/mounted_persistence_fix.patch）加上的；没有就不传，
        # 这样同一支探针在未打补丁的 app 代码上也能跑，对照组才有意义。
        extra = {"turn_id": turn_id} if turn_id and _accepts_turn_id() else {}
        try:
            state = await four_agent.run_turn(app, TENANT, thread, question, **extra)
        except Exception:
            if event_sink is not None:
                event_sink.extend((e.get("stage"), e.get("name"), e.get("attributes"))
                                  for e in trace.current().events)
            raise
        run = trace.current()
        events = [(e.get("stage"), e.get("name"), e.get("attributes")) for e in run.events]
    return (app.state.four_agent_graphs[TENANT], state,
            {"route": getattr(run, "route", ""), "specialist": getattr(run, "specialist", ""),
             "events": events})


def _accepts_turn_id() -> bool:
    import inspect

    return "turn_id" in inspect.signature(four_agent.run_turn).parameters


def compound(question):
    out = []
    if "ORD-" in question:
        out.append("consult_order")
    if "退货" in question or "运费" in question:
        out.append("consult_faq")
    return out or ["consult_general"]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    # 2026-10-28：`app/services/four_agent.py` 已换成薄适配器（run_turn → agent.cutover → agent/ 图）。
    # 本探针的桩全打在 app.multi_agent.graph.* 上，所以走 run_turn 的那些项会在模型解析处抛
    # RuntimeError——那是**实现换了**，不是被测链路回退了。留档不等于还能跑：当前接法的同一套账本
    # 在 probe_mounted_bridge.py（run_all.sh mounted 已经指过去）。这里宁可把话说清楚再退出，
    # 也不要留一段 traceback 让下一个人去猜。
    import inspect
    if "agent.cutover" in inspect.getsource(four_agent.run_turn):
        raise SystemExit(
            "此探针只描述旧接法（app/multi_agent/graph.py）。当前工作树的挂载入口是 agent.cutover → agent/ 图，"
            "请跑 agent/evaluation/probe_mounted_bridge.py（或 bash agent/evaluation/run_all.sh mounted）。")
    rows = []

    def emit(case, ok, **obs):
        rows.append({"case": case, "ok": bool(ok), **obs})
        print(f"[{'PASS' if ok else 'FINDING'}] {case}: " + json.dumps(obs, ensure_ascii=False)[:600])

    # ---- M1 compound, through run_turn (not a hand-built graph) ----------------------
    app, model = app_with(2, "t_demo:buyer1")
    graph, state, runinfo = await turn(app, model,
        Supervisor(compound, "订单 ORD-2024-003 状态：待发货。退货需核验商品与签收时间。"), POLICY_Q)
    restored = await graph.aget_state({"configurable": {"thread_id": V2 + "t_demo:buyer1"}})
    bridge = await app.state.faq_bridge.aget_state({"configurable": {"thread_id": "t_demo:buyer1"}})
    emit("M1_compound_entry",
         [d["specialist"] for d in state["delegations"]] == ["order", "faq"]
         and restored.values["answer"] == state["answer"]
         and runinfo["route"] == "multi_agent" and runinfo["specialist"] == "order,faq"
         and state["review_feedback"] == "",
         delegations=[d["specialist"] for d in state["delegations"]],
         order_codes=[r.get("code") for d in state["delegations"] if d["specialist"] == "order"
                      for r in d.get("verified_results", [])],
         faq_evidence=[d.get("evidence_ids") for d in state["delegations"] if d["specialist"] == "faq"],
         review_feedback=state["review_feedback"],
         answer_channel_checkpointed=restored.values["answer"] == state["answer"],
         trace_route=runinfo["route"], trace_specialist=runinfo["specialist"],
         # 成本列：Reviewer 多拦一次 = 多跑一遍 supervisor = 多一批 model_messages。
         # 不记这一列，"把规则收紧"的代价就会从对照表里消失（§5h 里 control 2 条 / patched 4 条
         # 就是这么被抓出来的）。
         attempts=state.get("attempts"), billed_msgs=len(state.get("model_messages") or []),
         legacy_history_messages_visible=len(bridge.values["messages"]))

    # ---- M2 replay: the same turn submitted twice (double click / proxy retry) -------
    app2, model2 = app_with()
    await turn(app2, model2, Supervisor(compound, "在运输中，预计周三送达。"), FOLLOW_Q,
               thread="t_demo:replay")
    _, s2, _ = await turn(app2, model2, Supervisor(compound, "在运输中，预计周三送达。"), FOLLOW_Q,
                          thread="t_demo:replay")
    humans = [m for m in s2["messages"] if isinstance(m, HumanMessage)]
    emit("M2_replay_idempotency", len(humans) == 1,
         turns_submitted=2, human_messages_in_state=len(humans),
         duplicate_of_last_human=sum(1 for m in humans if m.content == FOLLOW_Q),
         distinct_message_ids=len({m.id for m in s2["messages"]}), total_messages=len(s2["messages"]),
         billed_msgs_per_turn=len(s2["model_messages"]),
         note="FINDING: ChatRequest carries no turn_id, so the entry cannot tell a retry from a new "
              "turn; the same question lands twice in the checkpoint and both turns get billed. "
              "agent/ dedups on turn_id and leaves the state byte-identical.")

    # ---- M2b replay WITH an idempotency key (the other half of the §5e M2 finding) ----
    # 入口能不能带键由 app 侧决定；这一项专测"带了键之后还重不重复"。
    # 未打补丁时 run_turn 没有 turn_id 参数 ⇒ 直接判不通过并说明缺的是哪一半。
    # 断言只看**同一遍内**的第一轮 vs 重放后的状态（不跨遍对照 control 的数字：
    # 两遍的 model_messages 受 Reviewer 是否触发重试影响，跨遍对照会把成本差当成正确性差）。
    if _accepts_turn_id():
        app2b, model2b = app_with()
        _, first, _ = await turn(app2b, model2b, Supervisor(compound, "在运输中，预计周三送达。"),
                                 FOLLOW_Q, thread="t_demo:replay2", turn_id="turn-1")
        _, s2b, info2b = await turn(app2b, model2b, Supervisor(compound, "在运输中，预计周三送达。"),
                                    FOLLOW_Q, thread="t_demo:replay2", turn_id="turn-1")
        humans_b = [m for m in s2b["messages"] if isinstance(m, HumanMessage)]
        skipped = any(name == "replay_skipped" for _s, name, _a in info2b["events"])
        emit("M2b_replay_with_turn_id",
             len(humans_b) == 1 and skipped
             and s2b["answer"] == first["answer"]
             and len(s2b["model_messages"]) == len(first["model_messages"])
             and len(s2b["messages"]) == len(first["messages"]),
             human_messages=len(humans_b), replay_skipped=skipped,
             answer_identical=s2b["answer"] == first["answer"],
             transcript_messages=len(s2b["messages"]), transcript_after_first_turn=len(first["messages"]),
             billed_msgs_after_replay=len(s2b["model_messages"]),
             billed_msgs_first_turn=len(first["model_messages"]),
             note="PASS with the staged persistence patch: a completed turn replayed under the same "
                  "turn_id short-circuits before the model runs, so neither the transcript nor the "
                  "billing rows grow. Without a key the behaviour stays exactly as M2 shows. "
                  "Entry contract caveat: ChatRequest still has no turn_id field, so this only "
                  "engages once the caller (or the proxy) supplies one.")
    else:
        emit("M2b_replay_with_turn_id", False,
             note="FINDING: run_turn() 还没有 turn_id 参数——幂等键的入口契约不在 app 侧，"
                  "所以 M2 的重放现在无从去重（候选补丁见 deploy/mounted_persistence_fix.patch）。")

    # ---- M3 rollback visibility ------------------------------------------------------
    app3, model3 = app_with(4, "t_demo:rollback")
    before = app3.state.faq_bridge.length("t_demo:rollback")
    for _ in range(4):
        await turn(app3, model3, Supervisor(compound, "好的，已记录。"), FOLLOW_Q,
                   thread="t_demo:rollback", order_id="ORD-2024-003")
    _, own, _ = await turn(app3, model3, Supervisor(compound, "好的，已记录。"), FOLLOW_Q,
                           thread="t_demo:rollback", order_id="ORD-2024-003")
    after = app3.state.faq_bridge.length("t_demo:rollback")
    emit("M3_rollback_visibility", after > before,
         bridge_before=before, bridge_after_5_new_turns=after,
         new_path_turns_invisible_to_legacy=5, new_graph_own_messages=len(own["messages"]),
         note="FINDING: the legacy path reads recent_messages(faq_bridge) while the new graph writes "
              "only support_v2:*, so flipping the switch back off resumes the old path with the "
              "ON-period turns missing from its context. Visible chat history is NOT at risk (business "
              "MySQL owns it); agent-side continuity is.")

    # ---- M4 bounded review retry -----------------------------------------------------
    app4, model4 = app_with()
    stubborn = Supervisor(lambda q: ["consult_faq"], UNEVIDENCED)
    _, state4, run4 = await turn(app4, model4, stubborn, "退货运费谁出？",
                                faq=FaqDelegate(answer="运费承担见商品页细则。", evidence=()))
    emit("M4_bounded_retry", stubborn.calls <= 2 and UNEVIDENCED not in state4["answer"],
         supervisor_calls=stubborn.calls, attempts=state4.get("attempts"),
         review_rounds=sum(1 for e in run4["events"] if e[0] == "review"),
         review_reasons=[r for e in run4["events"] if e[0] == "review" for r in e[2].get("reasons", [])],
         leaked_unverified_claim=UNEVIDENCED in state4["answer"], final_answer=state4["answer"][:48])

    # ---- M5 model failure, then recovery from the same checkpointer ------------------
    app5, model5 = app_with()
    dying = Supervisor(compound, "", fail=RuntimeError("403 Free quota exhausted"))
    raised = None
    dying_events: list = []          # ← 故障那一轮自己的事件流；不接住就随 raise 一起丢了
    try:
        await turn(app5, model5, dying, FOLLOW_Q, thread="t_demo:dying",
                   order_id="ORD-2024-003", event_sink=dying_events)
    except Exception as exc:  # noqa: BLE001 - the question is what survives the raise
        raised = f"{type(exc).__name__}: {exc}"[:120]
    tup = await app5.state.checkpointer.aget_tuple({"configurable": {"thread_id": V2 + "t_demo:dying"}})
    persisted = tup.checkpoint.get("channel_values", {}).get("messages", []) if tup else []
    # 判据是"有没有**没人回答的人消息**"，不是"有没有人消息"：
    # 原写法 humans==0 会把两种正确修法都判成失败——① 回滚这条问句（旧桥的行为），
    # ② 留下问句但补一条安全答复（agent/ 与 §5 http 那 20 项的行为：降级不空白）。
    # 改判据对**两个臂同时生效**，所以对照数字仍然可比；control 该红的还是红。
    dangling = 0
    seen_human = False
    for m in persisted:
        if isinstance(m, HumanMessage):
            dangling += 1 if seen_human else 0   # 上一条人消息还没被回答
            seen_human = True
        elif isinstance(m, AIMessage) and (m.content or "").strip():
            seen_human = False
    dangling += 1 if seen_human else 0            # 结尾停在人消息上 = 悬空
    humans_left = sum(1 for m in persisted if isinstance(m, HumanMessage))
    ai_left = sum(1 for m in persisted if isinstance(m, AIMessage))
    # 失败原因到底可不可见，有两条通道，各量各的：
    #   last_error_persisted        —— 检查点通道，能活到下一轮、能进 payload
    #   failure_turn_error_events   —— **故障那一轮自己**的 trace 事件（event_sink 接住的）
    #   recovery_turn_error_events  —— 之后那条恢复轮的事件，正确读法是"恢复轮是干净的"，
    #                                  不能用它判断"故障当时可不可见"（早期版本就这么读反过一次）
    channels = (tup.checkpoint.get("channel_values") or {}) if tup else {}
    last_error_persisted = str(channels.get("last_error") or "")
    app5.state.four_agent_graphs = {}  # same saver, rebuilt graph == service restart
    _, s5, run5 = await turn(app5, model5, Supervisor(compound, "订单在运输中，预计周三到。"),
                             "那什么时候到？", thread="t_demo:dying", order_id="ORD-2024-003")
    emit("M5a_model_failure_does_not_break", raised is not None and bool(s5["answer"]),
         raised=raised, recovered_after_rebuild=bool(s5["answer"]),
         note="the graph neither hangs nor returns a blank answer once chat.py catches the error")
    emit("M5b_failure_state_and_visibility", dangling == 0,
         dangling_unanswered=dangling, human_messages_persisted=humans_left,
         assistant_messages_persisted=ai_left,
         last_error_persisted=last_error_persisted[:70],
         failure_turn_error_events=sum(1 for e in dying_events if e[0] == "error"),
         recovery_turn_error_events=sum(1 for e in run5["events"] if e[0] == "error"),
         note="FINDING: the failed turn leaves an answered-less user message in the checkpoint "
              "(the legacy path writes the bridge only on success), and there is no last_error "
              "channel, so the gateway reason reaches neither trace nor payload")

    # ---- M6 summary watermark bounds (direct graph call; run_turn imports <=12) ------
    async def summary_case(pairs, *, merge_error=None):
        app6, model6 = app_with(model=FakeModel(error=merge_error))
        seeded = [m for i in range(pairs)
                  for m in (HumanMessage(content=f"提问{i}"), AIMessage(content=f"回答{i}"))]
        graph6 = await compile_graph(app6, model6, Supervisor(compound, "好的，已记录。"),
                                     order_id="ORD-2024-003")
        run = trace.new_trace("mounted-probe-m6", TENANT, "t_demo:mem")
        token = trace.bind(run)
        try:
            with ExitStack() as stack:
                for ctx in [patch("app.multi_agent.graph.business_tools.lookup_order",
                                  AsyncMock(return_value=ORD_OK)),
                            patch("app.multi_agent.graph.business_tools.list_owned_orders",
                                  AsyncMock(return_value=ORD_OK))]:
                    stack.enter_context(ctx)
                st = await graph6.ainvoke({"messages": [*seeded, HumanMessage(content=FOLLOW_Q)]},
                                          {"configurable": {"thread_id": "t_demo:mem"},
                                           "recursion_limit": 16})
        finally:
            trace.reset(token)
        remaining = st["messages"]
        return {"merge_calls": model6.calls, "summary_chars": len(st.get("summary") or ""),
                "watermark": st.get("summary_watermark"), "input_messages": len(seeded) + 1,
                "remaining_messages": len(remaining),
                "recent_verbatim_humans": sum(1 for m in remaining if isinstance(m, HumanMessage)),
                "memory_events": [(e.get("stage"), e.get("name")) for e in run.events
                                  if e.get("stage") == "memory"]}

    ok6, fail6 = await summary_case(14), await summary_case(14, merge_error=RuntimeError("403"))
    emit("M6_summary_watermark",
         ok6["watermark"] == ok6["input_messages"] + 1 - ok6["remaining_messages"]
         and ok6["remaining_messages"] == 12 and ok6["summary_chars"] > 0
         and fail6["summary_chars"] == 0
         and fail6["remaining_messages"] == fail6["input_messages"] + 1,
         trigger=ok6, merge_failure=fail6)

    # ---- M7 order claim with no OK behind it ------------------------------------------
    app7, model7 = app_with()
    claimer = Supervisor(lambda q: ["consult_order"], CLAIMING)
    _, state7, run7 = await turn(app7, model7, claimer, FRIEND_Q, thread="t_demo:friend",
                                 order_result=ORD_DENIED, order_id="ORD-9999")
    emit("M7_unverified_order_claim", CLAIMING not in state7["answer"],
         supervisor_calls=claimer.calls,
         verified_codes=[r.get("code") for d in state7["delegations"] if d["specialist"] == "order"
                         for r in d.get("verified_results", [])],
         review_reasons=[r for e in run7["events"] if e[0] == "review" for r in e[2].get("reasons", [])][:3],
         leaked=CLAIMING in state7["answer"], final_answer=state7["answer"][:48])

    # ---- M9 same-process concurrency, through the production entry --------------------
    app9, model9 = app_with()
    sup9 = Supervisor(compound, "已记录，稍后同步进度。")
    with ExitStack() as stack:
        for ctx in env_patches(model9, sup9, order_id="ORD-2024-003"):
            stack.enter_context(ctx)
        outs9 = await asyncio.gather(*(
            four_agent.run_turn(app9, TENANT, "t_demo:conc", f"并发问题{index}")
            for index in range(4)))
    cfg9 = {"configurable": {"thread_id": V2 + "t_demo:conc"}}
    st9 = (await app9.state.four_agent_graphs[TENANT].aget_state(cfg9)).values
    humans9 = sum(1 for m in st9["messages"] if isinstance(m, HumanMessage))
    emit("M9_same_process_concurrency", humans9 == 4 and len(outs9) == 4,
         concurrent_turns=4, human_messages=humans9,
         assistant_messages=sum(1 for m in st9["messages"] if isinstance(m, AIMessage)),
         answers_returned=sum(1 for o in outs9 if (o.get("answer") or "")),
         note="four_agent._thread_locks is a process-local asyncio lock: it does serialise turns, "
              "but only inside the one process that holds it")

    # ---- M10 / M11 cross-instance writes on one thread key (two worker processes) ----
    async def cross_instance(factory, questions, thread_key):
        saver = InMemorySaver()
        graphs = [await factory(saver) for _ in questions]
        cfg = {"configurable": {"thread_id": thread_key}, "recursion_limit": 16}
        with ExitStack() as stack:
            for ctx in [patch("app.multi_agent.graph.business_tools.lookup_order",
                              AsyncMock(return_value=ORD_OK)),
                        patch("app.multi_agent.graph.business_tools.list_owned_orders",
                              AsyncMock(return_value=ORD_OK))]:
                stack.enter_context(ctx)
            await asyncio.gather(*(g.ainvoke({"messages": [HumanMessage(q)]}, cfg)
                                   for g, q in zip(graphs, questions)))
        state = (await graphs[0].aget_state(cfg)).values
        return (sum(1 for m in state["messages"] if isinstance(m, HumanMessage)),
                len(state["messages"]),
                {"ledger": len(state.get("ledger") or []),
                 "delegations": len(state.get("delegations") or []),
                 "watermark": int(state.get("summary_watermark") or 0)})

    async def mounted_factory(saver, index):
        app_x, model_x = app_with()
        app_x.state.checkpointer = saver
        return await compile_graph(app_x, model_x,
                                   Supervisor(compound, f"实例{index}的答复。"),
                                   order_id="ORD-2024-003")

    async def agent_factory(saver, index):
        from agent.fakes import ScriptedSupportModel, offline_deps
        from agent.service import SupportService

        async def deps_for_tenant(_tenant):
            deps, _m, _e, _c = offline_deps(model=ScriptedSupportModel(supervisor_script=[
                AIMessage(content=f"实例{index}的答复。")]))
            return deps

        service = SupportService(deps_for_tenant, checkpointer=saver)
        return await service.graph_for(TENANT)

    QS = ["ORD-2024-003 的状态，以及怎么退货？", "ORD-2024-004 的状态，以及怎么退货？"]
    mounted_trials, agent_trials = [], []
    for trial in range(5):
        mounted_trials.append(await cross_instance(
            lambda s, i=trial: mounted_factory(s, i), QS, f"{V2}t_demo:xproc-{trial}"))
        agent_trials.append(await cross_instance(
            lambda s, i=trial: agent_factory(s, i), QS, f"{V2}t_demo:xproc-a{trial}"))
    lost = lambda trials: sum(1 for humans, _n, _extra in trials if humans < 2)
    emit("M10_cross_instance_write_mounted", lost(mounted_trials) == 0,
         trials=5, human_counts=[h for h, _n, _x in mounted_trials],
         message_counts=[n for _h, n, _x in mounted_trials],
         ledgers=[x["ledger"] for _h, _n, x in mounted_trials],
         lost_turn_trials=lost(mounted_trials),
         note="two independently compiled graphs sharing one checkpointer and one thread key = the "
              "two-worker-process case; asyncio locks in different processes cannot protect it")
    emit("M11_cross_instance_write_agent_v2", lost(agent_trials) == 0,
         trials=5, human_counts=[h for h, _n, _x in agent_trials],
         message_counts=[n for _h, n, _x in agent_trials],
         ledgers=[x["ledger"] for _h, _n, x in agent_trials],
         lost_turn_trials=lost(agent_trials),
         note="the same experiment against agent/graph.py: the platform-level loss shows up on both "
              "sides, so the difference has to be read from the ledger column, not from the loss rate")

    summary = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "pass": sum(1 for r in rows if r["ok"]), "findings": sum(1 for r in rows if not r["ok"]),
               "total": len(rows), "rows": rows}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(summary, ensure_ascii=False) + "\n")
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"wrote → {args.out}")
    print(json.dumps({k: summary[k] for k in ("generated_at", "pass", "findings", "total")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
