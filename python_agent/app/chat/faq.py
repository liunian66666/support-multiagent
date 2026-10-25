"""
[ours] 本文件为 fork 新增：流程级 FAQ 规则（意图/关键词匹配 + 转人工/订单号识别）。
Conservative, tenant-neutral process FAQ imported from support-agent's approved process playbook.

Matching is intent/keyword based (not exact-string) so common paraphrases hit
the deterministic answer instead of falling through to LLM/RAG. Answers stay
process-level: they never assert a store policy, order state, refund
eligibility or a completed action. Tenant-owned facts stay in Qdrant, so
policy questions (postage thresholds, timelines, fees) intentionally miss
here and are answered by the tenant knowledge base.
"""
import re

_ANSWERS = {
    "return_apply": "请提供订单号和退货原因。客服核验订单归属、商品情况及商家政策后，才能判断如何处理。",
    "refund_apply": "请说明订单号与退款原因，客服核验订单归属、支付情况及商家政策后才能判断处理方式。当前系统不自动发起退款。",
    "order_status": "查询订单状态请提供订单号；如果不记得订单号，登录客户账号后可以回复「我的订单」，从本人订单列表中选择。",
    "find_order_id": "登录客户账号后，可以回复「我的订单」从本人订单列表中选择。订单号也可在以下位置找到：\n1. **购买记录**：登录平台进入「我的订单」页面查看\n2. **收货短信**：发货和揽收通知短信\n3. **电子发票**：发票上的订单编号\n4. **支付凭证**：支付宝/微信支付账单详情\n\n提供订单号后，我会先核验订单归属，再查询状态。",
    "capabilities": "您好！我是智能客服小Q，可以帮您解答以下问题：\n1. **平台与课程信息**：课程内容、平台政策等问题，我会帮您查询知识库\n2. **订单查询**：提供订单号，我会先核对订单归属，再返回该订单的状态\n3. **通用客服流程**：退货申请、退款咨询等流程说明\n4. **转接人工**：遇到无法解决的问题，我可以帮您转接人工客服\n\n订单状态查询已接通；退款和支付操作尚未接通，不能代为执行，相关请求会转人工处理。\n请问有什么可以帮您的吗？",
}

# 意图规则：(答案键, 关键词组A, 关键词组B)。两组各需命中其一才算该意图；
# 组A 约束话题（退货/退款/订单/能力），组B 约束问法（申请/查询/怎么……），
# 避免把政策类问题（多久到账、运费谁出）误判成流程问题。
_INTENTS = (
    ("return_apply", ("退货", "退换"), ("怎么", "如何", "申请", "操作", "流程", "办理", "方法", "步骤", "要退", "想退")),
    ("refund_apply", ("退款",), ("怎么", "如何", "申请", "操作", "流程", "办理", "方法", "步骤", "要退", "想退")),
    ("order_status", ("订单", "快递", "物流", "包裹"), ("状态", "到哪", "查", "进度")),
    ("find_order_id", ("订单号", "单号", "订单编号"), ("怎么找", "在哪", "在哪看", "怎么查", "是什么", "咋查", "如何查看", "哪里看", "咋找", "咋看", "在哪查", "怎么查看")),
    ("capabilities", ("能做什么", "可以做什么", "都会什么", "你会什么", "有什么功能", "能干嘛", "会干嘛", "什么功能", "提供什么服务", "你能帮我"), None),
)

_HANDOFF_EXACT = {"转人工", "转人工客服", "转接人工", "转接人工客服"}
_POLICY_QUESTION = re.compile(
    r"(?:退款|退货|退换).{0,12}(?:政策|规则|条件|期限|时效|到账|运费|手续费)"
    r"|(?:政策|规则|条件).{0,12}(?:退款|退货|退换)"
    # [ours] 问时限＝问政策事实，不能被流程话术抢答（留出集ho-t08实锤：
    # 「签收之后最晚几天内能申请退货」被return_apply话术挡掉，7天时限答不出）
    r"|(?:签收|收货|发出|发货|下单|退款|退货|退换|开票|开具)[^。]{0,12}(?:几天|多久|几个?工作日|多少天|期限|时限)"
)


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
    if is_policy_question(text):
        return None
    key = _intent(text)
    if not key:
        return None
    return _ANSWERS[key]


def is_policy_question(text: str) -> bool:
    """店铺政策事实必须进入租户知识库，不能被通用流程话术抢答。"""
    return bool(_POLICY_QUESTION.search(normalize(text)))


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
    """从用户消息中提取订单号。

    支持格式：
    - 演示格式：ORD-2024-001（要求订单/快递/物流/包裹上下文，避免误匹配）
    - 电商前缀：TB2024xxx / JD2024xxx / PDDxxx / DYxxx / TMALLxxx / 1688xxx（识别性强，不要求上下文）
    - 显式上下文：「订单号是 xxx」「单号: xxx」「订单编号 xxx」（此时可识别无前缀纯数字/字母数字组合）
    """
    if not text:
        return None
    # 1. 显式上下文优先：「订单号是 ABC123」「单号: 12345678」「订单编号 1234567890」（空格/冒号/是为分隔）
    explicit = re.search(r"(?:订单号|单号|订单编号)[\s:=：是为]*(?P<oid>[A-Za-z0-9-]{4,40})", text, re.IGNORECASE)
    if explicit and not explicit.group("oid").endswith("-"):
        return explicit.group("oid")
    # 2. [ours] 「订单」直接接单号（用户常说「查询订单10001」而不带「号」字）
    # 纯数字要求 ≥5 位，避免把「订单2024年发货吗」里的年份当成单号
    ctx = re.search(r"订单[\s:=：是为]*(?P<oid>[A-Za-z0-9-]{4,40})", text, re.IGNORECASE)
    if ctx and not ctx.group("oid").endswith("-") and not (ctx.group("oid").isdigit() and len(ctx.group("oid")) < 5):
        return ctx.group("oid")
    # 3. 电商前缀（识别性强，不要求上下文）
    m = re.search(r"(?<![A-Za-z0-9-])(?:TB|JD|PDD|DY|TMALL|1688)-?\d{6,20}(?![A-Za-z0-9-])", text, re.IGNORECASE)
    if m:
        return m.group(0)
    # 3. 演示格式 ORD-（要求订单/快递/物流/包裹/单号上下文，避免误匹配）
    if re.search(r"订单|order|快递|物流|包裹|单号", text, re.IGNORECASE):
        m = re.search(r"(?<![A-Za-z0-9-])ORD-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9-])", text, re.IGNORECASE)
        if m and 3 <= len(m.group(0)) - 4 <= 64:
            return m.group(0)
    # 3b. [ours] 演示库订单号（ORD-xxxx-xxx，两段及以上数字段）特征足够强，
    # 无上下文词也直接命中——修复「ORD-2024-001现在到哪一步了」被路由到 general
    m = re.search(r"(?<![A-Za-z0-9-])ORD-\d{3,}-\d{1,}(?![A-Za-z0-9-])", text)
    if m:
        return m.group(0)
    return None


# 指代词：用于历史订单回溯（「刚才那个订单」「之前那个快递」「它现在怎样」）
_REFERENCE_WORDS = ("刚才", "那个", "之前", "上次", "前面", "刚才的", "那个订单", "它", "这单", "这订单")
_ORDER_CONTEXT_WORDS = ("订单", "这单", "那单", "快递", "物流", "包裹", "单号", "状态", "到哪", "怎样", "咋样", "怎么了", "咋样了", "现在")


def has_order_reference(text: str) -> bool:
    """检测消息是否含订单/快递上下文 + 指代词，需从历史回溯订单号。

    用于「刚才那个订单现在怎样」「之前那个快递到哪了」这类指代场景：
    当前消息无具体订单号，但有订单上下文 + 指代词时，从历史取最近订单号。
    """
    if not text:
        return False
    value = normalize(text)
    has_context = any(k in value for k in _ORDER_CONTEXT_WORDS)
    has_ref = any(k in value for k in _REFERENCE_WORDS)
    return has_context and has_ref
