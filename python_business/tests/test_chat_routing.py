# 中文文案必然包含全角标点
"""Business 侧分流与审批冲突处理（不需要 MySQL：数据库读写全部被替换）。

钉住的是"业务状态先行、Agent 单入口"这一条：

* 分流顺序固定，人工接管与"用户自己点名转人工"先于排队；
* 有 ``status=0`` 工单时由 Business 给确定提示，**不再问 Agent**（这正是收敛前
  ``has_pending_ticket=true`` 的用法被撤掉的地方）；
* Agent 的 409 ``approval_pending`` 转成"建/复用工单 + resume + 提交 ticket"，
  503 ``approval_state_unknown`` 转成可重试的基础设施错误（不建单、不当作已回答）；
* 同一条轮次重试不会多出答复或第二张工单。
"""

from __future__ import annotations

import pytest

from business.chat import recovery, worker
from business.chat.routing import (ERROR_APPROVAL_PENDING, ERROR_APPROVAL_UNKNOWN,
                                   ROUTE_AGENT, ROUTE_ANSWER_THEN_HANDOFF, ROUTE_HANDOFF,
                                   ROUTE_HUMAN, ROUTE_MEDIA, ROUTE_PENDING,
                                   answer_then_handoff, route_message)

TICKET = {"id": 11, "ticket_no": "TK-1", "status": 0, "reason": "等人工", "created_at": None}


def row(text: str = "退货期限是多久？", msg_type: str = "text") -> dict:
    return {"id": 1, "turn_id": "turn-1", "tenant_id": "t_demo",
            "customer_id": "t_demo:buyer1", "thread_id": "t_demo:buyer1:s1",
            "user_message_id": 4, "user_message": text, "msg_type": msg_type,
            "attempt_count": 1, "lease_token": "lease-1"}


class Recorder:
    """收集 notify 事件、complete_turn 调用与外部副作用。"""

    def __init__(self):
        self.events: list[dict] = []
        self.committed: list[dict] = []
        self.recoveries: list[dict] = []
        self.tickets: list[str] = []
        self.failures: list[tuple] = []
        self.agent_calls = 0
        self.commit_ok = True
        self.lease_ok = True
        self.recovery_outcome = recovery.ResumeOutcome.CLEAR

    def install(self, monkeypatch, *, handling=None, queued=None, reply=None, gate=None):
        monkeypatch.setattr(worker, "handling_ticket", lambda _c: handling)
        monkeypatch.setattr(worker, "waiting_ticket", lambda _c: queued)
        monkeypatch.setattr(worker, "extract_from_content", lambda _c: {})
        monkeypatch.setattr(worker, "lease_held", lambda _row: self.lease_ok)

        def ticket(_customer, reason):
            self.tickets.append(reason)
            return TICKET, bool(queued)

        monkeypatch.setattr(worker, "ensure_customer_ticket", ticket)

        def recover_for_ticket(ticket_row, *, confirmed=True, wait_time=3, trace_id="",
                              record_result=True):
            self.recoveries.append({"ticket_no": ticket_row["ticket_no"],
                                    "trace_id": trace_id, "confirmed": confirmed,
                                    "outcome": self.recovery_outcome})
            return recovery.ResumeResult(self.recovery_outcome)

        monkeypatch.setattr(recovery, "recover_for_ticket", recover_for_ticket)
        monkeypatch.setattr(worker, "iso", lambda _value: "created")
        monkeypatch.setattr(worker, "notify",
                            lambda _row, event, **_kw: self.events.append(event))
        monkeypatch.setattr(worker, "send_reviewed_answer",
                            lambda _row, answer: self.events.append(
                                {"type": "chat_end", "msg": answer}))

        def complete(_row, **kwargs):
            if not self.commit_ok:
                return False
            self.committed.append(kwargs)
            return True

        monkeypatch.setattr(worker, "complete_turn", complete)

        def record_failure(_row, code, message):
            self.failures.append((code, message))
            return "queued"

        monkeypatch.setattr(worker, "record_failure", record_failure)

        def agent_reply(_row, _content):
            self.agent_calls += 1
            if gate is not None:
                raise gate
            return reply

        monkeypatch.setattr(worker, "reviewed_agent_reply", agent_reply)


# ---------------------------- 分流顺序 ----------------------------

def test_human_handoff_wins_over_everything() -> None:
    assert route_message(msg_type="text", handling=True, pending=True,
                         handoff=True) == ROUTE_HUMAN


def test_explicit_handoff_comes_before_the_waiting_notice() -> None:
    """用户再喊一次"转人工"应该复查工单，而不是被一句"请等待"挡回去。"""
    assert route_message(msg_type="text", handling=False, pending=True,
                         handoff=True) == ROUTE_HANDOFF


def test_pending_ticket_never_reaches_the_agent() -> None:
    assert route_message(msg_type="text", handling=False, pending=True,
                         handoff=False) == ROUTE_PENDING


def test_media_is_a_notice_not_an_agent_turn() -> None:
    assert route_message(msg_type="image", handling=False, pending=False,
                         handoff=False) == ROUTE_MEDIA


def test_everything_else_goes_to_the_four_agent_graph() -> None:
    assert route_message(msg_type="text", handling=False, pending=False,
                         handoff=False) == ROUTE_AGENT


def test_only_bare_commands_count_as_handoff() -> None:
    for text in ("转人工", "请转人工客服", "麻烦帮我转接人工，谢谢"):
        assert worker.explicit_handoff(text), text
    for text in ("人工客服几点下班？", "刚才那个订单能不能转人工处理", "退货期限是多久？"):
        assert not worker.explicit_handoff(text), text
    # 已知边界（沿用收敛前的正则，未在本轮改）：没有动词的"我要真人客服"不算点名转人工，
    # 会走 Agent；Agent 若判断需要人工，会由它自己的转人工能力处理。
    assert not worker.explicit_handoff("我要真人客服")


def test_conditional_refund_handoff_is_answered_first() -> None:
    assert answer_then_handoff("给我退货，你是否可以，不可以给我转接人工鼓舞")
    assert route_message(msg_type="text", handling=False, pending=False,
                         handoff=False, answer_then_transfer=True) == ROUTE_ANSWER_THEN_HANDOFF
    assert not answer_then_handoff("退货政策是什么？")
    assert not answer_then_handoff("人工客服几点下班？")


# ---------------------------- Worker 行为 ----------------------------

def test_human_taken_over_turn_does_not_touch_the_agent(monkeypatch) -> None:
    rec = Recorder()
    rec.install(monkeypatch, handling={**TICKET, "status": 1})
    worker.process_claim(row())

    assert rec.agent_calls == 0
    assert rec.committed[0]["result_type"] == "human"
    assert {event["type"] for event in rec.events} == {"user_msg", "human_notice"}


def test_waiting_ticket_gets_a_business_answer_without_calling_the_agent(monkeypatch) -> None:
    """排队期间客户拿到的必须是确定提示，且这一轮不会再产生第二张工单。"""
    rec = Recorder()
    rec.install(monkeypatch, queued=TICKET)
    worker.process_claim(row("还在吗？"))

    assert rec.agent_calls == 0
    assert rec.tickets == [], "已有工单时不得再建单"
    committed = rec.committed[0]
    assert committed["result_type"] == "ticket"
    assert "TK-1" in committed["answer"] and "等待人工接入" in committed["answer"]
    assert committed["ticket_id"] == TICKET["id"]


def test_explicit_handoff_creates_the_ticket_before_answering(monkeypatch) -> None:
    rec = Recorder()
    rec.install(monkeypatch)
    worker.process_claim(row("转人工"))

    assert rec.agent_calls == 0
    assert rec.tickets == ["用户主动请求转人工"]
    assert rec.committed[0]["result_type"] == "ticket"
    assert "TK-1" in rec.committed[0]["answer"]
    # 纯转人工没有 Agent 答复：工单号只出现一次，不额外追一条系统提示
    assert rec.committed[0]["notice"] == ""
    assert rec.committed[0]["answer_role"] == ""
    assert next(event for event in rec.events if event["type"] == "ticket")["answer"] == ""
    # 点名转人工不经过 Agent 闸门，但仍要看一眼旧审批：这条路径同样要收口
    assert rec.recoveries and rec.recoveries[0]["outcome"] is recovery.ResumeOutcome.CLEAR
    assert rec.recoveries[0]["trace_id"] == "turn-1"


def test_plain_question_goes_to_the_agent_and_commits_once(monkeypatch) -> None:
    rec = Recorder()
    rec.install(monkeypatch, reply="签收后 7 天内可申请退货。")
    worker.process_claim(row())

    assert rec.agent_calls == 1
    assert rec.tickets == []
    assert rec.committed[0]["result_type"] == "ai"
    assert rec.events[-1]["type"] == "chat_end"


def test_conditional_handoff_keeps_reviewed_answer_and_creates_one_ticket(monkeypatch) -> None:
    rec = Recorder()
    rec.install(monkeypatch, reply="目前查到签收后 7 天内可申请退货；我不能直接办理退货。")
    worker.process_claim(row("给我退货，你是否可以，不可以给我转接人工鼓舞"))

    assert rec.agent_calls == 1
    assert rec.tickets == ["用户主动请求转人工"]
    assert len(rec.committed) == 1
    commit = rec.committed[0]
    assert commit["result_type"] == "ticket"
    # 回答归回答：Agent 那段答复原样落库，工单号不再拼进正文
    assert commit["answer"] == "目前查到签收后 7 天内可申请退货；我不能直接办理退货。"
    assert "TK-1" not in commit["answer"]
    # 工单归工单：工单号走独立的 system 提示
    assert "TK-1" in commit["notice"]
    # 先答后转的答复确实是 Agent 写的，落 ai 才不会让刷新后的历史把它渲染成系统条
    assert commit["answer_role"] == "ai"
    assert not any(event["type"] == "chat_end" for event in rec.events)
    assert next(event for event in rec.events if event["type"] == "ticket")["answer"] == commit["answer"]


def test_conditional_handoff_agent_failure_does_not_claim_ticket(monkeypatch) -> None:
    rec = Recorder()
    rec.install(monkeypatch, gate=RuntimeError("agent unavailable"))
    worker.process_claim(row("不能退款就转人工"))

    assert rec.tickets == []
    assert rec.committed == []
    assert rec.failures


# ------------------- Agent 结构化拒绝：409 / 503 -------------------

def test_approval_pending_creates_a_ticket_and_resumes_the_old_approval(monkeypatch) -> None:
    """409 不是故障：建/复用工单 → resume → 本轮提交为 ``ticket``（客户只看到一条答复）。"""
    rec = Recorder()
    rec.install(monkeypatch,
                gate=worker.AgentGateError(ERROR_APPROVAL_PENDING,
                                           {"pending_approval": {"reason": "等人工确认"}},
                                           409))
    worker.process_claim(row("包邮规则是什么"))

    assert rec.agent_calls == 1
    assert rec.tickets == ["等人工确认"], "工单原因取自中断载荷"
    assert rec.committed[0]["result_type"] == "ticket"
    assert rec.committed[0]["ticket_no"] == "TK-1"
    assert rec.recoveries and rec.recoveries[0]["ticket_no"] == "TK-1", "resume 带上工单号"
    assert rec.recoveries[0]["trace_id"] == "turn-1", "resume 用同一个 turn_id 做归因"
    assert rec.recoveries[0]["outcome"] is recovery.ResumeOutcome.CLEAR
    types = [event["type"] for event in rec.events]
    assert types.count("ticket") == 1
    assert "chat_end" not in types, "不能同时给出 AI 答复与工单提示"


def test_approval_pending_without_reason_falls_back_to_a_stable_text(monkeypatch) -> None:
    rec = Recorder()
    rec.install(monkeypatch, gate=worker.AgentGateError(ERROR_APPROVAL_PENDING, {}, 409))
    worker.process_claim(row("在吗"))

    assert rec.tickets == ["会话存在未完成的转人工审批"]
    assert rec.committed[0]["result_type"] == "ticket"


def test_unknown_approval_state_is_retried_not_ticketed(monkeypatch) -> None:
    """503 状态未知：不建单、不提交答复、走可重试失败。"""
    rec = Recorder()
    rec.install(monkeypatch, gate=worker.AgentGateError(ERROR_APPROVAL_UNKNOWN, {}, 503))
    worker.process_claim(row())

    assert rec.tickets == [], "状态未知时不能建单"
    assert rec.committed == [], "也不能把这一轮说成已回答"
    assert rec.failures and rec.failures[0][0] == ERROR_APPROVAL_UNKNOWN


def test_a_stale_lease_cannot_notify_twice(monkeypatch) -> None:
    """重试同一条轮次：租约栅栏说"已提交过"时不再发第二条消息。"""
    rec = Recorder()
    rec.install(monkeypatch, gate=worker.AgentGateError(ERROR_APPROVAL_PENDING, {}, 409))
    worker.process_claim(row())
    first_events = len(rec.events)

    rec.commit_ok = False          # 第二次 claim 的租约已经过期
    worker.process_claim(row())

    assert len(rec.events) == first_events, "重复提交不得再通知客户"
    assert len(rec.tickets) == 2, "工单创建本身是幂等复用（同一张）"
    assert {item["ticket_no"] for item in rec.recoveries} == {"TK-1"}


def test_lost_lease_produces_no_ticket_and_no_resume(monkeypatch) -> None:
    """失去租约的 Worker 不能建单、也不能调 resume —— 这两步在数据库之外，收不回来。"""
    rec = Recorder()
    rec.install(monkeypatch, gate=worker.AgentGateError(ERROR_APPROVAL_PENDING, {}, 409))
    rec.lease_ok = False
    worker.process_claim(row())

    assert rec.tickets == [], "没有租约就不该创建工单"
    assert rec.recoveries == [], "没有租约就不该 resume"
    assert rec.committed == [], "也不能提交答复"
    assert rec.failures == [], "这不是失败，是租约已经被别人接管"


@pytest.mark.parametrize("outcome", [recovery.ResumeOutcome.UNKNOWN,
                                     recovery.ResumeOutcome.UNAVAILABLE])
def test_failed_resume_is_recorded_and_escalated(monkeypatch, outcome) -> None:
    """恢复失败不能静默结案：本轮仍是 ticket，但要留下待办并让运维看得见。"""
    rec = Recorder()
    rec.recovery_outcome = outcome
    rec.install(monkeypatch, gate=worker.AgentGateError(ERROR_APPROVAL_PENDING, {}, 409))
    worker.process_claim(row("包邮规则是什么"))

    assert rec.committed[0]["result_type"] == "ticket"
    assert rec.recoveries[0]["outcome"] is outcome
    escalation = [event for event in rec.events if event["type"] == "approval_recovery"]
    assert len(escalation) == 1
    assert escalation[0]["outcome"] == outcome.value
    assert escalation[0]["ticket_no"] == "TK-1"


def test_successful_resume_is_not_escalated(monkeypatch) -> None:
    rec = Recorder()
    rec.recovery_outcome = recovery.ResumeOutcome.RECOVERED
    rec.install(monkeypatch, gate=worker.AgentGateError(ERROR_APPROVAL_PENDING, {}, 409))
    worker.process_claim(row())

    assert [e for e in rec.events if e["type"] == "approval_recovery"] == []


@pytest.mark.parametrize("code,status", [(ERROR_APPROVAL_PENDING, 409),
                                        (ERROR_APPROVAL_UNKNOWN, 503)])
def test_gate_errors_are_not_swallowed_as_agent_failures(monkeypatch, code, status) -> None:
    """两种拒绝都不能落进 ``agent_unavailable`` 那条重试计数里。"""
    rec = Recorder()
    rec.install(monkeypatch, gate=worker.AgentGateError(code, {}, status))
    worker.process_claim(row())

    assert all(item[0] != "agent_unavailable" for item in rec.failures)


# ============ 先答后转：答复要收敛成业务结论，不能再教客户怎么转人工 ============

HANDOFF_HEAVY_REPLY = """您好，为您说明退货相关情况：
**我能否直接办理退货**
我无法直接执行退货、退款操作，需要通过退货流程或人工客服办理。
**店铺退货政策（供参考）**
需在签收后 7 天内发起退货，商品需保持吊牌完整、不影响二次销售。
质量问题退换货：往返运费由店铺承担。
**关于转人工**
您可以在对话中输入「转人工」，或通过官方客服热线、官方 App/官网的在线客服入口联系人工客服。
人工客服可能存在排队等待，高峰时段等待时间可能较长，请您耐心等待。"""


def test_answer_reply_drops_the_handoff_section() -> None:
    """下面就会自动建单，答复里再教"输入转人工/打热线"会让客户怀疑自己没转成。"""
    from business.chat.routing import tidy_handoff_reply

    cleaned = tidy_handoff_reply(HANDOFF_HEAVY_REPLY)

    assert "签收后 7 天" in cleaned and "往返运费" in cleaned, "业务结论必须留下"
    for guide in ("转人工", "热线", "App", "官网", "排队"):
        assert guide not in cleaned, f"转人工引导没清干净：{guide}"


def test_plain_reply_is_returned_unchanged() -> None:
    """没有转人工引导的答复一个字都不改——整理不能变成改写。"""
    from business.chat.routing import tidy_handoff_reply

    plain = "您好，为您查到以下订单：\n- 单号 A1｜耳机｜已签收｜11-01\n如需退换请在签收后 7 天内申请。"
    assert tidy_handoff_reply(plain) == plain


def test_reply_of_pure_guidance_collapses_to_empty() -> None:
    """整篇都是转人工引导时返回空串，让调用方退回"纯转人工"的单条系统提示。"""
    from business.chat.routing import tidy_handoff_reply

    assert tidy_handoff_reply("**关于转人工**\n您可以在对话中输入「转人工」，或拨打客服热线。") == ""


def test_handoff_turn_stores_tidied_answer_and_ticket_notice(monkeypatch) -> None:
    """接线检查：落库的答复是整理后的文本，工单号只出现在 notice 里。"""
    rec = Recorder()
    rec.install(monkeypatch, reply=HANDOFF_HEAVY_REPLY)
    worker.process_claim(row("给我退货，你是否可以，不可以给我转接人工"))

    commit = rec.committed[0]
    assert "签收后 7 天" in commit["answer"]
    assert "转人工" not in commit["answer"] and "热线" not in commit["answer"]
    assert "TK-1" in commit["notice"]
    assert commit["answer_role"] == "ai"

