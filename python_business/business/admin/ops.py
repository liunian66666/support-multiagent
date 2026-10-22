"""Operations dashboard, Agent proxy, archiving and model settings."""

import base64
import json
import os
import re
from datetime import date, datetime, timedelta

import httpx
import pymysql
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from business.admin.auth import Admin, current_admin, require_perm
from business.admin.catalog import to_int
from business.admin.sessions import DEFAULT_EXTRACT_RULES
from business.admin.tickets import AGENT_URL, reload_go_extract_rules
from business.core.errors import code_to_status, deny, soft
from business.core.requests import body_json
from business.customer.routes import iso
from business.infra.storage import cache, db, many, one, write

router = APIRouter()

# ============================================================================

def require_platform():
    """对齐 Go PlatformAdminOnly：仅平台管理员，非平台管理员 403 仅平台管理员可操作。"""
    def guard(admin: Admin = Depends(current_admin)) -> Admin:
        if not admin.is_platform_admin:
            deny(403, "仅平台管理员可操作")
        return admin
    return guard


# ---------- 代理到 Agent（Go ProxyToAgent 的等价物） ----------

def agent_call(method, path, query="", body=None, content_type="", trace_id="",
               tenant_id="", timeout=60):
    """对齐 Go agent.AgentRequest：透传 query/body/Content-Type/Trace/Tenant + 内部 token。

    返回 (bytes, None) 或 (None, err_text)。Go 侧不检查 Agent 的 HTTP 状态，
    拿到什么原样回写 200，这里保持一致。

    timeout 对齐 Go 的两个 client：业务读写 60s（httpClient），
    管理台透传 120s（adminHTTPClient，Prompt/知识库可能耗时较长）。
    """
    url = AGENT_URL + path + (("?" + query) if query else "")
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    if trace_id:
        headers["X-Trace-Id"] = trace_id
    if tenant_id:
        headers["X-Tenant-Id"] = tenant_id
    token = os.getenv("INTERNAL_API_TOKEN", "")
    if token:
        headers["X-Internal-Token"] = token
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method, url, content=body or None, headers=headers)
        return resp.content, None
    except Exception as exc:                      # noqa: BLE001 - 透传底层错误文本
        return None, str(exc)


def agent_payload_response(data):
    """把 Agent 的原始字节回写给前端，并按 body.code 改写 HTTP 状态码。

    Go 侧这里有两层：`ProxyToAgent` 写的是 `c.Data(http.StatusOK, "application/json", data)`，
    随后**全局 StatusCode 中间件**按 body.code 把状态改写（403→403、400→400、500→500）。
    所以端到端看到的状态码是 body.code 映射出来的，不是固定的 200。
    非 JSON 的响应体（例如 Agent 直接回的 "Internal Server Error" 纯文本）不改写，仍是 200。
    """
    status = 200
    try:
        parsed = json.loads(data)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        code = parsed.get("code")
        if isinstance(code, int) and not isinstance(code, bool):
            status = code_to_status(code)
    return Response(content=data, media_type="application/json",
                    status_code=status)


async def proxy_to_agent(request, method, agent_path, tenant_id, timeout=120):
    raw = await request.body()
    data, err = agent_call(method, agent_path, request.url.query, raw,
                           request.headers.get("content-type", ""),
                           request.headers.get("X-Trace-Id", ""), tenant_id,
                           timeout)
    if err is not None:
        return JSONResponse(status_code=500,
                            content={"code": 500, "msg": "调用agent失败:" + err})
    return agent_payload_response(data)


@router.get("/api/admin/session/stats")
async def admin_session_stats(request: Request,
                              admin: Admin = Depends(require_perm("session:view"))):
    return await proxy_to_agent(request, "GET", "/agent/session/stats", admin.tenant_id)


@router.get("/api/admin/session/cleanup-count")
async def admin_session_cleanup_count(request: Request,
                                      admin: Admin = Depends(require_perm("session:cleanup"))):
    return await proxy_to_agent(request, "GET", "/agent/session/cleanup-count",
                                admin.tenant_id)


@router.post("/api/admin/session/cleanup")
async def admin_session_cleanup(request: Request,
                                admin: Admin = Depends(require_perm("session:cleanup"))):
    return await proxy_to_agent(request, "POST", "/agent/session/cleanup", admin.tenant_id)


@router.get("/api/admin/token-usage")
async def admin_token_usage(request: Request,
                            admin: Admin = Depends(require_perm("usage:view",
                                                                biz_scope=False))):
    # 平台管理员可透传 tenant_id/days/group_by 全览；普通租户强制绑定自身 tenant_id
    query = {k: v for k, v in request.query_params.items()}
    if not admin.is_platform_admin:
        query["tenant_id"] = admin.tenant_id
    from urllib.parse import urlencode
    data, err = agent_call("GET", "/agent/token-usage", urlencode(sorted(query.items())),
                           None, "", request.headers.get("X-Trace-Id", ""),
                           admin.tenant_id)
    if err is not None:
        return JSONResponse(status_code=200,
                            content={"code": 500, "msg": "调用agent失败:" + err})
    return Response(content=data, media_type="application/json")


# ---------- 运营看板 ----------

def build_dashboard(tenant_id):
    """逐字对齐 Go repository.BuildDashboard。"""
    ov = one(
        "SELECT "
        " (SELECT COUNT(DISTINCT m.thread_id) FROM messages m WHERE m.tenant_id = %s) AS total_sessions,"
        " (SELECT COUNT(*) FROM messages m WHERE m.tenant_id = %s) AS total_messages,"
        " (SELECT COALESCE(SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END), 0)"
        "    FROM messages m WHERE m.tenant_id = %s) AS user_messages,"
        " (SELECT COUNT(*) FROM tickets t WHERE t.tenant_id = %s AND t.status IN (0,1)) AS open_tickets,"
        " (SELECT COUNT(DISTINCT t.thread_id) FROM tickets t WHERE t.tenant_id = %s"
        "    AND t.status IN (0,1)) AS handoff_sessions,"
        " (SELECT COALESCE(AVG(r.score), 0) FROM satisfaction_ratings r WHERE r.tenant_id = %s) AS avg_rating,"
        " (SELECT COUNT(*) FROM satisfaction_ratings r WHERE r.tenant_id = %s) AS rating_count,"
        " (SELECT COALESCE(SUM(CASE WHEN r.score >= 4 THEN 1 ELSE 0 END), 0) * 100.0"
        "    / NULLIF((SELECT COUNT(*) FROM satisfaction_ratings r WHERE r.tenant_id = %s), 0)"
        "    FROM satisfaction_ratings r WHERE r.tenant_id = %s) AS satisfaction_rate",
        (tenant_id,) * 9,
    ) or {}
    overview = {
        "total_sessions": int(ov.get("total_sessions") or 0),
        "total_messages": int(ov.get("total_messages") or 0),
        "user_messages": int(ov.get("user_messages") or 0),
        "open_tickets": int(ov.get("open_tickets") or 0),
        "handoff_sessions": int(ov.get("handoff_sessions") or 0),
        "avg_rating": float(ov.get("avg_rating") or 0),
        # 无评价时 Go 侧是 NULL，float64 零值输出 0
        "satisfaction_rate": float(ov["satisfaction_rate"])
        if ov.get("satisfaction_rate") is not None else 0.0,
        "rating_count": int(ov.get("rating_count") or 0),
    }

    start = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d") + " 00:00:00"
    rows = many(
        "SELECT DATE(created_at) AS day, COUNT(DISTINCT thread_id) AS sessions, "
        "COUNT(*) AS messages FROM messages WHERE tenant_id = %s AND created_at >= %s "
        "GROUP BY DATE(created_at)", (tenant_id, start))
    by_date = {}
    for row in rows:
        day = row["day"]
        key = day.strftime("%Y-%m-%d") if isinstance(day, (datetime, date)) else str(day)[:10]
        by_date[key] = {"date": key, "sessions": int(row["sessions"] or 0),
                        "messages": int(row["messages"] or 0)}
    trend = []
    for offset in range(6, -1, -1):
        key = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
        trend.append(by_date.get(key) or {"date": key, "sessions": 0, "messages": 0})

    names = {0: "待处理", 1: "接管中", 2: "已取消", 3: "已关闭"}
    tickets = [
        {"status": int(row["status"]), "name": names.get(int(row["status"]), ""),
         "count": int(row["count"] or 0)}
        for row in many("SELECT status, COUNT(*) AS count FROM tickets WHERE tenant_id = %s "
                        "GROUP BY status", (tenant_id,))
    ]

    agents = [
        {"handler_name": row["handler_name"], "total": int(row["total"] or 0),
         "open": int(row["open"] or 0)}
        for row in many(
            "SELECT handler_name, COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN status IN (0,1) THEN 1 ELSE 0 END), 0) AS open "
            "FROM tickets WHERE tenant_id = %s AND handler_name != '' "
            "GROUP BY handler_name ORDER BY total DESC", (tenant_id,))
    ]
    return {"overview": overview, "trend": trend, "tickets": tickets,
            # Go 这里是 nil slice（无数据时 JSON 为 null），不是空数组
            "agents": agents or None}


@router.get("/api/admin/ops/dashboard")
def admin_ops_dashboard(admin: Admin = Depends(require_perm("dashboard:view"))):
    if not admin.tenant_id:
        deny(401, "未获取到租户信息")
    try:
        data = build_dashboard(admin.tenant_id)
    except pymysql.MySQLError as exc:
        soft(500, "统计失败: " + str(exc))
    return {"code": 0, "data": data}


# ---------- 审计日志 ----------

@router.get("/api/admin/audit-logs")
def admin_audit_logs(page: str = "1", page_size: str = "20", action: str = "",
                     object_type: str = "", operator: str = "", ip: str = "",
                     time_from: str = "", time_to: str = "",
                     admin: Admin = Depends(require_perm("audit:view",
                                                         biz_scope=False))):
    # 注意：Go 里 page/pageSize 既用于查询也用于回显，但钳制发生在 repository 内部，
    # handler 回显的是 Atoi 得到的原始值（非法 → 0；page_size=999 也原样回显 999）
    echo_page = to_int(page, 0)
    echo_size = to_int(page_size, 0)
    p, size = echo_page, echo_size
    if p < 1:
        p = 1
    if size < 1 or size > 200:
        size = 20
    where, args = [], []
    # 平台管理员全览所有租户；租户管理端仅本租户
    if not admin.is_platform_admin:
        where.append("tenant_id = %s")
        args.append(admin.tenant_id)
    if action:
        where.append("action = %s")
        args.append(action)
    if object_type:
        where.append("object_type = %s")
        args.append(object_type)
    if operator:
        where.append("operator LIKE %s")
        args.append("%" + operator + "%")
    if ip:
        where.append("ip LIKE %s")
        args.append("%" + ip + "%")
    if time_from:
        where.append("created_at >= %s")
        args.append(time_from)
    if time_to:
        where.append("created_at <= %s")
        args.append(time_to)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        total = int(one(f"SELECT COUNT(*) AS n FROM audit_logs{clause}", tuple(args))["n"] or 0)
        rows = many(
            "SELECT id, tenant_id, operator, user_id, action, object_type, object_id, "
            f"detail, ip, created_at FROM audit_logs{clause} ORDER BY id DESC "
            "LIMIT %s OFFSET %s", tuple(args) + (size, (p - 1) * size))
    except pymysql.MySQLError as exc:
        soft(500, "查询审计日志失败:" + str(exc))
    items = [{
        "id": row["id"], "tenant_id": row["tenant_id"], "operator": row["operator"],
        "user_id": int(row["user_id"] or 0), "action": row["action"],
        "object_type": row["object_type"], "object_id": row["object_id"],
        "detail": row["detail"], "ip": row["ip"], "created_at": iso(row["created_at"]),
    } for row in rows]
    # GORM 的 Find 会把目标切片初始化为空切片，所以无数据时是 []（不是 null）
    return {"code": 0, "data": {"list": items, "total": total, "page": echo_page,
                                "page_size": echo_size}}


# ---------- 消息归档 ----------

ARCHIVE_BATCH_SIZE = 500
ARCHIVABLE_WHERE = ("m.created_at < %s AND (m.ticket_id IS NULL OR t.status IN (2, 3))")


def archive_days(raw):
    days = to_int(raw, 0)
    if days <= 0:
        days = 90
    if days > 365:
        days = 365
    return days


def count_archivable_messages(days, tenant_id):
    sql = ("SELECT COUNT(*) AS n FROM messages AS m "
           "LEFT JOIN tickets t ON m.ticket_id = t.id "
           f"WHERE {ARCHIVABLE_WHERE}")
    args = [datetime.now() - timedelta(days=days)]
    if tenant_id:
        sql += " AND m.tenant_id = %s"
        args.append(tenant_id)
    return int(one(sql, tuple(args))["n"] or 0)


def archive_old_messages(days, tenant_id):
    """对齐 Go ArchiveOldMessages：分批（500）迁移，单批事务，失败即停并返回已归档数。"""
    cutoff = datetime.now() - timedelta(days=days)
    total = 0
    while True:
        sql = ("SELECT m.id, m.thread_id, m.role, m.content, m.tenant_id, m.ticket_id, "
               "m.is_read, m.created_at FROM messages AS m "
               "LEFT JOIN tickets t ON m.ticket_id = t.id "
               f"WHERE {ARCHIVABLE_WHERE}")
        args = [cutoff]
        if tenant_id:
            sql += " AND m.tenant_id = %s"
            args.append(tenant_id)
        sql += " ORDER BY m.id ASC LIMIT %s"
        args.append(ARCHIVE_BATCH_SIZE)
        batch = many(sql, tuple(args))
        if not batch:
            break
        now = datetime.now()
        try:
            with db() as conn:
                conn.begin()
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO messages_archive (thread_id, role, content, tenant_id, "
                        "ticket_id, is_read, created_at, archived_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        [(r["thread_id"], r["role"], r["content"], r["tenant_id"],
                          r["ticket_id"], r["is_read"], r["created_at"], now)
                         for r in batch])
                    cur.execute(
                        "DELETE FROM messages WHERE id IN ("
                        + ",".join(["%s"] * len(batch)) + ")",
                        tuple(r["id"] for r in batch))
                conn.commit()
        except pymysql.MySQLError:
            break
        total += len(batch)
    return total


@router.get("/api/admin/messages/archive-count")
def admin_archive_count(days: str = "90",
                        admin: Admin = Depends(require_perm("session:cleanup"))):
    # 已知矛盾（Go 侧）：handler 注释写明「平台管理员全览」，tenant_id 传空即归档
    # 全部租户；但这两条路由挂在 Go 的 biz 组（BusinessOnly），平台管理员在进
    # handler 之前就被 403，空租户分支当前不可达。保持鉴权不变（改分组会让已验收
    # 的「平台管理员 403」行为漂移），分支代码保留，待 Go 下线时统一处理。
    tenant_id = "" if admin.is_platform_admin else admin.tenant_id
    try:
        count = count_archivable_messages(archive_days(days), tenant_id)
    except pymysql.MySQLError as exc:
        soft(500, "查询失败:" + str(exc))
    return {"code": 0, "data": {"count": count}}


@router.post("/api/admin/messages/archive")
def admin_archive_messages(days: str = "90",
                           admin: Admin = Depends(require_perm("session:cleanup"))):
    # 注意：Go 从 query 读 days（不是 body），POST 也一样
    # 平台管理员全租户分支当前不可达，原因见 admin_archive_count 上方注释
    tenant_id = "" if admin.is_platform_admin else admin.tenant_id
    archived = archive_old_messages(archive_days(days), tenant_id)
    return {"code": 0, "msg": "归档完成", "data": {"archived": archived}}


# ---------- API Key 加密（与 Go crypto 包二进制兼容） ----------

MODEL_KEY_DEV = "cs_prod_default_encrypt_key_2026!"


def model_key_bytes():
    key = os.getenv("MODEL_KEY_ENCRYPT_KEY", "")
    if not key:
        key = MODEL_KEY_DEV
    return key.encode()[:32].ljust(32, b"\x00")


def encrypt_api_key(plain):
    if not plain:
        return ""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    sealed = AESGCM(model_key_bytes()).encrypt(nonce, plain.encode(), None)
    return base64.b64encode(nonce + sealed).decode()


def decrypt_api_key(encrypted):
    """解密失败一律返回空串 —— Go 侧调用方全部忽略 err，行为等价。"""
    if not encrypted:
        return ""
    try:
        data = base64.b64decode(encrypted)
        if len(data) < 12:
            return ""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(model_key_bytes()).decrypt(data[:12], data[12:], None).decode()
    except Exception:                              # noqa: BLE001 - 与 Go 一致：坏密文当空
        return ""


def mask_api_key(plain):
    if not plain:
        return ""
    if len(plain) <= 4:
        return "****"
    return "..." + plain[-4:]


def is_masked_key(value, stored_plain=""):
    """判断前端回传的 API Key 是否为掩码（掩码 = 用户未修改，保留原值）。

    掩码有两种形态，来自 mask_api_key()：
      - 原 Key <= 4 位 → "****"（含星号）
      - 原 Key >  4 位 → "..." + 后 4 位（**不含星号**）

    只查星号会把 "...abcd" 当成真实 Key 写进库：用户打开模型配置页只改了模型名、
    没动 Key，保存后 Key 就变成掩码字面量，之后所有 LLM 调用都会失败。
    stored_plain 传入已存 Key 明文时，额外做一次精确比对兜底。
    """
    if not isinstance(value, str) or not value:
        return False
    if "*" in value:
        return True
    if value.startswith("...") and len(value) == 7:
        return True
    return bool(stored_plain) and value == stored_plain


def infer_provider_key(stored_key, model_name, api_base):
    if stored_key:
        return stored_key
    if not model_name and not api_base:
        return "custom"
    row = one("SELECT provider_key FROM model_providers WHERE status = 1 "
              "AND provider_key != 'custom' AND default_model = %s "
              "ORDER BY sort_order ASC, id ASC LIMIT 1", (model_name,))
    if row and row["provider_key"]:
        return row["provider_key"]
    if api_base:
        row = one("SELECT provider_key FROM model_providers WHERE status = 1 "
                  "AND provider_key != 'custom' AND default_api_base = %s "
                  "ORDER BY sort_order ASC, id ASC LIMIT 1", (api_base,))
        if row and row["provider_key"]:
            return row["provider_key"]
    return "custom"


TENANT_CONFIG_COLUMNS = (
    "tenant_id, model_provider, llm_provider_id, model_provider_key, model_name, "
    "model_api_base, model_api_key, chat_input_cache_price, chat_input_price, "
    "chat_output_price, embedding_price, embedding_provider, embedding_provider_id, "
    "embedding_provider_key, embedding_model_name, embedding_api_base, embedding_api_key"
)


def tenant_config_row(tenant_id):
    return one(f"SELECT {TENANT_CONFIG_COLUMNS} FROM tenants "
               f"WHERE tenant_id = %s AND deleted_at = 0 LIMIT 1", (tenant_id,))


def get_business_model_config(tenant_id):
    row = tenant_config_row(tenant_id)
    if not row:
        return None
    chat_key = decrypt_api_key(row["model_api_key"])
    emb_key = decrypt_api_key(row["embedding_api_key"])
    return {
        "tenant_id": tenant_id,
        "chat_input_cache_price": float(row["chat_input_cache_price"] or 0),
        "chat_input_price": float(row["chat_input_price"] or 0),
        "chat_output_price": float(row["chat_output_price"] or 0),
        "embedding_price": float(row["embedding_price"] or 0),
        "model_config": {
            "model_provider": row["model_provider"] or "",
            "provider_id": int(row["llm_provider_id"] or 0),
            "provider_key": infer_provider_key(row["model_provider_key"] or "",
                                               row["model_name"] or "",
                                               row["model_api_base"] or ""),
            "model_name": row["model_name"] or "",
            "model_api_base": row["model_api_base"] or "",
            "api_key_configured": bool(row["model_api_key"]),
            "api_key_masked": mask_api_key(chat_key),
        },
        "embedding_config": {
            "embedding_provider": row["embedding_provider"] or "",
            "provider_id": int(row["embedding_provider_id"] or 0),
            "provider_key": infer_provider_key(row["embedding_provider_key"] or "",
                                               row["embedding_model_name"] or "",
                                               row["embedding_api_base"] or ""),
            "embedding_model_name": row["embedding_model_name"] or "",
            "embedding_api_base": row["embedding_api_base"] or "",
            "api_key_configured": bool(row["embedding_api_key"]),
            "api_key_masked": mask_api_key(emb_key),
        },
    }


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def update_model_config(tenant_id, body):
    """对齐 Go service.UpdateModelConfig：逐字段与库中现值比较，只把真正变化的写入。"""
    row = tenant_config_row(tenant_id)
    if not row:
        raise LookupError("租户不存在")
    updates = {}

    mc = body.get("model_config")
    if isinstance(mc, dict):
        provider = mc.get("model_provider") or ""
        if provider and provider != (row["model_provider"] or ""):
            updates["model_provider"] = provider
        provider_id = mc.get("provider_id") or 0
        if not isinstance(provider_id, int) or isinstance(provider_id, bool):
            provider_id = 0
        if provider_id != int(row["llm_provider_id"] or 0):
            updates["llm_provider_id"] = provider_id
        provider_key = mc.get("provider_key") or ""
        if provider_key and provider_key != (row["model_provider_key"] or ""):
            updates["model_provider_key"] = provider_key
        api_key = mc.get("model_api_key") or ""
        clear_key = bool(mc.get("clear_api_key"))
        if provider == "custom" and not api_key and not clear_key:
            if not decrypt_api_key(row["model_api_key"]):
                raise ValueError("自定义对话模型必须填写 API Key")
        model_name = mc.get("model_name") or ""
        if model_name and model_name != (row["model_name"] or ""):
            updates["model_name"] = model_name
        if _num(mc.get("chat_input_cache_price")) != float(row["chat_input_cache_price"] or 0):
            updates["chat_input_cache_price"] = _num(mc.get("chat_input_cache_price"))
        if _num(mc.get("chat_input_price")) != float(row["chat_input_price"] or 0):
            updates["chat_input_price"] = _num(mc.get("chat_input_price"))
        if _num(mc.get("chat_output_price")) != float(row["chat_output_price"] or 0):
            updates["chat_output_price"] = _num(mc.get("chat_output_price"))
        api_base = mc.get("model_api_base") or ""
        if api_base and api_base != (row["model_api_base"] or ""):
            updates["model_api_base"] = api_base
        if clear_key:
            if row["model_api_key"]:
                updates["model_api_key"] = ""
        elif api_key and not is_masked_key(api_key, decrypt_api_key(row["model_api_key"])):
            if api_key != decrypt_api_key(row["model_api_key"]):
                updates["model_api_key"] = encrypt_api_key(api_key)

    ec = body.get("embedding_config")
    if isinstance(ec, dict):
        provider = ec.get("embedding_provider") or ""
        if provider and provider != (row["embedding_provider"] or ""):
            updates["embedding_provider"] = provider
        provider_id = ec.get("provider_id") or 0
        if not isinstance(provider_id, int) or isinstance(provider_id, bool):
            provider_id = 0
        if provider_id != int(row["embedding_provider_id"] or 0):
            updates["embedding_provider_id"] = provider_id
        provider_key = ec.get("provider_key") or ""
        if provider_key and provider_key != (row["embedding_provider_key"] or ""):
            updates["embedding_provider_key"] = provider_key
        api_key = ec.get("embedding_api_key") or ""
        clear_key = bool(ec.get("clear_api_key"))
        if provider == "custom" and not api_key and not clear_key:
            if not decrypt_api_key(row["embedding_api_key"]):
                raise ValueError("自定义 Embedding 模型必须填写 API Key")
        model_name = ec.get("embedding_model_name") or ""
        if model_name and model_name != (row["embedding_model_name"] or ""):
            updates["embedding_model_name"] = model_name
        if _num(ec.get("embedding_price")) != float(row["embedding_price"] or 0):
            updates["embedding_price"] = _num(ec.get("embedding_price"))
        api_base = ec.get("embedding_api_base") or ""
        if api_base and api_base != (row["embedding_api_base"] or ""):
            updates["embedding_api_base"] = api_base
        if clear_key:
            if row["embedding_api_key"]:
                updates["embedding_api_key"] = ""
        elif api_key and not is_masked_key(api_key,
                                           decrypt_api_key(row["embedding_api_key"])):
            if api_key != decrypt_api_key(row["embedding_api_key"]):
                updates["embedding_api_key"] = encrypt_api_key(api_key)

    if not updates:
        return None
    assignments = ", ".join(f"{key} = %s" for key in updates)
    write(f"UPDATE tenants SET {assignments} WHERE tenant_id = %s",
          tuple(updates.values()) + (tenant_id,))
    return updates


def model_update_scope(updates):
    has_chat = has_emb = False
    for key in updates:
        if key.startswith("model_") or key == "llm_provider_id" or key.startswith("chat_"):
            has_chat = True
        if key.startswith("embedding_") or key == "embedding_provider_id":
            has_emb = True
    if has_chat and not has_emb:
        return "chat"
    if not has_chat and has_emb:
        return "embedding"
    return "all"


@router.get("/api/admin/model-config")
def admin_model_config(admin: Admin = Depends(require_perm("model:view"))):
    if not admin.tenant_id:
        soft(401, "未登录或 token 无效")
    result = get_business_model_config(admin.tenant_id)
    if result is None:
        soft(404, "租户不存在")
    return {"code": 0, "data": result}


@router.put("/api/admin/model-config")
async def admin_model_config_update(request: Request,
                                    admin: Admin = Depends(require_perm("model:edit"))):
    if not admin.tenant_id:
        soft(401, "未登录或 token 无效")
    body = await body_json(request)
    if not isinstance(body, dict):
        soft(400, "EOF")
    try:
        updates = update_model_config(admin.tenant_id, body)
    except LookupError:
        soft(404, "租户不存在")
    except ValueError as exc:
        soft(500, str(exc))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    if updates is None:
        soft(400, "没有需要更新的字段")
    # 通知 Agent 清除该租户模型配置缓存（失败不影响主流程，同 Go）
    try:
        cache().publish("cs:model:cache:invalidate", json.dumps(
            {"tenant_id": admin.tenant_id, "scope": model_update_scope(updates)},
            ensure_ascii=False))
    except Exception:                              # noqa: BLE001 - best effort
        pass
    return {"code": 0, "msg": "模型配置已保存",
            "data": {"tenant_id": admin.tenant_id, "updated": updates}}


# ---------- 客户信息提取规则（平台级） ----------

def default_extract_rules_json():
    """复用阶段 3-2 的 DEFAULT_EXTRACT_RULES（(field, pattern) 元组），转成 API 的 JSON 形态。"""
    return [{"field": field, "pattern": pattern} for field, pattern in DEFAULT_EXTRACT_RULES]


# Go uses RE2 (regexp.Compile): no lookaround, no backreferences.
RE2_UNSUPPORTED = (("(?=", "(?="), ("(?!", "(?!"), ("(?<=", "(?<="), ("(?<!", "(?<!"),
                   ("\\1", "\\1"))


def compile_extract_pattern(pattern):
    """用 Go 的 RE2 语义校验；不通过时抛 ValueError，文案对齐 Go regexp 包。"""
    for token, _ in RE2_UNSUPPORTED:
        if token in pattern:
            raise ValueError("error parsing regexp: invalid or unsupported Perl syntax: `"
                             + token + "`")
    try:
        re.compile(pattern)
    except re.error as exc:
        text = str(exc)
        pos = getattr(exc, "pos", None) or 0
        tail = pattern[pos:] or pattern
        if "unterminated character set" in text:
            raise ValueError(f"error parsing regexp: missing closing ]: `{tail}`") from None
        if "unterminated subpattern" in text:
            raise ValueError(f"error parsing regexp: missing closing ): `{tail}`") from None
        if "nothing to repeat" in text:
            raise ValueError("error parsing regexp: missing argument to repetition "
                             f"operator: `{tail[:1]}`") from None
        if "bad escape" in text:
            raise ValueError(f"error parsing regexp: invalid escape sequence: `{pattern}`") \
                from None
        raise ValueError("error parsing regexp: " + text) from None


@router.get("/api/admin/extract-rules")
def admin_extract_rules(_admin: Admin = Depends(require_platform())):
    rows = many("SELECT config_key, config_value FROM platform_config "
                "WHERE config_key LIKE %s ORDER BY id ASC", ("cs_extract_rule_%",))
    rules = []
    for row in rows:
        field = row["config_key"].removeprefix("cs_extract_rule_")
        if field and row["config_value"]:
            rules.append({"field": field, "pattern": row["config_value"]})
    return {"code": 0, "data": rules or default_extract_rules_json()}


@router.put("/api/admin/extract-rules")
async def admin_extract_rules_update(request: Request,
                                     _admin: Admin = Depends(require_platform())):
    body = await body_json(request)
    if not isinstance(body, list):
        soft(400, "参数错误: json: cannot unmarshal object into Go value of type "
                  "[]model.ExtractRule")
    # 已修复（Go 侧缺陷）：Go 先 DELETE 全表再逐条校验，任一条 field/pattern 非法
    # 都会留下「旧规则已删、新规则只写了一部分」的中间态。这里先把整批校验完，
    # 全部通过才落库，失败时保持原规则不变。
    rules = []
    for item in body:
        if not isinstance(item, dict):
            soft(400, "参数错误: json: cannot unmarshal value into Go struct field "
                      "ExtractRule.field of type string")
        field = item.get("field") or ""
        pattern = item.get("pattern") or ""
        if not isinstance(field, str) or not isinstance(pattern, str):
            soft(400, "参数错误: json: cannot unmarshal value into Go struct field "
                      "ExtractRule.field of type string")
        if not field or not pattern:
            continue
        try:
            compile_extract_pattern(pattern)
        except ValueError as exc:
            soft(500, str(exc))
        rules.append((field, pattern))
    write("DELETE FROM platform_config WHERE config_key LIKE %s", ("cs_extract_rule_%",))
    now = datetime.now()
    for field, pattern in rules:
        write("INSERT INTO platform_config (config_key, config_value, description, "
              "created_at, updated_at) VALUES (%s, %s, %s, %s, %s)",
              ("cs_extract_rule_" + field, pattern, "客户信息提取规则-" + field, now, now))
    # Go 侧 extractRulesCache 是进程内缓存，不会感知本层的写入 → 主动失效（best effort）
    reload_go_extract_rules()
    return {"code": 0, "msg": "提取规则已更新"}


