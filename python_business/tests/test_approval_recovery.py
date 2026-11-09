# 中文文案必然包含全角标点
"""`/agent/resume` 客户端与恢复待办的状态机（不需要 MySQL / 不需要 Agent）。

这里钉的是"恢复失败不会被当成恢复成功"的每一条分支：只看 ``content`` 的旧实现会在
``success=false``、503、超时这三种情况下都返回空串，然后被当作"已恢复"继续往下走。
"""

from __future__ import annotations

import httpx
import pytest

from business.chat import recovery


class FakeResponse:
    def __init__(self, status_code: int = 200, body=None, *, bad_json: bool = False):
        self.status_code = status_code
        self._body = {} if body is None else body
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._body


class FakeAgent:
    """按脚本回答探测与恢复；记录调用次数，便于断言"没有白跑一轮"。"""

    def __init__(self, *, state="pending", resume=None):
        self.state = state
        self.resume = resume or FakeResponse(200, {"success": True, "content": "已恢复"})
        self.gets = 0
        self.posts = 0

    def get(self, *_args, **_kwargs):
        self.gets += 1
        return FakeResponse(200, {"approval_state": self.state})

    def post(self, *_args, **_kwargs):
        self.posts += 1
        if isinstance(self.resume, Exception):
            raise self.resume
        return self.resume


@pytest.fixture()
def agent(monkeypatch):
    fake = FakeAgent()
    monkeypatch.setattr(recovery.httpx, "get", fake.get)
    monkeypatch.setattr(recovery.httpx, "post", fake.post)
    monkeypatch.setattr(recovery, "record", lambda *_a, **_kw: None)
    return fake


def test_recovered_carries_the_answer(agent) -> None:
    result = recovery.recover_thread("t_demo:s1")
    assert result.outcome is recovery.ResumeOutcome.RECOVERED
    assert result.content == "已恢复"
    assert result.settled and not result.retryable
    assert agent.posts == 1


def test_success_false_no_pending_is_clear(agent) -> None:
    agent.resume = FakeResponse(200, {"success": False, "error_code": "no_pending_approval"})
    result = recovery.recover_thread("t_demo:s1")

    assert result.outcome is recovery.ResumeOutcome.CLEAR
    assert result.settled
    assert result.error_code == "no_pending_approval"


def test_already_clear_never_calls_resume(agent) -> None:
    """重试路径的核心：探测到 clear 就直接结案，不再白跑一轮旧图。"""
    agent.state = "clear"
    result = recovery.recover_thread("t_demo:s1")

    assert result.outcome is recovery.ResumeOutcome.CLEAR
    assert agent.posts == 0


def test_unknown_probe_never_resumes(agent) -> None:
    agent.state = "unknown"
    result = recovery.recover_thread("t_demo:s1")

    assert result.outcome is recovery.ResumeOutcome.UNKNOWN
    assert agent.posts == 0, "状态未知时不能猜着恢复"


def test_503_is_unknown_not_success(agent) -> None:
    agent.resume = FakeResponse(503, {"detail": {"error_code": "approval_state_unknown"}})
    result = recovery.recover_thread("t_demo:s1")

    assert result.outcome is recovery.ResumeOutcome.UNKNOWN
    assert result.retryable
    assert result.content == ""


def test_http_500_is_unavailable(agent) -> None:
    agent.resume = FakeResponse(500, {})
    result = recovery.recover_thread("t_demo:s1")
    assert result.outcome is recovery.ResumeOutcome.UNAVAILABLE


def test_resume_without_success_flag_is_not_recovered(agent) -> None:
    """老客户端只看 content —— 一个没有 success 的 200 也可能带正文。"""
    agent.resume = FakeResponse(200, {"content": "看起来像回答"})
    result = recovery.recover_thread("t_demo:s1")

    assert result.outcome is recovery.ResumeOutcome.UNKNOWN


def test_bad_json_is_unknown(agent) -> None:
    agent.resume = FakeResponse(200, {}, bad_json=True)
    assert recovery.recover_thread("t_demo:s1").outcome is recovery.ResumeOutcome.UNKNOWN


def test_timeout_then_clear_means_the_server_already_did_it(agent, monkeypatch) -> None:
    """服务端已恢复、客户端超时：复查即可结案，不能报"恢复成功"，也不能重试。"""
    agent.resume = httpx.ReadTimeout("timeout")
    calls = {"n": 0}

    def get(*_args, **_kwargs):
        calls["n"] += 1
        # 第一次探测 pending，超时后复查 clear
        state = "pending" if calls["n"] == 1 else "clear"
        return FakeResponse(200, {"approval_state": state})

    monkeypatch.setattr(recovery.httpx, "get", get)
    result = recovery.recover_thread("t_demo:s1")

    assert result.outcome is recovery.ResumeOutcome.RECOVERED
    assert result.detail["verified_after_failure"] is True
    assert agent.posts == 1
    assert calls["n"] == 2


def test_timeout_while_still_pending_is_unavailable(agent) -> None:
    agent.resume = httpx.ReadTimeout("timeout")
    result = recovery.recover_thread("t_demo:s1")

    assert result.outcome is recovery.ResumeOutcome.UNAVAILABLE
    assert result.detail.get("timeout") is True


def test_transport_error_is_unavailable(agent) -> None:
    agent.resume = httpx.ConnectError("boom")
    assert recovery.recover_thread("t_demo:s1").outcome is recovery.ResumeOutcome.UNAVAILABLE


def test_probe_transport_error_is_unknown(agent, monkeypatch) -> None:
    def boom(*_a, **_kw):
        raise httpx.ConnectError("probe down")

    monkeypatch.setattr(recovery.httpx, "get", boom)
    assert recovery.probe_state("t_demo:s1") == "unknown"


# ------------------------- 待办记账 -------------------------

class FakeLedger:
    def __init__(self, attempts: int = 0):
        self.attempts = attempts
        self.pending: list[tuple] = []
        self.settled: list[tuple] = []

    def install(self, monkeypatch):
        monkeypatch.setattr(recovery, "_store_attempts", lambda _id: self.attempts)
        monkeypatch.setattr(recovery, "_store_mark_pending",
                            lambda ticket_id, code, attempts: self.pending.append(
                                (ticket_id, code, attempts)))
        monkeypatch.setattr(recovery, "_store_mark_settled",
                            lambda ticket_id, outcome: self.settled.append((ticket_id, outcome)))


def test_settled_result_clears_the_todo(monkeypatch) -> None:
    ledger = FakeLedger()
    ledger.install(monkeypatch)
    recovery.record(7, recovery.ResumeResult(recovery.ResumeOutcome.RECOVERED))
    assert ledger.settled == [(7, "recovered")]
    assert ledger.pending == []


def test_failed_result_is_written_as_a_todo_with_backoff(monkeypatch) -> None:
    ledger = FakeLedger(attempts=2)
    ledger.install(monkeypatch)
    recovery.record(7, recovery.ResumeResult(recovery.ResumeOutcome.UNKNOWN,
                                             error_code="resume_timeout"))
    assert ledger.pending == [(7, "resume_timeout", 3)]
    assert ledger.settled == []


def test_record_never_raises_when_the_database_is_gone(monkeypatch) -> None:
    """记账失败不能把已经发生的恢复结果吞掉。"""
    def boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(recovery, "_store_mark_settled", boom)
    recovery.record(7, recovery.ResumeResult(recovery.ResumeOutcome.CLEAR))


def test_recover_due_counts_and_continues_after_one_failure(monkeypatch) -> None:
    monkeypatch.setattr(recovery, "claim_due",
                        lambda limit=20: [{"id": 1, "ticket_no": "TK-1"},
                                          {"id": 2, "ticket_no": "TK-2"}])

    def recover_for_ticket(ticket, **_kw):
        if ticket["id"] == 1:
            raise RuntimeError("boom")
        return recovery.ResumeResult(recovery.ResumeOutcome.RECOVERED)

    monkeypatch.setattr(recovery, "recover_for_ticket", recover_for_ticket)
    stats = recovery.recover_due()

    assert stats == {"claimed": 2, "recovered": 1, "still_pending": 1}


def test_the_resident_loop_runs_the_recovery_pass(monkeypatch) -> None:
    """容器重启后靠这个常驻循环续做：第一圈必须先跑一次恢复，而不是等客户发消息。"""
    from business.chat import worker

    calls: list[str] = []
    monkeypatch.setattr(worker, "run_once",
                        lambda *_a, **_k: calls.append("turn") or False)
    monkeypatch.setattr(worker, "queue_stats",
                        lambda: {"queued": 0, "running": 0, "stale_running": 0,
                                 "retried": 0, "oldest_queued_seconds": 0.0})
    monkeypatch.setattr(recovery, "recover_due",
                        lambda: calls.append("recover") or
                        {"claimed": 0, "recovered": 0, "still_pending": 0})

    def stop(_seconds):
        raise SystemExit

    monkeypatch.setattr(worker.time, "sleep", stop)
    with pytest.raises(SystemExit):
        worker.run_loop()

    assert calls[:2] == ["recover", "turn"]
    assert worker._recovery_pass  # 选主逻辑本身由 test_worker_concurrency 覆盖
