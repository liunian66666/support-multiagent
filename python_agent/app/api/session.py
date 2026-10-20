"""会话统计与手动清理（从 main.py 拆出）。"""
import logging

from fastapi import APIRouter, Request

from app.infra.config import SESSION_CLEANUP_INTERVAL, SESSION_RETENTION_DAYS
from app.infra.context import trace_id_var
from app.infra.db.session import cleanup_sessions
from app.infra.db_pool import _pool_conn
from app.schemas import SessionCleanupReq

logger = logging.getLogger("agent")
router = APIRouter()


@router.post("/agent/session/cleanup")
async def session_cleanup(req: SessionCleanupReq, request: Request):
    trace_id_var.set(request.headers.get("X-Trace-Id", "-"))
    try:
        async with _pool_conn(request.app) as conn:
            result = await cleanup_sessions(conn, request.app.state.checkpointer, req.days)
        logger.info("manual session cleanup: %s", result)
        return {"code": 0, "data": result}
    except Exception as e:
        logger.exception("/agent/session/cleanup error")
        return {"code": 500, "msg": str(e)}


@router.get("/agent/session/stats")
async def session_stats(request: Request):
    async with _pool_conn(request.app) as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT count(*), COALESCE(min(last_active_at), now()), COALESCE(max(last_active_at), now()),
                       COALESCE(sum(message_count), 0)
                FROM session_meta
            """)
            row = await cur.fetchone()
            await cur.execute("SELECT count(*) FROM checkpoints")
            cp = await cur.fetchone()
            await cur.execute(
                "SELECT count(*) FROM session_meta WHERE last_active_at < now() - make_interval(days => %s)",
                (SESSION_RETENTION_DAYS,))
            expired = await cur.fetchone()
    return {"code": 0, "data": {
        "sessions": row[0],
        "oldest_active": row[1].isoformat(),
        "newest_active": row[2].isoformat(),
        "total_messages": int(row[3]),
        "checkpoint_rows": int(cp[0]),
        "retention_days": SESSION_RETENTION_DAYS,
        "cleanup_interval": SESSION_CLEANUP_INTERVAL,
        "expired_sessions": int(expired[0]),
    }}


@router.get("/agent/session/cleanup-count")
async def session_cleanup_count(request: Request):
    """查询指定天数内可清理的过期会话数（前端按钮旁展示预计数量）"""
    try:
        days = int(request.query_params.get("days", "30"))
        if days <= 0 or days > 3650:
            days = 30
        async with _pool_conn(request.app) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM session_meta WHERE last_active_at < now() - make_interval(days => %s)",
                    (days,))
                row = await cur.fetchone()
        return {"code": 0, "data": {"count": int(row[0]), "days": days}}
    except Exception as e:
        logger.exception("/agent/session/cleanup-count error")
        return {"code": 500, "msg": str(e)}
