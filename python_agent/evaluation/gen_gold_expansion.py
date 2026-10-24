# -*- coding: utf-8 -*-
"""[ours] S4 评测集扩充生成器：把 gold_retrieval 从 24 扩到 100 条。

纪律（设计方案 §5 风险"基准偏→优化跑偏"）：
- 每条 answerable 检索用例的 gold_docs 必须是 demo 语料真实 doc_id；
- gold_evidence_terms 必须逐字出现在语料正文中（脚本内断言校验，防基准偏）；
- no_answer 用例的关键词必须**不**出现在语料正文；
- 只读现有语料做校验，不写业务数据；输出 JSONL 供评测直接消费。

用法：python gen_gold_expansion.py   （原地重写 gold_retrieval.jsonl）
"""
import json
from pathlib import Path

POLICY = "8b0a0bf7d19d41b8b519b2c2a00fb54e"   # 演示店铺服务政策
PROCESS = "c111e8bd171b4bfe8abf54fd7ce3ca30"  # 客服通用流程
HERE = Path(__file__).parent
TENANT = "t_demo"
CORPUS = (HERE.parent / "assets/kb_seed/demo-store-policy-v1.md").read_text("utf-8") + \
         (HERE.parent / "assets/kb_seed/support-agent-process-v1.md").read_text("utf-8")

# (id, category, gold_docs, gold_evidence_terms, query)
NEW_CASES = [
    # ---- 发货时效 -------------------------------------------------------
    ("ship-physical-hours", "policy", [POLICY], ["48 小时"], "现货商品付款后多久发货？"),
    ("ship-holiday-delay", "policy", [POLICY], ["节假日顺延"], "节假日下单什么时候发货？"),
    ("ship-presale", "policy", [POLICY], ["预售商品"], "预售商品按什么时间发货？"),
    ("ship-tracking-sync", "policy", [POLICY], ["订单页面"], "物流单号在哪里查看？"),
    ("ship-tracking-time", "policy", [POLICY], ["揽收"], "发货后多久能查到物流揽收记录？"),
    # ---- 运费与包邮 -----------------------------------------------------
    ("ship-fee-under99", "policy", [POLICY], ["8 元"], "订单不满99元运费是多少？"),
    ("ship-full99", "policy", [POLICY], ["99 元包邮"], "单笔订单实付多少钱可以包邮？"),
    ("ship-region-tibet", "policy", [POLICY], ["西藏"], "西藏地区运费怎么计算？"),
    ("ship-region-inner-mongolia", "policy", [POLICY], ["内蒙古"], "内蒙古部分地区运费如何？"),
    ("ship-fee-display", "policy", [POLICY], ["应付运费"], "下单时运费会显示出来吗？"),
    ("ship-remote-exclude", "policy", [POLICY], ["偏远地区除外"], "包邮政策对偏远地区适用吗？"),
    # ---- 退换货 ---------------------------------------------------------
    ("ret-quality-freight", "policy", [POLICY], ["质量问题"], "质量问题退换货的运费谁出？"),
    ("ret-quality-roundtrip", "policy", [POLICY], ["往返运费由店铺承担"], "质量问题换货运费谁承担？"),
    ("ret-no-reason-freight", "policy", [POLICY], ["买家承担"], "七天无理由退货的寄回运费谁付？"),
    ("ret-window", "policy", [POLICY], ["签收后 7 天"], "退换货需要在签收几天内发起？"),
    ("ret-tag-requirement", "policy", [POLICY], ["吊牌完整"], "退回商品需要保持什么状态？"),
    ("ret-resale-impact", "policy", [POLICY], ["二次销售"], "影响二次销售的商品还能退吗？"),
    ("ret-no-reason-days", "policy", [POLICY], ["七天无理由"], "支持七天无理由退货吗？"),
    # ---- 退款 -----------------------------------------------------------
    ("ref-initiate", "policy", [POLICY], ["1 个工作日"], "验收通过后多久发起退款？"),
    ("ref-channel", "policy", [POLICY], ["原路退回"], "退款退到哪个账户？"),
    ("ref-balance-time", "policy", [POLICY], ["实时到账"], "余额类退款多久到账？"),
    ("ref-card-time", "policy", [POLICY], ["银行卡 1 至 3 个工作日"], "银行卡退款多久到账？"),
    ("ref-view-progress", "policy", [POLICY], ["订单详情页"], "在哪里查看退款进度？"),
    ("ref-no-accelerate", "policy", [POLICY], ["不能人工加速"], "客服能帮忙加快退款到账吗？"),
    ("ref-verify-gate", "policy", [POLICY], ["验收通过后"], "退款发起的前提条件是什么？"),
    # ---- 发票 -----------------------------------------------------------
    ("inv-supported", "policy", [POLICY], ["电子普通发票"], "支持开具哪种发票？"),
    ("inv-no-special", "policy", [POLICY], ["暂不支持增值税专用发票"], "能开增值税专用发票吗？"),
    ("inv-fill", "policy", [POLICY], ["抬头"], "发票抬头和税号在哪里填写？"),
    ("inv-timing", "policy", [POLICY], ["3 个工作日"], "发票多久开出？"),
    ("inv-email", "policy", [POLICY], ["预留邮箱"], "发票会发送到哪里？"),
    # ---- 人工客服 -------------------------------------------------------
    ("hr-service-window", "policy", [POLICY], ["9:00"], "在线人工客服的服务时间是什么？"),
    ("hr-offhours", "policy", [POLICY], ["下一个服务时段"], "非服务时段怎么联系客服？"),
    ("hr-no-promise-wait", "policy", [POLICY], ["可能需要等待"], "转人工需要等多久？"),
    ("hr-no-promise-duration", "policy", [POLICY], ["不承诺具体响应时长"], "客服会承诺响应时长吗？"),
    # ---- 通用流程（process doc）----------------------------------------
    ("pr-return-collect", "process", [PROCESS], ["退货原因"], "发起退货前客服要收集哪些信息？"),
    ("pr-order-verify-first", "process", [PROCESS], ["归属"], "展示订单信息前需要先核验什么？"),
    ("pr-verify-fail", "process", [PROCESS], ["不推测订单状态"], "身份核验失败时客服会怎么处理？"),
    ("pr-refund-not-execute", "process", [PROCESS], ["不执行退款"], "客服能直接给我退款吗？"),
    ("pr-pay-not-execute", "process", [PROCESS], ["支付操作"], "客服可以替我完成支付吗？"),
    ("pr-handoff-takeover", "process", [PROCESS], ["人工客服接管"], "转人工之后由谁处理？"),
    ("pr-handoff-no-promise", "process", [PROCESS], ["不得承诺等待时长"], "转人工后会承诺多久回复吗？"),
    ("pr-handoff-create-ticket", "process", [PROCESS], ["创建工单"], "明确要求转人工时会发生什么？"),
    ("pr-process-not-policy", "process", [PROCESS], ["不构成退换货资格"], "流程文档能当店铺承诺用吗？"),
    # ---- 语料外（no_answer，关键词必须不在语料中）----------------------
    ("na-warranty", "no_answer", [], ["保修"], "手机保修期是几年？"),
    ("na-installment", "no_answer", [], ["分期"], "支持花呗分期付款吗？"),
    ("na-gift", "no_answer", [], ["赠品"], "下单会送赠品吗？"),
    ("na-membership", "no_answer", [], ["会员"], "店铺有会员积分制度吗？"),
    ("na-price-protect", "no_answer", [], ["价保"], "降价了可以申请价保退差价吗？"),
    ("na-cod", "no_answer", [], ["货到付款"], "支持货到付款吗？"),
    ("na-lazy-station", "no_answer", [], ["驿站"], "可以不送货直接放驿站吗？"),
    ("na-redpacket", "no_answer", [], ["红包"], "评价返现的红包怎么领？"),
    ("na-reissue", "no_answer", [], ["补发"], "商品少发了可以补发吗？"),
    ("na-address-edit", "no_answer", [], ["修改收货地址"], "发货后还能修改收货地址吗？"),
    ("na-coupon", "no_answer", [], ["优惠券"], "有店铺优惠券可以领吗？"),
    ("na-app", "no_answer", [], ["App"], "有没有手机App可以下单？"),
    # ---- 追加同证据词、不同问法的用例（提升查询覆盖密度）----------------
    ("ship-physical-48", "policy", [POLICY], ["现货商品"], "付完款现货多久能寄出？"),
    ("ship-tracking-24h", "policy", [POLICY], ["24 小时内"], "物流信息一般几天内能查到？"),
    ("ret-quality-who-pays", "policy", [POLICY], ["由店铺承担"], "东西有问题，退回来的邮费谁出？"),
    ("ret-buyer-pays", "policy", [POLICY], ["退回运费由买家承担"], "无理由退货为什么要我出运费？"),
    ("ret-condition", "policy", [POLICY], ["不影响二次销售"], "退货对商品本身有什么要求？"),
    ("ref-after-inspection", "policy", [POLICY], ["退货验收"], "退的货检查完几天开始退款？"),
    ("inv-remark-fill", "policy", [POLICY], ["备注中填写"], "开票信息下单时怎么给？"),
    ("inv-after-finish", "policy", [POLICY], ["订单完成后"], "发票是下单就开还是完成后开？"),
    ("hr-morning-open", "policy", [POLICY], ["至 21:00"], "客服晚上几点下班？"),
    ("hr-leave-message", "policy", [POLICY], ["可留言"], "客服下班了留言有用吗？"),
    ("hr-peak-wait", "policy", [POLICY], ["高峰期"], "为什么转人工要排队？"),
    ("pr-return-order-no", "process", [PROCESS], ["订单号"], "退货为什么要先报订单号？"),
    ("pr-identity-check", "process", [PROCESS], ["客户身份"], "查订单前为什么要验证身份？"),
    ("pr-refund-handoff", "process", [PROCESS], ["相关诉求可转人工"], "要退款但机器人办不了怎么办？"),
    ("pr-refund-approval", "process", [PROCESS], ["独立审批"], "退款功能开放需要什么条件？"),
    ("pr-handoff-request", "process", [PROCESS], ["明确要求"], "我不想跟机器人聊，怎么转人工？"),
    ("pr-scope-limit", "process", [PROCESS], ["咨询流程"], "流程说明里写的内容算商家承诺吗？"),
    ("pr-freight-clause", "process", [PROCESS], ["到账时间、运费"], "运费条款应该查政策还是流程文档？"),
    ("na-freight-insurance", "no_answer", [], ["运费险"], "订单带运费险吗？"),
    ("na-self-pickup", "no_answer", [], ["自提"], "线上门店支持到店自提吗？"),
    ("na-overseas", "no_answer", [], ["海外"], "可以邮寄到国外吗？"),
]


def _validate() -> None:
    for cid, _cat, docs, terms, _q in NEW_CASES:
        if docs:  # answerable：证据词必须逐字在语料正文
            for t in terms:
                assert t in CORPUS, f"{cid}: 证据词不在语料中: {t!r}"
        else:     # no_answer：关键词必须不在语料正文
            assert len(terms) == 1, f"{cid}: no_answer 需要恰好 1 个关键词"
            assert terms[0] not in CORPUS, f"{cid}: no_answer 关键词出现在语料: {terms[0]!r}"
    ids = [c[0] for c in NEW_CASES]
    assert len(ids) == len(set(ids)), "用例 id 重复"


def main() -> None:
    _validate()
    base_path = HERE / "gold_retrieval.jsonl"
    bak_path = HERE / "gold_retrieval_v1.jsonl.bak"
    existing = []
    src = bak_path if bak_path.exists() else base_path
    for line in src.read_text("utf-8").splitlines():
        line = line.strip()
        if line:
            existing.append(json.loads(line))
    old_ids = {r["id"] for r in existing}
    dup = old_ids & {c[0] for c in NEW_CASES}
    assert not dup, f"新增用例与基线 id 冲突: {dup}"

    new_rows = [{
        "id": cid, "category": cat, "tenant_id": TENANT, "query": q,
        "gold_docs": docs, "gold_evidence_terms": terms,
    } for cid, cat, docs, terms, q in NEW_CASES]

    rows = existing + new_rows
    total = len(rows)
    assert total == 100, f"目标 100 条，实际 {total} 条"

    with base_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"OK: {base_path.name} 写入 {total} 条（基线 {len(existing)} + 新增 {len(new_rows)}）")


if __name__ == "__main__":
    main()
