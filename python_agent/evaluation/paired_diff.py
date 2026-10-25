# -*- coding: utf-8 -*-
"""[ours] S4 配对评测：注入 vs 不注入的量化 diff（RSI 影子验证的评分端）。

配对方式（同一金标、同一模型、只切换召回开关，单变量对照）：
  1) 影子基线：agent 容器 EXPERIENCE_RECALL_MODE=shadow 时，在 business 容器跑
       python collect_answers_live.py gold_answers.jsonl --output base.jsonl
  2) 注入组：切 EXPERIENCE_RECALL_MODE=inject 后同命令跑出 inj.jsonl
  3) 本脚本对两次采集打分并输出逐用例翻转与总分布 diff：
       python paired_diff.py gold_answers.jsonl --base base.jsonl --cand inj.jsonl

判定口径与 score.py 完全一致（required 全含且 forbidden 全无 = pass）。
退出码：候选组 pass 率低于基线 → 1（供 CI/发布闸门拦截），否则 0。
"""
import argparse
import json
import sys
from pathlib import Path

try:
    from .score import score_case  # package import (unit tests)
except ImportError:
    from score import score_case  # direct script execution

# 回避/推诿措辞（S9 实测回退答案中的实际句式）：与"丢失金标数值"同时出现
# 即判数值拒答阻断项。宁可多查不可漏查，词表只增不删。
_EVASIVE_PATTERNS = (
    "无法为您确认", "无法确认", "无法据此", "不能作为", "不代表",
    "以店铺已发布政策为准", "以已发布政策为准", "抱歉", "无相关", "无法回答",
    "查不到", "没有相关", "暂无相关", "没有查询到",
    "没法给", "不能给", "不能当作",
)


def _load_jsonl(path: Path):
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def _score(rows_by_id, gold_row):  # noqa: E302
    """返回 (ok 或 None, 丢失的 required 数值词列表)。"""
    if gold_row["id"] not in rows_by_id:
        return None, []
    merged = {**gold_row, **rows_by_id[gold_row["id"]]}
    scored = score_case(merged)
    if "answer_rules_ok" not in scored:
        return None, []
    return bool(scored["answer_rules_ok"]), list(scored.get("missing_required", []))


def _pass(rows_by_id, gold_row):
    ok, _ = _score(rows_by_id, gold_row)
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gold", type=Path)
    parser.add_argument("--base", type=Path, required=True, help="不注入/影子组采集")
    parser.add_argument("--cand", type=Path, required=True, help="注入组采集")
    parser.add_argument("--output", type=Path, help="diff 报告 JSON 输出路径")
    args = parser.parse_args()

    gold = _load_jsonl(args.gold)
    gold_ids = [g["id"] for g in gold]
    assert len(gold_ids) == len(set(gold_ids)), "gold id 重复"
    base_by_id = {r["id"]: r for r in _load_jsonl(args.base)}
    cand_by_id = {r["id"]: r for r in _load_jsonl(args.cand)}

    flips, same, unscored, blockers, manual_review = [], [], [], [], []
    base_pass_n = cand_pass_n = scored_n = 0
    for g in gold:
        b, _ = _score(base_by_id, g)
        c, c_missing = _score(cand_by_id, g)
        if b is None or c is None:
            unscored.append(g["id"])
            continue
        scored_n += 1
        base_pass_n += int(b)
        cand_pass_n += int(c)
        if b != c:
            flips.append({"id": g["id"], "category": g.get("category", ""),
                          "direction": "regress" if b else "improve"})
        else:
            same.append(g["id"])
        # 阻断项：金标要求具体数值且基线答对，注入组丢掉该数值且答案出现
        # 回避/推诿措辞 → 无论总体 pass 率涨跌都判不放行（S9 保守回退教训）。
        numeric = [k for k in g.get("required", [])
                   if any(ch.isdigit() for ch in k)]
        if numeric and b and not c:
            lost = [k for k in numeric if k in c_missing]
            ans = cand_by_id[g["id"]].get("answer", "")
            evasive = any(p in ans for p in _EVASIVE_PATTERNS)
            # A literal number in a refusal or hypothetical example is not a factual answer.
            # Keep the historical string score visible, but require manual review instead
            # of claiming the candidate lost a correctly answered policy fact.
            base_ans = base_by_id[g["id"]].get("answer", "")
            base_body = base_ans.replace("以下为演示环境中的示例答复；涉及的店铺政策不代表真实商家承诺。", "")
            base_evasive = any(p in base_body for p in _EVASIVE_PATTERNS if p not in ("抱歉", "不代表"))
            if lost and base_evasive:
                manual_review.append({"id": g["id"], "reason": "基线仅字面包含数值且伴随拒答；需人工判断是否真正回答"})
            elif lost and evasive:
                blockers.append({"id": g["id"], "lost_numeric": lost,
                                 "reason": "基线给出金标数值，候选改为拒答/回避"})

    base_rate = round(base_pass_n / scored_n, 4) if scored_n else None
    cand_rate = round(cand_pass_n / scored_n, 4) if scored_n else None
    report = {
        "gold": args.gold.name, "scored": scored_n,
        "unscored_ids": unscored,
        "base": {"pass": base_pass_n, "pass_rate": base_rate},
        "cand": {"pass": cand_pass_n, "pass_rate": cand_rate},
        "delta_pass_rate": round(cand_pass_n/scored_n - base_pass_n/scored_n, 4) if scored_n else None,
        "flips": flips,
        "improve": sum(1 for f in flips if f["direction"] == "improve"),
        "regress": sum(1 for f in flips if f["direction"] == "regress"),
        "blockers": blockers,
        "manual_review": manual_review,
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")

    if blockers:
        sys.exit(2)  # 数值拒答阻断项：总体不下降也一票否决
    if manual_review:
        sys.exit(3)  # 金标的字面命中不足以自动判断，禁止自动发布

    if scored_n and cand_pass_n < base_pass_n:
        sys.exit(1)  # 注入组低于基线 → 发布闸门不放行


if __name__ == "__main__":
    main()
