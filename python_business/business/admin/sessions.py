"""Agent availability, conversations, customer profiles and media."""

import os
import re
import time
from datetime import datetime

import pymysql
from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel

from business.admin.auth import Admin, require_perm
from business.admin.catalog import to_int
from business.core.errors import deny, soft
from business.core.requests import body_json
from business.customer.routes import Customer, current_customer, iso
from business.infra.storage import many, one, write

router = APIRouter()

# ==========================================================================
# 在线客服状态 / 会话记录 / 客户档案 / 客户名册
# 全部为租户业务数据，平台管理员访问一律 403。
# 失败分支保持 HTTP 200 + body code 的软失败约定，用 soft() 原样保留。
# ==========================================================================

MSG_FILE_ROOT = "/app/msg_files"
MSG_IMAGE_MAX_MB = 5
MSG_VIDEO_MAX_MB = 50
MSG_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MSG_VIDEO_EXTS = {".mp4", ".webm"}


def tenant_of_thread(thread_id):
    """行为约定：首个冒号前的段，无冒号返回空串。"""
    head, sep, _ = (thread_id or "").partition(":")
    return head if sep else ""


def thread_in_tenant(thread_id, tenant_id):
    """租户归属校验（租户非空时校验 thread 前缀）。"""
    if not tenant_id:
        return
    if tenant_of_thread(thread_id) != tenant_id:
        deny(403, "无权访问该租户会话")


# ---------- 在线客服状态 ----------

AGENT_STATUS_COLUMNS = ("a.id, a.tenant_id, a.user_id, a.status, a.handling_count, "
                        "a.max_concurrent, a.updated_at, u.nickname, u.username")


def list_agent_status(tenant_id):
    rows = many(
        f"SELECT {AGENT_STATUS_COLUMNS} FROM agent_status AS a "
        "LEFT JOIN users u ON u.id = a.user_id AND u.deleted_at = 0 "
        "WHERE a.tenant_id = %s "
        "ORDER BY a.status DESC, a.handling_count ASC, a.id ASC",
        (tenant_id,),
    )
    data = []
    for row in rows:
        row["updated_at"] = iso(row["updated_at"])
        row["nickname"] = row["nickname"] or ""
        row["username"] = row["username"] or ""
        data.append(row)
    return data


@router.get("/api/admin/agent-status")
def admin_agent_status(admin: Admin = Depends(require_perm("ticket:view"))):
    try:
        return {"code": 0, "data": list_agent_status(admin.tenant_id)}
    except pymysql.MySQLError:
        deny(500, "查询客服状态失败")


@router.put("/api/admin/agent-status")
async def admin_agent_status_update(request: Request,
                                    admin: Admin = Depends(require_perm("ticket:handle"))):
    if admin.user_id == 0:
        deny(403, "app_key 管理员无需设置状态")
    body = await body_json(request)
    status = body.get("status") if isinstance(body, dict) else None
    if isinstance(status, bool) or not isinstance(status, int) or status not in (0, 1, 2):
        soft(400, "参数错误")
    try:
        write("UPDATE agent_status SET status = %s, updated_at = NOW() "
              "WHERE tenant_id = %s AND user_id = %s",
              (status, admin.tenant_id, admin.user_id))
    except pymysql.MySQLError:
        deny(500, "更新状态失败")
    return {"code": 0, "msg": "已更新"}


# ---------- 会话列表 ----------

SESSION_COLUMNS = (
    "m.thread_id, MAX(m.tenant_id) AS tenant_id, COUNT(*) AS message_count, "
    "COALESCE(SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END), 0) AS user_count, "
    "COALESCE(SUM(CASE WHEN m.role != 'user' AND m.is_read = 0 THEN 1 ELSE 0 END), 0) "
    "AS unread_count, MAX(m.created_at) AS last_active_at, "
    "COALESCE(MAX(CASE WHEN t.status IN (0,1) THEN 1 ELSE 0 END), 0) AS has_open_ticket, "
    "MAX(ss.summary) AS summary"
)

SESSION_JOINS = (
    "FROM messages m "
    "LEFT JOIN tickets t ON t.thread_id = m.thread_id AND t.status IN (0,1) "
    "LEFT JOIN customer_profiles cp ON cp.thread_id = m.thread_id "
    "LEFT JOIN session_summaries ss ON ss.thread_id = m.thread_id "
    "AND ss.tenant_id = m.tenant_id"
)

HANDOFF_HAVING = " HAVING COALESCE(MAX(CASE WHEN t.status IN (0,1) THEN 1 ELSE 0 END), 0) = 1"


def session_filter(tenant_id, keyword, date_from, date_to, tag):
    """会话列表的动态 where（COUNT 与主查询共用，tag 子查询在后）。"""
    where, args = ["m.tenant_id = %s"], [tenant_id]
    if keyword:
        where.append("(m.thread_id LIKE %s OR cp.name LIKE %s OR cp.phone LIKE %s "
                     "OR cp.email LIKE %s OR cp.address LIKE %s)")
        args.extend(["%" + keyword + "%"] * 5)
    if date_from:
        where.append("m.created_at >= %s")
        args.append(date_from + " 00:00:00")
    if date_to:
        where.append("m.created_at < %s")
        args.append(date_to + " 23:59:59")
    if tag:
        where.append("m.thread_id IN (SELECT thread_id FROM session_tags "
                     "WHERE tenant_id = %s AND tags LIKE %s)")
        args.extend([tenant_id, "%" + tag + "%"])
    return " AND ".join(where), tuple(args)


def list_sessions(tenant_id, page, size, keyword, date_from, date_to, handoff, tag):
    clause, args = session_filter(tenant_id, keyword, date_from, date_to, tag)
    # 已修复：total 曾用去重计数，只带 where、不带 handoffFilter，
    # 导致 handoff=1 时 total 远大于实际行数（分页控件显示错乱）。这里把 HAVING
    # 包进子查询，让 total 与列表行数口径一致。
    # 注意：HAVING 引用 t.status，子查询必须带上与主查询相同的 tickets JOIN，
    # 否则 MySQL 直接报 "Unknown column 't.status' in 'having clause'"。
    total = one(
        "SELECT COUNT(*) AS n FROM ("
        "SELECT m.thread_id FROM messages m "
        "LEFT JOIN tickets t ON t.thread_id = m.thread_id AND t.status IN (0,1) "
        "LEFT JOIN customer_profiles cp ON cp.thread_id = m.thread_id "
        "WHERE " + clause + " GROUP BY m.thread_id"
        + (HANDOFF_HAVING if handoff == 1 else "")
        + ") x",
        args,
    )["n"]
    profiles = {
        row["thread_id"]: row for row in many(
            "SELECT thread_id, name, phone, address, email, remark, extract_status "
            "FROM customer_profiles WHERE tenant_id = %s AND deleted_at = 0", (tenant_id,),
        )
    }
    rows = many(
        f"SELECT {SESSION_COLUMNS} {SESSION_JOINS} WHERE {clause} GROUP BY m.thread_id"
        + (HANDOFF_HAVING if handoff == 1 else "")
        + " ORDER BY last_active_at DESC LIMIT %s OFFSET %s",
        args + (size, (page - 1) * size),
    )
    tags = {
        row["thread_id"]: row["tags"] for row in many(
            "SELECT thread_id, tags FROM session_tags WHERE tenant_id = %s", (tenant_id,),
        )
    }
    items = []
    for row in rows:
        # 按会话逐条补最后一条消息内容（分页量小，可接受）
        last = one("SELECT content FROM messages WHERE thread_id = %s "
                   "ORDER BY id DESC LIMIT 1", (row["thread_id"],))
        profile = profiles.get(row["thread_id"]) or {}
        items.append({
            "thread_id": row["thread_id"],
            "tenant_id": row["tenant_id"],
            "message_count": int(row["message_count"] or 0),
            "user_count": int(row["user_count"] or 0),
            "unread_count": int(row["unread_count"] or 0),
            "last_content": (last or {}).get("content") or "",
            "last_active_at": iso(row["last_active_at"]),
            "has_open_ticket": bool(row["has_open_ticket"]),
            "name": profile.get("name") or "",
            "phone": profile.get("phone") or "",
            "address": profile.get("address") or "",
            "email": profile.get("email") or "",
            "remark": profile.get("remark") or "",
            "extract_status": int(profile.get("extract_status") or 0),
            "tags": tags.get(row["thread_id"]) or "",
            "summary": row["summary"] or "",
        })
    return items, total


@router.get("/api/admin/sessions")
def admin_sessions(page: str = "1", size: str = "20", q: str = "", start: str = "",
                   end: str = "", handoff: str = "", tag: str = "",
                   admin: Admin = Depends(require_perm("session:view"))):
    p = max(1, to_int(page, 0))          # 整数解析忽略错误 → 0 → 收敛为 1
    limit = to_int(size, 0)
    if limit < 1 or limit > 100:
        limit = 20
    try:
        items, total = list_sessions(
            admin.tenant_id, p, limit, q.strip(), start.strip(), end.strip(),
            1 if handoff == "1" else 0, tag.strip(),
        )
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "data": {"list": items, "total": total, "page": p, "size": limit}}


# ---------- 会话聊天记录（含归档） ----------

MESSAGE_ARCHIVE_SQL = (
    "SELECT id, thread_id, role, content, msg_type, tenant_id, ticket_id, is_read, "
    "created_at, 0 AS archived FROM messages WHERE thread_id = %s "
    "UNION ALL "
    "SELECT id, thread_id, role, content, msg_type, tenant_id, ticket_id, is_read, "
    "created_at, 1 AS archived FROM messages_archive WHERE thread_id = %s "
    "ORDER BY created_at ASC, id ASC"
)


def customer_profile(thread_id):
    """行为约定：查不到返回 None（JSON null），不报错。"""
    row = one(
        "SELECT id, thread_id, tenant_id, name, phone, address, email, remark, "
        "extract_status, created_at, updated_at FROM customer_profiles "
        "WHERE thread_id = %s AND deleted_at = 0 LIMIT 1",
        (thread_id,),
    )
    if not row:
        return None
    for field in ("name", "phone", "address", "email", "remark"):
        row[field] = row[field] or ""
    row["extract_status"] = int(row["extract_status"] or 0)
    row["created_at"] = iso(row["created_at"])
    row["updated_at"] = iso(row["updated_at"])
    return row


def message_with_archive(thread_id):
    data = []
    for row in many(MESSAGE_ARCHIVE_SQL, (thread_id, thread_id)):
        item = {
            "id": row["id"],
            "thread_id": row["thread_id"],
            "role": row["role"],
            "content": row["content"],
            "msg_type": row["msg_type"],
            "tenant_id": row["tenant_id"],
            "is_read": int(row["is_read"] or 0),
            "created_at": iso(row["created_at"]),
            "archived": bool(row["archived"]),
        }
        # ticket_id 为 NULL 时整个字段省略
        if row["ticket_id"] is not None:
            item["ticket_id"] = row["ticket_id"]
        data.append(item)
    return data


@router.get("/api/admin/sessions/{thread_id}/messages")
def admin_session_messages(thread_id: str,
                           admin: Admin = Depends(require_perm("session:view"))):
    thread_in_tenant(thread_id, admin.tenant_id)
    try:
        messages = message_with_archive(thread_id)
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "data": {"messages": messages,
                                "profile": customer_profile(thread_id)}}


# ---------- 客户档案（人工填写 / 重新提取） ----------

class CustomerProfileBody(BaseModel):
    name: str = ""
    phone: str = ""
    address: str = ""
    email: str = ""
    remark: str = ""


def save_customer_profile(tenant_id, thread_id, body):
    """行为约定：thread_id 唯一键 upsert，人工填写标记 2。"""
    values = (body.name, body.phone, body.address, body.email, body.remark)
    write(
        "INSERT INTO customer_profiles (thread_id, tenant_id, name, phone, address, email, "
        "remark, extract_status, created_at, updated_at, deleted_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 2, NOW(), NOW(), 0) "
        "ON DUPLICATE KEY UPDATE name = %s, phone = %s, address = %s, email = %s, "
        "remark = %s, extract_status = 2, updated_at = NOW()",
        (thread_id, tenant_id) + values + values,
    )


@router.put("/api/admin/sessions/{thread_id}/profile")
async def admin_session_profile(thread_id: str, request: Request,
                                admin: Admin = Depends(require_perm("session:profile:edit"))):
    thread_in_tenant(thread_id, admin.tenant_id)
    body = await body_json(request)
    if not isinstance(body, dict):
        soft(400, "参数错误: EOF")
    try:
        save_customer_profile(admin.tenant_id, thread_id, CustomerProfileBody(**{
            key: str(body.get(key) or "") for key in
            ("name", "phone", "address", "email", "remark")
        }))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "msg": "客户信息已保存"}


# 默认提取规则（零配置可用）：platform_config 中无 cs_extract_rule_* 时生效
DEFAULT_EXTRACT_RULES = (
    ("phone", r"(?:\+?86[\s-]?)?1[3-9]\d[\s-]?\d{4}[\s-]?\d{4}"),
    ("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)


def extract_rules():
    rows = many(
        "SELECT config_key, config_value FROM platform_config "
        "WHERE config_key LIKE %s", ("cs_extract_rule_%",),
    )
    rules = []
    for row in rows:
        field = (row["config_key"] or "").removeprefix("cs_extract_rule_")
        if field and row["config_value"]:
            rules.append((field, row["config_value"]))
    return tuple(rules) or DEFAULT_EXTRACT_RULES


def clean_phone(text):
    for token in (" ", "-", "\u00a0"):
        text = text.replace(token, "")
    text = text.strip()
    if text.startswith("+86"):
        return text[3:]
    return text[2:] if text.startswith("86") else text


def extract_from_content(content):
    """行为约定：只取每条规则的首个命中，phone 额外校数字边界。"""
    found = {}
    for field, pattern in extract_rules():
        try:
            # re.ASCII：RE2 的 \d \s \w 只认 ASCII，不能按 Unicode 语义放宽
            match = re.search(pattern, content or "", re.ASCII)
        except re.error:                    # 保存时已校验，这里仅防御
            continue
        if not match:
            continue
        value = match.group(0)
        if field == "phone":
            # RE2 不支持 lookbehind，用代码校验边界，防止从长数字串里截 11 位
            before = content[match.start() - 1] if match.start() > 0 else ""
            after = content[match.end()] if match.end() < len(content) else ""
            if (before.isdigit() and before.isascii()) or (after.isdigit() and after.isascii()):
                continue
            value = clean_phone(value)
        if value:
            found[field] = value
    return found


def upsert_extracted_profile(thread_id, tenant_id, extracted):
    """行为约定：只写 phone/email，保留人工填写字段。"""
    if not extracted:
        return
    row = one("SELECT id, phone, email FROM customer_profiles "
              "WHERE thread_id = %s AND deleted_at = 0 LIMIT 1", (thread_id,))
    phone = extracted.get("phone", row["phone"] if row else "")
    email = extracted.get("email", row["email"] if row else "")
    if row:
        write("UPDATE customer_profiles SET phone = %s, email = %s, extract_status = 1, "
              "updated_at = NOW() WHERE id = %s", (phone, email, row["id"]))
    else:
        write("INSERT INTO customer_profiles (thread_id, tenant_id, phone, email, "
              "extract_status, created_at, updated_at, deleted_at) "
              "VALUES (%s, %s, %s, %s, 1, NOW(), NOW(), 0)",
              (thread_id, tenant_id, phone, email))


@router.post("/api/admin/sessions/{thread_id}/reextract")
def admin_session_reextract(thread_id: str,
                            admin: Admin = Depends(require_perm("session:profile:edit"))):
    thread_in_tenant(thread_id, admin.tenant_id)
    try:
        merged = {}
        for row in many("SELECT content FROM messages WHERE thread_id = %s AND role = %s "
                        "ORDER BY id ASC", (thread_id, "user")):
            for field, value in extract_from_content(row["content"]).items():
                if value:
                    merged[field] = value
        upsert_extracted_profile(thread_id, tenant_of_thread(thread_id), merged)
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "msg": "重新提取完成"}


# ---------- 会话标签 ----------

def get_session_tags(tenant_id, thread_id):
    row = one("SELECT tags FROM session_tags WHERE tenant_id = %s AND thread_id = %s "
              "LIMIT 1", (tenant_id, thread_id))
    return (row["tags"] if row else "") or ""


@router.get("/api/admin/sessions/{thread_id}/tags")
def admin_session_tags(thread_id: str,
                       admin: Admin = Depends(require_perm("session:view"))):
    thread_in_tenant(thread_id, admin.tenant_id)
    return {"code": 0, "data": {"thread_id": thread_id,
                                "tags": get_session_tags(admin.tenant_id, thread_id)}}


@router.put("/api/admin/sessions/{thread_id}/tags")
async def admin_session_tags_update(thread_id: str, request: Request,
                                    admin: Admin = Depends(require_perm("session:profile:edit"))):
    thread_in_tenant(thread_id, admin.tenant_id)
    body = await body_json(request)
    if not isinstance(body, dict):
        soft(400, "参数错误: EOF")
    tags = str(body.get("tags") or "").strip()
    try:
        # session_tags 无软删除，使用「有则更新、无则插入」的 upsert
        write("INSERT INTO session_tags (tenant_id, thread_id, tags, updated_at) "
              "VALUES (%s, %s, %s, NOW()) "
              "ON DUPLICATE KEY UPDATE tags = %s, updated_at = NOW()",
              (admin.tenant_id, thread_id, tags, tags))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "msg": "标签已保存"}


# ---------- 会话图文上传 ----------

def media_kind(filename):
    suffix = os.path.splitext(filename or "")[1].lower()
    if suffix in MSG_IMAGE_EXTS:
        return "image"
    if suffix in MSG_VIDEO_EXTS:
        return "video"
    return ""


async def save_session_media(file: UploadFile, tenant_id: str):
    """Store customer and admin media under the authenticated tenant directory."""
    if file is None or not file.filename:
        soft(400, "请选择要上传的文件")
    kind = media_kind(file.filename)
    if not kind:
        soft(400, "仅支持图片(jpg/png/gif/webp)或视频(mp4/webm)")
    data = await file.read()
    limit = MSG_IMAGE_MAX_MB if kind == "image" else MSG_VIDEO_MAX_MB
    if len(data) > limit * 1024 * 1024:
        soft(400, f"{'图片' if kind == 'image' else '视频'}大小不能超过 {limit}MB")
    month = datetime.now().strftime("%Y%m")
    directory = os.path.join(MSG_FILE_ROOT, tenant_id, month)
    name = f"{time.time_ns()}{os.path.splitext(file.filename)[1].lower()}"
    try:
        os.makedirs(directory, mode=0o755, exist_ok=True)
        os.chmod(directory, 0o755)          # 容器 umask 可能是 0027，nginx 需可读
        with open(os.path.join(directory, name), "wb") as handle:
            handle.write(data)
    except OSError:
        soft(400, "文件保存失败")
    return {"code": 0, "data": {"url": f"/msg_files/{tenant_id}/{month}/{name}",
                                "msg_type": kind}}


@router.post("/api/admin/sessions/upload")
async def admin_session_upload(file: UploadFile = File(default=None),
                               admin: Admin = Depends(require_perm("ticket:handle"))):
    return await save_session_media(file, admin.tenant_id)


@router.post("/api/user/upload")
async def customer_session_upload(file: UploadFile = File(default=None),
                                  customer: Customer = Depends(current_customer)):
    return await save_session_media(file, customer.tenant_id)


# ---------- 客户名册 ----------

CUSTOMER_SQL = (
    "SELECT p.thread_id, p.tenant_id, p.name, p.phone, p.address, p.email, p.remark, "
    "p.extract_status, p.created_at, p.updated_at, m.last_active_at, "
    "(SELECT COUNT(*) FROM messages WHERE thread_id = p.thread_id) "
    "+ (SELECT COUNT(*) FROM messages_archive WHERE thread_id = p.thread_id) "
    "AS message_count "
    "FROM customer_profiles p "
    "LEFT JOIN (SELECT thread_id, MAX(created_at) AS last_active_at "
    "FROM (SELECT thread_id, created_at FROM messages WHERE tenant_id = %s "
    "UNION ALL SELECT thread_id, created_at FROM messages_archive WHERE tenant_id = %s) u "
    "GROUP BY thread_id) m ON m.thread_id = p.thread_id "
    "WHERE "
)


def list_customers(tenant_id, page, size, keyword):
    where, args = ["p.tenant_id = %s"], [tenant_id]
    if keyword:
        where.append("(p.name LIKE %s OR p.phone LIKE %s OR p.email LIKE %s "
                     "OR p.address LIKE %s OR p.remark LIKE %s)")
        args.extend(["%" + keyword + "%"] * 5)
    clause = " AND ".join(where)
    total = one("SELECT COUNT(*) AS n FROM customer_profiles p WHERE " + clause,
                tuple(args))["n"]
    rows = many(CUSTOMER_SQL + clause + " ORDER BY COALESCE(m.last_active_at, p.updated_at) "
                "DESC LIMIT %s OFFSET %s",
                (tenant_id, tenant_id) + tuple(args) + (size, (page - 1) * size))
    items = []
    for row in rows:
        items.append({
            "thread_id": row["thread_id"],
            "tenant_id": row["tenant_id"],
            "name": row["name"] or "",
            "phone": row["phone"] or "",
            "address": row["address"] or "",
            "email": row["email"] or "",
            "remark": row["remark"] or "",
            "extract_status": int(row["extract_status"] or 0),
            "last_active_at": iso(row["last_active_at"]),
            # 已修复：曾因查询字段名与结果模型不一致导致计数恒为 0。
            # 这里按 thread 统计 messages + messages_archive。
            "message_count": int(row["message_count"] or 0),
            "created_at": iso(row["created_at"]),
            "updated_at": iso(row["updated_at"]),
        })
    return items, total


@router.get("/api/admin/customers")
def admin_customers(page: str = "1", size: str = "20", q: str = "",
                    admin: Admin = Depends(require_perm("customer:view"))):
    p = max(1, to_int(page, 0))
    limit = to_int(size, 0)
    if limit < 1 or limit > 100:
        limit = 20
    keyword = q.strip()
    try:
        items, total = list_customers(admin.tenant_id, p, limit, keyword)
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    # 客户列表为空时序列化成 null，不是 []（会话列表相反）
    return {"code": 0, "data": {"list": items or None, "total": total,
                                "page": p, "size": limit, "q": keyword}}


