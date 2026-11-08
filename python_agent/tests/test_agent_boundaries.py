# ruff: noqa: RUF001
"""阶段4 的边界：主图只依赖能力层，不反向依赖旧编排器。

这几条是**源码级**断言，因为它们要守的正是"有没有人顺手 import 了上一层"——
一旦发生，运行时不会报错，只会在某天换实现时突然牵出一整条旧链路。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / "app" / "agent" / "support"
CAPABILITIES = ROOT / "app" / "agent" / "capabilities"
LEGACY = ROOT / "app" / "agent" / "legacy_approval"

FORBIDDEN_IN_GRAPH = ("app.services.supervisor", "app.services.specialists",
                      "app.chat.", "app.api.", "app.agent.legacy_approval")
FORBIDDEN_IN_CAPABILITIES = ("app.services.supervisor", "app.services.specialists",
                             "app.chat.", "app.api.", "app.agent.support.graph")


def _sources(root: Path) -> list[Path]:
    return [path for path in root.rglob("*.py") if "__pycache__" not in path.parts]


def test_main_graph_never_imports_the_legacy_orchestrator() -> None:
    offenders = []
    for path in _sources(SUPPORT):
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN_IN_GRAPH:
            if f"import {needle}" in text or f"from {needle}" in text:
                offenders.append(f"{path.relative_to(ROOT)} → {needle}")
    assert offenders == [], f"主图不得依赖旧编排器 / 传输层：{offenders}"


def test_capabilities_do_not_reach_up_into_orchestration() -> None:
    offenders = []
    for path in _sources(CAPABILITIES):
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN_IN_CAPABILITIES:
            if f"import {needle}" in text or f"from {needle}" in text:
                offenders.append(f"{path.relative_to(ROOT)} → {needle}")
    assert offenders == [], f"能力层不得反向依赖编排层：{offenders}"


def test_legacy_recovery_is_the_only_place_that_touches_the_old_graph() -> None:
    """旧图入口只能从 legacy_approval 出现，四 Agent 主图里不得直接调度它。"""
    assert (LEGACY / "approval.py").exists()
    graph_source = (SUPPORT / "graph.py").read_text(encoding="utf-8")
    assert "get_tenant_agent" not in graph_source
    assert "Command(resume" not in graph_source


def test_order_capability_has_no_write_surface() -> None:
    """退款/支付/建单在 Agent 侧根本不存在，而不是"被 prompt 禁止"。"""
    text = (CAPABILITIES / "orders.py").read_text(encoding="utf-8")
    for forbidden in ("refund", "pay(", "create_order", "cancel_order", "transfer_to_human"):
        assert forbidden not in text, f"订单能力出现了写操作：{forbidden}"


def test_one_request_and_one_response_model() -> None:
    """接口层只应存在一份请求模型与一份响应模型。"""
    import app.schemas as schemas
    from app.api import chat_v2

    assert hasattr(schemas, "ChatRequest") and hasattr(schemas, "AgentResp")
    assert chat_v2.ChatRequest is schemas.ChatRequest, \
        "调试入口必须复用 app.schemas.ChatRequest，不能自带一份"

    fields = set(schemas.AgentResp.model_fields)
    for required in ("success", "is_interrupt", "content", "answer_id", "turn_id",
                     "outcome", "error_code", "agents", "evidence_ids"):
        assert required in fields, f"响应模型缺字段：{required}"
