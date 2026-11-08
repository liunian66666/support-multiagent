# ruff: noqa: RUF001  # 报告文案含中文
"""旧版对照的逐题差异闸门：baseline 臂 vs 候选臂（1~2 次采集），逐题分类 + 汇总 + 退出码。

Why this file exists: the phase-6 deliverable asks for 逐题差异, and the model-blocked live arms
have to be diff-able the moment a gateway comes back. Two existing tools cover parts of it
(`score_heldout.py` scores one answer file, `runs/ho_regression_diff.py` diffs two *checklist*
files) but the collector `run_ab_live.py` writes answer rows with its own field names
(`required_missing` / `forbidden_hit` / `answer`), which the checklist differ does not read --
so the chain had a silent seam. This tool consumes **answer files directly** (any arm: the frozen
old baseline, a re-run of it, or the new-path `live` output), so one command closes the loop.

Judging reuses the official ruler on purpose: `from evaluation.score_heldout import norm`, never a
local copy -- §2 of the report documents a real incident where a private copy of the ruler invented
one fake regression out of a whitespace difference. `python_agent/tests/four_agent/test_ruler.py` keeps that pinned.

Per question it reports one of five classes, which is the distinction that matters for a gate:

    keep  保持通过        baseline pass & candidate pass
    fix   翻正            baseline fail & candidate pass
    REG   回退            baseline pass & candidate fail      <- blocks (exit 1)
    both  保持失败        baseline fail & candidate fail
    gap   候选未采集      the arm has no answer for this case  <- blocks too, but is NOT a 回退

That split is measured hygiene, not pedantry: the first ``custom``-arm run covered 12 of 36
questions and the table claimed "回退 24" while also reporting "候选缺题 24" — the same cases
charged twice, and a coverage gap dressed up as a regression. A partial arm must still fail the
gate (nothing was proven), but it has to say so with the right word.

and for every non-keep row it prints what is missing / which forbidden term tripped plus the answer
head, because a literal failure is a lead for human review, not a verdict.

Usage:
    python python_agent/evaluation/four_agent/heldout_compare.py python_agent/evaluation/gold_heldout_v1.jsonl \
        --baseline python_agent/evaluation/runs/ho_off_r4.jsonl \
        --candidate python_agent/evaluation/runs/ho_mem_r2_answers.jsonl \
        -o python_agent/evaluation/four_agent/runs/heldout_diff_sample.md
Exit code: 0 when no arm regresses against the baseline, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
for entry in (str(ROOT), str(ROOT / "python_agent")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from evaluation.score_heldout import norm  # the official ruler; do not re-implement

CLASSES = {"keep": "保持通过", "fix": "翻正", "REG": "回退", "both": "保持失败",
           "gap": "候选未采集"}


def rows_by_id(path: Path) -> dict[str, dict]:
    return {r["id"]: r for r in (json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip())}


def judge(gold_row: dict, answer_row: dict | None) -> tuple[bool, list, list]:
    """(literal_pass, missing_required, forbidden_hit) using the shared norm()."""
    text = norm((answer_row or {}).get("answer", ""))
    missing = [t for t in gold_row.get("required", []) if norm(t) not in text]
    tripped = [t for t in gold_row.get("forbidden", []) if norm(t) in text]
    ok = bool((answer_row or {}).get("answer")) and not missing and not tripped
    return ok, missing, tripped


def compare(gold: list[dict], baseline: dict, candidate: dict,
            cand_name: str) -> tuple[list[dict], dict[str, int]]:
    rows, counts = [], {"keep": 0, "fix": 0, "REG": 0, "both": 0, "gap": 0}
    for g in gold:
        cid = g["id"]
        base_ok, base_missing, base_forbid = judge(g, baseline.get(cid))
        if cid not in candidate:
            # Nothing was collected for this case: the arm cannot be judged on it, so it is
            # reported as a coverage gap. It still blocks (see `main`), just not as a 回退.
            counts["gap"] += 1
            rows.append({"id": cid, "category": g.get("category", ""), "class": "gap",
                         "baseline_pass": base_ok, "candidate_pass": False,
                         "baseline_missing": base_missing, "candidate_missing": [],
                         "baseline_forbidden": base_forbid, "candidate_forbidden": [],
                         "baseline_head": ((baseline.get(cid) or {}).get("answer", "") or "")[:70],
                         "candidate_head": "", "baseline_latency": (baseline.get(cid) or {}).get("latency_ms"),
                         "candidate_latency": None})
            continue
        cand_ok, cand_missing, cand_forbid = judge(g, candidate.get(cid))
        key = ("keep" if base_ok and cand_ok else
               "fix" if (not base_ok and cand_ok) else
               "REG" if (base_ok and not cand_ok) else "both")
        counts[key] += 1
        rows.append({"id": cid, "category": g.get("category", ""), "class": key,
                     "baseline_pass": base_ok, "candidate_pass": cand_ok,
                     "baseline_missing": base_missing, "candidate_missing": cand_missing,
                     "baseline_forbidden": base_forbid, "candidate_forbidden": cand_forbid,
                     "baseline_head": ((baseline.get(cid) or {}).get("answer", "") or "")[:70],
                     "candidate_head": ((candidate.get(cid) or {}).get("answer", "") or "")[:70],
                     "baseline_latency": (baseline.get(cid) or {}).get("latency_ms"),
                     **{f"candidate_latency": (candidate.get(cid) or {}).get("latency_ms")}})
    return rows, counts


def latencies(rows: list[dict], field: str) -> dict[str, Any]:
    vals = [r[field] for r in rows if isinstance(r.get(field), (int, float))]
    if not vals:
        return {"n": 0}
    ordered = sorted(vals)
    return {"n": len(vals), "p50": round(statistics.median(ordered), 1),
            "p95": round(ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))], 1),
            "max": round(max(vals), 1)}


def render(title: str, gold: list[dict], baseline_name: str, arms: list[tuple[str, list, dict]]) -> str:
    lines = [title, "",
             f"金标 {len(gold)} 题；判定尺子 = `evaluation.score_heldout.norm`（与旧基线同源）。",
             "字面失败只是**人工复核的线索**；`回退` 与 `候选未采集` 都拦（退出码 1），但两者不是一回事。", ""]
    total_reg = 0
    total_gap = 0
    for name, rows, counts in arms:
        covered = len(rows) - counts["gap"]
        base_pass = sum(1 for r in rows if r["baseline_pass"])
        cand_pass = sum(1 for r in rows if r["candidate_pass"])
        total_reg += counts["REG"]
        total_gap += counts["gap"]
        lines += [f"### 候选臂 `{name}` vs 基线 `{baseline_name}`", "",
                  f"覆盖：候选有答复 {covered}/{len(rows)} 题（缺 {counts['gap']} 题）；"
                  f"已覆盖题上字面通过 {cand_pass}/{covered}，基线同口径 {base_pass}/{len(rows)}",
                  f"保持通过 {counts['keep']}、翻正 {counts['fix']}、**回退 {counts['REG']}**、"
                  f"保持失败 {counts['both']}、候选未采集 {counts['gap']}",
                  f"延迟（毫秒，仅采集侧自报）：基线 {latencies(rows, 'baseline_latency')}；"
                  f"候选 {latencies(rows, 'candidate_latency')}", "",
                  "| case | 类别 | 候选缺的 required | 候选命中的 forbidden | 候选答复开头 |",
                  "| --- | --- | --- | --- | --- |"]
        for r in rows:
            if r["class"] in ("keep", "gap"):
                continue
            lines.append(f"| {r['id']} | {CLASSES[r['class']]} | {r['candidate_missing'] or '—'} | "
                         f"{r['candidate_forbidden'] or '—'} | {r['candidate_head'][:48]} |")
        if not any(r["class"] != "keep" for r in rows):
            lines.append("| —（逐题零翻转） | 保持通过 | — | — | — |")
        lines.append("")
    lines.append(f"合计回退 = **{total_reg}**，合计候选未采集 = **{total_gap}**"
                 + ("，闸门放行。" if total_reg + total_gap == 0
                    else "，闸门拦截（退出码 1）。缺题不是回退，但没测过就不能放行。"))
    return "\n".join(lines)


def main(args: argparse.Namespace) -> int:
    gold = [json.loads(l) for l in
            Path(args.gold).read_text(encoding="utf-8").splitlines() if l.strip()]
    baseline = rows_by_id(Path(args.baseline))
    arms = []
    for cand in args.candidate:
        rows, counts = compare(gold, baseline, rows_by_id(Path(cand)), Path(cand).stem)
        arms.append((Path(cand).stem, rows, counts))
    text = render(args.title, gold, Path(args.baseline).stem, arms)
    print(text)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"\n→ {out}")
    # 回退拦，缺题也拦：部分覆盖的臂什么都没证明，不能因为"没看到回退"就当通过。
    return 1 if any(counts["REG"] or counts["gap"] for _n, _r, counts in arms) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="per-question old-vs-new heldout gate")
    parser.add_argument("gold", help="gold jsonl with required/forbidden per case")
    parser.add_argument("--baseline", required=True, help="answer jsonl of the baseline arm")
    parser.add_argument("--candidate", action="append", required=True,
                        help="answer jsonl of a candidate arm (repeatable)")
    parser.add_argument("-o", "--out", default="", help="write the markdown table here")
    parser.add_argument("--title", default="留出集逐题差异（旧版基线 vs 候选臂）")
    sys.exit(main(parser.parse_args()))
