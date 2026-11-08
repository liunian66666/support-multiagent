"""清理函数的两条硬约束：双键删除、失败不删索引。

运行时不好造"某个检查点删失败"，所以用替身钉住：
* 新版图键 ``support_v2:{thread_id}`` 与旧审批残留的原始键都要删；
* 只有全部删成功的线程才进 ``DELETE FROM session_meta``；
* ``skip`` 里的线程一律保留，交给业务层下轮重试。
"""

from __future__ import annotations

import pytest

from app.infra.db.session import cleanup_sessions

pytestmark = pytest.mark.asyncio


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, sql, args=()):
        self.conn.executed.append((sql, args))

    async def fetchall(self):
        return self.conn.rows


class _FakeConn:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executed = []

    def cursor(self):
        return _FakeCursor(self)


class _FakeCheckpointer:
    def __init__(self, fail_on=()):
        self.deleted = []
        self.fail_on = set(fail_on)

    async def adelete_thread(self, key):
        if key in self.fail_on:
            raise RuntimeError("simulated delete failure")
        self.deleted.append(key)


async def test_deletes_both_namespaced_and_legacy_keys():
    conn, saver = _FakeConn(), _FakeCheckpointer()
    result = await cleanup_sessions(conn, saver, 7, thread_ids=["t_demo:buyer1:s1"])

    assert saver.deleted == ["support_v2:t_demo:buyer1:s1", "t_demo:buyer1:s1"]
    assert result["deleted_threads"] == ["t_demo:buyer1:s1"]
    assert result["failed"] == []


async def test_failed_delete_keeps_the_index_row():
    conn = _FakeConn()
    saver = _FakeCheckpointer(fail_on={"support_v2:t_demo:buyer1:broken"})
    result = await cleanup_sessions(
        conn, saver, 7, thread_ids=["t_demo:buyer1:ok", "t_demo:buyer1:broken"])

    assert result["deleted_threads"] == ["t_demo:buyer1:ok"]
    assert [item["thread_id"] for item in result["failed"]] == ["t_demo:buyer1:broken"]
    deletes = [args for sql, args in conn.executed if "DELETE FROM session_meta" in sql]
    assert deletes == [(["t_demo:buyer1:ok"],)], "只有删成功的线程才能离开索引"


async def test_skipped_threads_are_never_touched():
    conn, saver = _FakeConn(), _FakeCheckpointer()
    result = await cleanup_sessions(conn, saver, 7,
                                    thread_ids=["t_demo:buyer1:busy"],
                                    skip={"t_demo:buyer1:busy"})

    assert saver.deleted == [], "有进行中轮次/未完成工单的会话不能删检查点"
    assert result["deleted_threads"] == []
    assert result["failed"][0]["thread_id"] == "t_demo:buyer1:busy"
    assert not [sql for sql, _ in conn.executed if "DELETE FROM session_meta" in sql]


async def test_global_scan_uses_the_cutoff_and_reports_deleted_threads():
    conn = _FakeConn(rows=[("t_demo:buyer1:old",)])
    saver = _FakeCheckpointer()
    result = await cleanup_sessions(conn, saver, 7)

    assert result["scanned"] == 1
    assert result["deleted_threads"] == ["t_demo:buyer1:old"]
    selects = [args for sql, args in conn.executed if "SELECT thread_id" in sql]
    assert selects, "全局模式必须先按 last_active_at 扫出候选"
