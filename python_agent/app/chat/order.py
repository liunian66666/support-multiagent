"""订单查询结果码、转人工判定与「刚才那单」的订单号回溯（从 main.py 拆出）。"""
import logging
import re

from langchain_core.messages import AIMessage, HumanMessage

from app.chat.faq import order_id as process_order_id

logger = logging.getLogger("agent")

# [ours] 订单查询结果码：判定依据是结构化 code，不再匹配面向用户的文案。
# 早期实现按「未找到订单/不属于您/暂不可用」等文案子串判定，改一句措辞就会改流程。
ORDER_OK = "OK"                            # 查到且归属校验通过
ORDER_NOT_FOUND = "NOT_FOUND"              # 404：单号不存在或不属于该账号（服务端合并，防枚举）
ORDER_UNAUTHENTICATED = "UNAUTHENTICATED"  # 401：访客/未登录
ORDER_FORBIDDEN = "FORBIDDEN"              # 403：归属校验不通过
ORDER_UNAVAILABLE = "UNAVAILABLE"          # 5xx/网络异常：订单系统不可用
ORDER_UNVERIFIED = "UNVERIFIED"            # 身份上下文缺失或未知业务码

# 只有「用户无法自助纠正」的失败才升级人工；NOT_FOUND / UNAUTHENTICATED 留在自助链路：
# 单号打错、未登录都会让用户先自己纠正一次，避免一个字打错就静默建单。
AUTO_HANDOFF_ORDER_CODES = frozenset({ORDER_FORBIDDEN, ORDER_UNAVAILABLE, ORDER_UNVERIFIED})

_ORDER_HANDOFF_REASONS = {
    ORDER_FORBIDDEN: "订单 {oid} 归属校验未通过，需要人工核实",
    ORDER_UNAVAILABLE: "订单系统暂时不可用，无法核验订单 {oid}",
    ORDER_UNVERIFIED: "订单身份上下文缺失，无法核验订单 {oid}",
}


def should_auto_handoff(code: str | None) -> bool:
    """该结果码是否需要自动转人工（结构化判定，与文案解耦）。"""
    return code in AUTO_HANDOFF_ORDER_CODES


def order_handoff_reason(code: str | None, order_id: str) -> str:
    """转人工原因：会透出到前端，必须是用户能看懂的一句话。"""
    template = _ORDER_HANDOFF_REASONS.get(code or "", "订单 {oid} 查询未通过核验")
    return template.format(oid=order_id)


# [ours] 用户在被索要订单号后直接回复裸单号（如「10110」「ORD-2024-001」）。
# 原有 order_id() 只认带上下文/前缀的写法，导致「请提供订单号」→「10110」落到 general 分支。
_BARE_ORDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{3,39}$")
_ASK_HINTS = ("请提供", "请发送", "请回复", "麻烦提供", "提供订单号", "发我", "告诉我")


def looks_like_bare_order_id(text: str) -> bool:
    """消息本身就是一串订单号形态的字符（无「订单号」前缀）。

    仅在「上一条回复正在索要订单号」时使用，不能单独作为订单意图判据。
    """
    t = (text or "").strip()
    if not _BARE_ORDER_ID.match(t):
        return False
    return any(c.isdigit() for c in t)


async def _awaiting_order_id(app, thread_id: str) -> bool:
    """上一条 AI 回复是否在向用户索要订单号。读取失败按「否」处理。"""
    bridge = getattr(app.state, "faq_bridge", None)
    if bridge is None:
        return False
    try:
        state = await bridge.aget_state({"configurable": {"thread_id": thread_id}})
        if not state or not state.values:
            return False
        for msg in reversed(state.values.get("messages", [])):
            if isinstance(msg, AIMessage):
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                return "订单号" in content and any(h in content for h in _ASK_HINTS)
        return False
    except Exception:
        logger.exception("awaiting_order_id failed thread=%s", thread_id)
        return False



# [ours] 新增：指代回溯——「刚才/那个/之前」从检查点历史取最近订单号
async def _last_order_id_from_history(app, thread_id: str) -> str | None:
    """从会话检查点历史消息中找最近一条用户消息里的订单号。

    用于「刚才那个订单」「之前那个快递」这类指代场景：当前消息无具体订单号，
    但有订单上下文 + 指代词时，从历史复用上一轮订单号，避免 LLM 答非所问。
    读取失败不影响主流程，仅返回 None。
    """
    bridge = getattr(app.state, "faq_bridge", None)
    if bridge is None:
        return None
    try:
        config = {"configurable": {"thread_id": thread_id}}
        state = await bridge.aget_state(config)
        if not state or not state.values:
            return None
        messages = state.values.get("messages", [])
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                oid = process_order_id(msg.content)
                if oid:
                    return oid
        return None
    except Exception:
        logger.exception("last_order_id_from_history failed thread=%s", thread_id)
        return None
