"""JSON and SSE must use the same reviewed graph result without a bridge write."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import cutover
from agent.fakes import ScriptedSupportModel, offline_deps
from agent.service import SupportService

from app.api import chat
from app.infra.config import INTERNAL_API_TOKEN
from app.services import dialog


@asynccontextmanager
async def _connection(_app):
    yield object()


def _client():
    app = FastAPI()
    app.state.db_pool = None
    app.include_router(chat.router)
    return TestClient(app)


def _headers():
    return {"X-Internal-Token": INTERNAL_API_TOKEN, "X-Tenant-Id": "t_demo",
            "X-Agent-Graph": "v2", "X-Trace-Id": "canary-api-test-1"}


@pytest.mark.asyncio
async def test_override_usage_never_applies_another_models_price():
    billed = AIMessage(content="", response_metadata={
        "model_name": "glm-5.3", "model_source": "platform", "model_override": True,
    }, usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    with patch.object(dialog, "record_token_usage", AsyncMock()) as record, \
         patch.object(dialog, "current_chat_prices", AsyncMock(side_effect=AssertionError("wrong tariff"))):
        await dialog._record_usage_after_chat(SimpleNamespace(app=object()), object(),
                                              "t_demo", "t_demo:buyer1", [billed])
    kwargs = record.await_args.kwargs
    assert kwargs["fallback_model"] == "glm-5.3"
    assert kwargs["source"] == "platform"
    assert kwargs["chat_input_price"] == kwargs["chat_output_price"] == 0


def test_json_and_sse_share_reviewed_answer_and_skip_bridge():
    state = {"answer": "请提供订单号。", "model_messages": []}
    runner = AsyncMock(return_value=state)
    quota = AsyncMock(return_value=None)
    usage = AsyncMock()
    with patch.object(chat, "_pool_conn", _connection), \
         patch.object(chat, "check_concurrent_sessions", quota), \
         patch.object(chat, "touch_session_retry", AsyncMock()), \
         patch.object(chat, "_record_usage_after_chat", usage), \
         patch.object(chat.four_agent, "run_turn", runner), \
         patch.object(chat, "_record_to_checkpoint", AsyncMock()) as bridge:
        with _client() as client:
            payload = {"thread_id": "t_demo:canary_api", "user_message": "你好"}
            normal = client.post("/agent/chat", json=payload, headers=_headers())
            streamed = client.post("/agent/chat/stream", json=payload, headers=_headers())
    assert normal.status_code == 200
    assert normal.json()["success"] is True
    assert normal.json()["content"].startswith("请提供订单号。")
    assert streamed.status_code == 200
    assert '"type": "end"' in streamed.text
    assert "请提供订单号。" in streamed.text
    assert runner.await_count == 2
    bridge.assert_not_awaited()


def test_mounted_graph_replays_same_turn_without_second_bill():
    deps, model, _events, _captured = offline_deps(model=ScriptedSupportModel())

    async def deps_for_tenant(_tenant_id):
        return deps

    service = SupportService(deps_for_tenant, checkpointer=InMemorySaver())
    quota = AsyncMock(return_value=None)
    usage = AsyncMock()
    with patch.object(chat, "_pool_conn", _connection), \
         patch.object(chat, "check_concurrent_sessions", quota), \
         patch.object(chat, "touch_session_retry", AsyncMock()), \
         patch.object(chat, "_record_usage_after_chat", usage), \
         patch.object(cutover, "_service_for", AsyncMock(return_value=service)), \
         patch.object(cutover, "legacy_history", AsyncMock(return_value=[])), \
         patch.object(chat, "_record_to_checkpoint", AsyncMock()) as bridge:
        with _client() as client:
            payload = {"thread_id": "t_demo:mounted_replay", "user_message": "你好"}
            normal = client.post("/agent/chat", json=payload, headers=_headers())
            calls_after_first = len(model.recorded)
            replay = client.post("/agent/chat", json=payload, headers=_headers())
            streamed = client.post("/agent/chat/stream", json=payload, headers=_headers())
    assert normal.status_code == replay.status_code == streamed.status_code == 200
    assert normal.json()["content"] == replay.json()["content"]
    assert normal.json()["answer_id"] == replay.json()["answer_id"] == "canary-api-test-1"
    assert '"answer_id": "canary-api-test-1"' in streamed.text
    assert usage.await_count == 1
    assert len(model.recorded) == calls_after_first
    bridge.assert_not_awaited()


def test_default_entry_is_the_four_agent_graph(monkeypatch):
    from app.services import four_agent

    assert four_agent.enabled() is True
    assert four_agent.enabled(pending_ticket=True) is False
    monkeypatch.setenv("FOUR_AGENT_GRAPH", "off")
    assert four_agent.enabled() is True


def test_switch_rule_is_shared_with_the_graph_side():
    from agent.rollout import graph_enabled
    from app.services import four_agent

    for pending in (False, True):
        assert four_agent.enabled(pending_ticket=pending) is graph_enabled(
            pending_ticket=pending)


def test_missing_agent_package_fails_explicitly(monkeypatch):
    import builtins
    import importlib
    import app.services.four_agent as four_agent

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "agent.cutover":
            raise ImportError("simulated: agent package absent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(ImportError, match="agent package absent"):
        importlib.reload(four_agent)
    monkeypatch.setattr(builtins, "__import__", real_import)
    importlib.reload(four_agent)


def test_request_without_any_header_takes_the_new_graph(monkeypatch):
    """The point of this branch: no header, no env var, and still the four-agent graph."""
    monkeypatch.delenv("FOUR_AGENT_GRAPH", raising=False)
    state = {"answer": "默认入口就是四 Agent 图。", "model_messages": []}
    runner = AsyncMock(return_value=state)
    headers = {key: value for key, value in _headers().items() if key != "X-Agent-Graph"}
    with patch.object(chat, "_pool_conn", _connection), \
         patch.object(chat, "check_concurrent_sessions", AsyncMock(return_value=None)), \
         patch.object(chat, "touch_session_retry", AsyncMock()), \
         patch.object(chat, "_record_usage_after_chat", AsyncMock()), \
         patch.object(chat.four_agent, "run_turn", runner), \
         patch.object(chat, "_record_to_checkpoint", AsyncMock()) as bridge:
        with _client() as client:
            response = client.post("/agent/chat",
                                   json={"thread_id": "t_demo:default_entry",
                                         "user_message": "你好"},
                                   headers=headers)
    assert response.status_code == 200
    assert response.json()["content"].startswith("默认入口就是四 Agent 图。")
    assert runner.await_count == 1, "no header means the new graph must be the one that runs"
    bridge.assert_not_awaited()


def test_v1_header_cannot_reach_historical_chat_path():
    runner = AsyncMock(return_value={"answer": "新版回复", "model_messages": []})
    headers = {**_headers(), "X-Agent-Graph": "v1"}
    with patch.object(chat, "_pool_conn", _connection), \
         patch.object(chat, "check_concurrent_sessions", AsyncMock(return_value=None)), \
         patch.object(chat, "touch_session_retry", AsyncMock()), \
         patch.object(chat.four_agent, "run_turn", runner):
        with _client() as client:
            payload = {"thread_id": "t_demo:v1_header", "user_message": "你好"}
            response = client.post("/agent/chat", json=payload, headers=headers)
            streamed = client.post("/agent/chat/stream", json=payload, headers=headers)
    assert response.status_code == streamed.status_code == 200
    assert response.json()["content"].startswith("新版回复")
    assert '"type": "end"' in streamed.text
    assert "新版回复" in streamed.text
    assert runner.await_count == 2
