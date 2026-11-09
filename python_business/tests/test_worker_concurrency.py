"""多 Worker 的三件护栏：租约心跳、恢复选主、连接池。

全部是纯内存替身，不需要 MySQL / Redis —— 真库上的语义（``SKIP LOCKED``、
``NOT EXISTS``、``ALTER TABLE``）在 ``test_durable_turns.py`` 里由 ``CHAT_TURN_DB_TEST=1``
的门槛覆盖，这里只钉住"进程内多线程"这一层。
"""

import threading
import time

import pytest

from business.chat import recovery, worker
from business.infra import storage


# ---------------------------------------------------------------------------
# 租约心跳
# ---------------------------------------------------------------------------

def test_lease_keeper_renews_until_the_turn_finishes(monkeypatch) -> None:
    beats: list[str] = []
    monkeypatch.setattr(worker, "heartbeat", lambda row: beats.append(row["turn_id"]) or True)

    row = {"turn_id": "turn-1"}
    with worker.LeaseKeeper(row, interval=0.01) as keeper:
        time.sleep(0.06)

    assert len(beats) >= 2, "处理期间必须持续续租，否则长任务会被别人领走"
    assert keeper.lost is False
    after = len(beats)
    time.sleep(0.05)
    assert len(beats) == after, "退出后必须停掉心跳线程"


def test_lease_keeper_stops_once_the_lease_is_gone(monkeypatch) -> None:
    """``rowcount=0`` 说明这轮已经归别人：立刻停手，别再续租下去。"""
    calls: list[int] = []

    def gone(_row):
        calls.append(1)
        return False

    monkeypatch.setattr(worker, "heartbeat", gone)
    with worker.LeaseKeeper({"turn_id": "turn-2"}, interval=0.01) as keeper:
        time.sleep(0.08)

    assert keeper.lost is True
    assert len(calls) == 1, "丢租约后不该继续打数据库"


def test_lease_keeper_survives_a_broken_heartbeat(monkeypatch) -> None:
    """数据库抖一下不等于丢租约：记日志继续，真丢了由 complete_turn 兜底。"""
    attempts: list[int] = []

    def flaky(_row):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("db blip")
        return True

    monkeypatch.setattr(worker, "heartbeat", flaky)
    with worker.LeaseKeeper({"turn_id": "turn-3"}, interval=0.01) as keeper:
        time.sleep(0.06)

    assert keeper.lost is False
    assert len(attempts) >= 2


def test_lease_keeper_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setattr(worker, "heartbeat",
                        lambda _row: pytest.fail("interval=0 时不应该有心跳"))
    with worker.LeaseKeeper({"turn_id": "turn-4"}, interval=0) as keeper:
        time.sleep(0.03)
    assert keeper.lost is False


def test_run_once_keeps_the_lease_alive_for_a_slow_turn(monkeypatch) -> None:
    """一轮跑得比租约还久也不会被回收：处理期间有心跳。"""
    beats: list[int] = []
    row = {"turn_id": "turn-5", "tenant_id": "t_demo", "user_message_id": 1,
           "attempt_count": 1, "lease_token": "x"}
    monkeypatch.setattr(worker, "fail_expired_leases", lambda: [])
    monkeypatch.setattr(worker, "claim_next", lambda: row)
    monkeypatch.setattr(worker, "notify", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "heartbeat", lambda _row: beats.append(1) or True)
    monkeypatch.setattr(worker, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(worker, "process_claim", lambda _row: time.sleep(0.06))

    assert worker.run_once(3) is True
    assert len(beats) >= 2


# ---------------------------------------------------------------------------
# 僵尸租约回收的降频
# ---------------------------------------------------------------------------

def test_expired_lease_sweep_is_throttled_across_threads(monkeypatch) -> None:
    """N 个 Worker 共用进程时，不该每个线程每秒都扫一遍同一张表。"""
    calls: list[int] = []
    monkeypatch.setattr(worker, "fail_expired_leases", lambda: calls.append(1) or [])
    monkeypatch.setattr(worker, "_last_reclaim", 0.0)
    monkeypatch.setenv("CHAT_TURN_RECLAIM_SECONDS", "60")

    worker._reclaim_expired()
    worker._reclaim_expired()
    worker._reclaim_expired()

    assert len(calls) == 1
    worker._reclaim_expired(force=True)
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# 恢复扫描选主
# ---------------------------------------------------------------------------

class _FakeLock:
    def __init__(self, acquired: bool):
        self._acquired = acquired
        self.released = 0

    def acquire(self, blocking=False):
        return self._acquired

    def release(self):
        self.released += 1


class _FakeRedis:
    def __init__(self, acquired: bool):
        self.lock_object = _FakeLock(acquired)
        self.names: list[str] = []

    def lock(self, name, **_kwargs):
        self.names.append(name)
        return self.lock_object


def test_only_the_leader_runs_the_recovery_pass(monkeypatch) -> None:
    """抢到锁的线程做恢复；没抢到的直接返回，不空转抢同一批工单。"""
    client = _FakeRedis(acquired=True)
    monkeypatch.setattr(worker, "cache", lambda: client)
    monkeypatch.setattr(recovery, "recover_due",
                        lambda: {"claimed": 1, "recovered": 1, "still_pending": 0})

    assert worker._recovery_pass(15) == {"claimed": 1, "recovered": 1, "still_pending": 0}
    assert client.lock_object.released == 1, "做完必须放锁，否则下一圈没人能接"

    monkeypatch.setattr(worker, "cache", lambda: _FakeRedis(acquired=False))
    assert worker._recovery_pass(15) is None


def test_recovery_still_runs_when_redis_is_down(monkeypatch) -> None:
    """Redis 挂了宁可重复扫描：漏扫会让工单永远卡住，重复扫只是浪费。"""
    def broken():
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(worker, "cache", broken)
    monkeypatch.setattr(recovery, "recover_due",
                        lambda: {"claimed": 0, "recovered": 0, "still_pending": 0})

    assert worker._recovery_pass(15) == {"claimed": 0, "recovered": 0, "still_pending": 0}


def test_recovery_pass_releases_the_lock_on_failure(monkeypatch) -> None:
    client = _FakeRedis(acquired=True)
    monkeypatch.setattr(worker, "cache", lambda: client)

    def boom():
        raise RuntimeError("agent down")

    monkeypatch.setattr(recovery, "recover_due", boom)
    with pytest.raises(RuntimeError):
        worker._recovery_pass(15)
    assert client.lock_object.released == 1


def test_queue_snapshot_is_logged_only_when_there_is_work(monkeypatch, caplog) -> None:
    monkeypatch.setattr(worker, "_queue_logged", 0.0)
    monkeypatch.setenv("CHAT_QUEUE_STATS_SECONDS", "1")
    monkeypatch.setattr(worker, "queue_stats",
                        lambda: {"queued": 9, "running": 4, "stale_running": 0,
                                 "retried": 1, "oldest_queued_seconds": 42.0})
    with caplog.at_level("INFO", logger="business.chat.worker"):
        worker._log_queue(2)
    assert "oldest_queued=42.0s" in caplog.text
    assert "worker=2" in caplog.text


# ---------------------------------------------------------------------------
# 连接池
# ---------------------------------------------------------------------------

class _FakeConn:
    def __init__(self):
        self.open = True
        self.pings = 0
        self.commits = 0
        self.rollbacks = 0

    def ping(self, reconnect=False):
        self.pings += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.open = False


@pytest.fixture
def pool(monkeypatch):
    storage.reset_pool()
    monkeypatch.delenv("MYSQL_POOL_SIZE", raising=False)
    monkeypatch.delenv("MYSQL_POOL_TIMEOUT", raising=False)
    yield
    storage.reset_pool()


def _install_fake_connect(monkeypatch, factory=None):
    made: list[_FakeConn] = []

    def connect():
        conn = (factory or _FakeConn)()
        made.append(conn)
        return conn

    monkeypatch.setattr(storage, "_connect", connect)
    return made


def test_pool_reuses_connections(pool, monkeypatch) -> None:
    made = _install_fake_connect(monkeypatch)
    monkeypatch.setenv("MYSQL_POOL_SIZE", "2")

    with storage.db():
        pass
    with storage.db():
        pass

    assert len(made) == 1, "第二条查询应复用池里的连接，而不是重新建连"
    assert made[0].pings == 1
    assert storage.pool_stats() == {"size": 2, "open": 1, "free": 1, "overflow": 0}


def test_pool_replaces_a_connection_that_died_while_idle(pool, monkeypatch) -> None:
    """MySQL wait_timeout 关掉的空闲连接，应该在借出时被换掉而不是让查询报错。"""
    made = _install_fake_connect(monkeypatch)
    monkeypatch.setenv("MYSQL_POOL_SIZE", "1")

    with storage.db():
        pass
    made[0].open = False

    def ping_and_die(self, reconnect=False):
        raise RuntimeError("server has gone away")

    monkeypatch.setattr(_FakeConn, "ping", ping_and_die, raising=False)
    with storage.db():
        pass
    assert len(made) == 2


def test_pool_overflows_instead_of_failing(pool, monkeypatch) -> None:
    """池满时走"临时新建 + 归还即关闭"，是变慢而不是 500。"""
    made = _install_fake_connect(monkeypatch)
    monkeypatch.setenv("MYSQL_POOL_SIZE", "1")
    monkeypatch.setenv("MYSQL_POOL_TIMEOUT", "1")
    storage._get_pool().timeout = 0.01

    first = storage.db()
    second = storage.db()
    assert len(made) == 2
    assert storage.pool_stats()["overflow"] == 1

    second.close()          # 溢出连接不占槽位，归还即关闭
    assert made[1].open is False
    first.close()

    with storage.db():
        pass
    assert storage.pool_stats()["overflow"] == 1


def test_a_failed_commit_drops_the_connection(pool, monkeypatch) -> None:
    """提交失败说明连接已经不可信，不能塞回池里让下一个人踩。"""
    class BadCommit(_FakeConn):
        def commit(self):
            raise RuntimeError("connection reset")

    _install_fake_connect(monkeypatch, BadCommit)
    monkeypatch.setenv("MYSQL_POOL_SIZE", "1")

    with storage.db():
        pass

    stats = storage.pool_stats()
    assert stats["free"] == 0 and stats["open"] == 0


def test_returning_a_connection_with_an_open_transaction_rolls_back(pool, monkeypatch) -> None:
    """池里不能留一条"还挂着事务"的连接：下一个人一提交就把别人的活带走了。"""
    made = _install_fake_connect(monkeypatch)
    monkeypatch.setenv("MYSQL_POOL_SIZE", "1")

    with storage.db() as conn:
        conn._conn._transaction = True      # 模拟"begin 了但没结账"

    assert made[0].rollbacks == 1
    assert storage.pool_stats()["free"] == 1


def test_pool_can_be_disabled(pool, monkeypatch) -> None:
    made = _install_fake_connect(monkeypatch)
    monkeypatch.setenv("MYSQL_POOL_SIZE", "0")

    conn = storage.db()
    assert conn is made[0], "关闭池时应退回原来的「每次新建」"
    assert storage.pool_stats() == {}


def test_pool_stats_tolerate_a_bad_size(pool, monkeypatch) -> None:
    _install_fake_connect(monkeypatch)
    monkeypatch.setenv("MYSQL_POOL_SIZE", "not-a-number")
    assert storage.pool_stats()["size"] == 8


def test_worker_count_is_clamped(monkeypatch) -> None:
    import maintenance

    monkeypatch.setenv("CHAT_TURN_WORKERS", "3")
    assert maintenance._worker_count() == 3
    monkeypatch.setenv("CHAT_TURN_WORKERS", "999")
    assert maintenance._worker_count() == 16
    monkeypatch.setenv("CHAT_TURN_WORKERS", "0")
    assert maintenance._worker_count() == 1
    monkeypatch.setenv("CHAT_TURN_WORKERS", "")
    assert maintenance._worker_count() == 4


def test_run_loop_passes_its_worker_id(monkeypatch) -> None:
    """worker id 不参与语义，但必须传下去——否则日志里查不到是哪条线程跑的这轮。"""
    seen: list[int] = []
    monkeypatch.setattr(worker, "_recovery_pass", lambda _interval: None)
    monkeypatch.setattr(worker, "_log_queue", lambda _worker_id: None)

    def fake_run_once(worker_id=0):
        seen.append(worker_id)
        raise SystemExit

    monkeypatch.setattr(worker, "run_once", fake_run_once)
    with pytest.raises(SystemExit):
        worker.run_loop(5)
    assert seen == [5]


def test_two_workers_run_different_sessions_concurrently(monkeypatch) -> None:
    """两个 Worker 必须能同时在跑：这是把线程数调上去的唯一意义。

    用"峰值并发数"而不是耗时来断言，避免 CI 机器慢一点就假失败。
    """
    pending = [{"turn_id": "t-a", "tenant_id": "t_demo", "user_message_id": 1,
                "attempt_count": 1, "lease_token": "a"},
               {"turn_id": "t-b", "tenant_id": "t_demo", "user_message_id": 2,
                "attempt_count": 1, "lease_token": "b"}]
    guard = threading.Lock()
    state = {"active": 0, "peak": 0}

    def claim():
        with guard:
            return pending.pop(0) if pending else None

    def process(_row):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.2)
        with guard:
            state["active"] -= 1

    monkeypatch.setattr(worker, "claim_next", claim)
    monkeypatch.setattr(worker, "fail_expired_leases", lambda: [])
    monkeypatch.setattr(worker, "notify", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "heartbeat", lambda _row: True)
    monkeypatch.setattr(worker, "process_claim", process)

    threads = [threading.Thread(target=worker.run_once, args=(index,))
               for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert state["peak"] == 2, "两个 Worker 各领一轮时必须真的并行，而不是排成一列"
