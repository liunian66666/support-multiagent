# 中文文案必然包含全角标点
"""Runnable demo of the four-agent graph, offline, with an auditable dump.

    python -m agent.demo                      # composite consult + long chat + restart
    python -m agent.demo --turns 20           # longer memory segment
    python -m agent.demo --dump runs/x.jsonl  # keep the raw per-turn records
    python -m agent.demo --postgres "postgresql://user:pass@host:5432/db"
    python -m agent.demo --postgres "$DSN" --thread demo-pinned --keep

What this proves and what it does not: the model is scripted (:mod:`agent.fakes`), so
a run demonstrates delegation, merging, review, bounds, watermark memory and checkpoint
recovery. It never demonstrates answer quality, latency or token cost — those require
the live tenant model and belong to phase 6.

``--postgres`` exercises the real ``AsyncPostgresSaver`` (the documented async context
manager, ``.setup()`` first). This path has since been run against the deployed
database from inside the container network: it writes only its own
``support_v2:t_demo:<thread>`` key, reports the row count before and after, and deletes
that key on the way out unless ``--keep`` is passed — the same discipline
``verify_checkpoint_postgres.py`` uses. The thread id gets a random suffix by default so
a re-run can never collide with a previous demo and never reads someone else's session.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from . import memory
from .deps import SupportDeps
from .fakes import USAGE, ScriptedSupportModel, ai_with_tools, offline_deps, tool_call
from .service import SupportService, thread_key

TENANT = "t_demo"

COMPOSITE_QUESTION = "我的订单 ORD-1001 到哪了？另外退货运费谁承担？"
COMPOSITE_SCRIPT = [
    ai_with_tools("先并行核实订单和政策", [
        tool_call("consult_faq", {"question": "退货运费谁承担"}, "c1"),
        tool_call("consult_order", {"question": "ORD-1001 到哪了"}, "c2")]),
    AIMessage(content="订单 ORD-1001 已发货；退货请在签收后 7 天内申请，运费自理。",
              usage_metadata=USAGE),
]


@asynccontextmanager
async def open_saver(dsn: str | None) -> AsyncIterator[Any]:
    if not dsn:
        yield InMemorySaver()
        return
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()
        yield saver


def scripted_service(saver: Any, script: list[AIMessage] | None = None, *,
                     compact_trigger: int = 6, compact_keep: int = 4) -> SupportService:
    async def deps_for_tenant(_tenant_id: str) -> SupportDeps:
        deps, _model, _events, _captured = offline_deps(
            model=ScriptedSupportModel(supervisor_script=list(script or [])))
        return deps

    return SupportService(deps_for_tenant, checkpointer=saver,
                          graph_kwargs={"compact_trigger": compact_trigger,
                                        "compact_keep": compact_keep})


def _line(turn: Any, index: str) -> str:
    usage = turn.usage or {}
    return (f"{index:<10} outcome={turn.outcome:<18} agents={','.join(turn.roles) or '-':<12} "
            f"attempts={turn.attempts} evidence={len(turn.evidence_ids)} "
            f"tokens={usage.get('total_tokens', 0)} ctx={turn.context}")


async def _count(dsn: str, key: str) -> int:
    """Rows this thread owns in the durable store, on its own read-only connection."""
    import psycopg

    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM checkpoints WHERE thread_id = %s", (key,))
            return int((await cur.fetchone())[0])


async def main(args: argparse.Namespace) -> None:
    dump: list[dict[str, Any]] = []
    # thread_key() requires "<tenant>:<session>", the same contract the HTTP entry uses;
    # accept either form here so the demo cannot accidentally key outside its tenant.
    suffix = args.thread or f"demo-{uuid.uuid4().hex[:6]}"
    thread = suffix if suffix.startswith(f"{TENANT}:") else f"{TENANT}:{suffix}"
    key = thread_key(TENANT, thread)

    async with open_saver(args.dsn) as saver:
        # A pinned --thread may already carry a transcript; measure the base instead of
        # assuming zero, so re-running the demo on a live thread cannot look like data loss.
        base = await scripted_service(saver).memory_state(TENANT, thread)
        if args.dsn:
            print(f"\n线程键={key}  写入前检查点行数={await _count(args.dsn, key)} "
                  f"已有转写={base['total']} 条")

        print("\n[1] 复合咨询：Supervisor 应在同一轮里同时委派 FAQ 与 Order")
        service = scripted_service(saver, COMPOSITE_SCRIPT)
        turn = await service.run_turn(TENANT, thread, COMPOSITE_QUESTION, turn_id="demo-composite")
        print("   ", _line(turn, "composite"))
        print("    answer:", turn.answer)
        dump.append({"segment": "composite", **turn.to_payload()})

        print(f"\n[2] 长对话续聊：再 {args.turns} 轮，水位线推进且没有任何消息被删除")
        chatty = scripted_service(saver)
        for index in range(args.turns):
            turn = await chatty.run_turn(TENANT, thread, f"顺带问第 {index + 1} 个问题",
                                         turn_id=f"demo-{index}")
            if index % max(1, args.turns // 5) == 0 or index == args.turns - 1:
                print("   ", _line(turn, f"turn-{index + 1}"))
            dump.append({"segment": "long", "index": index, **turn.to_payload()})

        state = await chatty.memory_state(TENANT, thread)
        print(f"    水位线：total={state['total']} summarized={state['summarized']} "
              f"verbatim={state['verbatim']} watermark={state['watermark']}")
        assert state["total"] == base["total"] + (args.turns + 1) * 2, "转写不得丢轮"

        print("\n[3] 重启续聊：新建 Service（新图实例）复用同一 checkpointer")
        revived = scripted_service(saver)
        history = await revived.history(TENANT, thread)
        prompt = memory.model_context({"messages": [], "summary": state["summary"],
                                       "summary_watermark": 0}, window=2)
        print(f"    恢复消息数={len(history)} 末条={history[-1]['content'][:28]!r}")
        print(f"    摘要作为上下文而非事实来源：{bool(prompt) and '不作为订单状态或店铺政策的依据' in prompt[0].content}")

        if args.dsn:
            rows = await _count(args.dsn, key)
            print(f"\n[4] 落盘核对：本线程在 checkpoints 里占 {rows} 行")
            if args.keep:
                print(f"    --keep：线程 {key} 原样留下，可用 --thread {thread} 再演一次续聊")
            else:
                await saver.adelete_thread(key)
                left = await _count(args.dsn, key)
                print(f"    清理后 rows={left}"
                      + ("，零残留。" if left == 0 else " ← 清理失败，请手动删除该线程"))
                assert left == 0, "cleanup failed; leaving rows behind"

    if args.dump:
        Path(args.dump).parent.mkdir(parents=True, exist_ok=True)
        with open(args.dump, "w", encoding="utf-8") as handle:
            for record in dump:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"\n原始记录已写入 {args.dump}（{len(dump)} 条）")
    print("\n注意：以上由脚本化模型驱动，只证明编排、守卫与记忆机制；"
          "答复质量、延迟与 token 成本必须在真实租户模型上另测。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="four-agent support graph demo")
    parser.add_argument("--turns", type=int, default=8, help="long-chat turns after the composite one")
    parser.add_argument("--dump", default="", help="write raw per-turn JSONL here")
    parser.add_argument("--postgres", dest="dsn", default=None,
                        help="use AsyncPostgresSaver with this DSN; intentionally not read from "
                             "$POSTGRES_DSN, so no invocation silently switches durable store")
    parser.add_argument("--thread", default="",
                        help="fixed thread id; default is a random demo-* key so runs never collide")
    parser.add_argument("--keep", action="store_true",
                        help="on the PostgreSQL path, leave the thread behind instead of deleting it")
    asyncio.run(main(parser.parse_args()))
