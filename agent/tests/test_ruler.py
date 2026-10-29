# ruff: noqa: RUF001
"""The measurement itself is part of the product: the collector must score with one ruler.

``run_ab_live.literal_score`` used to strip only ``* _ ``, while the project's canonical
``score_heldout.norm`` also strips whitespace and ``>``. On the frozen off-baseline that
one-line difference reported 32/36 where the committed checklist says 33/36 — a phantom
one-case regression caused by "1 个工作日" being written with a space. Any A/B diff built on
two different rulers is meaningless, so the agreement is pinned here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent.evaluation.run_ab_live import literal_score, norm

ROOT = Path(__file__).resolve().parents[2]
GOLD = ROOT / "python_agent" / "evaluation" / "gold_heldout_v1.jsonl"
BASELINE = ROOT / "python_agent" / "evaluation" / "runs" / "ho_off_r4.jsonl"
CHECKLIST = ROOT / "python_agent" / "evaluation" / "runs" / "ho_off_r4_checklist.jsonl"

BASELINE_MISSING = not (GOLD.exists() and BASELINE.exists() and CHECKLIST.exists())


def canonical_norm(value: str) -> str:
    """Re-implemented from python_agent/evaluation/score_heldout.py:13 on purpose."""
    return re.sub(r"[\s*_`>]", "", str(value))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_norm_agrees_with_the_projects_own_ruler() -> None:
    for sample in ["签收后 **7 天**内", "1 个工作日内发起退款", "> 以店铺公示为准", "满 99 元包邮"]:
        assert norm(sample) == canonical_norm(sample), sample


def test_whitespace_emphasis_does_not_change_the_verdict() -> None:
    case = {"required": ["1个工作日"], "forbidden": []}
    spaced = literal_score("退货验收通过后 **1 个工作日内**发起退款", case)
    assert spaced["literal_pass"] is True, spaced["required_missing"]


def test_empty_answer_is_not_a_pass_when_gold_has_no_required_terms() -> None:
    case = {"required": [], "forbidden": []}
    assert literal_score("", case)["literal_pass"] is False
    assert literal_score("  \n", case)["literal_pass"] is False


@pytest.mark.skipif(BASELINE_MISSING, reason="frozen off-baseline files not present in this checkout")
def test_frozen_off_baseline_reproduces_the_committed_checklist() -> None:
    """Scoring the frozen answers must give the committed 33/36 with zero per-case flips."""
    gold = {row["id"]: row for row in read_jsonl(GOLD)}
    committed = {row["id"]: row for row in read_jsonl(CHECKLIST)}
    answers = read_jsonl(BASELINE)

    verdicts = {row["id"]: literal_score(row["answer"], gold[row["id"]])["literal_pass"] for row in answers}
    passed = sum(verdicts.values())
    flips = [row["id"] for row in answers if verdicts[row["id"]] != committed[row["id"]]["literal_pass"]]

    assert passed == 33 == sum(r["literal_pass"] for r in committed.values()), f"got {passed}/36"
    assert flips == [], f"per-case disagreement with the committed checklist: {flips}"
