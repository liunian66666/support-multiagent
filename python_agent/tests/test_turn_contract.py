# ruff: noqa: RUF001  # 断言文案含中文
"""生产端点的轮次契约（阶段1／阶段2／阶段3）。

这里测的是**前端真正调用的那两个端点**（``/agent/chat``、``/agent/chat/stream``），
不是独立的调试入口。四条不变量：

1. ``turn_id`` 从请求体进图，``answer_id`` 恒等于它，JSON 与 SSE 同值同文；
2. 同一 ID 携带不同问题 ⇒ 409，而不是悄悄多出一轮；
3. 请求体 ``has_pending_ticket`` 不再决定走哪张图（服务端检查点才算）；
4. 生产入口与调试入口共用**同一份**服务注册表。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.legacy_approval.approval import ApprovalState
from app.agent.support.service import NAMESPACE, TurnConflictError
from app.api import chat
from app.api.chat_v2 import build_router as build_v2_router
from app.infra.config import INTERNAL_API_TOKEN
from app.services import support_runtime

HEADERS = {"X-Internal-Token": INTERNAL_API_TOKEN, "X-Tenant-Id": "t_demo",
           "X-Trace-Id": "canary-turn"}


@asynccontextmanager
async def _connection(_app):
    yield object()


def _client(*, with_v2: bool = False) -> TestClient:
    app = FastAPI()
    app.state.db_pool = None
    app.include_router(chat.router)
    if with_v2:
        app.include_router(build_v2_router())
    return TestClient(app)


def _state(turn_id: str = "turn-1") -> dict:
    return {"answer": "签收后 7 天内可申请退货。", "turn_id": turn_id,
            "outcome": "answered", "model_messages": [], "_replayed": False,
            "delegations": [{"specialist": "faq"}], "evidence_ids": ["chunk-1"],
            "review_issues": [], "attempts": 1, "usage": {}}


def _patched(runner):
    return [
        patch.object(chat, "_pool_conn", _connection),
        patch.object(chat, "check_concurrent_sessions", AsyncMock(return_value=None)),
        patch.object(chat, "touch_session_retry", AsyncMock()),
        patch.object(chat, "_record_usage_after_chat", AsyncMock()),
        patch.object(chat.four_agent, "run_turn", runner),
    ]


def _enter(patches):
    for item in patches:
        item.start()


def _exit(patches):
    for item in reversed(patches):
        item.stop()


def _sse_events(text: str) -> list[dict]:
    return [json.loads(line[6:]) for block in text.strip().split("\n\n")
            for line in block.splitlines() if line.startswith("data: ")]


def test_json_and_sse_carry_the_same_turn_and_answer():
    runner = AsyncMock(return_value=_state("turn-1"))
    patches = _patched(runner)
    _enter(patches)
    try:
        with _client() as client:
            payload = {"thread_id": "t_demo:contract-1", "user_message": "退货多久？",
                       "turn_id": "turn-1"}
            normal = client.post("/agent/chat", json=payload, headers=HEADERS)
            streamed = client.post("/agent/chat/stream", json=payload, headers=HEADERS)
    finally:
        _exit(patches)

    body = normal.json()
    assert normal.status_code == streamed.status_code == 200
    assert body["turn_id"] == body["answer_id"] == "turn-1"
    assert body["outcome"] == "answered"
    assert body["agents"] == ["faq"]
    assert body["evidence_ids"] == ["chunk-1"]

    end = [event for event in _sse_events(streamed.text) if event["type"] == "end"][0]
    assert end["turn_id"] == end["answer_id"] == body["turn_id"]
    assert end["content"] == body["content"]
    assert end["outcome"] == body["outcome"]
    assert end["agents"] == body["agents"]
    assert end["evidence_ids"] == body["evidence_ids"], "JSON 与 SSE 的证据关联必须同源"


def test_explicit_turn_id_reaches_the_graph():
    runner = AsyncMock(return_value=_state("turn-abc"))
    patches = _patched(runner)
    _enter(patches)
    try:
        with _client() as client:
            client.post("/agent/chat",
                        json={"thread_id": "t_demo:contract-2", "user_message": "你好",
                              "turn_id": "turn-abc"},
                        headers=HEADERS)
    finally:
        _exit(patches)
    assert runner.await_args.args[4] == "turn-abc", "第五个位置参数是 turn_id"


def test_missing_turn_id_falls_back_to_the_trace_header():
    """旧调用方没升级也要能跑，但两个通道必须落到**同一个** ID 上。"""
    runner = AsyncMock(return_value=_state("canary-turn"))
    patches = _patched(runner)
    _enter(patches)
    try:
        with _client() as client:
            normal = client.post("/agent/chat",
                                 json={"thread_id": "t_demo:contract-3",
                                       "user_message": "你好"},
                                 headers=HEADERS)
    finally:
        _exit(patches)
    assert runner.await_args.args[4] == "canary-turn"
    assert normal.json()["answer_id"] == "canary-turn"


def test_reusing_a_turn_id_for_another_question_is_a_conflict():
    runner = AsyncMock(side_effect=TurnConflictError("used for another question"))
    patches = _patched(runner)
    _enter(patches)
    try:
        with _client() as client:
            response = client.post("/agent/chat",
                                   json={"thread_id": "t_demo:contract-4",
                                         "user_message": "另一个问题",
                                         "turn_id": "turn-1"},
                                   headers=HEADERS)
    finally:
        _exit(patches)
    assert response.status_code == 409
    assert "turn_id" in response.json()["detail"]


def test_forged_pending_flag_cannot_divert_the_production_entry():
    """业务层说"我这儿有工单在排队"不等于"这条线程停在旧审批上"。"""
    runner = AsyncMock(return_value=_state("turn-5"))
    patches = _patched(runner) + [
        patch.object(chat.four_agent, "approval_state",
                     AsyncMock(return_value=ApprovalState.CLEAR))]
    _enter(patches)
    try:
        with _client() as client:
            response = client.post("/agent/chat",
                                   json={"thread_id": "t_demo:contract-5",
                                         "user_message": "你好", "turn_id": "turn-5",
                                         "has_pending_ticket": True},
                                   headers=HEADERS)
    finally:
        _exit(patches)
    assert response.status_code == 200
    assert runner.await_count == 1, "伪造的排队标志不得把普通聊天推回旧图"


def _blocked(patches, path: str, body: dict):
    _enter(patches)
    try:
        with _client() as client:
            return (client.post(path, json=body, headers=HEADERS),
                    client.post("/agent/chat/stream", json=body, headers=HEADERS))
    finally:
        _exit(patches)


def test_pending_approval_is_a_conflict_not_an_old_agent_answer():
    """服务端确认停在旧审批上 ⇒ 409，且本轮**不执行任何 Agent**。

    这是"业务状态先行"的 Agent 侧一半：新问题不会被旧 FAQ/Intake 回答（那正是收敛前
    ``_legacy_json_impl`` 干的事），也不会进四 Agent（否则恢复后客户会看到两套事实）。
    """
    runner = AsyncMock()
    patches = _patched(runner) + [
        patch.object(chat.four_agent, "approval_state",
                     AsyncMock(return_value=ApprovalState.PENDING)),
        patch.object(chat.four_agent, "pending_approval_payload",
                     AsyncMock(return_value={"action": "transfer_to_human"}))]
    body = {"thread_id": "t_demo:contract-6", "user_message": "包邮规则", "turn_id": "turn-6"}
    normal, streamed = _blocked(patches, "/agent/chat", body)

    assert normal.status_code == streamed.status_code == 409
    detail = normal.json()["detail"]
    assert detail["error_code"] == "approval_pending"
    assert detail["retryable"] is False
    assert detail["resume_endpoint"] == "/agent/resume"
    assert detail["turn_id"] == "turn-6"
    assert detail["checkpoint_keys"] == ["support_v2:t_demo:contract-6", "t_demo:contract-6"]
    assert detail["pending_approval"] == {"action": "transfer_to_human"}
    assert "旧 Agent" in detail["detail"]
    assert runner.await_count == 0, "409 的那一轮不得碰主图"


def test_unknown_approval_state_is_retryable_and_never_runs_the_graph():
    """检查点读不到 ⇒ 503 retryable，既不放行也不冒充"没有审批"。"""
    runner = AsyncMock()
    patches = _patched(runner) + [
        patch.object(chat.four_agent, "approval_state",
                     AsyncMock(return_value=ApprovalState.UNKNOWN))]
    body = {"thread_id": "t_demo:contract-6b", "user_message": "你好", "turn_id": "turn-6b"}
    normal, streamed = _blocked(patches, "/agent/chat", body)

    assert normal.status_code == streamed.status_code == 503
    detail = normal.json()["detail"]
    assert detail["error_code"] == "approval_state_unknown"
    assert detail["retryable"] is True
    assert runner.await_count == 0


def test_one_service_registry_shared_by_both_entries():
    app = FastAPI()
    app.state.db_pool = None
    runtime = support_runtime.runtime_for(app)
    assert support_runtime.runtime_for(app) is runtime
    assert app.state.support_runtime is runtime

    sentinel = object()
    runtime.bind("t_demo", sentinel)
    assert runtime.services["t_demo"] is sentinel
    assert getattr(app.state, "support_v2_service", None) is None, \
        "调试入口不得再维护第二份服务缓存"


def test_turn_id_with_illegal_characters_is_rejected():
    with _client() as client:
        response = client.post("/agent/chat",
                               json={"thread_id": "t_demo:contract-7", "user_message": "你好",
                                     "turn_id": "has space"},
                               headers=HEADERS)
    assert response.status_code == 422


def test_there_is_no_switch_left_to_route_on():
    """"走哪张图"不再是配置问题：唯一判定是服务端审批三态。"""
    from app.services import four_agent

    assert not hasattr(four_agent, "enabled")
    assert {member.value for member in four_agent.ApprovalState} == {
        "pending", "clear", "unknown"}


def test_namespaced_checkpoint_key_is_still_support_v2():
    """阶段5 的键名约束：清理与调试接口都按这个前缀删/查。"""
    assert NAMESPACE == "support_v2"
    assert f"{NAMESPACE}:t_demo:x" == "support_v2:t_demo:x"


def test_billing_messages_uses_tenant_pricing_by_default(monkeypatch):
    """常规部署：不写 model_override，让记账用该租户自己的模型与单价。"""
    monkeypatch.delenv("FOUR_AGENT_EVAL", raising=False)
    monkeypatch.setenv("FOUR_AGENT_MODEL_NAME", "glm-5.3")
    from app.agent.support.service import TurnResult

    result = TurnResult(turn_id="t", answer="a", outcome="answered",
                        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    billed = support_runtime.billing_messages(result)
    assert billed[0].usage_metadata["total_tokens"] == 15
    assert not billed[0].response_metadata, "没有显式评测档时不得覆盖模型归因"


def test_billing_messages_stamps_the_eval_model(monkeypatch):
    monkeypatch.setenv("FOUR_AGENT_EVAL", "on")
    monkeypatch.setenv("FOUR_AGENT_MODEL_NAME", "glm-5.3")
    from app.agent.support.service import TurnResult

    result = TurnResult(turn_id="t", answer="a", outcome="answered",
                        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    meta = support_runtime.billing_messages(result)[0].response_metadata
    assert meta["model_name"] == "glm-5.3" and meta["model_override"] is True


def test_eval_model_override_is_inert_without_the_flag(monkeypatch):
    monkeypatch.delenv("FOUR_AGENT_EVAL", raising=False)
    monkeypatch.setenv("FOUR_AGENT_MODEL_NAME", "glm-5.3")
    assert support_runtime.eval_model_override() == ""
    assert support_runtime.model_plan() == ("tenant", "")
    monkeypatch.setenv("FOUR_AGENT_EVAL", "on")
    assert support_runtime.model_plan() == ("platform", "glm-5.3")


def test_no_usage_means_no_billing_row():
    from app.agent.support.service import TurnResult

    result = TurnResult(turn_id="t", answer="a", outcome="no_result", usage={})
    assert support_runtime.billing_messages(result) == []


@pytest.mark.asyncio
async def test_each_tenant_gets_its_own_model_config(monkeypatch):
    """两个租户的模型配置不同时各用各的：装配按租户调用，且默认来源是 tenant。"""
    seen: list[tuple[str, str, str]] = []

    async def fake_deps(app, tenant_id, *, reuse_faq_agent=True,
                        model_source="tenant", model_name=""):
        seen.append((tenant_id, model_source, model_name))
        return object()

    monkeypatch.setattr(support_runtime, "deps_for_app", fake_deps)
    monkeypatch.delenv("FOUR_AGENT_EVAL", raising=False)

    app = FastAPI()
    app.state.checkpointer = None
    runtime = support_runtime.SupportRuntime(app)

    first = await runtime.service_for("t_a")
    second = await runtime.service_for("t_b")
    same = await runtime.service_for("t_a")

    assert first is same and first is not second, "一个租户一个服务，且不重复装配"
    # 依赖是懒装配的（建图才触达模型），这里直接验证两个租户各自的装配口径。
    await first._deps_for_tenant("t_a")
    await second._deps_for_tenant("t_b")
    assert seen == [("t_a", "tenant", ""), ("t_b", "tenant", "")], \
        "常规部署必须按租户已验证配置取模型，且不覆盖模型 id"
