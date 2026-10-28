# ruff: noqa: RUF001  # 报告文案含中文
"""Per-case memory measurement of the MOUNTED app-side graph, in the same schema as the four arms.

Why this exists: §6's four-arm table (frozen 10 + geometry 11) measures the memory mechanism that
lives in `agent/memory.py`. The graph that `FOUR_AGENT_GRAPH` routes to is a *different*
implementation with its own `summary` / `summary_watermark` channels, and it never calls
`memory_messages()` -- the watermark summary that the four-arm protocol evaluated is bypassed
entirely. Criterion 3 asks for per-case evidence about the memory that ships, so the same protocol
has to be run against it:

  * compaction trigger `len(messages) > 24`, retaining the last 12 (`app/multi_agent/graph.py` summarize);
  * absorbed messages go through `RemoveMessage`, i.e. they leave the graph channel -- delete-and-rebuild;
  * the merge model is re-fed the previous summary, so merges are incremental rather than blind;
  * the fed prompt is `summary[:1000] + last 12 visible messages`, with **no anchor repair**.

Same transcripts as the four arms (imported, not re-typed), same deliberately pessimistic
summariser: the merge model returns a paraphrase containing no digits, no order id and no company
name -- the worst case this phase was written to survive. Same two booleans per needle:
``in_fed_context`` (was it actually handed to the model on the probe turn) and
``in_durable_transcript`` (does it still exist in the agent's own session state at all).

Rows carry ``arm="mounted_v2"``; with ``--merge-with runs/memory_context_ab.jsonl`` they land in one
file so `diff_arms.py` renders a single 五臂逐题表.

No model calls, no real data: `InMemorySaver`, scripted merge model, business order adapter patched.
Run:
  bash agent/evaluation/run_all.sh mounted-memory
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for entry in (str(ROOT), str(ROOT / "python_agent")):
    if entry not in sys.path:
        sys.path.insert(0, entry)


def _load(rel: str, name: str):
    """Import a sibling evaluation script by path (no package assumptions)."""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ab = _load("agent/evaluation/memory_context_ab.py", "_memory_context_ab")
_pmg = _load("agent/evaluation/probe_mounted_graph.py", "_probe_mounted_graph")

TENANT = "t_demo"
ARM = "mounted_v2"
LOOKUP = _pmg.OrderLookup("OK", "订单已核验，具体状态请在系统查看。")   # needle-free, no "状态："
DRAFT = "好的，我记下了。"
PESSIMISTIC_SUMMARY = "客户此前提供过订单信息并咨询过退货事项，具体单号、金额与抬头未在本次摘要中保留。"


class MergeModel:
    """The summarise node's model: every call returns the anchor-dropping paraphrase."""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        return _pmg.AIMessage(content=PESSIMISTIC_SUMMARY)


def text(message) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else str(content)


async def run_case(case: dict) -> dict:
    """Drive one long conversation through four_agent.run_turn, then score the probe turn."""
    merge = MergeModel()          # the graph is cached per tenant, so the first model is the one used
    app, _ = _pmg.app_with(model=merge)
    supervisor = _pmg.Supervisor(_pmg.compound, DRAFT)   # route like the real supervisor would,
    # so the reviewer's "政策必须委派 FAQ / 订单必须委派 Order" rules are satisfied and every turn
    # ends `answered`. Measuring memory through turns that end in the safe fallback would mix two
    # effects into one number.
    thread = f"{TENANT}:mem-mount-{case['id']}"
    state = {}
    for message in [*case["turns"], case["probe"]]:
        _graph, state, _info = await _pmg.turn(app, merge, supervisor, message,
                                               thread=thread, order_result=LOOKUP)
    fed = "\n".join(text(m) for m in supervisor.last_prompt)
    channel = state.get("messages", [])
    summary = state.get("summary") or ""
    durable = "\n".join(text(m) for m in channel) + "\n" + summary
    marks = {needle: {"in_fed_context": needle in fed,
                      "in_durable_transcript": needle in durable}
             for needle in _ab.needles(case)}
    return {"arm": ARM, "id": case["id"],
            "outcome": "answered" if not state.get("review_feedback") else "fallback",
            "review_feedback": (state.get("review_feedback") or "")[:120],
            "watermark": int(state.get("summary_watermark") or 0),
            "transcript_messages": len(channel),
            "summary_chars": len(summary),
            "fed_chars": len(fed),
            "supervisor_calls": supervisor.calls,
            "merge_calls": merge.calls,
            "durable_basis": "图通道（RemoveMessage 压缩后）+ 摘要文本；"
                             "客户可见历史在 business MySQL 仍完整，不属于本通道",
            "needles": marks,
            "fed_needle_count": sum(1 for m in marks.values() if m["in_fed_context"]),
            "durable_needle_count": sum(1 for m in marks.values() if m["in_durable_transcript"]),
            "recoverable": False}


async def main(args: argparse.Namespace) -> None:
    cases = _ab.geometry_cases() if args.geometry else _frozen_cases(args.limit)
    rows = []
    for case in cases:
        row = await run_case(case)
        rows.append(row)
        print(f"{ARM:<10} {row['id']:<14} fed={row['fed_needle_count']}/3 "
              f"durable={row['durable_needle_count']}/3 wm={row['watermark']} "
              f"msgs={row['transcript_messages']} sum={row['summary_chars']} "
              f"fed_chars={row['fed_chars']} outcome={row['outcome']}", flush=True)

    merged = list(rows)
    for extra in args.merge_with:
        for line in Path(extra).read_text(encoding="utf-8").splitlines():
            if line.strip():
                merged.append(json.loads(line))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in merged) + "\n",
                   encoding="utf-8")

    fed = [r["fed_needle_count"] for r in rows]
    dur = [r["durable_needle_count"] for r in rows]
    print(f"\n{ARM}: fed_sum={sum(fed)}/{len(fed) * 3} "
          f"durable_sum={sum(dur)}/{len(dur) * 3} "
          f"完全丢失(fed=0)的题目={sum(1 for v in fed if v == 0)}/{len(rows)} "
          f"逐题波动幅度={max(fed) - min(fed) if fed else 0}")
    print(f"原始数据（含 --merge-with 的对照臂）→ {out}")


def _frozen_cases(limit: int) -> list[dict]:
    from evaluation.memory_ab_live import cases as frozen
    all_cases = frozen()
    return all_cases if limit >= len(all_cases) else all_cases[:limit]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="mounted-graph memory per-case measurement")
    parser.add_argument("--limit", type=int, default=10, help="how many frozen conversations")
    parser.add_argument("--geometry", action="store_true",
                        help="use the needle-depth x dialogue-length cases instead")
    parser.add_argument("--out", default=str(ROOT / "agent" / "evaluation" / "runs" /
                                             "memory_context_mounted.jsonl"))
    parser.add_argument("--merge-with", nargs="*", default=[],
                        help="existing arm jsonl files to append, so diff_arms renders one table")
    asyncio.run(main(parser.parse_args()))
