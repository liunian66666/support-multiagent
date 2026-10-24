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

from score import score_case  # 复用同一判定，禁止另写一套口径


def _load_jsonl(path: Path):
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def _pass(rows_by_id, gold_row):
    if gold_row["id"] not in rows_by_id:
        return None  # unscored：该组没采集到
    merged = {**gold_row, **rows_by_id[gold_row["id"]]}
    scored = score_case(merged)
    if "answer_rules_ok" not in scored:
        return None
    return bool(scored["answer_rules_ok"])


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

    flips, same, unscored = [], [], []
    base_pass_n = cand_pass_n = scored_n = 0
    for g in gold:
        b, c = _pass(base_by_id, g), _pass(cand_by_id, g)
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
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")

    if scored_n and cand_pass_n < base_pass_n:
        sys.exit(1)  # 注入组低于基线 → 发布闸门不放行


if __name__ == "__main__":
    main()
