# ruff: noqa: RUF001
"""Phase-6 transport checks: the mounted entry over a real PostgreSQL saver.

The A/B answer-quality arms need a reachable chat model and are blocked (see
``probe_platform_completion.py``). Everything else phase 6 asks for — JSON/SSE
consistency, retry not duplicating messages, the rollback switch, an unauthorised
thread, and model-failure degradation — is transport and state behaviour, so it is
verified here instead of waiting for the gateway.

It is deliberately the *real* stack, not a test double of it:
``create_standalone_app`` → ``build_router`` → ``SupportService`` → compiled graph →
``AsyncPostgresSaver`` on the live ``cs_postgres``. Only the chat model is scripted,
because scripted is the one thing that makes the run reproducible while the gateway
is down.

Writes are confined to threads it creates itself (``httpcheck-*``) and deleted at the
end; the legacy ``chat:*`` threads and every existing row are untouched. Run with
``--keep`` to inspect the checkpoint rows afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid

from fastapi import Request
from langchain_core.messages import AIMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent.entrypoint import create_standalone_app
from agent.fakes import USAGE, ScriptedSupportModel, ai_with_tools, offline_deps, tool_call
from agent.service import SupportService

TENANT = "t_demo"
ANSWER = AIMessage(content="签收后 7 天内可申请退货，运费自理。", usage_metadata=USAGE)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{f'  [{detail}]' if detail else ''}")
    if not condition:
        failures.append(name)


def scripted(model: ScriptedSupportModel) -> None:
    """Re-arm the same two-call script; a popped list would not survive a replay."""
    model.supervisor_script[:] = [
        ai_with_tools("核实", [tool_call("consult_faq", {"question": "退货期限"}, "c1")]),
        ANSWER,
    ]


async def identity(request: Request, thread_id: str) -> tuple[str, str]:
    """Stands in for the host's verified session, exactly like ``_default_identity``."""
    return request.headers.get("x-test-tenant", ""), thread_id


def sse_payloads(text: str) -> list[dict]:
    events = []
    for block in text.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


async def rows_for(saver: AsyncPostgresSaver, key: str) -> int:
    # alist() is an async generator: it needs an explicit `async for`, and neither
    # `await alist(...)` nor `sum(1 async for ...)` is legal.
    count = 0
    async for _ in saver.alist({"configurable": {"thread_id": key}}):
        count += 1
    return count


async def channel_shape(saver: AsyncPostgresSaver, key: str) -> tuple[int, int, int]:
    """(messages, ledger_turns, distinct_message_ids) of the latest checkpoint.

    Row counts are NOT the idempotency signal: LangGraph writes one checkpoint row per
    super-step, so a replay of the same ``turn_id`` appends rows by design. The dedup
    guarantee lives in the channels — ``add_messages`` replaces by id, the ledger
    reducer upserts by ``turn_id`` — so that is what a replay has to leave unchanged.
    Distinct ids are reported next to length so a silent overwrite cannot be mistaken
    for a clean replay.
    """
    found = await saver.aget_tuple({"configurable": {"thread_id": key}})
    if found is None:
        return 0, 0, 0
    values = found.checkpoint.get("channel_values", {})
    messages = values.get("messages", []) or []
    ids = {getattr(message, "id", None) for message in messages}
    return len(messages), len(values.get("ledger", []) or []), len(ids)


async def main(args: argparse.Namespace) -> None:
    import httpx

    thread = f"{TENANT}:httpcheck-{uuid.uuid4().hex[:8]}"
    foreign = f"t_other:httpcheck-{uuid.uuid4().hex[:8]}"
    key = f"support_v2:{thread}"

    healthy_model = ScriptedSupportModel()
    healthy_deps, *_ = offline_deps(model=healthy_model)
    broken_model = ScriptedSupportModel(fail_supervisor=True)
    broken_deps, *_ = offline_deps(model=broken_model)

    async with AsyncPostgresSaver.from_conn_string(os.environ["POSTGRES_DSN"]) as saver:
        await saver.setup()

        def app_for(deps):
            async def provider(_tenant_id: str):
                return deps

            return create_standalone_app(identity,
                                         service=SupportService(provider, checkpointer=saver))

        healthy = app_for(healthy_deps)
        broken = app_for(broken_deps)

        async def call(app: object, path: str, *, message: str, turn_id: str = "",
                       tenant: str = TENANT, pending: bool = False) -> httpx.Response:
            transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
            async with httpx.AsyncClient(transport=transport, base_url="http://entry") as client:
                return await client.post(path, json={"thread_id": thread if tenant == TENANT
                                                     else foreign,
                                                     "user_message": message,
                                                     "turn_id": turn_id,
                                                     "has_pending_ticket": pending},
                                         headers={"x-test-tenant": tenant})

        print("\n[1] 回退开关与未完成审批（HTTP 层）")
        os.environ["SUPPORT_AGENT_V2"] = "off"
        r = await call(healthy, "/agent/chat/v2", message="退货期限多久？")
        check("flag off → 409（默认入口不受影响）", r.status_code == 409, f"HTTP {r.status_code}")
        check("flag off 未写入任何检查点", await rows_for(saver, key) == 0)
        os.environ["SUPPORT_AGENT_V2"] = "on"

        r = await call(healthy, "/agent/chat/v2", message="退货期限多久？", pending=True)
        check("未完成审批 → 409（该线程留在旧图）", r.status_code == 409, f"HTTP {r.status_code}")

        r = await call(healthy, "/agent/chat/v2", message="退货期限多久？", tenant="t_attacker")
        check("跨租户线程 → 403", r.status_code == 403, f"HTTP {r.status_code}")
        check("403 请求不留检查点", await rows_for(saver, f"support_v2:{foreign}") == 0)

        print("\n[2] JSON / SSE 一致 + 同 turn_id 重放不重复")
        scripted(healthy_model)
        turn = f"T-{uuid.uuid4().hex[:6]}"
        j = await call(healthy, "/agent/chat/v2", message="退货期限多久？", turn_id=turn)
        check("JSON 200", j.status_code == 200, f"HTTP {j.status_code}")
        json_payload = j.json()
        check("JSON 结论带证据", json_payload["outcome"] == "answered" and bool(json_payload["evidence_ids"]),
              f"outcome={json_payload['outcome']} evidence={json_payload['evidence_ids']}")
        after_first = await rows_for(saver, key)
        shape_first = await channel_shape(saver, key)
        check("首轮落库到真实 PG", after_first > 0 and shape_first[0] > 0,
              f"rows={after_first} messages={shape_first[0]} ledger={shape_first[1]}")

        scripted(healthy_model)
        s = await call(healthy, "/agent/chat/v2/stream", message="退货期限多久？", turn_id=turn)
        check("SSE 200 + text/event-stream",
              s.status_code == 200 and s.headers["content-type"].startswith("text/event-stream"),
              s.headers.get("content-type", ""))
        events = sse_payloads(s.text)
        check("SSE 两帧（chunk + end）", len(events) == 2, f"events={len(events)}")
        end = events[-1] if events else {}
        check("SSE content == JSON content（含同一页脚）",
              end.get("content") == json_payload["content"], f"sse={end.get('content', '')[:20]!r}")
        check("SSE end 携带同一 turn_id/outcome/agents",
              end.get("turn_id") == turn and end.get("outcome") == json_payload["outcome"]
              and end.get("agents") == json_payload["agents"], f"agents={end.get('agents')}")
        check("同 turn_id 重放：消息/轮次通道未增长",
              await channel_shape(saver, key) == shape_first,
              f"before={shape_first} after={await channel_shape(saver, key)} "
              f"rows={await rows_for(saver, key)}（行数按 super-step 增长，属预期）")

        print("\n[3] 续聊：第二轮看见第一轮（同一 PG 线程）")
        scripted(healthy_model)
        t2 = await call(healthy, "/agent/chat/v2", message="那运费谁出？", turn_id=f"T2-{turn}")
        check("第二轮 200", t2.status_code == 200, f"HTTP {t2.status_code}")
        ctx = t2.json()["context"]
        check("第二轮状态里累计了首轮（total ≥ 4 条可见消息）", ctx["total"] >= 4,
              f"total={ctx['total']} summarized={ctx['summarized']} verbatim={ctx['verbatim']}")

        print("\n[4] 模型故障降级（HTTP 层，不崩不空白）")
        b = await call(broken, "/agent/chat/v2", message="退货期限多久？",
                       turn_id=f"ERR-{uuid.uuid4().hex[:6]}")
        check("故障轮仍 200", b.status_code == 200, f"HTTP {b.status_code}")
        body = b.json()
        check("故障轮 outcome=no_result", body["outcome"] == "no_result", body["outcome"])
        check("故障轮答复非空白", bool(body["content"].strip()), repr(body["content"][:30]))
        check("故障轮透出 error 字段", bool(body["error"]), body["error"][:60])

        if args.keep:
            print(f"\n--keep：线程 {key} 保留，rows={await rows_for(saver, key)}")
        else:
            before = await rows_for(saver, key)
            await saver.adelete_thread(key)
            check("清理后该线程归零（线上会话未受影响）",
                  await rows_for(saver, key) == 0, f"cleaned={before}")

    print("\n" + ("FAIL: " + ", ".join(failures) if failures else
                  "PASS: 真实 PG 检查点之上，挂载入口的 JSON/SSE 一致、重放幂等、开关回退、越权拒绝、故障降级全部通过"))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="leave the created thread behind")
    asyncio.run(main(parser.parse_args()))
