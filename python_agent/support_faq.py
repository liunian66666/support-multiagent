"""Conservative, tenant-neutral process FAQ imported from support-agent's approved process playbook.

These answers explain what information support needs; they never assert a store policy,
order state, refund eligibility or completed action. Tenant-owned facts stay in Qdrant.
"""
import re

_FAQ = {
    "怎么申请退货": "请提供订单号和退货原因。客服核验订单归属、商品情况及商家政策后，才能判断如何处理。",
    "如何申请退货": "请提供订单号和退货原因。客服核验订单归属、商品情况及商家政策后，才能判断如何处理。",
    "退货申请怎么操作": "请提供订单号和退货原因。客服核验订单归属、商品情况及商家政策后，才能判断如何处理。",
    "你可以做什么": "我可以说明通用客服流程、检索当前租户的知识库，并帮您提交转人工工单。真实订单状态查询、退款和支付操作目前尚未接通，不能代为执行。",
    "你能做什么": "我可以说明通用客服流程、检索当前租户的知识库，并帮您提交转人工工单。真实订单状态查询、退款和支付操作目前尚未接通，不能代为执行。",
    "怎么申请退款": "请说明订单号与退款原因，客服核验订单归属、支付情况及商家政策后才能判断处理方式。当前系统不自动发起退款。",
    "怎么查询订单状态": "请提供订单号。核对订单归属后，客服才能查询状态；当前未接通真实订单系统时请联系人工客服。",
}
_SOURCE = "support-agent:process-playbook-v1 / generic-process"

def normalize(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text or "").lower()

def lookup(text: str, tenant_id: str) -> str | None:
    if not tenant_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tenant_id):
        return None
    answer = _FAQ.get(normalize(text))
    return f"{answer}\n（来源：{_SOURCE}；本店政策请以租户知识库为准）" if answer else None

def is_handoff(text: str) -> bool:
    value = normalize(text)
    return value in {"转人工", "转人工客服", "我要转人工", "我要转人工客服", "请转人工", "请转人工客服", "请转接人工客服", "我想找人工客服"}

def order_id(text: str) -> str | None:
    if not re.search(r"订单|order", text, re.IGNORECASE):
        return None
    found = re.search(r"\bORD-[A-Za-z0-9-]{3,64}\b", text, re.IGNORECASE)
    return found.group(0) if found else None
