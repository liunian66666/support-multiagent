# 校验输出含中文
"""Verify phase 3/4 against the real ``AsyncPostgresSaver``, not an in-memory stub.

    docker run --rm --network cs-customer-service_cs-network \\
      -v "$ROOT:/work:ro" -e PYTHONPATH=/work:/work/python_agent \\
      -w /work/python_agent cs-test:agent python /work/agent/evaluation/verify_checkpoint_postgres.py

The model is scripted, so nothing here measures answer quality — what is under test is
the checkpointer: durable resume across two separately compiled graph objects, replay
idempotency, and watermark advance with the transcript left intact.

It writes only its own ``support_v2:t_demo:pgcheck-*`` thread keys and deletes them
before exiting, then re-counts the rows to prove it left nothing behind. Existing
conversation data is not touched.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid

from langchain_core.messages import AIMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent import memory
from agent.fakes import USAGE, ScriptedSupportModel, ai_with_tools, offline_deps, tool_call
from agent.service import SupportService, thread_key

TENANT = "t_demo"
ANSWER = AIMessage(content="签收后 7 天内可申请退货，运费自理。", usage_metadata=USAGE)


def _service(saver, *, turns_script: bool = False):
    async def deps_for_tenant(_tenant_id):
        script = ([ai_with_tools("核实", [tool_call("consult_faq", {"question": "退货期限"}, "c1")]),
                   ANSWER] if turns_script else [])
        deps, _model, _events, _captured = offline_deps(
            model=ScriptedSupportModel(supervisor_script=list(script)))
        return deps

    return SupportService(deps_for_tenant, checkpointer=saver,
                          graph_kwargs={"compact_trigger": 4, "compact_keep": 2})


async def _count(dsn: str, key: str) -> int:
    import psycopg

    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM checkpoints WHERE thread_id = %s", (key,))
            return int((await cur.fetchone())[0])


async def main() -> None:
    parser = argparse.ArgumentParser(description="real-PostgreSQL checkpoint verification")
    parser.add_argument("--resume-thread", default="",
                        help="thread id written by an earlier run: skip the write phase and only "
                             "resume it, which is how a genuine process restart is proven")
    parser.add_argument("--keep", action="store_true", help="leave the thread behind for a later run")
    parser.add_argument("--thread", default="", help="fixed thread id, so a second process can resume it")
    args = parser.parse_args()

    dsn = os.getenv("POSTGRES_DSN") or ""
    if not dsn:
        from app.infra.config import POSTGRES_DSN as configured
        dsn = configured
    thread = args.resume_thread or args.thread or f"{TENANT}:pgcheck-{uuid.uuid4().hex[:8]}"
    key = thread_key(TENANT, thread)
    resumed = bool(args.resume_thread)

    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()
        print(f"thread={key}  rows_before={await _count(dsn, key)}  mode={'resume' if resumed else 'write'}")

        if resumed:
            revived = _service(saver)
            history = await revived.history(TENANT, thread)
            state = await revived.memory_state(TENANT, thread)
            graph = await revived.graph_for(TENANT)
            snapshot = await graph.aget_state({"configurable": {"thread_id": key}})
            print(f"resumed history={len(history)} watermark={state['watermark']} "
                  f"summarized={state['summarized']} verbatim={state['verbatim']}")
            assert len(history) == 12, f"expected the 6 turns written earlier, got {len(history)}"
            assert state["watermark"] > 0 and state["summary"], "memory must survive the restart"
            turn = await revived.run_turn(TENANT, thread, "重启后再问一句", turn_id="pg-after")
            assert turn.answer and turn.outcome in {"answered", "answered_unchecked", "no_result"}
            after = await revived.history(TENANT, thread)
            assert len(after) == len(history) + 2, "post-restart turn did not append exactly one exchange"
            assert len(snapshot.values.get("ledger", [])) == 6
            if not args.keep:
                await saver.adelete_thread(key)
            print(f"after_cleanup rows={await _count(dsn, key)}")
            print("PASS: 真实 PG 检查点在独立进程重启后仍可续聊，且水位线记忆随之恢复")
            return

        first = _service(saver, turns_script=True)
        turn_one = await first.run_turn(TENANT, thread, "退货期限是多久？", turn_id="pg-1")
        assert turn_one.outcome == "answered" and turn_one.roles == ["faq"], turn_one.outcome

        plain = _service(saver)
        for index in range(4):
            await plain.run_turn(TENANT, thread, f"再问一个问题 {index}", turn_id=f"pg-{index + 2}")

        # "restart": a fresh service object, a freshly compiled graph, same saver.
        revived = _service(saver)
        history = await revived.history(TENANT, thread)
        state = await revived.memory_state(TENANT, thread)
        print(f"resume history={len(history)} messages  watermark={state['watermark']} "
              f"summarized={state['summarized']} verbatim={state['verbatim']}")

        assert len(history) == 10, f"expected 5 turns x 2 messages, got {len(history)}"
        assert state["total"] == 10 and state["summarized"] + state["verbatim"] == 10
        assert state["watermark"] > 0, "compaction must have advanced the watermark"
        assert state["summary"], "summary channel must be populated in the checkpoint"

        replay = await revived.run_turn(TENANT, thread, "再问一个问题 3", turn_id="pg-6")
        after_replay = await revived.history(TENANT, thread)
        assert replay.answer and len(after_replay) == len(history) + 2
        again = await revived.run_turn(TENANT, thread, "再问一个问题 3", turn_id="pg-6")
        assert len(await revived.history(TENANT, thread)) == len(after_replay), "replay duplicated a turn"
        assert again.answer == replay.answer

        graph = await revived.graph_for(TENANT)
        snapshot = await graph.aget_state({"configurable": {"thread_id": key}})
        assert len(memory.visible_messages(snapshot.values)) == len(after_replay)
        rows = await _count(dsn, key)
        print(f"rows_in_checkpoints={rows}  ledger_turns={len(snapshot.values.get('ledger', []))}")
        assert len(snapshot.values.get("ledger", [])) == 6

        if args.keep:
            print(f"kept thread={thread} for a later --resume-thread run")
            return
        await saver.adelete_thread(key)
        left = await _count(dsn, key)
        print(f"after_cleanup rows={left}")
        assert left == 0, "cleanup failed; leaving rows behind"
        print("PASS: PG checkpointer 持久化 / 重启续聊 / 重放幂等 / 水位线推进 / 零残留")


if __name__ == "__main__":
    asyncio.run(main())
