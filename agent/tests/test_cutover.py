# 中文文案必然包含全角标点
"""The cutover shim must satisfy exactly what chat.py reads, or the one-line swap crashes.

``python_agent/app/api/chat.py`` consumes only ``state["answer"]`` and
``state.get("model_messages", [])`` from ``four_agent.run_turn(...)``, in both the JSON and
the SSE branch. Those two keys are therefore the contract this module has to keep — tested
here against the scripted model so the swap can be verified without a live gateway.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agent import cutover
from agent.fakes import ScriptedSupportModel, offline_deps
from agent.service import SupportService

TENANT = "t_demo"
THREAD = "t_demo:cutover-1"


def _app_with(saver: InMemorySaver) -> SimpleNamespace:
    """Just enough app state for the shim: it only touches ``app.state.checkpointer``."""
    return SimpleNamespace(state=SimpleNamespace(checkpointer=saver))


def test_rollout_flag_matrix(monkeypatch) -> None:
    """Pending approvals and an unset flag must both keep the old graph in charge."""
    monkeypatch.setenv(cutover.ENV_FLAG, "off")
    assert cutover.enabled() is False
    monkeypatch.setenv(cutover.ENV_FLAG, "on")
    assert cutover.enabled() is True
    assert cutover.enabled(pending_ticket=True) is False, "unfinished HITL thread must not switch"
    monkeypatch.setenv(cutover.ENV_FLAG, "1")
    assert cutover.enabled() is True
    monkeypatch.delenv(cutover.ENV_FLAG, raising=False)
    assert cutover.enabled() is False, "default off: deploying the code must not change traffic"


@pytest.mark.asyncio
async def test_run_turn_returns_the_two_keys_chat_py_reads(monkeypatch) -> None:
    """The whole point of the alias: chat.py's ``state["answer"]`` must exist."""
    deps, _model, _events, _captured = offline_deps(model=ScriptedSupportModel())

    async def deps_for_tenant(_tenant_id):
        return deps

    stub = SupportService(deps_for_tenant, checkpointer=InMemorySaver())

    async def fake_service(_app, _tenant_id):
        return stub

    monkeypatch.setattr(cutover, "_service_for", fake_service)
    state = await cutover.run_turn(_app_with(None), TENANT, THREAD, "退货期限是多久？")

    assert isinstance(state, dict)
    assert isinstance(state["answer"], str) and state["answer"].strip(), state
    # chat.py does state.get("model_messages", []) for usage accounting: the key must be
    # present and list-like, otherwise the usage recorder silently bills nothing.
    assert isinstance(state.get("model_messages"), list)
    assert state["outcome"] in {"answered", "answered_unchecked", "review_blocked", "no_result"}


@pytest.mark.asyncio
async def test_unverified_identity_is_rejected_before_any_model_call(monkeypatch) -> None:
    """Identity is checked first: an unverifiable thread must not reach the graph."""
    called = []

    async def exploding_service(_app, _tenant_id):
        called.append("service")
        raise AssertionError("must not be reached")

    monkeypatch.setattr(cutover, "_service_for", exploding_service)
    with pytest.raises(PermissionError):
        await cutover.run_turn(_app_with(None), TENANT, "other-tenant:thread", "查一下我的订单")
    assert called == [], "graph must not be built for an unverifiable identity"

