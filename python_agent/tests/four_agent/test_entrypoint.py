# ruff: noqa: RUF001  # 断言文案含中文
"""对等入口（/agent/chat/v2）在真实 ASGI 栈上的契约验证（不需要模型）。

覆盖验收要求的三件事：JSON 与 SSE 内容一致、重试不追加第二轮、身份不可核验时 fail-closed。
另外钉住两条本轮收敛后的语义：

* ``SUPPORT_AGENT_V2`` 已退役 —— 置 off 也不能把普通聊天切走主图；
* 是否回旧图只由**服务端检查点**里那个真实中断决定，请求体 ``has_pending_ticket=True``
  不再能单独把它推回旧图。
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

from app.agent.support.fakes import ScriptedSupportModel, offline_deps
from app.agent.support.service import SupportService
from app.api.chat_v2 import create_standalone_app

pytestmark = pytest.mark.asyncio

TENANT = "t_demo"
THREAD = "t_demo:sess-http-1"
ANSWER = "签收后 7 天内可申请退货，运费自理。"


def _service(saver):
    async def deps_for_tenant(_tenant_id):
        deps, _model, _events, _captured = offline_deps(
            model=ScriptedSupportModel(supervisor_script=[
                AIMessage(content=ANSWER,
                          usage_metadata={"input_tokens": 8, "output_tokens": 4,
                                          "total_tokens": 12})]))
        return deps

    return SupportService(deps_for_tenant, checkpointer=saver)


def build_client(saver=None):
    saver = saver or InMemorySaver()
    service = _service(saver)
    app = create_standalone_app(identity=lambda _request, thread_id: (TENANT, thread_id),
                                service=service, checkpointer=saver)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://svc"), service


def _paused_legacy_graph(saver):
    """一个真实的旧图中断态：审批还没被恢复，检查点里留着 __interrupt__。"""
    async def ask_human(_state):
        decision = interrupt({"action": "transfer_to_human", "reason": "等待人工确认"})
        return {"messages": [AIMessage(content=f"工单 {decision.get('ticket_id')} 已创建。")]}

    graph = StateGraph(MessagesState)
    graph.add_node("ask", ask_human)
    graph.add_edge(START, "ask")
    graph.add_edge("ask", END)
    return graph.compile(checkpointer=saver)


async def test_json_and_sse_return_identical_content() -> None:
    client, _service_ = build_client()
    body = {"thread_id": THREAD, "user_message": "退货期限是多久？", "turn_id": "h1"}
    json_response = await client.post("/agent/chat/v2", json=body)
    sse_response = await client.post("/agent/chat/v2/stream", json=body)

    assert json_response.status_code == 200 and sse_response.status_code == 200
    payload = json_response.json()
    events = [line[len("data: "):] for line in sse_response.text.strip().split("\n\n") if line]
    chunk = json.loads(events[0])
    end = json.loads(events[1])
    assert chunk["content"] == payload["content"] == end["content"]
    assert end["type"] == "end" and end["outcome"] == payload["outcome"] == "answered"
    assert end["turn_id"] == "h1" and end["answer_id"] == "h1"
    assert payload["turn_id"] == payload["answer_id"] == "h1"
    # 派给了谁是引擎的差别（tools 直答 ⇒ 空；nodes 会把政策问题补派给 FAQ），这条钉的是
    # 「JSON 与 SSE 是同一份内容、没有 error」，不要在这里顺手钉死角色集合。
    assert payload["error"] == ""
    assert set(payload["agents"]) <= {"faq", "order", "general"}


async def test_retried_request_does_not_append_a_second_turn() -> None:
    client, service = build_client()
    body = {"thread_id": THREAD, "user_message": "退货期限是多久？", "turn_id": "h2"}
    first = await client.post("/agent/chat/v2", json=body)
    replay = await client.post("/agent/chat/v2", json=body)
    assert first.json()["content"] == replay.json()["content"]
    history = await service.history(TENANT, THREAD)
    assert [m["message_id"] for m in history] == ["h2:u", "h2:a"]


async def test_forged_pending_flag_cannot_divert_a_normal_turn() -> None:
    """请求体说"有工单"不再是执行路径的开关；服务端检查点说了才算。"""
    client, _service_ = build_client()
    response = await client.post("/agent/chat/v2", json={
        "thread_id": THREAD, "user_message": "退货期限是多久？",
        "has_pending_ticket": True, "turn_id": "h4"})
    assert response.status_code == 200
    assert response.json()["success"] is True


async def test_real_pending_approval_stays_on_the_legacy_entry() -> None:
    saver = InMemorySaver()
    legacy = _paused_legacy_graph(saver)
    thread = "t_demo:sess-approval-http"
    await legacy.ainvoke({"messages": [HumanMessage(content="我要转人工")]},
                         {"configurable": {"thread_id": thread}})
    client, _service_ = build_client(saver=saver)
    response = await client.post("/agent/chat/v2", json={
        "thread_id": thread, "user_message": "转人工"})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error_code"] == "approval_pending"
    assert detail["resume_endpoint"] == "/agent/resume"
    assert detail["checkpoint_keys"] == [f"support_v2:{thread}", thread]


async def test_retired_rollout_flag_cannot_change_routing(monkeypatch) -> None:
    """``SUPPORT_AGENT_V2`` 退役后置 off 也不影响路由（普通聊天固定走主图）。"""
    monkeypatch.setenv("SUPPORT_AGENT_V2", "off")
    client, _service_ = build_client()
    response = await client.post("/agent/chat/v2", json={
        "thread_id": THREAD, "user_message": "退货期限是多久？", "turn_id": "h5"})
    assert response.status_code == 200
    assert response.json()["turn_id"] == "h5"


async def test_unverified_identity_fails_closed() -> None:
    app = create_standalone_app()  # the test resolver demands x-test-tenant
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://svc")
    response = await client.post("/agent/chat/v2", json={
        "thread_id": "t_demo:sess-x", "user_message": "你好"})
    assert response.status_code == 401
    # a tenant/thread mismatch is refused by thread_key downstream, and surfaced as a
    # 403 rather than a stack trace
    bad = await client.post("/agent/chat/v2", json={"thread_id": "other:sess-x",
                                                   "user_message": "你好"},
                            headers={"x-test-tenant": "t_demo"})
    assert bad.status_code == 403 and "verified" in bad.json()["detail"]
