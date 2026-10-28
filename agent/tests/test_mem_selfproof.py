# ruff: noqa: RUF001
"""长对话两臂的「数据自证性」——两行数据到底是不是同一段对话，失手能不能翻回每一轮。

为什么要钉这个文件：§6f 那张逐题对照表只回答"探针答没答对"，它的前提（两臂跑的是同一批话、
每一轮说过什么留在数据里）此前**只在代码结构上成立**——两个采集器都 import 同一个
`memory_ab_live.cases()`，但落盘的 jsonl 里连填充轮的原话都没有，新臂连 `thread` 都没写。
读者只能信"应该是一样的"。这个文件把"一样"变成可对账的字段，并把三类结论分开钉住：

* `proven`   两臂指纹相同、问法相同、轮数相同、逐轮转写齐备、都有 thread；
* `blind`    字段压根没写（旧数据）——**这不是回退，是这一批数据只能证明探针那一行**；
* `conflict` 两臂都写了却对不上——那不是同一对话，对照表没有意义。

最后一条测试读真实采集件，钉住"今天这两臂仍是不可证 10 / 冲突 0"，
这样重采之后若真出现冲突或全可证，必须有测试先响。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.evaluation.mem_arms_diff import (main, render_proof, render_transcripts,
                                            rows_by_id, selfproof)
from agent.evaluation.mem_corpus import (ANSWER_HEAD_CHARS, fingerprint, script,
                                         transcript_complete, turn_row)
from agent.evaluation.run_ab_live import compact_ledger

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "agent" / "evaluation" / "runs"
OFF_ARTIFACT = RUNS / "legacy_mem_dsv41.jsonl"
ON_ARTIFACT = RUNS / "live_mem_dsv41.jsonl"
MOCK_ARTIFACT = RUNS / "mock_mem.jsonl"
ARTIFACTS_MISSING = not (OFF_ARTIFACT.exists() and ON_ARTIFACT.exists())

CASE = {"id": "mem-01", "probe": "我最开始那单的抬头是哪家？",
        "turns": ["我的订单ORD-1一直没发货", "实付296元，退货运费谁出", "抬头是星辰科技",
                  "你们包邮门槛是多少", "退款几个工作日到账"]}


def proven_off(**over) -> dict:
    return _row("t_demo:legmem-mem-01-abc123", fingerprint(CASE), over)


def proven_on(**over) -> dict:
    return _row("t_demo:v2mem-mem-01-def456", fingerprint(CASE), over)


def _row(thread: str, sha: str, over: dict) -> dict:
    per = [turn_row(i, text, f"答复{i}" * 50, latency_ms=1000 + i,
                    extra={"outcome": "answered", "roles": ["order"]})
           for i, text in enumerate(CASE["turns"])]
    row = {"id": CASE["id"], "thread": thread, "script_sha": sha, "probe": CASE["probe"],
           "turns": len(CASE["turns"]) + 1, "per_turn": per,
           "answer": "您那单抬头是星辰科技", "expect": ["ORD-1", "星辰科技"]}
    row.update(over)
    return row


def test_fingerprint_follows_every_turn_that_was_actually_sent() -> None:
    assert script(CASE)[-1] == CASE["probe"]                      # 探针也算进脚本，但答复在行内
    assert fingerprint(CASE) == fingerprint(dict(CASE))            # 稳定，可跨臂对账
    changed = dict(CASE, turns=[*CASE["turns"][:-1], "客服工作时间是几点到几点"])
    assert fingerprint(changed) != fingerprint(CASE)               # 换一句填充就变 ⇒ 两臂没跑同一段话


def test_turn_row_keeps_the_question_verbatim_and_truncates_only_the_answer() -> None:
    row = turn_row(3, CASE["turns"][3], "答" * 400, latency_ms=1234.567,
                   extra={"outcome": "answered", "error": "", "roles": []})
    assert row["user"] == CASE["turns"][3]          # 原话全文：指针"埋针到底埋在第几轮"就靠它
    assert row["answer_head"] == "答" * ANSWER_HEAD_CHARS and row["answer_chars"] == 400
    assert row["latency_ms"] == 1234.6               # 单轮墙钟（整条对话另有 latency_ms）
    assert "error" not in row and "roles" in row     # 空字段不留，避免"没报错"与"没记"同形


def test_transcript_complete_separates_old_shape_from_missing_turns() -> None:
    assert transcript_complete(proven_on()) == (5, 5)
    legacy_shape = {"turns": 6, "per_turn": [{"turn": i, "error": "", "latency_ms": 70.0}
                                             for i in range(5)]}
    assert transcript_complete(legacy_shape) == (0, 5)             # 旧数据：有行数、没有原话
    assert transcript_complete({"turns": 6, "per_turn": [{"turn": 0, "user": "a"}]}) == (1, 5)
    assert transcript_complete(None) == (0, 0)                     # 没采到 ≠ 采了但没记


def test_selfproof_labels_identical_corpora_as_proven_and_a_sha_mismatch_as_conflict() -> None:
    off, on = {"mem-01": proven_off()}, {"mem-01": proven_on()}
    rows, counts = selfproof(off, on)
    assert counts == {"proven": 1, "blind": 0, "conflict": 0}, counts
    assert rows[0]["status"] == "proven" and rows[0]["reasons"] == []

    skew = {"mem-01": proven_off(script_sha="0123456789ab")}
    rows, counts = selfproof(skew, on)
    assert counts["conflict"] == 1 and "语料指纹不一致" in rows[0]["reasons"]

    no_thread = {"mem-01": proven_off(thread="")}
    rows, counts = selfproof(no_thread, on)
    assert counts == {"proven": 0, "blind": 1, "conflict": 0}      # 没 thread 就回查不了记账
    assert rows[0]["reasons"] == ["缺 thread 键"]

    partial = proven_on()
    partial["per_turn"] = partial["per_turn"][:3]     # 写了又缺：这才是"转写不完整"
    rows, counts = selfproof(off, {"mem-01": partial})
    assert counts["proven"] == 0 and counts["conflict"] == 0 and counts["blind"] == 1
    assert rows[0]["reasons"] == ["新臂逐轮转写不完整(3/5)"], rows[0]["reasons"]


def test_missing_fields_are_blind_not_regression_and_can_gate_on_demand() -> None:
    old_off = {"mem-01": {"id": "mem-01", "probe": CASE["probe"], "turns": 6,
                          "per_turn": [{"turn": i, "error": "", "latency_ms": 1.0}
                                       for i in range(5)],
                          "answer": "…", "expect": []}}
    rows, counts = selfproof(old_off, {"mem-01": proven_on()})
    assert counts == {"proven": 0, "blind": 1, "conflict": 0}
    # 空 vs 有值 ≠ 两臂在比不同的事：那只证明"旧臂没落这批字段"，归类必须停在不可证（陷阱 #1 的同族）。
    assert rows[0]["reasons"] == ["缺语料指纹", "旧臂缺逐轮转写"], rows[0]["reasons"]
    table = "\n".join(render_proof(rows, counts))
    assert "不可证（缺自证材料）" in table
    assert "不是回退" in table                       # 分类不能让人把"没记"读成"测出问题了"


def test_require_selfproof_flag_blocks_only_when_asked(tmp_path: Path) -> None:
    off_p = tmp_path / "off.jsonl"
    on_p = tmp_path / "on.jsonl"
    for path, row in ((off_p, proven_off()), (on_p, proven_on())):
        path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    args = lambda **kw: type("A", (), dict(off=str(off_p), on=str(on_p), out=str(tmp_path / "d.md"),
                                           transcripts=str(tmp_path / "t.md"), **kw))()

    assert main(args(require_selfproof=False)) == 0        # 默认：自证性只报告，不改门禁
    assert main(args(require_selfproof=True)) == 0         # 全可证：加了门禁也放行

    blind_on = proven_on()
    blind_on.pop("script_sha")
    blind_on["per_turn"] = [{"turn": i, "outcome": "answered"} for i in range(5)]
    on_p.write_text(json.dumps(blind_on, ensure_ascii=False) + "\n", encoding="utf-8")
    assert main(args(require_selfproof=False)) == 0        # 不可证≠回退，门禁默认不动
    assert main(args(require_selfproof=True)) == 1         # 重采后的臂该用这条把它挡在门外


def test_transcript附录把两臂并排且缺字段留空(tmp_path: Path) -> None:
    text = render_transcripts({"mem-01": proven_off()}, {"mem-01": proven_on()})
    assert CASE["turns"][3] in text                      # 用户原话逐轮可翻
    assert "答复3" in text and "| 探针 |" in text
    half = proven_off()
    half["per_turn"] = half["per_turn"][:2]
    assert render_transcripts({"mem-01": half}, {"mem-01": proven_on()}).count("| 0 |") == 1


def test_transcript行不会被答复里的换行劈成半行() -> None:
    """留档工具自己把数据弄脏，比不修更糟：第一版没转义换行，mem-01 第 0 行的三段换行
    把整行推到下一行去，看上去像"旧臂那一格是空的"。"""
    row = proven_on()
    row["per_turn"][1]["answer_head"] = "第一段\n第二段 | 带竖线\n第三段"
    text = render_transcripts({"mem-01": proven_off()}, {"mem-01": row})
    lines = [line for line in text.splitlines() if line.startswith("| 1 |")]
    assert len(lines) == 1, lines
    assert "第一段 ⏎ 第二段 \\| 带竖线 ⏎ 第三段" in lines[0]
    assert "\n| | " not in text                       # 不该出现被换行撕出来的碎行


def test_compact_ledger_keeps_below_trigger_apart_from_a_real_summary_failure() -> None:
    """水位线没动有两种原因，长得一样、结论完全不同：没到触发线 vs 摘要这一路失败了。

    §6f 以前只有 ``watermark`` 一个数，把 4 例 0 一律写成"并发限流"；串行重采（r3）仍有 3 例 0，
    那句解释就站不住了。从这一版起行内带 ``compact_ledger``，0 水位线必须能对回 ``fail_reasons``。
    """
    events = ([{"name": "skipped", "reason": ""}] * 10
              + [{"name": "skipped", "reason": "summary_model_failed", "absorbed": 0},
                 {"name": "compacted", "absorbed": 8, "restored": 2}])
    assert compact_ledger(events) == {"turns": 12, "compacted": 1, "below_trigger": 10,
                                      "failed": 1,
                                      "fail_reasons": {"summary_model_failed": 1},
                                      "absorbed": 8, "restored": 2}
    quiet = compact_ledger([{"name": "skipped", "reason": ""}] * 12)
    assert quiet["failed"] == 0 and quiet["below_trigger"] == 12   # 全没到触发线 ≠ 摘要坏了


@pytest.mark.skipif(not MOCK_ARTIFACT.exists(), reason="mock memory arm not collected")
def test_mock_arm_row_carries_an_attributable_compact_ledger() -> None:
    """采集件自证的第二层：``absorbed`` 合计必须等于水位线，否则这个字段本身在说谎。"""
    row = json.loads(MOCK_ARTIFACT.read_text(encoding="utf-8").splitlines()[0])
    ledger = row.get("compact_ledger") or {}
    assert ledger, "mock 臂这一行没有 compact_ledger——采集器旁路没接上"
    assert ledger["absorbed"] == row["memory_state"]["watermark"], (ledger, row["memory_state"])
    assert ledger["compacted"] + ledger["below_trigger"] + ledger["failed"] == ledger["turns"]


@pytest.mark.skipif(ARTIFACTS_MISSING, reason="collected real-model memory arms not present")
def test_today_仍是不可证_batch_and_that_is_recorded_not_hidden() -> None:
    """§6f 的两臂是在这批字段之前采的：数据自证性=10 不可证，冲突 0（诚实钉住，不许漂成"可证"）。"""
    rows, counts = selfproof(rows_by_id(OFF_ARTIFACT), rows_by_id(ON_ARTIFACT))
    assert counts["conflict"] == 0 and len(rows) == 10
    assert counts["blind"] == 10, counts
    assert all(not r["sha_off"] and not r["sha_on"] for r in rows)
