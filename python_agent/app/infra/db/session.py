"""会话表校验、会话心跳、过期清理与清理锁（从 db_ops.py 拆出）。"""
import logging
from datetime import datetime, timedelta, timezone

import psycopg

from app.infra.db_pool import _pool_conn

logger = logging.getLogger("agent.db_ops")

_AGENT_TABLES = ("platform_settings", "session_meta", "prompts", "prompt_versions",
                 "kb_documents", "token_usage")


async def verify_agent_tables(conn: psycopg.AsyncConnection):
    """校验 agent 依赖的表已存在（由 init SQL 导入）；缺表抛错提示执行 SQL"""
    async with conn.cursor() as cur:
        missing = []
        for t in _AGENT_TABLES:
            await cur.execute("SELECT to_regclass(%s)", (t,))
            if (await cur.fetchone())[0] is None:
                missing.append(t)
        if missing:
            raise RuntimeError(
                "agent 依赖表缺失: "
                + ", ".join(missing)
                + "。请先导入 deploy/init/postgres/init.sql（docker compose 首次启动会自动执行）"
            )
        logger.info("agent 依赖表校验通过: %s", ", ".join(_AGENT_TABLES))



async def touch_session(conn: psycopg.AsyncConnection, thread_id: str):
    """更新会话最后活跃时间与消息计数"""
    tenant_id = thread_id.split(":", 1)[0] if ":" in thread_id else ""
    async with conn.cursor() as cur:
        await cur.execute("""
            INSERT INTO session_meta (thread_id, tenant_id, last_active_at, message_count)
            VALUES (%s, %s, now(), 1)
            ON CONFLICT (thread_id) DO UPDATE SET
                last_active_at = now(),
                message_count = session_meta.message_count + 1
        """, (thread_id, tenant_id))


# ====================== 瞬时段连错误识别与重试 ======================
# PG 重启 / 服务器重启 / 网络抖动瞬间，连接池中可能残留 closed/broken 的死连接：
# - 池级防御：AsyncConnectionPool(check=AsyncConnectionPool.check_connection)，借出前 ping 验证；
# - 业务级兜底：已借出连接在执行中断开（AdminShutdown / connection is closed）时，丢弃重取重试一次。
# 仅对"瞬时连接故障"重试（幂等写操作），业务错误（如约束冲突）不重试。
_TRANSIENT_DB_EXC = (
    psycopg.errors.AdminShutdown,      # SQLSTATE 57P01：管理员终止连接（PG 重启/terminate）
    psycopg.errors.ConnectionException,  # 08 类：连接异常（无法连接/已关闭）
    psycopg.OperationalError,          # 运行时连接错误（"the connection is closed" 等）
    psycopg.InterfaceError,            # 客户端侧连接状态错误
)


def is_transient_db_error(exc: BaseException) -> bool:
    """判断是否为可安全重试的瞬时数据库连接错误"""
    return isinstance(exc, _TRANSIENT_DB_EXC)


async def touch_session_retry(app, thread_id: str):
    """touch_session + 瞬时段连错误自动重试一次（upsert 幂等，重试安全）。
    用法：await touch_session_retry(request.app, thread_id)"""
    from app.infra.db_pool import _pool_conn
    try:
        async with _pool_conn(app) as conn:
            await touch_session(conn, thread_id)
    except BaseException as exc:
        if not is_transient_db_error(exc):
            raise
        logger.warning("touch_session 遇瞬时段连错误（%s），重取连接重试一次", exc.__class__.__name__)
        async with _pool_conn(app) as conn:
            await touch_session(conn, thread_id)


async def cleanup_sessions(conn: psycopg.AsyncConnection, checkpointer, days: int) -> dict:
    """回收超过 days 天未活跃的 LangGraph 会话：
    通过 session_meta 找到过期线程 → 调用 checkpointer.adelete_thread 清理 checkpoint → 删除 session_meta
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with conn.cursor() as cur:
        await cur.execute("SELECT thread_id FROM session_meta WHERE last_active_at < %s", (cutoff,))
        rows = await cur.fetchall()
    thread_ids = [r[0] for r in rows]

    deleted = 0
    for tid in thread_ids:
        try:
            await checkpointer.adelete_thread(tid)
            deleted += 1
        except Exception as e:
            logger.warning("delete thread %s failed: %s", tid, e)

    if thread_ids:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM session_meta WHERE thread_id = ANY(%s)", (thread_ids,))
    return {"scanned": len(thread_ids), "deleted": deleted, "threads": thread_ids}


_CLEANUP_LOCK_KEY = "cs:lock:session_cleanup"
_CLEANUP_LOCK_TTL = 300  # 秒，超时自动释放防死锁


async def _acquire_cleanup_lock(app) -> str | None:
    """[ours] S6 治理：持有者 token 锁（Lua 校验后再删）；Redis 不可用→本轮不执行

    返回 token 供 release 校验；fail-closed 语义与旧实现一致。"""
    from app.infra import redis_lock
    rc = getattr(getattr(app, "state", None), "redis_client", None)
    if rc is None:
        logger.warning("cleanup lock skipped: redis_client 未初始化")
        return None
    return await redis_lock.acquire_lock(rc, _CLEANUP_LOCK_KEY, _CLEANUP_LOCK_TTL)


async def _release_cleanup_lock(app, token=None) -> None:
    from app.infra import redis_lock
    rc = getattr(getattr(app, "state", None), "redis_client", None)
    if rc is None:
        return
    await redis_lock.release_lock(rc, _CLEANUP_LOCK_KEY, token)
