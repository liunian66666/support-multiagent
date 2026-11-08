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

from app.agent.support import cutover
from app.agent.support.fakes import ScriptedSupportModel, offline_deps
from app.agent.support.service import SupportService

TENANT = "t_demo"
THREAD = "t_demo:cutover-1"


def _app_with(saver: InMemorySaver) -> SimpleNamespace:
    """Just enough app state for the shim: it only touches ``app.state.checkpointer``."""
    return SimpleNamespace(state=SimpleNamespace(checkpointer=saver))


def test_the_graph_switch_is_gone_not_just_defaulted(monkeypatch) -> None:
    """"走哪张图"已不是配置问题：``enabled()`` 只剩一座会炸的墓碑。

    收敛前这里返回 ``not pending_ticket``，聊天入口据此把未完成审批的会话送进旧图。
    现在那条分支被删了，任何残留调用都必须在第一次执行时失败，而不是得到一个
    "看起来还能用"的布尔值。
    """
    for pending in (False, True):
        with pytest.raises(RuntimeError, match="已退役"):
            cutover.enabled(pending_ticket=pending)
    monkeypatch.setenv("FOUR_AGENT_GRAPH", "off")
    with pytest.raises(RuntimeError):
        cutover.enabled()


def test_rollout_module_is_deleted_so_no_policy_can_drift() -> None:
    """策略只能有一份：判定函数搬家到 ``legacy_approval.approval_state``（三态）。

    读 app 侧源码而不是 import：这个套件按"可独立构建"设计，不假设 ``app`` 已安装。
    """
    from pathlib import Path

    root = Path(cutover.__file__).resolve().parents[4]
    service = (root / "python_agent/app/services/four_agent.py").read_text(encoding="utf-8")
    assert "approval_state" in service
    assert "graph_enabled" not in service and "os.getenv" not in service
    support_dir = Path(cutover.__file__).resolve().parent
    assert not (support_dir / "rollout.py").exists(), "开关模块必须被删掉，而不是留着不用"


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
    assert state["model_messages"]
    assert state["model_messages"][0].usage_metadata["total_tokens"] == state["usage"]["total_tokens"]
    assert state["outcome"] in {"answered", "answered_unchecked", "review_blocked", "no_result"}


@pytest.mark.asyncio
async def test_cutover_replay_does_not_bill_or_call_model_twice(monkeypatch) -> None:
    deps, model, _events, _captured = offline_deps(model=ScriptedSupportModel())

    async def deps_for_tenant(_tenant_id):
        return deps

    stub = SupportService(deps_for_tenant, checkpointer=InMemorySaver())

    async def fake_service(_app, _tenant_id):
        return stub

    monkeypatch.setattr(cutover, "_service_for", fake_service)
    first = await cutover.run_turn(_app_with(None), TENANT, THREAD, "你好", "stable-turn")
    before = len(model.recorded)
    replay = await cutover.run_turn(_app_with(None), TENANT, THREAD, "你好", "stable-turn")

    assert first["answer"] == replay["answer"]
    assert replay["_replayed"] is True
    assert replay["model_messages"] == first["model_messages"]
    assert len(model.recorded) == before


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

