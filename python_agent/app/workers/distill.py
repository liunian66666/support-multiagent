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

from app.agent.llm_config import current_chat_prices, current_model_name, current_model_source
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
1. 只依据轨迹中体现的知识检索证据（evidence/search_kb 事件的属性），\
不得虚构具体政策数值（金额、期限等）。
1a.【禁止保守倾向】"以店铺已发布政策为准""不确定时提示核实"这类回避话术\
【不能】作为经验做法的主体或结论——轨迹显示该轮应答成功给出了检索到的具体条款\
并获得好评，说明正确做法是"直接给出本轮检索证据中的数值并标注来源"。\
经验做法必须是：命中的资料已含用户所问的具体数值/条款时，直接引用回答；\
只有证据确实没有该信息时才说明无法确认。
2. 不出现客户身份、会话内容、订单号、内部系统名。
3. 输出两行：第一行经验正文（不超过120字，含适用情景）；\
第二行 `APPLICABILITY: 关键词1,关键词2`。
关键词是这条经验将来被召回的触发开关，必须是【客户提问原文里会直接出现的词】：
- 优先从 evidence/search_kb 事件的 query 属性（客户问题短语）里挑 2-4 个\
具体业务名词或动词，例如：包邮、发货、发票、退款、运费、签收。
- 禁止使用"政策""规则""政策咨询""FAQ""知识库检索""证据核验"这类\
客服内部视角的抽象词——它们会命中一切问题造成误注入，客户具体问法里\
也很少出现，用了就等于没有适用条件。

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
    """拆分『经验正文 + APPLICABILITY 行』；解析失败返回空（跳过不落库）。

    容错：剥离 markdown 代码围栏；APPLICABILITY 标签允许全角冒号与 **加粗** 包裹
    （实测模型常违反"恰好两行"约束，严格解析导致大量候选 unparsable）。
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.lower().startswith("text"):
            raw = raw[4:]
    lines = [x.strip() for x in raw.splitlines() if x.strip()]
    if not lines:
        return "", {}
    keywords, content_lines = [], []
    for ln in lines:
        norm = ln.replace("*", "").replace("：", ":")
        if norm.upper().startswith("APPLICABILITY:"):
            keywords = [k.strip() for k in
                        norm.split(":", 1)[1].replace("，", ",").split(",")
                        if k.strip()][:4]
        else:
            content_lines.append(ln)
    content = " ".join(content_lines).strip()[:300]
    # 模型偶尔把标签行混在正文里，只要拿到关键词就接受（正文已限 300 字）
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
    prompt = _DISTILL_PROMPT.format(tenant=tenant_id,
                                    score=cand.get("score", 5),
                                    digest=digest)
    try:
        from app.agent.llm_config import get_tenant_model
        model = await get_tenant_model(app, tenant_id)
        # [fix] 思考型模型思考 token 计入 completion，租户模型实例默认 max_tokens=1024
        # 时思考即可耗尽预算 → content 恒空（finish=length）。提炼单独放宽。
        model = model.bind(max_tokens=int(
            os.getenv("EXPERIENCE_DISTILL_MAX_TOKENS", "4096")))
        resp = await asyncio.wait_for(model.ainvoke(prompt), timeout=90)
    except Exception as e:
        logger.warning("distill model failed tenant=%s answer=%s: %s",
                       tenant_id, cand.get("answer_id", ""), str(e)[:120])
        return False
    text = (resp.content or "").strip() if hasattr(resp, "content") else str(resp).strip()
    content, applicability = _parse_digest(text)
    if not content:
        # 诊断线索：思考型模型 content 为空常因 max_tokens 被思考耗尽(finish=length)
        finish = ""
        try:
            md = getattr(resp, "response_metadata", None) or {}
            finish = str(md.get("finish_reason", ""))
        except Exception:  # noqa: BLE001
            pass
        logger.info("distill skipped (unparsable) tenant=%s answer=%s finish=%s text=%r",
                    tenant_id, cand.get("answer_id", ""), finish, text[:200])
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
