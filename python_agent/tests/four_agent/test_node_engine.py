# 中文文案必然包含全角标点
"""四 Agent 主图验收：路由只派发、专家各答一路、compose 只写答复、review 决定补派或重写。

断言的是「决策与执行分离」本身——同一份 Reviewer、同一份 state，但每一路专家是独立
节点，重试只补缺的那一路。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.support.deps import EvidenceLedger
from app.agent.support.fakes import USAGE, FakeSpecialist, ScriptedSupportModel, ai_with_tools, offline_deps, tool_call
from app.agent.support.graph import TURN_STEP_BUDGET, build_support_graph
from app.agent.support.plan import ROUTER_PROMPT

pytestmark = pytest.mark.asyncio


def plan(*calls: tuple[str, str]) -> AIMessage:
    """一条路由输出：只有标记，没有答复文本。"""
    return ai_with_tools("", [tool_call(tool, {"question": question}, f"c{index}")
                              for index, (tool, question) in enumerate(calls)])


async def ask(deps: Any, question: str, *, turn: str = "n1", **graph_kwargs: Any) -> dict[str, Any]:
    graph = await build_support_graph(deps, **graph_kwargs)
    config = {"configurable": {"thread_id": f"probe-{turn}"},
              "recursion_limit": TURN_STEP_BUDGET}
    return await graph.ainvoke(
        {"messages": [HumanMessage(content=question, id=f"{turn}:u")], "turn_id": turn}, config)


async def test_composite_question_fans_out_and_composes_once() -> None:
    deps, model, events, _ = offline_deps(model=ScriptedSupportModel(
        supervisor_script=[plan(("consult_faq", "退货运费谁承担"),
                                ("consult_order", "ORD-1001 到哪了"))],
        compose_script=[AIMessage(
            content="订单 ORD-1001 已发货；退货请在签收后 7 天内申请，运费自理。",
            usage_metadata=USAGE)]))
    state = await ask(deps, "我的订单 ORD-1001 到哪了？另外退货运费谁承担？")

    assert {item["specialist"] for item in state["delegations"]} == {"faq", "order"}
    assert state["outcome"] == "answered"
    assert "7 天" in state["answer"] and "ORD-1001" in state["answer"]
    # 两路并行跑完才写答复：撰写节点只调用一次。
    assert model.recorded.count("compose") == 1
    delegate = [event for event in events if event["stage"] == "delegate"]
    assert {"faq", "order"} <= {event["name"] for event in delegate}
    # route 的两个标记就是两次委派，并行写 usage 时不能互相覆盖。
    assert state["usage"]["n1"]["delegate_calls"] == 2


async def test_router_receives_its_own_system_prompt() -> None:
    """路由提示词必须真的发给模型。

    ``bind_tools`` 只提供工具 schema，不会附带「只路由、不回答」。少了这条 system message，
    真实模型会把路由调用当成一次普通问答直接写答复，``parse_decision`` 于是读到「直答」，
    整张图退化成单模型——而脚本化 fake 按脚本回放，照样能吐出 tool_calls，发现不了。
    所以这里断言模型侧收到的就是 ``ROUTER_PROMPT`` 本身。
    """
    deps, model, _events, _ledger = offline_deps(model=ScriptedSupportModel(
        supervisor_script=[plan(("consult_order", "ORD-1001 到哪了"))]))
    await ask(deps, "ORD-1001 到哪了？", turn="n-prompt")

    assert model.router_prompts, model.recorded
    assert model.router_prompts[0] == ROUTER_PROMPT


async def test_single_role_is_passed_through_without_a_compose_call() -> None:
    """单角色且订单状态确已核验：零撰写调用——这是 node 版最直接的省钱点。"""
    deps, model, _events, _ = offline_deps(model=ScriptedSupportModel(
        supervisor_script=[plan(("consult_order", "ORD-1001 到哪了"))]))
    state = await ask(deps, "ORD-1001 到哪了？", turn="n-order")

    assert state["outcome"] == "answered"
    assert "ORD-1001" in state["answer"]
    assert "compose" not in model.recorded


async def test_policy_answer_is_never_passed_through() -> None:
    """判定型 FAQ 走 compose：专家原文是条款，客户要的是「可以申请/不可以」。"""
    deps, model, _events, _ = offline_deps(model=ScriptedSupportModel(
        supervisor_script=[plan(("consult_faq", "这个可以退货吗"))],
        compose_script=[AIMessage(content="可以申请：签收后 7 天内都能退，运费自担。",
                                  usage_metadata=USAGE)]))
    state = await ask(deps, "这个可以退货吗？", turn="n-faq")

    assert model.recorded.count("compose") == 1
    assert "可以申请" in state["answer"]
    # 透传会把「店铺资料：……」这句专家原文直接发给客户。
    assert "店铺资料" not in state["answer"]


async def test_greeting_is_passed_through() -> None:
    """寒暄没有可核实的项，不需要撰写节点。"""
    deps, model, _events, _ = offline_deps(model=ScriptedSupportModel(
        supervisor_script=[plan(("consult_general", "你好"))]))
    state = await ask(deps, "你好", turn="n-hello")

    assert state["outcome"] == "answered"
    assert "compose" not in model.recorded


async def test_parallel_dispatch_does_not_cross_attribute_evidence() -> None:
    """并行扇出时，别的分支检索到的 chunk 不算这一路的证据。

    旧实现「执行前记游标、执行后取新增」在处理共享事件流时会把后到的检索结果算给
    先结束的分支；Reviewer 的「有证据才允许说政策数值」于是被别人的检索买通。
    """
    deps, _model, _events, _ledger = offline_deps(model=ScriptedSupportModel(
        supervisor_script=[plan(("consult_faq", "退货运费谁承担"),
                                ("consult_order", "ORD-1001 到哪了"))],
        compose_script=[AIMessage(content="订单 ORD-1001 已发货；退货请在签收后 7 天内申请。",
                                  usage_metadata=USAGE)]))
    state = await ask(deps, "我的订单 ORD-1001 到哪了？另外退货运费谁承担？", turn="n-attr")

    findings = {item["specialist"]: item for item in state["delegations"]}
    assert [item["id"] for item in findings["faq"]["evidence"]] == ["chunk-7d"]
    assert findings["order"]["evidence"] == []


async def test_evidence_ledger_isolates_parallel_branches() -> None:
    """账本本身的行为：并行窗口互不可见，窗口外的记录一律丢弃。"""
    ledger = EvidenceLedger()

    async def branch(key: str, delay: float) -> None:
        with ledger.scope(key):
            await asyncio.sleep(delay)
            ledger.record([key])

    await asyncio.gather(branch("faq|a", 0.02), branch("order|b", 0.0))
    ledger.record(["orphan"])  # 无归属窗口

    assert ledger.collected("faq|a") == ["faq|a"]
    assert ledger.collected("order|b") == ["order|b"]
    assert ledger.collected("") == []


async def test_missing_role_is_refilled_without_replanning() -> None:
    """Reviewer 说缺 FAQ：只补派 FAQ，不再问一次路由模型。

    第一版草稿刻意不含政策数值：一旦 compose 自己写出「7 天」，Reviewer 的硬规则
    （数值无当轮证据）会先于补派触发，整轮直接判死——那是另一条边界，由
    ``test_shape_gap_*`` 覆盖。这里要验的是软规则驱动的补派。
    """
    deps, model, events, _ = offline_deps(model=ScriptedSupportModel(
        supervisor_script=[plan(("consult_general", "退货期限多久"))],
        compose_script=[AIMessage(content="您好，我可以帮您解释店铺政策。",
                                  usage_metadata=USAGE),
                        AIMessage(content="可以申请：签收后 7 天内退货，运费自担。",
                                  usage_metadata=USAGE)]))
    state = await ask(deps, "退货期限多久？", turn="n-refill")

    assert state["outcome"] == "answered", state["review_issues"]
    assert "7 天" in state["answer"]
    assert state["attempts"] == 2
    assert model.recorded.count("supervisor") == 1      # 路由只规划了一次
    # 首轮 general 也要过 compose（单角色不再无条件透传），补派后再写一次。
    assert model.recorded.count("compose") == 2
    refill = [event for event in events if event["stage"] == "route"
              and event["attributes"].get("roles") == ["faq"]]
    assert refill, events


async def test_shape_gap_rewrites_the_answer_without_re_dispatching() -> None:
    """回复是一段政策而不是结论：只让 compose 重写，专家不重跑。"""
    long_policy = (
        "本店退货规则分为若干条款。"
        "第一条，客户在申请退货前应确认商品与包装的完好情况，并保留相关凭证。"
        "第二条，店铺会根据商品类型、签收时间与使用情况分别判断是否受理。"
        "第三条，涉及赠品、组合商品或活动商品的，按活动页面说明单独判断。"
        "第四条，店铺保留对异常申请的复核权利，复核期间答复可能延后。"
        "第五条，客户可通过订单页面提交申请，并按页面提示寄回商品。"
        "第六条，如对处理结果有异议，可联系人工协助核查。"
        "第七条，未尽事宜按店铺页面公示执行，感谢理解与配合。"
        "第八条，客户提交申请后，店铺会在收到商品后按流程处理，进度可通过订单页面查看。")
    faq = FakeSpecialist(answer=long_policy, tools=("search_kb",),
                         evidence=[{"id": "chunk-7d", "title": "退货政策",
                                    "text": "签收后 7 天内可以申请退货"}])
    deps, model, _events, _ = offline_deps(
        faq=faq, model=ScriptedSupportModel(
            supervisor_script=[plan(("consult_faq", "这个可以退货吗"))],
            compose_script=[AIMessage(content=long_policy, usage_metadata=USAGE),
                            AIMessage(content="可以申请：签收后 7 天内退货，运费自担。",
                                      usage_metadata=USAGE)]))
    state = await ask(deps, "这个可以退货吗？", turn="n-shape", max_passes=2)

    assert state["outcome"] == "answered", state["review_issues"]
    assert "可以申请" in state["answer"]
    assert state["attempts"] == 2
    assert model.recorded.count("compose") == 2   # 首写 + 重写，不重新派发
    assert len(faq.questions) == 1                # FAQ 专家没有被重跑


async def test_router_failure_degrades_to_a_safe_answer() -> None:
    deps, _model, _events, _ = offline_deps(model=ScriptedSupportModel(fail_supervisor=True))
    state = await ask(deps, "退货期限是多久？", turn="n-crash", max_passes=1)

    assert state["outcome"] == "no_result"
    assert state["answer"].strip()
    assert state["delegations"] == []
    assert "supervisor model unavailable" in state["last_error"]


async def test_router_failure_does_not_spin_the_rewrite_loop() -> None:
    """路由连续失败时不要空转：无草稿又无材料，「重写」没有可写的来源。"""
    deps, model, _events, _ = offline_deps(model=ScriptedSupportModel(fail_supervisor=True))
    state = await ask(deps, "退货期限是多久？", turn="n-crash-loop")   # 默认 max_passes=3

    assert state["outcome"] == "no_result"
    assert state["attempts"] == 1                 # 只判一次就收尾，不吃掉三轮预算
    assert model.recorded.count("supervisor") == 1
    assert "compose" not in model.recorded        # 空稿重写毫无意义，一次都不该跑


async def test_specialist_failure_is_recorded_not_silently_dropped() -> None:
    deps, _model, _events, _ = offline_deps(
        general=FakeSpecialist(answer="", raises=True),
        model=ScriptedSupportModel(supervisor_script=[plan(("consult_general", "你好"))]))
    state = await ask(deps, "你好", turn="n-fail")

    findings = {item["specialist"]: item for item in state["delegations"]}
    assert findings["general"]["failed"] is True
    assert state["answer"]


async def test_node_graph_shape_is_visible() -> None:
    deps, *_rest = offline_deps()
    graph = await build_support_graph(deps)
    nodes = set(graph.get_graph().nodes)
    assert {"open_turn", "precheck", "route", "faq_node", "order_node", "general_node",
            "compose", "review", "finalize", "compact"} <= nodes
    assert "supervisor" not in nodes
    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    assert ("review", "compose") in edges and ("review", "route") in edges
    assert ("route", "faq_node") in edges


class GatedFaq(FakeSpecialist):
    """检索完成后卡住，好让另一个会话在这段时间里开局。

    真实场景就是这一格：A 会话的 FAQ 正在检索，B 会话同时发来一句话，B 的 ``open_turn``
    在 A 的派发窗口还开着的时候执行。旧实现整本清账，A 那次派发的窗口连游标一起没了。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.retrieved = asyncio.Event()
        self.release = asyncio.Event()

    async def ainvoke(self, payload: dict[str, Any], config: Any = None) -> dict[str, Any]:
        result = await super().ainvoke(payload, config)  # 检索副作用已记账
        self.retrieved.set()
        await self.release.wait()
        return result


async def test_another_turn_starting_does_not_clear_this_turns_evidence() -> None:
    """账本随租户图共享：B 会话开局不能抹掉 A 会话正在进行的派发窗口。

    断言落在 finding 的证据上而不是答复上：证据丢了之后，Reviewer 的「有证据才允许说
    政策数值」只能拿专家答复文本充数，而这种退化在答复层面看不出来。
    """
    traced: list[dict[str, Any]] = []
    ledger = EvidenceLedger(window=lambda: len(traced),
                            since=lambda cursor: [eid for item in traced[cursor:]
                                                  for eid in item.get("ids", [])])
    faq = GatedFaq(answer="店铺资料：签收后 7 天内可申请退货，运费自担。",
                   tools=("search_kb",))
    faq.on_call = lambda: traced.append({"ids": ["chunk-7d"]})  # 生产路径的证据事件
    deps, _model, _events, _ = offline_deps(model=ScriptedSupportModel(
        supervisor_script=[plan(("consult_faq", "这个可以退货吗")),
                           plan(("consult_general", "你好"))],
        compose_script=[AIMessage(content="可以申请：签收后 7 天内退货，运费自担。",
                                  usage_metadata=USAGE)]), faq=faq)
    deps = replace(deps, evidence=ledger)
    graph = await build_support_graph(deps)

    def config(thread: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread}, "recursion_limit": TURN_STEP_BUDGET}

    first = asyncio.create_task(graph.ainvoke(
        {"messages": [HumanMessage(content="这个可以退货吗？", id="t-a:u")], "turn_id": "t-a"},
        config("probe-a")))
    await asyncio.wait_for(faq.retrieved.wait(), 5)   # A 已在窗口内，等 B 开局
    await graph.ainvoke({"messages": [HumanMessage(content="你好", id="t-b:u")], "turn_id": "t-b"},
                        config("probe-b"))
    faq.release.set()
    state = await asyncio.wait_for(first, 10)

    finding = next(item for item in state["delegations"] if item["specialist"] == "faq")
    assert [item["id"] for item in finding["evidence"]] == ["chunk-7d"]
    assert state["outcome"] == "answered", state["review_issues"]


async def test_ledger_clear_is_scoped_to_one_turn() -> None:
    """清账、消费、丢弃三种收尾都只能作用于自己那次派发。"""
    ledger = EvidenceLedger()
    with ledger.scope("t-a|faq|1"):
        ledger.record(["chunk-a"])
    with ledger.scope("t-b|faq|2"):
        ledger.record(["chunk-b"])

    ledger.clear("t-b")  # B 开局只清 B 的 key

    assert ledger.collected("t-a|faq|1") == ["chunk-a"]
    assert ledger.collected("t-b|faq|2") == []

    with ledger.scope("t-c|faq|3"):
        ledger.record(["chunk-c"])
    ledger.discard("t-c|faq|3")  # 失败派发没有 finding 来消费
    assert ledger.collected("t-c|faq|3") == []

    with ledger.scope("t-d|faq|4"):
        ledger.record(["chunk-d"])
    assert ledger.collected("t-d|faq|4") == ["chunk-d"]
    assert ledger.collected("t-d|faq|4") == []  # 读完即消费，账本不随进程时长增长


async def test_evidence_gap_hard_refills_the_missing_role() -> None:
    """误路由 + 撰写自己写出数值：先补派 FAQ 再复审，而不是直接判死。

    「硬规则先于补派」那版在这里是 review_blocked：政策问题被派给 general，compose 写出
    「7 天」，hard（数值无当轮证据）命中即收尾——明明补派一次 FAQ 就有证据。
    """
    deps, model, events, _ = offline_deps(model=ScriptedSupportModel(
        supervisor_script=[plan(("consult_general", "退货期限多久"))],
        compose_script=[AIMessage(content="签收后 7 天内可以申请退货。", usage_metadata=USAGE),
                        AIMessage(content="可以申请：签收后 7 天内退货，运费自担。",
                                  usage_metadata=USAGE)]))
    state = await ask(deps, "退货期限多久？", turn="n-gap")

    assert state["outcome"] == "answered", state["review_issues"]
    assert "可以申请" in state["answer"]
    assert state["attempts"] == 2
    assert model.recorded.count("supervisor") == 1      # 补派不再问一次路由模型
    assert model.recorded.count("compose") == 2         # 清掉不安全草稿后重写
    refill = [event for event in events
              if event["stage"] == "route" and event["name"] == "refill"]
    assert refill and refill[0]["attributes"]["roles"] == ["faq"], events


async def test_unauthorized_claim_is_not_refilled() -> None:
    """越权陈述是「这句不该出现」：补派拿回证据也救不了它，直接安全答复。"""
    deps, model, events, _ = offline_deps(model=ScriptedSupportModel(
        supervisor_script=[plan(("consult_general", "这个可以退货吗"))],
        compose_script=[AIMessage(content="已为您提交退款申请，签收后 7 天内到账。",
                                  usage_metadata=USAGE)]))
    state = await ask(deps, "这个可以退货吗？", turn="n-exec")

    assert state["outcome"] == "review_blocked"
    assert state["missing_roles"] == []
    assert model.recorded.count("compose") == 1
    assert not any(event["stage"] == "route" and event["name"] == "refill" for event in events)
    assert state["answer"].strip()


async def test_refill_does_not_bypass_the_review_budget() -> None:
    """预算用尽时由这一次 Reviewer 决定能否发出：缺角色不能再借一轮。"""
    deps, model, events, _ = offline_deps(model=ScriptedSupportModel(
        supervisor_script=[plan(("consult_general", "退货期限多久"))],
        compose_script=[AIMessage(content="签收后 7 天内可以申请退货。", usage_metadata=USAGE)]))
    state = await ask(deps, "退货期限多久？", turn="n-budget", max_passes=1)

    assert state["outcome"] == "review_blocked"
    assert state["attempts"] == 1
    assert model.recorded.count("compose") == 1
    assert not any(event["stage"] == "route" and event["name"] == "refill" for event in events)


async def test_fabricated_number_is_redrafted_once_without_the_old_draft() -> None:
    """材料已在手、只是撰写编了数值：丢掉旧稿重写一次，不补派、不重查。

    证据里只有「7 天」，首稿写成「15 天」——hard 命中，但 FAQ 已经派过、检索也有证据，
    再补派一次只会拿回同一份材料。这里要验三件事：重写发生了、没有重新派发专家、
    而且重写的材料里**没有旧稿**（带旧稿等于让模型照抄那个 15）。
    """
    deps, model, events, _ = offline_deps(model=ScriptedSupportModel(
        supervisor_script=[plan(("consult_faq", "这个可以退货吗"))],
        compose_script=[AIMessage(content="签收后 15 天内可以申请退货，运费自担。",
                                  usage_metadata=USAGE),
                        AIMessage(content="可以申请退货：签收后 7 天内，运费自担。",
                                  usage_metadata=USAGE)]))
    state = await ask(deps, "这个可以退货吗？", turn="n-redraft")

    assert state["outcome"] == "answered", state["review_issues"]
    assert "7 天" in state["answer"] and "15" not in state["answer"]
    assert state["attempts"] == 2
    assert model.recorded.count("compose") == 2                  # 首写 + 窄路重写
    assert not any(event["stage"] == "route" and event["name"] == "refill" for event in events)
    assert len(model.compose_prompts) == 2
    assert "【上一版答复】" not in model.compose_prompts[1]       # 旧稿不许回灌
    assert "15" not in model.compose_prompts[1]
    redraft = [event for event in events
               if event["stage"] == "compose" and event["name"] == "redrafted"]
    assert redraft, events


async def test_redraft_gives_up_after_one_try_and_is_blocked() -> None:
    """重写仍编数值：不再给第二次机会，交回 Reviewer 收窄为安全答复。

    第二次的草稿把 15 换成 30，依然是没依据的数值。``redraft_used`` 已经用掉，
    ``should_end`` 于是走 ``finalize`` → ``review_blocked``，而不是继续换措辞烧预算。
    """
    deps, model, _events, _ = offline_deps(model=ScriptedSupportModel(
        supervisor_script=[plan(("consult_faq", "这个可以退货吗"))],
        compose_script=[AIMessage(content="签收后 15 天内可以申请退货。", usage_metadata=USAGE),
                        AIMessage(content="签收后 30 天内可以申请退货。", usage_metadata=USAGE)]))
    state = await ask(deps, "这个可以退货吗？", turn="n-redraft-blocked")

    assert state["outcome"] == "review_blocked"
    assert state["attempts"] == 2
    assert model.recorded.count("compose") == 2   # 只重写一次，没有第三次
    assert state["answer"].strip()
    assert "30 天" not in state["answer"]
