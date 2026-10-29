"""平台/租户模型配置与模型实例缓存（BYOK，从 agent_factory.py 拆出）。"""
import logging
import os

from langchain.chat_models import init_chat_model
from langchain_openai import OpenAIEmbeddings

from app.infra.config import BUSINESS_API, INTERNAL_API_TOKEN

logger = logging.getLogger("agent.factory")


def _llm_max_retries() -> int:
    """[ours] S6 治理：LLM SDK 层重试次数（429/5xx/超时由 SDK 指数退避）。

    默认 0 = 与历史行为逐字节一致；运维在 compose 设 LLM_MAX_RETRIES=2 开启。
    """
    try:
        return max(0, int(os.getenv("LLM_MAX_RETRIES", "0")))
    except ValueError:
        return 0

_tenant_model_cache: dict = {}
# 配置缓存有效期（秒），避免每次对话都请求 业务层
_MODEL_CONFIG_TTL = 300


_MODEL_CONFIG_TTL = 300  # 5分钟缓存
_PLATFORM_CONFIG_TTL = 300  # 平台默认模型配置缓存


def clear_model_cache(tenant_id: str | None = None, scope: str = "all"):
    """清空模型配置/实例缓存。

    后端修改模型配置成功后调用（POST /agent/cache/clear），
    使新配置立即生效，无需等待 TTL 过期。
    - tenant_id 为空：清全部（平台默认配置变更，所有跟随平台的租户都受影响）
    - tenant_id 非空 + scope：
      - "chat"      ：只清对话模型相关（配置缓存 + _model_ 实例），不清向量
      - "embedding" ：只清向量模型相关（配置缓存 + _emb_ 实例），不清对话
      - "all"       ：全部清除
    说明：租户配置缓存（tenant_id 键）是 chat+embedding 合并存储的，
    任一变更都需刷新（仅多一次 HTTP 拉取）；模型实例缓存按前缀精确清理。
    进程内缓存天然随本函数清空；多 worker/多实例部署时由业务层广播调用，逐个实例清空。
    """
    if tenant_id:
        # 租户配置缓存合并存储（chat+embedding），任一变更都刷新
        _tenant_model_cache.pop(tenant_id, None)
        for k in list(_tenant_model_cache):
            is_model = k.startswith(f"_model_{tenant_id}:")
            is_emb = k.startswith(f"_emb_{tenant_id}:")
            if (scope == "chat" and is_model) or \
               (scope == "embedding" and is_emb) or \
               (scope == "all" and (is_model or is_emb)):
                _tenant_model_cache.pop(k, None)
        logger.info("✅ tenant model cache cleared: [%s] scope=%s", tenant_id, scope)
    else:
        _tenant_model_cache.clear()
        logger.info("✅ model config cache cleared (all tenants)")


async def get_platform_model_config(app) -> dict:
    """获取平台默认模型配置（= 平台管理员租户 t_admin 的模型配置，DB 单一数据源）"""
    cfg = await get_tenant_model_config(app, "t_admin")
    if not cfg:
        raise RuntimeError("获取平台默认模型配置失败：请先在后台配置模型提供商与平台默认模型")
    return {
        "model_config": {
            "model_provider": cfg.get("model_provider", ""),
            "model_name": cfg.get("model_name", ""),
            "api_base": cfg.get("api_base", ""),
            "api_key": cfg.get("api_key", ""),
            "use_custom": True,  # t_admin 即平台配置，始终生效
            "api_key_configured": bool(cfg.get("api_key")),
        },
        "embedding_config": {
            "embedding_provider": (cfg.get("embedding") or {}).get("provider", ""),
            "embedding_model_name": (cfg.get("embedding") or {}).get("model_name", ""),
            "embedding_api_base": (cfg.get("embedding") or {}).get("api_base", ""),
            "api_key": (cfg.get("embedding") or {}).get("api_key", ""),
            "use_custom": True,
            "api_key_configured": bool((cfg.get("embedding") or {}).get("api_key")),
        },
        "_ts": cfg.get("_ts", 0),
    }


async def get_platform_model(app):
    """获取平台默认模型实例（从平台配置创建，带缓存）"""
    cfg = await get_platform_model_config(app)
    mc = cfg.get("model_config", {})
    model_name = mc.get("model_name") or ""
    api_base = mc.get("api_base") or ""
    api_key = mc.get("api_key") or ""
    if not model_name or not api_key or not api_base:
        logger.error("❌ 平台默认对话模型配置不完整（请检查「平台模型配置」），model=%s base=%s key_configured=%s",
                     model_name, api_base, bool(api_key))
        raise RuntimeError("平台默认对话模型配置不完整，请检查后台模型配置")

    cache_key = f"_platform_model:{model_name}:{api_base}:{api_key[-4:]}"
    cached = _tenant_model_cache.get(cache_key)
    if cached:
        return cached

    try:
        model = init_chat_model(
            model=model_name,
            model_provider="openai",
            temperature=0,
            max_tokens=1024,
            timeout=30,
            max_retries=_llm_max_retries(),  # [ours] S6：env 化，默认 0 行为不变
            api_key=api_key,
            base_url=api_base,
        )
        _tenant_model_cache[cache_key] = model
        logger.info("✅ platform model created: model=%s", model_name)
        return model
    except Exception as e:
        logger.error("❌ 创建平台默认模型失败: %s", str(e))
        raise RuntimeError(f"平台默认对话模型鉴权失败：{str(e)}") from e


async def get_platform_embeddings(app):
    """获取平台默认 embeddings 实例（从平台配置创建，带缓存）"""
    cfg = await get_platform_model_config(app)
    ec = cfg.get("embedding_config", {})
    model_name = ec.get("embedding_model_name") or ""
    api_base = ec.get("embedding_api_base") or ""
    api_key = ec.get("api_key") or ""
    if not model_name or not api_key or not api_base:
        logger.error("❌ 平台默认 Embedding 模型配置不完整（请检查「平台模型配置」），model=%s base=%s key_configured=%s",
                     model_name, api_base, bool(api_key))
        raise RuntimeError("平台默认 Embedding 模型配置不完整，请检查后台模型配置")

    cache_key = f"_platform_emb:{model_name}:{api_base}:{api_key[-4:]}"
    cached = _tenant_model_cache.get(cache_key)
    if cached:
        return cached

    try:
        emb = OpenAIEmbeddings(
            model=model_name,
            api_key=api_key,
            base_url=api_base,
            check_embedding_ctx_length=False,
            chunk_size=10,
        )
        _tenant_model_cache[cache_key] = emb
        logger.info("✅ platform embeddings created: model=%s", model_name)
        return emb
    except Exception as e:
        logger.error("❌ 创建平台默认 embeddings 失败: %s", str(e))
        raise RuntimeError(f"平台默认 Embedding 模型鉴权失败：{str(e)}") from e


async def get_tenant_model_config(app, tenant_id: str) -> dict:
    """从 业务层获取租户模型配置，带缓存"""
    import time
    now = time.time()
    cached = _tenant_model_cache.get(tenant_id)
    if cached and (now - cached.get("_ts", 0)) < _MODEL_CONFIG_TTL:
        return cached

    try:
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
        # 业务层瞬时未就绪可能返回 null/非对象：按失败处理（不抛 NoneType 异常），下次请求重试
        if isinstance(data, dict) and data.get("code") == 0 and data.get("data"):
            cfg = data["data"]
            cfg["_ts"] = now
            _tenant_model_cache[tenant_id] = cfg
            logger.info("✅ tenant model config loaded: [%s] use_custom=%s", tenant_id, cfg.get("use_custom"))
            return cfg
    except Exception as e:
        logger.warning("⚠️ 获取租户模型配置失败 [%s]: %s，使用平台默认模型", tenant_id, str(e))

    # 失败时返回 None（不再回退 .env；上层业务给出明确错误）
    logger.error("❌ 租户模型配置获取失败 [%s]，请检查后台「模型提供商/租户模型配置」", tenant_id)
    return None


def invalidate_tenant_model_cache(tenant_id: str):
    """清除租户模型配置缓存（模型配置变更后调用）"""
    _tenant_model_cache.pop(tenant_id, None)


async def current_model_name(app, tenant_id: str) -> str:
    """获取租户当前生效的 LLM 模型名（token 用量 fallback 用，带缓存）"""
    try:
        cfg = await get_tenant_model_config(app, tenant_id)
        if cfg:
            name = cfg.get("model_name") or ""
            if name:
                return name
    except Exception as e:
        logger.warning("current_model_name 获取失败: %s", str(e))
    return ""


async def current_model_source(app, tenant_id: str) -> str:
    """获取租户当前 LLM 用量来源：'platform'（使用平台模型）/ 'custom'（租户自定义模型）
    token 用量统计用：平台模型专项统计只计 source='platform' 的记录"""
    try:
        cfg = await get_tenant_model_config(app, tenant_id)
        if cfg and cfg.get("use_custom"):
            return "custom"
    except Exception as e:
        logger.warning("current_model_source 获取失败: %s", str(e))
    return "platform"


async def current_chat_prices(app, tenant_id: str):
    """获取租户当前生效 chat 单价（元/百万tokens），返回 (input_cache, input_miss, output)
    输入分「命中缓存/未命中」两档（DeepSeek 等）；平台模型取 t_admin 行单价，租户自定义取租户行单价；
    读取失败/未定价返回 (0,0,0)"""
    try:
        cfg = await get_tenant_model_config(app, tenant_id)
        if cfg:
            try:
                return (float(cfg.get("chat_input_cache_price") or 0),
                        float(cfg.get("chat_input_price") or 0),
                        float(cfg.get("chat_output_price") or 0))
            except (TypeError, ValueError):
                pass
    except Exception as e:
        logger.warning("current_chat_prices 获取失败: %s", str(e))
    return 0.0, 0.0, 0.0


async def get_tenant_model(app, tenant_id: str):
    """获取租户专属模型实例（无自定义配置则用平台默认模型）"""
    cfg = await get_tenant_model_config(app, tenant_id)
    if not cfg:
        raise RuntimeError(f"租户{tenant_id}模型配置获取失败，请检查后台模型配置")
    if not cfg.get("use_custom"):
        return await get_platform_model(app)

    # 租户自定义模型：创建新的模型实例
    model_name = cfg.get("model_name")
    api_key = cfg.get("api_key")
    api_base = cfg.get("api_base")

    # 校验平台emb配置非空
    if not model_name or not api_key or not api_base:
        logger.error("❌ 租户[%s]自定义对话模型环境，变量缺失", tenant_id)
        # 不fallback，直接抛异常，上层业务捕获后返回错误给用户
        raise RuntimeError(f"租户{tenant_id}自定义对话模型环境，变量缺失")

    # 缓存 key：用配置内容做 hash，避免配置变更后还用旧模型
    cache_key = f"{tenant_id}:{model_name}:{api_base}:{api_key[-4:]}"
    cached_model = _tenant_model_cache.get(f"_model_{cache_key}")
    if cached_model:
        return cached_model

    try:
        model = init_chat_model(
            model=model_name,
            model_provider="openai",
            temperature=0,
            max_tokens=1024,
            timeout=30,
            max_retries=_llm_max_retries(),  # [ours] S6：env 化，默认 0 行为不变
            api_key=api_key,
            base_url=api_base,
        )
        _tenant_model_cache[f"_model_{cache_key}"] = model
        logger.info("✅ tenant custom model created: [%s] model=%s", tenant_id, model_name)
        return model
    except Exception as e:
        # logger.error("❌ 创建租户自定义模型失败 [%s]: %s，回退到平台默认模型", tenant_id, str(e))
        # return app.state.model
        logger.error("❌ 租户[%s]自定义对话模型校验失败，对话不可用: %s", tenant_id, str(e))
        # 不fallback，直接抛异常，上层业务捕获后返回错误给用户
        raise RuntimeError(f"租户{tenant_id}自定义对话模型鉴权失败：{str(e)}") from e


async def get_tenant_embeddings(app, tenant_id: str):
    """获取租户专属 embeddings 实例（无自定义配置则用平台默认）"""
    cfg = await get_tenant_model_config(app, tenant_id)
    if not cfg:
        raise RuntimeError(f"租户{tenant_id}模型配置获取失败，请检查后台模型配置")
    emb_cfg = cfg.get("embedding", {}) if isinstance(cfg, dict) else {}
    if not emb_cfg.get("use_custom"):
        return await get_platform_embeddings(app)

    model_name = emb_cfg.get("model_name") or ""
    api_key = emb_cfg.get("api_key") or ""
    api_base = emb_cfg.get("api_base") or ""

    cache_key = f"_emb_{tenant_id}:{model_name}:{api_base}:{api_key[-4:]}"
    cached = _tenant_model_cache.get(cache_key)
    if cached:
        return cached

    try:
        emb = OpenAIEmbeddings(
            model=model_name,
            api_key=api_key,
            base_url=api_base,
            check_embedding_ctx_length=False,
            chunk_size=10,
        )
        _tenant_model_cache[cache_key] = emb
        logger.info("✅ tenant custom embeddings created: [%s] model=%s", tenant_id, model_name)
        return emb
    except Exception as e:
        # logger.error("❌ 创建租户自定义 embeddings 失败 [%s]: %s，回退到平台默认", tenant_id, str(e))
        # return app.state.embeddings
        # 这里会捕获：空key、401鉴权失败、模型不存在、网络错误
        logger.error("❌ 租户[%s]自定义embedding校验失败，BYOK不可用: %s", tenant_id, str(e))
        # 不fallback，直接抛异常，上层业务捕获后返回错误给用户
        raise RuntimeError(f"租户{tenant_id}自定义向量模型鉴权失败：{str(e)}") from e
