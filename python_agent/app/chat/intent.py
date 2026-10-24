"""确定性意图路由（纯规则，零 DB/网络，从 multi_agent.py 拆出）。

原项目 SQLite/Milvus 的 specialist 无法直接搬过来：订单身份归属在 FastAPI 业务层，
租户知识库在 Qdrant。本模块只保留 router 的判定边界，安全适配器复用现有实现。
"""
import re
from dataclasses import dataclass

from app.chat.faq import handoff_reason, is_handoff, is_policy_question, lookup, order_id


@dataclass(frozen=True)
class Route:
    specialist: str
    order_id: str | None = None
    answer: str | None = None
    reason: str | None = None


_ACTION = re.compile(r"(?:给我|帮我|我要|现在|立即|直接).{0,12}(?:退款|扣款|付款|支付|退货)|^请(?:立即|直接)?(?:退款|退货|支付|付款)|(?:退款|扣款|付款|支付).{0,10}(?:这单|该订单|订单号)")
_FAQ = re.compile(r"政策|规则|运费|包邮|发票|课程|价格|配送|退换|退款|退货|能退|可退|货到付款|分期|支付方式|付款方式|物流|保修|售后|知识库|有什么|是什么|多久|如何|怎么")
_DIRECT_ACTION = re.compile(r"(?:给我|帮我|我要|现在|立即|直接|请)(?:立刻|立即|直接)?(?:申请|发起|办理)?(?:退款|扣款|付款|支付|退货)")
_DIRECT_ORDER_ACTION = re.compile(
    r"(?:请|帮我|给我|我要)(?:给|把|为)?\s*"
    r"(?:订单(?:号)?\s*[A-Za-z0-9-]{4,40}|这单|该单)\s*"
    r"(?:申请|发起|办理)?(?:退款|退货|支付|付款)"
)
_ORDER_LIST = re.compile(r"我的订单|我(?:有|都有哪些|有哪些).*订单|订单号(?:忘了|忘记|不知道|不记得|找不到)|(?:忘了|忘记|不知道|不记得|找不到)订单号")


def route_message(message: str, tenant_id: str, *, pending_ticket: bool = False) -> Route:
    """Route only by the trusted tenant and current message; never infer identity."""
    if is_handoff(message) and not pending_ticket:
        return Route("handoff", reason=handoff_reason(message))
    order_action = bool(_DIRECT_ORDER_ACTION.search(message))
    explicit_action = bool(_DIRECT_ACTION.search(message) or order_action)
    if (_ACTION.search(message) or order_action) and not (is_policy_question(message) and not explicit_action):
        if pending_ticket:
            return Route("faq_fast", answer="您已提交人工客服，正在等待接入。客服接入后会继续核验退款或支付请求。")
        # No refund/payment mutation specialist is exposed until business APIs,
        # ownership checks and independent approval are all wired up.
        return Route("handoff", reason="退款或支付操作需要人工核验")
    oid = order_id(message)
    if oid:
        return Route("order", order_id=oid)
    if _ORDER_LIST.search(message):
        return Route("order_list")
    answer = lookup(message, tenant_id)
    if answer:
        return Route("faq_fast", answer=answer)
    if _FAQ.search(message):
        return Route("faq")
    return Route("general")
