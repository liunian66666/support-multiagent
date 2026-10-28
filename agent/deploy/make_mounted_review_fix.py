# ruff: noqa: RUF001  # 文案含中文全角标点
"""生成"挂载那张图的 Reviewer 补齐内容级规则"的**候选补丁**，并且**不落工作树**。

为什么要有这份产物：报告 §5f 实测，同一份 30 例对抗语料打在
`python_agent/app/multi_agent/graph.py` 自己那份 Reviewer 上，语料原样**漏放 9/25**。
验收标准 5 说"安全无回退"，那这条路上就得有一个**可审查、且被实测过**的修法，
而不是只交一份"这里有洞"的账本。

边界（硬约束，不是保守）：
* 用户明确"不修改 app 部分的代码"。本脚本**从不写 `python_agent/` 下的任何文件**：
  它只把打过补丁的副本写到 `--dest`（临时目录），并生成 diff 工件
  `agent/deploy/mounted_review_fix.patch` 供人工审查。
* 规则不是新发明的：谓词逐条抄自 `agent/guards.py`——那份在 §5d 里跑到 0 漏放 / 0 误拦。
  `agent/` 不在生产镜像里（§5e 实测），app 侧不能 import 它，只能自带一份；
  这正是"两份 Reviewer 实现"这个结构性缺陷的成本，补丁头部把这句话写在了代码里，
  免得后来人以为这是重复劳动而不是被迫重复。

    python agent/deploy/make_mounted_review_fix.py --dest /tmp/mounted_fix
    bash agent/evaluation/run_all.sh mounted-guards-fixed   # 在临时副本上实测前后差
"""

from __future__ import annotations

import argparse
import difflib
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TARGET = Path("app/multi_agent/graph.py")
SERVICE = Path("app/services/four_agent.py")

ANCHOR_CONSTANTS = '_POLICY_NUMBER = re.compile(r"\\d+(?:\\.\\d+)?\\s*(?:元|天|小时|工作日|%)")\n'
ANCHOR_REVIEW_START = "    def review(state: SupportState) -> dict:\n"
ANCHOR_REVIEW_END = "    def should_continue(state: SupportState) -> str:"

ADDED_CONSTANTS = r'''

# --- 内容级规则：谓词抄自 agent/guards.py（§5d 实测 0 漏放 / 0 误拦）---
# agent/ 不在生产镜像里（§5e 实测），app 侧无法 import 它，只能自带一份。
# 收敛方向是抽一个两套实现共用的规则模块，而不是长期维护两份正则。
import unicodedata
from decimal import Decimal, InvalidOperation

EXECUTED_CLAIM = re.compile(
    r"(?:已(?:经)?(?:为您)?|现在已)(退款|退还|打款|支付|扣款|开通|取消订单|发货|退货|换货|"
    r"创建工单|建工单|转接人工|转人工|提交申请|安排上门)")
EXEC_ACTION = re.compile(r"(退款申请|申请退款|退款|退了款|退回款项|退还|打款|款项打回|退回.{0,3}账户"
                         r"|支付|付款|扣款|开通|取消订单|建工单|创建工单|工单|转接人工|转人工"
                         r"|提交.{0,4}申请|申请.{0,4}提交|上门取件|安排上门|改地址|修改地址|变更地址|地址"
                         r"|换货|退货|发货)")
PERFECT = re.compile(r"(已经|已|刚刚|现已|这边已|随后会?已)[^。！？;；]{0,14}?"
                     r"(提交|办理|建好|建了|开好|改好|改完|打回|退回|安排|处理|完成|操作|转接|扣|支付|开通)"
                     r"|(?:把|将)[^。！？;；]{0,10}?(?:改好|改完|办好|弄好|处理好|完成|打回|退回|退了|退了款)"
                     r"|(退了款|建好了|改好了|办好了|打回了|退回去了)")
NEVER_EVIDENCABLE = {"退还", "打款", "支付", "扣款", "开通", "取消订单", "创建工单", "建工单",
                     "转接人工", "转人工", "提交申请", "安排上门",
                     "退款", "退款申请", "申请退款", "退了款", "退回款项", "款项打回", "付款",
                     "上门取件", "改地址", "修改地址", "变更地址", "地址", "工单", "打回"}
EXEC_PROMISE = re.compile(r"(?:帮|给|替)您?[^。！？;；]{0,6}?(?:把|将)?[^。！？;；]{0,6}?"
                          r"(退款|支付|打款|扣款|取消订单|换货|退货|开票)[^。！？;；]{0,8}?"
                          r"(操作|完成|办理|办好|搞定|处理完)")
INTERNAL_LEAK = re.compile(r"\[K\d+\]|知识库片段|检索片段|chunk_id|doc_id|score\s*[:=]")
ORDER_ID_IN_TEXT = re.compile(
    r"(?<![A-Za-z0-9-])(?:ORD-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*|"
    r"(?:TB|JD|PDD|DY|TMALL|1688|SF)[ -]?\d{6,20})(?![A-Za-z0-9-])", re.I)
POLICY_NUMBER = re.compile(r"(\d+(?:\.\d+)?|[零一二两三四五六七八九十百千半]{1,6})\s*"
                           r"(元|天|日|周|年|个工作日|工作日|小时|个月|月|%)")
MONEY_PREFIX = re.compile(r"(?:￥|¥|RMB)\s*(\d+(?:\.\d+)?)")
UNIT_ALIASES = {"个工作日": "工作日", "日": "天", "月": "个月"}
ORDER_FIELD = re.compile(r"(?:状态|金额|价格|物流|发货|签收|下单时间|订单号)\s*[:：]")
NOT_SHIPPED = re.compile(r"未支付|待支付|未发货|待发货|已取消|已退款|退款成功")
CLAIM_SHIPPED = re.compile(r"已发货|已签收|运输中|已揽收|包裹已|快件已|正在派送")
NON_ASSERTIVE = re.compile(r"[?？]|建议|请以|为准|无法|暂不|不确定|请(?:提供|补充|稍后|核对)|"
                           r"未(?:说明|提及)|需要.{0,6}核实")
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
              "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def _is_ok(code: Any) -> bool:
    # 两套实现对同一个业务码用了不同字面量：app 侧 "OK"，agent/ 侧 "ok"。
    return str(code or "").lower() == "ok"


def _to_decimal(value: str):
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _from_chinese(value: str):
    total, current = Decimal(0), Decimal(0)
    for ch in value:
        if ch not in _CN_DIGITS and ch not in _CN_UNITS:
            return None
        if ch in _CN_UNITS:
            total += (current or Decimal(1)) * _CN_UNITS[ch]
            current = Decimal(0)
        else:
            current += Decimal(_CN_DIGITS[ch])
    return total + current


def _numbers_with_unit(text: str) -> set:
    """数值必须与单位成对比较：「7 天」永远不能给「7 元」背书。"""
    found = set()
    for value, unit in POLICY_NUMBER.findall(text):
        number = _to_decimal(value) if value[0].isdigit() else _from_chinese(value)
        if number is not None:
            found.add((number, UNIT_ALIASES.get(unit, unit)))
    for amount in MONEY_PREFIX.findall(text):
        number = _to_decimal(amount)
        if number is not None:
            found.add((number, "元"))
    return found


def _evidence_text(delegations: list) -> str:
    """本轮所有可核文本：专职 Agent 的原话 + 只读接口返回的原文。"""
    parts = [str(item.get("answer") or "") for item in delegations]
    parts += [str(result.get("text") or "") for item in delegations
              for result in (item.get("verified_results") or [])]
    return " ".join(parts)


def _verified_order_ids(delegations: list) -> set:
    ids = set()
    for item in delegations:
        for result in (item.get("verified_results") or []):
            if _is_ok(result.get("code")):
                ids.update(_ids(str(result.get("text") or "")))
        if item.get("specialist") == "order":
            ids.update(_ids(str(item.get("answer") or "")))
    return ids


def _ids(text: str) -> set:
    """全角也要认。「ＴＢ１２３４５６７」和「TB1234567」是同一个单号，
    不归一化就会让跨客户单号从正则底下漏过去（ord-05 实测就是这样）。"""
    return {str(identifier) for identifier in
            ORDER_ID_IN_TEXT.findall(unicodedata.normalize("NFKC", text))}


def _unbacked_claims(text: str, delegations: list) -> list:
    verified = " ".join(str(result.get("text") or "") for item in delegations
                        for result in (item.get("verified_results") or []) if _is_ok(result.get("code")))
    offenders = set(EXECUTED_CLAIM.findall(text))
    for clause in re.split(r"[。！!？?；;，,、\n]", text):
        action = EXEC_ACTION.search(clause)
        if action and PERFECT.search(clause):
            offenders.add(action.group(1))
    return [verb for verb in dict.fromkeys(offenders)
            if verb in NEVER_EVIDENCABLE or verb not in verified]


def _evidence_supported(text: str, delegations: list) -> bool:
    claims = _numbers_with_unit(text)
    if not claims:
        return True
    return claims <= _numbers_with_unit(_evidence_text(delegations))
'''

REVIEW_BODY = r'''    def review(state: SupportState) -> dict:
        question = _last_user(state["messages"])
        delegations = state.get("delegations", [])
        roles = {item.get("specialist") for item in delegations}
        draft = state.get("draft", "")
        ok_results = [result for item in delegations
                      for result in (item.get("verified_results") or []) if _is_ok(result.get("code"))]
        issues = []
        if _POLICY.search(question) and "faq" not in roles:
            issues.append("政策问题必须委派 FAQ Agent")
        if _ORDER.search(question) and "order" not in roles:
            issues.append("个人订单问题必须委派 Order Agent")
        if _ORDER.search(question) and "order" in roles and not any(
            item.get("verified_results") for item in delegations
            if item.get("specialist") == "order"
        ):
            issues.append("订单 Agent 未调用受保护的只读订单工具")
        if _UNSUPPORTED_ACTION.search(draft):
            issues.append("不得声称已经执行退款、支付、退货、建工单或转人工")
        if _POLICY_NUMBER.search(draft) and _POLICY.search(question):
            if not any(item.get("evidence_ids") for item in delegations
                       if item.get("specialist") == "faq"):
                issues.append("政策数值没有当轮知识库证据")
        if _POLICY.search(question) and "faq" in roles and not any(
            item.get("used_search") for item in delegations
            if item.get("specialist") == "faq"
        ):
            issues.append("FAQ Agent 未调用知识库检索工具")
        if _ORDER.search(question) and re.search(r"状态[:：]|金额[:：]", draft):
            if not any(result.get("code") == "OK" for item in delegations
                       if item.get("specialist") == "order"
                       for result in item.get("verified_results", [])):
                issues.append("订单事实没有通过本人订单接口核验")

        # --- §5f 实测缺口的补法：原实现只看"证据字段非空"，从不比对草稿内容 ---
        offenders = _unbacked_claims(draft, delegations)
        if offenders:
            issues.append("声称已执行" + "、".join(offenders) + "等未被授权或无核验依据的操作")
        if EXEC_PROMISE.search(draft):
            issues.append("承诺代为执行退款/支付等已关闭的操作")
        if INTERNAL_LEAK.search(draft):
            issues.append("向客户暴露内部检索片段或编号")
        if not _evidence_supported(draft, delegations):
            issues.append("政策或订单数值没有当轮证据支持")
        quoted = _ids(draft)
        if quoted - _verified_order_ids(delegations):
            issues.append("引用了本轮未经业务接口核验归属的订单号")
        if ORDER_FIELD.search(draft) and not ok_results:
            issues.append("订单事实未经本人订单接口核验")
        if any(NOT_SHIPPED.search(str(result.get("text") or "")) for result in ok_results) \
                and CLAIM_SHIPPED.search(draft):
            issues.append("回答与已核验的订单状态冲突")
        feedback = "；".join(issues)
        trace.record("review", "result", passed=not bool(feedback), reasons=issues)
        return {"review_feedback": feedback}


'''


def patch_source(source: str) -> str:
    """Anchored replacements; a missing anchor raises instead of half-patching."""
    if ANCHOR_CONSTANTS not in source:
        raise SystemExit(f"锚点缺失（常量块）：{TARGET} 的 _POLICY_NUMBER 定义行已改动，"
                         "需要人工核对补丁是否还适用")
    start, end = source.find(ANCHOR_REVIEW_START), source.find(ANCHOR_REVIEW_END)
    if start < 0 or end < 0 or end < start:
        raise SystemExit(f"锚点缺失（review 节点）：{TARGET} 的 review/should_continue 边界已改动")
    out = source.replace(ANCHOR_CONSTANTS, ANCHOR_CONSTANTS + ADDED_CONSTANTS, 1)
    start = out.find(ANCHOR_REVIEW_START)
    end = out.find(ANCHOR_REVIEW_END)
    return out[:start] + REVIEW_BODY + out[end:]


# --- 第二项：§5e 的"重放无幂等键"（M2）------------------------------------------------
# 范围要说清楚：这补的是 run_turn 侧的**能力**。入口契约（ChatRequest 的 turn_id 字段、
# 前端/代理要不要带它）是另一个决定，没带就还是今天的行为——补丁不改变默认路径的语义。
SERVICE_OLD = '''async def run_turn(app, tenant_id: str, thread_id: str, user_message: str) -> dict:'''

SERVICE_NEW = '''async def run_turn(app, tenant_id: str, thread_id: str, user_message: str,
                 turn_id: str = "") -> dict:'''

SERVICE_BLOCK_OLD = '''        old = await graph.aget_state(config)
        incoming = [HumanMessage(content=user_message)]'''

SERVICE_BLOCK_NEW = '''        old = await graph.aget_state(config)
        previous = old.values or {}
        # 只有"同一 turn_id 且上一轮已经答完"才算重放；中途失败的轮次要重跑，
        # 否则会把半成品状态当成答复返回给客户（这是 §5e 那条发现里唯一的小心处）。
        if turn_id and previous.get("turn_id") == turn_id and previous.get("answer"):
            trace.record("memory", "replay_skipped", turn_id=turn_id)
            return dict(previous)
        # 人消息带确定性 id：重跑时 add_messages 按 id 替换，转写不会多出一条。
        incoming = [HumanMessage(content=user_message,
                                 id=f"{turn_id}:user" if turn_id else None)]'''

SERVICE_INVOKE_OLD = '''        result = await graph.ainvoke({"messages": incoming}, config=config)'''

SERVICE_INVOKE_NEW = '''        result = await graph.ainvoke({"messages": incoming, "turn_id": turn_id}, config=config)'''

GRAPH_STATE_OLD = '''    attempts: int
    delegations: list[dict]'''

GRAPH_STATE_NEW = '''    attempts: int
    turn_id: str
    delegations: list[dict]'''

GRAPH_FINALIZE_OLD = '''        return {"answer": answer, "messages": [AIMessage(content=answer)]}'''

GRAPH_FINALIZE_NEW = '''        # 答复也带确定性 id（与 agent/ 的 answer_id 同构）：同一 turn_id 重跑时
        # add_messages 是替换而不是追加，历史里不会出现两条一样的助手消息。
        return {"answer": answer,
                "messages": [AIMessage(content=answer,
                                       id=(f"{state.get('turn_id')}:assistant"
                                           if state.get("turn_id") else None))]}'''


def patch_persistence(service: str, graph: str) -> tuple[str, str]:
    for anchor, name in ((SERVICE_OLD, "run_turn 签名"), (SERVICE_BLOCK_OLD, "aget_state 段"),
                         (SERVICE_INVOKE_OLD, "ainvoke 调用"), (GRAPH_STATE_OLD, "状态通道"),
                         (GRAPH_FINALIZE_OLD, "finalize 返回")):
        haystack = graph if name in ("状态通道", "finalize 返回") else service
        if anchor not in haystack:
            raise SystemExit(f"锚点缺失（持久化补丁 · {name}）：app 侧代码已改动，补丁需要人工重写")
    service = service.replace(SERVICE_OLD, SERVICE_NEW, 1)
    service = service.replace(SERVICE_BLOCK_OLD, SERVICE_BLOCK_NEW, 1)
    service = service.replace(SERVICE_INVOKE_OLD, SERVICE_INVOKE_NEW, 1)
    graph = graph.replace(GRAPH_STATE_OLD, GRAPH_STATE_NEW, 1)
    graph = graph.replace(GRAPH_FINALIZE_OLD, GRAPH_FINALIZE_NEW, 1)
    return service, graph


# --- 第三项：§5e 的"失败轮留下没人回答的人消息，且原因不可见"（M5b）--------------------
# 参照实现是 agent/ 那条路：模型故障时仍然 finalize（§5 那 20 项里"降级不崩不空白"就是它）。
# 所以修法不是"把这条问句回滚掉"，而是"给它配一个安全答复 + 把原因写进检查点与 trace"，
# 然后把异常**原样抛出**——chat.py 的降级分支照旧工作，行为契约不变。
RECOVERY_SAFE_ANSWER = "目前无法可靠核实这项信息。请稍后再试，或回复「转人工」由客服协助。"

RECOVERY_INVOKE_VARIANTS = (
    '''        result = await graph.ainvoke({"messages": incoming, "turn_id": turn_id}, config=config)''',
    '''        result = await graph.ainvoke({"messages": incoming}, config=config)''',
)

RECOVERY_WRAP = '''        try:
            {invoke}
        except Exception as exc:
            # 失败轮也要"有人回答"：把安全答复与原因写进同一线程的检查点，
            # 这样历史里不会出现一条悬空的客户问句；随后原样抛出，交给 chat.py 的降级分支。
            detail = f"{{type(exc).__name__}}: {{exc}}"[:200]
            await graph.aupdate_state(config, {{"answer": SAFE_FAILURE_ANSWER,
                                               "last_error": detail,
                                               "messages": [AIMessage(content=SAFE_FAILURE_ANSWER)]}})
            trace.record("error", "model_failure", detail=detail)
            raise'''

RECOVERY_CONSTANT = '''SAFE_FAILURE_ANSWER = "目前无法可靠核实这项信息。请稍后再试，或回复「转人工」由客服协助。"


'''

GRAPH_STATE_LAST_ERROR_OLD = '''    attempts: int
    turn_id: str
    delegations: list[dict]'''

GRAPH_STATE_LAST_ERROR_NEW = '''    attempts: int
    turn_id: str
    last_error: str
    delegations: list[dict]'''

GRAPH_STATE_PLAIN_OLD = '''    attempts: int
    delegations: list[dict]'''

GRAPH_STATE_PLAIN_NEW = '''    attempts: int
    last_error: str
    delegations: list[dict]'''


def patch_recovery(service: str, graph: str) -> tuple[str, str]:
    """包裹 ainvoke。必须在 patch_persistence 之后调用：锚点允许是"已带 turn_id"的那一版。"""
    hit = next((line for line in RECOVERY_INVOKE_VARIANTS if line in service), None)
    if hit is None:
        raise SystemExit("锚点缺失（失败恢复补丁 · ainvoke 调用）：app 侧代码已改动，补丁需要人工重写")
    state_old, state_new = ((GRAPH_STATE_LAST_ERROR_OLD, GRAPH_STATE_LAST_ERROR_NEW)
                            if GRAPH_STATE_LAST_ERROR_OLD in graph
                            else (GRAPH_STATE_PLAIN_OLD, GRAPH_STATE_PLAIN_NEW))
    if state_old not in graph:
        raise SystemExit("锚点缺失（失败恢复补丁 · 状态通道）：app 侧代码已改动，补丁需要人工重写")
    service = service.replace(hit, RECOVERY_WRAP.format(invoke=hit.strip()), 1)
    service = service.replace("async def run_turn(", RECOVERY_CONSTANT + "async def run_turn(", 1)
    graph = graph.replace(state_old, state_new, 1)
    return service, graph


def _diff(before: str, after: str, rel: Path) -> str:    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}"))


def build(dest: str, scope: str) -> tuple[Path, list[Path]]:
    """Stage patched copies of the packages the probes import; never touch the repo."""
    base = Path(dest)
    stage = base / "repo"
    for name in ("python_agent", "agent"):
        src, dst = REPO / name, stage / name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                                "node_modules", ".venv"))
    graph_path = stage / "python_agent" / TARGET
    service_path = stage / "python_agent" / SERVICE
    graph_original = graph_path.read_text(encoding="utf-8")
    service_original = service_path.read_text(encoding="utf-8")
    written: list[Path] = []

    graph, service = graph_original, service_original
    if scope in {"guards", "all"}:
        patched = patch_source(graph_original)
        guards_patch = REPO / "agent" / "deploy" / "mounted_review_fix.patch"
        guards_patch.write_text(_diff(graph_original, patched, TARGET), encoding="utf-8")
        written.append(guards_patch)
        graph = patched

    if scope in {"persistence", "all"}:
        before_graph, before_service = graph, service
        service, graph = patch_persistence(before_service, before_graph)
        persistence_patch = REPO / "agent" / "deploy" / "mounted_persistence_fix.patch"
        persistence_patch.write_text(_diff(before_service, service, SERVICE)
                                      + _diff(before_graph, graph, TARGET), encoding="utf-8")
        written.append(persistence_patch)

    if scope in {"recovery", "all"}:
        before_graph, before_service = graph, service
        service, graph = patch_recovery(before_service, before_graph)
        recovery_patch = REPO / "agent" / "deploy" / "mounted_recovery_fix.patch"
        recovery_patch.write_text(_diff(before_service, service, SERVICE)
                                  + _diff(before_graph, graph, TARGET), encoding="utf-8")
        written.append(recovery_patch)

    service_path.write_text(service, encoding="utf-8")
    graph_path.write_text(graph, encoding="utf-8")
    return stage, written


def main() -> int:
    parser = argparse.ArgumentParser(description="stage (never apply) the mounted-path fixes")
    parser.add_argument("--dest", required=True, help="staging root (a temp dir)")
    parser.add_argument("--scope", choices=["guards", "persistence", "recovery", "all"], default="all",
                        help="guards = §5f 的 Reviewer 内容规则；persistence = §5e M2 的重放幂等键；"
                             "recovery = §5e M5b 的失败轮收尾；all = 三份一起")
    args = parser.parse_args()
    stage, written = build(args.dest, args.scope)
    print(f"补丁副本 → {stage}   （工作树未被修改：python_agent/ 下一个字节都没动）")
    for path in written:
        text = path.read_text(encoding="utf-8")
        added = sum(1 for line in text.splitlines() if line.startswith("+"))
        removed = sum(1 for line in text.splitlines() if line.startswith("-"))
        print(f"可审查 diff → {path.name}（+{added} / -{removed}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
