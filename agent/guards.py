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
    r"退货|退款政策|退款规则|换货|包邮|运费|邮费|发票|开票|售后|保修|质保|理赔|保障|"
    r"支付方式|货到付款|分期|到账|时效|期限|多久|几天|手续费|押金|定金|起订|门槛")
ORDER_MARKERS = re.compile(
    r"(?:ORD-[A-Za-z0-9-]+|TB\d{6,}|JD\d{6,}|PDD\d{6,}|DY\d{6,}|SF\d{6,})|"
    r"我的订单|查(?:一下|看|询|下)?(?:我的)?订单|订单(?:号|状态|进度|信息)|"
    r"物流|发货|签收|快递到哪|什么时候到", re.I)

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
ORDER_FIELD = re.compile(r"(?:状态|金额|价格|物流|发货|签收|下单时间|订单号)\s*[:：]")
# Verified state that contradicts a "already on its way" style promise.
NOT_SHIPPED = re.compile(r"未支付|待支付|未发货|待发货|已取消|已退款|退款成功")
CLAIM_SHIPPED = re.compile(r"已发货|已签收|运输中|已揽收|包裹已|快件已|正在派送")
# A draft that only asks or hedges has not asserted anything, so soft rules skip it.
NON_ASSERTIVE = re.compile(r"[?？]|建议|请以|为准|无法|暂不|不确定|请(?:提供|补充|稍后|核对)|"
                           r"未(?:说明|提及)|需要.{0,6}核实")

INTERNAL_LEAK = re.compile(r"\[K\d+\]|知识库片段|检索片段|chunk_id|doc_id|score\s*[:=]")


_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def is_policy_question(text: str) -> bool:
    return bool(POLICY_MARKERS.search(text))


def is_order_question(text: str) -> bool:
    return bool(ORDER_MARKERS.search(text))


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
    if not evidence_supported(draft, delegations):
        hard.append("政策或订单数值没有当轮证据支持")

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

    if ORDER_FIELD.search(draft) and is_assertive(draft) and not ok_results and not only_restatement:
        hard.append("订单事实未经本人订单接口核验")
    verified_states = {order_id for result in ok_results
                       for order_id, segment in _order_segments(result.get("text", "")).items()
                       if NOT_SHIPPED.search(segment)}
    claimed_shipped = {order_id for order_id, segment in _order_segments(draft).items()
                       if CLAIM_SHIPPED.search(segment)}
    unscoped_shipped_claim = CLAIM_SHIPPED.search(draft) and not _order_segments(draft)
    if (verified_states & claimed_shipped) or (verified_states and unscoped_shipped_claim):
        hard.append("回答与已核验的订单状态冲突")

    if is_policy_question(question) and "faq" not in roles and is_assertive(draft):
        soft.append("政策问题必须委派 FAQ Agent")
    if is_order_question(question) and "order" not in roles and is_assertive(draft) and not only_restatement:
        soft.append("个人订单问题必须委派 Order Agent")
    if faq and is_policy_question(question) and is_assertive(draft) \
            and not any(item["used_search"] for item in faq):
        soft.append("FAQ Agent 未调用知识库检索工具")
    if order and is_order_question(question) and is_assertive(draft) \
            and not any(item["verified_results"] for item in order):
        soft.append("Order Agent 未调用受保护的只读订单接口")
    if any(item["failed"] for item in delegations) and is_assertive(draft):
        soft.append("有专职 Agent 执行失败，需补查或转为澄清")
    return hard, soft


def safe_fallback(hard: list[str], soft: list[str], delegations: list[Delegation],
                  question: str) -> str:
    """Never promising anything, say what could not be verified and what to do next."""
    parts = []
    if any("订单" in issue for issue in hard):
        parts.append("这笔订单的状态我暂时无法可靠核验，请确认订单号或登录客户账号后再问一次。")
    if any("政策" in issue or "数值" in issue for issue in hard) and is_policy_question(question):
        parts.append("相关店铺政策我没有查到可核实的依据，具体以店铺公示为准。")
    if not parts:
        parts.append("目前无法可靠核实这项信息，请补充更具体的问题。")
    parts.append("需要人工协助时回复「转人工」。")
    return "\n".join(parts)
