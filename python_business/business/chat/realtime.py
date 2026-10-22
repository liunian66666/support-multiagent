"""Customer and admin WebSockets, HTTP chat and human handoff."""

import asyncio
import json
import os
import secrets
import time

import httpx
import pymysql
from fastapi import (APIRouter, Depends, HTTPException, Request,
                     WebSocket, WebSocketDisconnect)

from business.admin.auth import admin_for_token
from business.admin.platform import utc_day_start_local
from business.admin.tickets import (AGENT_URL, TICKET_COLUMNS, agent_resume,
                                    go_broadcast, save_message)
from business.core.constants import TENANT_ID
from business.core.errors import soft
from business.core.requests import body_json
from business.customer.routes import (Customer, current_customer,
                                      customer_for_token, customer_messages,
                                      internal_guard, iso)
from business.infra.runtime import metrics, realtime_log, ws_hub
from business.infra.storage import db, one, write

router = APIRouter()

def handling_ticket(customer):
    return one(
        f"SELECT {TICKET_COLUMNS} FROM tickets WHERE tenant_id = %s "
        "AND thread_id = %s AND status = 1 AND deleted_at = 0 "
        "ORDER BY id DESC LIMIT 1",
        (customer.tenant_id, customer.thread_id),
    )


def pending_ticket(customer):
    return one(
        "SELECT id FROM tickets WHERE tenant_id = %s AND thread_id = %s "
        "AND status = 0 AND deleted_at = 0 LIMIT 1",
        (customer.tenant_id, customer.thread_id),
    ) is not None


def assign_last_user_message(customer, ticket_id):
    row = one("SELECT id FROM messages WHERE tenant_id = %s AND thread_id = %s "
              "AND role = 'user' AND ticket_id IS NULL ORDER BY id DESC LIMIT 1",
              (customer.tenant_id, customer.thread_id))
    if row:
        write("UPDATE messages SET ticket_id = %s WHERE id = %s AND tenant_id = %s",
              (ticket_id, row["id"], customer.tenant_id))


@router.websocket("/ws/admin")
async def admin_websocket(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    try:
        admin = await asyncio.to_thread(admin_for_token, token)
    except HTTPException:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    client = ws_hub.register_admin(websocket, admin.tenant_id)
    try:
        if admin.user_id > 0:
            await asyncio.to_thread(
                write,
                "INSERT INTO agent_status (tenant_id, user_id, status, handling_count, "
                "max_concurrent, updated_at) VALUES (%s, %s, 1, 0, 3, NOW()) "
                "ON DUPLICATE KEY UPDATE status = 1, updated_at = NOW()",
                (admin.tenant_id, admin.user_id),
            )
        while True:
            await asyncio.wait_for(websocket.receive_text(), timeout=60)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        ws_hub.unregister_admin(client)
        if admin.user_id > 0:
            try:
                await asyncio.to_thread(
                    write,
                    "UPDATE agent_status SET status = 0, updated_at = NOW() "
                    "WHERE tenant_id = %s AND user_id = %s",
                    (admin.tenant_id, admin.user_id),
                )
            except pymysql.MySQLError:
                realtime_log.error("admin presence update failed")


@router.websocket("/ws/chat")
async def customer_websocket(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    try:
        customer = await asyncio.to_thread(customer_for_token, token)
    except HTTPException:
        await websocket.close(code=1008)
        return
    ip = (websocket.headers.get("X-Real-IP") or
          (websocket.client.host if websocket.client else "unknown"))
    if not ws_hub.acquire_connection(ip, customer.thread_id):
        await websocket.close(code=1013)
        return
    tasks = set()
    client = None
    try:
        quota = await asyncio.to_thread(
            one, "SELECT max_concurrent_sessions FROM tenants "
                 "WHERE tenant_id = %s AND status = 1 AND deleted_at = 0 LIMIT 1",
            (customer.tenant_id,),
        )
        if not quota:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        limit = int(quota["max_concurrent_sessions"] or 0)
        if limit > 0 and ws_hub.count_tenant(
            customer.tenant_id, excluding=customer.thread_id
        ) >= limit:
            await websocket.send_json({"type": "error", "code": "concurrency_full",
                "msg": f"当前在线会话数已达上限（{limit}），请稍后再试"})
            await websocket.close()
            return
        client = await ws_hub.register_user(
            websocket, customer.tenant_id, customer.thread_id)
        history = await asyncio.to_thread(customer_messages, customer, 200, None)
        await client.send({"type": "history", "messages": history["data"]})
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=90)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            if kind == "ping":
                await client.send({"type": "pong"})
            elif kind == "read":
                await asyncio.to_thread(
                    write, "UPDATE messages SET is_read = 1 WHERE tenant_id = %s "
                           "AND thread_id = %s AND role != 'user' AND is_read = 0",
                    (customer.tenant_id, customer.thread_id),
                )
                await ws_hub.send_admins(customer.tenant_id, {
                    "type": "read_receipt", "thread_id": customer.thread_id,
                    "timestamp": int(time.time()),
                })
            elif kind == "chat" and isinstance(message.get("msg"), str) and message["msg"]:
                if not ws_hub.allow_message(customer.thread_id):
                    await client.send({"type": "rate_limit",
                                       "msg": "消息发送过于频繁（每秒最多 5 条）"})
                    continue
                task = asyncio.create_task(process_ws_chat(customer, message, client))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if client:
            ws_hub.unregister_user(customer.thread_id, client)
        ws_hub.release_connection(ip, customer.thread_id)


@router.post("/api/agent/ws/broadcast")
async def agent_ws_broadcast(request: Request):
    internal_guard(request)
    body = await body_json(request)
    if not isinstance(body, dict) or not isinstance(body.get("payload"), dict):
        soft(400, "参数错误")
    target = body.get("target")
    key = body.get("key") or ""
    tenant_id = body.get("tenant_id") or ""
    if target not in ("user", "admins", "admins_tenant") or target == "user" and not key:
        soft(400, "target/key 参数错误")
    if target == "admins_tenant" and not TENANT_ID.fullmatch(tenant_id):
        soft(400, "tenant_id 参数错误")
    await ws_hub.broadcast(target, body["payload"], key=key,
                           tenant_id=tenant_id)
    return {"code": 0, "msg": "ok"}


@router.get("/api/agent/ws/conns")
def agent_ws_conns(request: Request):
    internal_guard(request)
    return {"code": 0, "data": ws_hub.count_all_tenants()}


@router.get("/api/agent/tenant-concurrency")
def agent_tenant_concurrency(request: Request, tenant_id: str = ""):
    internal_guard(request)
    if not TENANT_ID.fullmatch(tenant_id):
        soft(400, "缺少或无效的 tenant_id")
    return {"code": 0, "data": {"tenant_id": tenant_id,
            "concurrent_sessions": ws_hub.count_tenant(tenant_id)}}


def ensure_customer_ticket(customer, reason):
    """Serialize ticket creation on the durable customer-session row."""
    with db() as conn:
        conn.begin()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT thread_id FROM customer_sessions WHERE tenant_id = %s "
                    "AND thread_id = %s AND expires_at > NOW() LIMIT 1 FOR UPDATE",
                    (customer.tenant_id, customer.thread_id),
                )
                if not cur.fetchone():
                    raise ValueError("客户会话无效或已过期")
                cur.execute(
                    f"SELECT {TICKET_COLUMNS} FROM tickets WHERE tenant_id = %s "
                    "AND thread_id = %s AND status IN (0, 1) AND deleted_at = 0 "
                    "ORDER BY id ASC LIMIT 1 FOR UPDATE",
                    (customer.tenant_id, customer.thread_id),
                )
                ticket = cur.fetchone()
                if ticket:
                    cur.execute("UPDATE tickets SET reason = %s WHERE id = %s",
                                (reason, ticket["id"]))
                    ticket["reason"] = reason
                    conn.commit()
                    return ticket, True
                ticket_no = "TK-" + secrets.token_hex(4)
                cur.execute(
                    "INSERT INTO tickets (thread_id, ticket_no, user_id, reason, "
                    "status, tenant_id, priority, handler_uid, created_at, deleted_at) "
                    "VALUES (%s, %s, %s, %s, 0, %s, 1, 0, NOW(3), 0)",
                    (customer.thread_id, ticket_no, customer.thread_id,
                     reason, customer.tenant_id),
                )
                ticket_id = cur.lastrowid
                cur.execute(
                    "INSERT INTO ticket_logs (ticket_id, thread_id, tenant_id, "
                    "action, old_status, new_status, old_priority, new_priority, "
                    "operator, remark, created_at) "
                    "VALUES (%s, %s, %s, 'create', -1, 0, 0, 1, 'system', "
                    "'用户发起转人工，创建工单', NOW(3))",
                    (ticket_id, customer.thread_id, customer.tenant_id),
                )
                cur.execute(
                    "SELECT a.id AS status_id, a.user_id, a.handling_count, "
                    "a.max_concurrent, COALESCE(NULLIF(u.nickname, ''), "
                    "u.username) AS nickname FROM agent_status a JOIN users u "
                    "ON u.id = a.user_id AND u.deleted_at = 0 AND u.status = 1 "
                    "WHERE a.tenant_id = %s AND a.status = 1 "
                    "AND a.handling_count < a.max_concurrent "
                    "ORDER BY a.handling_count ASC, a.id ASC LIMIT 1 FOR UPDATE",
                    (customer.tenant_id,),
                )
                agent = cur.fetchone()
                if agent:
                    cur.execute(
                        "UPDATE tickets SET handler_uid = %s, handler_name = %s, "
                        "status = 1 WHERE id = %s",
                        (agent["user_id"], agent["nickname"], ticket_id),
                    )
                    count = int(agent["handling_count"] or 0) + 1
                    new_status = (2 if count >= int(agent["max_concurrent"] or 3)
                                  else 1)
                    cur.execute(
                        "UPDATE agent_status SET handling_count = %s, status = %s, "
                        "updated_at = NOW() WHERE id = %s",
                        (count, new_status, agent["status_id"]),
                    )
                    cur.execute(
                        "INSERT INTO ticket_logs (ticket_id, thread_id, tenant_id, "
                        "action, old_status, new_status, old_priority, new_priority, "
                        "operator, remark, created_at) "
                        "VALUES (%s, %s, %s, 'assign', 0, 1, 0, 1, %s, "
                        "'系统自动分配（处理量最少优先）', NOW(3))",
                        (ticket_id, customer.thread_id, customer.tenant_id,
                         agent["nickname"]),
                    )
                cur.execute(f"SELECT {TICKET_COLUMNS} FROM tickets WHERE id = %s",
                            (ticket_id,))
                ticket = cur.fetchone()
            conn.commit()
            return ticket, False
        except Exception:
            conn.rollback()
            raise


async def deliver_to_human(customer, content, msg_type, client):
    ticket = await asyncio.to_thread(handling_ticket, customer)
    if not ticket:
        return False
    await asyncio.to_thread(assign_last_user_message, customer, ticket["id"])
    await ws_hub.send_admins(customer.tenant_id, {
        "type": "user_msg", "thread_id": customer.thread_id,
        "user_id": customer.thread_id, "msg": content,
        "msg_type": msg_type,
    })
    await client.send({"type": "human_notice",
                       "msg": ("您当前由人工客服接待，文件已转给人工客服" if
                               msg_type in ("image", "video") else
                               "您当前由人工客服接待，消息已转给人工客服，请稍候")})
    return True


async def stream_agent_reply(customer, content, pending, client):
    trace_id = secrets.token_hex(16)
    headers = {"X-Internal-Token": os.getenv("INTERNAL_API_TOKEN", ""),
               "X-Tenant-Id": customer.tenant_id, "X-Trace-Id": trace_id,
               "Accept": "text/event-stream"}
    payload = {"thread_id": customer.thread_id, "user_message": content,
               "has_pending_ticket": pending}
    completed = False
    success = False
    async with httpx.AsyncClient(timeout=60) as transport:
        async with transport.stream("POST", AGENT_URL + "/agent/chat/stream",
                                    json=payload, headers=headers) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                if kind == "chunk":
                    await client.send({"type": "chat_chunk",
                                       "chunk": event.get("content") or ""})
                elif kind == "end":
                    answer = event.get("content") or ""
                    ticket = await asyncio.to_thread(handling_ticket, customer)
                    await asyncio.to_thread(save_message, customer.thread_id, "ai",
                                            answer, "text", ticket["id"] if ticket else None)
                    await client.send({"type": "chat_end", "msg": answer,
                                       "trace_id": trace_id})
                    completed = True
                    success = True
                    break
                elif kind == "error":
                    await client.send({"type": "error", "trace_id": trace_id,
                                       "msg": "调用agent失败:" + str(event.get("msg") or "未知错误")})
                    completed = True
                    break
                elif kind == "interrupt":
                    data = event.get("data") or {}
                    reason = str(data.get("reason") or "")
                    ticket, reused = await asyncio.to_thread(
                        ensure_customer_ticket, customer, reason)
                    if not reused:
                        await asyncio.to_thread(
                            save_message, customer.thread_id, "system",
                            "已提交人工客服，工单号：" + ticket["ticket_no"],
                            "text", ticket["id"],
                        )
                        await asyncio.to_thread(assign_last_user_message,
                                                customer, ticket["id"])
                        await ws_hub.send_admins(customer.tenant_id, {
                            "type": "new_ticket", "ticket": {
                                "id": ticket["id"], "ticket_no": ticket["ticket_no"],
                                "thread_id": customer.thread_id,
                                "user_id": customer.thread_id, "reason": reason,
                                "status": ticket["status"],
                                "created_at": iso(ticket["created_at"]),
                            },
                        })
                    await client.send({"type": "ticket",
                                       "ticket_no": ticket["ticket_no"],
                                       "reused": reused, "reason": reason,
                                       "trace_id": trace_id,
                                       "msg": "已提交人工客服，请等待处理"})
                    if not data.get("direct_handoff"):
                        try:
                            await asyncio.to_thread(agent_resume, customer.thread_id,
                                                    True, 3, ticket["ticket_no"], trace_id)
                        except RuntimeError:
                            pass
                    completed = True
                    success = True
                    break
    if not completed:
        await client.send({"type": "error", "msg": "agent流式连接异常: 响应提前结束"})
    return success


async def process_ws_chat(customer, message, client):
    content = message["msg"]
    msg_type = message.get("msg_type") or "text"
    if msg_type not in ("text", "image", "video"):
        await client.send({"type": "error", "msg": "不支持的消息类型"})
        return
    if msg_type in ("image", "video") and not content.startswith(
        f"/msg_files/{customer.tenant_id}/"
    ):
        await client.send({"type": "error", "msg": "文件不属于当前店铺"})
        return
    if not ws_hub.try_lock_thread(customer.thread_id):
        await client.send({"type": "notice",
                           "msg": "上一条消息还在处理中，请稍候再发送"})
        return
    try:
        await asyncio.to_thread(save_message, customer.thread_id, "user",
                                content, msg_type)
        if await deliver_to_human(customer, content, msg_type, client):
            return
        if msg_type in ("image", "video"):
            await client.send({"type": "notice",
                               "msg": "图片/视频已收到，当前 AI 暂不支持查看内容，如需发送请转人工客服"})
            return
        quota = await asyncio.to_thread(
            one, "SELECT daily_message_limit FROM tenants WHERE tenant_id = %s "
                 "AND status = 1 AND deleted_at = 0 LIMIT 1",
            (customer.tenant_id,),
        )
        if not quota:
            await client.send({"type": "error", "msg": "店铺暂时不可用"})
            return
        limit = int(quota["daily_message_limit"] or 0)
        if limit > 0:
            count = await asyncio.to_thread(
                one, "SELECT COUNT(*) AS n FROM messages WHERE tenant_id = %s "
                     "AND created_at >= %s",
                (customer.tenant_id, utc_day_start_local()),
            )
            if int(count["n"] or 0) >= limit:
                await client.send({"type": "quota",
                                   "msg": "今日咨询量已达上限，请明天再试或联系人工客服"})
                return
        pending = await asyncio.to_thread(pending_ticket, customer)
        started = time.monotonic()
        succeeded = False
        try:
            succeeded = await stream_agent_reply(customer, content, pending, client)
        finally:
            metrics.observe_agent("/agent/chat/stream", succeeded,
                                  time.monotonic() - started)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        realtime_log.error("chat failed: %s", type(exc).__name__)
        await client.send({"type": "error", "msg": "客服服务暂时不可用，请稍后再试"})
    finally:
        ws_hub.unlock_thread(customer.thread_id)


def process_http_chat(customer, content, trace_id):
    """Synchronous legacy customer API; identity comes from the signed session."""
    save_message(customer.thread_id, "user", content, "text")
    ticket = handling_ticket(customer)
    if ticket:
        assign_last_user_message(customer, ticket["id"])
        go_broadcast("admins_tenant", {
            "type": "user_msg", "thread_id": customer.thread_id,
            "user_id": customer.thread_id, "msg": content,
        }, tenant_id=customer.tenant_id)
        return {"code": 0, "msg": "您当前由人工客服接待，消息已转给人工客服，请稍候",
                "is_human": True}
    quota = one("SELECT daily_message_limit FROM tenants WHERE tenant_id = %s "
                "AND status = 1 AND deleted_at = 0 LIMIT 1",
                (customer.tenant_id,))
    if not quota:
        soft(503, "店铺暂时不可用")
    limit = int(quota["daily_message_limit"] or 0)
    if limit > 0:
        count = one("SELECT COUNT(*) AS n FROM messages WHERE tenant_id = %s "
                    "AND created_at >= %s",
                    (customer.tenant_id, utc_day_start_local()))
        if int(count["n"] or 0) >= limit:
            soft(429, "今日咨询量已达上限，请明天再试或联系人工客服")
    pending = pending_ticket(customer)
    started = time.monotonic()
    succeeded = False
    try:
        response = httpx.post(
            AGENT_URL + "/agent/chat",
            json={"thread_id": customer.thread_id, "user_message": content,
                  "has_pending_ticket": pending},
            headers={"X-Internal-Token": os.getenv("INTERNAL_API_TOKEN", ""),
                     "X-Tenant-Id": customer.tenant_id, "X-Trace-Id": trace_id},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        succeeded = bool(data.get("success"))
    finally:
        metrics.observe_agent("/agent/chat", succeeded,
                              time.monotonic() - started)
    if not data.get("success"):
        soft(503, "智能客服暂时不可用，请稍后再试")
    if data.get("is_interrupt"):
        interrupt = data.get("interrupt_data") or {}
        reason = str(interrupt.get("reason") or "")
        ticket, reused = ensure_customer_ticket(customer, reason)
        if not reused:
            save_message(customer.thread_id, "system",
                         "已提交人工客服，工单号：" + ticket["ticket_no"],
                         "text", ticket["id"])
            assign_last_user_message(customer, ticket["id"])
            go_broadcast("admins_tenant", {"type": "new_ticket", "ticket": {
                "id": ticket["id"], "ticket_no": ticket["ticket_no"],
                "thread_id": customer.thread_id, "user_id": customer.thread_id,
                "reason": reason, "status": ticket["status"],
                "created_at": iso(ticket["created_at"]),
            }}, tenant_id=customer.tenant_id)
            if not interrupt.get("direct_handoff"):
                try:
                    agent_resume(customer.thread_id, True, 3, ticket["ticket_no"],
                                 trace_id)
                except RuntimeError:
                    pass
        return {"code": 0, "msg": "已提交人工客服，请等待处理",
                "is_human_wait": True, "ticket_no": ticket["ticket_no"],
                "reason": reason, "trace_id": data.get("trace_id") or trace_id}
    answer = data.get("content") or ""
    ticket = handling_ticket(customer)
    save_message(customer.thread_id, "ai", answer, "text",
                 ticket["id"] if ticket else None)
    return {"code": 0, "msg": answer,
            "trace_id": data.get("trace_id") or trace_id}


@router.post("/api/user/chat")
async def customer_http_chat(request: Request,
                             customer: Customer = Depends(current_customer)):
    body = await body_json(request)
    content = body.get("msg") if isinstance(body, dict) else None
    if not isinstance(content, str) or not content.strip():
        soft(400, "msg 不能为空")
    if not ws_hub.try_lock_thread(customer.thread_id):
        return {"code": 0, "msg": "上一条消息还在处理中，请稍候再发送"}
    try:
        trace_id = request.headers.get("X-Trace-Id") or secrets.token_hex(16)
        return await asyncio.to_thread(process_http_chat, customer, content, trace_id)
    except (pymysql.MySQLError, httpx.HTTPError, ValueError):
        soft(503, "客服服务暂时不可用，请稍后再试")
    finally:
        ws_hub.unlock_thread(customer.thread_id)
