"""Internal read/write contracts consumed by the Python Agent."""

import hashlib
from fastapi import APIRouter, Request

from business.admin.content import resolve_chat_config, resolve_emb_config
from business.admin.ops import decrypt_api_key, tenant_config_row
from business.core.constants import TENANT_ID
from business.core.errors import deny, soft
from business.core.requests import body_json
from business.customer.routes import internal_guard
from business.infra.storage import many, one, write

router = APIRouter()

@router.get("/api/agent/tenant-quota")
def agent_tenant_quota(request: Request, tenant_id: str = ""):
    internal_guard(request)
    if not TENANT_ID.fullmatch(tenant_id):
        soft(400, "缺少或无效的 tenant_id")
    row = one(
        "SELECT max_concurrent_sessions, daily_message_limit, max_kb_docs, "
        "max_kb_size_mb, memory_enabled FROM tenants WHERE tenant_id = %s AND deleted_at = 0 "
        "LIMIT 1", (tenant_id,),
    )
    if not row:
        soft(404, "租户不存在")
    return {"code": 0, "data": {"tenant_id": tenant_id,
            **{key: int(row[key] or 0) for key in (
                "max_concurrent_sessions", "daily_message_limit",
                "max_kb_docs", "max_kb_size_mb")},
            # 记忆注入租户级开关：列缺失（旧库未 ALTER）时默认开
            "memory_enabled": int(row.get("memory_enabled")
                                  if row.get("memory_enabled") is not None else 1)}}


@router.get("/api/agent/model-config")
def agent_internal_model_config(request: Request, tenant_id: str = ""):
    internal_guard(request)
    if not TENANT_ID.fullmatch(tenant_id):
        soft(400, "tenant_id 不能为空")
    row = tenant_config_row(tenant_id)
    if not row:
        soft(404, "租户不存在")
    try:
        chat = resolve_chat_config(tenant_id)
        embedding = resolve_emb_config(tenant_id)
    except ValueError as exc:
        soft(404, str(exc))
    chat_own = bool(decrypt_api_key(row["model_api_key"])) and (
        int(row["llm_provider_id"] or 0) > 0 or
        (row["model_provider"] == "custom" and
         bool(row["model_name"] or row["model_api_base"])))
    emb_own = bool(decrypt_api_key(row["embedding_api_key"])) and (
        int(row["embedding_provider_id"] or 0) > 0 or
        (row["embedding_provider"] == "custom" and
         bool(row["embedding_model_name"] or row["embedding_api_base"])))
    prices = {key: float(row[key] or 0) for key in (
        "chat_input_cache_price", "chat_input_price", "chat_output_price",
        "embedding_price")}
    if tenant_id != "t_admin" and (not chat_own or not emb_own):
        platform = tenant_config_row("t_admin")
        if platform:
            for key in prices:
                if (key == "embedding_price" and not emb_own or
                    key != "embedding_price" and not chat_own):
                    prices[key] = float(platform[key] or 0)
    signature = "|".join((chat[0], chat[1], chat[3], chat[2],
                          embedding[0], embedding[1], embedding[3], embedding[2]))
    return {"code": 0, "data": {
        "tenant_id": tenant_id, "use_custom": chat_own,
        "model_provider": chat[0], "model_name": chat[1],
        "api_key": chat[3], "api_base": chat[2],
        **prices,
        "config_version": hashlib.sha256(signature.encode()).hexdigest()[:16],
        "embedding": {"use_custom": emb_own, "provider": embedding[0],
                      "model_name": embedding[1], "api_key": embedding[3],
                      "api_base": embedding[2]},
    }}


@router.get("/api/agent/session-summaries/pending")
def agent_summary_pending(request: Request, limit: int = 20, idle_minutes: int = 30,
                          thread_id: str = ""):
    """水位增量候选：水位后有新消息且已空闲 idle_minutes 的会话。

    [ours] 长对话记忆：原实现按 s.thread_id IS NULL 只挑“从未摘要”的会话，
    摘要每会话仅生成一次且曾取最近 40 条覆盖写；改为携带水位与增量消息，
    由 agent 侧做滚动合并并回写新水位。idle_minutes 下限 0（评测场景可调小）。
    """
    internal_guard(request)
    limit = limit if 1 <= limit <= 100 else 20
    idle = idle_minutes if 0 <= idle_minutes <= 1440 else 30
    threads = many(
        "SELECT m.thread_id, m.tenant_id, MAX(m.created_at) AS last_active, "
        "COALESCE(MAX(s.last_summarized_message_id), 0) AS watermark, "
        "MAX(s.summary) AS old_summary "
        "FROM messages m LEFT JOIN session_summaries s ON "
        "s.thread_id = m.thread_id AND s.tenant_id = m.tenant_id "
        "WHERE m.created_at < DATE_SUB(NOW(), INTERVAL %s MINUTE) "
        + ("AND m.thread_id = %s " if thread_id else "") +
        "GROUP BY m.thread_id, m.tenant_id "
        "HAVING MAX(m.id) > COALESCE(MAX(s.last_summarized_message_id), 0) "
        "ORDER BY last_active DESC LIMIT %s",
        ((idle, thread_id, limit) if thread_id else (idle, limit)),
    )
    candidates = []
    for item in threads:
        messages = list(many(
            "SELECT id, role, content FROM messages WHERE thread_id = %s "
            "AND tenant_id = %s AND id > %s ORDER BY id ASC LIMIT 40",
            (item["thread_id"], item["tenant_id"], item["watermark"]),
        ))
        if not messages:
            continue
        candidates.append({"thread_id": item["thread_id"],
                           "tenant_id": item["tenant_id"],
                           "watermark": int(item["watermark"]),
                           "old_summary": item["old_summary"] or "",
                           "messages": messages})
    return {"code": 0, "data": candidates}


@router.get("/api/agent/session-summaries")
def agent_summary_get(request: Request, thread_id: str = ""):
    """读路径：按 thread 取当前摘要（agent 续聊时拼入 prompt）。"""
    internal_guard(request)
    if not thread_id:
        soft(400, "thread_id 不能为空")
    row = one(
        "SELECT summary, last_summarized_message_id, summary_version "
        "FROM session_summaries WHERE thread_id = %s LIMIT 1", (thread_id,),
    )
    if not row or not row["summary"]:
        return {"code": 0, "data": None}
    return {"code": 0, "data": {
        "summary": row["summary"],
        "watermark": int(row["last_summarized_message_id"] or 0),
        "version": int(row["summary_version"] or 0)}}


@router.post("/api/agent/session-summaries")
async def agent_summary_save(request: Request):
    internal_guard(request)
    body = await body_json(request)
    if not isinstance(body, dict) or not all(
        isinstance(body.get(key), str) and body[key]
        for key in ("thread_id", "tenant_id", "summary")
    ):
        soft(400, "thread_id/tenant_id/summary 不能为空")
    if (not TENANT_ID.fullmatch(body["tenant_id"]) or
        not body["thread_id"].startswith(body["tenant_id"] + ":")):
        deny(403, "会话不属于该租户")
    # [ours] 长对话记忆：水位随摘要原子推进；last_message_id 必须是十进制正整数
    last_message_id = body.get("last_message_id", 0)
    if not isinstance(last_message_id, int) or last_message_id < 0:
        soft(400, "last_message_id 必须是非负整数")
    write(
        "INSERT INTO session_summaries (tenant_id, thread_id, summary, "
        "last_summarized_message_id, summary_version, summarized_at) "
        "VALUES (%s, %s, %s, %s, 1, NOW()) "
        "ON DUPLICATE KEY UPDATE "
        "summary = IF(VALUES(last_summarized_message_id) > last_summarized_message_id, "
        "VALUES(summary), summary), "
        "summary_version = IF(VALUES(last_summarized_message_id) > last_summarized_message_id, "
        "summary_version + 1, summary_version), "
        "summarized_at = IF(VALUES(last_summarized_message_id) > last_summarized_message_id, "
        "NOW(), summarized_at), "
        "last_summarized_message_id = GREATEST(last_summarized_message_id, "
        "VALUES(last_summarized_message_id))",
        (body["tenant_id"], body["thread_id"], body["summary"], last_message_id),
    )
    return {"code": 0, "msg": "会话摘要已保存"}


# ---------- WebSocket cutover (not exposed by Nginx until acceptance) ----------

