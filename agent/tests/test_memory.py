# 中文文案必然包含全角标点
"""Acceptance for phase 4: summary + watermark, with nothing deleted."""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import memory
from agent.fakes import USAGE, ScriptedSupportModel, ai_with_tools, offline_deps as make_deps, tool_call
from agent.service import SupportService

pytestmark = pytest.mark.asyncio

TENANT = "t_demo"


def transcript(pairs: int, *, summary: str = "", watermark: int = 0) -> dict:
    messages = []
    for index in range(pairs):
        messages.append(HumanMessage(content=f"第{index}笔 ORD-{1000 + index} 金额 20 元 到哪了",
                                     id=f"u{index}"))
        messages.append(AIMessage(content=f"第{index}笔已发货", id=f"a{index}"))
    return {"messages": messages, "summary": summary, "summary_watermark": watermark}


async def test_context_is_summary_above_watermark_plus_verbatim_tail() -> None:
    state = transcript(4, summary="客户要求周末配送。", watermark=4)
    prompt = memory.model_context(state, window=4)
    assert prompt[0].content.startswith(memory.SUMMARY_BANNER)
    assert "客户要求周末配送。" in prompt[0].content
    # Only messages at or after the watermark are replayed verbatim, and `window`
    # counts those messages exactly.
    assert [m.content for m in prompt[1:]] == ["第2笔 ORD-1002 金额 20 元 到哪了", "第2笔已发货",
                                               "第3笔 ORD-1003 金额 20 元 到哪了", "第3笔已发货"]
    assert [m.content for m in memory.model_context(state, window=2)[1:]] == [
        "第3笔 ORD-1003 金额 20 元 到哪了", "第3笔已发货"]


async def test_compaction_is_skipped_below_the_trigger() -> None:
    state = transcript(2)
    update = await memory.compact(state, model=ScriptedSupportModel(), trigger=12, keep=6)
    assert update == {}


async def test_compaction_advances_watermark_without_deleting() -> None:
    state = transcript(6)
    update = await memory.compact(state, model=ScriptedSupportModel(summary_mode="keep_anchors"),
                                  trigger=4, keep=2)
    assert update["summary_watermark"] == 10
    assert update["summary"].startswith("对话要点：")
    assert memory.visible_messages(state) == state["messages"]  # compact never mutates


async def test_dropped_anchors_are_written_back_into_the_summary() -> None:
    """The regression the old path had: summarising silently lost order ids and numbers."""
    state = transcript(6)
    update = await memory.compact(state, model=ScriptedSupportModel(summary_mode="drop_anchors"),
                                  trigger=4, keep=2)
    summary = update["summary"]
    assert "待保留要点" in summary
    assert "ORD-1000" in summary and "20 元" in summary


async def test_summarisation_gets_its_own_output_budget() -> None:
    """摘要那一次调用不能继承聊天模型的 1024 token 预算——实测那就是空正文的成因。

    臂内 41 次压缩尝试 34 次 ``empty_summary``（全是"200 成功、正文为空"，不是异常也不是限流）；
    隔离探针用臂一样长的输入重跑：1024 预算下 4 次里 3 次 ``finish_reason=length``，
    4096 预算下 4 次全部正常收尾（报告 §6g 发现三）。
    """
    model = ScriptedSupportModel(summary_mode="keep_anchors")
    await memory.compact(transcript(6), model=model, trigger=4, keep=2)
    assert model.summary_kwargs.get("max_tokens") == memory.SUMMARY_MAX_TOKENS
    assert memory.SUMMARY_MAX_TOKENS >= 4096, "预算调回去就等于把 §6g 那个 83% 空正文率放回来"


async def test_a_truncated_summary_must_not_advance_the_watermark() -> None:
    """半条摘要比没有摘要更坏：它会推进水位线，把截掉的那段原文从模型视野里拿走。

    1024 预算下真实出现过"正文 12 字 / 100 字且 finish_reason=length"还被当成摘要成功的轮次，
    所以 `finish_reason` 是必须看的字段，不是日志装饰。
    """
    state = transcript(6)
    update = await memory.compact(state, model=ScriptedSupportModel(summary_finish="length"),
                                  trigger=4, keep=2)
    assert update == {"_compacted": False, "_reason": "truncated_summary"}
    ok = await memory.compact(state, model=ScriptedSupportModel(summary_finish="stop"),
                              trigger=4, keep=2)
    assert ok["summary_watermark"] == 10                      # 同一个替身，只有收尾原因不同


async def test_summary_failure_leaves_the_watermark_untouched() -> None:
    state = transcript(6)
    update = await memory.compact(state, model=ScriptedSupportModel(fail_summary=True),
                                  trigger=4, keep=2)
    assert update == {"_compacted": False, "_reason": "summary_model_failed"}
    assert "summary_watermark" not in update


async def test_long_summarised_text_cannot_push_the_anchors_out() -> None:
    """Regression: truncation used to cut the tail, i.e. the repaired anchors itself."""
    state = transcript(14)
    # Make the prior summary long enough that naive tail truncation would drop the
    # repair line, then compact twice so the budget has to be respected.
    update = await memory.compact(state, model=ScriptedSupportModel(summary_mode="drop_anchors"),
                                  trigger=4, keep=2)
    assert "待保留要点" in update["summary"]
    assert "ORD-1000" in update["summary"]
    assert len(update["summary"]) <= memory.SUMMARY_LIMIT
    fed = "\n".join(memory.message_text(m) for m in
                    memory.model_context({**state, **update, "messages": state["messages"]}, window=4))
    assert "ORD-1000" in fed


async def test_named_fields_survive_a_summariser_that_forgets_them() -> None:
    """An invoice title is neither an id nor a number, and was the last thing to go."""
    state = {"messages": [
        HumanMessage(content="对了，那笔单子的发票抬头我填的是星辰科技，别给我开错了", id="u1"),
        AIMessage(content="好的，已记下抬头信息。", id="a1"),
        HumanMessage(content="订单 ORD-2024-701 这单实付 259 元", id="u2"),
        AIMessage(content="该订单已发货。", id="a2"),
        HumanMessage(content="顺带问一句", id="u3"), AIMessage(content="嗯", id="a3"),
        HumanMessage(content="再问一句", id="u4"), AIMessage(content="哦", id="a4"),
    ], "summary": "", "summary_watermark": 0}
    update = await memory.compact(state, model=ScriptedSupportModel(summary_mode="drop_anchors"),
                                  trigger=4, keep=2)
    summary = update["summary"]
    assert "星辰科技" in summary, "抬头必须被回补，否则客户点名要记住的字段最先丢"
    assert "ORD-2024-701" in summary and "259 元" in summary.replace("259元", "259 元")
    assert len(summary) <= memory.SUMMARY_LIMIT
    # priority packing: ids and named-field phrases outrank values and generic clauses
    # (an anchor is stored as the phrase that starts with the keyword, which is what
    # carries the value the customer will ask back)
    assert memory.prioritise_anchors({"ORD-1", "发票抬头我填的是星辰科技", "7 天", "别弄错"}) == [
        "ORD-1", "发票抬头我填的是星辰科技", "7 天", "别弄错"]


def _service(saver, **kwargs):
    trigger = kwargs.pop("compact_trigger", 3)
    keep = kwargs.pop("compact_keep", 2)

    async def deps_for_tenant(_tenant_id):
        deps, _model, _events, _captured = make_deps(**kwargs)
        return deps

    return SupportService(deps_for_tenant, checkpointer=saver,
                          graph_kwargs={"compact_trigger": trigger, "compact_keep": keep})


async def test_long_conversation_keeps_every_turn_and_a_moving_watermark() -> None:
    saver = InMemorySaver()
    service = _service(saver)
    for index in range(5):
        await service.run_turn(TENANT, "t_demo:long", f"问题{index}：ORD-{1000 + index} 多少钱",
                               turn_id=f"L{index}")
    graph = await service.graph_for(TENANT)
    state = (await graph.aget_state({"configurable": {
        "thread_id": "support_v2:t_demo:long"}})).values
    assert len(memory.visible_messages(state)) == 10  # 5 turns, none dropped
    assert state["summary_watermark"] > 0
    assert state["summary"]
    assert len(state["ledger"]) == 5
    counters = memory.context_state(state)
    assert counters["total"] == 10 and counters["summarized"] + counters["verbatim"] == 10


async def test_coreference_survives_compaction() -> None:
    saver = InMemorySaver()
    service = _service(saver, model=ScriptedSupportModel(summary_mode="keep_anchors"))
    await service.run_turn(TENANT, "t_demo:ref", "ORD-2024 金额 30 元 到哪了", turn_id="R1")
    for index in range(4):
        await service.run_turn(TENANT, "t_demo:ref", f"顺带问一下问题{index}", turn_id=f"R{index + 2}")
    graph = await service.graph_for(TENANT)
    state = (await graph.aget_state({"configurable": {
        "thread_id": "support_v2:t_demo:ref"}})).values
    prompt = memory.model_context(state, window=2)
    banner = prompt[0].content
    assert "ORD-2024" in banner or "ORD-2024" in " ".join(m.content for m in prompt[1:])
    assert "不作为订单状态或店铺政策的依据" in banner


async def test_concurrent_turns_on_one_thread_do_not_lose_the_watermark() -> None:
    """Regression for the overwrite bug: two writers must not clobber each other."""
    saver = InMemorySaver()
    service = _service(saver)
    await asyncio.gather(*(service.run_turn(TENANT, "t_demo:racy", f"并发问题{index}", turn_id=f"C{index}")
                           for index in range(4)))
    graph = await service.graph_for(TENANT)
    state = (await graph.aget_state({"configurable": {
        "thread_id": "support_v2:t_demo:racy"}})).values
    assert len(state["ledger"]) == 4
    assert len(memory.visible_messages(state)) == 8
    assert {record["turn_id"] for record in state["ledger"]} == {"C0", "C1", "C2", "C3"}
    assert state["summary_watermark"] <= len(memory.visible_messages(state))


# --- 阶段4 边界的正面证明：摘要记住了 ≠ 可以照着说 ------------------------------------

async def _state_of(service: SupportService, thread: str) -> dict:
    graph = await service.graph_for(TENANT)
    return (await graph.aget_state({"configurable": {"thread_id": f"support_v2:{thread}"}})).values


async def test_summary_cannot_license_a_policy_number() -> None:
    """"摘要不是店铺政策的事实来源"必须是可证的，而不只是结构上碰巧成立。

    先把一轮真实检索过的 "7 天" 推进摘要区（`keep_anchors` 让脚本摘要器**故意**把它
    原样留在 summary 里），再让模型在没有任何当轮委派的情况下复述这个数值。数值确实
    还在摘要中——它仍然不得出现在给客户的答案里。
    """
    saver = InMemorySaver()
    model = ScriptedSupportModel(summary_mode="keep_anchors")
    service = _service(saver, model=model, compact_trigger=4, compact_keep=2)
    thread = "t_demo:sumlic"

    model.supervisor_script[:] = [
        ai_with_tools("核实", [tool_call("consult_faq", {"question": "退货期限"}, "c1")]),
        AIMessage(content="签收后 7 天内可申请退货。", usage_metadata=USAGE)]
    await service.run_turn(TENANT, thread, "退货期限是多久？", turn_id="L1")

    model.supervisor_script[:] = []
    for index in range(4):
        await service.run_turn(TENANT, thread, f"顺带问一句{index}", turn_id=f"L{index + 2}")

    state = await _state_of(service, thread)
    assert state["summary_watermark"] > 0, "第 1 轮应已被吸收进摘要区"
    assert "7 天" in state["summary"], state["summary"]

    model.supervisor_script[:] = [
        AIMessage(content="本店退货期就是 7 天，您可以放心申请。", usage_metadata=USAGE)]
    turn = await service.run_turn(TENANT, thread, "那我现在还能退吗？", turn_id="L9")
    assert "7 天" not in turn.answer, turn.answer
    assert any("证据" in issue for issue in turn.review_issues), turn.review_issues

    # 对照组：同一个数值、同一线程，只要本轮真的检索到证据就放行 ——
    # 证明上面拦的是"无当轮证据"，不是"7 天"这个值本身被禁。
    model.supervisor_script[:] = [
        ai_with_tools("重新核实", [tool_call("consult_faq", {"question": "退货期限"}, "c2")]),
        AIMessage(content="本店退货期为签收后 7 天。", usage_metadata=USAGE)]
    recheck = await service.run_turn(TENANT, thread, "帮我按现行资料再确认一次退货期", turn_id="L10")
    assert recheck.outcome == "answered", (recheck.outcome, recheck.review_issues)
    assert "7 天" in recheck.answer, recheck.answer


async def test_changed_condition_forces_a_fresh_order_check() -> None:
    """条件冲突：客户改口之后，摘要里的旧订单结论不能替代本轮重核。

    同一条线程上先委派订单 Agent 拿到 ORD-1001 的事实，之后客户把"退货"改成"换货"并
    追问状态。两条分支都要验：靠摘要作答 → hard 拦下；本轮重新委派 → 放行，且委派记录
    属于本轮（不是上一轮的残留）。
    """
    saver = InMemorySaver()
    model = ScriptedSupportModel(summary_mode="keep_anchors")
    service = _service(saver, model=model, compact_trigger=4, compact_keep=2)
    thread = "t_demo:conflict"

    model.supervisor_script[:] = [
        ai_with_tools("核实", [tool_call("consult_order", {"question": "ORD-1001 到哪了"}, "c1")]),
        AIMessage(content="订单 ORD-1001 状态：已发货；金额：199.00 元。", usage_metadata=USAGE)]
    await service.run_turn(TENANT, thread, "ORD-1001 到哪了？我要退货", turn_id="K1")

    model.supervisor_script[:] = []
    for index in range(4):
        await service.run_turn(TENANT, thread, f"顺带问一句{index}", turn_id=f"K{index + 2}")

    state = await _state_of(service, thread)
    assert "ORD-1001" in state["summary"], state["summary"]

    # 分支一：不重核，直接引用摘要里的旧结论 → 必须拦
    model.supervisor_script[:] = [
        AIMessage(content="订单 ORD-1001 已发货，可以放心换货。", usage_metadata=USAGE)]
    stale = await service.run_turn(TENANT, thread, "改成换货吧，这单现在什么状态？", turn_id="K9")
    assert "ORD-1001" not in stale.answer, stale.answer
    assert any("核验" in issue or "订单" in issue for issue in stale.review_issues), stale.review_issues

    # 分支二：本轮重新委派 → 放行，且本轮委派记录里就有 order
    model.supervisor_script[:] = [
        ai_with_tools("重新核实", [tool_call("consult_order", {"question": "ORD-1001 状态"}, "c9")]),
        AIMessage(content="订单 ORD-1001 状态：已发货；金额：199.00 元。", usage_metadata=USAGE)]
    fresh = await service.run_turn(TENANT, thread, "那你重新帮我看下这单状态", turn_id="K10")
    assert fresh.outcome == "answered", (fresh.outcome, fresh.review_issues)
    assert "ORD-1001" in fresh.answer
    turn_delegations = [item for item in fresh.roles]
    assert "order" in turn_delegations, turn_delegations


async def test_summary_channel_cannot_license_an_unverified_order_id() -> None:
    """记忆通道里的单号不是授权：跨客户/未核验的 id 被摘要"记住"后仍不得对客户断言。

    这条把验收标准 5 与阶段4 的"摘要不是订单事实来源"接起来：前面两条边界各自有测试
    （`test_graph.py` 里模型凭空引用 ORD-9999 被拦、本文件里摘要里的旧结论被拦），但没
    有人证明过**id 是从记忆通道进来的**这一路——它是最容易被实现"顺手放宽"的方向：只看
    "本轮委派过的 id"会不会因为多轮委派累积、或因为摘要文本进了 prompt 就变成合法引用。

    结构：客户自己提到一个从未核验的他人单号 → 压缩把它留在摘要锚点里 → 模型照着摘要说
    → 必须拦；对照组是本轮真的查过自己的 ORD-1001 → 必须放行。
    """
    saver = InMemorySaver()
    model = ScriptedSupportModel(summary_mode="keep_anchors")
    service = _service(saver, model=model, compact_trigger=4, compact_keep=2)
    thread = "t_demo:poisoned"

    model.supervisor_script[:] = []
    await service.run_turn(TENANT, thread, "我朋友那单 ORD-8888 显示已签收了，帮我也看看",
                           turn_id="M1")
    for index in range(4):
        await service.run_turn(TENANT, thread, f"另外问一句{index}", turn_id=f"M{index + 2}")

    state = await _state_of(service, thread)
    assert state["summary_watermark"] > 0, "前面几轮应已被吸收进摘要区"
    assert "ORD-8888" in state["summary"], f"锚点回补没把这个单号留在摘要里：{state['summary']}"

    # 分支一：照着记忆通道里的他人单号作答 → 必须 hard 拦，且不能把 id 透出去
    model.supervisor_script[:] = [
        AIMessage(content="您朋友那单 ORD-8888 已签收，您这单应该也快了。", usage_metadata=USAGE)]
    cited = await service.run_turn(TENANT, thread, "那我那单呢？", turn_id="M9")
    assert "ORD-8888" not in cited.answer, cited.answer
    assert "引用了本轮未经业务接口核验归属的订单号" in cited.review_issues, cited.review_issues

    # 用户已确认可有限复述同一会话中自己提供的单号；不能由此断言归属或订单状态。
    model.supervisor_script[:] = [
        AIMessage(content="您最开始提到的单号是 ORD-8888；尚未核验订单归属。", usage_metadata=USAGE)]
    restated = await service.run_turn(TENANT, thread, "我最早说的那个单号是多少？", turn_id="M9b")
    assert restated.outcome == "answered", restated.review_issues
    assert "ORD-8888" in restated.answer and "未核验" in restated.answer

    # 对照组：本轮真的通过业务接口核验了自己的单号 → 同一个线程、同样的措辞必须放行
    model.supervisor_script[:] = [
        ai_with_tools("查这单", [tool_call("consult_order", {"question": "ORD-1001 状态"}, "c1")]),
        AIMessage(content="您的订单 ORD-1001 状态：已发货；金额：199.00 元。", usage_metadata=USAGE)]
    own = await service.run_turn(TENANT, thread, "帮我查我自己那单", turn_id="M10")
    assert own.outcome == "answered", (own.outcome, own.review_issues)
    assert "ORD-1001" in own.answer, own.answer
