"""MySQL 连接池与 Redis 客户端工厂。

单 Worker 时代 ``db()`` 每次调用都新建一条连接（一轮处理要建 6–8 次），代价被串行掩盖了。
``CHAT_TURN_WORKERS`` 调到多个之后，建连/断连会成为主要开销，也更容易撞到 MySQL 的
``max_connections``，所以这里换成一个小连接池。

池是**软上限**，不是硬闸：拿不到空闲连接时先等 ``MYSQL_POOL_TIMEOUT`` 秒，仍拿不到就临时
新建一条，归还时直接关闭。高峰期的正确降级是"变慢"，不是"报 500"。``MYSQL_POOL_SIZE=0``
可以退回原来的"每次新建"，用于排查连接泄漏。
"""

import logging
import os
import queue
import threading

import pymysql
import redis

logger = logging.getLogger("business.infra.storage")


def _connect():
    return pymysql.connect(
        host=os.environ["MYSQL_HOST"], port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"], password=os.environ["MYSQL_PASSWORD"],
        database=os.environ["MYSQL_DATABASE"], charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, autocommit=True,
        connect_timeout=3, read_timeout=5, write_timeout=5,
    )


def _env_number(name, default, low, high):
    try:
        return max(low, min(int(os.getenv(name, str(default))), high))
    except (TypeError, ValueError):
        return default


class _Pool:
    """LIFO 空闲表 + 计数信号量；不依赖 DBUtils，避免为这点需求加重镜像。"""

    def __init__(self, size, timeout):
        self.size = size
        self.timeout = timeout
        self._free = queue.LifoQueue()
        self._slots = threading.BoundedSemaphore(size)
        self._lock = threading.Lock()
        self._open = 0
        self._overflow = 0

    def _discard(self, conn):
        try:
            conn.close()
        except Exception:
            pass
        with self._lock:
            self._open -= 1

    def acquire(self):
        """返回 ``(conn, from_pool)``；``from_pool=False`` 表示这是一条溢出连接。"""
        if not self._slots.acquire(timeout=self.timeout):
            with self._lock:
                self._overflow += 1
                overflow = self._overflow
            if overflow == 1 or overflow % 50 == 0:
                logger.warning("mysql pool exhausted size=%d overflow=%d; "
                               "opening an extra connection", self.size, overflow)
            return _connect(), False
        try:
            conn = self._free.get_nowait()
        except queue.Empty:
            try:
                conn = _connect()
            except BaseException:
                self._slots.release()
                raise
            with self._lock:
                self._open += 1
            return conn, True
        try:
            conn.ping(reconnect=True)
        except Exception:
            # 空闲太久的连接（MySQL wait_timeout）在这里被换掉，而不是让业务查询报错
            self._discard(conn)
            try:
                conn = _connect()
            except BaseException:
                self._slots.release()
                raise
            with self._lock:
                self._open += 1
        return conn, True

    def release(self, conn, *, pooled: bool, reusable: bool):
        """``pooled`` 的连接占着一个槽位，溢出连接不占 —— 两者的归还路径不同。"""
        if pooled and reusable and getattr(conn, "open", False):
            self._free.put_nowait(conn)
        else:
            try:
                conn.close()
            except Exception:
                pass
            if pooled:
                with self._lock:
                    self._open -= 1
        if pooled:
            self._slots.release()

    def stats(self):
        with self._lock:
            return {"size": self.size, "open": self._open,
                    "free": self._free.qsize(), "overflow": self._overflow}


class _PooledConnection:
    """把 ``with db() as conn`` 的语义搬到池上：``close()`` 是归还，不是断开。

    ``pymysql.Connection.__exit__`` 的语义是"无异常就 commit，然后 close"，这里逐字
    保留：否则 ``rbac.transaction()`` 里显式 ``commit()`` 后再 ``close()`` 就变成了"提前
    断开"，池里也会留下未提交的事务。
    """

    __slots__ = ("_conn", "_pool", "_pooled", "_closed", "_broken")

    def __init__(self, conn, pool, pooled: bool):
        self._conn = conn
        self._pool = pool
        self._pooled = pooled
        self._closed = False
        self._broken = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                try:
                    self._conn.commit()
                except Exception:
                    self._broken = True
                    logger.warning("mysql commit failed; dropping the connection",
                                   exc_info=True)
            else:
                try:
                    self._conn.rollback()
                except Exception:
                    self._broken = True
        finally:
            self.close()
        return False

    def close(self):
        if self._closed:
            return
        self._closed = True
        # 归还前必须确认没有挂着的事务：否则下一个人会继承它，提交时把别人的活一起提交
        if not self._broken and getattr(self._conn, "_transaction", None):
            try:
                self._conn.rollback()
            except Exception:
                self._broken = True
                logger.warning("mysql rollback on release failed; dropping the connection",
                               exc_info=True)
        self._pool.release(self._conn, pooled=self._pooled,
                           reusable=not self._broken)

    def __getattr__(self, name):
        return getattr(self._conn, name)


_FALLBACK = object()
_pool_lock = threading.Lock()
_pool = _FALLBACK


def _get_pool():
    global _pool
    if _pool is not _FALLBACK:
        return _pool
    with _pool_lock:
        if _pool is _FALLBACK:
            size = _env_number("MYSQL_POOL_SIZE", 8, 0, 64)
            _pool = None if size == 0 else _Pool(
                size, _env_number("MYSQL_POOL_TIMEOUT", 5, 1, 60))
            if _pool is None:
                logger.info("mysql connection pool disabled (MYSQL_POOL_SIZE=0)")
    return _pool


def db():
    pool = _get_pool()
    if pool is None:
        return _connect()
    conn, pooled = pool.acquire()
    return _PooledConnection(conn, pool, pooled)


def pool_stats():
    """给 ``/metrics`` 用；池未启用时返回空。"""
    pool = _get_pool()
    return pool.stats() if pool is not None else {}


def reset_pool():
    """测试与进程重启用：丢掉空闲连接。"""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, _FALLBACK
    if pool not in (None, _FALLBACK):
        while True:
            try:
                pool._free.get_nowait().close()
            except queue.Empty:
                break
            except Exception:
                pass


def cache():
    return redis.Redis.from_url(os.environ["REDIS_URL"], socket_timeout=3)


def one(sql, args=()):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()


def many(sql, args=()):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def write(sql, args):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        # [ours] 返回受影响行数，供条件 UPDATE 做 CAS 判定；既有调用方均忽略返回值，零行为影响。
        return cur.rowcount
