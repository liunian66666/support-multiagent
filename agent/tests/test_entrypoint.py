# ruff: noqa: RUF001  # 断言文案含中文
"""The mountable HTTP entry, exercised over a real ASGI stack (no model needed).

Covers what the phase-3 acceptance asks of the transport: JSON and SSE return the same
content, a retried request does not append a second copy of the turn, the rollout flag
and the pending-approval carve-out actually refuse the new path, and an identity header
that is missing fails closed.
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import InMemorySaver

from agent.entrypoint import create_standalone_app
from agent.fakes import ScriptedSupportModel, offline_deps
from agent.service import SupportService

pytestmark = pytest.mark.asyncio

TENANT = "t_demo"
THREAD = "t_demo:sess-http-1"


def build_client(monkeypatch, answer: str = "签收后 7 天内可申请退货，运费自理。"):
    monkeypatch.setenv("SUPPORT_AGENT_V2", "on")
    saver = InMemorySaver()

    async def deps_for_tenant(_tenant_id):
        from langchain_core.messages import AIMessage
        deps, _model, _events, _captured = offline_deps(
            model=ScriptedSupportModel(supervisor_script=[
                AIMessage(content=answer, usage_metadata={"input_tokens": 8, "output_tokens": 4,
                                                          "total_tokens": 12})]))
        return deps

    service = SupportService(deps_for_tenant, checkpointer=saver)
    app = create_standalone_app(identity=lambda _request, thread_id: (TENANT, thread_id),
                                service=service)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://svc"), service


async def test_json_and_sse_return_identical_content(monkeypatch) -> None:
    client, _service = build_client(monkeypatch)
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
    assert payload["agents"] == [] and payload["error"] == ""


async def test_retried_request_does_not_append_a_second_turn(monkeypatch) -> None:
    client, service = build_client(monkeypatch)
    body = {"thread_id": THREAD, "user_message": "退货期限是多久？", "turn_id": "h2"}
    first = await client.post("/agent/chat/v2", json=body)
    replay = await client.post("/agent/chat/v2", json=body)
    assert first.json()["content"] == replay.json()["content"]
    history = await service.history(TENANT, THREAD)
    assert [m["message_id"] for m in history] == ["h2:u", "h2:a"]


async def test_pending_approval_falls_back_to_the_legacy_entry(monkeypatch) -> None:
    client, _service = build_client(monkeypatch)
    response = await client.post("/agent/chat/v2", json={
        "thread_id": THREAD, "user_message": "转人工", "has_pending_ticket": True})
    assert response.status_code == 409
    assert "原入口" in response.json()["detail"]


async def test_rollout_flag_defaults_to_off(monkeypatch) -> None:
    monkeypatch.delenv("SUPPORT_AGENT_V2", raising=False)
    client, _service = build_client(monkeypatch)
    monkeypatch.delenv("SUPPORT_AGENT_V2", raising=False)
    response = await client.post("/agent/chat/v2", json={
        "thread_id": THREAD, "user_message": "退货期限是多久？"})
    assert response.status_code == 409


async def test_unverified_identity_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("SUPPORT_AGENT_V2", "on")
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
