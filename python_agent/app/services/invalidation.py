"""配置变更后的缓存失效编排（HTTP 入口与 Redis 广播共用，从 routers_cache.py 拆出）。

清除逻辑同时被 POST /agent/cache/clear 与 Redis 订阅消费共用，保证两路行为一致：
模型/Embedding 配置变更后，不仅配置缓存要清，已构建的租户 Agent（LangGraph 图绑定旧模型
实例）与向量检索器（绑定旧 Embedding 实例）也必须清空，否则聊天仍会走旧模型。
"""
import logging

from app.agent.llm_config import clear_model_cache

logger = logging.getLogger("agent.routers_cache")

def clear_tenant_caches(app, tenant_id: str | None, scope: str = "all") -> dict:
    """按租户+scope 清除模型缓存（HTTP 入口与 Redis 广播订阅共用，保证两路行为一致）。

    - tenant_id 为空：清全部（平台默认配置变更，所有跟随平台的租户都受影响）
    - tenant_id 非空 + scope：
      - "chat"      ：只清对话模型缓存 + 该租户 Agent（Agent 绑定对话模型实例）
      - "embedding" ：只清向量模型缓存 + 该租户检索器 + Agent（Agent 持有旧 retriever 引用，必须一并重建）
      - "all"       ：全部清除
    """
    clear_model_cache(tenant_id, scope)
    if scope in ("chat", "embedding", "all"):
        try:
            from app.services.four_agent import clear_cached

            clear_cached(tenant_id)
        except ImportError:
            logger.warning("four-agent package unavailable during cache invalidation")
    # 检索结果缓存（kb_cache）与向量模型绑定：向量配置变更时一并清空，避免旧答案残留
    try:
        from app.infra.cache import clear_kb_cache
        n_kb = clear_kb_cache()
        if n_kb:
            logger.info("✅ kb cache cleared: %d items", n_kb)
    except Exception as e:
        logger.warning("清 kb_cache 失败: %s", e)
    agents = getattr(app.state, "tenant_agents", {})
    faq_agents = getattr(app.state, "faq_agents", {})
    intake_agents = getattr(app.state, "intake_agents", {})
    four_agent_graphs = getattr(app.state, "four_agent_graphs", {})
    retr = getattr(app.state, "tenant_retrievers", {})
    if tenant_id:
        n_agents = n_retr = 0
        if scope in ("chat", "all") and tenant_id in agents:
            agents.pop(tenant_id, None)
            n_agents = 1
        if scope in ("chat", "all"):
            faq_agents.pop(tenant_id, None)
            intake_agents.pop(tenant_id, None)
            four_agent_graphs.pop(tenant_id, None)
        if scope in ("embedding", "all"):
            if tenant_id in retr:
                retr.pop(tenant_id, None)
                n_retr = 1
            if tenant_id in agents:
                agents.pop(tenant_id, None)
                n_agents = 1  # Agent 持有旧 retriever 引用，向量模型变更必须重建
            four_agent_graphs.pop(tenant_id, None)
        logger.info("✅ caches cleared: config, agents=%d, retrievers=%d (tenant=%s, scope=%s)",
                    n_agents, n_retr, tenant_id, scope)
    else:
        n_agents = len(agents)
        n_retr = len(retr)
        agents.clear()
        faq_agents.clear()
        intake_agents.clear()
        four_agent_graphs.clear()
        retr.clear()
        logger.info("✅ caches cleared: config, agents=%d, retrievers=%d (all tenants)",
                    n_agents, n_retr)
    return {"code": 0, "msg": "cache cleared"}
