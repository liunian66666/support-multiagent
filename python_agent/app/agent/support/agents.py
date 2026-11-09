# 中文文案必然包含全角标点
"""The four agents, expressed with the official LangChain 1.x primitives.

三个专家各是一个可按调用体：谁该被调用由图的条件边（``graph.route``）决定，没有第四个
Agent 负责「决定」。专家保持无状态、吃一份快照，所以没有第二份记忆与水位状态竞争；
父图独占会话与 checkpoint。

Bounded by official middleware rather than by hand-written counters:
``ToolCallLimitMiddleware`` caps each specialist's tool rounds; the graph-level
``should_end`` bounds the review-retry loop.

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
from langchain.agents.middleware import ToolCallLimitMiddleware, before_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from . import guards
from .deps import EVIDENCE_ROLES, SupportDeps
from .plan import ROLE_TOOLS
from .state import Delegation, OrderFact

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


COMPOSE_PROMPT = """你是电商客服的答复撰写 Agent。你拿到的是本轮各专职 Agent 的核验结果，
只负责把它们写成客户能看懂的一段答复，不再自行核实任何事实。
规则：
1. 先看材料里的【问题类型】，按档作答：
   - 资格判定（能不能、是否、多久、多少钱）：把客户这句话拆成要回答的具体项
     （例如「订单状态」「未拆封能否退款」「拆封后能否退款」），逐项作答，每项第一句是
     结论、第二句才是依据。结论只允许四种说法：可以申请 / 不可以 /
     目前还不能申请，满足某个条件后即可 / 目前无法确认。
     一段政策条款不是结论，客户问「能不能退」就必须先看到这四者之一。
     「退换」是两件事，退货与换货都要给落点，不能只讲退货。
     材料同时给了订单状态和政策条件时，要按已知状态把条件推成结论：某个前置条件
     尚未达成（例如订单已发货但还没签收、7 天时限尚未起算）就用第三档，写清
     「现在还不行 + 差哪一个条件 + 满足后可以怎么做」，不得拿「目前无法确认」代替；
     「目前无法确认」只允许用在本轮确实没有可引用依据的时候。
     客户问到的项一项都不能漏，也不要顺带声明与问题无关的订单状态。
   - 操作指引（怎么查、在哪、步骤、入口）：第一句就直接给第一步，按步骤顺序作答，
     不要用「可以申请 / 不可以 / 目前无法确认」开头或收尾，也不要先声明能不能查。
     材料里没有明确路径时，不得自行编写任何页面名称、菜单层级或按钮位置，
     只给下一步（如需人工协助请回复「转人工」）。
2. 客户没问的字段（金额、下单时间、商品分类）不要展示。不要罗列完整的例外清单、政策目录，
   或「以下情况不支持：①②③」这类条款合集；依据最多两条、每条一句。
3. 只能引用本轮核验过的订单号；客户提供的其他账号订单不要复述为事实。
4. 不得执行或声称已执行退款、支付、取消订单、发货、建工单、转人工，也不要承诺代为执行。
   工单只能由业务层实际创建并另行提示；你不知道工单是否会创建，不得预测「稍后会有工单」
   或「人工客服会跟进」。用户未要求转人工时，可告知如需人工协助请回复「转人工」。
   用户同时请求转人工时只写业务结论，
   不要写"请转人工 / 输入「转人工」/ 联系客服热线 / 官网在线客服入口"这类引导
   （客户紧接着就会看到建单提示，再教他转一次是自相矛盾的），
   也不要声称整个客服系统没有转人工能力，不要自行承诺工单已经创建。
   已经给出明确结论或条件式结论时，不要再追加「如需人工协助请回复『转人工』」这类尾巴。
5. 身份、账号或密码问题不得从对话历史猜测身份，不得复述、生成或声称可查看密码；
   只说明当前客服无法核验身份或获取密码，并指引客户使用商城官方账号渠道。
6. 多笔订单或多个条目要逐行列出，每行以「- 」开头、保留「单号｜商品｜状态｜日期」的字段顺序，
   不要把列表压成一段话；单笔订单的字段可以用「**标签**：值」的形式分行给出。
7. 收到【校验反馈】时，按项重写答复本身，不要在原文后面追加一段解释。
8. 不得向客户描述 FAQ、专家节点、检索、核验等内部执行过程；答复里不得出现
   「本轮」「店铺资料」「知识库」「没有查到」「未查到」「核验结果」「检索片段」等词。
   不写「如有其他问题……很乐意继续为您解答」这类结尾客套。
9. 材料中标注【本轮无可用依据】或【待业务确认】的内容一律不得写进答复：
   前者只说明当前缺少可核实的依据，后者不得输出任何占位文字或示例路径。
不输出工具 JSON、检索片段或内部编号。用客户能懂的中文简明回答。"""

COMPOSE_MATERIAL = 900
"""每位专家最多贡献多少字材料。旧实现把三份 3000 字的工具 JSON 全塞回模型，这一步就是
token 的主要来源；撰写只需要答复与依据要点，不需要工具调用的原始结构。"""


async def build_specialists(deps: SupportDeps, *, max_tool_calls: int = 3
                            ) -> tuple[dict[str, Any], Any]:
    """一个角色一个可调用体，供图直接当节点体使用。

    谁调用它们由图的条件边决定，不是模型通过 tool_calls 触发。模型一并返回，避免撰写
    节点再取一次（租户模型每次构造都可能是一次新的客户端）。
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
        role = item["specialist"]
        lines = [f"【{role}】"]
        has_basis = bool(item["evidence"] or item["verified_results"])
        if role in {"faq", "order"} and not has_basis:
            # 专家说「资料里没有查到……」只是它的自述，不是可引用的事实。以前把它
            # 原样写成「答复：」，模型就照抄进客服答复，于是客户看到「本轮店铺资料中
            # 没有查到账单查询入口」这种内部过程描述，接着自行补一段菜单路径。
            # general 的澄清答复没有 evidence 是常态，不能一起挡掉。
            lines.append("【本轮无可用依据】这一路没有取得可引用的材料，"
                         "不能据此断定店铺没有相关规则")
        else:
            lines.append(f"答复：{item['answer'][:COMPOSE_MATERIAL]}")
        for evidence in item["evidence"][:3]:
            lines.append(f"依据（{evidence['id']}）：{evidence['text'][:240]}")
        for result in item["verified_results"][:3]:
            lines.append(f"接口核验（{result['code']}）：{result['text'][:240]}")
        if item["failed"]:
            lines.append("该项本轮未能核实；不能据此断定店铺没有相关规则")
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
    kind = ("操作指引" if guards.is_howto_question(question) else
            "资格判定" if guards.is_policy_question(question) or guards.is_order_question(question)
            else "其他")
    payload = (f"客户问题：{question}\n【问题类型】{kind}\n\n各专职 Agent 的核验结果：\n"
               f"{compose_material(findings, feedback=feedback, prior=prior)}")
    messages = [SystemMessage(content=COMPOSE_PROMPT), HumanMessage(content=payload)]
    result = await asyncio.wait_for(
        model.ainvoke(messages, config={"recursion_limit": 4}), timeout)
    return _text(result).strip(), collect_usage([result])


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
                                            if call.get("name") in ROLE_TOOLS)
    return totals
