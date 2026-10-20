"""模型/检索缓存手工清理入口（从 routers_cache.py 拆出，保留用于调试排障）。

⚠️ 强制 Redis 后，Go 侧缓存失效已统一走 Redis 广播（cs:model:cache:invalidate），
不再 HTTP 直调本接口。
"""
import logging

from fastapi import APIRouter, Request

from app.services.invalidation import clear_tenant_caches


logger = logging.getLogger("agent.routers_cache")
router = APIRouter()


@router.post("/agent/cache/clear")
async def cache_clear(request: Request, tenant_id: str | None = None, scope: str = "all"):
    """清空模型配置/实例/Agent/检索器缓存（由 Go 在配置保存成功后调用）。"""
    try:
        return clear_tenant_caches(request.app, tenant_id, scope)
    except Exception as e:
        logger.warning("清空模型缓存失败: %s", e)
        return {"code": 500, "msg": "清空缓存失败: " + str(e)}
