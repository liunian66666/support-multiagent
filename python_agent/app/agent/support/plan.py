# 中文文案必然包含全角标点
"""路由计划：模型只决定「派给谁、每一路问什么」。

这一步仍是模型判断——模糊意图（“我买的那个东西有点问题想退”“上次说的活动还算数吗”）
规则判不出来，硬判只会漏角色，然后靠重试兜底，反而更慢。变的是**决策与执行的分离**：

* 决策：这里一次性拿到结构化计划（角色 + 子问题），进程内可检查、可比较、可存进 state；
* 执行：``graph.py`` 用条件边把每个角色当独立节点跑，于是每一路可单独超时、重试、
  观测，也能在重试时只补跑缺失的那一路。

工具（``consult_faq`` / ``consult_order`` / ``consult_general``）在这里只作为 schema 使用，
**永远不会被执行**：模型的 tool_calls 只被读成计划，真正的执行在节点里。这条边界是本次
改造的核心——同一个意图识别能力，换成显式控制流。

模型也可能选择“不派发、直接回答”（寒暄、澄清）。这条路径必须保留，否则每次问候都要
白白跑一遍 general 专家；但直接回答的文本要当作草稿进入同一套 Reviewer。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.tools import BaseTool, tool

from .state import RouteItem

ROLE_TOOLS = {"consult_faq": "faq", "consult_order": "order", "consult_general": "general"}
"""工具名 → 角色。图里的节点名由角色拼出来，所以这张表是唯一的映射来源。"""

ROLES = ("faq", "order", "general")
FALLBACK_ROLE = "general"
"""计划为空时的兜底：宁可让接待 Agent 说一句澄清，也不要空扇出把图卡住。"""

MAX_QUESTION = 300
"""子问题上限。真实提问很少超过这个长度，超出部分通常是模型把整段对话复述进来了。"""

ROUTER_STEP_BUDGET = 4
"""路由是单次结构化调用；给 4 步只为容纳一次重试，不沿用 Supervisor 的 32。"""

ROUTER_PROMPT = """你是电商客服的 Supervisor Agent，但这一轮只负责路由：决定派给哪些专职 Agent、
每一路问什么，不写面向客户的答复。
路由目标以工具形式给出（这些工具只用于标记，不返回任何内容）：
consult_faq（店铺政策、费用、时限、售后条件与知识库事实）、
consult_order（本人订单只读核验）、consult_general（问候、能力说明与澄清）。
规则：
1. 涉及店铺政策、费用、时限、售后条件的结论，必须标记 consult_faq。
2. 只有客户在问**他自己那一单**的状态、金额、物流时才标记 consult_order；
   政策条文里出现的「订单页」「物流页」「下单时显示」这类通用说法不算本人订单问题。
   没有订单号但确实要看本人订单时也要标记，由它列单或说明还缺什么。
3. 复合问题要在同一轮里把需要的角色一次标记齐，缺一个就是这一轮没做完。
4. 较早对话摘要只用于理解「这个」「刚才那单」这类指代；订单状态和政策数值必须当轮重新查。
5. 每个角色的 question 写成能独立执行的一句话，只问它该查的那一件事；
   不要把整个复合问题原样复述给每一路。
6. 收到【校验反馈】时，只标记需要补的那几路，已经核验过的不要重复。
7. 寒暄、能力介绍、需要澄清的问题标记 consult_general。
8. 只有在无需任何核验就能回答时才直接写答复，此时不要标记任何工具。
不输出工具 JSON、检索片段或内部编号。"""


def _text(message: Any) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else str(content)


def _sub_question(args: Any, fallback: str) -> str:
    """模型有时传 ``{"question": ...}``，有时直接传字符串；两者都要能用。"""
    if isinstance(args, str):
        return args.strip()[:MAX_QUESTION] or fallback[:MAX_QUESTION]
    if isinstance(args, dict):
        value = args.get("question") or args.get("query") or ""
        if isinstance(value, str) and value.strip():
            return value.strip()[:MAX_QUESTION]
    return fallback[:MAX_QUESTION]


@dataclass(frozen=True)
class RouteDecision:
    """One router call's outcome: what to dispatch, or what to answer directly."""

    items: list[RouteItem] = field(default_factory=list)
    direct: str = ""
    """非空表示模型选择不派发、直接答复；它和专家结果走同一套 Review。"""
    dropped: int = 0
    """被忽略的重复/未知工具调用数，只用于 trace：它是模型跑偏的早期信号。"""

    @property
    def roles(self) -> list[str]:
        return [item["role"] for item in self.items]


def parse_decision(message: Any, question: str) -> RouteDecision:
    """把一次模型输出读成计划。

    三个降级分支，顺序有意义：有工具调用就按调用派发；没有调用但有文本，就是直接答复
    （旧的 Supervisor 也允许这种形状，端到端用例依赖它）；两者都没有，才兜底一个角色。
    同一角色只取第一次调用——``delegations`` 的键是 ``turn|specialist``，第二次只会覆盖
    第一次的结果，多跑一遍没有意义。
    """
    calls = list(getattr(message, "tool_calls", None) or [])
    items: list[RouteItem] = []
    seen: set[str] = set()
    dropped = 0
    for call in calls:
        role = ROLE_TOOLS.get(str(call.get("name", "")))
        if role is None or role in seen:
            dropped += 1
            continue
        seen.add(role)
        items.append({"role": role, "question": _sub_question(call.get("args"), question)})
    if items:
        return RouteDecision(items=items, dropped=dropped)
    direct = _text(message).strip()
    if direct:
        return RouteDecision(direct=direct[:1500], dropped=dropped)
    return RouteDecision(items=[{"role": FALLBACK_ROLE, "question": question[:MAX_QUESTION]}],
                         dropped=dropped)


@tool("consult_faq")
async def consult_faq(question: str) -> str:
    """Route to the FAQ specialist for shop policy, fees, time limits or after-sales terms."""
    return ""


@tool("consult_order")
async def consult_order(question: str) -> str:
    """Route to the read-only order specialist to verify this customer's own order."""
    return ""


@tool("consult_general")
async def consult_general(question: str) -> str:
    """Route to the reception specialist for greetings, capability answers or clarification."""
    return ""


ROUTER_TOOLS: tuple[BaseTool, ...] = (consult_faq, consult_order, consult_general)
"""绑定给模型只为拿 schema；这三个函数不会被调用（返回空串）。"""


async def plan_turn(model: Any, prompt: list[BaseMessage], *, question: str,
                    timeout: float = 20.0) -> tuple[RouteDecision, list[Any], str]:
    """Call the router once. Returns ``(decision, used_messages, error)``, never raises.

    ``ROUTER_PROMPT`` 必须由这里注入：``bind_tools`` 只改变工具 schema，不会附带任何
    「只路由、不回答」的指令。少了这一条 system message，真实模型会把路由调用当成一次
    普通问答直接写答复——``parse_decision`` 于是读到「直答」，整张图退化成没有专家的
    单模型，而脚本化 fake 因为只按脚本回放，发现不了这个退化。

    模型故障不能把客户问题变成异常：返回空计划 + 错误原因，由 ``compose`` 产出空草稿，
    再由 ``finalize`` 走安全话术——与旧实现里 ``no_result`` 的降级路径一致。
    """
    try:
        bound = model.bind_tools(ROUTER_TOOLS)
        result = await asyncio.wait_for(
            bound.ainvoke([SystemMessage(content=ROUTER_PROMPT), *prompt],
                          config={"recursion_limit": ROUTER_STEP_BUDGET}), timeout)
    except Exception as exc:  # noqa: BLE001  路由失败必须降级，不能冒泡给客户
        return RouteDecision(), [], f"{type(exc).__name__}: {str(exc)[:180]}"
    return parse_decision(result, question), [result], ""
