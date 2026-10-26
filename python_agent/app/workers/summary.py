# -*- coding: utf-8 -*-
"""
会话自动摘要后台任务（🚀②）：
- 定时（SESSION_SUMMARY_INTERVAL，默认 600s）调用业务层内部接口拉取待生成摘要的会话
- 每个会话取最近 40 条消息 → 租户模型生成 ≤80 字中文摘要 → 回写业务层落库
- 多实例互斥：Redis 分布式锁；单会话失败跳过下轮重试，不影响服务
"""
import asyncio
import logging
import os

from app.infra.config import BUSINESS_API, INTERNAL_API_TOKEN
from app.agent.llm_config import get_tenant_model
from app.infra import redis_lock

logger = logging.getLogger("agent.session_summary")

SUMMARY_PROMPT = """请用不超过120字的中文，概括以下智能客服会话的主要内容。要求：
1. 说清客户的核心诉求、问题是否解决、是否需要人工跟进
2. 必须原样保留对话中出现的关键事实：订单号、金额、时限、商品名（数字与编号逐字符保留，不得改写）
3. 只输出摘要本身，不要任何前缀、标题或解释
4. 不要出现"摘要："之类的引导语

会话记录：
{history}"""

MERGE_PROMPT = """以下是一段客服会话的"已有摘要"和"其后新增的对话"。请把两者合并成一份不超过160字的新摘要。要求：
1. 保留已有摘要中仍然有效的结论，并纳入新增对话的进展
2. 必须原样保留所有出现过的订单号、金额、时限、商品名（数字与编号逐字符保留，不得改写）
3. 同一事实前后矛盾的，以新增对话为准
4. 只输出摘要本身，不要任何前缀、标题或解释；不要出现"摘要："引导语

已有摘要：
{old_summary}

新增对话：
{history}"""

MAX_CANDIDATES = int(os.getenv("SESSION_SUMMARY_MAX_PER_ROUND", "20"))
HISTORY_LIMIT = 40
MESSAGE_INTERVAL = int(os.getenv("SESSION_SUMMARY_INTERVAL", "600"))
# [ours] 长对话记忆：候选空闲阈值（分钟），默认 30 与旧行为一致；评测可设 0
IDLE_MINUTES = int(os.getenv("SESSION_SUMMARY_IDLE_MINUTES", "30"))

_LOCK_KEY = "cs:lock:session_summary"
_LOCK_TTL = 300


async def _acquire_lock(app) -> str | None:
    # [ours] S6 治理：改经持有者 token 锁（Lua 校验后再删，消除误删他人锁）
    rc = getattr(getattr(app, "state", None), "redis_client", None)
    return await redis_lock.acquire_lock(rc, _LOCK_KEY, _LOCK_TTL)


async def _release_lock(app, token) -> None:
    rc = getattr(getattr(app, "state", None), "redis_client", None)
    await redis_lock.release_lock(rc, _LOCK_KEY, token)


async def _internal_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if INTERNAL_API_TOKEN:
        h["X-Internal-Token"] = INTERNAL_API_TOKEN
    return h


def _format_history(msgs: list) -> str:
    lines = []
    for m in msgs:
        role = m.get("role", "")
        content = (m.get("content") or "").strip().replace("\n", " ")
        if not content:
            continue
        who = "客户" if role in ("user", "human") else "客服"
        lines.append(f"{who}: {content[:200]}")
    return "\n".join(lines)


async def _generate_summary(app, tenant_id: str, history: str,
                            old_summary: str = "") -> str:
    """用租户模型生成/滚动合并摘要（失败抛异常，由调用方跳过该会话、不推水位）"""
    model = await get_tenant_model(app, tenant_id)
    if old_summary:
        prompt = MERGE_PROMPT.format(old_summary=old_summary[:600],
                                     history=history[:4000])
    else:
        prompt = SUMMARY_PROMPT.format(history=history[:4000])
    # [ours] 长对话记忆：租户对话模型 max_tokens=1024 是共享配置不动；
    # qwen3 类推理模型的思考内容会吃满输出预算导致 content 为空（冒烟实锤：
    # 空响应 output_tokens 恰好=1024，且思考量不稳定，4096 也会偶发耗尽），
    # 摘要任务单独放大预算+超时，空响应重试一次，仍空则抛错不推水位
    llm = model.bind(max_tokens=8192).with_config(timeout=180)
    text = ""
    for _ in range(2):
        resp = await llm.ainvoke(prompt)
        text = (resp.content or "").strip() if hasattr(resp, "content") else str(resp).strip()
        if text:
            break
    if not text:
        raise RuntimeError("模型返回空摘要（疑似输出预算被思考内容占满）")
    # 去掉可能的前缀/引号，截断 200 字兜底
    for p in ("摘要：", "摘要:", "总结：", "总结:"):
        if text.startswith(p):
            text = text[len(p):].strip()
    text = text.strip("\"'“”")
    return text[:200]


async def _process_one(app, cand: dict) -> bool:
    """处理单个候选会话，成功返回 True。

    [ours] 长对话记忆：cand 携带水位后增量消息与旧摘要；滚动合并成功后把水位
    推进到本次实际读到的最大消息 id（生成/保存任一失败都不推进，下轮幂等重试）。
    """
    thread_id = cand.get("thread_id", "")
    tenant_id = cand.get("tenant_id", "")
    msgs = cand.get("messages") or []
    if not thread_id or not tenant_id or not msgs:
        return False
    history = _format_history(msgs)
    if not history:
        return False
    batch_watermark = max(int(m.get("id") or 0) for m in msgs)
    try:
        summary = await _generate_summary(app, tenant_id, history,
                                          old_summary=cand.get("old_summary") or "")
    except Exception as e:
        logger.warning("summary generate failed tenant=%s thread=%s: %s", tenant_id, thread_id, str(e)[:120])
        return False
    if not summary:
        return False
    try:
        resp = await app.state.http_client.post(
            f"{BUSINESS_API}/api/agent/session-summaries",
            headers=await _internal_headers(),
            json={"thread_id": thread_id, "tenant_id": tenant_id,
                  "summary": summary, "last_message_id": batch_watermark},
            timeout=10.0,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.warning("summary save failed thread=%s: %s", thread_id, data.get("msg"))
            return False
        logger.info("✅ 会话摘要已生成 thread=%s tenant=%s watermark=%s summary=%s",
                    thread_id, tenant_id, batch_watermark, summary[:40])
        return True
    except Exception as e:
        logger.warning("summary save error thread=%s: %s", thread_id, str(e)[:120])
        return False


async def _run_round(app) -> None:
    """执行一轮：拉候选 → 逐会话生成 → 回写"""
    try:
        resp = await app.state.http_client.get(
            f"{BUSINESS_API}/api/agent/session-summaries/pending",
            params={"limit": MAX_CANDIDATES, "idle_minutes": IDLE_MINUTES},
            headers=await _internal_headers(),
            timeout=10.0,
        )
        data = resp.json()
    except Exception as e:
        logger.warning("summary candidates fetch error: %s", str(e)[:120])
        return
    cands = data.get("data") or []
    if not cands:
        return
    done = 0
    for cand in cands:
        try:
            if await _process_one(app, cand):
                done += 1
        except Exception as e:
            logger.warning("summary round item error: %s", str(e)[:120])
    logger.info("🔁 会话摘要本轮处理 %d/%d", done, len(cands))


async def session_summary_loop(app):
    """后台定时任务：会话自动摘要"""
    if MESSAGE_INTERVAL <= 0:
        logger.info("⏸ session summary loop disabled")
        return
    await asyncio.sleep(MESSAGE_INTERVAL)
    while True:
        try:
            lock_token = await _acquire_lock(app)
            if not lock_token:
                logger.info("⏭ summary lock held by another instance, skip")
                await asyncio.sleep(MESSAGE_INTERVAL)
                continue
            try:
                await _run_round(app)
            finally:
                await _release_lock(app, lock_token)
        except Exception:
            logger.exception("session summary round failed")
        await asyncio.sleep(MESSAGE_INTERVAL)
