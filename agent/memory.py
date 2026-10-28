# 中文文案必然包含全角标点
"""Watermark memory: a rolling summary plus the verbatim messages after it.

Why not the official ``SummarizationMiddleware``: its documented behaviour is to
return ``RemoveMessage(id=REMOVE_ALL_MESSAGES)`` and rebuild the channel from a
summary plus the retained tail. That is correct for an agent's private scratch
history, but here the same ``messages`` channel is the canonical conversation the
UI replays after a restart — deleting from it would silently drop turns, which is
exactly the failure this phase has to remove.

So the transcript stays append-only and the *model context* is derived instead:

    summary (covers messages[:watermark]) + messages[watermark:][-window:]

Three properties are enforced, and each one has a test:
  * the watermark only moves forward, and only past messages that were summarised;
  * a failed or empty summary leaves the watermark where it was (no silent loss);
  * order ids and quoted numbers/constraints survive compaction, otherwise the
    previous summary is kept and the missing anchors are appended back.

A summary is never a source of fact: order state and shop policy must be re-read
from Business and the knowledge base on every turn, so the banner says so.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from .state import SupportState

SUMMARY_BANNER = "【较早对话摘要｜仅用于理解指代，不作为订单状态或店铺政策的依据】"
SUMMARY_LIMIT = 900
DEFAULT_TRIGGER = 12
"""Summarise once this many verbatim messages sit above the watermark."""
DEFAULT_KEEP = 6
"""Always keep this many messages verbatim on top of the summary."""

ORDER_ANCHOR = re.compile(
    r"(?<![A-Za-z0-9-])(?:ORD-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*|"
    r"(?:TB|JD|PDD|DY|TMALL|1688|SF)-?\d{6,20})(?![A-Za-z0-9-])", re.I)
NUMBER_ANCHOR = re.compile(r"\d+(?:\.\d+)?\s*(?:元|天|个?工作日|小时|个月|%)")
CONSTRAINT_ANCHOR = re.compile(
    r"(?:不要|不能|不想|不接受|别|必须|只能|希望|要求|优先|等到|不超过|至少)[^\n。；;]{0,40}")
# Fields the customer names explicitly and will ask back verbatim: an invoice title or a
# recipient is not an id and not a number, so without this it is the first thing a
# paraphrasing summariser throws away.
ENTITY_ANCHOR = re.compile(
    r"(?:发票抬头|抬头|公司名称|公司|收件人|联系人|发票|地址|邮箱|手机)[^\n。；;]{0,30}")

SUMMARY_PROMPT = """把已有摘要与新增对话合并成一段不超过 {limit} 字的中文记忆。
必须保留：仍然有效的订单号、客户明确提出的约束（不要/必须/希望/不超过等）、
金额与时限数值、以及尚未解决的问题。
冲突时以较新的消息为准，并在句中写明「客户后改为」。
不要写成结论式的政策或订单状态——这些内容下一轮仍会重新查询核实。
只输出摘要正文。

已有摘要：
{previous}

新增对话：
{newly}
"""


def message_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else str(content)


def visible_messages(state: SupportState) -> list[BaseMessage]:
    """Customer-facing turns only; internal tool traffic is not part of the channel."""
    return [m for m in state.get("messages", []) if isinstance(m, (HumanMessage, AIMessage))]


def anchors(*texts: str) -> set[str]:
    found: set[str] = set()
    for text in texts:
        found.update(ORDER_ANCHOR.findall(text))
        found.update(NUMBER_ANCHOR.findall(text))
        found.update(CONSTRAINT_ANCHOR.findall(text))
        found.update(ENTITY_ANCHOR.findall(text))
    return {item.strip() for item in found if item.strip()}


def pending_count(state: SupportState) -> int:
    return max(0, len(visible_messages(state)) - int(state.get("summary_watermark", 0) or 0))


def model_context(state: SupportState, *, window: int = 12) -> list[BaseMessage]:
    """Summary above the watermark, then the verbatim messages after it.

    ``window`` is the number of verbatim messages kept, counted from the newest end
    and clipped at the watermark — never a multiplier of it.
    """
    messages = visible_messages(state)
    watermark = min(max(0, int(state.get("summary_watermark", 0) or 0)), len(messages))
    tail = messages[watermark:][-window:] if window else messages[watermark:]
    prompt: list[BaseMessage] = []
    summary = (state.get("summary") or "").strip()
    if summary:
        prompt.append(HumanMessage(content=f"{SUMMARY_BANNER}\n{summary[:SUMMARY_LIMIT]}"))
    prompt.extend(tail)
    return prompt


def render(messages: list[BaseMessage]) -> str:
    return "\n".join(f"{getattr(m, 'type', 'message')}: {message_text(m)[:280]}" for m in messages)


def prioritise_anchors(lost: set[str]) -> list[str]:
    """Order ids and money/duration values first; free-text clauses last.

    A flat count cap is wrong here: an absorbed stretch of dialogue easily yields more
    than a dozen anchors, and sorting them lexicographically happens to drop exactly the
    ones that carry an order id or an amount — the ones the customer will ask back.
    """
    orders = sorted(item for item in lost if ORDER_ANCHOR.fullmatch(item))
    entities = sorted(item for item in lost if item not in orders and ENTITY_ANCHOR.match(item))
    values = sorted(item for item in lost if item not in orders and item not in entities
                    and NUMBER_ANCHOR.search(item))
    clauses = sorted(lost - set(orders) - set(entities) - set(values))
    return [*orders, *entities, *values, *clauses]


def repair_line(lost: set[str], budget: int) -> str:
    """Pack as many highest-priority anchors as fit the remaining summary budget."""
    parts = []
    used = len("【待保留要点】")
    for item in prioritise_anchors(lost):
        extra = len(item) + (1 if parts else 0)
        if used + extra > budget:
            break
        parts.append(item)
        used += extra
    return "【待保留要点】" + "；".join(parts) if parts else ""


async def compact(state: SupportState, *, model: Any, trigger: int = DEFAULT_TRIGGER,
                  keep: int = DEFAULT_KEEP, repair: bool = True) -> dict[str, Any]:
    """Fold the oldest messages above the watermark into the summary. Never deletes.

    ``repair=False`` exists only so the measurement harness can isolate how much of
    the retention comes from the anchor check rather than from the model's own
    phrasing; production wiring leaves it on.
    """
    messages = visible_messages(state)
    watermark = min(max(0, int(state.get("summary_watermark", 0) or 0)), len(messages))
    absorbable = len(messages) - watermark - keep
    if absorbable <= 0 or len(messages) - watermark <= trigger:
        return {}
    chunk = messages[watermark:watermark + absorbable]
    previous = (state.get("summary") or "").strip()
    try:
        response = await model.ainvoke([HumanMessage(content=SUMMARY_PROMPT.format(
            limit=SUMMARY_LIMIT, previous=previous or "（无）", newly=render(chunk)[:5000]))])
    except Exception:
        return {"_compacted": False, "_reason": "summary_model_failed"}
    generated = message_text(response).strip()
    if not generated:
        return {"_compacted": False, "_reason": "empty_summary"}

    lost = anchors(previous, render(chunk)) - anchors(generated)
    if lost and repair:
        # Budget the paraphrase first: truncating *after* appending the anchors would
        # cut off exactly the line that repairs them.
        line = repair_line(lost, SUMMARY_LIMIT - len(generated) - 1)
        if line:
            generated = f"{generated}\n{line}"
    generated = generated[:SUMMARY_LIMIT]
    return {"summary": generated, "summary_watermark": watermark + absorbable,
            "_compacted": True, "_absorbed": absorbable, "_restored": len(lost)}


def context_state(state: SupportState) -> dict[str, int]:
    """Small numeric ledger for the report: how much is summary vs verbatim."""
    messages = visible_messages(state)
    watermark = int(state.get("summary_watermark", 0) or 0)
    return {"total": len(messages), "summarized": watermark,
            "verbatim": max(0, len(messages) - watermark)}
