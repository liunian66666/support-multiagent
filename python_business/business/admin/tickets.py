"""Human-support tickets, takeover, messaging and approval decisions."""

import os
from datetime import datetime, timedelta

import httpx
import pymysql
from fastapi import APIRouter, Depends, Request

from business.admin.auth import Admin, require_perm, user_perms, write_audit
from business.admin.catalog import to_int
from business.admin.sessions import tenant_of_thread, extract_from_content, upsert_extracted_profile
from business.core.errors import deny, soft
from business.core.requests import body_json
from business.customer.routes import iso
from business.infra.runtime import ws_hub
from business.infra.storage import many, one, write

router = APIRouter()

# ==========================================================================
# 阶段 3-3：工单域（12 条）
# 这批端点不是纯 SQL：接管/回复/关闭/转交要推 WS、待处理接管还要唤醒 Agent 的 HITL 中断。
# 客户与管理端的长连接由业务层 WebSocket Hub 维护，推送走本地 Hub
# POST /api/agent/ws/broadcast（本轮新增，仅转发不落库），保证只有一个 WS Hub。
# ==========================================================================

AGENT_URL = os.getenv("CS_AGENT_URL", "http://agent:8000")

TICKET_STATUS_PENDING = 0
TICKET_STATUS_HANDLING = 1
TICKET_STATUS_CANCELED = 2
TICKET_STATUS_CLOSED = 3

TICKET_COLUMNS = ("id, thread_id, ticket_no, user_id, reason, status, priority, tenant_id, "
                  "handler_uid, handler_name, closed_at, created_at")
TICKET_LOG_COLUMNS = ("id, ticket_id, thread_id, tenant_id, action, old_status, new_status, "
                      "old_priority, new_priority, operator, remark, created_at")

# 权限目录全量（用于 IsAdminUser 判定）
ALL_PERMISSIONS = {
    "session:view", "session:profile:edit", "session:cleanup",
    "ticket:view", "ticket:handle", "customer:view", "dashboard:view",
    "knowledge:view", "knowledge:add", "knowledge:edit", "knowledge:remove",
    "prompt:view", "prompt:add", "prompt:edit", "prompt:remove",
    "model:view", "model:edit", "usage:view", "audit:view",
    "quickreply:view", "quickreply:add", "quickreply:edit", "quickreply:remove",
    "rating:view",
    "member:view", "member:add", "member:edit", "member:remove",
    "role:view", "role:add", "role:edit", "role:remove",
}


def go_broadcast(target, payload, key="", tenant_id=""):
    """Deliver ticket and human-agent updates through the local Python Hub."""
    ws_hub.broadcast_from_thread(target, payload, key=key,
                                 tenant_id=tenant_id)


def reload_go_extract_rules():
    """Rules are read from shared storage; there is no gateway-side cache."""
    return


def agent_resume(thread_id, confirmed, wait_time, ticket_no, trace_id=""):
    """行为约定：POST {agent}/agent/resume，传输失败重试一次。"""
    payload = {"thread_id": thread_id, "confirmed": confirmed,
               "wait_time": wait_time, "ticket_id": ticket_no}
    headers = {"X-Internal-Token": os.getenv("INTERNAL_API_TOKEN", ""),
               "X-Tenant-Id": tenant_of_thread(thread_id),
               "X-Trace-Id": trace_id}
    error = None
    for _ in range(2):
        try:
            response = httpx.post(AGENT_URL + "/agent/resume", json=payload,
                                  headers=headers, timeout=60)
            return (response.json() or {}).get("content") or ""
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            error = exc
    raise RuntimeError(str(error))


def save_message(thread_id, role, content, msg_type="", ticket_id=None, answer_id=""):
    """行为约定：用户消息默认已读，其余未读；文本消息触发客户信息提取。

    [ours] S1：answer_id 为 agent 返回的轮次归因锚点，仅 AI 消息携带，默认空串。
    """
    if not content:
        return
    msg_type = msg_type or "text"
    write(
        "INSERT INTO messages (thread_id, role, content, msg_type, tenant_id, ticket_id, "
        "is_read, answer_id, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())",
        (thread_id, role, content, msg_type, tenant_of_thread(thread_id), ticket_id,
         1 if role == "user" else 0, (answer_id or "")[:64]),
    )
    if msg_type == "text" and role == "user":
        upsert_extracted_profile(thread_id, tenant_of_thread(thread_id),
                                 extract_from_content(content))


def add_ticket_log(ticket, action, old_status, new_status, old_priority, new_priority,
                   operator, remark):
    write(
        "INSERT INTO ticket_logs (ticket_id, thread_id, tenant_id, action, old_status, "
        "new_status, old_priority, new_priority, operator, remark, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
        (ticket["id"], ticket["thread_id"], ticket["tenant_id"], action, old_status,
         new_status, old_priority, new_priority, operator, remark),
    )


def adjust_handling(tenant_id, user_id, delta):
    """行为约定：达上限转忙碌(2)，否则在线(1)；离线(0)不联动。"""
    if not tenant_id or not user_id:
        return
    row = one("SELECT id, status, handling_count, max_concurrent FROM agent_status "
              "WHERE tenant_id = %s AND user_id = %s LIMIT 1", (tenant_id, user_id))
    if not row:
        return
    count = int(row["handling_count"] or 0) + delta
    if count < 0:
        count = 0
    status = int(row["status"] or 0)
    if status != 0:
        status = 2 if count >= int(row["max_concurrent"] or 3) else 1
    write("UPDATE agent_status SET handling_count = %s, status = %s, updated_at = NOW() "
          "WHERE id = %s", (count, status, row["id"]))


def update_ticket_status(ticket, status):
    """行为约定：关闭/取消时补 closed_at（工单消息窗口上界）。"""
    ticket["status"] = status
    if status in (TICKET_STATUS_CANCELED, TICKET_STATUS_CLOSED):
        write("UPDATE tickets SET status = %s, closed_at = NOW() WHERE id = %s",
              (status, ticket["id"]))
        ticket["closed_at"] = datetime.now()
    else:
        write("UPDATE tickets SET status = %s WHERE id = %s", (status, ticket["id"]))


def ticket_json(ticket, unread=0):
    item = {
        "id": ticket["id"],
        "thread_id": ticket["thread_id"],
        "ticket_no": ticket["ticket_no"],
        "user_id": ticket["user_id"],
        "reason": ticket["reason"],
        "status": int(ticket["status"] or 0),
        "priority": int(ticket["priority"] or 0),
        "tenant_id": ticket["tenant_id"],
        "handler_uid": int(ticket["handler_uid"] or 0),
        "handler_name": ticket["handler_name"] or "",
    }
    if ticket.get("closed_at"):
        item["closed_at"] = iso(ticket["closed_at"])   # 关闭时间为 NULL 时整个字段省略
    item["created_at"] = iso(ticket["created_at"])
    # UnreadCount 是只读字段，详情/推送里恒为 0，列表里才填充
    item["unread_count"] = unread
    return item


def ticket_id_of(raw):
    """行为约定：非数字/负数/越界都算参数错误。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        soft(400, "参数错误")
    if value < 0 or value > 0xFFFFFFFFFFFFFFFF:
        soft(400, "参数错误")
    return value


def load_ticket(raw_id, tenant_id):
    ticket = one(f"SELECT {TICKET_COLUMNS} FROM tickets WHERE id = %s AND deleted_at = 0 "
                 f"LIMIT 1", (ticket_id_of(raw_id),))
    if not ticket:
        soft(404, "工单不存在")
    if ticket["tenant_id"] != tenant_id:
        deny(403, "无权访问该租户工单")
    return ticket


def can_handle_ticket(admin, ticket):
    """行为约定：app_key 管理员 / 全权限管理员 / 本人工单。"""
    if admin.user_id == 0:
        return True
    if ALL_PERMISSIONS <= user_perms(admin.user_id):
        return True
    return int(ticket["handler_uid"] or 0) == admin.user_id


def handler_label(name):
    return name or "-"


def current_handler_name(admin):
    """行为约定：子账号昵称优先，无则用户名；app_key 管理员记租户 ID。"""
    if admin.user_id > 0:
        row = one("SELECT nickname, username FROM users WHERE id = %s AND deleted_at = 0 "
                  "LIMIT 1", (admin.user_id,))
        if row:
            return row["nickname"] or row["username"] or admin.tenant_id
    return admin.tenant_id


# ---------- 列表 / 详情 / 消息 / 日志 / 已读 ----------

def list_tickets(tenant_id, status, page, size, handler_uid, keyword, priority):
    if page < 1:
        page = 1
    if size < 1 or size > 100:
        size = 20
    where, args = ["tenant_id = %s", "deleted_at = 0"], [tenant_id]
    if status >= 0:
        where.append("status = %s")
        args.append(status)
    if handler_uid > 0:
        where.append("handler_uid = %s")
        args.append(handler_uid)
    if keyword:
        where.append("(ticket_no LIKE %s OR user_id LIKE %s OR handler_name LIKE %s)")
        args.extend(["%" + keyword + "%"] * 3)
    if priority >= 0:
        where.append("priority = %s")
        args.append(priority)
    clause = " AND ".join(where)
    total = one(f"SELECT COUNT(*) AS n FROM tickets WHERE {clause}", tuple(args))["n"]
    rows = many(
        f"SELECT {TICKET_COLUMNS} FROM tickets WHERE {clause} "
        f"ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
        tuple(args) + (size, (page - 1) * size),
    )
    items = []
    for row in rows:
        unread = one("SELECT COUNT(*) AS n FROM messages WHERE thread_id = %s "
                     "AND role != %s AND is_read = 0", (row["thread_id"], "user"))["n"]
        items.append(ticket_json(row, int(unread or 0)))
    return items, int(total or 0), page, size


@router.get("/api/admin/tickets")
def admin_tickets(status: str = "", page: str = "1", size: str = "20", q: str = "",
                  priority: str = "", scope: str = "",
                  admin: Admin = Depends(require_perm("ticket:view"))):
    state = -1
    if status:
        value = to_int(status)
        if value is not None:
            state = value
    p, limit = to_int(page, 0), to_int(size, 0)
    level = -1
    if priority:
        value = to_int(priority)
        if value is not None and 0 <= value <= 3:
            level = value
    handler_uid = admin.user_id if scope == "mine" else 0
    try:
        items, total, p, limit = list_tickets(admin.tenant_id, state, p, limit,
                                              handler_uid, q.strip(), level)
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "data": {"list": items, "total": total, "page": p, "size": limit}}


@router.get("/api/admin/tickets/transfer-targets")
def admin_ticket_transfer_targets(admin: Admin = Depends(require_perm("ticket:handle"))):
    try:
        rows = many("SELECT id, nickname, username FROM users WHERE tenant_id = %s "
                    "AND status = 1 AND deleted_at = 0 ORDER BY id ASC",
                    (admin.tenant_id,))
    except pymysql.MySQLError:
        deny(500, "查询客服失败")
    return {"code": 0, "data": [{"id": row["id"],
                                 "nickname": row["nickname"] or row["username"],
                                 "username": row["username"]} for row in rows]}


@router.get("/api/admin/tickets/{id}")
def admin_ticket_detail(id: str, admin: Admin = Depends(require_perm("ticket:view"))):
    return {"code": 0, "data": ticket_json(load_ticket(id, admin.tenant_id))}


def ticket_message(row, *, archived=False):
    item = {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "role": row["role"],
        "content": row["content"],
        # 合并归档时构造消息模型但没带 MsgType → 归档消息 msg_type 恒为空串
        "msg_type": "" if archived else row["msg_type"],
        "tenant_id": row["tenant_id"],
        "is_read": int(row["is_read"] or 0),
        "created_at": iso(row["created_at"]),
    }
    if row["ticket_id"] is not None:
        item["ticket_id"] = row["ticket_id"]
    return item


def ticket_messages(ticket):
    """行为约定：待处理=创建+2s；接管中=now；已关闭/取消=关闭+2s。"""
    status = int(ticket["status"] or 0)
    if status == TICKET_STATUS_PENDING:
        upper = ticket["created_at"] + timedelta(seconds=2)
    elif status == TICKET_STATUS_HANDLING:
        upper = datetime.now()
    elif ticket["closed_at"]:
        upper = ticket["closed_at"] + timedelta(seconds=2)
    else:
        upper = ticket["created_at"] + timedelta(seconds=2)
    rows = many("SELECT id, thread_id, role, content, msg_type, tenant_id, ticket_id, "
                "is_read, created_at FROM messages WHERE thread_id = %s AND created_at <= %s "
                "ORDER BY id ASC", (ticket["thread_id"], upper))
    items = [ticket_message(row) for row in rows]
    archived = many("SELECT id, thread_id, role, content, tenant_id, ticket_id, is_read, "
                    "created_at FROM messages_archive WHERE thread_id = %s "
                    "AND created_at <= %s ORDER BY id ASC", (ticket["thread_id"], upper))
    if archived:
        merged = items + [ticket_message(row, archived=True) for row in archived]
        merged.sort(key=lambda m: (m["created_at"], m["id"]))
        return merged
    return items


@router.get("/api/admin/tickets/{id}/messages")
def admin_ticket_messages(id: str, admin: Admin = Depends(require_perm("ticket:view"))):
    ticket = load_ticket(id, admin.tenant_id)
    try:
        return {"code": 0, "data": ticket_messages(ticket)}
    except pymysql.MySQLError as exc:
        soft(500, str(exc))


@router.get("/api/admin/tickets/{id}/logs")
def admin_ticket_logs(id: str, admin: Admin = Depends(require_perm("ticket:view"))):
    ticket = load_ticket(id, admin.tenant_id)
    try:
        rows = many(f"SELECT {TICKET_LOG_COLUMNS} FROM ticket_logs WHERE ticket_id = %s "
                    f"ORDER BY id ASC", (ticket["id"],))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    data = []
    for row in rows:
        row["created_at"] = iso(row["created_at"])
        for field in ("old_status", "new_status", "old_priority", "new_priority"):
            row[field] = int(row[field] or 0)
        row["operator"] = row["operator"] or ""
        row["remark"] = row["remark"] or ""
        data.append(row)
    return {"code": 0, "data": data}


@router.post("/api/admin/tickets/{id}/read")
def admin_ticket_read(id: str, admin: Admin = Depends(require_perm("ticket:view"))):
    ticket = load_ticket(id, admin.tenant_id)
    try:
        write("UPDATE messages SET is_read = 1 WHERE thread_id = %s AND role != %s "
              "AND is_read = 0", (ticket["thread_id"], "user"))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "msg": "ok"}


# ---------- 状态流转 ----------

def takeover_ticket(ticket, wait_time, trace_id, handler_uid, handler_name):
    if int(ticket["status"] or 0) == TICKET_STATUS_HANDLING:
        return "该工单已在人工接管中"
    msg = ""
    status = int(ticket["status"] or 0)
    claimed = True
    handler_name = handler_name or "system"
    if status == TICKET_STATUS_PENDING:
        # [ours] CAS：单条 autocommit UPDATE 的 WHERE 谓词在 InnoDB 行锁下按最新已提交
        # 版本求值，并发接管的第二方会阻塞到第一方提交后得到 rowcount=0，即抢锁失败。
        # 抢锁必须在唤醒 Agent 之前 —— 否则两个客服都会各调一次 /agent/resume，
        # 用户收到重复系统消息、handler 被后写者覆盖、handling_count 双计。
        claimed = bool(write(
            "UPDATE tickets SET handler_uid = %s, handler_name = %s WHERE id = %s AND status = %s",
            (handler_uid, handler_name, ticket["id"], TICKET_STATUS_PENDING)))
        if claimed:
            ticket["handler_uid"] = handler_uid
            ticket["handler_name"] = handler_name
            # 线程处于 HITL 中断，恢复 Agent 会话（best effort，失败不阻断人工接管）
            try:
                msg = agent_resume(ticket["thread_id"], True, wait_time, ticket["ticket_no"],
                                   trace_id)
            except RuntimeError as exc:
                msg = "接管成功，但恢复Agent会话失败:" + str(exc)
    if not claimed:
        # 抢锁失败：不写状态/日志/消息/计数/广播。用 deny 而非 soft —— soft() 不置
        # request.state.denied，会经 audit_writes 中间件多落一条 audit_logs，
        # 与本仓「仅成功变更写审计」的约定和 stage33 的审计条数断言冲突。
        # 出口体仍是 soft 错误格式：{"code":409,"msg":...}，与同端点的 403 分支一致。
        deny(409, "该工单已被其他客服接管")
    adjust_handling(ticket["tenant_id"], handler_uid, 1)
    update_ticket_status(ticket, TICKET_STATUS_HANDLING)
    add_ticket_log(ticket, "takeover", TICKET_STATUS_PENDING, TICKET_STATUS_HANDLING,
                   int(ticket["priority"] or 0), int(ticket["priority"] or 0),
                   handler_name, "人工接管工单")
    notice = "人工客服已接管对话，工单号：" + ticket["ticket_no"]
    save_message(ticket["thread_id"], "system", notice, "text", ticket["id"])
    go_broadcast("user", {"type": "human_notice", "msg": notice}, key=ticket["thread_id"])
    go_broadcast("admins_tenant", {"type": "ticket_update", "ticket": ticket_json(ticket)},
                 tenant_id=tenant_of_thread(ticket["user_id"] or ""))
    return msg or "已接管"


@router.post("/api/admin/tickets/{id}/takeover")
async def admin_ticket_takeover(id: str, request: Request,
                                admin: Admin = Depends(require_perm("ticket:handle"))):
    ticket = load_ticket(id, admin.tenant_id)
    body = await body_json(request)
    wait_time = body.get("wait_time") if isinstance(body, dict) else None
    if isinstance(wait_time, bool) or not isinstance(wait_time, int):
        wait_time = 0                     # 忽略绑定错误，WaitTime 保持零值
    status = int(ticket["status"] or 0)
    if status == TICKET_STATUS_CLOSED:
        soft(400, "工单已关闭")
    if status == TICKET_STATUS_CANCELED:
        soft(400, "工单已取消")
    if status == TICKET_STATUS_HANDLING and not can_handle_ticket(admin, ticket):
        deny(403, "该工单由 " + handler_label(ticket["handler_name"]) + " 接管中，无权接管")
    try:
        msg = takeover_ticket(ticket, wait_time,
                              request.headers.get("X-Trace-Id", ""),
                              admin.user_id, current_handler_name(admin))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "msg": msg}


@router.post("/api/admin/tickets/{id}/reply")
async def admin_ticket_reply(id: str, request: Request,
                             admin: Admin = Depends(require_perm("ticket:handle"))):
    body = await body_json(request)
    content = body.get("content") if isinstance(body, dict) else None
    if not isinstance(content, str) or not content:
        soft(400, "content 不能为空")
    msg_type = body.get("msg_type") or ""
    ticket = load_ticket(id, admin.tenant_id)
    if int(ticket["status"] or 0) != TICKET_STATUS_HANDLING:
        soft(400, "该工单未处于人工接管中，无法回复")
    if not can_handle_ticket(admin, ticket):
        deny(403, "该工单由 " + handler_label(ticket["handler_name"]) + " 接管中，无权回复")
    msg_type = msg_type if isinstance(msg_type, str) else ""
    try:
        save_message(ticket["thread_id"], "human", content, msg_type, ticket["id"])
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    msg_type = msg_type or "text"
    go_broadcast("user", {"type": "human_reply", "msg": content, "msg_type": msg_type,
                          "ticket_no": ticket["ticket_no"]}, key=ticket["thread_id"])
    go_broadcast("admins_tenant", {"type": "human_reply", "thread_id": ticket["thread_id"],
                                   "msg": content, "msg_type": msg_type},
                 tenant_id=tenant_of_thread(ticket["user_id"] or ""))
    return {"code": 0, "msg": "ok"}


def close_ticket(ticket, operator):
    if int(ticket["status"] or 0) == TICKET_STATUS_CLOSED:
        return
    operator = operator or "system"
    # 已修复的历史缺陷：曾写死 old_status=Handling，关闭一个「待处理」工单时
    # 日志会显示 handling → closed，与实际状态流转不符。这里取关闭前的真实状态。
    old_status = int(ticket["status"] or 0)
    update_ticket_status(ticket, TICKET_STATUS_CLOSED)
    adjust_handling(ticket["tenant_id"], int(ticket["handler_uid"] or 0), -1)
    add_ticket_log(ticket, "close", old_status, TICKET_STATUS_CLOSED,
                   int(ticket["priority"] or 0), int(ticket["priority"] or 0),
                   operator, "关闭工单，转回智能客服")
    save_message(ticket["thread_id"], "system", "本次人工会话已结束，转回智能客服",
                 "text", ticket["id"])
    go_broadcast("user", {"type": "human_notice",
                          "msg": "本次人工会话已结束，智能客服继续为您服务"},
                 key=ticket["thread_id"])
    go_broadcast("admins_tenant", {"type": "ticket_update", "ticket": ticket_json(ticket)},
                 tenant_id=tenant_of_thread(ticket["user_id"] or ""))


@router.post("/api/admin/tickets/{id}/close")
def admin_ticket_close(id: str, admin: Admin = Depends(require_perm("ticket:handle"))):
    ticket = load_ticket(id, admin.tenant_id)
    if int(ticket["status"] or 0) == TICKET_STATUS_CLOSED:
        return {"code": 0, "msg": "工单已关闭"}
    if (int(ticket["status"] or 0) == TICKET_STATUS_HANDLING
            and not can_handle_ticket(admin, ticket)):
        deny(403, "该工单由 " + handler_label(ticket["handler_name"]) + " 接管中，无权关闭")
    try:
        close_ticket(ticket, current_handler_name(admin))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "msg": "已关闭"}


@router.post("/api/admin/tickets/{id}/priority")
async def admin_ticket_priority(id: str, request: Request,
                                admin: Admin = Depends(require_perm("ticket:handle"))):
    body = await body_json(request)
    priority = body.get("priority") if isinstance(body, dict) else None
    # 优先级必填：0 视为未提供。
    if isinstance(priority, bool) or not isinstance(priority, int) or priority == 0:
        soft(400, "参数错误：priority 必填")
    remark = body.get("remark") or ""
    ticket = load_ticket(id, admin.tenant_id)
    if priority < 0 or priority > 3:
        soft(400, "priority 取值范围：0-3")
    old_priority = int(ticket["priority"] or 0)
    try:
        write("UPDATE tickets SET priority = %s WHERE id = %s", (priority, ticket["id"]))
        ticket["priority"] = priority
        add_ticket_log(ticket, "priority", int(ticket["status"] or 0),
                       int(ticket["status"] or 0), old_priority, priority,
                       current_handler_name(admin), remark if isinstance(remark, str) else "")
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    go_broadcast("admins_tenant", {"type": "ticket_update", "ticket": ticket_json(ticket)},
                 tenant_id=ticket["tenant_id"])
    return {"code": 0, "msg": "优先级设置成功", "data": {"priority": priority}}


@router.post("/api/admin/tickets/{id}/transfer")
async def admin_ticket_transfer(id: str, request: Request,
                                admin: Admin = Depends(require_perm("ticket:handle"))):
    body = await body_json(request)
    to_user_id = body.get("to_user_id") if isinstance(body, dict) else None
    if isinstance(to_user_id, bool) or not isinstance(to_user_id, int) or to_user_id <= 0:
        soft(400, "to_user_id 不能为空")
    ticket = load_ticket(id, admin.tenant_id)
    status = int(ticket["status"] or 0)
    if status not in (TICKET_STATUS_HANDLING, TICKET_STATUS_PENDING):
        soft(400, "仅待处理或接管中的工单可转交")
    if not can_handle_ticket(admin, ticket):
        deny(403, "该工单由 " + handler_label(ticket["handler_name"]) + " 接管中，无权转交")
    target = one("SELECT id, tenant_id, nickname, username, status FROM users "
                 "WHERE id = %s AND deleted_at = 0 LIMIT 1", (to_user_id,))
    if not target:
        soft(400, "目标客服不存在")
    if target["tenant_id"] != ticket["tenant_id"]:
        soft(400, "目标客服不属于当前租户")
    if int(target["status"] or 0) != 1:
        soft(400, "目标客服已禁用，不能转交")
    to_name = target["nickname"] or target["username"] or ""
    operator = current_handler_name(admin)
    old_uid, old_status = int(ticket["handler_uid"] or 0), status
    new_status = TICKET_STATUS_HANDLING if status == TICKET_STATUS_PENDING else status
    try:
        ticket["handler_uid"], ticket["handler_name"] = to_user_id, to_name
        ticket["status"] = new_status
        write("UPDATE tickets SET handler_uid = %s, handler_name = %s, status = %s "
              "WHERE id = %s", (to_user_id, to_name, new_status, ticket["id"]))
        adjust_handling(ticket["tenant_id"], old_uid, -1)
        adjust_handling(ticket["tenant_id"], to_user_id, 1)
        add_ticket_log(ticket, "transfer", old_status, new_status,
                       int(ticket["priority"] or 0), int(ticket["priority"] or 0),
                       operator, "转交工单给客服 " + to_name)
        save_message(ticket["thread_id"], "system",
                     "工单已转交给客服 " + to_name + "，请继续为您服务", "text", ticket["id"])
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    # 除中间件自动审计外，还额外手写了一条带 detail 的审计，两条都要有
    try:
        write_audit(admin, request, "update", str(ticket["id"]),
                    "/api/admin/tickets",
                    "转交工单：" + ticket["ticket_no"] + " → " + to_name)
    except Exception:
        pass
    go_broadcast("admins_tenant", {"type": "ticket_update", "ticket": ticket_json(ticket)},
                 tenant_id=ticket["tenant_id"])
    return {"code": 0, "msg": "已转交"}


@router.post("/api/admin/ticket/approve")
async def admin_ticket_approve(request: Request,
                               admin: Admin = Depends(require_perm("ticket:handle"))):
    body = await body_json(request)
    ticket_no = body.get("ticket_no") if isinstance(body, dict) else None
    if not isinstance(ticket_no, str) or not ticket_no:
        soft(400, "参数错误")
    confirmed = body.get("confirmed", False)
    wait_time = body.get("wait_time", 0)
    # bool/int 强类型绑定：类型不符直接算绑定失败
    if (not isinstance(confirmed, bool) or isinstance(wait_time, bool)
            or not isinstance(wait_time, int)):
        soft(400, "参数错误")
    ticket = one(f"SELECT {TICKET_COLUMNS} FROM tickets WHERE ticket_no = %s "
                 f"AND deleted_at = 0 LIMIT 1", (ticket_no,))
    if not ticket:
        soft(404, "工单不存在")
    if ticket["tenant_id"] != admin.tenant_id:
        deny(403, "无权访问该租户工单")
    trace_id = request.headers.get("X-Trace-Id", "")
    if not confirmed:
        # 取消转接：先改状态，再唤醒 Agent（保持该次序 —— Agent 失败时状态已改）
        try:
            update_ticket_status(ticket, TICKET_STATUS_CANCELED)
            add_ticket_log(ticket, "cancel", TICKET_STATUS_PENDING, TICKET_STATUS_CANCELED,
                           int(ticket["priority"] or 0), int(ticket["priority"] or 0),
                           "system", "取消转接申请，智能客服继续服务")
            content = agent_resume(ticket["thread_id"], False, wait_time,
                                   ticket["ticket_no"], trace_id)
        except RuntimeError as exc:
            soft(500, str(exc))
        except pymysql.MySQLError as exc:
            soft(500, str(exc))
        save_message(ticket["thread_id"], "system", "转接申请已取消，智能客服继续服务",
                     "text", ticket["id"])
        go_broadcast("user", {"type": "human_notice",
                              "msg": "转接申请已取消，智能客服继续为您服务"},
                     key=ticket["thread_id"])
        go_broadcast("admins_tenant", {"type": "ticket_update",
                                       "ticket": ticket_json(ticket)},
                     tenant_id=tenant_of_thread(ticket["user_id"] or ""))
        return {"code": 0, "msg": content}
    try:
        msg = takeover_ticket(ticket, wait_time, trace_id, 0, "system")
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "msg": msg}


# ============================================================================
# 阶段 3-4：聚合运维域（看板 / 审计日志 / 用量 / 会话运维 / 归档 / 模型配置 / 提取规则）
