# ruff: noqa: RUF001
"""Measure the part of latency and cost the graph actually controls — no model needed.

The live A/B arms are blocked on the model gateway, so answer quality, end-to-end latency
and billed tokens cannot be collected. But two drivers of cost are properties of *this*
graph and are measurable today:

1. **编排与持久化开销** — wall time per turn with an instantly-answering scripted model.
   This is node execution, reducers, review, compaction and checkpoint writes, with model
   time removed. It is the overhead the new graph *adds*, so it is the honest number to
   publish even though it excludes inference.
2. **喂给模型的上下文规模** — characters/messages per turn, across three regimes:
   ``full_transcript`` (everything after the watermark), ``window_12`` (the old
   ``recent_messages(limit=12)`` behaviour) and ``watermark`` (production: banner + tail +
   repaired anchors). Input tokens grow with this, so its shape over a 40-turn conversation
   is the cost story the memory design is responsible for.

Both are labelled as what they are: measured structure, not model performance. Do not
read row 1 as "latency" or row 2 as "tokens".

Usage:
    python agent/evaluation/measure_overhead.py --turns 40
    python agent/evaluation/measure_overhead.py --turns 40 --postgres   # in-container, real saver
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for entry in (str(ROOT), str(ROOT / "python_agent")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from langgraph.checkpoint.memory import InMemorySaver

from agent import memory
from agent.deps import SupportDeps
from agent.fakes import ScriptedSupportModel, offline_deps
from agent.service import SupportService

TENANT = "t_demo"
CONTEXT_WINDOW = 12

# Three context regimes. ``window_12`` is the old path's behaviour, not a straw man:
# app.agent.faq_agent.recent_messages(limit=12) keeps the last 12 messages and nothing else.
ARMS: dict[str, dict[str, Any]] = {
    "full_transcript": {"compact_trigger": 10 ** 9, "compact_keep": 6, "context_window": 0},
    "window_12": {"compact_trigger": 10 ** 9, "compact_keep": 6, "context_window": CONTEXT_WINDOW},
    "watermark": {"compact_trigger": 12, "compact_keep": 6, "context_window": CONTEXT_WINDOW},
}

_QUIPS = ["我的订单什么时候到", "退货运费谁承担", "能开发票吗", "什么时候发货", "这个能便宜点吗",
          "帮我查下物流", "支持货到付款吗", "多久能到", "怎么申请退款", "有保修吗"]


def turns(count: int) -> list[str]:
    return [f"{_QUIPS[index % len(_QUIPS)]}（第{index + 1}问）" for index in range(count)]


def make_service(arm: str, checkpointer: Any) -> SupportService:
    async def deps_for_tenant(_tenant_id: str) -> SupportDeps:
        # keep_anchors so the summary keeps real text: otherwise the banner shrinks to
        # nothing and the context-size comparison would flatter the watermark arm.
        deps, _model, _events, _captured = offline_deps(
            model=ScriptedSupportModel(summary_mode="keep_anchors"))
        return deps

    return SupportService(deps_for_tenant, checkpointer=checkpointer, graph_kwargs=dict(ARMS[arm]))


async def measure(arm: str, messages: list[str], checkpointer: Any) -> list[dict]:
    service = make_service(arm, checkpointer)
    thread = f"{TENANT}:ovh-{arm}"
    graph = await service.graph_for(TENANT)
    rows = []
    for index, message in enumerate(messages):
        started = time.perf_counter()
        await service.run_turn(TENANT, thread, message, turn_id=f"O{index}")
        elapsed = (time.perf_counter() - started) * 1000.0
        values = (await graph.aget_state({"configurable": {"thread_id": f"support_v2:{thread}"}})).values
        fed = memory.model_context(values, window=ARMS[arm]["context_window"])
        rows.append({"arm": arm, "turn": index, "latency_ms": round(elapsed, 2),
                     "fed_chars": sum(len(memory.message_text(m)) for m in fed),
                     "fed_messages": len(fed),
                     "transcript_messages": len(memory.visible_messages(values)),
                     "summary_chars": len(values.get("summary", "")),
                     "watermark": int(values.get("summary_watermark", 0) or 0)})
    return rows


def summarise(rows: list[dict]) -> dict:
    arms = {}
    for arm in ARMS:
        mine = [r for r in rows if r["arm"] == arm]
        if not mine:
            continue
        lat = sorted(r["latency_ms"] for r in mine)
        chars = [r["fed_chars"] for r in mine]
        arms[arm] = {
            "turns": len(mine),
            "latency_ms_p50": round(statistics.median(lat), 2),
            "latency_ms_p95": round(lat[max(0, int(len(lat) * 0.95) - 1)], 2),
            "latency_ms_max": round(lat[-1], 2),
            "fed_chars_first": chars[0], "fed_chars_last": chars[-1],
            "fed_chars_peak": max(chars), "fed_chars_mean": round(statistics.fmean(chars)),
            "fed_chars_growth": round((chars[-1] / chars[0]) if chars[0] else 0, 2),
            "transcript_messages_last": mine[-1]["transcript_messages"],
            "summary_chars_last": mine[-1]["summary_chars"],
        }
    return arms


async def drop_threads(checkpointer: Any) -> dict[str, int]:
    """Delete exactly the threads this script created, and prove it did.

    The measurement writes ~16 checkpoint rows per turn per arm; leaving those in the
    live ``cs_agent_db`` would be an unannounced write to production data. Counting uses
    an explicit ``async for`` because ``alist()`` is an async generator -- ``sum(1 async
    for ...)`` is a TypeError, and an exception here would silently skip the cleanup.
    """
    removed = {}
    for arm in ARMS:
        key = f"support_v2:{TENANT}:ovh-{arm}"

        async def count(thread_key: str) -> int:
            # thread_key is a parameter, not a closed-over loop variable: the count is
            # taken twice per iteration and late binding would be a latent footgun.
            total = 0
            async for _ in checkpointer.alist({"configurable": {"thread_id": thread_key}}):
                total += 1
            return total

        before = await count(key)
        await checkpointer.adelete_thread(key)
        removed[arm] = before
        left = await count(key)
        if left:
            raise RuntimeError(f"cleanup failed for {key}: {left} rows remain")
    return removed


async def main(args: argparse.Namespace) -> None:
    messages = turns(args.turns)
    if args.postgres:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(os.environ["POSTGRES_DSN"]) as saver:
            await saver.setup()
            rows = [row for arm in ARMS for row in await measure(arm, messages, saver)]
            removed = await drop_threads(saver)
    else:
        rows = [row for arm in ARMS for row in await measure(arm, messages, InMemorySaver())]
        removed = {}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    summary = summarise(rows)
    print(f"\n口径：模型即刻返回的脚本化模型 + {'真实 AsyncPostgresSaver' if args.postgres else 'InMemorySaver'}；"
          f"{args.turns} 轮/臂。latency 只含编排与持久化，不含推理；fed_chars 是喂给模型的上下文字符数。\n")
    header = ("arm", "p50ms", "p95ms", "maxms", "首字", "末字", "峰值", "增长倍数", "转写条数", "摘要字")
    print("{:<17}{:>8}{:>8}{:>8}{:>7}{:>7}{:>7}{:>10}{:>9}{:>8}".format(*header))
    for arm, metrics in summary.items():
        print("{:<17}{:>8}{:>8}{:>8}{:>7}{:>7}{:>7}{:>10}{:>9}{:>8}".format(
            arm, metrics["latency_ms_p50"], metrics["latency_ms_p95"], metrics["latency_ms_max"],
            metrics["fed_chars_first"], metrics["fed_chars_last"], metrics["fed_chars_peak"],
            metrics["fed_chars_growth"], metrics["transcript_messages_last"],
            metrics["summary_chars_last"]))

    peak_watermark = summary.get("watermark", {}).get("fed_chars_peak", 0)
    peak_full = summary.get("full_transcript", {}).get("fed_chars_peak", 0)
    if peak_full:
        print(f"\n40 轮末尾：水位线上下文峰值 {peak_watermark} 字 vs 全量转写 {peak_full} 字 "
              f"→ 压缩到 {peak_watermark / peak_full:.0%}（相对全量）；"
              f"vs 旧路径 window_12 峰值 {summary.get('window_12', {}).get('fed_chars_peak', 0)} 字。")
    if removed:
        print("清理自检（行数=删除前该线程的检查点行数，删除后一律为 0）："
              + ", ".join(f"{arm}={rows_removed}" for arm, rows_removed in removed.items()))
    print(f"原始数据 → {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=40)
    parser.add_argument("--postgres", action="store_true", help="use the real PostgreSQL saver")
    parser.add_argument("--out", default=str(ROOT / "agent" / "evaluation" / "runs" /
                                             "orchestration_overhead.jsonl"))
    asyncio.run(main(parser.parse_args()))
