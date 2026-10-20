"""会话过期清理后台循环（从 db_ops.py 拆出，与 HTTP 生命周期解耦）。"""
import asyncio
import logging

from fastapi import FastAPI

from app.infra.cache import _kb_cache, _kb_cache_cleanup
from app.infra.config import SESSION_CLEANUP_INTERVAL, SESSION_RETENTION_DAYS
from app.infra.db.session import (_acquire_cleanup_lock, _release_cleanup_lock,
                                  cleanup_sessions)

logger = logging.getLogger("agent.db_ops")

async def session_cleanup_loop(app: FastAPI):
    """后台定时回收过期会话"""
    if SESSION_CLEANUP_INTERVAL <= 0:
        logger.info("⏸ periodic session cleanup disabled")
        return
    await asyncio.sleep(SESSION_CLEANUP_INTERVAL)
    while True:
        try:
            # 多实例互斥：Redis 分布式锁（SETNX），抢不到锁则跳过本轮（其他实例在执行）
            if not await _acquire_cleanup_lock(app):
                logger.info("⏭ cleanup lock held by another instance, skip")
                await asyncio.sleep(SESSION_CLEANUP_INTERVAL)
                continue
            async with app.state.db_pool.connection() as conn:
                res = await cleanup_sessions(conn, app.state.checkpointer, SESSION_RETENTION_DAYS)
            logger.info("🔁 periodic session cleanup: %s", res)
        except Exception:
            logger.exception("periodic session cleanup failed")
        finally:
            await _release_cleanup_lock(app)
        # P2-2：同时清理过期的知识库检索缓存
        try:
            expired = _kb_cache_cleanup()
            if expired > 0:
                logger.info(f"🧹 KB cache cleanup: removed {expired} expired entries, total {len(_kb_cache)}")
        except Exception:
            logger.exception("KB cache cleanup failed")
        await asyncio.sleep(SESSION_CLEANUP_INTERVAL)
