# 中文文案必然包含全角标点
"""答复形状：钉住「怎么查询账单信息？」那类防御性回答。

线上实例的四个毛病分别落在四处：判定式结论（COMPOSE_PROMPT）、把专家自述当事实
（compose_material）、无依据时仍调模型猜菜单路径（graph.compose 短路）、内部过程词
与客套尾句被放行（answer_gaps）。这里逐个钉住，并留对照用例证明判定型问题的收敛
语义没被改掉。
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.support import guards
from app.agent.support.agents import compose_material
from app.agent.support.fakes import (
    USAGE,
    FakeSpecialist,
    ScriptedSupportModel,
    ai_with_tools,
    offline_deps,
    tool_call,
)
from app.agent.support.graph import TURN_STEP_BUDGET, build_support_graph
from app.agent.support.state import Delegation

QUESTION = "怎么查询账单信息？"
DEFENSIVE = ("关于账单查询方式：目前无法确认。本轮店铺资料中没有查到账单查询入口"
             "和具体步骤的说明，我无法给您准确的操作指引。建议您登录商城后，"
             "在个人中心或订单相关页面查看是否有账单/交易记录选项。"
             "如有其他问题（如订单、退换货等），我很乐意继续为您解答。")


def finding(role: str, answer: str, *, evidence: list[dict] | None = None) -> Delegation:
    return {"turn_id": "t1", "specialist": role, "answer": answer,
            "tool_calls": [], "evidence": evidence or [], "verified_results": [],
            "used_search": False, "failed": False}


def route_faq(question: str) -> AIMessage:
    return ai_with_tools("", [tool_call("consult_faq", {"question": question}, "c1")])


async def ask(deps: Any, question: str, *, turn: str) -> dict[str, Any]:
    graph = await build_support_graph(deps)
    config = {"configurable": {"thread_id": f"probe-{turn}"},
              "recursion_limit": TURN_STEP_BUDGET}
    return await graph.ainvoke(
        {"messages": [HumanMessage(content=question, id=f"{turn}:u")], "turn_id": turn}, config)


def test_howto_questions_are_recognised() -> None:
    for text in ("怎么查询账单信息？", "在哪查交易记录", "退款进度怎么查", "发票下载入口"):
        assert guards.is_howto_question(text), text
    for text in ("未拆封能退吗？", "ORD-1001 到哪了？", "签收后 7 天内可以退货吗"):
        assert not guards.is_howto_question(text), text


def test_defensive_answer_is_rejected_by_the_reviewer() -> None:
    gaps = guards.answer_gaps(QUESTION, DEFENSIVE, [])
    assert any("内部" in gap for gap in gaps), gaps
    assert any("客套" in gap for gap in gaps), gaps
    assert any("目前无法确认" in gap for gap in gaps), gaps


def test_verdict_questions_keep_their_original_shape_rules() -> None:
    # 对照：判定型问题的短答复不受「操作指引」这条新规则影响。
    assert guards.answer_gaps("未拆封能退吗？", "目前无法确认能否退款。", []) == []


def test_compose_material_never_copies_an_unevidenced_specialist_claim() -> None:
    material = compose_material([finding("faq", "本轮店铺资料中没有查到账单查询入口")])
    assert "答复：" not in material
    assert "【本轮无可用依据】" in material
    assert "没有查到账单查询入口" not in material
    # general 的澄清本来就没有 evidence，不能被一起挡掉。
    general = compose_material([finding("general", "请问您是想查账单还是订单？")])
    assert "答复：请问您是想查账单还是订单？" in general


def test_uncovered_reply_gives_a_next_step_without_inventing_a_path() -> None:
    reply = guards.uncovered_reply(QUESTION)
    assert "转人工" in reply
    assert not any(term in reply for term in guards.INTERNAL_TERMS), reply
    for invented in ("个人中心", "订单相关页面", "登录商城后"):
        assert invented not in reply
    # 自己生成的安全话术不能被 Reviewer 退回重写，否则会空转到预算用尽。
    assert guards.answer_gaps(QUESTION, reply, []) == []


@pytest.mark.asyncio
async def test_howto_without_evidence_short_circuits_compose() -> None:
    faq = FakeSpecialist(answer="本轮店铺资料中没有查到账单查询入口和具体步骤的说明",
                         tools=(), evidence=[])
    deps, model, _events, _ = offline_deps(
        model=ScriptedSupportModel(supervisor_script=[route_faq("账单查询入口")]), faq=faq)
    state = await ask(deps, QUESTION, turn="bill-uncovered")

    assert model.recorded.count("compose") == 0, model.recorded
    assert state["answer"] == guards.uncovered_reply(QUESTION), state["review_issues"]
    assert "目前无法确认" not in state["answer"]
    assert not any(term in state["answer"] for term in guards.INTERNAL_TERMS)
    assert "个人中心" not in state["answer"]


@pytest.mark.asyncio
async def test_howto_with_evidence_still_goes_through_compose() -> None:
    answer = "进入账单页面后选择时间范围，即可查看对应的交易记录。"
    deps, model, _events, _ = offline_deps(model=ScriptedSupportModel(
        supervisor_script=[route_faq("账单查询入口")],
        compose_script=[AIMessage(content=answer, usage_metadata=USAGE)]))
    state = await ask(deps, QUESTION, turn="bill-covered")

    assert model.recorded.count("compose") == 1, model.recorded
    assert "【问题类型】操作指引" in model.compose_prompts[0], model.compose_prompts
    assert state["outcome"] == "answered", state["review_issues"]
    assert state["answer"] == answer


# ---------------------------------------------------------------------------
# 「TB202410180001 可以退换吗？」：条件尚未满足 ≠ 信息不足
# ---------------------------------------------------------------------------

RETURN_SWAP_QUESTION = "TB202410180001可以退换吗？"
DEFENSIVE_VERDICT = ("能否退换：目前无法确认。退货时限需从签收后起算，"
                     "因此现在还不能判断是否符合退货条件。\n"
                     "依据：签收后 7 天内可申请退货，商品需保持完好、不影响二次销售。\n"
                     "等您收到商品后，如需人工协助请回复「转人工」。")
CONDITIONAL_VERDICT = ("现在还不可以申请：7 天时限从签收当天起算。"
                       "签收后 7 天内、商品吊牌完整且不影响二次销售即可申请退货；"
                       "换尺码或颜色还需目标规格有库存，质量问题换货由店铺承担运费。")


def return_swap_findings(*, faq_failed: bool = False) -> list[Delegation]:
    return [
        {"turn_id": "t1", "specialist": "faq", "answer": "签收后 7 天内可申请退货",
         "tool_calls": [],
         "evidence": [] if faq_failed else [{"id": "chunk-7d", "title": "退换货",
                                             "text": "签收后 7 天内可申请退换货"}],
         "verified_results": [], "used_search": not faq_failed, "failed": faq_failed},
        {"turn_id": "t1", "specialist": "order", "answer": "已发货，尚未签收",
         "tool_calls": [], "evidence": [],
         "verified_results": [{"code": "ok", "text": "订单已发货，尚未签收"}],
         "used_search": False, "failed": False},
    ]


def test_defensive_verdict_no_longer_slips_past_the_reviewer() -> None:
    """线上那条答复此前被三重放行：NON_ASSERTIVE 早退 + CONCLUSION 认「无法确认」
    + _ASK_ITEMS 认不出「退换」是两件事。"""
    assert not guards.is_assertive(DEFENSIVE_VERDICT)      # 早退确实发生过
    assert guards.CONCLUSION.search(DEFENSIVE_VERDICT)     # 「无法确认」曾被当合法结论
    assert not [k for k, p in guards._ASK_ITEMS.items() if p.search(RETURN_SWAP_QUESTION)]
    gaps = guards.answer_gaps(RETURN_SWAP_QUESTION, DEFENSIVE_VERDICT,
                              return_swap_findings())
    assert any("目前无法确认" in gap for gap in gaps), gaps
    assert any("换货" in gap for gap in gaps), gaps
    assert guards.is_policy_question(RETURN_SWAP_QUESTION)


def test_conditional_verdict_and_clarification_are_not_sent_back() -> None:
    """对照：条件式结论、真缺政策依据、反问澄清都不该被推去重答。"""
    findings = return_swap_findings()
    assert guards.answer_gaps(RETURN_SWAP_QUESTION, CONDITIONAL_VERDICT, findings) == []
    assert guards.answer_gaps(RETURN_SWAP_QUESTION, "请问您是想退货还是换货？", findings) == []
    failed = return_swap_findings(faq_failed=True)
    assert not any("目前无法确认" in gap
                   for gap in guards.answer_gaps("ORD-2024-003这个可以退吗？",
                                                 "目前无法确认能否退款。", failed))


def test_compose_prompt_has_a_fourth_verdict_for_unmet_conditions() -> None:
    from app.agent.support.agents import COMPOSE_PROMPT
    assert "目前还不能申请" in COMPOSE_PROMPT
    assert "「退换」是两件事" in COMPOSE_PROMPT
    assert "不得拿「目前无法确认」代替" in COMPOSE_PROMPT


@pytest.mark.asyncio
async def test_defensive_verdict_is_rewritten_into_a_conditional_answer() -> None:
    """图级：第一稿被 Reviewer 退回，第二稿按已知条件给结论，客户能看到「换货」。"""
    faq = FakeSpecialist(answer="签收后 7 天内可申请退换货；换货需目标规格有库存",
                         tools=("search_kb",),
                         evidence=[{"id": "chunk-7d", "title": "退换货",
                                    "text": "签收后 7 天内可申请退换货"}])
    # 不带订单号：这条用例只验证结论档位，带订单号会走既有的 Order 补派链路，
    # 那是 test_node_engine 里另有一组用例覆盖的复合场景。
    question = "东西还没收到，可以退换吗？"
    deps, model, _events, _ = offline_deps(
        model=ScriptedSupportModel(
            supervisor_script=[route_faq("未签收时能否退换的政策条件")],
            compose_script=[AIMessage(content=DEFENSIVE_VERDICT, usage_metadata=USAGE),
                            AIMessage(content=CONDITIONAL_VERDICT, usage_metadata=USAGE)]),
        faq=faq)
    state = await ask(deps, question, turn="return-swap")

    assert model.recorded.count("compose") == 2, model.recorded
    assert state["outcome"] == "answered", state["review_issues"]
    assert "目前无法确认" not in state["answer"], state["answer"]
    assert "换货" in state["answer"] or "换尺码" in state["answer"], state["answer"]
    assert "签收后 7 天内" in state["answer"], state["answer"]
