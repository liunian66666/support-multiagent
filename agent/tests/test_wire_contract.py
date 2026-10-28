# ruff: noqa: RUF001  # 断言文案含中文
"""The wire contract with ``python_business`` — the layer the browser actually talks to.

The customer front-end never calls the Agent service directly: ``web/sdk/cs-chat.js``
speaks to ``python_business/business/chat/realtime.py``, which is the real consumer of
``POST /agent/chat`` and ``POST /agent/chat/stream``. Its parser is the spec, and it is
stricter than it looks:

* JSON — it gates on ``data["success"]`` and returns 503 to the customer when falsy,
  then persists ``data["content"]`` together with ``data["answer_id"]`` as the turn
  attribution anchor. A missing ``answer_id`` does **not** fail loudly: the reply
  renders normally while every rating silently loses its anchor.
* SSE — it acts only on ``chunk`` / ``end`` / ``error`` / ``interrupt``. Any other
  ``type`` is skipped, and if no ``end`` frame arrives the turn is never marked
  completed.

So the useful acceptance check is not "our JSON and SSE agree with each other" (already
covered) but "our frames survive their parser". That is what this file pins.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from httpx import ASGITransport, AsyncClient

from agent.entrypoint import create_standalone_app
from agent.fakes import ScriptedSupportModel, offline_deps
from agent.service import SupportService

pytestmark = pytest.mark.asyncio

TENANT = "t_demo"
THREAD = "t_demo:wire-1"
ANSWER = "签收后 7 天内可申请退货，运费自理。"

# Copied from realtime.py: the only event types its SSE loop branches on.
BUSINESS_SSE_TYPES = {"chunk", "end", "error", "interrupt"}
# Copied from realtime.py: the keys its JSON branch reads.
BUSINESS_JSON_KEYS = ("success", "is_interrupt", "content", "answer_id")


def _client():
    async def deps_for_tenant(_tenant_id):
        deps, _model, _events, _captured = offline_deps(
            model=ScriptedSupportModel(supervisor_script=[
                AIMessage(content=ANSWER, usage_metadata={"input_tokens": 8, "output_tokens": 4,
                                                          "total_tokens": 12})]))
        return deps

    app = create_standalone_app(
        identity=lambda _request, thread_id: (TENANT, thread_id),
        service=SupportService(deps_for_tenant, checkpointer=InMemorySaver()))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://svc")


async def test_json_payload_satisfies_the_business_parser(monkeypatch) -> None:
    monkeypatch.setenv("SUPPORT_AGENT_V2", "on")
    async with _client() as client:
        response = await client.post("/agent/chat/v2", json={
            "thread_id": THREAD, "user_message": "退货期限多久？", "has_pending_ticket": False})
    assert response.status_code == 200
    data = response.json()

    missing = [key for key in BUSINESS_JSON_KEYS if key not in data]
    assert missing == [], f"业务层会读到的键缺失：{missing}"

    # business: `succeeded = bool(data.get("success"))` then 503 when falsy.
    assert data["success"] is True, "success=False 会让业务层对客户报「智能客服暂时不可用」"
    assert isinstance(data["content"], str) and data["content"].strip()
    assert data["is_interrupt"] is False
    # The attribution anchor must be a non-empty string: business stores
    # str(data.get("answer_id") or "") and an empty value silently un-ties the rating.
    assert isinstance(data["answer_id"], str) and data["answer_id"], \
        "answer_id 为空会让评分归因静默退化成『按会话归因』"


async def test_sse_frames_are_all_understood_by_the_business_loop(monkeypatch) -> None:
    monkeypatch.setenv("SUPPORT_AGENT_V2", "on")
    thread = f"{TENANT}:wire-sse"
    async with _client() as client:
        response = await client.post("/agent/chat/v2/stream", json={
            "thread_id": thread, "user_message": "退货期限多久？", "has_pending_ticket": False})
    assert response.status_code == 200

    events = []
    for block in response.text.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    assert events, "业务层没收到任何 data: 帧"

    # An unrecognised type is silently skipped by their parser; if it were the only
    # frame, the turn would never be marked completed.
    unknown = [event["type"] for event in events if event.get("type") not in BUSINESS_SSE_TYPES]
    assert unknown == [], f"业务层解析器会跳过的帧类型：{unknown}"

    end = [event for event in events if event.get("type") == "end"]
    assert len(end) == 1, "业务层在 end 帧落库并 break；缺失或重复都会丢轮次"
    assert end[0]["content"].strip()
    assert isinstance(end[0].get("answer_id"), str) and end[0]["answer_id"], \
        "SSE 的 end 帧同样必须携带 answer_id，否则落库消息没有归因锚点"


async def test_error_and_refusal_never_render_as_a_blank_bubble(monkeypatch) -> None:
    """business 的 `answer` 落库前不判空；空串会让客户看到空气泡。"""
    monkeypatch.setenv("SUPPORT_AGENT_V2", "on")
    broken_model = ScriptedSupportModel(fail_supervisor=True)

    async def deps_for_tenant(_tenant_id):
        deps, _model, _events, _captured = offline_deps(model=broken_model)
        return deps

    app = create_standalone_app(
        identity=lambda _request, thread_id: (TENANT, thread_id),
        service=SupportService(deps_for_tenant, checkpointer=InMemorySaver()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://svc") as client:
        data = (await client.post("/agent/chat/v2", json={
            "thread_id": f"{TENANT}:wire-err", "user_message": "退货期限多久？"})).json()

    # success stays True because safe text was produced: the point of the no_result
    # branch is that the customer still sees a sentence, not a 503.
    assert data["success"] is True and data["content"].strip(), data
    assert data["outcome"] == "no_result", data["outcome"]
