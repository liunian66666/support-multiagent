# 中文文案必然包含全角标点
"""订单只读能力：归属判定只在业务层，Agent 侧不持有客户身份。

主图与旧图都从这里取订单事实，因此"跨客户订单被拒"只有一处实现：

* 工具参数只有 ``order_id``，没有任何客户/租户参数 —— prompt 无法指定别人；
* 身份来自服务端 ContextVar（由入口在鉴权后设置），缺失即 ``unverified``；
* 业务层状态码映射成固定码集，主图与评审规则据此判断，不解析自然语言。

这一层永不写订单：退款、支付、改单、建单都不在这里，也不在任何 Agent 里。
"""

from __future__ import annotations

from app.agent.support.state import OrderFact

ORDER_CODES = {"ok", "not_found", "unauthenticated", "forbidden", "unavailable", "unverified"}
"""主图与评审器认识的唯一码集；不在表内的一律降级为 ``unverified``。"""


def normalize_code(code: str) -> str:
    """业务层返回 ``OK`` / ``NOT_FOUND``，图契约是小写。"""
    normalized = str(code or "").strip().lower()
    return normalized if normalized in ORDER_CODES else "unverified"


def _as_fact(code: str, text: str, order_id: str = "") -> OrderFact:
    fact: OrderFact = {"code": normalize_code(code), "text": text[:600]}
    if order_id:
        fact["order_id"] = order_id
    return fact


async def read_order(order_id: str) -> OrderFact:
    """查一单（只读）。归属由业务接口判定，结果码如实回传。"""
    from app.agent.tools import lookup_order

    result = await lookup_order(order_id)
    return _as_fact(result.code, result.text, order_id)


async def list_orders() -> OrderFact:
    """列出**当前登录客户**的订单（只读）。"""
    from app.agent.tools import list_owned_orders

    result = await list_owned_orders()
    return _as_fact(result.code, result.text)


def current_identity() -> tuple[str, str]:
    """服务端会话里的 ``(tenant_id, thread_id)``；入口鉴权后写入。"""
    from app.infra.context import tenant_var, thread_var

    tenant = tenant_var.get() or ""
    thread = (thread_var.get() if thread_var is not None else "") or ""
    return tenant, thread


def identity_is_verified(identity: tuple[str, str]) -> bool:
    """``thread_id`` 必须以 ``{tenant_id}:`` 开头 —— 这是唯一的归属凭据。"""
    tenant, thread = identity
    return bool(tenant) and thread.startswith(f"{tenant}:")


__all__ = ["ORDER_CODES", "current_identity", "identity_is_verified",
           "list_orders", "normalize_code", "read_order"]
