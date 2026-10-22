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
        "max_kb_size_mb FROM tenants WHERE tenant_id = %s AND deleted_at = 0 "
        "LIMIT 1", (tenant_id,),
    )
    if not row:
        soft(404, "租户不存在")
    return {"code": 0, "data": {"tenant_id": tenant_id,
            **{key: int(row[key] or 0) for key in (
                "max_concurrent_sessions", "daily_message_limit",
                "max_kb_docs", "max_kb_size_mb")}}}


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
def agent_summary_pending(request: Request, limit: int = 20):
    internal_guard(request)
    limit = limit if 1 <= limit <= 100 else 20
    threads = many(
        "SELECT m.thread_id, m.tenant_id, MAX(m.created_at) AS last_active "
        "FROM messages m LEFT JOIN session_summaries s ON "
        "s.thread_id = m.thread_id AND s.tenant_id = m.tenant_id "
        "WHERE s.thread_id IS NULL AND m.created_at < "
        "DATE_SUB(NOW(), INTERVAL 30 MINUTE) "
        "GROUP BY m.thread_id, m.tenant_id ORDER BY last_active DESC LIMIT %s",
        (limit,),
    )
    candidates = []
    for item in threads:
        messages = list(many(
            "SELECT role, content FROM messages WHERE thread_id = %s "
            "AND tenant_id = %s ORDER BY id DESC LIMIT 40",
            (item["thread_id"], item["tenant_id"]),
        ))
        messages.reverse()
        candidates.append({"thread_id": item["thread_id"],
                           "tenant_id": item["tenant_id"], "messages": messages})
    return {"code": 0, "data": candidates}


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
    write(
        "INSERT INTO session_summaries (tenant_id, thread_id, summary) "
        "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE summary = VALUES(summary)",
        (body["tenant_id"], body["thread_id"], body["summary"]),
    )
    return {"code": 0, "msg": "会话摘要已保存"}


# ---------- WebSocket cutover (not exposed by Nginx until acceptance) ----------

