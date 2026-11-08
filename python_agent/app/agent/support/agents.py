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
from contextlib import nullcontext
from typing import Any
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    before_model,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from . import guards
from .deps import EVIDENCE_ROLES, SupportDeps
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
   用户同时请求转人工时，业务层会在你答复**之后**自动建单并单独提示工单号；因此只写业务结论，
   不要写"请转人工 / 输入「转人工」/ 联系客服热线 / 官方 App 或官网在线客服入口"这类引导
   （客户紧接着就会看到建单提示，再教他转一次是自相矛盾的），
   也不要声称整个客服系统没有转人工能力，不要自行承诺工单已经创建。
6. 只能引用本轮工具核验过的订单号；客户提供的其他账号订单不要复述为事实。
7. 收到【校验反馈】时，先补调用缺失的专职 Agent，再回答；不要重复同样的回答。
8. 汇总时先把客户这句话拆成要回答的具体项（例如「订单状态」「未拆封能否退款」
   「拆封后能否退款」），逐项作答：每项第一句是结论，第二句才是依据。
   结论只允许三种说法：可以申请 / 不可以 / 目前无法确认。
   一段政策条款不是结论，客户问「能不能退」就必须先看到这三者之一。
   客户问到的项一项都不能漏；某一路核验没通过就照实说那一路没核到并给出下一步，
   不要把「无法确认/查不到」写成整句答复的主体，也不要顺带声明与问题无关的订单状态。
9. 客户没问的字段（金额、下单时间、商品分类）不要展示。不要罗列完整的例外清单、
   政策目录，或「以下情况不支持：①②③」这类条款合集；依据最多两条、每条一句。
   收到「没有给出结论」「漏项」「结论没有前置」这类校验反馈时，按项重写答复本身，
   不要在原文后面追加一段解释。
10. 身份、账号或密码问题不得从对话历史猜测身份，不得复述、生成或声称可查看密码；只说明当前客服无法核验身份或获取密码，并指引客户使用商城官方账号渠道。
11. 多笔订单或多个条目要逐行列出，每行以「- 」开头、保留「单号｜商品｜状态｜日期」的字段顺序，
    不要把列表压成一段话；单笔订单的字段可以用「**标签**：值」的形式分行给出。
用客户能懂的中文简明回答，不输出工具 JSON、检索片段或内部编号。"""

ORDER_PROMPT = """你是只读订单 Agent。
客户给了订单号就调用 lookup_my_order；没给订单号但想看本人订单就调用 list_my_orders。
只复述工具返回的核验结果；工具说未登录、不属于当前账号、查不到或系统不可用时，如实说明并给出下一步。
客户没问到的字段（金额、下单时间、商品分类）不必罗列——Supervisor 还要把这些材料压成
「能不能退」的结论，多给的字段只会被拼进答复里。
工具给的是列表时保持列表：一笔订单一行，以「- 」开头，字段顺序与工具一致，不要合并成一句话。
说明未核验时写"这笔订单本次未能核验，请提供订单号或重新登录后我再核"——把下一步一起给出来，
不要写成"无法确认""查不到"这种只表示拒绝、不告诉客户该怎么办的句子。
你不能判断订单归属，也不能推断政策；不调用知识库检索。"""

@before_model(can_jump_to=["end"])
def safety_gate(state: dict, runtime: Any) -> dict | None:
    """Local content guard so ``agent/`` does not depend on the legacy middleware."""
    messages = state.get("messages") or []
    last = messages[-1] if messages else None
    content = str(getattr(last, "content", ""))
    if guards.is_blocked_question(content):
        return {"jump_to": "end", "messages": [HumanMessage(content=guards.BLOCKED_REPLY)]}
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


def _dispatch_key(specialist: str, turn_id: str = "") -> str:
    """一次派发一个 key。加随机后缀而不是用 ``attempts``：重试与并行都不共享账。"""
    return f"{turn_id}|{specialist}|{uuid4().hex[:8]}"


def _dispatch_scope(deps: SupportDeps, specialist: str, key: str) -> Any:
    """只有 FAQ 需要开证据窗口；其余角色连窗口都不开，免得把别人的检索算进来。"""
    return deps.evidence.scope(key) if specialist in EVIDENCE_ROLES else nullcontext()


def _dispatch_evidence(deps: SupportDeps, specialist: str, key: str) -> list[Any]:
    return deps.evidence.collected(key) if specialist in EVIDENCE_ROLES else []


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
        key = _dispatch_key(specialist)
        try:
            with _dispatch_scope(deps, specialist, key):
                result = await _run(agent, question, timeout=timeout_seconds,
                                    recursion_limit=recursion_limit)
            finding = _finding(specialist, result)
        except Exception:
            deps.trace("delegate", specialist, status="failed")
            # 没有 finding 会来消费这次派发的窗口，收掉它，别让它留到下一轮。
            deps.evidence.discard(key)
            return _bounded({"specialist": specialist, "answer": "", "failed": True})
        raw = _dispatch_evidence(deps, specialist, key)
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


COMPOSE_PROMPT = """你是电商客服的答复撰写 Agent。你拿到的是本轮各专职 Agent 的核验结果，
只负责把它们写成客户能看懂的一段答复，不再自行核实任何事实。
规则：
1. 先把客户这句话拆成要回答的具体项（例如「订单状态」「未拆封能否退款」「拆封后能否退款」），
   逐项作答：每项第一句是结论，第二句才是依据。结论只允许三种说法：可以申请 / 不可以 /
   目前无法确认。一段政策条款不是结论，客户问「能不能退」就必须先看到这三者之一。
   客户问到的项一项都不能漏；某一路核验没通过就照实说那一路没核到并给出下一步，
   不要把「无法确认」「查不到」写成整句答复的主体，也不要顺带声明与问题无关的订单状态。
2. 客户没问的字段（金额、下单时间、商品分类）不要展示。不要罗列完整的例外清单、政策目录，
   或「以下情况不支持：①②③」这类条款合集；依据最多两条、每条一句。
3. 只能引用本轮核验过的订单号；客户提供的其他账号订单不要复述为事实。
4. 不得执行或声称已执行退款、支付、取消订单、发货、建工单、转人工，也不要承诺代为执行。
   用户同时请求转人工时，业务层会在你答复**之后**自动建单并单独提示工单号；所以只写业务结论，
   不要写"请转人工 / 输入「转人工」/ 联系客服热线 / 官网在线客服入口"这类引导
   （客户紧接着就会看到建单提示，再教他转一次是自相矛盾的），
   也不要声称整个客服系统没有转人工能力，不要自行承诺工单已经创建。
5. 身份、账号或密码问题不得从对话历史猜测身份，不得复述、生成或声称可查看密码；
   只说明当前客服无法核验身份或获取密码，并指引客户使用商城官方账号渠道。
6. 多笔订单或多个条目要逐行列出，每行以「- 」开头、保留「单号｜商品｜状态｜日期」的字段顺序，
   不要把列表压成一段话；单笔订单的字段可以用「**标签**：值」的形式分行给出。
7. 收到【校验反馈】时，按项重写答复本身，不要在原文后面追加一段解释。
不输出工具 JSON、检索片段或内部编号。用客户能懂的中文简明回答。"""

COMPOSE_MATERIAL = 900
"""每位专家最多贡献多少字材料。旧实现把三份 3000 字的工具 JSON 全塞回模型，这一步就是
token 的主要来源；撰写只需要答复与依据要点，不需要工具调用的原始结构。"""


async def build_specialists(deps: SupportDeps, *, max_tool_calls: int = 3
                            ) -> tuple[dict[str, Any], Any]:
    """一个角色一个可调用体，供 node 版图直接当节点体使用。

    与 ``build_supervisor`` 用的是同一批 agent 构造：区别只在于谁来调用它们——
    以前是模型通过 tool_calls 触发，现在由图的条件边触发。模型一并返回，避免撰写节点
    再取一次（租户模型每次构造都可能是一次新的客户端）。
    """
    model = await deps.model()
    return ({"faq": await deps.faq_agent(),
             "order": build_order_agent(deps, model, max_tool_calls=max_tool_calls),
             "general": await deps.general_agent()}, model)


async def run_specialist(deps: SupportDeps, role: str, question: str, agent: Any, *,
                         turn_id: str, timeout: float, recursion_limit: int
                         ) -> tuple[Delegation, dict[str, Any]]:
    """Run one specialist and normalise its outcome into a ``Delegation``.

    Failure is data, not an exception: a dead specialist must still produce a finding so
    the Reviewer's soft rule ("有专职 Agent 执行失败") can push for a补查, and so the
    ledger shows which path failed instead of an unexplained empty answer.

    证据按「本次派发」的 key 归属：扇出的三个节点是并行 asyncio 任务，共享的 trace 事件流
    会把兄弟分支的检索结果混进来，而 Reviewer 的「有证据才允许说政策数值」正是靠这条边界
    成立——所以这里开窗口、按 key 取，非 FAQ 角色一律空手。
    """
    key = _dispatch_key(role, turn_id)
    try:
        with _dispatch_scope(deps, role, key):
            result = await _run(agent, question, timeout=timeout, recursion_limit=recursion_limit)
        finding = _finding(role, result)
        usage = collect_usage(result.get("messages") or [])
    except Exception as exc:  # noqa: BLE001  一个专家失败不能让整轮崩掉
        deps.trace("delegate", role, status="failed",
                   error=f"{type(exc).__name__}: {str(exc)[:120]}")
        deps.evidence.discard(key)  # 失败派发没有 finding 来消费窗口，直接收掉
        return ({"turn_id": turn_id, "specialist": role, "answer": "", "tool_calls": [],
                 "evidence": [], "verified_results": [], "used_search": False, "failed": True}, {})
    raw = _dispatch_evidence(deps, role, key)
    # Evidence is a TypedDict, so the check is against dict, never the alias itself.
    finding["evidence"] = [item if isinstance(item, dict) else
                           {"id": str(item), "title": "", "text": ""} for item in raw]
    finding["turn_id"] = turn_id
    deps.trace("delegate", role, used_search=finding["used_search"],
               evidence_ids=[item["id"] for item in finding["evidence"]][:8],
               codes=[item["code"] for item in finding["verified_results"]])
    return finding, usage


def compose_material(findings: list[Delegation], *, feedback: str = "", prior: str = "") -> str:
    """材料按「结论 → 依据」组织，而不是把工具 JSON 原样回灌。"""
    blocks: list[str] = []
    for item in findings:
        lines = [f"【{item['specialist']}】", f"答复：{item['answer'][:COMPOSE_MATERIAL]}"]
        for evidence in item["evidence"][:3]:
            lines.append(f"依据（{evidence['id']}）：{evidence['text'][:240]}")
        for result in item["verified_results"][:3]:
            lines.append(f"接口核验（{result['code']}）：{result['text'][:240]}")
        if item["failed"]:
            lines.append("状态：这一路执行失败，没有可用结果")
        blocks.append("\n".join(lines))
    if feedback:
        blocks.append(f"【校验反馈】{feedback}")
    if prior:
        blocks.append(f"【上一版答复】{prior[:COMPOSE_MATERIAL]}")
    return "\n\n".join(blocks)


async def compose_reply(model: Any, *, question: str, findings: list[Delegation],
                        feedback: str = "", prior: str = "", timeout: float = 30.0
                        ) -> tuple[str, dict[str, Any]]:
    """One model call that turns findings into the customer-facing answer."""
    payload = (f"客户问题：{question}\n\n各专职 Agent 的核验结果：\n"
               f"{compose_material(findings, feedback=feedback, prior=prior)}")
    messages = [SystemMessage(content=COMPOSE_PROMPT), HumanMessage(content=payload)]
    result = await asyncio.wait_for(
        model.ainvoke(messages, config={"recursion_limit": 4}), timeout)
    return _text(result).strip(), collect_usage([result])


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
