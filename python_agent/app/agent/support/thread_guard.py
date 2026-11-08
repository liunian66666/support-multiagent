# 中文文案必然包含全角标点
"""跨进程轮次串行化：一把按 thread key 取的 Postgres advisory 锁。

为什么需要它。`SupportService` 原本用 `dict[key] -> asyncio.Lock()` 串行化同一线程的轮次，
`asyncio.Lock` 只在**一个进程内**有效。生产一旦起第二个 worker（uvicorn `--workers 2`、
compose 副本、或 k8s 多 Pod），两个进程各自持有自己的锁，会同时读同一份检查点、各自写完再覆盖：
实测账本里那条"跨进程并发写同一线程会丢轮"（报告 §5e 的 M10/M11）就是这个 —— 而且
`agent/graph.py` 与挂载图**两个实现都会丢**，所以它不是挂载引入的缺陷，是平台层的。
检查点写入本身是原子的（单条 UPSERT），原子写入不等于**读-改-写**不丢：
两边的 messages 通道各自从同一旧快照算出"追加一条"，后写的那个把前一个覆盖了。

为什么用 advisory lock 而不是 `SELECT ... FOR UPDATE` 读检查点：
后者要把整个 ainvoke 的事务包进来，而 LangGraph 的检查点是每次 super-step 各写一行、
不在一个事务里跑完整轮（官方文档：`aput` 是每个 checkpoint 一次）；把轮次圈进一个数据库事务
既不现实也不官方。advisory lock 是 PostgreSQL 给"应用级互斥"留的正门，官方语义清楚：
会话级锁在显式 unlock 或连接断开时释放，所以连接必须**独占**且活得比这一轮长。

三个刻意的设计决定：

1. **锁号自己算，不用 `hashtext()`。** `hashtext(text)` 返回 32 位整数，几万线程就会生日碰撞；
   撞上的是"两个不相关的客户线程被串行化"这种偶发全局卡死，比丢轮更难查。这里用 blake2b
   摘要折成 64 位有符号整数（`pg_advisory_lock` 的 bigint 域），碰撞概率可忽略，且跨 PG 版本稳定。
2. **失败不静默。** 连不上数据库 / `lock_timeout` 到点 ⇒ 直接抛，让 `run_turn` 现有的降级路径
   把这一轮记成带原因的 error 答复。**绝不"拿不到锁就先跑"** —— 那等于把这条锁变成装饰，
   而且下一次测量会告诉你"丢轮率降了"，其实只是没人看见。
3. **锁的粒度是一轮，不是一条消息。** 摘要水位线、ledger、usage 都在轮次级别上做读-改-写，
   锁只盖住 ainvoke 这一段；`history()` / `memory_state()` 这些只读路径不取锁。
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

DEFAULT_LOCK_TIMEOUT = "30s"

GUARD_ENV_FLAG = "SUPPORT_V2_THREAD_LOCK"


def guard_from_env() -> Any:
    """按环境装配跨进程锁：`SUPPORT_V2_THREAD_LOCK=on` + `POSTGRES_DSN` 才生效。

    默认关 ⇒ 与单进程部署的今天逐字一致。开它的唯一时机是"要起第二个 worker /
    第二个副本"：那时 `asyncio.Lock` 管不到对面进程，同一线程的轮次会互相覆盖。
    这是**唯一**一处读该开关的地方，生产入口与调试入口都从这里取，避免两份语义。
    """
    if os.getenv(GUARD_ENV_FLAG, "").strip().lower() not in {"on", "true", "1"}:
        return None
    dsn = os.getenv("POSTGRES_DSN") or ""
    if not dsn:
        return None
    return pg_advisory_guard(dsn)


def lock_number(key: str) -> int:
    """thread key → pg advisory lock 的 bigint（有符号 64 位）。纯函数，可单独测。"""
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value - (1 << 63) if value >= (1 << 63) else value


def pg_advisory_guard(dsn: str, *, lock_timeout: str = DEFAULT_LOCK_TIMEOUT,
                      connect_factory: Any = None):
    """返回 `async with guard(thread_key): ...` 用的工厂函数（`SupportService(thread_guard=...)`）。

    `connect_factory` 只为测试留出注入位（真机上就是 `psycopg.AsyncConnection.connect`）；
    生产不传，函数内**延迟 import** psycopg，好让 InMemory 那条路（测试、离线探针）
    不需要装 psycopg 也能构造服务。
    """
    @asynccontextmanager
    async def guard(key: str) -> AsyncIterator[None]:
        factory = connect_factory
        if factory is None:                      # 延迟 import：没配持久后端时不该要求 psycopg
            import psycopg  # noqa: PLC0415  (官方异步入口)
            factory = psycopg.AsyncConnection.connect
        conn = await factory(dsn, autocommit=True)
        try:
            # 会话级设置：锁排队超过 lock_timeout 就报错，而不是让一个卡死的 worker 拖住整个线程。
            # 走 set_config(第三个参数 false = 会话级)而不是拼 SQL —— `SET x = $1` 不支持占位符，
            # 拼字符串会把"超时值来自配置"这件事变成注入面。
            await conn.execute("SELECT set_config('lock_timeout', %s, false)", (lock_timeout,))
            await conn.execute("SELECT pg_advisory_lock(%s)", (lock_number(key),))
            try:
                yield
            finally:
                # 显式解锁放在同一连接上；即使这段抛了，关闭连接也会让服务端回收会话锁。
                await conn.execute("SELECT pg_advisory_unlock(%s)", (lock_number(key),))
        finally:
            await conn.close()

    return guard
