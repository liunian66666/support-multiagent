# 中文文案必然包含全角标点
"""Deterministic review rules.

This is the Reviewer: plain predicates over state, no model call, and deliberately
not counted as a fifth agent. It answers one question — may this draft reach the
customer? — and splits its findings in two tiers, because a false positive in the
soft tier costs a retry while a false positive in the hard tier costs an answer:

``hard``  the draft must never be sent (fabricated transaction, unevidenced policy
          number, another customer's order id, a claim contradicted by the
          verified order state).
``soft``  the turn is incomplete (a required specialist was not consulted); it
          drives one bounded retry, and if the budget runs out the draft may still
          be released only when no hard rule fired.

Order ownership is never decided here: the Business service already refused the
lookup, and this module only checks that the draft quotes what Business returned.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

from .state import Delegation

# --- question shape -----------------------------------------------------------

POLICY_MARKERS = re.compile(
    r"退货|退换|换货|退款政策|退款规则|包邮|运费|邮费|发票|开票|售后|保修|质保|理赔|保障|"
    r"支付方式|货到付款|分期|到账|时效|期限|多久|几天|手续费|押金|定金|起订|门槛")
"""「退换」是一个词，既不含「退货」也不含「换货」；漏了它，客户问「可以退换吗」
连政策问题都算不上，Reviewer 也不会要求这一轮必须派 FAQ。"""
ORDER_MARKERS = re.compile(
    r"(?:ORD-[A-Za-z0-9-]+|TB\d{6,}|JD\d{6,}|PDD\d{6,}|DY\d{6,}|SF\d{6,})|"
    r"我的订单|查(?:一下|看|询|下)?(?:我的)?订单|订单(?:号|状态|进度|信息)|"
    r"物流|发货|签收|快递到哪|什么时候到", re.I)
ACCOUNT_MARKERS = re.compile(
    r"我是谁|我的(?:账号|账户|身份|姓名|用户名|密码)|(?:账号|账户|登录|支付)密码|"
    r"忘记密码|找回密码|重置密码|修改密码|个人资料|个人信息|身份信息")
# 寒暄/致谢/告别，必须整句匹配：单角色透传只对这一档放行，判据取严。
GREETING_MARKERS = re.compile(
    r"^(?:你好|您好|hi|hello|hey|在吗|在么|在不在|有人吗|早上好|上午好|中午好|下午好|"
    r"晚上好|晚安|谢谢|多谢|感谢|辛苦了|好的|好嘞|嗯嗯|收到|明白|再见|拜拜)"
    r"[\s!！。,.，~～、?？]*$", re.I)
GREETING_MAX = 24
"""超过这个长度基本都带了具体诉求，不再算寒暄。"""

HOWTO_MARKERS = re.compile(
    r"怎么(?:查|看|找|弄|办|操作|申请|退|换|下载|开|用|设置|改)|"
    r"如何(?:查|看|找|办|操作|申请|退|换|下载|开|用|设置|改)|"
    r"怎样|在哪(?:里|儿)?|哪里|入口|步骤|路径|从哪|去哪|账单|交易记录")
"""操作指引型提问：客户要「第一步做什么」，不是「能不能」的判定。

这类问题套用资格判定的三选一结论，会被逼出「目前无法确认」，模型随后自行补一段
菜单路径——那正是无依据时最容易编造的地方。实际线上案例：「怎么查询账单信息？」
答成了「目前无法确认。本轮店铺资料中没有查到……建议登录商城后在个人中心查看」。
"""

# --- forbidden claims ---------------------------------------------------------

# Claims that an action already happened. The old form matched only 「已(经)(为您)?+动词」
# adjacency, which a model defeats by reordering: 「退款申请这边已经提交了」, 「已经帮您
# 把地址改好了」, 「我们直接把款项打回您的账户」. These are now detected clause-wise:
# an action noun plus a completion marker anywhere in the same clause.
EXECUTED_CLAIM = re.compile(
    r"(?:已(?:经)?(?:为您)?|现在已)(退款|退还|打款|支付|扣款|开通|取消订单|发货|退货|换货|"
    r"创建工单|建工单|转接人工|转人工|提交申请|安排上门)")
# Action nouns a read-only graph can never have performed.
EXEC_ACTION = re.compile(r"(退款申请|申请退款|退款|退了款|退回款项|退还|打款|款项打回|退回.{0,3}账户"
                         r"|支付|付款|扣款|开通|取消订单|建工单|创建工单|工单|转接人工|转人工"
                         r"|提交.{0,4}申请|申请.{0,4}提交|上门取件|安排上门|改地址|修改地址|变更地址|地址"
                         r"|换货|退货|发货)")
# Completion markers, kept deliberately narrow: a bare 「了」 is too common
# ("了解了", "了解了您的情况") and would over-block.
PERFECT = re.compile(r"(已经|已|刚刚|现已|这边已|随后会?已)[^。！？;；]{0,14}?"
                     r"(提交|办理|建好|建了|开好|改好|改完|打回|退回|安排|处理|完成|操作|转接|扣|支付|开通)"
                     r"|(?:把|将)[^。！？;；]{0,10}?(?:改好|改完|办好|弄好|处理好|完成|打回|退回|退了|退了款)"
                     r"|(退了款|建好了|改好了|办好了|打回了|退回去了)")
# Verbs no read-only lookup can ever evidence.
NEVER_EVIDENCABLE = {"退还", "打款", "支付", "扣款", "开通", "取消订单", "创建工单", "建工单",
                     "转接人工", "转人工", "提交申请", "安排上门",
                     "退款", "退款申请", "申请退款", "退了款", "退回款项", "款项打回", "付款",
                     "上门取件", "改地址", "修改地址", "变更地址", "地址", "工单", "打回"}
# Promising to perform a disabled operation is as closed as claiming it was done.
# Narrow on purpose: 「您可以申请退款」 is a customer action and must stay allowed.
EXEC_PROMISE = re.compile(r"(?:帮|给|替)您?[^。！？;；]{0,6}?(?:把|将)?[^。！？;；]{0,6}?"
                          r"(退款|支付|打款|扣款|取消订单|换货|退货|开票)[^。！？;；]{0,8}?"
                          r"(操作|完成|办理|办好|搞定|处理完)")
ORDER_ID_IN_TEXT = re.compile(
    r"(?<![A-Za-z0-9-])(?:ORD-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*|"
    r"(?:TB|JD|PDD|DY|TMALL|1688|SF)[ -]?\d{6,20})(?![A-Za-z0-9-])", re.I)
# Arabic *or* Chinese numerals, a currency prefix, and the units a shop actually uses:
# 「七天」「一周」「满九十九元包邮」「￥8」 are policy values exactly as much as 「7 天」 is.
_CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
       "八": 8, "九": 9, "十": 10, "百": 100, "千": 1000, "半": 0.5}
POLICY_NUMBER = re.compile(r"(\d+(?:\.\d+)?|[零一二两三四五六七八九十百千半]{1,6})\s*"
                           r"(元|天|日|周|年|个工作日|工作日|小时|个月|月|%)")
MONEY_PREFIX = re.compile(r"(?:￥|¥|RMB)\s*(\d+(?:\.\d+)?)")
UNIT_ALIASES = {"个工作日": "工作日", "日": "天", "月": "个月"}
# An order *field assertion* needs the label plus an explicit separator; "签收后 7 天内"
# is policy wording, not an order fact, and must not trip the unverified-fact rule.
# 商品/货物 + 状态 是描述货物本身（"商品状态：完好、吊牌齐全"），属于知识库政策口径，
# 不能按"本人订单事实"拦下；只有"订单状态：已发货"这类才需要订单接口背书。
ORDER_FIELD = re.compile(
    r"(?P<prefix>商品|货物|衣物|包装|产品)?"
    r"(?P<label>状态|金额|价格|物流|发货|签收|下单时间|订单号)\s*[:：]")
# Verified state that contradicts a "already on its way" style promise.
NOT_SHIPPED = re.compile(r"未支付|待支付|未发货|待发货|已取消|已退款|退款成功")
CLAIM_SHIPPED = re.compile(r"已发货|已签收|运输中|已揽收|包裹已|快件已|正在派送")
# A draft that only asks or hedges has not asserted anything, so soft rules skip it.
NON_ASSERTIVE = re.compile(r"[?？]|建议|请以|为准|无法|暂不|不确定|请(?:提供|补充|稍后|核对)|"
                           r"未(?:说明|提及)|需要.{0,6}核实")

INTERNAL_LEAK = re.compile(r"\[K\d+\]|知识库片段|检索片段|chunk_id|doc_id|score\s*[:=]")
INTERNAL_PROCESS = re.compile(
    r"(?:政策|订单|FAQ|Order|General).{0,12}(?:核验|查询|检索).{0,12}(?:这一路|分支|节点|没有返回结果)"
    r"|(?:FAQ|Order|General)\s*Agent", re.I)
UNCONFIRMED_HANDOFF = re.compile(
    r"(?:请求|问题|申请).{0,12}(?:会|将|由).{0,10}人工(?:客服)?.{0,12}(?:跟进|处理|联系|接管)"
    r"|人工(?:客服)?.{0,8}(?:会|将).{0,10}(?:跟进|处理|联系|接管)"
    r"|(?:稍后|随后|接下来|届时).{0,14}(?:工单(?:号|提示)?|人工(?:客服)?)"
    r"|(?:会|将|即将).{0,10}(?:创建|生成|提交|建立).{0,4}工单")

BLOCKED_TERMS = ("黄X", "X博", "违法")
"""请求本身越界，不必再走向任何专家。放在 guards 里是因为两个入口都要用同一条判定：
图入口的 ``precheck`` 节点，以及每个 agent 内的 ``before_model`` 安全闸。"""

BLOCKED_REPLY = "抱歉，我不能处理这个请求。"


def is_blocked_question(text: str) -> bool:
    return any(word in text for word in BLOCKED_TERMS)

#: 这些字段天然只属于某一笔订单，出现即视为在陈述本人订单事实
_INHERENT_ORDER_LABELS = frozenset({"订单号", "下单时间", "金额", "价格"})
#: 订单指代：字段旁出现这些词，说明说的是"您这一单"而不是通用政策
_ORDER_CONTEXT = re.compile(r"订单|单号|本单|这笔|这单|该单|您(?:的)?(?:包裹|快递)")


def asserts_order_field(draft: str) -> bool:
    """草稿是否断言了只有订单接口才能背书的字段。

    ``商品状态：完好``、``物流：发货后 24 小时可查`` 是知识库政策口径，不是"您这一单"的
    事实；``订单状态：已发货``、``金额：199 元`` 才是。判定分三层：

    1. ``金额 / 价格 / 订单号 / 下单时间`` 天然只属于某一笔订单，直接算订单事实；
    2. 带货物前缀的 ``状态``（商品状态、包装状态）描述货物本身，豁免；
    3. 其余字段（状态 / 物流 / 发货 / 签收）只有在旁边出现订单指代，或冒号后
       紧跟订单状态词（已发货、待发货、已签收…）时，才算订单事实。
    """
    for match in ORDER_FIELD.finditer(draft):
        label = match.group("label")
        if label in _INHERENT_ORDER_LABELS:
            return True
        # 冒号后紧跟订单状态词：不管前面写的是"商品状态"还是"状态"，都是在说这一单
        value = draft[match.end():match.end() + 16]
        if NOT_SHIPPED.search(value) or CLAIM_SHIPPED.search(value):
            return True
        if label == "状态" and match.group("prefix"):
            continue
        window = draft[max(0, match.start() - 14):match.end() + 14]
        if _ORDER_CONTEXT.search(window):
            return True
    return False


def polish_customer_reply(text: str, question: str, delegations: list[Delegation]) -> str:
    """Remove internal wording and unsupported navigation from customer replies."""
    text = text.replace("店铺知识库", "店铺现有规则").replace("知识库", "店铺资料")
    text = text.replace("需要我帮您转接人工客服吗？", "如需人工协助，请回复「转人工」。")
    text, removed = re.subn(
        r"(?m)^\s*(?:\d+[.、]\s*|[-*]\s*)?在订单详情页查看是否有[“\"']?取消订单[”\"']?入口[；;。]?\s*$",
        "", text)
    if removed:
        text = re.sub(r"(?m)^\s*2[.、]\s*", "", text, count=1)
    text = re.sub(r"(?m)^\s*建议您可以[:：]\s*$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    verified = " ".join(r.get("text", "") for item in delegations
                        for r in item.get("verified_results", []) if r.get("code") == "ok")
    if ("课程" in verified and "待发货" in verified and "待发货" in text and
            re.search(r"退货|退款|取消", question) and
            not re.search(r"课程.{0,12}(?:开通|使用|交付)|(?:开通|使用|交付).{0,12}课程", text)):
        text += "\n\n这笔订单是课程，还需核实课程是否已开通或使用，才能判断能否取消或退款。"
    return text


_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def is_policy_question(text: str) -> bool:
    return bool(POLICY_MARKERS.search(text))


def is_order_question(text: str) -> bool:
    return bool(ORDER_MARKERS.search(text))


def is_account_question(text: str) -> bool:
    return bool(ACCOUNT_MARKERS.search(text))


def is_howto_question(text: str) -> bool:
    """客户在问操作路径/步骤，而不是资格判定。``compose`` 据此切换写作档位。"""
    return bool(HOWTO_MARKERS.search(text))


def is_greeting(text: str) -> bool:
    """寒暄、致谢、告别：短、整句、不含任何可核实项。

    只有这一档允许单角色透传（不经过 compose 重写），所以判据取严：必须整句匹配寒暄词，
    且不带政策/订单/账户标记——「你好，我这个订单能退吗」不算寒暄。
    """
    stripped = text.strip()
    if not stripped or len(stripped) > GREETING_MAX:
        return False
    if is_policy_question(stripped) or is_order_question(stripped) or is_account_question(stripped):
        return False
    return bool(GREETING_MARKERS.match(stripped))


def account_safe_reply(question: str) -> str:
    """No account identity or credential store is available to this read-only chat."""
    identity = bool(re.search(r"我是谁|我的(?:账号|账户|身份|姓名|用户名)|个人资料|个人信息|身份信息", question))
    password = bool(re.search(r"密码", question))
    if identity and password:
        return ("我无法仅凭聊天确认您的真实身份，也无法查看或提供账号密码。"
                "如果您使用的是商城账号，请通过该商城的官方账号渠道核对资料或处理密码问题。")
    if password:
        return "我无法查看或提供账号密码。如果您使用的是商城账号，请通过该商城的官方账号渠道处理密码问题。"
    return "我无法仅凭聊天确认您的真实身份。如果您使用的是商城账号，请通过该商城的官方账号渠道核对资料。"


def is_assertive(text: str) -> bool:
    return bool(text.strip()) and not NON_ASSERTIVE.search(text)


def _to_decimal(value: str) -> Decimal | None:
    """``7`` and ``七天`` are the same claim; only ``7`` parses as a number.

    Chinese numerals are ordinary in customer-facing Chinese ("签收后七天", "满九十九元
    包邮"), so a digit-only detector lets a model state an unevidenced policy value just
    by writing it out. Supports the 十/百/千 compositional forms.
    """
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _from_chinese(value: str) -> Decimal | None:
    if any(ch not in _CN and ch not in _CN_UNITS for ch in value):
        return None
    total, current = Decimal(0), Decimal(0)
    for ch in value:
        if ch in _CN_UNITS:
            total += (current or Decimal(1)) * _CN_UNITS[ch]
            current = Decimal(0)
        else:
            current += Decimal(_CN[ch])
    return total + current


def _numbers_with_unit(text: str) -> set[tuple[Decimal, str]]:
    """Numbers paired with their unit: ``7 天`` must never vouch for ``7 元``."""
    found: set[tuple[Decimal, str]] = set()
    for value, unit in POLICY_NUMBER.findall(text):
        number = _to_decimal(value) if value[0].isdigit() else _from_chinese(value)
        if number is not None:
            found.add((number, UNIT_ALIASES.get(unit, unit)))
    for amount in MONEY_PREFIX.findall(text):
        number = _to_decimal(amount)
        if number is not None:
            found.add((number, "元"))
    return found


def evidence_supported(text: str, delegations: list[Delegation]) -> bool:
    """Every 数字+单位 in the draft must appear in this turn's evidence or order facts."""
    claims = _numbers_with_unit(text)
    if not claims:
        return True
    source = " ".join([item["answer"] for item in delegations]
                      + [e["text"] for item in delegations for e in item["evidence"]]
                      + [r["text"] for item in delegations for r in item["verified_results"]])
    return claims <= _numbers_with_unit(source)


def _ids(text: str) -> set[str]:
    """Order ids with full-width folded first.

    「ＴＢ１２３４５６７」 is the same id as 「TB1234567」, but the regex only sees ASCII
    digits, so a foreign order number written in full-width characters used to slip past
    both the "quoted id" and the "verified id" side of the cross-customer rule.
    """
    return {str(identifier) for identifier in
            ORDER_ID_IN_TEXT.findall(unicodedata.normalize("NFKC", text))}


def _order_segments(text: str) -> dict[str, str]:
    """Associate each status phrase with its own order, even in a multi-order list."""
    normalized = unicodedata.normalize("NFKC", text)
    matches = list(ORDER_ID_IN_TEXT.finditer(normalized))
    return {match.group(0).upper(): normalized[match.end():
            matches[index + 1].start() if index + 1 < len(matches) else len(normalized)]
            for index, match in enumerate(matches)}


def verified_order_ids(delegations: list[Delegation]) -> set[str]:
    ids: set[str] = set()
    for item in delegations:
        for result in item["verified_results"]:
            if result.get("code") == "ok":
                ids |= _ids(result.get("text", ""))
        if item["specialist"] == "order":
            ids |= _ids(item["answer"])
    return ids


def unbacked_claims(text: str, delegations: list[Delegation]) -> list[str]:
    """Executed-action phrasings that no read-only verification from this turn supports.

    Clause-wise, because the adversarial sweep showed adjacency matching loses to plain
    reordering: 「退款申请这边已经提交了」 and 「这边已经把地址改好了」 put the action
    noun and the completion marker on opposite sides of the clause. A clause carrying an
    action noun *and* a completion marker claims that we acted; whether that is admissible
    still depends on Business evidence, so a verified 「已发货」 stays legal.
    """
    verified = " ".join(r["text"] for item in delegations for r in item["verified_results"]
                        if r.get("code") == "ok")
    offenders = set(EXECUTED_CLAIM.findall(text))
    for clause in re.split(r"[。！!？?；;，,、\n]", text):
        action = EXEC_ACTION.search(clause)
        if action and PERFECT.search(clause):
            offenders.add(action.group(1))
    return [verb for verb in dict.fromkeys(offenders)
            if verb in NEVER_EVIDENCABLE or verb not in verified]


# --- answer completeness ------------------------------------------------------

# 「能不能退」是一个判定问题，客户要的是结论。实测里 Supervisor 把 700 字的政策条款
# 直接铺给客户：材料齐了、证据齐了、安全规则一条没犯，答复却等于没答。这一组判据
# 补的就是这一格——它看的是"有没有回答"，不是"回答得对不对"，所以全部进 soft 层：
# 命中只驱动重写，不会把一条有材料的答复降级成"无法核实"。
#
# 只认二值判定问法：``多久``/``谁承担``/``到哪了`` 要的是事实或数值，不受这条约束。
DECISION_MARKERS = re.compile(
    r"能(?:不能|否)|可否|可不可以|行不行|是否(?:可以|能|支持|允许)|可以.{0,8}吗|支持.{0,8}吗")
# 结论词只认明确的判定措辞。政策原句「签收后 7 天内可申请退货」不在其中：那是条款，
# 不是对这单的回答；把它算成结论，就等于承认政策复述可以充当答复。
CONCLUSION = re.compile(
    r"可以(?:为您)?(?:申请|办理|退还|退货|退款)|(?:仍|还)可以(?:申请|退|换)|"
    r"不能(?:申请|退|换|办理)|不可以|不予(?:受理|退还|退)|不支持(?:申请|退|换|退货|退款)|"
    r"无法(?:确认|判断|核定)|暂(?:时)?无法|需要?(?:您)?(?:再)?核实|"
    r"已(?:超出|超过)[^。！？]{0,6}(?:期限|时效)|仍在[^。！？]{0,8}内")
# 客户会分开问的互斥条件。同一句里出现两项以上时，草稿必须逐项给出落点。
# 「未拆封」与「已拆封」必须互斥匹配：``没拆封`` 里的 ``拆封`` 不能被当成已拆封的证据。
_ASK_ITEMS = {
    "未拆封": re.compile(r"没拆封|未拆封|没拆开|未拆开|没开封|未开封|没动过|未使用|没用过|包装完好|原封"),
    "已拆封": re.compile(r"(?<![未没])拆(?:封|开)|已使用|使用过|用过了"),
    "超期": re.compile(r"超(?:过|出)[^。！？]{0,6}(?:七|7|天|期限|时效)|逾期|过了[^。！？]{0,4}天"),
}
# 对"这一笔订单"下能否退款的结论，必须有本轮本人订单核验兜着；否则是拿通用政策
# 冒充这笔订单的判定——正是客户看到「这笔订单可以退」却查不到依据的那种回答。
_THIS_ORDER_VERDICT = re.compile(
    r"(?:这|该|本)(?:笔|单|个)(?:订单|商品)[^。！？]{0,14}?"
    r"(?:可以申请|可以退|能退|不能退|不支持|不予)")
_LONG_ANSWER = 240
"""超过这个字数还看不到结论，就不再是简短回答，而是政策复述（实测 700 字）。"""
_CONCLUSION_LEAD = 160


INTERNAL_TERMS = ("本轮店铺资料", "店铺资料中", "没有查到", "未查到", "检索失败",
                  "专职 Agent", "核验结果", "检索片段", "本轮尚未核实", "待业务确认")
"""答复里不该出现的内部过程措辞。

刻意取复合短语而不是「本轮」「检索」这类词：后者在正常答复和现有用例里到处都是，
一拦就会大面积误伤。这里只对应真实线上那句「本轮店铺资料中没有查到……」。
"""

COURTESY_TAIL = re.compile(
    r"如有其他问题|如有其他疑问|很乐意(?:继续)?为您|随时(?:欢迎)?(?:咨询|联系)|祝您生活愉快")
"""与诉求无关的结尾客套：客户没拿到答案时先看到这句，观感最差。"""

UNCONFIRMED_VERDICT = re.compile(
    r"目前无法确认|无法(?:确认|判断|核定)|暂(?:时)?无法|不能(?:判断|确定)")
"""「已经下了一个消极判定」的措辞，不是澄清。

``NON_ASSERTIVE`` 里的「无法」会把这类整段答复当成澄清式回答提前放行，于是漏项、
结论前置等形状检查全部失效——最防御性的答复反而最容易通过 Reviewer。含这些词的
草稿必须继续走后面的形状检查；真正在反问客户的句子（带问号、请补充）不含它们。
"""

RETURN_SWAP_QUESTION = re.compile(r"退换|退或换|退货.{0,6}换货|换货.{0,6}退货")
RETURN_TERM = re.compile(r"退货|退款|退回|退掉")
SWAP_TERM = re.compile(r"换货|换码|换尺码|换颜色|调换|换一个|换款")
"""「退换」其实是两件事。``_ASK_ITEMS`` 只认拆解条件（未拆封/已拆封/超期），认不出
这一档，所以「只讲退货、不提换货」的答复一直能过。KB 里换货条款是有的。"""


def has_policy_basis(delegations: list[Delegation]) -> bool:
    """本轮是否有可引用的政策依据，足以把已知条件推成结论。

    只认 FAQ 这一路的证据，且该路不能失败：只有订单状态、政策这一路没派或失败时，
    缺的正是判定条件，写「目前无法确认」是诚实的，不该被推去重答。反过来，只要有
    政策条款，再叠上订单状态，就没有理由把「条件尚未满足」说成「信息不足」。
    """
    return any(item["evidence"] and not item["failed"]
               for item in delegations if item["specialist"] == "faq")


def answer_gaps(question: str, draft: str, delegations: list[Delegation]) -> list[str]:
    """答复是否真的回答了客户问的那几件事。

    四条判据，都只在草稿是断言性文本时生效（澄清式回答不在此列）：

    1. 判定型问题 + 长答复 + 通篇没有结论词：客户问「能不能」，收到的是一段政策；
    2. 客户分别问了两项及以上互斥条件（未拆封 / 已拆封 / 超期），草稿缺其中一项；
    3. 结论出现在长答复的后半段，客户得先读完条款才知道能不能；
    4. 对「这一笔订单」下了能否退款的结论，但本轮没有本人订单核验。

    阈值取 240 字：短答复里的澄清、反问、拒绝都不该被这条推去重写，只有铺开的条款
    才算"用政策代替结论"。
    """
    gaps: list[str] = []
    # 这三条与「草稿是不是断言」无关，必须放在 is_assertive 早退之前：澄清式、
    # 反问式答复里出现内部过程词或客套尾句，一样不该发给客户。
    leaked = [term for term in INTERNAL_TERMS if term in draft]
    if leaked:
        gaps.append("答复暴露了内部检索过程（" + "、".join(leaked[:2])
                    + "），改为直接给结论或下一步")
    if COURTESY_TAIL.search(draft):
        gaps.append("答复带了无关的结尾客套，删掉")
    if is_howto_question(question) and "目前无法确认" in draft:
        gaps.append("操作指引类问题不应以「目前无法确认」收尾，应给步骤或明确的下一步")
    hedged = bool(UNCONFIRMED_VERDICT.search(draft))
    if hedged and DECISION_MARKERS.search(question) and has_policy_basis(delegations):
        # 材料里既有订单状态又有政策条件，缺的不是证据，是把已知条件推成结论。
        gaps.append("本轮已有可引用依据却以「目前无法确认」收尾：按已知订单状态与政策条件"
                    "给出「现在还不行、满足什么条件后可以」的结论")
    if RETURN_SWAP_QUESTION.search(question) and (is_assertive(draft) or hedged):
        missing = [label for label, pattern in (("退货", RETURN_TERM), ("换货", SWAP_TERM))
                   if not pattern.search(draft)]
        if missing:
            gaps.append("客户问的是退换两件事，答复没有回应"
                        + "、".join(f"「{label}」" for label in missing) + "，需要逐项作答")
    # 消极判定也是判定，不能借 NON_ASSERTIVE 里的「无法」绕过后面的漏项与结论前置检查。
    if not is_assertive(draft) and not hedged:
        return gaps
    decision = bool(DECISION_MARKERS.search(question))
    conclusion = CONCLUSION.search(draft)
    if decision and len(draft) >= _LONG_ANSWER and conclusion is None:
        gaps.append("客户问的是能不能，答复通篇只有政策条款，没有给出可以申请/不可以/目前无法确认的结论")
    asked = [label for label, pattern in _ASK_ITEMS.items() if pattern.search(question)]
    if len(asked) >= 2:
        missing = [label for label in asked if not _ASK_ITEMS[label].search(draft)]
        if missing:
            gaps.append("答复没有回应客户问到的" + "、".join(f"「{label}」" for label in missing)
                        + "，需要逐项回答")
    if (decision and len(draft) >= _LONG_ANSWER * 2 and conclusion is not None
            and conclusion.start() > _CONCLUSION_LEAD):
        gaps.append(f"结论没有前置：{len(draft)} 字答复里，能不能的结论出现在第 "
                    f"{conclusion.start()} 字之后，应放到第一句")
    if (not any(result.get("code") == "ok" for item in delegations
                for result in item["verified_results"])
            and _THIS_ORDER_VERDICT.search(draft)):
        gaps.append("对具体这一笔订单下了能否退款的结论，但本轮没有本人订单核验")
    return gaps


ROLE_ISSUE_TEXT: dict[str, tuple[str, ...]] = {
    "faq": ("政策问题必须委派 FAQ Agent", "FAQ Agent 未调用知识库检索工具"),
    "order": ("个人订单问题必须委派 Order Agent", "Order Agent 未调用受保护的只读订单接口"),
}
"""「这一路要再派一次」的 soft 文案，按角色分组。

图要用它把 Reviewer 的 soft 拆成两类：补派（这里）与重写（其余）。文案因此只能有
一个来源——两处各写一份，改一处就会让补派悄悄失效。第二类（派了但没检索/没核验）
也算补派：compose 重写不出证据，只能让专家自己再查一遍。"""


def refill_roles(soft: list[str]) -> list[str]:
    """Reviewer 指出的、需要重新派发的角色。

    只从 soft 里认，不自己再判一次问句类型：Reviewer 已经处理了「澄清式回答不算断言」
    这类边界，这里再判一遍就会出现两边不一致的分歧。
    """
    return [role for role, texts in ROLE_ISSUE_TEXT.items()
            if any(text in soft for text in texts)]


def rewriting_gaps(soft: list[str]) -> list[str]:
    """与角色无关的形状问题：只能靠重写答复解决，不该触发一次专家重跑。"""
    role_texts = {text for texts in ROLE_ISSUE_TEXT.values() for text in texts}
    return [issue for issue in soft if issue not in role_texts]


UNSUPPORTED_VALUES = "政策或订单数值没有当轮证据支持"
UNVERIFIED_ORDER_FACTS = "订单事实未经本人订单接口核验"
"""两条 hard 的共同点：它们说的都是「这段话缺当轮证据」，而不是「这段话说错了事」。
补派一次缺失的专家，草稿就有材料可依；重写也能把这些数值换成结论。其余 hard 不是
「证据不够」，而是「这句陈述本身不该出现」——重派或重写都不会让它变合规。"""

REFILLABLE_HARD = frozenset({UNSUPPORTED_VALUES, UNVERIFIED_ORDER_FACTS})
"""可以由一次定向补派修复的 hard。清单刻意取白名单而不是黑名单：新增一条 hard 默认
不可补派，宁可多给客户一句安全答复，也不让新规则被一次重试悄悄绕过。"""


def refillable_hard(hard: list[str]) -> bool:
    """这轮 hard 是否只是「缺证据」——只有这种才允许先补派再复审。

    ``hard`` 里同时含一条「声称已执行退款」时返回 False：补派 FAQ 拿回证据，那句
    「已为您提交退款申请」依然是越权陈述，多跑一轮只是把预算烧在同一个结论上。
    """
    return bool(hard) and all(issue in REFILLABLE_HARD for issue in hard)


INTERNAL_PROCESS_ISSUE = "向客户暴露专家或检索流程"
UNCONFIRMED_HANDOFF_ISSUE = "承诺尚未创建的工单或人工接管"
REDRAFTABLE_HARD = frozenset({UNSUPPORTED_VALUES, INTERNAL_PROCESS_ISSUE,
                              UNCONFIRMED_HANDOFF_ISSUE})
"""可以由「只重写、不补派」修复的 hard。

``UNVERIFIED_ORDER_FACTS`` 不在里面：订单事实没核验，重写再多次也变不出核验结果，
那一路必须补派 Order（或直接说无法确认）。数值编造、内部流程措辞和未发生的工单承诺
都可以在材料已到手时重新措辞；只给一次不带旧稿的机会，复审不过仍然拦截。
"""


def redraftable_hard(hard: list[str]) -> bool:
    """这轮 hard 是否只含可用已有材料重新措辞修复的问题。"""
    return bool(hard) and all(issue in REDRAFTABLE_HARD for issue in hard)


def review(question: str, draft: str, delegations: list[Delegation],
           *, customer_order_ids: set[str] | None = None) -> tuple[list[str], list[str]]:
    """Return ``(hard, soft)`` issue lists for one turn."""
    if not draft.strip():
        return [], ["空草稿：本轮没有可校验的回答"]

    hard: list[str] = []
    soft: list[str] = []
    roles = {item["specialist"] for item in delegations}
    faq = [item for item in delegations if item["specialist"] == "faq"]
    order = [item for item in delegations if item["specialist"] == "order"]
    ok_results = [r for item in delegations for r in item["verified_results"] if r.get("code") == "ok"]

    offenders = unbacked_claims(draft, delegations)
    if offenders:
        hard.append("声称已执行" + "、".join(dict.fromkeys(offenders)) + "等未被授权或无核验依据的操作")
    if EXEC_PROMISE.search(draft):
        hard.append("承诺代为执行退款/支付等已关闭的操作，应改为指引客户自助申请或转人工")
    if INTERNAL_LEAK.search(draft):
        hard.append("向客户暴露内部检索片段或编号")
    if INTERNAL_PROCESS.search(draft):
        hard.append(INTERNAL_PROCESS_ISSUE)
    if UNCONFIRMED_HANDOFF.search(draft):
        hard.append(UNCONFIRMED_HANDOFF_ISSUE)
    if not evidence_supported(draft, delegations):
        hard.append(UNSUPPORTED_VALUES)

    quoted = _ids(draft)
    recall_request = bool(re.search(r"(?:之前|先前|刚才|最早|前面|我说|提到).*?(?:订单号|单号)", question))
    restatement = unicodedata.normalize("NFKC", draft)
    restatement = ORDER_ID_IN_TEXT.sub("__ID__", restatement)
    limited_wording = bool(re.fullmatch(
        r"(?:您|你)?(?:先前|之前|最开始|最早|刚才|前面)?(?:在本会话中)?"
        r"(?:提供|说过|提到)的?(?:订单号|单号)(?:是|为)[:：]?\s*__ID__"
        r"[；;，,。 ]*(?:该)?(?:订单)?(?:尚未|未曾|还未|没有|未)核验(?:订单)?归属[。！!]?",
        restatement.strip()))
    only_restatement = (recall_request and len(quoted) == 1 and limited_wording
                        and quoted <= (customer_order_ids or set())
                        )
    if quoted - verified_order_ids(delegations) and not only_restatement:
        hard.append("引用了本轮未经业务接口核验归属的订单号")

    if asserts_order_field(draft) and is_assertive(draft) and not ok_results and not only_restatement:
        hard.append(UNVERIFIED_ORDER_FACTS)
    verified_states = {order_id for result in ok_results
                       for order_id, segment in _order_segments(result.get("text", "")).items()
                       if NOT_SHIPPED.search(segment)}
    claimed_shipped = {order_id for order_id, segment in _order_segments(draft).items()
                       if CLAIM_SHIPPED.search(segment)}
    unscoped_shipped_claim = CLAIM_SHIPPED.search(draft) and not _order_segments(draft)
    if (verified_states & claimed_shipped) or (verified_states and unscoped_shipped_claim):
        hard.append("回答与已核验的订单状态冲突")

    if is_policy_question(question) and "faq" not in roles and is_assertive(draft):
        soft.append(ROLE_ISSUE_TEXT["faq"][0])
    if is_order_question(question) and "order" not in roles and is_assertive(draft) and not only_restatement:
        soft.append(ROLE_ISSUE_TEXT["order"][0])
    if faq and is_policy_question(question) and is_assertive(draft) \
            and not any(item["used_search"] for item in faq):
        soft.append(ROLE_ISSUE_TEXT["faq"][1])
    if order and is_order_question(question) and is_assertive(draft) \
            and not any(item["verified_results"] for item in order):
        soft.append(ROLE_ISSUE_TEXT["order"][1])
    if any(item["failed"] for item in delegations) and is_assertive(draft):
        soft.append("有专职 Agent 执行失败，需补查或转为澄清")
    # 材料齐全但答复不等于回答：逐项结论、漏项、结论前置，都在这里退回重写。
    soft.extend(answer_gaps(question, draft, delegations))
    return hard, soft


def safe_fallback(hard: list[str], soft: list[str], delegations: list[Delegation],
                  question: str) -> str:
    """Never promising anything, say what could not be verified and what to do next."""
    if is_account_question(question) and not is_order_question(question):
        return account_safe_reply(question)
    parts = []
    if is_order_question(question) and any("订单" in issue for issue in hard):
        parts.append("这笔订单的状态我暂时无法可靠核验，请确认订单号或登录客户账号后再问一次。")
    if any("政策" in issue or "数值" in issue for issue in hard) and is_policy_question(question):
        parts.append("相关店铺政策我没有查到可核实的依据，具体以店铺公示为准。")
    if not parts and is_order_question(question):
        parts.append("这笔订单暂时无法可靠核验，请提供订单号或稍后重试。")
    if not parts and is_policy_question(question):
        parts.append("相关店铺政策暂时无法可靠核实，请说明您关心的具体条款。")
    if not parts:
        parts.append("目前无法可靠核实这项信息，请补充更具体的问题。")
    parts.append("需要人工协助时回复「转人工」。")
    return "\n".join(parts)


def uncovered_reply(question: str) -> str:
    """本轮没有可用依据时的确定性话术：给下一步，不解释「我为什么不能」。

    由 ``compose`` 在无材料时直接返回，连一次模型调用都不花。刻意避开
    ``INTERNAL_TERMS`` 里的措辞（不写「没有查到」「本轮……」），否则 Reviewer 会把
    这句自己生成的安全话术重新退回重写，来回空转到预算用尽。
    """
    if is_account_question(question) and not is_order_question(question) \
            and not is_policy_question(question):
        return account_safe_reply(question)
    if is_howto_question(question):
        return ("这个操作的入口当前缺少可核实的店铺说明，为避免指错位置，请回复「转人工」，"
                "由客服直接告诉您具体路径。")
    if is_policy_question(question):
        return ("这条规则当前缺少可核实的条款，具体以店铺公示为准。"
                "需要人工协助时回复「转人工」。")
    return "这个问题当前缺少可核实的信息，请补充更具体的说明；需要人工协助时回复「转人工」。"
