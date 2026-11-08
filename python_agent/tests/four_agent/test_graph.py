# 中文文案必然包含全角标点
"""Acceptance for phase 2: real delegation, merging, bounds and review."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.support.deps import SupportDeps
from app.agent.support.fakes import USAGE, FakeSpecialist, ScriptedSupportModel, ai_with_tools, tool_call
from app.agent.support.fakes import offline_deps as make_deps
from app.agent.support.graph import TURN_STEP_BUDGET, build_support_graph

pytestmark = pytest.mark.asyncio


async def ask(deps: SupportDeps, question: str, *, turn: str = "t1",
              thread: str | None = None, **graph_kwargs: Any) -> dict[str, Any]:
    """Run one turn through the real graph with a scripted model.

    The step budget is imported, not copy-pasted: a test that runs on a different budget than
    ``SupportService`` does would pass while production fails (that is exactly the defect
    ``test_heavy_delegation_across_retries_…`` was written for).
    """
    graph = await build_support_graph(deps, **{"engine": "tools", **graph_kwargs})
    config = {"configurable": {"thread_id": thread or f"probe-{turn}"},
              "recursion_limit": TURN_STEP_BUDGET}
    return await graph.ainvoke(
        {"messages": [HumanMessage(content=question, id=f"{turn}:u")], "turn_id": turn}, config)


async def test_composite_question_delegates_both_specialists_and_merges() -> None:
    deps, _model, events, _ = make_deps(model=ScriptedSupportModel(supervisor_script=[
        ai_with_tools("先并行核实", [
            tool_call("consult_faq", {"question": "退货运费谁承担"}, "c1"),
            tool_call("consult_order", {"question": "ORD-1001 到哪了"}, "c2")]),
        AIMessage(content="订单 ORD-1001 已发货；退货请在签收后 7 天内申请，运费自理。",
                  usage_metadata=USAGE)]))
    state = await ask(deps, "我的订单 ORD-1001 到哪了？另外退货运费谁承担？")

    assert {item["specialist"] for item in state["delegations"]} == {"faq", "order"}
    assert state["outcome"] == "answered"
    assert "7 天" in state["answer"] and "ORD-1001" in state["answer"]

    # The trace must show which agents actually ran, not merely that text appeared.
    delegate = [e for e in events if e["stage"] == "delegate"]
    assert {"faq", "order"} <= {e["name"] for e in delegate}
    assert [e for e in delegate if e["name"] == "supervisor"]
    ledger = state["ledger"][0]
    assert set(ledger["roles"]) == {"faq", "order"}
    assert ledger["evidence_ids"] == ["chunk-7d"]
    assert ledger["review_issues"] == []
    assert state["usage"]["t1"]["delegate_calls"] == 2
    # The order fact really came out of the read-only business tool.
    order_finding = next(item for item in state["delegations"] if item["specialist"] == "order")
    assert [r["code"] for r in order_finding["verified_results"]] == ["ok"]
    assert "lookup_my_order" in order_finding["tool_calls"]


async def test_unsupported_transaction_claim_is_blocked() -> None:
    deps, _model, _events, _ = make_deps(model=ScriptedSupportModel(supervisor_script=[
        ai_with_tools("核实中", [tool_call("consult_faq", {"question": "退款多久到账"}, "c1")]),
        AIMessage(content="已为您退款 7 元，请留意到账。", usage_metadata=USAGE)]))
    state = await ask(deps, "退款什么时候到账？", turn="t-claim", max_passes=1)
    assert state["review_hard"] is True
    assert state["outcome"] == "review_blocked"
    assert "已为您退款" not in state["answer"]
    assert "转人工" in state["answer"]


async def test_policy_number_without_this_turn_evidence_is_blocked() -> None:
    deps, _model, _events, _ = make_deps(model=ScriptedSupportModel(supervisor_script=[
        AIMessage(content="本店退货期为 15 天，逾期不予受理。", usage_metadata=USAGE)]))
    state = await ask(deps, "退货期限是多久？", turn="t-num", max_passes=1)
    assert state["review_hard"] is True
    assert "15 天" not in state["answer"]
    assert any("证据" in issue for issue in state["review_issues"])


async def test_order_id_not_verified_this_turn_is_blocked() -> None:
    deps, _model, _events, _ = make_deps(model=ScriptedSupportModel(supervisor_script=[
        ai_with_tools("核实中", [tool_call("consult_order", {"question": "查订单"}, "c1")]),
        AIMessage(content="订单 ORD-9999 状态：已签收。", usage_metadata=USAGE)]))
    state = await ask(deps, "帮我看看 ORD-9999 的状态。", turn="t-cross", max_passes=1)
    assert state["review_hard"] is True
    assert "ORD-9999" not in state["answer"]
    assert "无法可靠核验" in state["answer"]


async def test_claim_contradicted_by_verified_order_state_is_blocked() -> None:
    deps, _model, _events, _ = make_deps(
        orders={"ORD-3003": {"code": "ok", "text": "订单 ORD-3003 状态：未发货", "order_id": "ORD-3003"}},
        model=ScriptedSupportModel(supervisor_script=[
            ai_with_tools("核实中", [tool_call("consult_order", {"question": "ORD-3003 到哪了"}, "c1")]),
            AIMessage(content="订单 ORD-3003 已发货，正在派送。", usage_metadata=USAGE)]))
    state = await ask(deps, "ORD-3003 到哪了？", turn="t-conflict", max_passes=1)
    assert state["review_hard"] is True
    assert any("冲突" in issue for issue in state["review_issues"])
    assert "已发货" not in state["answer"]


async def test_denied_order_access_is_reported_honestly() -> None:
    """A forbidden lookup must not be turned into an order fact."""
    deps, _model, _events, _ = make_deps(
        orders={"ORD-2002": {"code": "forbidden",
                             "text": "订单 ORD-2002 不属于当前登录账号，无法核验状态。",
                             "order_id": "ORD-2002"}},
        model=ScriptedSupportModel(
            supervisor_script=[
                ai_with_tools("核实中", [tool_call("consult_order", {"question": "ORD-2002 状态"}, "c1")]),
                AIMessage(content="这笔订单不属于当前账号，无法确认状态；请登录后再查。",
                          usage_metadata=USAGE)],
            order_answer="工具返回：订单不属于当前登录账号，无法核验状态。"))
    state = await ask(deps, "ORD-2002 现在什么状态？", turn="t-forbidden")
    assert state["review_hard"] is False
    assert state["outcome"] == "answered"
    assert "无法确认状态" in state["answer"]


async def test_soft_finding_retries_once_then_answers() -> None:
    deps, model, _events, _ = make_deps(model=ScriptedSupportModel(supervisor_script=[
        AIMessage(content="本店支持顺丰上门取件退回。", usage_metadata=USAGE),
        ai_with_tools("补充核实", [tool_call("consult_faq", {"question": "退货是否支持上门取件"}, "c1")]),
        AIMessage(content="退货请在签收后 7 天内申请，运费自担。", usage_metadata=USAGE)]))
    state = await ask(deps, "退货可以上门取件吗？", turn="t-retry")
    # 3 model calls over 2 graph passes: pass 1 answers directly, pass 2 delegates
    # (one call to emit the tool call, one to write up its result).
    assert model.recorded.count("supervisor") == 3
    assert state["attempts"] == 2
    assert state["outcome"] == "answered"
    assert state["ledger"][0]["review_issues"]


async def test_review_retry_loop_is_bounded() -> None:
    stubborn = AIMessage(content="本店支持顺丰上门取件退回。", usage_metadata=USAGE)
    deps, model, events, _ = make_deps(
        model=ScriptedSupportModel(supervisor_script=[stubborn] * 6))
    state = await ask(deps, "退货可以上门取件吗？", turn="t-bound", max_passes=3)
    assert model.recorded.count("supervisor") == 3
    assert state["attempts"] == 3
    assert state["outcome"] == "answered_unchecked"
    assert state["review_hard"] is False
    assert len([e for e in events if e["stage"] == "review"]) == 3


async def test_specialist_failure_is_recorded_not_silently_dropped() -> None:
    deps, _model, _events, _ = make_deps(
        general=FakeSpecialist(answer="", raises=True),
        model=ScriptedSupportModel(supervisor_script=[
            ai_with_tools("先问候", [tool_call("consult_general", {"question": "你好"}, "c1")]),
            AIMessage(content="我可以帮您查本人订单，或解释店铺政策。", usage_metadata=USAGE)]))
    state = await ask(deps, "你好", turn="t-fail")
    findings = {item["specialist"]: item for item in state["delegations"]}
    assert findings["general"]["failed"] is True
    assert state["answer"]


async def test_supervisor_model_failure_degrades_instead_of_crashing() -> None:
    """A model outage must not propagate out of the graph, and never yields blank text."""
    deps, _model, _events, _ = make_deps(model=ScriptedSupportModel(fail_supervisor=True))
    state = await ask(deps, "退货期限是多久？", turn="t-crash", max_passes=1)
    assert state["outcome"] == "no_result"
    assert state["answer"].strip()
    assert state["delegations"] == []
    # The cause has to survive the degradation, or nobody can debug it.
    assert "supervisor model unavailable" in state["last_error"]


async def test_graph_shape_is_visible_and_replayable() -> None:
    deps, _model, _events, _ = make_deps()
    # 这个文件整体是遗留 tools 引擎的回归，形状断言也必须显式钉住引擎：
    # 跟随默认值的话，默认值一换这条用例就会去 check 一张根本不存在的 supervisor 节点。
    graph = await build_support_graph(deps, engine="tools")
    nodes = set(graph.get_graph().nodes)
    assert {"open_turn", "supervisor", "review", "finalize", "compact"} <= nodes
    # The Reviewer is a rule node, never a fifth agent: only three delegates exist.
    assert not {"faq_agent", "order_agent", "general_agent"} & nodes
    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    assert ("review", "supervisor") in edges and ("review", "finalize") in edges


async def test_heavy_delegation_across_every_retry_still_answers() -> None:
    """最贵的合法形状：三个 pass，每个都先委派 FAQ、再委派 General、最后收口。

    这条是被 ``custom`` 臂（报告 §8e）逼出来的：真实 HTTP 模型往返里这一形状每次都调用成功，
    整轮却因为 ``recursion_limit`` 耗尽而 ``no_result``。LangGraph 把**子图的步数计入父图剩余
    预算**，所以只按外层节点数定的预算是错的——循环有界（attempts 封顶 3）不等于预算够用。
    这里关掉压缩，把变量只剩"步数预算"这一件事。
    """
    script = []
    for index in range(3):  # DEFAULT_MAX_PASSES 个 pass
        script += [
            ai_with_tools("先查政策", [tool_call("consult_faq", {"question": "退货期限"}, f"f{index}")]),
            ai_with_tools("再问能力", [tool_call("consult_general", {"question": "还有什么办法"}, f"g{index}")]),
            AIMessage(content="签收后 30 天内可申请退货。", usage_metadata=USAGE),  # 30 天没有当轮证据
        ]
    deps, _model, _events, _ = make_deps(model=ScriptedSupportModel(supervisor_script=script))
    state = await ask(deps, "退货期限多久？", turn="t-budget", compact_trigger=999)

    assert state["attempts"] == 3                       # 重试封顶仍然成立
    assert state["outcome"] != "no_result", state["last_error"]
    assert state["last_error"] == "", state["last_error"]
    assert {"faq", "general"} <= {item["specialist"] for item in state["delegations"]}
    # 违规数值没有证据，所以放行的是安全话术，不是那句「30 天」
    assert "30 天内可申请退货" not in state["answer"]
    assert any("证据" in issue for issue in state["review_issues"])
    assert state["usage"]["t-budget"]["model_calls"] >= 9   # 真的跑满了九个模型调用
