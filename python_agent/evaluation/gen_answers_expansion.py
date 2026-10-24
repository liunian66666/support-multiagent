# -*- coding: utf-8 -*-
"""[ours] S4 评测集扩充生成器：把 gold_answers 从 7 扩到 50 条。

纪律（防基准偏）：
- answer_policy 用例的 required 必须逐字出现在 demo 语料正文（脚本断言）；
- answer_no_policy / applicability 用例的 forbidden 是"错误肯定短语"，
  必须**不**出现在语料正文（脚本断言），且必须具体、可判，不用泛化否定；
- 订单类用例只引用 init.sql 真实演示订单（ORD-2024-001 已完成 / ORD-2024-002 配送中）；
- 通用泄漏黑名单沿用基线：原始检索片段、未接通占位话术不得出现在最终回答。

用法：python gen_answers_expansion.py   （原地重写 gold_answers.jsonl）
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
CORPUS = (HERE.parent / "assets/kb_seed/demo-store-policy-v1.md").read_text("utf-8") + \
         (HERE.parent / "assets/kb_seed/support-agent-process-v1.md").read_text("utf-8")

# 基线已验证的通用禁止项（泄漏原始片段 / 占位话术）
LEAK = ["已找到以下知识库片段", "当前主项目尚未接通", "知识来源：", "[K1]"]

# (id, category, customer, query, required, forbidden_extra)
NEW_CASES = [
    # ---- 政策类：required 逐字来自语料 --------------------------------
    ("ans-ship-48h", "answer_policy", "guest", "现货商品付款后多久发货？", ["48 小时"], []),
    ("ans-ship-holiday", "answer_policy", "guest", "节假日下单发货会延迟吗？", ["节假日顺延"], []),
    ("ans-ship-presale", "answer_policy", "guest", "预售商品什么时候发货？", ["商品页标注"], []),
    ("ans-ship-tracking", "answer_policy", "guest", "发货后物流信息在哪查？", ["订单页面"], []),
    ("ans-ship-pickup-time", "answer_policy", "guest", "发货后多久能查到揽收？", ["24 小时"], []),
    ("ans-fee-99", "answer_policy", "guest", "多少钱包邮？", ["99 元包邮"], []),
    ("ans-fee-under99", "answer_policy", "guest", "不够包邮金额运费多少？", ["8 元"], []),
    ("ans-fee-xinjiang", "answer_policy", "guest", "新疆收货运费怎么算？", ["新疆"], []),
    ("ans-fee-tibet", "answer_policy", "guest", "西藏能享受包邮吗？", ["西藏"], []),
    ("ans-fee-display", "answer_policy", "guest", "下单时能看到运费吗？", ["应付运费"], []),
    ("ans-ret-quality-freight", "answer_policy", "guest", "质量问题退换货运费谁出？", ["质量问题"], []),
    ("ans-ret-nr-freight", "answer_policy", "guest", "无理由退货运费谁承担？", ["买家承担"], []),
    ("ans-ret-window", "answer_policy", "guest", "退换货时限是多久？", ["签收后 7 天"], []),
    ("ans-ret-condition", "answer_policy", "guest", "退货商品有什么要求？", ["吊牌完整"], []),
    ("ans-ref-initiate", "answer_policy", "guest", "退款多久发起？", ["1 个工作日"], []),
    ("ans-ref-channel", "answer_policy", "guest", "退款会退到哪里？", ["原路退回"], []),
    ("ans-ref-balance", "answer_policy", "guest", "余额退款到账要多久？", ["实时到账"], []),
    ("ans-ref-card", "answer_policy", "guest", "银行卡退款多久到账？", ["1 至 3 个工作日"], []),
    ("ans-ref-progress", "answer_policy", "guest", "在哪看退款进度？", ["订单详情页"], []),
    ("ans-ref-accelerate", "answer_policy", "guest", "客服能加急退款到账吗？", ["不能人工加速"], []),
    ("ans-inv-type", "answer_policy", "guest", "你们开什么类型的发票？", ["电子普通发票"], []),
    ("ans-inv-special", "answer_policy", "guest", "能开专票吗？", ["增值税专用发票"], []),
    ("ans-inv-fill", "answer_policy", "guest", "开票信息怎么填？", ["抬头"], []),
    ("ans-inv-time", "answer_policy", "guest", "发票多久能开好？", ["3 个工作日"], []),
    ("ans-inv-email", "answer_policy", "guest", "发票发送到哪里？", ["邮箱"], []),
    ("ans-hr-hours", "answer_policy", "guest", "人工客服几点到几点在线？", ["21:00"], []),
    ("ans-hr-offhours", "answer_policy", "guest", "客服下班了还能联系吗？", ["留言"], []),
    ("ans-hr-wait", "answer_policy", "guest", "转人工大概要等多久？", ["等待"], []),
    # ---- 语料外：禁止错误肯定 ------------------------------------------
    ("ans-na-coupon", "answer_no_policy", "guest", "有优惠券吗？", [], ["可以领取优惠券", "有优惠券可以领"]),
    ("ans-na-points", "answer_no_policy", "guest", "消费能积分吗？", [], ["可以积分", "能获得积分"]),
    ("ans-na-gift", "answer_no_policy", "guest", "下单送赠品吗？", [], ["会送赠品", "有赠品"]),
    ("ans-na-installment", "answer_no_policy", "guest", "支持花呗分期吗？", [], ["支持花呗", "可以分期"]),
    ("ans-na-warranty", "answer_no_policy", "guest", "商品保修几年？", [], ["保修 1 年", "保修两年"]),
    ("ans-na-price-protect", "answer_no_policy", "guest", "降价能退差价吗？", [], ["可以申请价保", "可以退差价"]),
    ("ans-na-redpacket", "answer_no_policy", "guest", "好评返现怎么领？", [], ["返现红包", "好评返 5 元"]),
    ("ans-na-reissue", "answer_no_policy", "guest", "少发货了能补发吗？", [], ["可以给您补发", "会安排补发"]),
    ("ans-na-address", "answer_no_policy", "guest", "发货后能改地址吗？", [], ["可以修改收货地址", "能改地址"]),
    ("ans-na-insurance", "answer_no_policy", "guest", "订单有运费险吗？", [], ["附带运费险", "有运费险"]),
    ("ans-na-overseas", "answer_no_policy", "guest", "能寄到国外吗？", [], ["可以邮寄到国外", "支持海外配送"]),
    # ---- 订单类：仅用 init.sql 真实演示订单 -----------------------------
    ("ans-order-001", "answer_order_lookup", "buyer1", "订单ORD-2024-001现在什么状态？", ["ORD-2024-001", "已完成"], []),
    ("ans-order-002", "answer_order_lookup", "buyer1", "帮我查一下ORD-2024-002的物流状态", ["ORD-2024-002", "配送中"], []),
    # ---- 复合：政策套用边界 ---------------------------------------------
    ("ans-app-vip-return", "answer_applicability", "guest", "VIP会员服务没效果可以七天无理由退吗？", [], ["吊牌完整即可退货"]),
    ("ans-app-cannot-pay", "answer_process", "guest", "帮我把这个订单付了", [], ["已为您完成支付", "支付成功"]),
]


def _validate() -> None:
    for cid, cat, _cust, _q, required, extra_forbidden in NEW_CASES:
        if cat == "answer_policy":
            for t in required:
                assert t in CORPUS, f"{cid}: required 不在语料中: {t!r}"
        if cat == "answer_no_policy":
            for t in extra_forbidden:
                assert t not in CORPUS, f"{cid}: forbidden 出现在语料中: {t!r}"
    ids = [c[0] for c in NEW_CASES]
    assert len(ids) == len(set(ids)), "用例 id 重复"


def main() -> None:
    _validate()
    base_path = HERE / "gold_answers.jsonl"
    bak_path = HERE / "gold_answers_v1.jsonl.bak"
    src = bak_path if bak_path.exists() else base_path
    existing = [json.loads(x) for x in src.read_text("utf-8").splitlines() if x.strip()]
    dup = {r["id"] for r in existing} & {c[0] for c in NEW_CASES}
    assert not dup, f"新增用例与基线 id 冲突: {dup}"

    new_rows = [{
        "id": cid, "category": cat, "customer": cust, "query": q,
        "required": list(required),
        "forbidden": sorted(set(LEAK) | set(extra_forbidden)),
    } for cid, cat, cust, q, required, extra_forbidden in NEW_CASES]

    rows = existing + new_rows
    assert len(rows) == 50, f"目标 50 条，实际 {len(rows)} 条"
    with base_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"OK: {base_path.name} 写入 {len(rows)} 条（基线 {len(existing)} + 新增 {len(new_rows)}）")


if __name__ == "__main__":
    main()
