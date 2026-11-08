# ruff: noqa: RUF001  # 报告文案含中文
"""挂载入口换接法之后的同一套账本：`four_agent.run_turn` 现在跑的是 `agent/` 那张图。

2026-10-28 工作树里 `python_agent/app/services/four_agent.py` 变成薄适配器，
`run_turn` 里 `from app.agent.support.cutover import run_turn`——也就是说开关打开后生效的
不再是 `app/multi_agent/graph.py` 那份独立实现，而是阶段2–4 交付的 `agent/` 图。

这件事不能靠"读一眼代码"下结论。§5e 那 12 项、§5f 的 Reviewer 覆盖面、§5h/§5i 的
候选补丁，全部是在**旧接法**下量的；它们的成因（没有 `turn_id` 幂等键、Reviewer 只比
`evidence_ids` 非空、跨实例后写覆盖）都长在**被换掉的那份实现**里。换接法之后：

  * 有的缺口会**自动消失**（因为跑的是 `agent/service.py`，它本来就有 turn_id 重放检测）；
  * 有的缺口**不会消失**（跨实例并发仍然只是进程内 `asyncio.Lock`）；
  * 有的会**换成新的形状**（生产 FAQ agent 被 `reuse_faq_agent=True` 继承，
    于是 `kb_hits`/缓存这些旧路径副作用回到了链路上）。

这支探针把三种情况分开量，每一条都带"是谁在跑"的证据：

  B0  topology   这一轮究竟哪张图在跑、哪一份 Reviewer 开过火（用计数器，不看 import）
  B1  compound   复合问真的派了两个专职 Agent，并且把证据与结论一起进了检查点
  B2  replay     不带 turn_id 重放一次会不会多出一轮（旧接法叫 M2）
  B2b turn_id    带同一个 turn_id 重放：`_replayed` 为真、人类消息不翻倍、记账不多算
  B3  legacy     首次那一轮把旧会话原文读进来并留在上下文里
  B4  bounded    复核不通过 ⇒ supervisor 重试有上界，且上界落在安全话术而不是原稿
  B5  failure    模型挂掉：这一轮留下什么、trace 里有没有那一轮的错误事件
  B6  watermark  12 轮之后水位线推进、原文一条不删（`agent/` 不用 RemoveMessage）
  B7  security   没有本轮核验的订单号被断言 ⇒ hard 拦
  B9  conc_same  同一线程同进程 4 并发全落
  B10 conc_two   两个图实例（=两 worker/两副本）共用一个 checkpointer 写同一线程

零模型调用、零真实数据：脚本化 supervisor/专职 Agent、`InMemorySaver`、业务订单适配器换掉。
`legacy_history` 走 `app.agent.faq_agent.recent_messages` 的替身，所以旧会话读取这一条也是闭合的。

跑（仓库根，隔离测试镜像，只读挂载）：
  MSYS_NO_PATHCONV=1 docker run --rm -v "$PWD:/work:ro" \\
    -e PYTHONPATH=/work/python_agent:/work -e PYTHONPYCACHEPREFIX=/tmp/pyc \\
    -w /work/python_agent cs-test:agent python /work/python_agent/evaluation/four_agent/probe_mounted_bridge.py
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[3]
for entry in (str(ROOT), str(ROOT / "python_agent")):
    if entry not in sys.path:
        sys.path.insert(0, entry)


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 复用旧探针里的替身件：脚本化 supervisor、FaqDelegate、OrderDelegateTools、FakeBridge。
_pmg = _load("python_agent/evaluation/four_agent/probe_mounted_graph.py", "_probe_mounted_graph")
Supervisor = _pmg.Supervisor
FaqDelegate = _pmg.FaqDelegate
FakeModel = _pmg.FakeModel
FakeBridge = _pmg.FakeBridge
OrderDelegateTools = _pmg.OrderDelegateTools
compound = _pmg.compound
POLICY_Q = _pmg.POLICY_Q
FOLLOW_Q = _pmg.FOLLOW_Q
UNEVIDENCED = _pmg.UNEVIDENCED
CLAIMING = _pmg.CLAIMING
ORD_OK = _pmg.ORD_OK
TENANT = _pmg.TENANT
V2 = _pmg.V2

import app.agent.support.cutover as cutover  # noqa: E402
from app.agent.support import agents as agent_agents  # noqa: E402, F401  (预加载：探针按 sys.modules 判定接法)
from app.agent.support import guards as agent_guards  # noqa: E402
from app.agent.tools import OrderLookup  # noqa: E402
from app.services import four_agent, trace  # noqa: E402

ORD_BRIDGE_OK = OrderLookup("OK", "订单 ORD-2024-003 状态：待发货；金额：199元。")


class BridgeModel(FakeModel):
    """FakeModel + `bind()`：`memory.compact()` 现在会 `model.bind(max_tokens=SUMMARY_MAX_TOKENS)`，
    没有 bind 的替身会让压缩走进 compactor_error 分支，于是"水位线没推进"看起来像被测机制失效，
    其实是探针的替身缺一个方法（陷阱 #23/#31 的同一个形状在替身上重现）。"""

    def bind(self, *_a, **_k):
        return self


def fresh_saver():
    """One isolated process-level "app": cached services hold a checkpointer, so a case
    that reuses the previous service would silently measure yesterday's graph."""
    cutover.clear_services()
    from langgraph.checkpoint.memory import InMemorySaver
    return InMemorySaver()


def app_with(saver=None, *, model=None):
    """One stand-in "app": the bridge reads model/FAQ/intake off app state at call time."""
    state = SimpleNamespace(
        checkpointer=saver if saver is not None else fresh_saver(),
        faq_bridge=FakeBridge(),
        # 挂载侧新接法要读租户模型配置：给一个假的 http_client，
        # 但探针把 get_tenant_model 整个换掉，所以它一次也不会真的被用到（B0 会验这一条）。
        http_client=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
            json=lambda: {"code": -1, "msg": "probe: never expected to be called"}))),
    )
    return SimpleNamespace(state=state), (model or BridgeModel())


def bridge_patches(model, supervisor, *, faq=None, order_result=ORD_BRIDGE_OK,
                   order_id="ORD-9999", history=()):
    """Everything that must stay patched while the bridge builds and runs the agent/ graph.

    The patch targets are the **source modules** the adapter imports at call time
    (`agent/legacy_bindings.py` does `from app.agent.llm_config import get_tenant_model`
    inside the function), which is why patching here works at all — patching the
    `agent.legacy_bindings` attribute would be too late for a function-local import.
    """
    general = SimpleNamespace(ainvoke=AsyncMock(return_value={
        "messages": [_human_placeholder(), model_answer("我是智能客服，可以帮您查订单和店铺政策。")]}))

    def fake_create_agent(model=None, tools=None, *, name=None, system_prompt="", **kwargs):
        names = {getattr(tool_obj, "name", "") for tool_obj in (tools or [])}
        if name == "supervisor" or "consult_faq" in names:
            # 脚本化 supervisor 必须拿到**真**的 consult_* 工具对象，否则它委派不出去，
            # 整轮就只剩"空草稿"——那是探针自己把链路拆了（旧探针在 create_agent_patch 里做同一件事）。
            supervisor.tools = {tool_obj.name: tool_obj for tool_obj in (tools or [])}
            return supervisor
        # build_order_agent() is the only other create_agent call in agent/agents.py,
        # and it is the only one carrying the two read-only business tools.
        if "lookup_my_order" in names:
            return OrderDelegateTools(tools, order_id=order_id)
        return general

    return [
        patch("app.agent.llm_config.get_tenant_model", AsyncMock(return_value=model)),
        patch("app.agent.llm_config.get_platform_model", AsyncMock(return_value=model)),
        patch("app.agent.faq_agent.faq_agent", AsyncMock(return_value=faq or FaqDelegate())),
        patch("app.agent.intake_agent.intake_agent", AsyncMock(return_value=general)),
        patch("app.agent.faq_agent.recent_messages",
              AsyncMock(return_value=list(history))),
        patch("app.agent.tools.lookup_order", AsyncMock(return_value=order_result)),
        patch("app.agent.tools.list_owned_orders", AsyncMock(return_value=order_result)),
        patch("app.agent.support.agents.create_agent", side_effect=fake_create_agent),
    ]


def _human_placeholder():
    from langchain_core.messages import HumanMessage
    return HumanMessage(content="")


def model_answer(text):
    from langchain_core.messages import AIMessage
    return AIMessage(content=text)


async def bturn(app, model, supervisor, question, *, thread=f"{TENANT}:buyer1",
                order_result=ORD_BRIDGE_OK, faq=None, order_id="ORD-9999", turn_id="",
                history=(), event_sink=None):
    """One turn through the production entry (four_agent → agent.cutover → agent/ graph)."""
    with ExitStack() as stack:
        for ctx in bridge_patches(model, supervisor, faq=faq, order_result=order_result,
                                  order_id=order_id, history=history):
            stack.enter_context(ctx)
        token = trace.bind(trace.new_trace("mounted-bridge", TENANT, thread))
        stack.callback(trace.reset, token)
        extra = {"turn_id": turn_id} if turn_id else {}
        try:
            state = await four_agent.run_turn(app, TENANT, thread, question, **extra)
        except Exception:
            if event_sink is not None:
                event_sink.extend((e.get("stage"), e.get("name"), e.get("attributes"))
                                  for e in trace.current().events)
            raise
        run = trace.current()
        events = [(e.get("stage"), e.get("name"), e.get("attributes")) for e in run.events]
    service = cutover._services[TENANT]
    graph = await service.graph_for(TENANT)
    return graph, state, {"route": getattr(run, "route", ""),
                          "specialist": getattr(run, "specialist", ""), "events": events}


async def persisted(graph, thread):
    return (await graph.aget_state({"configurable": {"thread_id": V2 + thread}})).values


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--gate", action="store_true",
                    help="十条不变式当闸门：任一条红就退出码 1（B2/B11 是缺口的描述，不进闸门）")
    args = ap.parse_args()
    rows = []

    def emit(case, ok, **obs):
        rows.append({"case": case, "ok": bool(ok), **obs})
        print(f"[{'PASS' if ok else 'FINDING'}] {case}: "
              + json.dumps(obs, ensure_ascii=False)[:620])

    # ---- B0: 这一轮究竟是谁在跑（用计数器，不靠读 import） --------------------------------
    calls = {"app_graph_build": 0, "agent_review": 0, "app_review": 0}
    real_review = agent_guards.review
    _app_graph_mod = sys.modules.get("app.multi_agent.graph")
    _app_review = getattr(_app_graph_mod, "review", None) if _app_graph_mod else None

    def counting_review(*a, **k):
        calls["agent_review"] += 1
        return real_review(*a, **k)

    def counting_app_review(*a, **k):
        calls["app_review"] += 1
        return _app_review(*a, **k)

    async def counting_build(*a, **k):
        calls["app_graph_build"] += 1
        return await _app_graph_mod.build_graph(*a, **k)

    patch_ctx = [patch("app.agent.support.guards.review", side_effect=counting_review)]
    if _app_review is not None:
        patch_ctx.append(patch("app.multi_agent.graph.review", side_effect=counting_app_review))
        patch_ctx.append(patch("app.multi_agent.graph.build_graph", side_effect=counting_build))
    with ExitStack() as stack:
        for ctx in patch_ctx:
            stack.enter_context(ctx)
        app, model = app_with()
        _g, s0, _i = await bturn(app, model, Supervisor(compound, "订单待发货；签收后 7 天内可退。"),
                                 POLICY_Q, thread=f"{TENANT}:b0")
    emit("B0_topology",
         "app.agent.support.cutover" in sys.modules and calls["agent_review"] >= 1 and calls["app_review"] == 0,
         entry="app.services.four_agent.run_turn",
         cutover_loaded="app.agent.support.cutover" in sys.modules,
         agent_review_calls=calls["agent_review"], app_review_calls=calls["app_review"],
         app_graph_builds=calls["app_graph_build"],
         outcome=s0.get("outcome"), turn_id_field=bool(s0.get("turn_id")),
         # 新接法返回的形状里有没有旧形状那些字段——下游 chat.py 只读 answer/model_messages，
         # 但 §5e 的账本读 delegations/review_feedback，字段名一错就是"看不见"。
         returned_keys=sorted(s0.keys()))

    # ---- B1: 复合问进生产入口，委派与证据一起落检查点 --------------------------------------
    app, model = app_with()
    graph, state, runinfo = await bturn(app, model, Supervisor(
        compound, "订单 ORD-2024-003 状态：待发货。退货需核验商品与签收时间。"), POLICY_Q,
        thread=f"{TENANT}:b1", order_id="ORD-2024-003", order_result=ORD_OK,
        faq=FaqDelegate(answer="签收后七天内可申请退货。", evidence=("chunk-7d",)))
    vals = await persisted(graph, f"{TENANT}:b1")
    delegs = vals.get("delegations") or []
    codes = [r.get("code") for d in delegs if d["specialist"] == "order"
             for r in (d.get("verified_results") or [])]
    emit("B1_compound_entry",
         [d["specialist"] for d in delegs] and set(d["specialist"] for d in delegs) == {"order", "faq"}
         and "ok" in codes and vals.get("answer") == state["answer"]
         and runinfo["route"] == "multi_agent" and not (state.get("review_issues") or []),
         delegated=[d["specialist"] for d in delegs], order_codes=codes,
         faq_evidence=[[getattr(e, "id", None) or (e.get("id") if isinstance(e, dict) else None)
                        for e in (d.get("evidence") or [])]
                       for d in delegs if d["specialist"] == "faq"],
         delegation_keys=sorted({k for d in delegs for k in d.keys()}),
         order_tool_calls=[d.get("tool_calls") for d in delegs if d["specialist"] == "order"],
         review_issues=state.get("review_issues"), answer_channel_checkpointed=True,
         trace_specialist=runinfo["specialist"], attempts=state.get("attempts"),
         billed_msgs=len(state.get("model_messages") or []),
         ledger_roles=[l.get("roles") for l in (vals.get("ledger") or [])],
         legacy_side_effects={"kb_hits_written": False, "faq_cache_used": "reuse_faq_agent=True"})

    # ---- B2: 不带 turn_id 重放（旧接法里这一条叫 M2，是"两轮两记账"） ----------------------
    app, model = app_with()
    await bturn(app, model, Supervisor(compound, "在运输中，预计周三送达。"), FOLLOW_Q,
                thread=f"{TENANT}:b2")
    _, s2, _ = await bturn(app, model, Supervisor(compound, "在运输中，预计周三送达。"), FOLLOW_Q,
                           thread=f"{TENANT}:b2")
    vals = await persisted(cutover._services[TENANT] and await _graph_for(), f"{TENANT}:b2")
    humans = [m for m in vals.get("messages", []) if m.type == "human"]
    emit("B2_replay_without_turn_id", len(humans) == 1,
         human_messages=len(humans), ledger_turns=len(vals.get("ledger") or []),
         turn_ids=[l.get("turn_id") for l in (vals.get("ledger") or [])],
         replayed_flag=s2.get("_replayed"),
         note="生产调用把 trace_id 当 turn_id 传（chat.py:100 与 :225），所以同一请求内 JSON/SSE 共用一个键、"
              "跨请求重试必然换一个键：§5e-M2 的缺口没有因为换接法而消失，ChatRequest 仍然没有 turn_id 字段")

    # ---- B2b: 带同一个 turn_id 重放 --------------------------------------------------------
    app, model = app_with()
    await bturn(app, model, Supervisor(compound, "在运输中，预计周三送达。"), FOLLOW_Q,
                thread=f"{TENANT}:b2b", turn_id="turn-1")
    _, s2b, _ = await bturn(app, model, Supervisor(compound, "在运输中，预计周三送达。"), FOLLOW_Q,
                            thread=f"{TENANT}:b2b", turn_id="turn-1")
    vals = await persisted(await _graph_for(), f"{TENANT}:b2b")
    humans = [m for m in vals.get("messages", []) if m.type == "human"]
    emit("B2b_replay_with_turn_id", bool(s2b.get("_replayed")) and len(humans) == 1,
         replayed=s2b.get("_replayed"), human_messages=len(humans),
         billed_msgs_first_vs_second=len(s2b.get("model_messages") or []),
         message_ids=[getattr(m, "id", None) for m in vals.get("messages", [])][:6])

    # ---- B3: 旧会话原文被一次性读进来（不是靠 faq_bridge 常驻） ----------------------------
    from langchain_core.messages import AIMessage, HumanMessage
    seeded = [HumanMessage(content="历史提问0"), AIMessage(content="历史回答0"),
              HumanMessage(content="历史提问1"), AIMessage(content="历史回答1")]
    app, model = app_with()
    graph, state, _ = await bturn(app, model, Supervisor(compound, "好的，已为您记录。"),
                                  "再说说上次那件事", thread=f"{TENANT}:b3", history=seeded)
    vals = await persisted(graph, f"{TENANT}:b3")
    texts = [str(m.content) for m in vals.get("messages", [])]
    visible = sum(1 for s in seeded if str(s.content) in " ".join(texts))
    emit("B3_legacy_history_visible", visible == len(seeded),
         legacy_messages=len(seeded), legacy_in_checkpoint=visible,
         checkpoint_messages=len(vals.get("messages", [])))

    # ---- B4: 复核不过 ⇒ 重试有上界，且落在安全话术 ----------------------------------------
    app, model = app_with()
    stubborn = Supervisor(lambda q: ["consult_faq"], UNEVIDENCED)
    graph, state, info = await bturn(app, model, stubborn, "退货运费谁承担？",
                                     thread=f"{TENANT}:b4",
                                     faq=FaqDelegate(answer="签收后七天内可申请退货。", evidence=()))
    vals = await persisted(graph, f"{TENANT}:b4")
    emit("B4_bounded_retry", stubborn.calls <= 3 and UNEVIDENCED not in (state.get("answer") or ""),
         supervisor_calls=stubborn.calls, attempts=state.get("attempts"),
         review_issues=state.get("review_issues"), answer_head=(state.get("answer") or "")[:60],
         outcome=state.get("outcome"))

    # ---- B5: 模型挂掉的那一轮留下什么 ------------------------------------------------------
    app, model = app_with()
    boom = Supervisor(lambda q: ["consult_general"], "", fail=RuntimeError("403 Free quota exhausted"))
    sink: list = []
    raised = None
    info5 = {"events": []}
    try:
        _g5, _s5, info5 = await bturn(app, model, boom, "ORD-2024-003 到哪了？",
                                      thread=f"{TENANT}:b5", event_sink=sink)
    except Exception as exc:
        raised = repr(exc)[:80]
    graph = await _graph_for()
    vals = await persisted(graph, f"{TENANT}:b5")
    seen_events = sink + list(info5.get("events") or [])
    err_stage = [e for e in seen_events if e[0] == "error"]
    failed_attr = [e for e in seen_events
                   if (e[2] or {}).get("status") == "failed" or (e[2] or {}).get("error")]
    humans = [m for m in vals.get("messages", []) if m.type == "human"]
    emit("B5_failure_turn_persisted_and_explained",
         len(humans) <= 1 and bool(failed_attr or err_stage) and bool(vals.get("last_error")),
         raised=raised, human_messages=len(humans),
         error_stage_events=err_stage[:3], failed_attribute_events=failed_attr[:3],
         answer_after_failure=(vals.get("answer") or "")[:60], last_error=bool(vals.get("last_error")))

    # ---- B6: 12 轮之后水位线推进，且原文一条不删 ------------------------------------------
    app, model = app_with()
    for i in range(12):
        await bturn(app, model, Supervisor(lambda q: ["consult_general"], f"第{i}轮答复"),
                    f"第{i}个问题", thread=f"{TENANT}:b6")
    graph = await _graph_for()
    mem = await cutover.memory_state(app, TENANT, f"{TENANT}:b6")
    vals = await persisted(graph, f"{TENANT}:b6")
    msgs = vals.get("messages", [])
    humans = [m for m in msgs if m.type == "human"]
    emit("B6_watermark_advances_without_deleting",
         mem.get("watermark", 0) > 0 and len(humans) == 12,
         memory_state=mem, checkpoint_messages=len(msgs), human_messages=len(humans),
         summarized=mem.get("summarized"), verbatim=mem.get("verbatim"))

    # ---- B7: 本轮没核验过的单号被断言 ⇒ hard 拦 --------------------------------------------
    app, model = app_with()
    graph, state, _ = await bturn(app, model, Supervisor(lambda q: ["consult_faq"], CLAIMING),
                                  "我朋友那单 ORD-9999 到哪了？", thread=f"{TENANT}:b7",
                                  faq=FaqDelegate(answer="签收后七天内可申请退货。",
                                                  evidence=("chunk-7d",)))
    emit("B7_unverified_order_claim_blocked", CLAIMING not in (state.get("answer") or ""),
         review_issues=state.get("review_issues"), outcome=state.get("outcome"),
         answer_head=(state.get("answer") or "")[:80])

    # ---- B9: 同进程同一线程 4 并发 --------------------------------------------------------
    app, model = app_with()
    outs = await asyncio.gather(*[bturn(app, model, Supervisor(lambda q: ["consult_general"], f"并发{i}"),
                                        f"并发问题{i}", thread=f"{TENANT}:b9") for i in range(4)])
    vals = await persisted(outs[0][0], f"{TENANT}:b9")
    humans = [m for m in vals.get("messages", []) if m.type == "human"]
    ledgers = [l.get("turn_id") for l in (vals.get("ledger") or [])]
    emit("B9_same_process_concurrency", len(humans) == 4,
         human_messages=len(humans), ledger_turns=len(ledgers),
         distinct_ledger_turns=len(set(ledgers)))

    # ---- B10: 挂载入口的锁粒度——生产入口一 tenant 只有一个 service ----------------------
    from langgraph.checkpoint.memory import InMemorySaver
    shared = InMemorySaver()
    app10, model10 = app_with(shared)
    await bturn(app10, model10, Supervisor(lambda q: ["consult_general"], "热身"),
                "先建立 service", thread=f"{TENANT}:b10")
    service_id = id(cutover._services[TENANT])
    # 第二个 app 带着**另一个** saver 进来：缓存按 tenant，不按 app，所以它会被忽略。
    app10b, model10b = app_with(InMemorySaver())
    _g10b, _s10b, _i10b = await bturn(app10b, model10b,
                                      Supervisor(lambda q: ["consult_general"], "第二个 app"),
                                      "第二个 app 的问题", thread=f"{TENANT}:b10")
    same_service = id(cutover._services[TENANT]) == service_id
    graph10 = await _graph_for()
    outs = await asyncio.gather(*[bturn(app10, model10,
                                        Supervisor(lambda q: ["consult_general"], f"并发{i}"),
                                        f"并发问题{i}", thread=f"{TENANT}:b10c") for i in range(3)])
    vals = await persisted(graph10, f"{TENANT}:b10c")
    humans = [m for m in vals.get("messages", []) if m.type == "human"]
    emit("B10_entry_lock_granularity", same_service and len(humans) == 3,
         one_service_per_tenant=same_service, second_app_saver_ignored=True,
         concurrent_turns=3, human_messages=len(humans), lost_turns=max(0, 3 - len(humans)),
         thread_guard=type(getattr(cutover._services[TENANT], "_thread_guard", None)).__name__
         if getattr(cutover._services[TENANT], "_thread_guard", None) else None,
         note="锁在 SupportService._thread_locks 上，而挂载入口按 tenant 只缓存一个 service ⇒ "
              "同进程内并发轮次共用同一把锁，M10 那种「两个独立编译的图写同一线程」在生产入口上出不来；"
              "代价是 app.state.checkpointer 只在第一次建 service 时生效（第二个 app 的 saver 被忽略）")

    # ---- B11: 真正两个实例（=第二个 worker / 第二个副本）并发写同一线程 ---------------------
    from langgraph.checkpoint.memory import InMemorySaver

    from app.agent.support.fakes import offline_deps
    from app.agent.support.service import SupportService
    shared2 = InMemorySaver()

    def new_instance():
        deps, _m, _ev, _cap = offline_deps()

        async def provide(_tenant):
            return deps

        return SupportService(provide, checkpointer=shared2)

    svcA, svcB = new_instance(), new_instance()
    async def burn(svc, tag, n):
        for i in range(n):
            await svc.run_turn(TENANT, f"{TENANT}:b11", f"{tag} 第{i}问", turn_id=f"{tag}{i}")

    await asyncio.gather(burn(svcA, "A", 3), burn(svcB, "B", 3))
    graphA = await svcA.graph_for(TENANT)
    vals = await persisted(graphA, f"{TENANT}:b11")
    humans = [m for m in vals.get("messages", []) if m.type == "human"]
    # 判据不能写成恒真：这一项的"通过"意思是"两个独立实例确实会互相覆盖"，
    # 如果 0 丢轮，就必须把它读成"本探针的调度没能复现 M10"，而不是"换接法把并发问题修好了"。
    emit("B11_two_instances_same_thread_loses", len(humans) < 6,
         expected_turns=6, human_messages=len(humans), lost_turns=max(0, 6 - len(humans)),
         lock_scope="per SupportService instance",
         note="这就是 §5e-M10 的形状：两个各自编译的图共用一个 checkpointer 时后写覆盖。"
              "换接法没有改变它（锁的粒度还是 service 实例）；能治它的是 agent/thread_guard.py 的 "
              "PG advisory 锁，挂载入口通过 FOUR_AGENT_GRAPH 之外再加 SUPPORT_V2_THREAD_LOCK + POSTGRES_DSN 就会接上"
              "（§4b 已实测加锁六批零丢轮）")

    out = {"generated_at": datetime.now(timezone.utc).isoformat(),
           "entry": "app.services.four_agent.run_turn -> agent.cutover.run_turn -> agent/graph.py",
           "total": len(rows), "pass": sum(1 for r in rows if r["ok"]),
           "findings": sum(1 for r in rows if not r["ok"]), "rows": rows}
    print(json.dumps({k: out[k] for k in ("generated_at", "entry", "total", "pass", "findings")},
                     ensure_ascii=False))
    if args.out:
        Path(args.out).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                                  encoding="utf-8")
        Path(args.out + ".summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")
    if args.gate:
        # 闸门只押"换接法之后仍然必须成立"的那十条：委派是真的、记忆在推进、安全规则开火、
        # 同进程并发不丢轮、失败轮有解释、旧会话读得进来。
        # B2（无 turn_id 的重放仍算两轮）与 B11（两个实例写同一线程会覆盖）是**缺口的描述**：
        # 它们今天这样就是预期，把极性写进闸门会在"问题被修好"那天误报（陷阱 #21）。
        invariant = {"B0_topology", "B1_compound_entry", "B2b_replay_with_turn_id",
                     "B3_legacy_history_visible", "B4_bounded_retry",
                     "B5_failure_turn_persisted_and_explained",
                     "B6_watermark_advances_without_deleting",
                     "B7_unverified_order_claim_blocked", "B9_same_process_concurrency",
                     "B10_entry_lock_granularity"}
        bad = [r["case"] for r in rows if r["case"] in invariant and not r["ok"]]
        print(f"GATE {'FAIL ' + ','.join(bad) if bad else 'PASS（10 条不变式全绿）'}")
        return 1 if bad else 0
    return 0


async def _graph_for():
    """The graph the cached service is holding (it is keyed by tenant, not by app)."""
    return await cutover._services[TENANT].graph_for(TENANT)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
