# -*- coding: utf-8 -*-
"""[ours] 长对话记忆：会话摘要按水位加载并拼入当轮上下文。

数据面：摘要存于业务侧 MySQL session_summaries（含 last_summarized_message_id
水位），agent 续聊时经内部只读接口拉取，prompt 组装为
[更早对话摘要] + [最近窗口消息] + [本轮输入]；摘要只进模型输入，
不写检查点（specialist 为无状态快照，检查点仍只存原话）。

开关（三级，任一为 off 即不注入）：
1. Redis 键 cs:memory:inject == "off" → 平台级一票否决（评测 off 臂 / 应急热回滚）；
2. 租户级 tenants.memory_enabled（管理后台每租户开关，默认 1，经 tenant-quota
   内部接口读取，复用 quota 30s 缓存；拉取失败按开处理=行为同旧版）；
3. env MEMORY_INJECT 默认 on（部署级兜底）。
"""
import logging
import os
import time

from langchain_core.messages import HumanMessage

from app.infra.config import BUSINESS_API, INTERNAL_API_TOKEN

logger = logging.getLogger("agent.memory")

_CACHE_TTL = 15.0
_cache: dict = {}
INJECT_REDIS_KEY = "cs:memory:inject"

_SYSTEM_PREFIX = ("【更早对话摘要】（本会话更早轮次的背景，来自用户本人陈述与先前答复；"
                  "用户说过的订单号、金额等可直接复述引用；"
                  "涉及店铺政策数值仍须以本轮检索和工具为准，不得据此承诺）\n")


async def inject_enabled(app, tenant_id: str = "") -> bool:
    # 第 1 级：平台应急闸门（Redis 可写即生效，不依赖重启）
    rc = getattr(getattr(app, "state", None), "redis_client", None)
    if rc is not None:
        try:
            v = await rc.get(INJECT_REDIS_KEY)
            if v is not None:
                text = v if isinstance(v, str) else v.decode()
                if text.strip().lower() == "off":
                    return False
        except Exception:
            pass  # Redis 异常继续走租户/env 判定
    # 第 2 级：租户级开关（tenant_id 为空=平台内部调用，跳过本级的租户维度）
    if tenant_id:
        try:
            from app.infra.db.quota import get_tenant_quota
            q = await get_tenant_quota(app, tenant_id)
            if int(q.get("memory_enabled", 1) if q.get("memory_enabled") is not None else 1) == 0:
                return False
        except Exception as e:
            logger.warning("tenant memory_enabled fetch failed tenant=%s: %s",
                           tenant_id, str(e)[:120])  # 失败按开，降级行为同旧版
    # 第 3 级：部署级默认
    return os.getenv("MEMORY_INJECT", "on").strip().lower() != "off"


async def summary_block(app, tenant_id: str, thread_id: str) -> str:
    """拉当前摘要文本（15s TTL 进程缓存；失败返回空串，行为同旧版）。"""
    now = time.time()
    key = (tenant_id, thread_id)
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    text = ""
    try:
        resp = await app.state.http_client.get(
            f"{BUSINESS_API}/api/agent/session-summaries",
            params={"thread_id": thread_id},
            headers={"X-Internal-Token": INTERNAL_API_TOKEN},
            timeout=3.0,
        )
        data = (resp.json() or {}).get("data")
        if data and data.get("summary"):
            text = data["summary"]
    except Exception as e:
        logger.warning("memory summary fetch failed thread=%s: %s",
                       thread_id, str(e)[:120])
    _cache[key] = (now, text)
    return text


async def memory_messages(app, tenant_id: str, thread_id: str) -> list:
    """prompt 组装的长程记忆段；开关 off 或无摘要时返回 []（与旧行为完全一致）。"""
    if not await inject_enabled(app, tenant_id):
        return []
    text = await summary_block(app, tenant_id, thread_id)
    if not text:
        return []
    # 用 HumanMessage 而非 SystemMessage：specialist 图自带 system prompt，
    # 部分网关要求 system 只能位于消息列表首位，中段 system 直接 400（冒烟实锤）
    return [HumanMessage(content=_SYSTEM_PREFIX + text[:400])]
