# 中文文案必然包含全角标点
"""答题完整性：材料齐全不等于回答了问题。

回归的是这条真实问句——

    ORD-2024-001 能不能退款？没拆封的话能不能退款，拆开用过了还能不能退款？

当时的答复是 700 字的政策复述：证据齐、安全规则一条没犯，客户却看不到"能不能"。
这里钉的是 ``answer_gaps`` 本身；同一句反馈真的把撰写退回重写，由
``test_node_engine.py::test_shape_gap_rewrites_the_answer_without_re_dispatching`` 证明。
"""

from __future__ import annotations

from app.agent.support import guards
from app.agent.support.state import Delegation

QUESTION = "ORD-2024-001 能不能退款？没拆封的话能不能退款，拆开用过了还能不能退款？"

ORDER = {"code": "ok", "text": "订单 ORD-2024-001 状态：已发货", "order_id": "ORD-2024-001"}

POLICY_DUMP = (
    "店铺退货规则说明如下。"
    "第一条，客户签收后有权提出退货，商品需保持全新状态，吊牌与包装完整。"
    "第二条，商品需不影响二次销售，附件与赠品需一并寄回。"
    "第三条，款项按原路径退回，到账时间由渠道处理。"
    "第四条，退货运费按责任方承担，质量问题由店铺承担，非质量问题由客户承担。"
    "第五条，定制类、生鲜类、已激活的虚拟商品不在受理范围内。"
    "第六条，活动期间规则以活动页面公示内容执行。"
    "第七条，客户需在申请时填写原因，便于仓库核对。"
    "第八条，退货需在规定期限内提出，超出期限的申请需单独审核。"
    "第九条，店铺保留对异常订单的复核权利，复核期间答复可能延后。")

ITEM_BY_ITEM = (
    "订单 ORD-2024-001 已发货。\n"
    "未拆封：可以申请。签收后 7 天内、商品与包装完好即可申请退货。\n"
    "已拆封：不可以。拆封使用后影响二次销售，不符合七天无理由退货条件。")


def finding(specialist: str, *, answer: str = "", verified=()) -> Delegation:
    return {"turn_id": "t1", "specialist": specialist, "answer": answer, "tool_calls": [],
            "evidence": [], "verified_results": list(verified), "used_search": True,
            "failed": False}


def test_policy_dump_without_a_verdict_is_sent_back() -> None:
    """700 字政策条款不是回答「能不能退款」。"""
    assert len(POLICY_DUMP) >= guards._LONG_ANSWER
    gaps = guards.answer_gaps(QUESTION, POLICY_DUMP, [finding("faq", answer=POLICY_DUMP)])
    assert any("没有给出" in gap and "结论" in gap for gap in gaps), gaps


def test_item_by_item_reply_is_accepted() -> None:
    """逐项结论 + 一句依据：这是要的形状，不能被判成漏项或缺结论。"""
    hard, soft = guards.review(QUESTION, ITEM_BY_ITEM, [
        finding("faq", answer="签收后 7 天内可申请退货。"),
        finding("order", answer="订单 ORD-2024-001 状态：已发货", verified=[ORDER])])
    assert hard == [] and soft == [], (hard, soft)


def test_missing_condition_item_is_sent_back() -> None:
    """客户问了「没拆封」和「拆开用过」两项，只答一项就是漏答。"""
    partial = "订单 ORD-2024-001 已发货。未拆封：可以申请。签收后 7 天内、商品完好即可申请退货。"
    gaps = guards.answer_gaps(QUESTION, partial, [])
    assert any("「已拆封」" in gap for gap in gaps), gaps
    assert not any("「未拆封」" in gap for gap in gaps), gaps


def test_verdict_on_this_order_without_a_lookup_is_sent_back() -> None:
    """通用条款不能直接写成「这笔订单可以退」。"""
    gaps = guards.answer_gaps("ORD-2024-001 能退吗？", "这笔订单可以申请退款。", [])
    assert any("没有本人订单核验" in gap for gap in gaps), gaps


def test_verdict_on_this_order_with_a_lookup_is_accepted() -> None:
    gaps = guards.answer_gaps("ORD-2024-001 能退吗？", "这笔订单可以申请退款。",
                              [finding("order", answer="已发货", verified=[ORDER])])
    assert gaps == [], gaps


def test_conclusion_buried_after_the_clauses_is_sent_back() -> None:
    """结论写在 500 字条款之后，等于要客户自己读完再总结。"""
    buried = POLICY_DUMP + POLICY_DUMP + "这笔订单可以申请退款。"
    gaps = guards.answer_gaps(QUESTION, buried, [finding("faq", answer=buried)])
    assert any("结论没有前置" in gap for gap in gaps), gaps


def test_clarifying_reply_and_short_answer_are_not_sent_back() -> None:
    """澄清和一句话回答不该被"缺结论"推去重写——只有铺开的条款才是政策复述。"""
    assert guards.answer_gaps(QUESTION, "请问您是想查这笔订单的状态，还是想了解退货政策？", []) == []
    assert guards.answer_gaps("退货怎么弄？", "签收后 7 天内可申请退货。", []) == []
