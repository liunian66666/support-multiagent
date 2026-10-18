"""Conservative, tenant-neutral process FAQ imported from support-agent's approved process playbook.

Matching is intent/keyword based (not exact-string) so common paraphrases hit
the deterministic answer instead of falling through to LLM/RAG. Answers stay
process-level: they never assert a store policy, order state, refund
eligibility or a completed action. Tenant-owned facts stay in Qdrant, so
policy questions (postage thresholds, timelines, fees) intentionally miss
here and are answered by the tenant knowledge base.
"""
import re

_SOURCE = "support-agent:process-playbook-v1 / generic-process"

_ANSWERS = {
    "return_apply": "请提供订单号和退货原因。客服核验订单归属、商品情况及商家政策后，才能判断如何处理。",
    "refund_apply": "请说明订单号与退款原因，客服核验订单归属、支付情况及商家政策后才能判断处理方式。当前系统不自动发起退款。",
    "order_status": "请提供订单号。核对订单归属后，客服才能查询状态；当前未接通真实订单系统时请联系人工客服。",
    "capabilities": "我可以说明通用客服流程、检索当前租户的知识库，并帮您提交转人工工单。真实订单状态查询、退款和支付操作目前尚未接通，不能代为执行。",
}

# 意图规则：(答案键, 关键词组A, 关键词组B)。两组各需命中其一才算该意图；
# 组A 约束话题（退货/退款/订单/能力），组B 约束问法（申请/查询/怎么……），
# 避免把政策类问题（多久到账、运费谁出）误判成流程问题。
_INTENTS = (
    ("return_apply", ("退货", "退换"), ("怎么", "如何", "申请", "操作", "流程", "办理", "方法", "步骤", "要退", "想退")),
    ("refund_apply", ("退款",), ("怎么", "如何", "申请", "操作", "流程", "办理", "方法", "步骤", "要退", "想退")),
    ("order_status", ("订单", "快递", "物流", "包裹"), ("状态", "到哪", "查", "进度")),
    ("capabilities", ("能做什么", "可以做什么", "都会什么", "你会什么", "有什么功能", "能干嘛", "会干嘛", "什么功能", "提供什么服务", "你能帮我"), None),
)

_HANDOFF_EXACT = {"转人工", "转人工客服", "转接人工", "转接人工客服"}


def normalize(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text or "").lower()


def _intent(text: str) -> str | None:
    value = normalize(text)
    for key, topic, ask in _INTENTS:
        if not any(t in value for t in topic):
            continue
        if ask is None or any(a in value for a in ask):
            return key
    return None


def lookup(text: str, tenant_id: str) -> str | None:
    if not tenant_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tenant_id):
        return None
    key = _intent(text)
    if not key:
        return None
    return f"{_ANSWERS[key]}\n（来源：{_SOURCE}；本店政策请以租户知识库为准）"


def is_handoff(text: str) -> bool:
    value = normalize(text)
    if not value:
        return False
    if any(p in value for p in _HANDOFF_EXACT):
        return True
    if "人工" in value and any(w in value for w in ("转", "找", "接", "请", "需要", "给我", "来个")):
        return True
    return any(p in value for p in ("人工客服", "找客服", "投诉", "转接"))


def handoff_reason(text: str) -> str:
    if "投诉" in normalize(text):
        return "用户投诉，转人工处理"
    return "用户主动请求转人工"


def order_id(text: str) -> str | None:
    if not re.search(r"订单|order|快递|物流|包裹", text, re.IGNORECASE):
        return None
    found = re.search(r"\bORD-[A-Za-z0-9-]{3,64}\b", text, re.IGNORECASE)
    return found.group(0) if found else None
