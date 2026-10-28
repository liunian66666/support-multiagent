# ruff: noqa: RUF001
"""Per-case (逐题) diff for the two *real-model* long-chat memory arms (report §6f).

`run_ab_live.py mem` (new four-Agent graph) and `run_legacy_heldout.py --arm mem` (old path)
both write one row per conversation with the probe answer, the needle list and the ruler verdict.
This script joins them by case id and answers the only question acceptance criterion 3 cares about:

    改善 / 回退 / 都失败 / 波动 —— 逐例，且每例说明"针是半丢还是全丢"。

Four things this deliberately refuses to hide:

* The ruler is re-derived here from ``evaluation.score_heldout.norm`` instead of trusting the
  stored ``containment`` field, so a row cannot pass because a collector's own string-matching
  changed shape (pitfall #2/#21).
* ``watermark == 0`` on the ON arm is printed as a **measurement-contamination flag**, not as
  "memory broke": that shape came from running both arms against one key concurrently, where the
  *auxiliary* summarisation call fails first and ``memory.compact`` then keeps every message
  (pitfall #23). A table that only showed 5/10 would have let that ride.
* ``route`` from the OFF arm is shown per case, because the old path's failures are mostly
  routing (the probe never reaches history) rather than forgetting — different cause, different fix.
* **数据自证性**（``selfproof``）：两臂真的跑了同一段对话吗？每一轮说过什么、模型当时答了什么，
  原始数据里翻得出来吗？能翻回来的那几例记 ``proven``，字段没落盘的记 ``blind``（**不是回退，
  是这一批数据只能证明探针那一行**），两臂都写了却对不上的记 ``conflict``（那就是"没在比同一件事"）。
  默认只报告、不改门禁；``--require-selfproof`` 才把 blind/conflict 也拦成退出码 1。

Usage:
    python agent/evaluation/mem_arms_diff.py \
        --off agent/evaluation/runs/legacy_mem_dsv41.jsonl \
        --on  agent/evaluation/runs/live_mem_dsv41.jsonl \
        -o    agent/evaluation/runs/mem_diff_dsv41_off_vs_on.md \
        --transcripts agent/evaluation/runs/mem_transcripts_dsv41.md
Exit code: 0 when the ON arm regresses on nothing and covers every OFF case, 1 otherwise
(missing coverage blocks too: an uncollected case proves nothing, but it is not a 回退).
With ``--require-selfproof`` an unprovable or contradictory corpus also blocks (exit 1).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for entry in (str(ROOT), str(ROOT / "python_agent")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from evaluation.score_heldout import norm      # the official ruler; do not re-implement

try:
    from agent.evaluation.mem_corpus import transcript_complete
except ImportError:                              # invoked as a bare script
    from pathlib import Path as _P
    import importlib.util as _iu

    _spec = _iu.spec_from_file_location("mem_corpus",
                                        _P(__file__).resolve().parent / "mem_corpus.py")
    _mod = _iu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)             # type: ignore[attr-defined]
    transcript_complete = _mod.transcript_complete

CLASSES = {"fix": "翻正（旧失败 → 新通过）", "REG": "回退（旧通过 → 新失败）",
           "keep": "保持通过", "both": "保持失败", "gap": "该臂未采集"}


def rows_by_id(path: Path) -> dict[str, dict]:
    return {r["id"]: r for r in (json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip())}


def judge(row: dict | None) -> tuple[bool | None, list[str]]:
    """(all_needles_hit, missed_needles) recomputed with the shared norm().

    ``None`` means the case was not collected on that arm — absence is coverage, not a score.
    """
    if not row:
        return None, []
    text = norm(row.get("answer") or "")
    needles = list(row.get("expect") or [])
    missed = [t for t in needles if norm(t) not in text]
    return (bool(needles) and not missed), missed


def tokens(row: dict | None) -> int:
    """Billed tokens for one conversation, whatever shape the collector wrote.

    The two arms grew their accounting separately: the new-graph row carries a
    ``tokens_total`` dict (``input/output/total``), the old-path row carries ``usage.total_tokens``.
    A bare int is also accepted so the helper stays useful if either shape is flattened later.
    """
    if not row:
        return 0
    for key in ("tokens_total", "usage"):
        box = row.get(key)
        if isinstance(box, dict):
            for field in ("total_tokens", "total"):
                if isinstance(box.get(field), int):
                    return int(box[field])
            return int(box.get("input_tokens") or 0) + int(box.get("output_tokens") or 0)
        if isinstance(box, int):
            return box
    return 0


def compare(off: dict[str, dict], on: dict[str, dict]) -> tuple[list[dict], dict[str, int]]:
    counts = {"fix": 0, "REG": 0, "keep": 0, "both": 0, "gap": 0}
    out = []
    for cid in sorted(set(off) | set(on)):
        o, n = off.get(cid), on.get(cid)
        off_ok, off_missing = judge(o)
        on_ok, on_missing = judge(n)
        if off_ok is None or on_ok is None:
            cls = "gap"
        elif off_ok and on_ok:
            cls = "keep"
        elif off_ok and not on_ok:
            cls = "REG"
        elif on_ok and not off_ok:
            cls = "fix"
        else:
            cls = "both"
        counts[cls] += 1
        state = (n or {}).get("memory_state") or {}
        ledger = (n or {}).get("compact_ledger") or {}
        out.append({
            "id": cid, "class": cls,
            "off_pass": off_ok, "on_pass": on_ok,
            "off_missing": off_missing, "on_missing": on_missing,
            "needles": " / ".join((n or o or {}).get("expect") or []),
            "route": (o or {}).get("route", ""),
            "watermark": state.get("watermark"),
            "summarized": state.get("summarized"),
            "verbatim": state.get("verbatim"),
            "total": state.get("total"),
            "off_tokens": tokens(o), "on_tokens": tokens(n),
            # 摘要这一路到底做了什么：只有带 compact_ledger 的新采集件才有（旧批次=None，
            # 表格退回以前那种"只知道水位线是 0"的写法）。
            "on_failed": ledger.get("failed"), "on_below": ledger.get("below_trigger"),
            "on_fail_reasons": ledger.get("fail_reasons") or {},
            "on_absorbed": ledger.get("absorbed"),
            "off_latency": (o or {}).get("latency_ms"), "on_latency": (n or {}).get("latency_ms"),
        })
    return out, counts


def _yes(v: bool | None) -> str:
    return "—" if v is None else ("✓" if v else "✗")


def selfproof(off: dict[str, dict], on: dict[str, dict]) -> tuple[list[dict], dict[str, int]]:
    """逐例回答一个前置问题：这两行数据证的**是不是同一段对话**，以及失手能不能复核。

    Three outcomes per case, deliberately not merged into the pass/fail counts:

    * ``conflict`` — both arms stamped a corpus fingerprint / probe / turn count and they
      **disagree**. The two rows are then not the same conversation and no comparison between
      them means anything; fix the collectors before talking about scores.
    * ``proven``   — same fingerprint, same probe, same turn count, complete per-turn transcripts
      on both sides and a thread key on both sides.
    * ``blind``    — the material to prove it was never written (every artifact collected before
      this check existed) or was written only half-way. The conclusion stands on the probe answer
      alone; say so instead of pretending the table is self-evidencing.

    Identity 与 material 分开记：前者能证伪对照表，后者只能证"这一批数据没自证过"。把缺字段算成
    冲突，会让旧数据一夜之间变成"两臂在比不同的事"——那是凭空造出一批假回退。
    """
    counts = {"proven": 0, "blind": 0, "conflict": 0}
    out = []
    for cid in sorted(set(off) | set(on)):
        o, n = off.get(cid), on.get(cid)
        sha_o, sha_n = (o or {}).get("script_sha", ""), (n or {}).get("script_sha", "")
        identity: list[str] = []
        material: list[str] = []
        if o and n:
            if sha_o and sha_n and sha_o != sha_n:
                identity.append("语料指纹不一致")
            elif not (sha_o and sha_n):
                material.append("缺语料指纹")
            if norm(o.get("probe") or "") != norm(n.get("probe") or ""):
                identity.append("探针问法不一致")
            if (o.get("turns") or 0) != (n.get("turns") or 0):
                identity.append("轮数不一致")
        else:
            material.append("该例只有一臂采集")
        t_off, w_off = transcript_complete(o)
        t_on, w_on = transcript_complete(n)
        for side, got, want in (("旧", t_off, w_off), ("新", t_on, w_on)):
            if not (o if side == "旧" else n):
                continue
            if want and got == 0:
                material.append(f"{side}臂缺逐轮转写")
            elif got < want:
                material.append(f"{side}臂逐轮转写不完整({got}/{want})")
        if not (o or {}).get("thread", True) or not (n or {}).get("thread", True):
            material.append("缺 thread 键")
        if identity:
            status, reasons = "conflict", identity + material
        elif material:
            status, reasons = "blind", material
        else:
            status, reasons = "proven", []
        counts[status] += 1
        out.append({"id": cid, "status": status, "sha_off": sha_o, "sha_on": sha_n,
                    "turns_off": (o or {}).get("turns"), "turns_on": (n or {}).get("turns"),
                    "transcript_off": (t_off, w_off), "transcript_on": (t_on, w_on),
                    "thread_off": bool((o or {}).get("thread")),
                    "thread_on": bool((n or {}).get("thread")),
                    "reasons": reasons})
    return out, counts


PROOF_LABELS = {"proven": "可自证", "blind": "不可证（缺自证材料）",
               "conflict": "冲突（不是同一对话）"}


def render_proof(rows: list[dict], counts: dict[str, int]) -> list[str]:
    lines = [
        "",
        "## 数据自证性（这两行是不是同一段对话？失手能不能复核？）",
        "",
        "对照表只回答“探针答没答对”；这一节回答它的**前提**——两臂跑的确实是同一批话，"
        "而且每一轮说过什么、模型当时答了什么留在原始数据里可翻。"
        "语料指纹 = `agent/evaluation/mem_corpus.fingerprint`（12 句话按发送顺序 sha1 前 12 位），"
        "两臂同值才叫“同一对话”；`转写` = 逐轮行数/应有行数（应有 = 总轮数-1，探针答复在行内 `answer`）；"
        "`thread` = 这一例跑在哪条线程上，用来与 `token_usage`、检查点、消息表对账"
        "（采集器跑完会自清理自建线程，所以回查窗口是采集当时，不是永久）。",
        "",
        "| 例 | 语料指纹（旧｜新） | 轮数（旧/新） | 转写（旧/新） | thread（旧/新） | 结论 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        to, wo = r["transcript_off"]
        tn, wn = r["transcript_on"]
        lines.append(f"| {r['id']} | {r['sha_off'] or '—'} ｜ {r['sha_on'] or '—'} | "
                     f"{r['turns_off'] or '—'}/{r['turns_on'] or '—'} | "
                     f"{to}/{wo}｜{tn}/{wn} | "
                     f"{'✓' if r['thread_off'] else '✗'}/{'✓' if r['thread_on'] else '✗'} | "
                     f"{PROOF_LABELS[r['status']]}"
                     f"{'' if not r['reasons'] else '（' + '、'.join(r['reasons']) + '）'} |")
    lines += ["",
              f"合计：**可自证 {counts['proven']} / 不可证 {counts['blind']} / 冲突 {counts['conflict']}**。",
              "",
              "读法：**“不可证”不是回退，是这一批数据只能证明“探针那一轮的答案”——**"
              "埋针与填充轮的原话没落盘，两臂语料一致目前只能靠“两边都 import 同一个 `cases()`”"
              "这个代码结构上的承诺担保。要把它变成可查的数，就得用现在这版采集器重采（字段已就位）。",
              "“冲突”才是硬问题：两臂写的指纹/问法/轮数对不上，那张对照表就没有意义，先修采集再谈分数。"]
    return lines


def _one_line(text: str) -> str:
    """把答复压成一行并把竖线转义——否则答复里的换行会把 markdown 表格劈成半行。

    第一版没做这件事，附录里 mem-01 第 0 轮的新图答复带了三段换行，直接把整行的列错位
    （看起来像"旧臂那一格是空的"，其实是被换行推下去了）。留档工具自己把数据弄脏，
    比不修更糟。换行统一换成可见的 ⏎，正文一个字不删。
    """
    return str(text or "").replace("|", "\\|").replace("\r\n", "\n").replace("\n", " ⏎ ").strip()


def render_transcripts(off: dict[str, dict], on: dict[str, dict]) -> str:
    """逐轮转写附录：每一例把 11 轮原话 + 两臂当时的答复开头并排写出来（缺字段的例如实留空）。"""
    lines = ["# 长对话逐轮转写（旧路径臂 vs 新图臂）", "",
             "用户原话逐轮全文；答复只截前 120 字（`answer_head`）+ 该轮墙钟，"
             "探针那一轮的答复全文在各自行内 `answer`。要更全的正文按 `thread` 回检查点/消息表取，"
             "但自建线程在采集收尾时会被清理——这份附录就是留档本身。",
             "答复里的换行写成 ⏎（否则会把表格劈成半行），竖线已转义。",
             "空白列＝那一臂没落这个字段（旧数据），不是“没说话”。", ""]
    for cid in sorted(set(off) | set(on)):
        o, n = off.get(cid, {}), on.get(cid, {})
        lines += [f"## {cid} · 语料指纹 旧 {o.get('script_sha', '—')} / "
                  f"新 {n.get('script_sha', '—')} · thread 旧 {o.get('thread', '—')} / "
                  f"新 {n.get('thread', '—')}", "",
                  f"探针：{(n or o).get('probe', '')}", "",
                  "| 轮 | 用户原话 | 旧路径答复开头 | 新图答复开头 | 新图 roles |",
                  "| --- | --- | --- | --- | --- |"]
        turns = max(len(o.get("per_turn") or []), len(n.get("per_turn") or []))
        for i in range(turns):
            ro = (o.get("per_turn") or [])[i] if i < len(o.get("per_turn") or []) else {}
            rn = (n.get("per_turn") or [])[i] if i < len(n.get("per_turn") or []) else {}
            user = _one_line(rn.get("user") or ro.get("user") or "")
            lines.append(f"| {i} | {user} | {_one_line(ro.get('answer_head', ''))} | "
                         f"{_one_line(rn.get('answer_head', ''))} | "
                         f"{'、'.join(rn.get('roles') or [])} |")
        lines += ["| 探针 | %s | %s | %s | %s |" % (
            _one_line((n or o).get("probe", "")), _one_line((o.get("answer") or "")[:120]),
            _one_line((n.get("answer") or "")[:120]), "、".join(n.get("roles") or [])), ""]
    return "\n".join(lines) + "\n"


def watermark_cell(row: dict) -> str:
    """水位线那一格，顺带把"为什么是 0"能说的都说了。

    旧采集件没有 ``compact_ledger``，只能说"0 且不知道为什么"（``0 ⚠``，沿用以前的标记）；
    新采集件能分开两种 0：**摘要调用失败**（机制真的没折叠）与**一轮都没到触发线**
    （机制没坏，是配置/长度问题）。这两种在报告里过去被混成同一个"并发限流"。
    """
    wm = row.get("watermark")
    if wm is None:
        return "—"
    if wm > 0:
        return str(wm)
    failed, below = row.get("on_failed"), row.get("on_below")
    if failed is None:
        return "0 ⚠"
    reasons = "、".join(f"{k}×{v}" for k, v in (row.get("on_fail_reasons") or {}).items())
    if failed:
        return f"0 ⚠（摘要失败×{failed}{'：' + reasons if reasons else ''}）"
    return f"0 ○（{below or 0} 轮都没到触发线，摘要没坏）"


def render(rows: list[dict], counts: dict[str, int], off_name: str, on_name: str,
           proof: tuple[list[dict], dict[str, int]] | None = None) -> str:
    def agg(key: str, side: str) -> str:
        vals = [r[key] for r in rows if r.get(key)]
        if not vals:
            return "—"
        if side == "tokens":
            return f"合计 {sum(vals):,} · p50 {int(statistics.median(vals)):,}"
        # latency_ms is milliseconds on both arms; rendering it as seconds here is exactly
        # the unit slip the report keeps warning about (the 33007 "seconds" bug this script
        # first shipped), so divide and say so.
        return f"p50 {statistics.median(vals) / 1000:.1f}s"

    lines = [
        f"# 长对话记忆逐题对照（真实模型 · {off_name} vs {on_name}）",
        "",
        f"判据：探针答复必须字面含住**全部**针（订单号 + 发票抬头），尺子 = "
        f"`evaluation.score_heldout.norm`（与旧基线、与 §8g 那张 36 题表同源，此处**重新计算**，"
        f"不采信行内 `containment`）。",
        "",
        "| 例 | 判定 | 旧路径 | 新图 | 未命中针（旧 / 新） | 新图水位线 | 摘要条数 | 原文条数 | "
        "旧route | 旧token | 新token |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        miss = []
        if r["off_missing"]:
            miss.append("旧:" + "、".join(r["off_missing"]))
        if r["on_missing"]:
            miss.append("新:" + "、".join(r["on_missing"]))
        wm_cell = watermark_cell(r)
        lines.append(f"| {r['id']} | {CLASSES[r['class']]} | {_yes(r['off_pass'])} | "
                     f"{_yes(r['on_pass'])} | {'；'.join(miss) or '—'} | {wm_cell} | "
                     f"{r['summarized'] if r['summarized'] is not None else '—'} | "
                     f"{r['verbatim'] if r['verbatim'] is not None else '—'} | {r['route'] or '—'} | "
                     f"{r['off_tokens'] or '—'} | {r['on_tokens'] or '—'} |")
    lines += ["", f"合计：**翻正 {counts['fix']} / 回退 {counts['REG']} / 保持通过 {counts['keep']} / "
                  f"保持失败 {counts['both']} / 未采集 {counts['gap']}**。",
              "",
              f"成本（每条对话一整条链路，不是单轮）：旧路径 token {agg('off_tokens', 'tokens')}；"
              f"新图 token {agg('on_tokens', 'tokens')}；旧路径墙钟 {agg('off_latency', 'lat')}；"
              f"新图墙钟 {agg('on_latency', 'lat')}"
              "。行内 `latency_ms` 是**整条对话**（11 轮埋针/填充 + 探针轮）的墙钟，"
              "不是探针那一轮——单写“p50 33s”会被读成每轮 33s，这里明确单位与口径。",
              "",
              "⚠ 表里带 ⚠ 的水位线＝0：摘要没把水位线推上去，检查点仍是全量原文（`compact` 走"
              "「失败 ⇒ 一条都不删」，没丢东西）。**这一格的原因列以前是没有的**，四批数据（r1 并发 "
              "4 例、r2 4 例、r3 串行 3 例、recheck 2 例 0）就一律被写成\"两臂并发打同一把 key 被限流\""
              "（陷阱 #23 那条污染是真的，但它不是 0 的全部解释——串行照样出 0）。"
              "从带 `compact_ledger` 的采集件起，0 分两种写法：`⚠（摘要失败×N：原因）` 与 "
              "`○（N 轮都没到触发线，摘要没坏）`。旧 jsonl 没有这个字段，只能沿用 `0 ⚠`。",
              "",
              "读法提醒：`route=order` 且旧路径未命中的例子，失败在**规则路由把复述题判成订单查询、"
              "走未登录兜底句**，压根没读到历史（§6f 第 2 条）；新图未命中但水位线 > 0 的例子才是"
              "记忆／安全规则层面的事。两者修法不同，不要用同一个分数混起来。",
              ""]
    if proof:
        lines += render_proof(proof[0], proof[1]) + [""]
    gate = "放行（退出码 0）" if not counts["REG"] and not counts["gap"] else "拦截（退出码 1）"
    lines.append(f"闸门：**回退 {counts['REG']}、未采集 {counts['gap']} ⇒ {gate}**。"
                 "回退=0 才允许把这条链路往前推；未采集不是回退，但没测过就不算证明。")
    return "\n".join(lines) + "\n"


def main(args: argparse.Namespace) -> int:
    off_path, on_path = Path(args.off), Path(args.on)
    off, on = rows_by_id(off_path), rows_by_id(on_path)
    rows, counts = compare(off, on)
    proof = selfproof(off, on)
    table = render(rows, counts, off_path.name, on_path.name, proof)
    out = Path(args.out) if args.out else ROOT / "agent" / "evaluation" / "runs" / "mem_arms_diff.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(table, encoding="utf-8")
    print(table)
    print(f"原始表 → {out}")
    if args.transcripts:
        tpath = Path(args.transcripts)
        tpath.parent.mkdir(parents=True, exist_ok=True)
        tpath.write_text(render_transcripts(off, on), encoding="utf-8")
        print(f"逐轮转写附录 → {tpath}")
    blocked = counts["REG"] or counts["gap"]
    if args.require_selfproof and (proof[1]["conflict"] or proof[1]["blind"]):
        blocked = 1
        print(f"\n--require-selfproof：可自证 {proof[1]['proven']}/"
              f"不可证 {proof[1]['blind']}/冲突 {proof[1]['conflict']} ⇒ 拦截（退出码 1）。"
              "\n这一臂的质量结论目前只能靠探针那一行，语料一致性与逐轮过程没在数据里留下证据。")
    return 1 if blocked else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--off", required=True, help="旧路径臂 jsonl（run_legacy_heldout.py --arm mem）")
    parser.add_argument("--on", required=True, help="新图臂 jsonl（run_ab_live.py mem）")
    parser.add_argument("-o", "--out", default="")
    parser.add_argument("--transcripts", default="",
                        help="把逐轮转写（用户原话 + 两臂答复开头）另写一份 markdown")
    parser.add_argument("--require-selfproof", action="store_true", dest="require_selfproof",
                        help="数据自证性也进门禁：有『冲突』或『不可证』就退 1（重采后的两臂该用这个）")
    raise SystemExit(main(parser.parse_args()))
