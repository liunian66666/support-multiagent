# ruff: noqa: RUF001
"""阶段0：把**当前**的请求/响应契约落成可重复运行的样本。

用途不是评测质量，而是回归判据：以后任何一次改动，只要跑这个脚本 diff 输出，就能
分清"预期变更"与"回归"。样本覆盖总目标里的五类场景：

1. 普通 FAQ（委派 faq，带证据 ID）
2. 本人订单（委派 order，业务归属码 ok）
3. 复合咨询（同一轮委派 order + faq）
4. 未完成审批（服务端检查点里有中断 ⇒ 对等入口 409，两种检查点键可见）
5. 旧审批恢复（在原键上 resume，恢复后不再 pending）

默认走**离线替身**（脚本模型 + InMemorySaver），因为它可重复；要采真实链路样本，
用 ``--mode live`` 指向已启动的服务，脚本会打 ``/agent/chat``、``/agent/chat/stream``、
``/agent/resume`` 与 ``/agent/checkpoint/{thread}``。

    python -m evaluation.four_agent.capture_contract_baseline --mode offline
    python -m evaluation.four_agent.capture_contract_baseline --mode live \
        --base-url http://127.0.0.1:8000 --tenant t_demo --thread t_demo:sample-1 \
        --token "$INTERNAL_API_TOKEN"

输出：``evaluation/four_agent/runs/contract_baseline.jsonl``（每行一个场景，含 mode 与时间戳）。
换路径用 ``--out``；作为回归闸门用 ``--compare <基线文件>``——两边的随机 id / 时间戳先打码
（``normalize_row``）再逐行 diff，**契约关系（字段是否出现、``turn_id == answer_id``、
message id 带 turn 前缀、SSE 与 JSON 文本一致）保留在比较里**，有差异即退出码 1。
只想起草归一化结果看 diff 时用 ``--normalize <文件>``（不采集）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent / "runs" / "contract_baseline.jsonl"
TENANT = "t_demo"

# 每次采集都会变的字段：时间戳、随机 6~8 位 hex 后缀（thread / turn / answer / message id 都带）。
# 归一化只抹掉"这一次跑出来的随机尾巴"，保留**字段是否出现、类型、彼此相等**这些契约事实。
# 关键细节：随机 id **不映射成同一个占位符**，而是按"首次出现顺序"编号（``<id1>``、``<id2>``…）。
# 打成一个 ``<id>`` 会把 ``turn_id == answer_id`` 这种幂等契约一起打没——闸门就永远放行了
# （``tests/four_agent/test_contract_baseline.py`` 第三项就是钉这个：同一行里两个 id 不同时必须看得出来）。
_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]*-[0-9a-f]{6,10}")


def normalize_row(row: dict[str, Any]) -> str:
    """把一行样本压成可 diff 的稳定形态（键排序 + 随机尾巴按出现顺序编号）。"""
    text = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
    text = _TS.sub("<ts>", text)
    seen: dict[str, str] = {}

    def _mask(match: re.Match[str]) -> str:
        raw = match.group(0)
        if raw not in seen:
            seen[raw] = f"<id{len(seen) + 1}>"
        return seen[raw]

    return _ID.sub(_mask, text)


def read_rows(path: Path) -> list[str]:
    return [normalize_row(json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ============================ 离线臂 ============================

def _service():
    """离线服务：真图、真检查点、脚本模型。"""
    from langgraph.checkpoint.memory import InMemorySaver

    from app.agent.support.fakes import ScriptedSupportModel, offline_deps
    from app.agent.support.service import SupportService

    saver = InMemorySaver()
    model = ScriptedSupportModel()

    async def deps_for_tenant(_tenant_id: str):
        deps, _model, _events, _captured = offline_deps(model=model)
        return deps

    return SupportService(deps_for_tenant, checkpointer=saver), saver, model


def _patched_app(service, saver):
    """生产 app + 对等入口，服务用注入的那一份（同一注册表）。"""
    from fastapi import FastAPI

    from app.api import chat
    from app.api.chat_v2 import build_router as build_v2_router
    from app.services import support_runtime

    app = FastAPI()
    app.state.db_pool = None
    app.state.checkpointer = saver
    app.include_router(chat.router)
    app.include_router(build_v2_router())
    support_runtime.runtime_for(app).bind(TENANT, service)
    return app


@asynccontextmanager
async def _patches(chat_module):
    """把离线臂里不该真连的东西挡掉（只影响配额/心跳/记账三处）。"""
    from unittest.mock import AsyncMock, patch

    @asynccontextmanager
    async def _conn(_app):
        yield object()

    with patch.object(chat_module, "_pool_conn", _conn), \
         patch.object(chat_module, "check_concurrent_sessions", AsyncMock(return_value=None)), \
         patch.object(chat_module, "touch_session_retry", AsyncMock()), \
         patch.object(chat_module, "_record_usage_after_chat", AsyncMock()):
        yield


def _arm(model, kind: str) -> None:
    from langchain_core.messages import AIMessage

    from app.agent.support.fakes import USAGE, ai_with_tools, tool_call

    scripts = {
        "faq": [ai_with_tools("核实政策", [tool_call("consult_faq", {"question": "退货期限"}, "c1")]),
                AIMessage(content="签收后 7 天内可申请退货，运费自担。", usage_metadata=USAGE)],
        "order": [ai_with_tools("核实订单", [tool_call("consult_order", {"question": "ORD-1001 到哪了"}, "c2")]),
                  AIMessage(content="订单 ORD-1001 状态：已发货；金额：199.00 元。", usage_metadata=USAGE)],
        "compound": [ai_with_tools("两路都要核", [
            tool_call("consult_order", {"question": "ORD-1001 状态"}, "c3"),
            tool_call("consult_faq", {"question": "退货规则"}, "c4"),
        ]), AIMessage(content="订单 ORD-1001 已发货；签收后 7 天内可申请退货。", usage_metadata=USAGE)],
    }
    model.supervisor_script[:] = [m.model_copy(deep=True) for m in scripts[kind]]


def _sse(text: str) -> list[dict]:
    return [json.loads(line[6:]) for block in text.strip().split("\n\n")
            for line in block.splitlines() if line.startswith("data: ")]


def _headers() -> dict[str, str]:
    """离线臂也要过生产鉴权：这里模拟业务层带上的三个头。"""
    from app.infra.config import INTERNAL_API_TOKEN

    return {"X-Internal-Token": INTERNAL_API_TOKEN, "X-Tenant-Id": TENANT,
            "X-Trace-Id": "baseline-offline"}


async def _offline() -> list[dict]:
    import httpx

    from app.agent.legacy_approval import approval
    from app.api import chat

    service, saver, model = _service()
    app = _patched_app(service, saver)
    rows: list[dict] = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://offline") as client:
        async with _patches(chat):
            for kind, question in (("faq", "退货期限是多久？"),
                                   ("order", "ORD-1001 到哪一步了？"),
                                   ("compound", "ORD-1001 到哪了？还能退吗？")):
                thread = f"{TENANT}:baseline-{kind}-{uuid.uuid4().hex[:6]}"
                turn = f"{kind}-{uuid.uuid4().hex[:6]}"
                _arm(model, kind)
                payload = {"thread_id": thread, "user_message": question, "turn_id": turn}
                normal = await client.post("/agent/chat", json=payload, headers=_headers())
                _arm(model, kind)
                streamed = await client.post("/agent/chat/stream", json=payload, headers=_headers())
                rows.append({
                    "scenario": kind,
                    "request": payload,
                    "json": normal.json(),
                    "sse": _sse(streamed.text),
                    "checkpoint_key": f"support_v2:{thread}",
                    "history": await service.history(TENANT, thread),
                })

        # 未完成审批：在旧键上造一个真中断
        thread = f"{TENANT}:baseline-approval-{uuid.uuid4().hex[:6]}"
        await _pause(saver, thread)
        rejected = await client.post("/agent/chat/v2", json={
            "thread_id": thread, "user_message": "转人工", "turn_id": "approval-1"},
            headers=_headers())
        rows.append({
            "scenario": "pending_approval",
            "request": {"thread_id": thread, "user_message": "转人工", "turn_id": "approval-1"},
            "status": rejected.status_code,
            "detail": rejected.json().get("detail"),
            "error_code": (rejected.json().get("detail") or {}).get("error_code"),
            "approval_state": (await approval.approval_state(app, thread)).value,
            "checkpoint_keys": [f"support_v2:{thread}", thread],
            "legacy_payload": await approval.pending_approval_payload(app, thread),
        })

        # 旧审批恢复：在原键上 resume，之后不再 pending
        before = (await approval.approval_state(app, thread)).value
        resumed = await _resume_paused(saver, thread)
        rows.append({
            "scenario": "legacy_resume",
            "thread_id": thread,
            "state_before": before,
            "state_after": (await approval.approval_state(app, thread)).value,
            "resumed_answer": resumed,
            "checkpoint_key": thread,
        })
    return rows


async def _pause(saver, thread: str) -> None:
    from langchain_core.messages import AIMessage
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.types import interrupt

    async def ask_human(_state):
        interrupt({"action": "transfer_to_human", "reason": "等待人工确认"})
        return {"messages": [AIMessage(content="已建单")]}

    graph = StateGraph(MessagesState)
    graph.add_node("ask", ask_human)
    graph.add_edge(START, "ask")
    graph.add_edge("ask", END)
    await graph.compile(checkpointer=saver).ainvoke(
        {"messages": [AIMessage(content="我要转人工")]},
        {"configurable": {"thread_id": thread}})


async def _resume_paused(saver, thread: str) -> str:
    from langchain_core.messages import AIMessage
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.types import Command, interrupt

    async def ask_human(_state):
        decision = interrupt({"action": "transfer_to_human"})
        return {"messages": [AIMessage(content=f"工单 {decision.get('ticket_id')} 已创建")]}

    graph = StateGraph(MessagesState)
    graph.add_node("ask", ask_human)
    graph.add_edge(START, "ask")
    graph.add_edge("ask", END)
    # 同一个 saver 上同名线程 = 同一个检查点，resume 即在原键上继续
    result = await graph.compile(checkpointer=saver).ainvoke(
        Command(resume={"confirmed": True, "ticket_id": "T-BASE"}),
        {"configurable": {"thread_id": thread}})
    return str(result["messages"][-1].content)


# ============================ 实链臂 ============================

async def _live(args: argparse.Namespace) -> list[dict]:
    import httpx

    headers = {"X-Internal-Token": args.token, "X-Tenant-Id": args.tenant,
               "X-Trace-Id": f"baseline-{uuid.uuid4().hex[:8]}"}
    base = args.base_url.rstrip("/")
    rows: list[dict] = []
    async with httpx.AsyncClient(base_url=base, timeout=90) as client:
        for index, question in enumerate(args.questions):
            thread = f"{args.tenant}:baseline-{uuid.uuid4().hex[:8]}"
            turn = f"baseline-{index}-{uuid.uuid4().hex[:6]}"
            payload = {"thread_id": thread, "user_message": question, "turn_id": turn}
            normal = await client.post("/agent/chat", json=payload, headers=headers)
            streamed = await client.post("/agent/chat/stream", json=payload, headers=headers)
            checkpoints = await client.get(f"/agent/checkpoint/{thread}", headers=headers)
            rows.append({
                "scenario": f"live_{index}",
                "request": payload,
                "json": normal.json(),
                "sse": _sse(streamed.text),
                "checkpoints": checkpoints.json() if checkpoints.status_code == 200 else
                {"status": checkpoints.status_code, "text": checkpoints.text[:400]},
            })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--tenant", default=TENANT)
    parser.add_argument("--token", default=os.getenv("INTERNAL_API_TOKEN", ""))
    parser.add_argument("--out", default=str(OUT),
                        help="输出 jsonl（默认 evaluation/four_agent/runs/contract_baseline.jsonl）")
    parser.add_argument("--normalize", metavar="PATH",
                        help="只把既有样本归一化后打到 stdout（给 diff 用，不采集）")
    parser.add_argument("--compare", metavar="PATH",
                        help="采集后与给定基线逐行归一化比较；有差异即退出码 1")
    parser.add_argument("--questions", nargs="*", default=[
        "退货期限是多久？", "ORD-1001 到哪一步了？", "ORD-1001 到哪了？还能退吗？"])
    args = parser.parse_args()

    if args.normalize:
        for line in read_rows(Path(args.normalize)):
            print(line)
        return 0

    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    rows = asyncio.run(_offline() if args.mode == "offline" else _live(args))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rendered = [json.dumps({"mode": args.mode, "captured_at": started, **row},
                           ensure_ascii=False, default=str) for row in rows]
    out.write_text("\n".join(rendered) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} rows → {out}")

    if not args.compare:
        return 0
    fresh = [normalize_row(json.loads(line)) for line in rendered]
    base = read_rows(Path(args.compare))
    if fresh == base:
        print(f"契约未变：{len(fresh)} 行与 {args.compare} 归一化后逐行一致")
        return 0
    import difflib

    print(f"契约有变化（左=本次 {out}，右=基线 {args.compare}）：", file=sys.stderr)
    for line in difflib.unified_diff(base, fresh, fromfile=str(args.compare),
                                     tofile=str(out), lineterm="", n=1):
        print(line[:400], file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())


__all__: list[Any] = []
