# 中文文案必然包含全角标点
"""Unit tests for the Reviewer rules: each rule must be nameable and falsifiable."""

from __future__ import annotations

from app.agent.support import guards
from app.agent.support.state import Delegation


def finding(specialist: str, *, answer: str = "", evidence=(), verified=(),
            used_search: bool = False, failed: bool = False) -> Delegation:
    return {"turn_id": "t1", "specialist": specialist, "answer": answer, "tool_calls": [],
            "evidence": list(evidence), "verified_results": list(verified),
            "used_search": used_search, "failed": failed}


EVIDENCE_7D = [{"id": "c1", "title": "退货政策", "text": "签收后 7 天内可申请退货，运费自理"}]
OK_ORDER = [{"code": "ok", "text": "订单 ORD-1001 状态：已发货；金额：199.00 元",
             "order_id": "ORD-1001"}]


def test_customer_reply_avoids_internal_terms_and_unverified_cancel_button() -> None:
    order = {"code": "ok", "text": "订单 ORD-2024-003 状态：待发货；商品：数据分析课程。"}
    draft = ("订单 ORD-2024-003 当前状态：待发货。店铺知识库中没有明确政策。\n建议您可以：\n"
             "1. 在订单详情页查看是否有\"取消订单\"入口；\n"
             "2. 联系人工客服核实。")
    answer = guards.polish_customer_reply(
        draft, "ORD-2024-003 未发货可以退货吗？",
        [finding("order", verified=[order])])
    assert "知识库" not in answer
    assert "取消订单\"入口" not in answer
    assert "2." not in answer
    assert "课程是否已开通或使用" in answer


def test_unconfirmed_handoff_and_internal_process_are_rejected() -> None:
    order = {"code": "ok", "text": "订单 ORD-2024-003 状态：待发货；商品：数据分析课程。"}
    draft = ("订单 ORD-2024-003 待发货，但目前无法确认能否退款。"
             "政策核验这一路没有返回结果。您的退款请求会由人工客服跟进处理，"
             "稍后会有工单提示。")
    hard, _ = guards.review("ORD-2024-003 这个可以退吗？", draft,
                            [finding("order", verified=[order])])
    assert guards.INTERNAL_PROCESS_ISSUE in hard
    assert guards.UNCONFIRMED_HANDOFF_ISSUE in hard
    assert guards.redraftable_hard(hard)


def test_optional_handoff_guidance_is_not_a_ticket_promise() -> None:
    answer = "目前无法确认能否退款。如需人工协助，可以回复「转人工」。"
    assert not guards.UNCONFIRMED_HANDOFF.search(answer)
    assert not guards.INTERNAL_PROCESS.search(answer)


def test_hard_fabricated_transaction() -> None:
    hard, _soft = guards.review("退款多久到账？", "已为您退款 199.00 元。",
                                [finding("faq", answer="退款一般 3 个工作日到账。",
                                         evidence=EVIDENCE_7D, used_search=True)])
    assert any("未被授权" in issue for issue in hard)


def test_hard_payment_can_never_be_evidenced() -> None:
    hard, _ = guards.review("支付方式有哪些？", "已经为您支付成功。", [])
    assert hard and "支付" in hard[0]


def test_verified_ship_state_is_not_treated_as_a_fabrication() -> None:
    hard, soft = guards.review("ORD-1001 到哪了？", "订单 ORD-1001 已发货，金额 199.00 元。",
                               [finding("order", answer="订单 ORD-1001 已发货",
                                         verified=[OK_ORDER[0]])])
    assert hard == [] and soft == []


def test_hard_unit_mismatch_is_not_covered_by_another_number() -> None:
    """``7 天`` in the evidence must never license a ``7 元`` shipping fee."""
    hard, _ = guards.review("退货运费多少？", "退货运费 7 元。",
                            [finding("faq", answer="签收后 7 天内可申请退货。",
                                     evidence=EVIDENCE_7D, used_search=True)])
    assert any("证据" in issue for issue in hard)


def test_verified_amount_with_currency_unit_supports_normalized_decimal() -> None:
    order = {"code": "ok", "text": "订单 TB202410180001 状态：已发货；金额：159.0 元"}
    hard, _ = guards.review("TB202410180001订单状态", "TB202410180001 已发货；金额：159.00 元。",
                            [finding("order", answer=order["text"], verified=[order])])
    assert not any("数值" in issue for issue in hard), hard
    hard, _ = guards.review("TB202410180001订单状态", "TB202410180001 已发货；金额：199 元。",
                            [finding("order", answer=order["text"], verified=[order])])
    assert any("数值" in issue for issue in hard), hard


def test_hard_foreign_order_reference() -> None:
    hard, _ = guards.review("ORD-2222 的状态？", "订单 ORD-2222 状态：已发货。",
                            [finding("order", answer="查到 1 笔", verified=[OK_ORDER[0]])])
    assert any("订单号" in issue for issue in hard)


def test_customer_may_only_restate_an_order_id_from_this_thread() -> None:
    question = "我最早说的那个订单号是什么？"
    draft = "您先前提供的订单号是 ORD-8888；尚未核验订单归属。"
    hard, soft = guards.review(question, draft, [], customer_order_ids={"ORD-8888"})
    assert hard == soft == []
    hard, _ = guards.review(question, draft, [], customer_order_ids=set())
    assert any("订单号" in issue for issue in hard)
    hard, _ = guards.review(question, "您的订单 ORD-8888 已发货；尚未核验归属。", [],
                            customer_order_ids={"ORD-8888"})
    assert hard
    hard, _ = guards.review(question, "您的订单号是 ORD-8888。", [],
                            customer_order_ids={"ORD-8888"})
    assert any("订单号" in issue for issue in hard)
    hard, _ = guards.review(question,
                            "您先前提供的订单号是 ORD-8888；尚未核验归属，但正在配送。", [],
                            customer_order_ids={"ORD-8888"})
    assert any("订单号" in issue for issue in hard)


def test_full_width_order_id_is_still_a_foreign_reference() -> None:
    """全角单号绕不过跨客户规则。

    「ＴＢ１２３４５６７」经 NFKC 归一后就是 TB1234567。不归一化时，草稿侧与已核验侧
    同时看不见它，于是这条主张只在"本轮没有委派"时才被别的规则顺手拦住——一旦订单
    事实已核验（「已发货」在证据里），"声称已执行发货"会被 verified 兜住而放行，
    跨客户单号就漏了。§5f 的 backed 一遍正是这样发现的。
    """
    hard, _ = guards.review("TB1234567 到哪了？", "ＴＢ１２３４５６７ 已发货。",
                            [finding("order", answer="订单 ORD-1001 已发货",
                                     verified=[OK_ORDER[0]])])
    assert any("订单号" in issue for issue in hard), hard


def test_hard_conflict_with_verified_state() -> None:
    verified = [{"code": "ok", "text": "订单 ORD-1001 状态：未发货", "order_id": "ORD-1001"}]
    hard, _ = guards.review("ORD-1001 到哪了？", "订单 ORD-1001 已发货。",
                            [finding("order", answer="未发货", verified=verified)])
    assert any("冲突" in issue for issue in hard)


def test_order_list_with_mixed_statuses_is_not_a_conflict() -> None:
    listed = {"code": "ok", "text": (
        "以下是您最近的订单：\n- ORD-2024-001｜会员｜已发货\n"
        "- ORD-2024-003｜课程｜待发货")}
    draft = "您的订单：ORD-2024-001 已发货；ORD-2024-003 待发货。"
    hard, _ = guards.review("查询我的订单", draft,
                            [finding("order", answer=draft, verified=[listed])])
    assert not any("冲突" in issue for issue in hard), hard

    wrong = "您的订单：ORD-2024-001 已发货；ORD-2024-003 已发货。"
    hard, _ = guards.review("查询我的订单", wrong,
                            [finding("order", answer=wrong, verified=[listed])])
    assert any("冲突" in issue for issue in hard), hard

    hard, _ = guards.review("查询我的订单", "您的订单已发货。",
                            [finding("order", answer="待发货", verified=[listed])])
    assert any("冲突" in issue for issue in hard), hard


def test_hard_internal_leak() -> None:
    hard, _ = guards.review("退货政策？", "依据知识库片段 [K3]，签收后 7 天内可退。",
                            [finding("faq", answer="签收后 7 天内可申请退货。",
                                     evidence=EVIDENCE_7D, used_search=True)])
    assert any("内部" in issue for issue in hard)


def test_soft_missing_delegation_and_search() -> None:
    _hard, soft = guards.review("退货期限多久？", "本店支持顺丰上门取件退回。", [])
    assert any("FAQ" in issue for issue in soft)

    _hard, soft = guards.review("退货期限是几天？", "签收后 7 天内可申请退货。",
                                [finding("faq", answer="签收后 7 天内可申请退货。")])
    assert any("检索" in issue for issue in soft)


def test_clarifying_reply_to_a_policy_question_is_not_blocked() -> None:
    hard, soft = guards.review("退货怎么弄？", "请问您是想查订单状态，还是了解退货时限？", [])
    assert hard == [] and soft == []


def test_empty_draft_is_only_soft() -> None:
    hard, soft = guards.review("退货怎么弄？", "   ", [])
    assert hard == [] and soft


def test_safe_fallback_makes_no_promises() -> None:
    text = guards.safe_fallback(["订单事实未经本人订单接口核验"], [], [], "ORD-9 状态？")
    assert "已" not in text.replace("已经", "")
    assert "转人工" in text and "无法可靠核验" in text


def test_goods_condition_label_is_not_an_order_fact() -> None:
    """“商品状态：完好”是政策口径，只有“订单状态：已发货”才需要订单接口背书。"""
    question = "支持七天无理由退货吗？退回商品要保持什么状态？"
    draft = "支持。签收后 7 天内可申请退货，商品状态：完好、吊牌齐全、不影响二次销售。"
    hard, _soft = guards.review(question, draft, [],
                                customer_order_ids=set())
    assert "订单事实未经本人订单接口核验" not in hard

    hard, _soft = guards.review(question, "您的订单状态：已发货。", [],
                                customer_order_ids=set())
    assert "订单事实未经本人订单接口核验" in hard


def test_policy_labels_are_not_order_facts_but_state_claims_still_are() -> None:
    """通用政策里带冒号的字段（物流、发货、运费）不是订单事实；说"已签收"仍然是。"""
    for draft in ("物流：发货后 24 小时内可查揽收记录。",
                  "发货：付款后 48 小时内发出。",
                  "退货运费：质量问题由店铺承担。"):
        assert not guards.asserts_order_field(draft), draft

    for draft in ("包装状态：已签收。", "金额：199 元，已退款。", "状态：已签收"):
        assert guards.asserts_order_field(draft), draft


def test_adversarial_paraphrase_corpus_is_fully_blocked_and_never_overblocks() -> None:
    """The rules must hold for reworded violations, not just for their canonical phrasing.

    The corpus lives in ``evaluation/probe_guards_adversarial`` so the probe and this test
    cannot drift apart. It was written after that sweep found 11 of 25 unsafe drafts
    passing: 「已经为您办理退款了」, 「退款申请这边已经提交了」, 「签收后七天之内」,
    「满九十九元包邮」, 「订单 TB 1234567」. Both directions are asserted, because a
    Reviewer that blocks everything also "passes" a leak test.
    """
    from evaluation.four_agent.probe_guards_adversarial import CORPUS

    leaks, overkills = [], []
    for case_id, _category, question, draft, delegations, unsafe in CORPUS:
        hard, _soft = guards.review(question, draft, list(delegations))
        if unsafe and not hard:
            leaks.append(case_id)
        if not unsafe and hard:
            overkills.append((case_id, hard))

    assert leaks == [], f"无证据承诺/禁用操作/跨客户订单被放行：{leaks}"
    assert overkills == [], f"有证据的正常回答被误拦：{overkills}"
