# ruff: noqa: RUF001  # 断言文案含中文
"""同一 ``thread_id`` 的八步连续演练：业务状态先行 + Agent 单入口。

这是一条**端到端**的判据，不是单元测试的堆叠：真的 Worker（``business.chat.worker``）
打真的 Agent app（``app/api/chat.py``，uvicorn 真 HTTP 往返），只有两样东西是替身——

* MySQL：用一个内存工单/轮次台账（``FakeDesk``）替，它实现与生产相同的语义：
  ``status=0`` 排队、``status=1`` 人工接管、``status=2`` 关闭；``complete_turn`` 按
  ``turn_id`` 幂等，重复提交返回 False。
* 旧图：只在一个线程上造一个真 ``interrupt``，并用桩 ``get_tenant_agent`` 指向同一张图，
  以便 ``/agent/resume`` 真的把它恢复掉。

八步都落在同一条线程上（``t_demo:drill``），每一步核对四条不变量：

1. 每一轮**恰好一条**最终答复；
2. 全线程**最多一张有效工单**（``status IN (0,1)``）；
3. 新提问**绝不**调用旧 Agent（``get_tenant_agent`` 只允许被 resume 触达）；
4. 排队期间、审批未完成期间都不再问 Agent（``/agent/chat*`` 计数为 0）。
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python_business"))

worker = pytest.importorskip("business.chat.worker")
tickets_mod = pytest.importorskip("business.admin.tickets")
recovery = pytest.importorskip("business.chat.recovery")

from types import SimpleNamespace  # noqa: E402

from app.agent.legacy_approval.approval import ApprovalState, approval_state  # noqa: E402
from app.api import chat  # noqa: E402
from app.services import support_runtime  # noqa: E402
from evaluation.four_agent.capture_contract_baseline import _arm, _service  # noqa: E402

pytestmark = pytest.mark.asyncio

TENANT = "t_demo"
THREAD = "t_demo:drill"
COUNTS: dict[str, int] = {}
LEGACY_CALLS: list[str] = []


# ---------------------------- 替身：业务台账 ----------------------------

class FakeDesk:
    """内存版工单 / 轮次台账（语义与 MySQL 侧一致：状态机 + 幂等提交）。"""

    def __init__(self) -> None:
        self.tickets: list[dict] = []
        self.answers: list[dict] = []
        self.failures: list[tuple[str, str]] = []
        self.events: list[dict] = []
        self.recoveries: list[dict] = []
        self.turns: dict[str, dict] = {}
        self.lease_ok = True
        self._seq = 0
        self._msg = 100

    # --- 供 worker 调用的四个读 ---
    def handling_ticket(self, _customer):
        return next((t for t in self.tickets if t["status"] == 1), None)

    def waiting_ticket(self, _customer):
        return next((t for t in self.tickets if t["status"] == 0), None)

    def ensure_customer_ticket(self, _customer, reason):
        existing = next((t for t in self.tickets if t["status"] in (0, 1)), None)
        if existing is not None:
            existing["reason"] = reason
            return existing, True
        self._seq += 1
        ticket = {"id": self._seq, "ticket_no": f"TK-{self._seq:04d}", "status": 0,
                  "reason": reason, "created_at": None, "thread_id": _customer.thread_id,
                  "resume_state": "none", "resume_attempts": 0}
        self.tickets.append(ticket)
        return ticket, False

    def complete_turn(self, row, *, answer, result_type="ai", ticket_id=None,
                      ticket_no="", notice="", answer_role=""):
        state = self.turns.setdefault(row["turn_id"], {"status": "running"})
        if state["status"] != "running":
            return False                      # 租约栅栏：重复提交不再写第二条
        state["status"] = "completed"
        self.answers.append({"turn_id": row["turn_id"], "answer": answer,
                             "result_type": result_type, "ticket_no": ticket_no,
                             "notice": notice, "answer_role": answer_role})
        return True

    def record_failure(self, _row, code, message):
        self.failures.append((code, message))
        return "queued"

    def notify(self, _row, payload, **_kwargs):
        self.events.append(payload)

    def send_reviewed_answer(self, _row, answer):
        self.events.append({"type": "chat_end", "msg": answer})

    # --- 供断言用的动作 ---
    def take_over(self):
        ticket = self.waiting_ticket(None)
        assert ticket is not None, "接管前必须有一张排队中的工单"
        ticket["status"] = 1

    def close(self):
        for ticket in self.tickets:
            if ticket["status"] in (0, 1):
                ticket["status"] = 2

    def active_tickets(self):
        return [t for t in self.tickets if t["status"] in (0, 1)]

    def answers_for(self, turn_id):
        return [row for row in self.answers if row["turn_id"] == turn_id]

    def install(self, monkeypatch):
        monkeypatch.setattr(worker, "handling_ticket", self.handling_ticket)
        monkeypatch.setattr(worker, "waiting_ticket", self.waiting_ticket)
        monkeypatch.setattr(worker, "ensure_customer_ticket", self.ensure_customer_ticket)
        monkeypatch.setattr(worker, "complete_turn", self.complete_turn)
        monkeypatch.setattr(worker, "record_failure", self.record_failure)
        monkeypatch.setattr(worker, "notify", self.notify)
        monkeypatch.setattr(worker, "send_reviewed_answer", self.send_reviewed_answer)
        monkeypatch.setattr(worker, "lease_held", lambda _row: self.lease_ok)
        monkeypatch.setattr(worker, "extract_from_content", lambda _c: {})
        monkeypatch.setattr(worker, "upsert_extracted_profile", lambda *a: None)
        monkeypatch.setattr(worker, "iso", lambda _v: "created")
        # 恢复结果记账：真实现写 MySQL，这里记在台账上（语义：待办 + 次数 + 下次重试）
        monkeypatch.setattr(recovery, "record", self.record_resume)
        real_recover = recovery.recover_thread

        def recover_thread(*args, **kwargs):
            self.recoveries.append({"thread_id": args[0],
                                    "ticket_no": kwargs.get("ticket_no", ""),
                                    "confirmed": kwargs.get("confirmed", True),
                                    "trace_id": kwargs.get("trace_id", "")})
            return real_recover(*args, **kwargs)

        monkeypatch.setattr(recovery, "recover_thread", recover_thread)

    def record_resume(self, ticket_id, result):
        row = next((t for t in self.tickets if t["id"] == ticket_id), None)
        if row is None:
            return
        if result.settled:
            row["resume_state"] = "done"
            row["resume_error"] = result.outcome.value
        else:
            row["resume_state"] = "pending"
            row["resume_error"] = result.error_code or result.outcome.value
            row["resume_attempts"] = row.get("resume_attempts", 0) + 1

    def pending_resumes(self):
        return [t for t in self.tickets if t.get("resume_state") == "pending"]

    def recover_due(self, monkeypatch):
        """后台重试：把台账里"待恢复"的工单当作 claim_due 的返回，跑一次真恢复。"""
        rows = [{"id": t["id"], "thread_id": t["thread_id"], "ticket_no": t["ticket_no"],
                 "approval_resume_attempts": t.get("resume_attempts", 0)}
                for t in self.pending_resumes()]
        monkeypatch.setattr(recovery, "claim_due", lambda limit=20: list(rows))
        return recovery.recover_due()


# ------------------------- 探针可坏的 saver -------------------------

class DrillSaver(InMemorySaver):
    """真 ``InMemorySaver`` + 一个可切换的"探测失败"开关（模拟 PG 抖动）。

    必须是 ``BaseCheckpointSaver`` 的子类：LangGraph 在 ``compile`` 时做 isinstance 校验，
    一个纯代理对象会被拒（这也是提醒——检查点后端只能是 LangGraph 认的那几种）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.probe_broken = False

    async def aget_tuple(self, config):
        if self.probe_broken:
            raise RuntimeError("checkpoint probe down")
        return await super().aget_tuple(config)


# ------------------------------ 旧图桩 ------------------------------

def _legacy_graph(saver):
    async def ask_human(_state):
        decision = interrupt({"action": "transfer_to_human", "reason": "等待人工确认"})
        return {"messages": [AIMessage(content=f"工单 {decision.get('ticket_id')} 已创建")]}

    graph = StateGraph(MessagesState)
    graph.add_node("ask", ask_human)
    graph.add_edge(START, "ask")
    graph.add_edge("ask", END)
    return graph.compile(checkpointer=saver)


# ------------------------------ 服务装配 ------------------------------

def _build_app(saver: DrillSaver):
    service, _saver, model = _service()
    legacy = _legacy_graph(saver)

    app = FastAPI()
    app.state.db_pool = None
    app.state.checkpointer = saver
    app.include_router(chat.router)
    support_runtime.runtime_for(app).bind(TENANT, service)

    @app.middleware("http")
    async def _count(request, call_next):
        COUNTS[request.url.path] = COUNTS.get(request.url.path, 0) + 1
        return await call_next(request)

    @app.post("/_drill/pause")
    async def _pause_thread(payload: dict):
        await legacy.ainvoke(
            {"messages": [HumanMessage(content="我要转人工")]},
            {"configurable": {"thread_id": payload["thread_id"]}})
        return {"ok": True}

    @app.post("/_drill/break_probe")
    async def _break_probe(payload: dict):
        saver.probe_broken = bool(payload.get("broken"))
        return {"broken": saver.probe_broken}

    async def _fake_agent(_app, _tenant_id):
        LEGACY_CALLS.append("get_tenant_agent")
        return legacy

    return app, model, legacy, _fake_agent


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Server:
    """uvicorn 跑在后台线程：Worker 的同步 httpx 走真 TCP，Agent 侧用真事件循环。"""

    def __init__(self, app: FastAPI) -> None:
        import uvicorn

        self.port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                log_level="warning", lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 30
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        assert self.server.started, "Agent 本地服务没起来"
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *_exc):
        self.server.should_exit = True
        self.thread.join(timeout=15)


def _row(turn_id: str, text: str, desk: FakeDesk, *, msg_type: str = "text",
         attempt: int = 1) -> dict:
    desk._msg += 1
    return {"id": desk._msg, "turn_id": turn_id, "tenant_id": TENANT,
            "customer_id": f"{TENANT}:buyer1", "thread_id": THREAD,
            "user_message_id": desk._msg, "user_message": text, "msg_type": msg_type,
            "attempt_count": attempt, "lease_token": f"lease-{turn_id}-{attempt}"}


@pytest.fixture()
def drill(monkeypatch):
    """装好替身 + 起本地 Agent 服务；退出时把 patch 全撤掉。"""
    COUNTS.clear()
    LEGACY_CALLS.clear()
    desk = FakeDesk()
    saver = DrillSaver()
    app, model, legacy, fake_agent = _build_app(saver)
    desk.install(monkeypatch)

    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, patch

    @asynccontextmanager
    async def _conn(_app):
        yield object()

    patches = [patch.object(chat, "_pool_conn", _conn),
               patch.object(chat, "check_concurrent_sessions", AsyncMock(return_value=None)),
               patch.object(chat, "touch_session_retry", AsyncMock()),
               patch.object(chat, "_record_usage_after_chat", AsyncMock()),
               patch("app.agent.builder.get_tenant_agent", fake_agent)]
    for item in patches:
        item.start()
    with _Server(app) as base_url:
        monkeypatch.setattr(worker, "AGENT_URL", base_url)
        monkeypatch.setattr(tickets_mod, "AGENT_URL", base_url)
        monkeypatch.setattr(recovery, "AGENT_URL", base_url)
        yield desk, model, saver, base_url
    for item in reversed(patches):
        item.stop()


async def _step(*, model, desk, turn_id: str, text: str, arm: str = "faq",
                msg_type: str = "text", attempt: int = 1) -> dict:
    """跑一轮 Worker（同步实现放到线程里），返回本轮的答复台账。"""
    if arm:
        _arm(model, arm)
    row = _row(turn_id, text, desk, msg_type=msg_type, attempt=attempt)
    await asyncio.to_thread(worker.process_claim, row)
    return row


# ================================ 八步 ================================

async def test_same_thread_walks_the_whole_lifecycle(drill) -> None:
    desk, model, saver, base_url = drill

    # 1) 普通问答：进四 Agent，一条答复，零工单
    await _step(model=model, desk=desk, turn_id="t1", text="退货期限是多久？")
    assert len(desk.answers_for("t1")) == 1
    assert desk.tickets == []
    assert COUNTS.get("/agent/chat/stream") == 1
    assert LEGACY_CALLS == []

    # 2) 主动转人工：业务建单，不问 Agent
    before = COUNTS.get("/agent/chat/stream", 0)
    recoveries_before = len(desk.recoveries)
    await _step(model=model, desk=desk, turn_id="t2", text="转人工", arm="")
    assert len(desk.answers_for("t2")) == 1
    assert len(desk.active_tickets()) == 1
    assert COUNTS.get("/agent/chat/stream", 0) == before, "点名转人工不该再问 Agent"
    # 转人工也要看一眼旧审批（这条路径不经过 Agent 闸门）；此处确认没有待审批
    assert len(desk.recoveries) == recoveries_before + 1
    assert desk.tickets[0]["resume_state"] == "done"

    # 3) 排队期间发新消息：确定提示，不建第二张单、不问 Agent
    await _step(model=model, desk=desk, turn_id="t3", text="还在吗？", arm="")
    assert len(desk.answers_for("t3")) == 1
    assert "等待人工接入" in desk.answers_for("t3")[0]["answer"]
    assert len(desk.active_tickets()) == 1
    assert COUNTS.get("/agent/chat/stream", 0) == before, "排队期间绝不能问 Agent"

    # 4) 人工接管：消息转人工，仍是一张单
    desk.take_over()
    await _step(model=model, desk=desk, turn_id="t4", text="我要改地址", arm="")
    assert desk.answers_for("t4")[0]["result_type"] == "human"
    assert len(desk.active_tickets()) == 1
    assert COUNTS.get("/agent/chat/stream", 0) == before, "人工接管期间不问 Agent"

    # 5) 关闭工单后继续 AI：回到四 Agent
    desk.close()
    await _step(model=model, desk=desk, turn_id="t5", text="退货期限是多久？")
    assert len(desk.answers_for("t5")) == 1
    assert desk.answers_for("t5")[0]["result_type"] == "ai"
    assert desk.active_tickets() == []
    assert COUNTS.get("/agent/chat/stream", 0) == before + 1
    assert LEGACY_CALLS == []

    # 6) 旧审批恢复：Agent 409 → 建单 + resume → 本轮提交为 ticket
    async with httpx.AsyncClient(base_url=base_url) as client:
        await client.post("/_drill/pause", json={"thread_id": THREAD})
    streamed_before = COUNTS.get("/agent/chat/stream", 0)
    await _step(model=model, desk=desk, turn_id="t6", text="包邮规则是什么", arm="")
    assert len(desk.answers_for("t6")) == 1
    assert desk.answers_for("t6")[0]["result_type"] == "ticket"
    assert len(desk.active_tickets()) == 1, "全程最多一张有效工单"
    assert desk.recoveries[-1]["ticket_no"] == desk.active_tickets()[0]["ticket_no"]
    assert desk.recoveries[-1]["trace_id"] == "t6"
    assert COUNTS.get("/agent/chat/stream", 0) == streamed_before + 1, "409 是一次真实请求"
    assert LEGACY_CALLS == ["get_tenant_agent"], "旧 Agent 只因 resume 被触达"
    # resume 之后这条线程不再停在旧审批上（直接问探测函数，跳过只读 SQL 视图）
    probe_app = SimpleNamespace(state=SimpleNamespace(checkpointer=saver))
    assert await approval_state(probe_app, THREAD) is ApprovalState.CLEAR

    # 7) 检查点短暂故障：503 → 不建单、不提交答复、可重试
    #    先把上一轮升级出来的工单结掉（人工接管后关闭），否则分流会停在"排队提示"，
    #    根本走不到 Agent 的 503 —— 那本身也是一条要成立的行为（见第 3 步）。
    desk.close()
    async with httpx.AsyncClient(base_url=base_url) as client:
        await client.post("/_drill/break_probe", json={"broken": True})
    answers_before = len(desk.answers)
    tickets_before = len(desk.tickets)
    await _step(model=model, desk=desk, turn_id="t7", text="退货期限是多久？")
    assert len(desk.answers) == answers_before, "状态未知时不能给客户编答案"
    assert len(desk.tickets) == tickets_before, "状态未知时不能建单"
    assert [code for code, _msg in desk.failures] == ["approval_state_unknown"]

    # 8) Worker 重试：故障恢复后同一条 turn_id 重跑，只落一条答复
    async with httpx.AsyncClient(base_url=base_url) as client:
        await client.post("/_drill/break_probe", json={"broken": False})
    await _step(model=model, desk=desk, turn_id="t7", text="退货期限是多久？", attempt=2)
    assert len(desk.answers_for("t7")) == 1, "重试不得产生第二条答复"
    assert LEGACY_CALLS == ["get_tenant_agent"]

    # 全程核对：每轮恰好一条答复，任何时刻最多一张有效工单
    per_turn = {turn: len(desk.answers_for(turn)) for turn in
                ("t1", "t2", "t3", "t4", "t5", "t6", "t7")}
    assert per_turn == {turn: 1 for turn in per_turn}, per_turn
    assert len(desk.tickets) == 2, "只有一次人工升级 + 一次旧审批各建了一张"
    assert desk.active_tickets() == [], "收尾时两张工单都已关闭"
    assert len({t["ticket_no"] for t in desk.tickets}) == 2, "工单号不复用"
    assert "/agent/chat" not in COUNTS, "Worker 不再走 JSON 分支"


# ====================== 恢复闭环：旧审批没恢复完的四种情形 ======================

async def _pause_thread(base_url: str, thread: str = THREAD) -> None:
    """在旧图上造一条真中断（用业务侧的 /agent/resume 之外的手段：直接 ainvoke）。"""
    async with httpx.AsyncClient(base_url=base_url) as client:
        response = await client.post("/_drill/pause", json={"thread_id": thread})
    assert response.status_code == 200


async def _state(saver, thread: str = THREAD):
    return await approval_state(SimpleNamespace(state=SimpleNamespace(checkpointer=saver)),
                                thread)


def _patch_post_after_success(monkeypatch, real_post):
    """服务端恢复成功，但客户端拿到超时 —— 最容易误判成"没恢复"的一种。"""
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        real_post(*args, **kwargs)
        raise httpx.ReadTimeout("client gave up after the server finished")

    monkeypatch.setattr(recovery.httpx, "post", flaky)
    return calls


async def test_direct_handoff_also_closes_a_pending_approval(drill) -> None:
    """客户直接说"转人工"：不过 Agent 闸门，但旧审批同样要被收口。"""
    desk, model, saver, base_url = drill
    await _pause_thread(base_url)
    assert await _state(saver) is ApprovalState.PENDING

    await _step(model=model, desk=desk, turn_id="h1", text="转人工", arm="")

    assert len(desk.active_tickets()) == 1
    assert desk.answers_for("h1")[0]["result_type"] == "ticket"
    assert await _state(saver) is ApprovalState.CLEAR, "转人工后旧审批必须已经恢复"
    assert desk.tickets[0]["resume_state"] == "done"


async def test_resume_failure_is_recorded_then_recovered_in_the_background(
        drill, monkeypatch) -> None:
    """恢复失败 → 记成待办 → 排队期间不给新答复 → 后台重试收口（模拟进程重启后再跑）。"""
    desk, model, saver, base_url = drill
    await _pause_thread(base_url)

    real_resume_once = recovery.resume_once
    # 只让 resume 这一步失败（探测仍然打真服务，所以状态会如实报 pending）
    monkeypatch.setattr(recovery, "resume_once",
                        lambda *_a, **_kw: recovery.ResumeResult(
                            recovery.ResumeOutcome.UNAVAILABLE, error_code="resume_timeout"))
    await _step(model=model, desk=desk, turn_id="r1", text="包邮规则是什么", arm="")

    assert desk.tickets[0]["resume_state"] == "pending"
    assert desk.tickets[0]["resume_error"] == "resume_timeout"
    assert await _state(saver) is ApprovalState.PENDING, "没恢复就是没恢复，不能装作 clear"
    assert desk.answers_for("r1")[0]["result_type"] == "ticket"

    # 排队期间客户再发消息：只拿到等待提示，不会再去问 Agent（这条路走不到 409）
    before = COUNTS.get("/agent/chat/stream", 0)
    await _step(model=model, desk=desk, turn_id="r2", text="还在吗？", arm="")
    assert COUNTS.get("/agent/chat/stream", 0) == before
    assert desk.answers_for("r2")[0]["result_type"] == "ticket"
    assert await _state(saver) is ApprovalState.PENDING

    # 后台重试（等价于容器重启后后台线程继续做）：不需要客户再发消息
    monkeypatch.setattr(recovery, "resume_once", real_resume_once)
    stats = desk.recover_due(monkeypatch)

    assert stats == {"claimed": 1, "recovered": 1, "still_pending": 0}
    assert await _state(saver) is ApprovalState.CLEAR
    assert desk.tickets[0]["resume_state"] == "done"
    assert len(desk.answers) == 2, "后台恢复不能再给客户发第二条消息"


async def test_timeout_after_the_server_recovered_is_not_a_second_resume(
        drill, monkeypatch) -> None:
    """服务端已恢复、客户端超时：复查结案，绝不能因此再跑一轮旧图。"""
    desk, model, saver, base_url = drill
    await _pause_thread(base_url)
    calls = _patch_post_after_success(monkeypatch, httpx.post)

    result = await asyncio.to_thread(recovery.recover_thread, THREAD,
                                     ticket_no="TK-manual", trace_id="manual")

    assert result.outcome is recovery.ResumeOutcome.RECOVERED
    assert result.detail["verified_after_failure"] is True
    assert len(calls) == 1, "只应发生一次 resume，复查用的是探测而不是再 resume"
    assert await _state(saver) is ApprovalState.CLEAR
    assert LEGACY_CALLS == ["get_tenant_agent"]


async def test_resume_without_a_pending_approval_is_clear_not_recovered(drill) -> None:
    """`success=false` 是"本来就没有待审批"，不是"恢复成功"。"""
    _desk, _model, saver, _base = drill
    result = await asyncio.to_thread(recovery.resume_once, THREAD)

    assert result.outcome is recovery.ResumeOutcome.CLEAR
    assert result.content == ""
    assert result.error_code == "no_pending_approval"
    assert LEGACY_CALLS == [], "没有待审批时不该跑旧图"


async def test_a_lost_lease_creates_no_ticket_and_no_resume(drill) -> None:
    """失去租约的 Worker 不许产生外部副作用：建单与 resume 都收不回来。"""
    desk, model, saver, base_url = drill
    await _pause_thread(base_url)
    desk.lease_ok = False

    await _step(model=model, desk=desk, turn_id="l1", text="包邮规则是什么", arm="")

    assert desk.tickets == [], "没有租约就不建单"
    assert desk.recoveries == [], "没有租约就不 resume"
    assert desk.answers == [], "也不能提交答复"
    assert desk.failures == [], "这不是失败，是租约已被别人接管"
    assert await _state(saver) is ApprovalState.PENDING
    assert LEGACY_CALLS == []
