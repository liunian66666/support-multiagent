# 聚合文案含中文
"""Aggregate the retrieval-stability batches into one honest picture.

    bash python_agent/evaluation/four_agent/run_all.sh retrieval-batches 3      # 3 个独立批次（每批一个容器进程）
    python python_agent/evaluation/four_agent/summarize_retrieval_batches.py    # 然后把所有批次并成一张表

One batch can only answer "did the top-3 move in this batch". The number that actually
matters for phase 5 is a *rate*: how often a case flips across independent batches, and
whether any of those flips ever costs the gold document or the required clause. A single
batch is what let an earlier version of this report claim "零抖动", so the aggregation is
the fix, not decoration.

Read-only: it never touches Qdrant or the model, it just re-reads `runs/*.jsonl`.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

RUNS = Path(__file__).resolve().parent / "runs"


def load(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if isinstance(r, dict) and "runs" in r]


def main(args: argparse.Namespace) -> int:
    files = sorted(Path(args.runs).glob(args.pattern))
    if not files:
        raise SystemExit(f"no batch files matched {args.pattern} under {args.runs}")

    print(f"{'批次':<40} {'题':>3} {'翻 chunk':>8} {'翻 doc':>7} {'分数逐位不同':>12} "
          f"{'数值集合变':>10} {'金标掉':>6} {'条款掉':>6}")
    set_flips: Counter = Counter()
    score_drift = value_change = hit_loss = cover_loss = 0
    case_batches = retrievals = 0
    for f in files:
        rows = load(f)
        if not rows:
            continue
        sets = [r for r in rows if r["distinct_candidate_sets"] > 1]
        docs = [r for r in rows if len({tuple(x["candidate_docs"]) for x in r["runs"]}) > 1]
        # older batches predate the extra columns; treat "unknown" as "not measured", not "clean"
        drift = [r for r in rows if (r.get("distinct_score_tuples") or 1) > 1]
        vals = [r for r in rows if (r.get("distinct_value_tuples") or 1) > 1]
        hits = [r for r in rows if (r["gold_hit_rate"] or 1) < 1]
        covs = [r for r in rows if (r["clause_coverage_rate"] or 1) < 1]
        print(f"{f.name:<40} {len(rows):>3} {len(sets):>8} {len(docs):>7} {len(drift):>12} "
              f"{len(vals):>10} {len(hits):>6} {len(covs):>6}")
        case_batches += len(rows)
        retrievals += sum(len(r["runs"]) for r in rows)
        score_drift += len(drift)
        value_change += len(vals)
        hit_loss += len(hits)
        cover_loss += len(covs)
        for r in sets:
            set_flips[r["id"]] += 1

    measured = [f for f in files if load(f) and "distinct_score_tuples" in load(f)[0]]
    print(f"\n合计 {len(files)} 批 · {case_batches} 个题次 · {retrievals} 次检索")
    print(f"  chunk 组成翻动：{sum(set_flips.values())}/{case_batches} 题次"
          f"（{100 * sum(set_flips.values()) / max(case_batches, 1):.1f}%）→ {dict(set_flips)}")
    if measured:
        mb = sum(len(load(f)) for f in measured)
        md = sum(1 for f in measured for r in load(f) if (r.get("distinct_score_tuples") or 1) > 1)
        mv = sum(1 for f in measured for r in load(f) if (r.get("distinct_value_tuples") or 1) > 1)
        print(f"  分数逐位不同：{md}/{mb} 题次（{100 * md / mb:.1f}%）——只有新批次带这一列")
        print(f"  候选里的数值集合变过：{mv}/{mb} 题次（{100 * mv / mb:.1f}%）")
    else:
        print("  分数逐位不同：这批文件都没有该列（探针升级前采的），不能报这个数")
    print(f"  金标掉出过 top-3：{hit_loss} 题次；条款覆盖掉过：{cover_loss} 题次")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="summarize retrieval stability batches")
    parser.add_argument("--runs", default=str(RUNS))
    parser.add_argument("--pattern", default="retr*.jsonl")
    raise SystemExit(main(parser.parse_args()))
