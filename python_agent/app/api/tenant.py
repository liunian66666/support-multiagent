"""租户预热、租户清理与租户用量报表（从 main.py 拆出）。"""
import logging

from fastapi import APIRouter, Request

from app.agent.builder import get_tenant_agent
from app.infra.context import trace_id_var
from app.infra.db.tenant import collection_name
from app.infra.db_pool import _pool_conn
from app.schemas import TenantWarmReq

logger = logging.getLogger("agent")
router = APIRouter()


@router.post("/agent/tenants/prewarm")
async def tenant_prewarm(req: TenantWarmReq, request: Request):
    trace_id_var.set(request.headers.get("X-Trace-Id", "-"))
    try:
        await get_tenant_agent(request.app, req.tenant_id)
        return {"code": 0, "data": {"tenant_id": req.tenant_id, "status": "ready"}}
    except Exception as e:
        # 降级策略：对话模型或向量模型任一不可用时不阻塞预热（服务保持可用），
        # 对话/索引时再由对应环节明确报错。Go 侧仅记录 warning。
        logger.warning("tenant prewarm degraded [%s]: %s", req.tenant_id, str(e)[:200])
        return {"code": 0, "data": {"tenant_id": req.tenant_id, "status": "degraded", "reason": str(e)[:200]}}


@router.delete("/agent/tenants/{tenant_id}")
async def tenant_cleanup(tenant_id: str, request: Request):
    """清理指定租户的全部业务数据：PG prompts/kb_documents + Qdrant 集合 + 内存缓存"""
    trace_id_var.set(request.headers.get("X-Trace-Id", "-"))
    try:
        result = {"prompts_deleted": 0, "kb_docs_deleted": 0, "qdrant_collection": ""}
        async with _pool_conn(request.app) as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM prompts WHERE tenant_id = %s", (tenant_id,))
                result["prompts_deleted"] = cur.rowcount
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM kb_documents WHERE tenant_id = %s", (tenant_id,))
                result["kb_docs_deleted"] = cur.rowcount
        # 删除 Qdrant 集合
        col = collection_name(tenant_id)
        try:
            request.app.state.qdrant_client.delete_collection(collection_name=col)
            result["qdrant_collection"] = col
        except Exception as qe:
            logger.warning("delete qdrant collection %s failed: %s", col, qe)
        # 清理内存缓存
        request.app.state.tenant_stores.pop(tenant_id, None)
        request.app.state.tenant_retrievers.pop(tenant_id, None)
        request.app.state.tenant_agents.pop(tenant_id, None)
        logger.info("tenant %s cleanup done: %s", tenant_id, result)
        return {"code": 0, "data": result}
    except Exception as e:
        logger.exception("tenant cleanup error")
        return {"code": 500, "msg": str(e)}


@router.get("/agent/tenant-usage")
async def tenant_usage_report(request: Request):
    """批量返回各租户用量（与配额校验口径一致）：
      active_sessions  最近 30 分钟活跃会话数（并发）
      kb_docs          知识库文档数
      kb_size_bytes    知识库占用字节数
    返回 {tenant_id: {active_sessions, kb_docs, kb_size_bytes}}
    """
    try:
        async with _pool_conn(request.app) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT tenant_id, count(*) FROM session_meta "
                    "WHERE last_active_at > now() - interval '30 minutes' "
                    "GROUP BY tenant_id")
                sessions = {r[0]: int(r[1]) for r in await cur.fetchall()}
                await cur.execute(
                    "SELECT tenant_id, count(*), COALESCE(SUM(file_size), 0) "
                    "FROM kb_documents GROUP BY tenant_id")
                kb = {r[0]: (int(r[1]), int(r[2])) for r in await cur.fetchall()}
        data = {}
        for tid in set(sessions) | set(kb):
            s = sessions.get(tid, 0)
            k = kb.get(tid, (0, 0))
            data[tid] = {"active_sessions": s, "kb_docs": k[0], "kb_size_bytes": k[1]}
        return {"code": 0, "data": data}
    except Exception as e:
        logger.exception("/agent/tenant-usage error")
        return {"code": 500, "msg": str(e)}
