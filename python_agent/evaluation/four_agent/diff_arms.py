# ruff: noqa: RUF001
"""Render the four-arm memory ablation as a per-case (逐题) table.

`memory_context_ab.jsonl` holds one row per (case × arm) with every needle annotated by
two independent booleans, and the distinction is the whole point:

* ``in_durable_transcript`` — the fact still exists in the append-only transcript, so a
  later turn *could* be answered from the session at all.
* ``in_fed_context`` — the fact is actually inside the message list handed to the model
  this turn, so the model does see it.

A needle can be recoverable but unseen (window-only: the transcript kept it, the context
dropped it) or unrecoverable (delete-and-rebuild: it is gone). Reporting only
"recoverable 10/10" would hide that three of the four arms fail for the customer.

Usage:
    python python_agent/evaluation/four_agent/diff_arms.py [runs/memory_context_ab.jsonl] [-o table.md]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

# Order matters: the two control arms first, then the ablation, then the shipped design.
ARM_ORDER = ["window_only", "delete_rebuild", "watermark_norepair", "watermark"]
ARM_LABEL = {
    "window_only": "只留滑动窗口（不压缩）",
    "delete_rebuild": "删档重建（RemoveMessage 风格）",
    "watermark_norepair": "水位线，摘要丢锚点且不回补",
    "watermark": "水位线 + 锚点回补（本实现）",
    "mounted_v2": "挂载的 app 侧图（窗口 12 + RemoveMessage 压缩，无锚点回补）",
}


def rows_for(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def tally(row: dict) -> tuple[int, int, int]:
    needles = row["needles"]
    total = len(needles)
    fed = sum(1 for v in needles.values() if v["in_fed_context"])
    durable = sum(1 for v in needles.values() if v["in_durable_transcript"])
    return fed, durable, total


def main(args: argparse.Namespace) -> None:
    rows = rows_for(Path(args.src))
    by_case: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in rows:
        by_case[row["id"]][row["arm"]] = row

    arms = [arm for arm in ARM_ORDER if any(arm in case for case in by_case.values())]
    arms += sorted({arm for case in by_case.values() for arm in case} - set(arms))

    needles_per_case = tally(rows[0])[2]
    lines = [f"逐题{len(arms)}臂对照（{len(by_case)} 条长对话 × {len(arms)} 臂，"
             f"每臂每题 {needles_per_case} 个关键事实）",
             "",
             "单元格 = 进入模型上下文的锚点数 / 仍可从转写恢复的锚点数。",
             "",
             "| case | 水位线 | 摘要字数 | " + " | ".join(f"{a}（{ARM_LABEL.get(a, a)}）" for a in arms) + " |",
             "| --- | --- | --- | " + " | ".join("---" for _ in arms) + " |"]

    for case_id in sorted(by_case):
        case = by_case[case_id]
        anchor = case.get(arms[-1], next(iter(case.values())))
        cells = []
        for arm in arms:
            row = case.get(arm)
            if row is None:
                cells.append("—")
                continue
            fed, durable, total = tally(row)
            flag = "✅" if fed == total else ("❌" if durable < total else "⚠️")
            cells.append(f"{fed}/{total} ({durable}/{total}) {flag}")
        lines.append(f"| {case_id} | {anchor.get('watermark', '?')} | "
                     f"{anchor.get('summary_chars', '?')} | " + " | ".join(cells) + " |")

    totals = {arm: [0, 0, 0] for arm in arms}
    for row in rows:
        if row["arm"] in totals:
            fed, durable, total = tally(row)
            totals[row["arm"]][0] += fed
            totals[row["arm"]][1] += durable
            totals[row["arm"]][2] += total
    lines.append("")
    lines.append("汇总（锚点计数，非题数）：")
    for arm in arms:
        fed, durable, total = totals[arm]
        lines.append(f"  {arm:<20} 进上下文 {fed:>3}/{total}   可恢复 {durable:>3}/{total}   "
                     f"{ARM_LABEL.get(arm, '')}")

    text = "\n".join(lines)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"\n→ {args.out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("src", nargs="?", default="python_agent/evaluation/four_agent/runs/memory_context_ab.jsonl")
    parser.add_argument("-o", "--out", default="", help="also write the markdown table here")
    main(parser.parse_args())
