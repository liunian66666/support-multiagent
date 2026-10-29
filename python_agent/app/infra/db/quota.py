"""租户配额与知识库配额（从 db_ops.py 拆出）。"""
import logging
from typing import Optional

import psycopg

from app.infra.config import BUSINESS_API, INTERNAL_API_TOKEN

logger = logging.getLogger("agent.db_ops")

_tenant_quota_cache: dict = {}
_TENANT_QUOTA_TTL = 30  # 秒


async def get_tenant_quota(app, tenant_id: str) -> dict:
    """获取租户配额（带 30s 缓存），失败返回默认不限制"""
    import time
    now = time.time()
    cached = _tenant_quota_cache.get(tenant_id)
    if cached and (now - cached.get("_ts", 0)) < _TENANT_QUOTA_TTL:
        return cached
    default = {"max_concurrent_sessions": 0, "daily_message_limit": 0, "max_kb_docs": 0, "max_kb_size_mb": 0}
    try:
        headers = {}
        if INTERNAL_API_TOKEN:
            headers["X-Internal-Token"] = INTERNAL_API_TOKEN
        resp = await app.state.http_client.get(
            f"{BUSINESS_API}/api/agent/tenant-quota",
            params={"tenant_id": tenant_id}, headers=headers, timeout=5.0)
        data = resp.json()
        if data.get("code") == 0 and data.get("data"):
            d = data["data"]
            d["_ts"] = now
            _tenant_quota_cache[tenant_id] = d
            return d
    except Exception as e:
        logger.warning("get tenant quota failed: %s", str(e))
    return default


async def check_kb_quota(conn: psycopg.AsyncConnection, app, tenant_id: str) -> Optional[dict]:
    """知识库配额校验（文档数/容量），超限返回错误 dict，否则 None"""
    q = await get_tenant_quota(app, tenant_id)
    max_docs = int(q.get("max_kb_docs") or 0)
    max_size = int(q.get("max_kb_size_mb") or 0)
    if max_docs <= 0 and max_size <= 0:
        return None
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*), COALESCE(SUM(file_size), 0) FROM kb_documents WHERE tenant_id = %s",
            (tenant_id,))
        row = await cur.fetchone()
    docs, size = row[0], row[1] or 0
    if max_docs > 0 and docs >= max_docs:
        return {"code": 429, "msg": f"知识库文档数已达上限（{max_docs} 篇），请删除部分文档后再上传"}
    if max_size > 0 and size >= max_size * 1024 * 1024:
        return {"code": 429, "msg": f"知识库容量已达上限（{max_size} MB），请清理后重试"}
    return None


async def check_concurrent_sessions(conn: psycopg.AsyncConnection, app, tenant_id: str) -> Optional[dict]:
    """并发会话配额校验（实时在线连接数，由 业务层 WebSocket hub 统计），超限返回错误 dict，否则 None"""
    q = await get_tenant_quota(app, tenant_id)
    limit = int(q.get("max_concurrent_sessions") or 0)
    if limit <= 0:
        return None
    try:
        headers = {}
        if INTERNAL_API_TOKEN:
            headers["X-Internal-Token"] = INTERNAL_API_TOKEN
        resp = await app.state.http_client.get(
            f"{BUSINESS_API}/api/agent/tenant-concurrency",
            params={"tenant_id": tenant_id}, headers=headers, timeout=5.0)
        data = resp.json()
        if data.get("code") == 0 and data.get("data"):
            cnt = int(data["data"].get("concurrent_sessions") or 0)
            if cnt >= limit:
                return {"code": 429, "msg": f"当前在线会话数已达上限（{limit}），请稍后再试"}
    except Exception as e:
        logger.warning("check concurrent sessions failed: %s", str(e))
    return None
