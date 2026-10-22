"""Pure, bounded planning for read-only compound support requests."""

import re
from dataclasses import dataclass

from app.chat.faq import order_id

_ORDER = re.compile(r"订单|这单|那单|快递|包裹|物流|发货|到货|收货")
_ORDER_ASK = re.compile(r"查|核对|核验|状态|到哪|进度|什么时候|发货|到货|收货|配送|能退|可退|可以退|符合")
_KNOWLEDGE = re.compile(r"政策|规则|条件|运费|包邮|退货|退换|退款|能退|可退|退吗|售后|保修|发票|课程|价格|流程|怎么|如何")
_TOPICS = (
    ("退换售后", re.compile(r"退货|退换|退款|能退|可退|退吗|售后|保修")),
    ("配送运费", re.compile(r"运费|包邮|配送|发货|到货|物流")),
    ("发票", re.compile(r"发票")),
    ("课程商品", re.compile(r"课程|商品|价格")),
)
_ID = re.compile(r"(?<![A-Za-z0-9-])(?:ORD-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*|(?:TB|JD|PDD|DY|TMALL|1688)-?\d{6,20})(?![A-Za-z0-9-])", re.I)
_MULTI_PART = re.compile(r"和|以及|并且|分别|同时|还有|另外|再结合|同时问")


@dataclass(frozen=True)
class ReadTask:
    kind: str  # order or kb
    query: str


@dataclass(frozen=True)
class TaskPlan:
    tasks: tuple[ReadTask, ...]
    order_id: str | None
    needs_order_id: bool


def plan_compound(message: str, *, resumed_order_id: str | None = None,
                  resumed_question: str | None = None) -> TaskPlan | None:
    """Plan at most one owned-order read and two independent KB reads.

    Explicit handoff or write actions are rejected by the caller's existing
    router before this function is used. This planner never grants identity.
    """
    question = resumed_question or message
    oid = resumed_order_id or order_id(question)
    topics = [name for name, pattern in _TOPICS if pattern.search(question)]
    order_asked = bool(_ORDER.search(question) and (_ORDER_ASK.search(question) or (oid and topics)))
    # A broad policy question is still a KB task even without a named topic.
    if not topics and _KNOWLEDGE.search(question):
        topics = ["店铺政策"]
    # A delivery-status request alone is an order read, not a policy lookup.
    if topics == ["配送运费"] and order_asked and not re.search(r"运费|包邮|配送政策|配送规则", question):
        topics = []
    if not order_asked and (len(topics) < 2 or not _MULTI_PART.search(question)):
        return None
    if order_asked and not topics:
        return None
    tasks: list[ReadTask] = []
    if order_asked and oid:
        tasks.append(ReadTask("order", oid))
    # Never send a personal order identifier into shared KB search.
    public_question = _ID.sub("", question)
    public_question = re.sub(r"订单号[\s:=：是为]*[A-Za-z0-9-]{4,40}", "", public_question)
    for topic in topics[:2]:
        tasks.append(ReadTask("kb", f"{topic}：{public_question[:160]}"))
    return TaskPlan(tuple(tasks), oid if order_asked else None, order_asked and not oid)
