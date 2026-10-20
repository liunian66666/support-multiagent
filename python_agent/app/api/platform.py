"""平台配置缓存失效入口（从 main.py 拆出）。"""
import logging

from fastapi import APIRouter, Request

from app.agent.llm_config import _tenant_model_cache

logger = logging.getLogger("agent")
router = APIRouter()


@router.post("/agent/platform/cache-invalidate")
async def platform_cache_invalidate(request: Request):
    """清除平台默认模型配置缓存（平台管理员修改配置后调用）"""
    try:
        # 清除平台配置缓存
        _tenant_model_cache.pop("_platform_config", None)
        # 清除所有以 _platform_ 开头的缓存（平台模型/embeddings 实例）
        keys_to_remove = [k for k in _tenant_model_cache.keys() if k.startswith("_platform_")]
        for k in keys_to_remove:
            _tenant_model_cache.pop(k, None)
        logger.info("✅ 平台默认模型配置缓存已清除，清除 %d 个缓存项", len(keys_to_remove) + 1)
        return {"code": 0, "msg": "缓存已清除", "cleared": len(keys_to_remove) + 1}
    except Exception as e:
        logger.error("❌ 清除平台配置缓存失败: %s", str(e))
        return {"code": 500, "msg": str(e)}
