# ruff: noqa: RUF001  # 报告文案含中文
"""跨进程并发写同一线程：不加锁会丢轮，加了 advisory 锁还丢不算修好。

这条测量针对报告 §5e 的 M10/M11：两个实现（挂载图与 `agent/`）在"两个进程共用同一
thread key"时都会丢轮，根因是 `asyncio.Lock` 只到自己进程为止。**只在内存 saver 上测
是测不出来的**（同一进程内的 gather 一定被那把锁挡住），所以这里必须起**真的子进程**，
并且检查点是**真的 `AsyncPostgresSaver`**。

三种角色：

    --role worker        一个进程：并发提交 --turns 轮（每轮独立 turn_id），把自己的
                         submitted / lock_unavailable 计数写成一行 jsonl。
    --role run           编排者：fork `--workers` 个 worker 子进程同时打同一个 thread，
                         全部结束后连库数"到底落了几轮"，并删掉自建线程。

判定口径（丢轮 ≠ 拒绝）：

    submitted  = 所有 worker 提交的轮数
    refused    = outcome=lock_unavailable 的轮数（**看得见**的拒绝，写进答复与 trace）
    written    = 检查点 messages 里不同 `:u` 人消息 id 数（再用 ledger 交叉核对）
    lost       = submitted - refused - written        ← 只有这个数下降才叫"锁起作用了"

刻意把 `compact_trigger` 抬到 10000：压缩会把消息移出通道，那样"少了几条"就分不清是
丢轮还是被摘要折叠掉了。这里要量的只有并发写入，不量压缩。

只写自己那条 `support_v2:t_demo:xproc-*` 线程，结束前删除并复点行数为 0。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.agent.support.fakes import USAGE, ScriptedSupportModel, offline_deps
from app.agent.support.service import SupportService, thread_key
from app.agent.support.thread_guard import pg_advisory_guard

TENANT = "t_demo"
ANSWER = AIMessage(content="签收后 7 天内可申请退货，运费自理。", usage_metadata=USAGE)
# 脚本化模型按调用消耗答复，取完会抛。按最坏形状给足：workers × turns × 每轮最多 2 次模型调用。
SCRIPTED_ANSWERS = [ANSWER] * 64


def _dsn() -> str:
    dsn = os.getenv("POSTGRES_DSN") or ""
    if dsn:
        return dsn
    from app.infra.config import POSTGRES_DSN as configured  # noqa: PLC0415
    return configured


def _service(saver, *, guard: bool):
    async def deps_for_tenant(_tenant_id):
        deps, _model, _events, _captured = offline_deps(
            model=ScriptedSupportModel(supervisor_script=list(SCRIPTED_ANSWERS)))
        return deps

    return SupportService(deps_for_tenant, checkpointer=saver,
                          graph_kwargs={"compact_trigger": 10000, "compact_keep": 2},
                          thread_guard=(pg_advisory_guard(_dsn()) if guard else None))


async def worker(thread: str, turns: int, guard: bool, out: Path, worker_id: str) -> dict:
    """一个进程里并发打 `turns` 轮；每个 worker 用可区分的 turn_id 前缀。"""
    async with AsyncPostgresSaver.from_conn_string(_dsn()) as saver:
        await saver.setup()
        service = _service(saver, guard=guard)
        started = time.monotonic()
        results = await asyncio.gather(*[
            service.run_turn(TENANT, thread, f"退货期限？（worker {worker_id} 第 {i} 轮）",
                             turn_id=f"x-{worker_id}-{i}")
            for i in range(turns)], return_exceptions=True)
        outcomes = [r.outcome if isinstance(r, object) and hasattr(r, "outcome") else "raised"
                    for r in results]
        row = {"role": "worker", "worker": worker_id, "thread": thread, "guard": guard,
               "submitted": turns, "answered": sum(1 for o in outcomes if o != "lock_unavailable"),
               "lock_unavailable": outcomes.count("lock_unavailable"),
               "raised": sum(1 for r in results if not hasattr(r, "outcome")),
               "elapsed_ms": int((time.monotonic() - started) * 1000)}
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps(row, ensure_ascii=False))
        return row


async def count_written(thread: str) -> dict:
    """连库数落进检查点的轮数：人消息 id 去重 + ledger 的 turn_id 两条独立来源。"""
    key = thread_key(TENANT, thread)
    async with AsyncPostgresSaver.from_conn_string(_dsn()) as saver:
        graph = await _service(saver, guard=False).graph_for(TENANT)
        snap = await graph.aget_state({"configurable": {"thread_id": key}})
        values = snap.values or {}
        humans = [m for m in values.get("messages", []) if isinstance(m, HumanMessage)]
        ids = {m.id for m in humans}
        ledger = [rec.get("turn_id", "") for rec in (values.get("ledger") or [])]
        return {"human_messages": len(humans), "distinct_human_ids": len(ids),
                "ledger_turns": len(set(ledger)), "total_messages": len(values.get("messages", []))}


async def cleanup(thread: str) -> int:
    key = thread_key(TENANT, thread)
    async with AsyncPostgresSaver.from_conn_string(_dsn()) as saver:
        await saver.adelete_thread(key)
    import psycopg  # noqa: PLC0415
    async with await psycopg.AsyncConnection.connect(_dsn()) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM checkpoints WHERE thread_id = %s", (key,))
            return int((await cur.fetchone())[0])


async def orchestrator(workers: int, turns: int, guard: bool, out: Path, thread: str) -> int:
    procs = [subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--role", "worker",
         "--thread", thread, "--turns", str(turns), "--out", str(out), "--worker", str(w),
         *([] if guard else ["--no-guard"])],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT) for w in range(workers)]
    # 同一台机器上 fork 完立刻一起放：真正的重叠靠的是"同一 thread key + 不同进程"，
    # 不是靠 sleep ——任何 sleep 都会把并发窗口让出去，测出来就变成"没冲突所以没丢"。
    codes = [p.wait() for p in procs]
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("thread") == thread
            and json.loads(line).get("role") == "worker"]   # 账本累加，只认本次这条线程的 worker 行
    submitted = sum(r["submitted"] for r in rows)
    refused = sum(r["lock_unavailable"] for r in rows)
    raised = sum(r["raised"] for r in rows)
    written = await count_written(thread)
    lost = submitted - refused - written["distinct_human_ids"]
    summary = {"role": "summary", "thread": thread, "guard": guard, "workers": workers,
               "turns_per_worker": turns, "submitted": submitted, "refused": refused,
               "raised": raised, "worker_exit_codes": codes,
               "worker_elapsed_ms": [r["elapsed_ms"] for r in rows],
               "slowest_worker_ms": max((r["elapsed_ms"] for r in rows), default=0),
               **written, "lost_turns": lost,
               "ledger_matches_transcript": written["ledger_turns"] == written["distinct_human_ids"],
               "rows_after_cleanup": await cleanup(thread)}
    with out.open("a", encoding="utf-8") as fh:      # 汇总也进同一份 jsonl，报告可直接引
        fh.write(json.dumps(summary, ensure_ascii=False) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if (lost == 0 and raised == 0 and summary["rows_after_cleanup"] == 0) else 1


async def main() -> int:
    ap = argparse.ArgumentParser(description="cross-process write serialisation probe")
    ap.add_argument("--role", choices=["worker", "run"], default="run")
    ap.add_argument("--thread", default="")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--turns", type=int, default=4)
    ap.add_argument("--guard", action=argparse.BooleanOptionalAction, default=True,
                    help="--no-guard 复现 §5e M10/M11 的丢轮；--guard 走 pg advisory 锁")
    ap.add_argument("--out", default="cross_process_lock.jsonl")
    ap.add_argument("--worker", default="0")
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    thread = args.thread or f"{TENANT}:xproc-{uuid.uuid4().hex[:8]}"
    if args.role == "worker":
        await worker(thread, args.turns, args.guard, out, args.worker)
        return 0
    print(f"thread={thread}  workers={args.workers}  turns={args.turns}  guard={args.guard}")
    return await orchestrator(args.workers, args.turns, args.guard, out, thread)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
