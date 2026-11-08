# 中文文案必然包含全角标点
"""模型能力：租户已验证配置优先，平台配置只作显式声明时使用。

``thinking`` 关断放在这里，而不是每个调用点各写一遍：qwen3.8 的 thinking 会把首字
延迟拉长到用户以为服务卡死，而它只需要在**创建模型**时绑定一次。
"""

from __future__ import annotations

import re
from typing import Any


def _thinking_disabled(model: Any) -> Any:
    """按模型名关断 thinking（仅对已知会开 thinking 的模型绑定参数）。"""
    if re.match(r"qwen3\.8", str(getattr(model, "model_name", "")), re.I):
        return model.bind(extra_body={"enable_thinking": False})
    return model


async def tenant_model(app: Any, tenant_id: str) -> Any:
    """该租户**已验证**配置里的对话模型（平台模型或租户自定义，由配置决定）。"""
    if not tenant_id:
        raise PermissionError("tenant_id is required")
    from app.agent.llm_config import get_tenant_model

    return _thinking_disabled(await get_tenant_model(app, tenant_id))


async def platform_model(app: Any) -> Any:
    """平台默认模型（配置在 ``t_admin`` 行）。"""
    from app.agent.llm_config import get_platform_model

    return _thinking_disabled(await get_platform_model(app))


async def platform_model_by_name(app: Any, model_name: str) -> Any:
    """换模型 id、保留平台 key/base —— **仅评测档**（``FOUR_AGENT_EVAL=on``）调用。

    存在的原因是可复现：同一个 key 下各模型的免费额度不同，换 id 是唯一能跑通另一种
    模型的路径；但它与基线不可比，调用方必须把结果行标成不可比。
    """
    from langchain.chat_models import init_chat_model

    from app.agent.llm_config import get_platform_model_config

    cfg = (await get_platform_model_config(app)).get("model_config", {})
    key, base = cfg.get("api_key") or "", cfg.get("api_base") or ""
    if not key or not base:
        raise RuntimeError("t_admin 缺 api_key/api_base，无法按名字换模型")
    options = {"reasoning_effort": "low"} if model_name == "glm-5.3" else {}
    return _thinking_disabled(init_chat_model(model=model_name, model_provider="openai",
                                              temperature=0, max_tokens=1024, timeout=60,
                                              api_key=key, base_url=base, **options))


async def billing_identity(app: Any, tenant_id: str) -> tuple[str, str]:
    """``(model_name, model_source)`` —— 记账与 trace 归因用。"""
    from app.agent.llm_config import current_model_name, current_model_source

    return (await current_model_name(app, tenant_id),
            await current_model_source(app, tenant_id))


async def billing_prices(app: Any, tenant_id: str) -> tuple[float, float, float]:
    """``(input_cache, input_miss, output)`` 元/百万 token；未定价返回全 0。"""
    from app.agent.llm_config import current_chat_prices

    return await current_chat_prices(app, tenant_id)
