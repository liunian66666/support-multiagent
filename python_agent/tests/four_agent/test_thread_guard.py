# ruff: noqa: RUF001  # 断言与文案含中文
"""跨进程锁的单元测试：锁号纯函数 + guard 的取放顺序 + 失败绝不静默。

真数据库上的"两个进程打同一线程"由 `python_agent/evaluation/four_agent/probe_cross_process_lock.py`
（`run_all.sh pg-concurrency`）在两个容器里实测；这里只锁住**语义**——
最容易悄悄坏掉的三件事：锁号算法变了（会串行化不相关的线程）、拿不到锁时降级成"先跑"、
异常路径没解锁（连接归还后会话锁残留 = 整条线程永久卡死）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.support.fakes import ScriptedSupportModel, offline_deps
from app.agent.support.service import LOCK_UNAVAILABLE_ANSWER, SupportService, thread_key
from app.agent.support.thread_guard import lock_number, pg_advisory_guard

# 本仓库没有配 asyncio_mode=auto：async 测试**必须**逐个带 @pytest.mark.asyncio，
# 否则 pytest 直接判 "async def functions are not natively supported"（写这个文件时先踩了一次）。
# 也不要图省事写模块级 pytestmark —— 那会让同文件里的同步测试收到一条 asyncio 警告。

TENANT, THREAD = "t_demo", "t_demo:guard-1"


def build_service(guard=None):
    deps, _model, _events, _captured = offline_deps(
        model=ScriptedSupportModel(supervisor_script=[
            AIMessage(content="您好，我可以帮您查订单或解释店铺政策。",
                      usage_metadata={"input_tokens": 8, "output_tokens": 6, "total_tokens": 14})]))

    async def deps_for_tenant(_tenant_id: str):
        return deps

    return SupportService(deps_for_tenant, checkpointer=InMemorySaver(), thread_guard=guard)


def test_lock_number_is_stable_signed_64bit_and_distinct() -> None:
    key = "support_v2:t_demo:buyer1"
    assert lock_number(key) == lock_number(key)
    assert -(1 << 63) <= lock_number(key) < (1 << 63)
    # 不相干的线程不该撞到同一个锁号（hashtext 是 32 位，几万键就会生日碰撞，这里必须更宽）
    assert len({lock_number(f"support_v2:t_demo:thread-{i}") for i in range(2000)}) == 2000


class RecordingConnection:
    """假 psycopg 连接：记下每条 SQL 与参数，方便断言顺序；可让某一步失败。"""

    def __init__(self, *, fail_on: str = "") -> None:
        self.calls: list[tuple[str, tuple | None]] = []
        self.closed = False
        self.fail_on = fail_on

    async def execute(self, sql: str, params=None):
        self.calls.append((sql, params))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError(f"lock boom: {sql}")
        return None

    async def close(self) -> None:
        self.closed = True


def make_guard(**kwargs):
    conns: list[RecordingConnection] = []

    async def factory(dsn, **opts):
        conn = RecordingConnection(**kwargs)
        conns.append(conn)
        return conn

    return pg_advisory_guard("dsn://unused", connect_factory=factory), conns


@pytest.mark.asyncio
async def test_guard_locks_runs_unlocks_in_order() -> None:
    guard, conns = make_guard()
    order: list[str] = []
    async with guard("support_v2:t_demo:buyer1") as _none:
        order.append("turn")
    assert order == ["turn"]
    sqls = [c[0] for c in conns[0].calls]
    assert sqls == ["SELECT set_config('lock_timeout', %s, false)",
                    "SELECT pg_advisory_lock(%s)",
                    "SELECT pg_advisory_unlock(%s)"]
    assert conns[0].calls[1][1] == (lock_number("support_v2:t_demo:buyer1"),)
    assert conns[0].closed is True          # 每轮独占连接用完归还，不常驻


@pytest.mark.asyncio
async def test_guard_unlocks_and_closes_even_when_the_body_raises() -> None:
    guard, conns = make_guard()
    with pytest.raises(ValueError):
        async with guard("support_v2:t_demo:x"):
            raise ValueError("graph blew up mid-turn")
    sqls = [c[0] for c in conns[0].calls]
    assert "SELECT pg_advisory_unlock(%s)" in sqls
    assert conns[0].closed is True


@pytest.mark.asyncio
async def test_guard_fails_loud_instead_of_running_unprotected() -> None:
    """拿不到锁时必须抛 ⇒ run_turn 的降级路径会把这一轮记成带原因的 error。

    "先跑再说"的写法会让丢轮率看着下降、实际只是没人看见——这条钉住它不许退化成装饰。
    """
    guard, conns = make_guard(fail_on="pg_advisory_lock")
    ran = False
    with pytest.raises(RuntimeError):
        async with guard("support_v2:t_demo:y"):
            ran = True
    assert ran is False
    assert conns[0].closed is True           # 失败也要归还连接，否则会漏会话


@pytest.mark.asyncio
async def test_service_takes_the_guard_with_the_thread_key_and_releases_it() -> None:
    seen: list[str] = []

    @asynccontextmanager
    async def guard(key: str):
        seen.append(f"enter:{key}")
        yield
        seen.append(f"exit:{key}")

    service = build_service(guard=guard)
    result = await service.run_turn(TENANT, THREAD, " ORD-1001 到哪了？", turn_id="g-1")
    assert result.outcome != "" and result.answer
    assert seen == [f"enter:{thread_key(TENANT, THREAD)}", f"exit:{thread_key(TENANT, THREAD)}"]


@pytest.mark.asyncio
async def test_service_without_guard_is_unchanged() -> None:
    """默认路径（没接跨进程锁）必须与今天完全一致——这条钉住"加了参数就改了语义"。"""
    service = build_service()
    first = await service.run_turn(TENANT, THREAD, "退货政策？", turn_id="g-2")
    replay = await service.run_turn(TENANT, THREAD, "退货政策？", turn_id="g-2")
    history = await service.history(TENANT, THREAD)
    assert first.answer == replay.answer and len(history) == 2


@pytest.mark.asyncio
async def test_guard_failure_yields_a_safe_answer_instead_of_running_unprotected() -> None:
    @asynccontextmanager
    async def broken_guard(_key: str):
        raise TimeoutError("could not acquire advisory lock")
        yield                                        # noqa: B018 —— 让上面那行真的先执行

    service = build_service(guard=broken_guard)
    result = await service.run_turn(TENANT, THREAD, "退货政策？", turn_id="g-3")
    assert result.outcome == "lock_unavailable"
    assert result.answer == LOCK_UNAVAILABLE_ANSWER and result.answer.strip()
    assert "TimeoutError" in result.error
    # 取不到锁 ⇒ 这一轮根本没进图：检查点里不该留下这条问句
    assert await service.history(TENANT, THREAD) == []


def test_entrypoint_wires_the_guard_only_when_switch_and_dsn_are_both_present(monkeypatch) -> None:
    """`SUPPORT_V2_THREAD_LOCK` 是"要起第二个进程"的显式声明，缺省必须为关。

    两个条件缺一不可：只有开关没有 DSN ⇒ 无处可锁；只有 DSN 没开关 ⇒ 单进程部署
    白白为每轮多开一条连接。两种组合都必须回 None（也就是今天的语义）。
    """
    from app.agent.support import thread_guard

    monkeypatch.delenv("SUPPORT_V2_THREAD_LOCK", raising=False)
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://u:p@h/db")
    assert thread_guard.guard_from_env() is None

    monkeypatch.setenv("SUPPORT_V2_THREAD_LOCK", "on")
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    assert thread_guard.guard_from_env() is None

    monkeypatch.setenv("POSTGRES_DSN", "postgresql://u:p@h/db")
    assert callable(thread_guard.guard_from_env())
