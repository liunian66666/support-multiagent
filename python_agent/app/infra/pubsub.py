"""Redis 缓存失效广播订阅（多实例支持）。

Go 网关保存模型配置后 PUBLISH 到 channel `cs:model:cache:invalidate`，
每个 Agent 实例订阅并清除本地缓存（配置/Agent图/检索器），
实现多实例下模型切换全量即时生效。

消息格式：{"tenant_id": "t_demo", "scope": "chat"}；tenant_id 为空=清全部。
"""
import json
import logging
import os

import redis.asyncio as aioredis

logger = logging.getLogger("agent.redis_pubsub")

INVALIDATE_CHANNEL = "cs:model:cache:invalidate"


def redis_url() -> str:
    return os.getenv("REDIS_URL", "redis://redis:6379/0")


async def invalidate_listener(app, redis_client, handler=None) -> None:
    """订阅缓存失效广播并消费（断线由 redis.asyncio 自动重连/重新订阅）。

    handler(app, tenant_id, scope) 由 lifespan 在启动时注入（应用服务层实现）；
    未注入时回退到默认实现，避免 infra 层在模块顶层反向依赖应用层。
    """
    if handler is None:
        from app.services.invalidation import clear_tenant_caches as handler  # 兜底
    pubsub = redis_client.pubsub()
    while True:
        try:
            await pubsub.subscribe(INVALIDATE_CHANNEL)
            logger.info("📡 Redis 缓存失效广播订阅已启动: %s", INVALIDATE_CHANNEL)
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    data = json.loads(msg["data"])
                    tid = data.get("tenant_id") or None
                    scope = data.get("scope", "all")
                    if scope == "prompt":
                        # 平台级 Prompt 变更：各实例自行判断『无激活自有 Prompt』的租户并失效其 Agent
                        from app.agent.builder import invalidate_platform_prompt_agents
                        await invalidate_platform_prompt_agents(app)
                    else:
                        handler(app, tid, scope)
                except Exception as e:
                    logger.warning("处理失效广播失败: %s", e)
        except Exception as e:
            logger.warning("Redis 订阅连接异常（3s 后重连）: %s", e)
            await asyncio_sleep(3)


async def asyncio_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


def make_redis_client() -> aioredis.Redis:
    """创建 Redis 客户端（连接由 redis.asyncio 池化管理）。"""
    return aioredis.from_url(redis_url(), decode_responses=True)
