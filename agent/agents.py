# 中文文案必然包含全角标点
"""The four agents, expressed with the official LangChain 1.x primitives.

Supervisor + three specialists, wired the documented *subagents-as-tools* way: the
supervisor is a ``create_agent`` loop whose tools invoke the FAQ, Order and General
agents and return one bounded finding each. The parent graph in ``graph.py`` owns
the conversation and the checkpoint; the delegates stay stateless and are fed a
snapshot, so there is no second memory competing with the watermark state.

Bounded by official middleware rather than by hand-written counters:
``ModelCallLimitMiddleware`` caps supervisor model calls and
``ToolCallLimitMiddleware`` caps the delegate tools per run. The graph-level
``should_continue`` still bounds the review-retry loop.

Order reads never take a customer identity from the model: the tools accept only an
order id, and Business decides ownership.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    before_model,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from .deps import SupportDeps
from .state import Delegation, OrderFact

DELEGATE_TOOLS = ("consult_faq", "consult_order", "consult_general")
SPECIALIST_OF_TOOL = {"consult_faq": "faq", "consult_order": "order", "consult_general": "general"}

SUPERVISOR_PROMPT = """你是电商客服的 Supervisor Agent，负责理解问题、动态委派并汇总结果。
可用的专职 Agent 以工具形式提供：consult_faq（店铺政策与知识库事实）、
consult_order（本人订单只读核验）、consult_general（问候、能力说明与澄清）。
规则：
1. 涉及店铺政策、费用、时限、售后条件的结论，必须先 consult_faq，并只采用它给出的内容。
2. 只有客户在问**他自己那一单**的状态、金额、物流时才 consult_order；政策条文里出现的「订单页」「物流页」「下单时显示」这类通用说法不算本人订单问题，不要为它去查单。没有订单号但确实要看本人订单时，让它列单或请客户补充。
3. 复合问题应在同一轮里把需要的专职 Agent 都调用齐，缺一个就是未完成。
4. 较早对话摘要只用于理解「这个」「刚才那单」这类指代；订单状态和政策数值必须当轮重新查询。
5. 不得执行或声称已执行退款、支付、取消订单、发货、建工单、转人工。
6. 只能引用本轮工具核验过的订单号；客户提供的其他账号订单不要复述为事实。
7. 收到【校验反馈】时，先补调用缺失的专职 Agent，再回答；不要重复同样的回答。
8. 汇总时只对客户问的事情作答：某一路核验没通过就照实说那一路没核到并给出下一步，不要把「无法确认/查不到」写成整句答复的主体，也不要顺带声明与问题无关的订单状态。
用客户能懂的中文简明回答，不输出工具 JSON、检索片段或内部编号。"""

ORDER_PROMPT = """你是只读订单 Agent。
客户给了订单号就调用 lookup_my_order；没给订单号但想看本人订单就调用 list_my_orders。
只复述工具返回的核验结果；工具说未登录、不属于当前账号、查不到或系统不可用时，如实说明并给出下一步。
说明未核验时写"这笔订单本次未能核验，请提供订单号或重新登录后我再核"——把下一步一起给出来，
不要写成"无法确认""查不到"这种只表示拒绝、不告诉客户该怎么办的句子。
你不能判断订单归属，也不能推断政策；不调用知识库检索。"""

_BLOCKED_TERMS = ("黄X", "X博", "违法")


@before_model(can_jump_to=["end"])
def safety_gate(state: dict, runtime: Any) -> dict | None:
    """Local content guard so ``agent/`` does not depend on the legacy middleware."""
    messages = state.get("messages") or []
    last = messages[-1] if messages else None
    content = str(getattr(last, "content", ""))
    if any(word in content for word in _BLOCKED_TERMS):
        return {"jump_to": "end", "messages": [HumanMessage(content="抱歉，我不能处理这个请求。")]}
    return None


def _text(message: Any) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else str(content)


def _tool_calls_of(messages: list[Any]) -> list[str]:
    return [call.get("name", "") for msg in messages for call in (getattr(msg, "tool_calls", None) or [])]


def _order_facts(messages: list[Any]) -> list[OrderFact]:
    facts: list[OrderFact] = []
    for msg in messages:
        if isinstance(msg, ToolMessage) and getattr(msg, "name", "") in {"lookup_my_order", "list_my_orders"}:
            try:
                payload = json.loads(_text(msg))
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("code"):
                facts.append({"code": str(payload["code"]), "text": str(payload.get("text", ""))[:600]})
    return facts


def _finding(specialist: str, result: dict[str, Any]) -> Delegation:
    messages = result.get("messages") or []
    return {"turn_id": "", "specialist": specialist, "answer": _text(messages[-1]) if messages else "",
            "tool_calls": _tool_calls_of(messages), "evidence": [], "verified_results": _order_facts(messages),
            "used_search": "search_kb" in _tool_calls_of(messages), "failed": not bool(messages)}


def _bounded(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)[:3000]


async def _run(agent: Any, question: str, *, timeout: float, recursion_limit: int) -> dict[str, Any]:
    return await asyncio.wait_for(
        agent.ainvoke({"messages": [HumanMessage(content=question)]},
                      config={"recursion_limit": recursion_limit}), timeout)


def build_order_agent(deps: SupportDeps, model: Any, *, max_tool_calls: int = 3) -> Any:
    """A real model agent over two read-only business tools."""

    @tool("lookup_my_order")
    async def lookup_my_order(order_id: str) -> str:
        """Read one order of the verified customer; ownership is checked by the business API."""
        result = await deps.lookup_order(re.sub(r"\s", "", order_id)[:64])
        deps.trace("tool", "lookup_my_order", code=result.get("code", ""))
        return _bounded(result)

    @tool("list_my_orders")
    async def list_my_orders() -> str:
        """List orders belonging to the verified customer; never accepts a customer id."""
        result = await deps.list_orders()
        deps.trace("tool", "list_my_orders", code=result.get("code", ""))
        return _bounded(result)

    return create_agent(
        model=model, tools=[lookup_my_order, list_my_orders],
        middleware=[safety_gate,
                    ToolCallLimitMiddleware(run_limit=max_tool_calls, exit_behavior="continue")],
        system_prompt=ORDER_PROMPT,
    )


async def build_supervisor(deps: SupportDeps, *, max_delegate_calls: int = 4,
                           max_model_calls: int = 8, timeout_seconds: float = 30.0,
                           recursion_limit: int = 12) -> Any:
    """Supervisor agent whose tools are the three specialist agents."""
    model = await deps.model()
    faq = await deps.faq_agent()
    general = await deps.general_agent()
    order = build_order_agent(deps, model, max_tool_calls=max_delegate_calls)

    async def _delegate(specialist: str, agent: Any, question: str) -> str:
        cursor = deps.evidence_cursor()
        try:
            result = await _run(agent, question, timeout=timeout_seconds, recursion_limit=recursion_limit)
            finding = _finding(specialist, result)
        except Exception:
            deps.trace("delegate", specialist, status="failed")
            return _bounded({"specialist": specialist, "answer": "", "failed": True})
        raw = deps.evidence_ids_since(cursor)
        # Evidence is a TypedDict, so the check is against dict, never the alias itself.
        finding["evidence"] = [item if isinstance(item, dict) else
                               {"id": str(item), "title": "", "text": ""} for item in raw]
        deps.trace("delegate", specialist, used_search=finding["used_search"],
                   evidence_ids=[item["id"] for item in finding["evidence"]][:8],
                   codes=[item["code"] for item in finding["verified_results"]])
        return _bounded({**finding, "turn_id": ""})

    @tool("consult_faq")
    async def consult_faq(question: str) -> str:
        """Ask the FAQ agent for published shop policy, fees, or time limits with evidence."""
        return await _delegate("faq", faq, question)

    @tool("consult_order")
    async def consult_order(question: str) -> str:
        """Ask the read-only order agent to verify this customer's order or list their orders."""
        return await _delegate("order", order, question)

    @tool("consult_general")
    async def consult_general(question: str) -> str:
        """Ask the general agent for greetings, capability answers, or a clarifying question."""
        return await _delegate("general", general, question)

    return create_agent(
        model=model, tools=[consult_faq, consult_order, consult_general],
        middleware=[safety_gate,
                    ToolCallLimitMiddleware(tool_name="consult_faq", run_limit=max_delegate_calls),
                    ToolCallLimitMiddleware(tool_name="consult_order", run_limit=max_delegate_calls),
                    ToolCallLimitMiddleware(tool_name="consult_general", run_limit=max_delegate_calls),
                    ModelCallLimitMiddleware(run_limit=max_model_calls, exit_behavior="end")],
        system_prompt=SUPERVISOR_PROMPT, name="supervisor")


def collect_delegations(messages: list[Any], turn_id: str) -> list[Delegation]:
    """Read this turn's specialist findings back out of the supervisor's ToolMessages."""
    findings: list[Delegation] = []
    for msg in messages:
        if not isinstance(msg, ToolMessage) or getattr(msg, "name", "") not in DELEGATE_TOOLS:
            continue
        try:
            payload = json.loads(_text(msg))
        except (TypeError, ValueError):
            payload = {"specialist": SPECIALIST_OF_TOOL.get(msg.name, "unknown"), "failed": True}
        if isinstance(payload, dict) and payload.get("specialist"):
            findings.append(normalize(payload, turn_id))
    return findings


def normalize(payload: dict[str, Any], turn_id: str) -> Delegation:
    evidence = payload.get("evidence") or []
    return {"turn_id": turn_id, "specialist": str(payload["specialist"])[:16],
            "answer": str(payload.get("answer", ""))[:1500],
            "tool_calls": [str(item)[:40] for item in (payload.get("tool_calls") or [])][:12],
            "evidence": [item if isinstance(item, dict) else {"id": str(item), "title": "", "text": ""}
                         for item in evidence][:8],
            "verified_results": [item for item in (payload.get("verified_results") or [])
                                 if isinstance(item, dict)][:8],
            "used_search": bool(payload.get("used_search")), "failed": bool(payload.get("failed"))}


def last_ai_text(messages: list[Any]) -> str:
    return next((_text(m) for m in reversed(messages) if isinstance(m, AIMessage)), "")


def add_usage(prior: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    """Total for the turn so far: one entry per turn_id, summed across supervisor passes.

    Measured on the ``custom`` arm (report §8e): before this, a turn that needed a retry
    reported only its **last** pass — three passes with nine model calls billed as three.
    Retries are real spend, so the retry loop has to add, not replace. Replay stays
    idempotent because ``open_turn`` clears this turn's entry before the first pass.
    """
    merged = dict(prior or {})
    for key, value in current.items():
        merged[key] = int(merged.get(key) or 0) + int(value or 0)
    return merged


def collect_usage(messages: list[Any]) -> dict[str, Any]:
    """Token accounting for this pass only; keyed by turn_id in the usage channel.

    A replayed turn overwrites its own entry, so retrying a request cannot bill the
    same model calls twice.
    """
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "model_calls": 0,
              "delegate_calls": 0, "tool_calls": 0}
    for msg in messages:
        usage = getattr(msg, "usage_metadata", None) or {}
        if isinstance(usage, dict) and usage:
            totals["input_tokens"] += int(usage.get("input_tokens") or 0)
            totals["output_tokens"] += int(usage.get("output_tokens") or 0)
            totals["total_tokens"] += int(usage.get("total_tokens") or 0)
        if isinstance(msg, AIMessage):
            if getattr(msg, "tool_calls", None):
                totals["tool_calls"] += len(msg.tool_calls)
            totals["model_calls"] += 1
            totals["delegate_calls"] += sum(1 for call in (msg.tool_calls or [])
                                            if call.get("name") in DELEGATE_TOOLS)
    return totals
