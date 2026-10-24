# -*- coding: utf-8 -*-
"""[ours] S3 经验提炼后台任务：高评分轮次 → LLM 提炼 → experience_items(candidate)。

设计口径（docs/support-multiAgent能力优化设计方案.md §4.2/§4.3）：
- 输入：experience_feedback(评分>=4) join agent_trace_runs(answer_id 锚点)，
  且当轮 trace 带 kb 证据（经验必须锚定已发布知识，防幻觉经验）
- 输出：candidate 态经验条目，等人工审核后才可召回（S4 发布）
- 脱敏：trace 事件本身白名单化（无用户原话）；提炼 prompt 只含证据事件属性
  与聚合评分，不含会话消息；产出内容限 300 字
- 多实例互斥：Redis 锁（同 summary worker）；提炼模型调用接入 usage 记账
- EXPERIENCE_DISTILL_INTERVAL=0 可整体关闭
"""
import asyncio
import logging
import os

from app.agent.llm_config import (current_chat_prices, current_model_name,
                                  current_model_source)
from app.infra import redis_lock
from app.infra.db.experience import find_distill_candidates, insert_experience_item
from app.infra.db.usage import record_token_usage

logger = logging.getLogger("agent.experience_distill")

INTERVAL = int(os.getenv("EXPERIENCE_DISTILL_INTERVAL", "1800"))
MAX_PER_ROUND = int(os.getenv("EXPERIENCE_DISTILL_MAX_PER_ROUND", "5"))

_LOCK_KEY = "cs:lock:experience_distill"
_LOCK_TTL = 600

_DISTILL_PROMPT = """你是客服经验提炼员。下面是 {tenant} 租户一条获得客户 {score} 星好评的\
客服应答的运行轨迹摘要（仅结构化事件属性，无客户原话）。请提炼一条可复用的\
「情景→做法」经验，供人工审核后参考。

硬性要求：
1. 只依据轨迹中体现的知识检索证据（evidence/kb_retrieval 事件的属性），\
不得虚构具体政策数值（金额、期限等）；不确定的写"以店铺已发布政策为准"。
2. 不出现客户身份、会话内容、订单号、内部系统名。
3. 输出两行：第一行经验正文（不超过120字，含适用情景）；\
第二行 `APPLICABILITY: 关键词1,关键词2`（2-4 个召回触发关键词）。

轨迹摘要：
{digest}"""


def _digest_events(events: list) -> str:
    """trace events → 提炼用摘要（只保留结构化属性，含证据数量/命中 ID）。"""
    lines = []
    for ev in events or []:
        attrs = ev.get("attributes") or {}
        safe = {k: v for k, v in attrs.items()
                if isinstance(v, (str, int, float, bool, list))}
        lines.append(f"{ev.get('stage','')}/{ev.get('name','')}: {safe}")
    return "\n".join(lines)[:2500]


def _parse_digest(text: str) -> tuple[str, dict]:
    """拆分『经验正文 + APPLICABILITY 行』；解析失败返回空（跳过不落库）。"""
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    if not lines or len(lines) > 6:
        return "", {}
    keywords, content_lines = [], []
    for ln in lines:
        if ln.upper().startswith("APPLICABILITY:"):
            keywords = [k.strip() for k in
                        ln.split(":", 1)[1].replace("，", ",").split(",")
                        if k.strip()][:4]
        else:
            content_lines.append(ln)
    content = " ".join(content_lines).strip()[:300]
    if not content or not keywords:
        return "", {}
    return content, {"keywords": keywords}


async def _acquire_lock(app) -> str | None:
    # [ours] S6 治理：持有者 token 锁（同 summary）
    rc = getattr(getattr(app, "state", None), "redis_client", None)
    return await redis_lock.acquire_lock(rc, _LOCK_KEY, _LOCK_TTL)


async def _release_lock(app, token) -> None:
    rc = getattr(getattr(app, "state", None), "redis_client", None)
    await redis_lock.release_lock(rc, _LOCK_KEY, token)


async def _distill_one(app, cand: dict) -> bool:
    """单条提炼：LLM → 解析 → candidate 落库 + usage 记账。失败返回 False。"""
    tenant_id = cand["tenant_id"]
    # 证据事件属性里可能带 evidence_ids 列表（chunk ID，非用户数据）；
    # 用户/会话内容从未进入 trace，此处无需再脱敏。
    digest = _digest_events(cand.get("events"))
    prompt = _DISTILL_PROMPT.format(tenant_id=tenant_id,
                                    score=cand.get("score", 5),
                                    digest=digest)
    try:
        from app.agent.llm_config import get_tenant_model
        model = await get_tenant_model(app, tenant_id)
        resp = await asyncio.wait_for(model.ainvoke(prompt), timeout=60)
    except Exception as e:
        logger.warning("distill model failed tenant=%s answer=%s: %s",
                       tenant_id, cand.get("answer_id", ""), str(e)[:120])
        return False
    text = (resp.content or "").strip() if hasattr(resp, "content") else str(resp).strip()
    content, applicability = _parse_digest(text)
    if not content:
        logger.info("distill skipped (unparsable) tenant=%s answer=%s",
                    tenant_id, cand.get("answer_id", ""))
        return False
    try:
        exp_id = await insert_experience_item(app, {
            "tenant_id": tenant_id,
            "source_trace_id": cand.get("trace_id", ""),
            "source_answer_id": cand.get("answer_id", ""),
            "route": cand.get("route", ""),
            "content": content,
            "applicability": applicability,
        })
    except Exception:
        logger.exception("distill persist failed tenant=%s", tenant_id)
        return False
    if exp_id:
        # 提炼用量记账（修复 summary worker 同类不记账问题——设计 §4.2 要求）
        try:
            from app.infra.db_pool import _pool_conn
            async with _pool_conn(app) as conn:
                in_cache, in_miss, out_price = await current_chat_prices(app, tenant_id)
                await record_token_usage(
                    conn, tenant_id, cand.get("thread_id", ""), [resp],
                    fallback_model=await current_model_name(app, tenant_id),
                    source=await current_model_source(app, tenant_id),
                    chat_input_cache_price=in_cache, chat_input_price=in_miss,
                    chat_output_price=out_price)
        except Exception:
            logger.warning("distill usage record failed", exc_info=True)
        logger.info("✅ 候选经验#%d 提炼完成 tenant=%s answer=%s",
                    exp_id, tenant_id, cand.get("answer_id", ""))
    return bool(exp_id)


async def run_distill_round(app, limit: int = MAX_PER_ROUND) -> int:
    """执行一轮提炼，返回成功条数（供测试与手动触发复用）。"""
    cands = await find_distill_candidates(app, limit)
    done = 0
    for cand in cands:
        try:
            if await _distill_one(app, cand):
                done += 1
        except Exception:
            logger.exception("distill item error tenant=%s", cand.get("tenant_id"))
    if cands:
        logger.info("🔁 经验提炼本轮 %d/%d tenant 分布=%s", done, len(cands),
                    sorted({c["tenant_id"] for c in cands}))
    return done


async def experience_distill_loop(app):
    """后台定时任务：经验提炼（默认 30 分钟一轮）。"""
    if INTERVAL <= 0:
        logger.info("⏸ experience distill loop disabled")
        return
    await asyncio.sleep(INTERVAL)
    while True:
        try:
            lock_token = await _acquire_lock(app)
            if not lock_token:
                logger.info("⏭ distill lock held by another instance, skip")
            else:
                try:
                    await run_distill_round(app)
                finally:
                    await _release_lock(app, lock_token)
        except Exception:
            logger.exception("distill round failed")
        await asyncio.sleep(INTERVAL)
