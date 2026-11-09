# 中文文案必然包含全角标点
"""The parent LangGraph: one durable turn of four-agent collaboration.

拓扑是显式的：``route`` 只决定「派给谁、每一路问什么」，条件边把每个角色当独立节点
扇出执行，``compose`` 负责把材料写成客户答复，``review`` 用确定性规则决定补派、重写
还是收尾。每一路单独超时、单独失败、单独观测；重试只补 Reviewer 指出的那一路，
不重跑已经核过的角色。

（这里曾并存过一版 ``tools`` 引擎：Supervisor 是一个 ``create_agent`` 循环，三个专家以
工具形式挂在它下面。2026-11-09 移除——同一轮里模型既选工具又写答复，每 pass 多一次
模型往返，且工具结果以 JSON 回灌进第二次调用。）

Bounds, from the inside out: middleware caps each agent's model/tool calls, the
node-level ``attempts`` cap bounds the review-rewrite loop, and ``recursion_limit``
caps graph steps. Every bound is observable in ``ledger`` and ``usage``.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Overwrite

from . import guards, memory
from .agents import (
    build_specialists,
    collect_usage,
    compose_reply,
    run_specialist,
)
from .deps import SupportDeps
from .plan import ROLES, plan_turn
from .state import Delegation, SupportState, TurnRecord

DEFAULT_MAX_PASSES = 3
"""Review rounds per turn: one plan plus two evidence-backed retries."""

TURN_STEP_BUDGET = 48
"""Steps the whole turn may take — every caller must use this number, tests included.

LangGraph charges a subgraph's steps against the *parent's* remaining budget, so the parent
has to be able to hand a pass's budget over more than once: ``max_passes`` rounds plus the
outer nodes. The value shipped before this measurement was 24, which is why a compound
question could exhaust the turn on its second retry.

The fan-out charges each specialist's inner steps here, so this number is *not* lowered
without re-running the same bisect.
"""

SPECIALIST_TIMEOUT = 30.0
ROUTER_TIMEOUT = 20.0

PROGRESS_NODES = frozenset({"open_turn", "precheck", "route", "faq_node", "order_node",
                            "general_node", "compose", "review", "finalize"})
"""节点名 → 前端可显示的阶段。``service`` 只允许这一组名字泄漏出去，且只有名字。"""


def _question(state: SupportState) -> str:
    turn_id = state.get("turn_id", "")
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage) and (not turn_id or message.id == f"{turn_id}:u"):
            return memory.message_text(message)
    return memory.message_text(state["messages"][-1]) if state.get("messages") else ""


def _roles(delegations: list[Delegation]) -> list[str]:
    return list(dict.fromkeys(item["specialist"] for item in delegations))


def _may_passthrough(question: str, finding: Delegation) -> bool:
    """单角色透传的准入：只有「订单状态确已被只读接口核到」和「寒暄」两档。

    FAQ 一律不许透传。专家原文是资料条款，而客户问「能不能退」时要的是
    「可以申请 / 不可以 / 目前无法确认」这句结论——这条规则只住在 ``COMPOSE_PROMPT`` 里，
    透传等于绕开它；而 Reviewer 只看形状，一段短的政策复述可以蒙过去（长篇才会被
    ``answer_gaps`` 抓）。所以透传省下的那一次模型调用，不能拿汇总规则去换。

    ``general`` 也只对寒暄放行：需要澄清的问题必须让 compose 按材料重写，否则一句
    「您能再说明一下吗」会被原样发给客户。
    """
    if finding["failed"]:
        return False
    role = finding["specialist"]
    if role == "order":
        # 只有真的调到了受保护的只读接口才算「订单事实已核验」。
        return bool(finding["verified_results"]) and guards.is_order_question(question)
    if role == "general":
        return guards.is_greeting(question)
    return False


def _has_usable_material(delegations: list[Delegation], turn_id: str) -> bool:
    """本轮是否已经拿到可复用的材料：FAQ 检索到的证据，或订单接口的核验结果。

    这是「只重写、不补派」的前置条件。没有材料时重写只能让模型换个说法，数值照样是
    编的——那种情况该补派（缺角色），或者干脆承认无法确认，而不是再花一次模型调用。
    """
    return any(item["turn_id"] == turn_id and not item["failed"]
               and item["specialist"] in {"faq", "order"}
               and bool(item["evidence"] or item["verified_results"])
               for item in delegations)


async def build_support_graph(deps: SupportDeps, *, checkpointer: Any = None,
                              max_passes: int = DEFAULT_MAX_PASSES, context_window: int = 12,
                              compact_trigger: int = memory.DEFAULT_TRIGGER,
                              compact_keep: int = memory.DEFAULT_KEEP,
                              compact_repair: bool = True,
                              timeout_seconds: float = 60.0,
                              max_delegate_calls: int = 4,
                              recursion_limit: int = 12):
    """Compile one tenant-scoped graph. Only this outer graph gets a checkpointer."""
    if max_passes < 1:
        raise ValueError("max_passes must be at least 1")

    specialists, model = await build_specialists(deps, max_tool_calls=max_delegate_calls)

    async def open_turn(state: SupportState) -> dict[str, Any]:
        """Clear the previous turn's scratch channels; history is untouched.

        ``delegations`` must be cleared too, and via ``Overwrite`` because every one of
        these channels has an appending/merging reducer — returning an empty list would
        merge into the old value. Without the reset the Reviewer would happily treat an
        earlier turn's retrieval as this turn's evidence, which is exactly the
        "no evidence, no promise" boundary buying off with stale receipts.

        ``plan`` / ``missing_roles`` / ``review_gaps`` are plain last-value channels, but
        they are reset here for the same reason: a previous turn's plan must never steer
        this turn's fan-out.
        """
        turn_id = state.get("turn_id", "")
        # 只清本轮：账本随租户图共享，整本清空会抹掉另一个会话正在进行的派发窗口。
        deps.evidence.clear(turn_id)
        return {"draft": "", "answer": "", "review_feedback": "", "last_error": "",
                "review_issues": Overwrite([]), "delegations": Overwrite([]),
                "review_hard": False, "attempts": 0, "outcome": "",
                "plan": [], "missing_roles": [], "review_gaps": [],
                "redraft": False, "redraft_used": False,
                # Only *this* turn's usage entry is dropped: passes add to it, so a replay of
                # the same turn recomputes the same total instead of stacking a second copy.
                "usage": Overwrite({k: v for k, v in (state.get("usage") or {}).items()
                                    if k != state.get("turn_id", "")}),
                "turn_id": state.get("turn_id", "")}

    def precheck(state: SupportState) -> dict[str, Any]:
        """确定性短路：账户问题与越界请求不进入任何模型调用。

        这两条以前分别住在 Supervisor 的 Python 分支和 agent 内的 ``before_model`` 闸里；
        现在提到图入口，是为了让「不调模型也能答」这件事在拓扑上就能看见——测试里
        ``model.recorded == []`` 断言的就是它。
        """
        question = _question(state)
        if (guards.is_account_question(question) and not guards.is_order_question(question)
                and not guards.is_policy_question(question)):
            deps.trace("precheck", "account")
            return {"draft": guards.account_safe_reply(question)}
        if guards.is_blocked_question(question):
            deps.trace("precheck", "blocked")
            return {"draft": guards.BLOCKED_REPLY}
        return {}

    def after_precheck(state: SupportState) -> str:
        return "review" if state.get("draft") else "route"

    async def route(state: SupportState) -> dict[str, Any]:
        """模型只做路由：派给谁、每一路问什么。

        补派不走模型：Reviewer 已经指明缺哪一路，再规划一次既多一次往返，也可能换出
        另一套角色集，把已经核过的 finding 白白丢掉。已核过的角色因此不会被重跑。
        """
        turn_id = state.get("turn_id", "")
        question = _question(state)
        refill = [role for role in (state.get("missing_roles") or []) if role in ROLES]
        if refill:
            deps.trace("route", "refill", roles=refill, attempt=state.get("attempts", 0))
            return {"plan": [{"role": role, "question": question} for role in refill],
                    "draft": "", "missing_roles": []}
        if model is None:  # pragma: no cover  只在装配错误时可达
            return {"plan": [], "draft": "", "last_error": "router model missing"}

        prompt = memory.model_context(state, window=context_window)
        try:
            experiences = await deps.recall_experience(question)
        except Exception:
            experiences = []  # Optional guidance cannot block the checked answer.
        if experiences:
            from app.services.experience_recall import augment_message
            for index in range(len(prompt) - 1, -1, -1):
                if isinstance(prompt[index], HumanMessage):
                    prompt = [*prompt[:index],
                              HumanMessage(content=augment_message(
                                  memory.message_text(prompt[index]), experiences)),
                              *prompt[index + 1:]]
                    break
        if state.get("review_feedback"):
            prompt = [*prompt, HumanMessage(content=f"【校验反馈】{state['review_feedback']}")]

        decision, used, error = await plan_turn(
            model, prompt, question=question, timeout=min(timeout_seconds, ROUTER_TIMEOUT))
        if error:
            deps.trace("route", "failed", error=error, attempt=state.get("attempts", 0))
            return {"plan": [], "draft": "", "last_error": error}
        deps.trace("route", "planned", roles=decision.roles, dropped=decision.dropped,
                   direct=bool(decision.direct), attempt=state.get("attempts", 0))
        update: dict[str, Any] = {"plan": decision.items, "draft": decision.direct,
                                  "last_error": ""}
        usage = collect_usage(used)
        if any(usage.values()):
            update["usage"] = {turn_id: usage}
        return update

    def fan_out(state: SupportState) -> list[str]:
        """条件边返回角色子集即并行扇出。

        不用 ``Send``：三个角色固定、每个只有一份子问题，静态节点名 + 列表返回值就够；
        ``Send`` 只在需要给每个分支不同 payload 或同角色多实例时才必要，而它会额外要求
        汇合节点处理 ``defer``。空计划走 ``compose``，绝不留一条没有目的地的边。
        """
        roles = [item["role"] for item in (state.get("plan") or []) if item["role"] in ROLES]
        unique = list(dict.fromkeys(roles))
        return [f"{role}_node" for role in unique] if unique else ["compose"]

    def make_specialist(role: str):
        async def node(state: SupportState) -> dict[str, Any]:
            turn_id = state.get("turn_id", "")
            question = _question(state)
            sub = next((item["question"] for item in (state.get("plan") or [])
                        if item["role"] == role), question)
            finding, usage = await run_specialist(
                deps, role, sub, specialists[role], turn_id=turn_id,
                timeout=min(timeout_seconds, SPECIALIST_TIMEOUT),
                recursion_limit=recursion_limit)
            update: dict[str, Any] = {"delegations": [finding]}
            if any(usage.values()):
                update["usage"] = {turn_id: usage}
            return update
        return node

    async def compose(state: SupportState) -> dict[str, Any]:
        """把材料写成答复。

        只有 ``_may_passthrough`` 认可的那两档（订单状态已核验、寒暄）才不调模型；
        FAQ 单角色一律过 compose，因为「先给结论、少列字段、不承诺」这些规则只在
        ``COMPOSE_PROMPT`` 里，透传就是把它们丢掉。

        ``redraft`` 置位时（Reviewer 判定材料已在手、草稿措辞不合规）重写**不带旧稿**：
        否则模型可能照抄错误数值或虚构的工单承诺。
        """
        turn_id = state.get("turn_id", "")
        question = _question(state)
        feedback = state.get("review_feedback", "")
        redraft = bool(state.get("redraft"))
        clear_redraft: dict[str, Any] = {"redraft": False} if redraft else {}
        own = [item for item in (state.get("delegations") or [])
               if item["turn_id"] == turn_id]
        if state.get("draft") and not feedback:
            return clear_redraft  # 入口或路由已经给出定稿，无需重写
        if not own and not feedback:
            return {**clear_redraft, "draft": state.get("draft", "")}
        if not own and not state.get("draft"):
            # 没有任何材料可写：宁可交白卷走安全话术，也不让模型凭空补一段。
            return {**clear_redraft, "draft": ""}
        if (guards.is_howto_question(question) and not feedback
                and not _has_usable_material(own, turn_id)):
            # 操作指引型问题在「无依据」时最容易被逼出两段编造：先说「目前无法确认」，
            # 再猜一个菜单路径（线上实例：账单查询被答成「建议登录商城后在个人中心查看」）。
            # 有材料时照常撰写，没材料就不调模型，直接给下一步。只对这一档短路：
            # 判定型问题仍走原来的 Reviewer → 安全话术路径，别改变既有收敛语义。
            deps.trace("compose", "uncovered", roles=_roles(own))
            return {**clear_redraft, "draft": guards.uncovered_reply(question)}
        if len(own) == 1 and not feedback and _may_passthrough(question, own[0]):
            deps.trace("compose", "passthrough", role=own[0]["specialist"])
            return clear_redraft | {"draft": own[0]["answer"]}
        try:
            draft, usage = await compose_reply(
                model, question=question, findings=own, feedback=feedback,
                prior="" if redraft else state.get("draft", ""),
                timeout=min(timeout_seconds, SPECIALIST_TIMEOUT))
        except Exception as exc:  # noqa: BLE001  撰写失败要降级，不能吞掉原因
            error = f"{type(exc).__name__}: {str(exc)[:180]}"
            deps.trace("compose", "failed", error=error)
            return {**clear_redraft, "last_error": error}
        deps.trace("compose", "redrafted" if redraft else "rewritten", roles=_roles(own),
                   chars=len(draft), attempt=state.get("attempts", 0))
        return {**clear_redraft, "draft": draft, "usage": {turn_id: usage}}

    def make_review():
        def node(state: SupportState) -> dict[str, Any]:
            """The Reviewer: deterministic rules, no model call, not a fifth agent."""
            customer_order_ids = set().union(*(
                guards._ids(memory.message_text(message))
                for message in state.get("messages", []) if isinstance(message, HumanMessage)))
            hard, soft = guards.review(_question(state), state.get("draft", ""),
                                       state.get("delegations", []),
                                       customer_order_ids=customer_order_ids)
            issues = [*hard, *soft]
            deps.trace("review", "result", passed=not issues, hard=hard[:4], reasons=soft[:4],
                       attempts=state.get("attempts", 0))
            update: dict[str, Any] = {"review_feedback": "；".join(issues),
                                      "review_issues": issues, "review_hard": bool(hard)}
            # ``attempts`` 是「Reviewer 轮次」：判定一次算一次，
            # 因为它同时决定补派（route）与重写（compose）能不能再来一轮。
            update["attempts"] = state.get("attempts", 0) + 1
            # 只有「缺当轮证据」这一类 hard 可以由补派修复，且必须真有缺失角色；
            # 越权声明、暴露内部编号、引用未核验订单号一律不填 missing_roles——
            # 那样 should_end 会直接收尾走安全答复，不会靠一轮重试把硬规则绕过去。
            refill = guards.refill_roles(soft)
            if hard and not guards.refillable_hard(hard):
                refill = []
            update["missing_roles"] = refill
            gaps = guards.rewriting_gaps(soft)
            if not (state.get("draft") or "").strip() \
                    and not _has_usable_material(state.get("delegations") or [],
                                                 state.get("turn_id", "")):
                # 「空草稿」只有在**有材料可写**时才值得重写一次。路由连续失败
                # （模型超时/限流）时既无草稿也无 finding，重写只会把同一份空稿
                # 再交一遍，白白空转到预算用尽；这里直接交给 finalize 说安全话术。
                gaps = []
            update["review_gaps"] = gaps
            # 三种处理，按可修复性从强到弱：缺角色补派；材料已在手、草稿含可修正
            # 的数值或措辞问题则重写一次；其余 hard 由 should_end 收尾。
            update["redraft"] = bool(
                hard and not refill and not state.get("redraft_used")
                and guards.redraftable_hard(hard)
                and _has_usable_material(state.get("delegations") or [],
                                         state.get("turn_id", "")))
            if update["redraft"]:
                update["redraft_used"] = True
            return update
        return node

    def should_end(state: SupportState) -> str:
        """显式有界边：补派、重写、收尾三选一。

        顺序是有意的：**预算先判死**。补派与重写都属于重试，重试次数用尽时由这一次
        Reviewer 的结论决定能不能发出，不允许「还有角色没派」再借一轮把硬检查绕过去。

        过了预算这关之后才是补派优先：``missing_roles`` 只在两种情况下非空——没有 hard，
        或 hard 全都是「缺当轮证据」；后者的草稿已经被 ``route`` 清掉，补派拿回证据后由
        ``compose`` 依据新材料重写，再走一次 ``review``。

        再往后是这条窄路：hard 只含可重写的问题、材料已在手、又还没有重写过，
        就给一次「不看旧稿」的重写；``redraft`` 由 Reviewer 置位且每轮只置一次，所以
        第二次仍不合规必然落到 ``finalize``，不会靠换措辞拖满预算。其余 hard 说的是
        「这段陈述本身不该出现」，重派或重写都不会让它变合规，直接收尾说安全话术。
        """
        if state.get("attempts", 0) >= max_passes:
            return "finalize"
        if state.get("missing_roles"):
            return "route"
        if state.get("review_hard"):
            return "compose" if state.get("redraft") else "finalize"
        if state.get("review_gaps"):
            return "compose"
        return "finalize"

    def finalize(state: SupportState) -> dict[str, Any]:
        turn_id = state.get("turn_id", "")
        delegations = state.get("delegations", [])
        issues = state.get("review_issues", [])
        # Wording keys off the last pass; the ledger keeps everything found along the way.
        latest = [item for item in state.get("review_feedback", "").split("；") if item]
        if state.get("review_hard"):
            answer = guards.safe_fallback(latest, [], delegations, _question(state))
            outcome = "review_blocked"
        elif not state.get("draft"):
            # The customer always gets a sentence: an empty answer here would surface
            # as a blank bubble in the chat UI.
            answer = guards.safe_fallback([], [], delegations, _question(state))
            outcome = "no_result"
        elif state.get("review_feedback"):
            answer, outcome = state["draft"], "answered_unchecked"
        else:
            answer, outcome = state["draft"], "answered"
        answer = guards.polish_customer_reply(answer, _question(state), delegations)
        deps.trace("finish", outcome, roles=_roles(delegations), attempts=state.get("attempts", 0),
                   answer_chars=len(answer))
        record: TurnRecord = {"turn_id": turn_id, "question": _question(state)[:500],
                              "answer": answer[:2000], "outcome": outcome, "roles": _roles(delegations),
                              "evidence_ids": [item["id"] for d in delegations for item in d["evidence"]][:12],
                              "review_issues": issues[:8], "attempts": state.get("attempts", 0)}
        updates: dict[str, Any] = {"answer": answer, "outcome": outcome, "ledger": [record]}
        if answer:
            updates["messages"] = [AIMessage(content=answer, id=f"{turn_id}:a")]
        return updates

    async def compact(state: SupportState) -> dict[str, Any]:
        """Fold the oldest messages above the watermark into the rolling summary."""
        try:
            summary_model = await deps.model()
            update = await memory.compact(state, model=summary_model, trigger=compact_trigger,
                                          keep=compact_keep, repair=compact_repair)
        except Exception:
            update = {"_compacted": False, "_reason": "compactor_error"}
        compacted = bool(update.pop("_compacted", False))
        reason = update.pop("_reason", "")
        absorbed = update.pop("_absorbed", 0)
        restored = update.pop("_restored", 0)
        deps.trace("memory", "compacted" if compacted else "skipped", absorbed=absorbed,
                   restored=restored, reason=reason, watermark=update.get("summary_watermark",
                                                                          state.get("summary_watermark", 0)))
        return update

    graph = StateGraph(SupportState)
    for name, node in (("open_turn", open_turn), ("precheck", precheck), ("route", route),
                       ("compose", compose), ("review", make_review()),
                       ("finalize", finalize), ("compact", compact)):
        graph.add_node(name, node)
    for role in ROLES:
        graph.add_node(f"{role}_node", make_specialist(role))
    graph.add_edge(START, "open_turn")
    graph.add_edge("open_turn", "precheck")
    graph.add_conditional_edges("precheck", after_precheck,
                                {"review": "review", "route": "route"})
    graph.add_conditional_edges("route", fan_out,
                                [*(f"{role}_node" for role in ROLES), "compose"])
    for role in ROLES:
        graph.add_edge(f"{role}_node", "compose")
    graph.add_edge("compose", "review")
    graph.add_conditional_edges("review", should_end,
                                {"route": "route", "compose": "compose",
                                 "finalize": "finalize"})
    graph.add_edge("finalize", "compact")
    graph.add_edge("compact", END)
    return graph.compile(checkpointer=checkpointer)
