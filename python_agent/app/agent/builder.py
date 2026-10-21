"""Agent 构建、配置签名校验与缓存失效（从 agent_factory.py 拆出）。"""
import logging

from langchain.agents import create_agent

from app.agent.llm_config import (_tenant_model_cache, clear_model_cache,
                                  get_tenant_model)
from app.agent.middleware import content_guard
from app.agent.retrieval import ensure_tenant_vector, get_tenant_retriever
from app.infra.config import BUSINESS_API, INTERNAL_API_TOKEN
from app.infra.db.prompt import load_active_prompt

logger = logging.getLogger("agent.factory")

# def build_agent(app, system_prompt: str):
#     """用指定提示词构建平台默认 Agent，并保存到 app.state.agent。
#
#     新聊天入口使用 FAQ/Intake 专职图；旧图仅用于历史审批中断的恢复。
#     """
#     agent = create_agent(
#         model=app.state.model,
#         tools=app.state.tools,
#         middleware=[content_guard],
#         checkpointer=app.state.checkpointer,
#         system_prompt=system_prompt,
#     )
#     app.state.agent = agent
#     logger.info("✅ Agent rebuilt with system_prompt len=%d", len(system_prompt))


async def get_tenant_agent(app, tenant_id: str):
    """旧检查点恢复所需的租户图；新聊天不向该全工具图委派。"""
    # 配置签名懒检查：Redis 广播丢失时的最终一致兜底（TTL 内跳过，失败不阻断）
    sig_version = await _check_config_version(app, tenant_id)
    if tenant_id in app.state.tenant_agents:
        return app.state.tenant_agents[tenant_id]
    await ensure_tenant_vector(app, tenant_id)
    get_tenant_retriever(app, tenant_id)
    async with app.state.db_pool.connection() as conn:
        prompt = await load_active_prompt(conn, tenant_id)
    # 使用租户专属模型（BYOK：有自定义配置则用租户的，否则用平台默认）
    model = await get_tenant_model(app, tenant_id)
    agent = create_agent(
        model=model,
        tools=app.state.tools,
        middleware=[content_guard],
        checkpointer=app.state.checkpointer,
        system_prompt=prompt,
    )
    app.state.tenant_agents[tenant_id] = agent
    # 记录本次构建对应的配置签名：
    # - 签名检查路径：_check_config_version 返回的 remote version
    # - 广播路径：TTL 内跳过检查返回 None，从刚拉取的配置缓存补记，避免 10s 后误判一次变化
    if not sig_version:
        cached_cfg = _tenant_model_cache.get(tenant_id) or {}
        sig_version = cached_cfg.get("config_version") or None
    if sig_version:
        _agent_sigs[tenant_id] = sig_version
    logger.info("✅ tenant agent built: [%s] prompt_len=%d", tenant_id, len(prompt))
    return agent


_agent_sigs: dict = {}          # tenant_id -> 构建 Agent 时的 config_version
_sig_check_ts: dict = {}        # tenant_id -> 上次检查时间（monotonic）
_SIG_CHECK_TTL = 10             # 秒


async def _fetch_config_version(app, tenant_id: str) -> str | None:
    """从 Go 拉取当前配置签名（无缓存，保证新鲜）"""
    headers = {}
    if INTERNAL_API_TOKEN:
        headers["X-Internal-Token"] = INTERNAL_API_TOKEN
    resp = await app.state.http_client.get(
        f"{BUSINESS_API}/api/agent/model-config",
        params={"tenant_id": tenant_id},
        headers=headers,
        timeout=5.0,
    )
    data = resp.json()
    if data.get("code") == 0 and data.get("data"):
        return data["data"].get("config_version") or None
    return None


async def _check_config_version(app, tenant_id: str) -> str | None:
    """懒检查配置签名；变化则清除该租户缓存（下次调用重建 Agent）。
    返回当前 remote version（TTL 内或拉取失败返回 None，不阻断聊天）。"""
    import time
    now = time.monotonic()
    if now - _sig_check_ts.get(tenant_id, 0) < _SIG_CHECK_TTL:
        return None
    _sig_check_ts[tenant_id] = now
    try:
        remote = await _fetch_config_version(app, tenant_id)
    except Exception as e:
        logger.warning("⚠️ 配置签名检查失败 [%s]（跳过本轮）: %s", tenant_id, str(e))
        return None
    if remote is None:
        return None
    if remote != _agent_sigs.get(tenant_id):
        # 配置已变化（可能广播丢失）：清该租户缓存，get_tenant_agent 走重建路径
        logger.info("🔄 配置签名变化 [%s] %s -> %s（广播兜底，重建 Agent）",
                    tenant_id, _agent_sigs.get(tenant_id, "(none)"), remote)
        clear_model_cache(tenant_id, "all")
        app.state.tenant_agents.pop(tenant_id, None)
        getattr(app.state, "faq_agents", {}).pop(tenant_id, None)
        getattr(app.state, "intake_agents", {}).pop(tenant_id, None)
        app.state.tenant_retrievers.pop(tenant_id, None)
    return remote


async def rebuild_tenant_agent(app, tenant_id: str):
    """按租户最新 Prompt 重建其 Agent（Prompt 管理变更后调用）"""
    if tenant_id in app.state.tenant_agents:
        del app.state.tenant_agents[tenant_id]
    return await get_tenant_agent(app, tenant_id)


async def invalidate_platform_prompt_agents(app) -> int:
    """平台级 Prompt 变更：仅使『无激活自有 Prompt』的租户 Agent 失效（懒重建读新平台 Prompt）。

    有自有 Prompt 的租户不受平台级 Prompt 变更影响，Agent 缓存保留。
    返回失效的 Agent 数量。
    """
    async with app.state.db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT DISTINCT tenant_id FROM prompts WHERE tenant_id <> '' AND is_active = true")
            rows = await cur.fetchall()
    with_own = {r[0] for r in rows}
    agents = getattr(app.state, "tenant_agents", {})
    affected = [t for t in list(agents.keys()) if t not in with_own]
    for t in affected:
        agents.pop(t, None)
    logger.info("✅ 平台级 Prompt 变更：%d 个租户 Agent 失效（无自有 Prompt）；有自有 Prompt 的 %d 个租户保留",
                len(affected), len(with_own))
    return len(affected)
