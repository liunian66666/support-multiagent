# ruff: noqa: RUF001
"""The real-model long-chat diff (§6f) must classify each case, not just count passes.

Why this file exists: `mem_arms_diff.py` compares two arms whose rows were written by two
different collectors with two different token shapes, and its job is to keep three confusions
from being folded into one number:

* a needle that is present with different spacing must still count (official ``norm``);
* ``watermark == 0`` is printed as a **measurement flag**, not as "memory broke" — the summary
  never deletes anything on failure, so the transcript stays complete. But it is *not* the same
  thing as "concurrency contaminated the run": a serial re-collection (r3) still produced three
  zeros, so a row that carries ``compact_ledger`` now says **why** it is zero (摘要失败 vs 没到触发线),
  and a row that predates that field says ``0 ⚠`` and nothing more (pitfall #23, corrected by §6g).
* a case missing from one arm is a coverage gap, which blocks the gate but is not a 回退.

The last test reads the two collected artifacts and pins their aggregate, so a silently
regenerated artifact cannot move the headline number (5 翻正 / 0 回退) without a test failing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.four_agent.mem_arms_diff import compare, judge, render, tokens, watermark_cell

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "python_agent" / "evaluation" / "four_agent" / "runs"
OFF_ARTIFACT = RUNS / "legacy_mem_dsv41.jsonl"
ON_ARTIFACT = RUNS / "live_mem_dsv41.jsonl"
ARTIFACTS_MISSING = not (OFF_ARTIFACT.exists() and ON_ARTIFACT.exists())


def on_row(cid: str, answer: str, watermark: int = 16) -> dict:
    return {"id": cid, "answer": answer, "expect": ["ORD-1", "甲公司"],
            "memory_state": {"watermark": watermark, "summarized": watermark,
                             "verbatim": 24 - watermark, "total": 24},
            "tokens_total": {"input_tokens": 700, "output_tokens": 100, "total_tokens": 800},
            "latency_ms": 180000.0}


def off_row(cid: str, answer: str, route: str = "order") -> dict:
    return {"id": cid, "answer": answer, "expect": ["ORD-1", "甲公司"], "route": route,
            "usage": {"total_tokens": 180}, "latency_ms": 33000.0}


BOTH_HIT = "您那单 ORD-1 的发票抬头是甲公司。"
ONLY_ORDER = "那单是 ORD-1，抬头我这边不能凭记忆报。"
NEITHER = "现有店铺资料没有明确说明这个具体事项。"


def test_judge_uses_the_shared_ruler_not_raw_contains() -> None:
    """Same needles, different emphasis/whitespace: still a hit (the §2 whitespace lesson)."""
    assert judge(off_row("x", "ORD-1 的抬头是 **甲公司**")) == (True, [])
    assert judge(off_row("x", ONLY_ORDER))[1] == ["甲公司"]     # half-needle is visible per side
    assert judge(off_row("x", NEITHER)) == (False, ["ORD-1", "甲公司"])
    assert judge(None) == (None, [])                            # absent ≠ failed


def test_token_helper_reads_both_collector_shapes() -> None:
    assert tokens(on_row("a", BOTH_HIT)) == 800          # tokens_total dict
    assert tokens(off_row("a", BOTH_HIT)) == 180         # usage.total_tokens
    assert tokens({"tokens_total": 55}) == 55            # flattened int still accepted
    assert tokens(None) == 0


def test_classes_and_gate_are_per_case() -> None:
    off = {"m1": off_row("m1", NEITHER), "m2": off_row("m2", BOTH_HIT),
           "m3": off_row("m3", NEITHER), "m4": off_row("m4", BOTH_HIT)}
    on = {"m1": on_row("m1", BOTH_HIT), "m2": on_row("m2", NEITHER),
          "m3": on_row("m3", ONLY_ORDER)}                          # m4 never collected
    rows, counts = compare(off, on)
    by_id = {r["id"]: r["class"] for r in rows}
    assert by_id == {"m1": "fix", "m2": "REG", "m3": "both", "m4": "gap"}
    assert counts == {"fix": 1, "REG": 1, "keep": 0, "both": 1, "gap": 1}


def test_watermark_zero_is_flagged_as_contamination_not_failure() -> None:
    table = render(compare({"m1": off_row("m1", NEITHER)},
                           {"m1": on_row("m1", NEITHER, watermark=0)})[0],
                   {"fix": 0, "REG": 0, "keep": 0, "both": 1, "gap": 0}, "off.jsonl", "on.jsonl")
    assert "0 ⚠" in table                       # the row itself carries the flag
    assert "陷阱 #23" in table                  # and the table explains what it means
    assert "读法提醒" in table and "规则路由" in table  # old-path cause stays attributable


def test_latency_is_rendered_as_whole_conversation_seconds() -> None:
    """`latency_ms` is per conversation (11 turns + probe); a "33000s" table would be a unit lie."""
    table = render(compare({"m1": off_row("m1", NEITHER)}, {"m1": on_row("m1", BOTH_HIT)})[0],
                   {"fix": 1, "REG": 0, "keep": 0, "both": 0, "gap": 0}, "off.jsonl", "on.jsonl")
    assert "p50 33.0s" in table and "p50 180.0s" in table
    assert "整条对话" in table


def test_watermark_zero_now_says_whether_the_summary_actually_broke() -> None:
    """三种 0 不能写成同一句话：没记归因字段、记了且摘要失败、记了但一轮都没到触发线。

    r1/r2 那批把 0 全归给"两臂并发限流"，r3 串行重采仍有 3/10 是 0 —— 解释不够用了。
    带 ``compact_ledger`` 的采集件必须把两种 0 分开印出来（陷阱 #23 只说并发会污染辅助调用，
    它不是"0 的唯一原因"）。
    """
    assert watermark_cell({"watermark": 0}) == "0 ⚠"                       # 旧采集件：不知道为什么
    assert watermark_cell({"watermark": 16}) == "16"
    broken = {"watermark": 0, "on_failed": 2, "on_below": 9,
              "on_fail_reasons": {"summary_model_failed": 2}}
    assert watermark_cell(broken) == "0 ⚠（摘要失败×2：summary_model_failed×2）"
    quiet = {"watermark": 0, "on_failed": 0, "on_below": 12, "on_fail_reasons": {}}
    assert watermark_cell(quiet) == "0 ○（12 轮都没到触发线，摘要没坏）"


@pytest.mark.skipif(ARTIFACTS_MISSING, reason="collected real-model memory arms not present")
def test_collected_arms_keep_the_reported_aggregate() -> None:
    def read(path: Path) -> dict[str, dict]:
        return {r["id"]: r for r in (json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip())}

    off, on = read(OFF_ARTIFACT), read(ON_ARTIFACT)
    rows, counts = compare(off, on)
    assert len(rows) == 10 and counts["REG"] == 0 and counts["gap"] == 0
    assert counts["fix"] == 5 and counts["both"] == 5, counts
    # the four contaminated conversations must be exactly the ones the report names
    tainted = sorted(r["id"] for r in rows if r["watermark"] == 0)
    assert tainted == ["mem-02", "mem-05", "mem-06", "mem-10"], tainted
    # stored verdict vs recomputed verdict: the ruler has not drifted
    for r in rows:
        assert (r["on_pass"] is True) == bool(on[r["id"]]["all_needles_recalled"]), r["id"]
