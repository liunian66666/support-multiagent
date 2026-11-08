"""Token 用量的解析与记账（从 db_ops.py 拆出）。"""
import logging
from typing import Optional

import psycopg

logger = logging.getLogger("agent.db_ops")

def extract_usage_metadata(messages) -> Optional[dict]:
    """从最终消息列表提取最后一个带 usage_metadata 的 AI 消息用量（best-effort）
    返回含输入缓存拆分：cache_hit_tokens/cache_miss_tokens 取自 response_metadata.usage
    （DeepSeek/通义等返回 prompt_cache_hit_tokens/prompt_cache_miss_tokens；不返回则 0=全部按未命中计）"""
    for msg in reversed(messages or []):
        um = getattr(msg, "usage_metadata", None)
        if um:
            rm = getattr(msg, "response_metadata", {}) or {}
            usage = rm.get("usage") or {}
            return {
                # OpenAI 兼容接口模型名在 response_metadata.model；部分实现放在 model_name
                "model": rm.get("model", "") or rm.get("model_name", "") or um.get("model_name", "") or "",
                "input_tokens": int(um.get("input_tokens") or 0),
                "output_tokens": int(um.get("output_tokens") or 0),
                "total_tokens": int(um.get("total_tokens") or 0),
                "cache_hit_tokens": int(usage.get("prompt_cache_hit_tokens") or 0),
                "cache_miss_tokens": int(usage.get("prompt_cache_miss_tokens") or 0),
            }
    return None


async def record_token_usage(conn: psycopg.AsyncConnection, tenant_id: str, thread_id: str,
                             messages, fallback_model: str = "", source: str = "platform",
                             chat_input_cache_price: float = 0.0, chat_input_price: float = 0.0,
                             chat_output_price: float = 0.0) -> None:
    """P1-1：记录一次对话的 LLM token 用量（异步 best-effort，失败仅告警不影响主流程）
    fallback_model：流式场景 chunk 的 response_metadata 不含 model，用租户生效模型名兜底
    source：platform=平台模型 / custom=租户自定义模型（平台模型专项统计用）
    单价（元/百万tokens，0=未定价）：输入分「命中缓存/未命中」两档 + 输出；
    费用按发生时单价快照，价格调整不影响历史记录。
    输入拆分：API 返回 cache_hit/cache_miss 则分档计价；不返回则全部按未命中价计。"""
    try:
        um = extract_usage_metadata(messages)
        if not um or (um["input_tokens"] == 0 and um["output_tokens"] == 0):
            return
        model = um["model"] or fallback_model or ""
        total = um["total_tokens"] or (um["input_tokens"] + um["output_tokens"])
        hit = um["cache_hit_tokens"] or 0
        miss = um["cache_miss_tokens"] or 0
        if hit == 0 and miss == 0:
            miss = um["input_tokens"]  # 无缓存拆分 → 全部输入按未命中价
        cost = (hit / 1_000_000.0 * chat_input_cache_price
                + miss / 1_000_000.0 * chat_input_price
                + um["output_tokens"] / 1_000_000.0 * chat_output_price)
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO token_usage (tenant_id, thread_id, model, source, input_tokens, output_tokens, total_tokens, cost)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (tenant_id, thread_id, model, source, um["input_tokens"], um["output_tokens"],
                  total, round(cost, 4)))
    except Exception as e:
        logger.warning("record token usage skipped: %s", e)
