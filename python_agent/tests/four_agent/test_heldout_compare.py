"""The heldout per-question gate must be able to see a flip -- not just report zero.

Why these tests exist: `heldout_compare.py` is meant to be the thing that says "the new arm
regressed ho-c07" when the live model arms finally get collected. A diff tool that always prints
"零翻转" would pass that job just as quietly as a correct one, so the tests deliberately inject
each class of change and assert the classification, not just the totals. The judge also reuses
`evaluation.score_heldout.norm`, which `test_ruler.py` pins against the official ruler -- a local
copy here would reintroduce §2's whitespace-invented regression.
"""

from __future__ import annotations

import json
from pathlib import Path

from evaluation.four_agent.heldout_compare import compare, latencies, render

GOLD = [
    {"id": "ho-a", "category": "policy", "required": ["7天内", "99元"], "forbidden": ["一定退款"]},
    {"id": "ho-b", "category": "order", "required": ["待发货"], "forbidden": []},
    {"id": "ho-c", "category": "policy", "required": ["包邮"], "forbidden": ["无条件退货"]},
]
BASELINE = {"ho-a": {"answer": "签收后 7天内 可申请，满 99元 可退。"},
            "ho-b": {"answer": "订单状态：待发货。", "latency_ms": 1200.0},
            "ho-c": {"answer": "配送与运费范围请以结算页说明为准。"}}   # baseline literal-fail


def rows(cand: dict):
    return compare(GOLD, BASELINE, cand, "cand")


def test_identical_answers_report_zero_flips() -> None:
    _rows, counts = rows(dict(BASELINE))
    assert counts == {"keep": 2, "fix": 0, "REG": 0, "both": 1, "gap": 0}


def test_dropping_a_required_term_is_classified_as_regression() -> None:
    cand = {"ho-a": {"answer": "签收后可以申请退款。"}, "ho-b": BASELINE["ho-b"],
            "ho-c": BASELINE["ho-c"]}
    out, counts = rows(cand)
    assert counts["REG"] == 1
    row = next(r for r in out if r["id"] == "ho-a")
    assert row["class"] == "REG" and row["candidate_missing"] == ["7天内", "99元"]


def test_forbidden_term_alone_flips_a_passing_case_to_regression() -> None:
    cand = {"ho-a": {"answer": "7天内、99元，一定退款。"}, "ho-b": BASELINE["ho-b"],
            "ho-c": BASELINE["ho-c"]}
    _out, counts = rows(cand)
    assert counts["REG"] == 1


def test_whitespace_inside_a_required_term_is_not_a_regression() -> None:
    """§2's incident: the ruler strips whitespace, so re-flowed text must stay a keep."""
    cand = {"ho-a": {"answer": "签收后 **7 天内** 可申请，满 **99 元** 可退。"},
            "ho-b": BASELINE["ho-b"], "ho-c": BASELINE["ho-c"]}
    _out, counts = rows(cand)
    assert counts["REG"] == 0 and counts["keep"] == 2 and counts["both"] == 1


def test_fixing_a_baseline_failure_is_classified_as_improvement() -> None:
    cand = {"ho-a": BASELINE["ho-a"], "ho-b": BASELINE["ho-b"],
            "ho-c": {"answer": "本店满 89 元 包邮。"}}
    _out, counts = rows(cand)
    assert counts == {"keep": 2, "fix": 1, "REG": 0, "both": 0, "gap": 0}


def test_missing_case_is_a_coverage_gap_not_a_regression() -> None:
    cand = {"ho-a": BASELINE["ho-a"], "ho-b": BASELINE["ho-b"]}
    out, counts = rows(cand)
    row = next(r for r in out if r["id"] == "ho-c")
    assert counts["gap"] == 1 and counts["REG"] == 0     # 缺题 ≠ 回退
    assert row["class"] == "gap" and row["candidate_pass"] is False
    # 但缺题依然拦闸门：只测了一半的臂不能算"没发现回退所以通过"
    assert "候选未采集 = **1**" in render("t", GOLD, "base", [("cand", out, counts)])


def test_render_and_latency_summary() -> None:
    out, counts = rows(dict(BASELINE))
    text = render("t", GOLD, "base", [("cand", out, counts)])
    assert "合计回退 = **0**" in text                  # identical answers really do flip nothing
    assert "保持失败" in text                          # ... while the still-failing case is listed
    assert latencies(out, "baseline_latency")["p50"] == 1200.0
    assert latencies(out, "candidate_latency") == {"n": 1, "p50": 1200.0,
                                                   "p95": 1200.0, "max": 1200.0}


def test_gate_exit_code_is_driven_by_regressions(tmp_path: Path) -> None:
    from evaluation.four_agent import heldout_compare as hc

    gold_path = tmp_path / "gold.jsonl"
    gold_path.write_text("\n".join(json.dumps(g) for g in GOLD), encoding="utf-8")
    base_path = tmp_path / "base.jsonl"
    base_path.write_text("\n".join(json.dumps(v | {"id": k}) for k, v in BASELINE.items()),
                         encoding="utf-8")
    bad_path = tmp_path / "bad.jsonl"
    bad = {"ho-a": {"id": "ho-a", "answer": "抱歉，无法确认。"},
           "ho-b": {"id": "ho-b", "answer": "待发货"}, "ho-c": {"id": "ho-c", "answer": "包邮"}}
    bad_path.write_text("\n".join(json.dumps(v) for v in bad.values()), encoding="utf-8")

    class Args:
        gold, baseline, out, title = str(gold_path), str(base_path), "", "t"
        candidate = [str(bad_path)]

    assert hc.main(Args()) == 1                       # 回退 ho-a → block
    Args.candidate = [str(base_path)]
    assert hc.main(Args()) == 0                       # 自比零翻转 → pass
