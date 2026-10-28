# ruff: noqa: RUF001  # 报告文案含中文
"""Deterministic four-arm memory comparison over the 10 frozen long conversations.

Answers one question that does **not** need a working LLM: at the moment the probe is
asked, is each needle (订单号 / 金额 / 抬头) still inside the context the model is
actually fed, and is it still anywhere in the durable thread?

Arms (all through the real graph, identical transcripts, identical scripted
summariser):

``window_only``        纯最近窗口，等价于旧路径 ``recent_messages(limit=12)`` 的行为
``delete_rebuild``     官方 ``SummarizationMiddleware`` 语义：``REMOVE_ALL_MESSAGES`` 后
                       用「摘要 + 保留尾部」重建通道（用同一份 fed context，但转写已不可恢复）
``watermark_norepair`` 水位线记忆，关掉锚点回补 —— 用来隔离回补本身的贡献
``watermark``          生产配置：水位线 + 最近原文 + 锚点回补

The summariser here is deliberately pessimistic (it paraphrases numbers and order ids
away), which is the failure mode the phase was written to survive. So this table
measures the mechanism's worst case, not a best case: a needle counted present is
present *despite* the summary losing it.

What this cannot claim: anything about answer wording, latency or token cost. Those
need a live model, which the environment currently does not have.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
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
ARMS: dict[str, dict[str, Any]] = {
    "window_only": {"compact_trigger": 10 ** 9, "compact_keep": 6, "compact_repair": True},
    "delete_rebuild": {"compact_trigger": 12, "compact_keep": 6, "compact_repair": False},
    "watermark_norepair": {"compact_trigger": 12, "compact_keep": 6, "compact_repair": False},
    "watermark": {"compact_trigger": 12, "compact_keep": 6, "compact_repair": True},
}


def needles(case: dict) -> list[str]:
    return [case["needles"]["ref"], f'{case["needles"]["amount"]}元', case["needles"]["company"]]


def make_service(arm: str) -> SupportService:
    async def deps_for_tenant(_tenant_id: str) -> SupportDeps:
        deps, _model, _events, _captured = offline_deps(
            model=ScriptedSupportModel(summary_mode="drop_anchors"))
        return deps

    kwargs = dict(ARMS[arm])
    return SupportService(deps_for_tenant, checkpointer=InMemorySaver(),
                          graph_kwargs={"context_window": CONTEXT_WINDOW, **kwargs})


async def run_case(case: dict, arm: str, durable: bool) -> dict[str, Any]:
    service = make_service(arm)
    thread = f"{TENANT}:memab-{arm}-{case['id']}"
    for index, message in enumerate(case["turns"]):
        await service.run_turn(TENANT, thread, message, turn_id=f"{case['id']}-t{index}")
    probe = await service.run_turn(TENANT, thread, case["probe"], turn_id=f"{case['id']}-probe")

    graph = await service.graph_for(TENANT)
    values = (await graph.aget_state({"configurable": {
        "thread_id": f"support_v2:{thread}"}})).values
    fed = "\n".join(memory.message_text(m) for m in memory.model_context(values,
                                                                         window=CONTEXT_WINDOW))
    # delete_rebuild emulates the official middleware, which removes the absorbed
    # messages from the channel; everything older than the retained tail is then gone.
    if arm == "delete_rebuild":
        watermark = int(values.get("summary_watermark", 0) or 0)
        durable_text = "\n".join(memory.message_text(m)
                                 for m in memory.visible_messages(values)[watermark:])
    else:
        durable_text = fed + "\n" + "\n".join(memory.message_text(m)
                                              for m in memory.visible_messages(values))
    marks = {needle: {"in_fed_context": needle in fed, "in_durable_transcript": needle in durable_text}
             for needle in needles(case)}
    return {"arm": arm, "id": case["id"], "outcome": probe.outcome,
            "watermark": values.get("summary_watermark", 0),
            "transcript_messages": len(memory.visible_messages(values)),
            "summary_chars": len(values.get("summary", "")), "needles": marks,
            "fed_needle_count": sum(1 for m in marks.values() if m["in_fed_context"]),
            "durable_needle_count": sum(1 for m in marks.values() if m["in_durable_transcript"]),
            "recoverable": durable}


def geometry_cases() -> list[dict[str, Any]]:
    """Structurally varied cases, alongside (not instead of) the frozen ten.

    ``evaluation.memory_ab_live.cases()`` is ten different *payloads* over one dialogue
    skeleton: every case is 11 turns + probe (= 24 messages, which happens to sit exactly at
    the mounted graph's `>24` compaction threshold), so every arm lands on the same watermark
    and the same summary length. That shows substituted values survive; it cannot show that the
    mechanism survives a needle introduced at a different depth, or a dialogue long enough
    to compact twice.

    Here the needle turn moves (depth 0/3/6/9) and the dialogue grows past the compaction
    trigger (8/13/20 turns, trigger=12 keeps 6), so the interesting cases — needle exactly
    at the watermark, needle inside an already-compacted-and-recompacted region — exist.
    Filler deliberately avoids every needle substring, so "present" is unambiguous.
    """
    cases = []
    for depth in (0, 3, 6, 9):
        for total in (8, 13, 20):
            if depth >= total:
                continue
            ref, amount, company = f"ORD-G{depth}{total}-77", 168 + depth * total, f"测试厂商{depth}{total}"
            turns = []
            for index in range(total):
                if index == depth:
                    # "{amount}元" with no space, matching evaluation.memory_ab_live's own
                    # wording: a stray space makes the needle string unmatchable and looks
                    # like eleven lost anchors when it is really one test-data typo.
                    turns.append(f"这一单我当时实付了{amount}元，单号是 {ref}，抬头开「{company}」。")
                else:
                    turns.append(f"第{index + 1}句日常追问：好的，那现在进度如何，还有别的要注意吗？")
            cases.append({"id": f"geo-d{depth}-t{total}", "turns": turns,
                          "probe": "刚才那个单号、金额和抬头分别是什么？",
                          "needles": {"ref": ref, "amount": amount, "company": company},
                          "_depth": depth, "_total": total})
    return cases


async def main(args: argparse.Namespace) -> None:
    if args.geometry:
        cases = geometry_cases()
    else:
        from evaluation.memory_ab_live import cases as frozen
        cases = frozen() if args.limit >= 10 else frozen()[: args.limit]

    rows = []
    for arm in ARMS:
        for case in cases:
            row = await run_case(case, arm, arm != "delete_rebuild")
            rows.append(row)
            print(f"{arm:<19} {row['id']:<8} fed={row['fed_needle_count']}/3 "
                  f"durable={row['durable_needle_count']}/3 wm={row['watermark']} "
                  f"msgs={row['transcript_messages']} outcome={row['outcome']}", flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    print("\n== 汇总（fed = 探针时被喂给模型的上下文里还剩几根针） ==")
    baseline = {r["id"]: r["fed_needle_count"] for r in rows if r["arm"] == "window_only"}
    for arm in ARMS:
        fed = [r["fed_needle_count"] for r in rows if r["arm"] == arm]
        dur = [r["durable_needle_count"] for r in rows if r["arm"] == arm]
        improved = sum(1 for r in rows if r["arm"] == arm and r["fed_needle_count"] > baseline[r["id"]])
        regressed = sum(1 for r in rows if r["arm"] == arm and r["fed_needle_count"] < baseline[r["id"]])
        spread = max(fed) - min(fed) if fed else 0
        print(f"{arm:<19} fed_sum={sum(fed):>3}/{len(fed) * 3} durable_sum={sum(dur):>3} "
              f"vs滑窗: 改善{improved} 回退{regressed} 逐题波动幅度={spread}")
    print(f"\n原始数据 → {out}")
    print("注：摘要器被刻意设为『会把数值和单号改写没』的最差情况；"
          "本表只测机制，不测措辞、延迟与 token 成本。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="deterministic memory arms comparison")
    parser.add_argument("--limit", type=int, default=10, help="how many of the 10 conversations")
    parser.add_argument("--geometry", action="store_true",
                        help="run the structurally varied synthetic cases (needle depth × dialogue "
                             "length) instead of the ten same-shaped frozen conversations")
    parser.add_argument("--out", default="",
                        help="defaults to runs/memory_context_ab.jsonl, or ..._geometry.jsonl with --geometry")
    args = parser.parse_args()
    if not args.out:
        name = "memory_context_geometry" if args.geometry else "memory_context_ab"
        args.out = str(Path(__file__).parent / "runs" / f"{name}.jsonl")
    asyncio.run(main(args))
