# ruff: noqa: RUF001  # 断言文案含中文
"""旧图未完成审批：判定、恢复与检查点键（阶段5）。

三条不变量：

1. 判定读的是**检查点里那条真实中断**，不是请求体，也不是"有没有旧状态"；
2. 恢复只在旧键 ``{thread_id}`` 上做，绝不碰 ``support_v2:{thread_id}``；
3. 没有未完成审批时 resume 明确报错，不在空检查点上按默认分支编一句答复。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import Command, interrupt

from app.agent.legacy_approval import approval
from app.agent.support.service import NAMESPACE

TENANT = "t_demo"
THREAD = "t_demo:sess-approval-1"


def _app(saver=None):
    return SimpleNamespace(state=SimpleNamespace(checkpointer=saver))


async def _paused_thread(saver: InMemorySaver, thread: str = THREAD) -> None:
    async def ask_human(_state):
        decision = interrupt({"action": "transfer_to_human", "reason": "等待人工确认"})
        return {"messages": [AIMessage(content=f"工单 {decision.get('ticket_id')} 已创建。")]}

    graph = StateGraph(MessagesState)
    graph.add_node("ask", ask_human)
    graph.add_edge(START, "ask")
    graph.add_edge("ask", END)
    await graph.compile(checkpointer=saver).ainvoke(
        {"messages": [HumanMessage(content="我要转人工")]},
        {"configurable": {"thread_id": thread}})


def test_keys_are_namespaced_apart():
    assert approval.legacy_config("t_demo:x")["configurable"]["thread_id"] == "t_demo:x"
    assert approval.support_config("t_demo:x")["configurable"]["thread_id"] == \
        f"{NAMESPACE}:t_demo:x"
    assert approval.legacy_config("t_demo:x")["configurable"]["checkpoint_ns"] == ""


@pytest.mark.asyncio
async def test_unknown_thread_has_no_pending_approval():
    assert await approval.has_unfinished_approval(_app(InMemorySaver()), THREAD) is False


@pytest.mark.asyncio
async def test_missing_checkpointer_fails_soft():
    """没有持久后端时判 ``clear``：这条线程在这个进程里不可能有旧审批。

    这与"读失败"是两件事——见下面那条 ``unknown``。
    """
    assert await approval.has_unfinished_approval(_app(None), THREAD) is False


@pytest.mark.asyncio
async def test_missing_checkpointer_in_production_is_unknown():
    """生产（配了持久检查点）拿不到 saver 时**不能**放行。

    装配意外（DSN 写错、saver 初始化失败）会让所有线程都被判成"没有旧审批"，而旧审批
    其实还在数据库里——那正是新旧两条链路同会话并行的入口。所以这里必须 fail-closed。
    """
    app = _app(None)
    app.state.require_checkpointer = True

    assert await approval.approval_state(app, THREAD) is approval.ApprovalState.UNKNOWN
    with pytest.raises(approval.ApprovalStateUnknown):
        await approval.has_unfinished_approval(app, THREAD)


@pytest.mark.asyncio
async def test_offline_double_still_clears_without_a_saver():
    """离线替身没有 require 标记，保持原有行为（否则所有离线测试都会被 503 拦住）。"""
    app = _app(None)
    app.state.require_checkpointer = False
    assert await approval.approval_state(app, THREAD) is approval.ApprovalState.CLEAR


class _BrokenSaver(InMemorySaver):
    """模拟 DSN 抖动 / 表缺失：探测本身抛异常。"""

    async def aget_tuple(self, _config):
        raise RuntimeError("connection reset by peer")


@pytest.mark.asyncio
async def test_probe_failure_is_unknown_not_clear():
    """读失败**不等于**没有旧审批：必须产出第三种状态，而不是 False。

    这是本次收敛的关键一条：折成 False 会让新图在"旧审批其实还停着"的会话上回答
    新问题，客户随后会同时看到新图答复和恢复出来的旧工单，两套事实。
    """
    broken = _BrokenSaver()
    assert await approval.approval_state(_app(broken), THREAD) is approval.ApprovalState.UNKNOWN
    with pytest.raises(approval.ApprovalStateUnknown):
        await approval.has_unfinished_approval(_app(broken), THREAD)


@pytest.mark.asyncio
async def test_resume_refuses_when_the_state_is_unknown(monkeypatch):
    """状态未知时不能"试着恢复一下"：那会在空检查点上跑旧图。"""
    monkeypatch.setattr(approval, "approval_state",
                        AsyncMock(return_value=approval.ApprovalState.UNKNOWN))
    with pytest.raises(approval.ApprovalStateUnknown):
        await approval.resume_approval(_app(InMemorySaver()), TENANT, THREAD,
                                       confirmed=True, wait_time=3, ticket_id="T-1")


@pytest.mark.asyncio
async def test_a_real_interrupt_is_detected():
    saver = InMemorySaver()
    await _paused_thread(saver)
    assert await approval.has_unfinished_approval(_app(saver), THREAD) is True


@pytest.mark.asyncio
async def test_interrupt_payload_is_available_for_the_console():
    saver = InMemorySaver()
    await _paused_thread(saver)
    payload = await approval.pending_approval_payload(_app(saver), THREAD)
    assert payload == {"action": "transfer_to_human", "reason": "等待人工确认"}


@pytest.mark.asyncio
async def test_resume_without_a_pending_approval_is_refused():
    with pytest.raises(approval.NoPendingApproval):
        await approval.resume_approval(_app(InMemorySaver()), TENANT, THREAD,
                                       confirmed=True, wait_time=3, ticket_id="T-1")


@pytest.mark.asyncio
async def test_resume_runs_on_the_legacy_checkpoint_only(monkeypatch):
    saver = InMemorySaver()
    await _paused_thread(saver)
    seen: dict = {}

    class _LegacyGraph:
        async def ainvoke(self, command, config):
            seen["command"] = command
            seen["config"] = config
            return {"messages": [AIMessage(content="工单 T-1 已创建。")]}

    monkeypatch.setattr("app.agent.builder.get_tenant_agent",
                        AsyncMock(return_value=_LegacyGraph()))
    content = await approval.resume_approval(_app(saver), TENANT, THREAD,
                                             confirmed=True, wait_time=3, ticket_id="T-1")
    assert content == "工单 T-1 已创建。"
    assert isinstance(seen["command"], Command)
    assert seen["command"].resume == {"confirmed": True, "wait_time": 3, "ticket_id": "T-1"}
    assert seen["config"]["configurable"]["thread_id"] == THREAD, "必须落在旧键上"


@pytest.mark.asyncio
async def test_resume_does_not_touch_the_new_graph_key(monkeypatch):
    """恢复只动旧检查点：主图那把键在恢复前后都不该出现内容。"""
    saver = InMemorySaver()
    await _paused_thread(saver)

    class _LegacyGraph:
        async def ainvoke(self, _command, config):
            return {"messages": [AIMessage(content="完成")]}

    monkeypatch.setattr("app.agent.builder.get_tenant_agent",
                        AsyncMock(return_value=_LegacyGraph()))
    await approval.resume_approval(_app(saver), TENANT, THREAD,
                                   confirmed=True, wait_time=0, ticket_id="T-2")
    assert await saver.aget_tuple(approval.support_config(THREAD)) is None


def test_resume_endpoint_reports_a_missing_approval():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import chat
    from app.infra.config import INTERNAL_API_TOKEN

    app = FastAPI()
    app.state.db_pool = None
    app.include_router(chat.router)

    class _Pool:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return False

    with patch.object(chat, "_pool_conn", lambda _app: _Pool()), \
         patch.object(chat, "check_concurrent_sessions", AsyncMock(return_value=None)), \
         patch.object(chat, "touch_session_retry", AsyncMock()), \
         patch.object(chat, "resume_approval",
                      AsyncMock(side_effect=approval.NoPendingApproval("没有待审批"))):
        with TestClient(app) as client:
            response = client.post("/agent/resume",
                                   json={"thread_id": THREAD, "confirmed": True,
                                         "wait_time": 0, "ticket_id": "T-9"},
                                   headers={"X-Internal-Token": INTERNAL_API_TOKEN,
                                            "X-Tenant-Id": TENANT})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["error_code"] == "no_pending_approval"
    assert body["content"] == "没有待审批"


def test_resume_endpoint_reports_an_unknown_state_as_retryable():
    """``/agent/resume`` 遇到读失败报 ``approval_state_unknown``（业务层据此重试）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import chat
    from app.infra.config import INTERNAL_API_TOKEN

    app = FastAPI()
    app.state.db_pool = None
    app.include_router(chat.router)

    class _Pool:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return False

    with patch.object(chat, "_pool_conn", lambda _app: _Pool()), \
         patch.object(chat, "check_concurrent_sessions", AsyncMock(return_value=None)), \
         patch.object(chat, "touch_session_retry", AsyncMock()), \
         patch.object(chat, "resume_approval",
                      AsyncMock(side_effect=approval.ApprovalStateUnknown("读不到"))):
        with TestClient(app) as client:
            response = client.post("/agent/resume",
                                   json={"thread_id": THREAD, "confirmed": True,
                                         "wait_time": 0, "ticket_id": "T-9"},
                                   headers={"X-Internal-Token": INTERNAL_API_TOKEN,
                                            "X-Tenant-Id": TENANT})
    body = response.json()
    assert body["success"] is False
    assert body["error_code"] == "approval_state_unknown"
    assert "稍后重试" in body["content"]


def _probe_app(saver):
    from fastapi import FastAPI

    from app.api import chat

    app = FastAPI()
    app.state.db_pool = None
    app.state.checkpointer = saver
    app.include_router(chat.router)
    return app, chat


def test_probe_endpoint_reports_the_three_states():
    """业务层重试前要问的就是这一个端点：只回三态，不查检查点表。"""
    from fastapi.testclient import TestClient

    from app.infra.config import INTERNAL_API_TOKEN

    headers = {"X-Internal-Token": INTERNAL_API_TOKEN, "X-Tenant-Id": TENANT}

    for saver, expected in ((InMemorySaver(), "clear"), (_BrokenSaver(), "unknown")):
        app, _chat = _probe_app(saver)
        with TestClient(app) as client:
            response = client.get(f"/agent/approval/{THREAD}", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["approval_state"] == expected


@pytest.mark.asyncio
async def test_probe_endpoint_reports_pending_after_a_real_interrupt():
    from fastapi.testclient import TestClient

    from app.infra.config import INTERNAL_API_TOKEN

    saver = InMemorySaver()
    app, _chat = _probe_app(saver)
    await _paused_thread(saver)
    with TestClient(app) as client:
        response = client.get(f"/agent/approval/{THREAD}",
                              headers={"X-Internal-Token": INTERNAL_API_TOKEN,
                                       "X-Tenant-Id": TENANT})
    assert response.json()["approval_state"] == "pending"
