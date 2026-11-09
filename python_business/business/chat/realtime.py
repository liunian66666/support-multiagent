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
from business.admin.tickets import AGENT_URL, TICKET_COLUMNS, go_broadcast, save_message
from business.chat import recovery
from business.core.constants import CONVERSATION_RETENTION_DAYS, TENANT_ID
from business.core.errors import soft
from business.core.requests import body_json
from business.chat.routing import (APPROVAL_REASON, ERROR_APPROVAL_PENDING,
                                   HANDOFF_REASON, HUMAN_ANSWER, TICKET_ANSWER,
                                   WAITING_ANSWER, explicit_handoff)
from business.chat.turns import (TurnConflict, accept_turn, list_turns,
                                 owned_turn, retry_turn)
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


def waiting_ticket(customer):
    """队列里还没被接管的工单（``status=0``）；没有则 ``None``。

    返回整行而不是布尔：分流到"等待人工接入"时要回填工单号，并让这一轮消息挂到该工单上。
    """
    return one(
        f"SELECT {TICKET_COLUMNS} FROM tickets WHERE tenant_id = %s "
        "AND thread_id = %s AND status = 0 AND deleted_at = 0 "
        "ORDER BY id ASC LIMIT 1",
        (customer.tenant_id, customer.thread_id),
    )


def pending_ticket(customer):
    return waiting_ticket(customer) is not None


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
        turns = await asyncio.to_thread(list_turns, customer)
        await client.send({"type": "history", "messages": history["data"],
                           "turns": turns})
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
                await receive_chat_turn(customer, message, client)
            elif kind == "retry" and isinstance(message.get("turn_id"), str):
                await receive_retry(customer, message["turn_id"], client)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        if client:
            ws_hub.unregister_user(customer.thread_id, client)
        ws_hub.release_connection(ip, customer.thread_id)


def _turn_event(row):
    return {"type": "turn_update", "turn_id": row["turn_id"],
            "status": row["status"], "user_message_id": row["user_message_id"],
            "attempt_count": row["attempt_count"],
            "error": row.get("error_message") or ""}


async def receive_chat_turn(customer, message, client):
    """Accept and acknowledge a durable turn; processing belongs to maintenance."""
    content = message["msg"].strip()
    msg_type = message.get("msg_type") or "text"
    if msg_type not in ("text", "image", "video"):
        await client.send({"type": "error", "msg": "不支持的消息类型"})
        return
    if msg_type in ("image", "video") and not content.startswith(
        f"/msg_files/{customer.tenant_id}/"
    ):
        await client.send({"type": "error", "msg": "文件不属于当前店铺"})
        return
    request_id = message.get("request_id")
    try:
        if request_id:
            existing = await asyncio.to_thread(owned_turn, customer, request_id)
            if existing:
                if existing["user_message"] != content or existing["msg_type"] != msg_type:
                    raise TurnConflict("请求 ID 与当前会话不匹配")
                await client.send(_turn_event(existing))
                if existing["status"] == "completed":
                    history = await asyncio.to_thread(customer_messages, customer, 200, None)
                    turns = await asyncio.to_thread(list_turns, customer)
                    await client.send({"type": "history", "messages": history["data"],
                                       "turns": turns})
                return
        if not await asyncio.to_thread(handling_ticket, customer):
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
        row, _created = await asyncio.to_thread(
            accept_turn, customer, content, msg_type, request_id)
        await client.send(_turn_event(row))
    except (TurnConflict, ValueError, PermissionError) as exc:
        await client.send({"type": "error", "msg": str(exc)})
    except pymysql.MySQLError:
        realtime_log.exception("chat turn acceptance failed")
        await client.send({"type": "error", "msg": "消息暂未受理，请重试"})


async def receive_retry(customer, turn_id, client):
    try:
        row = await asyncio.to_thread(retry_turn, customer, turn_id)
        await client.send(_turn_event(row))
    except (LookupError, ValueError):
        await client.send({"type": "error", "msg": "轮次不存在或无权重试"})
    except pymysql.MySQLError:
        realtime_log.exception("chat turn retry failed")
        await client.send({"type": "error", "msg": "重试暂不可用，请稍后再试"})


@router.get("/api/customer/chat-turns")
def customer_chat_turns(customer: Customer = Depends(current_customer), limit: int = 200):
    return {"code": 0, "data": list_turns(customer, limit)}


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
    """Serialize ticket creation on the durable conversation row.

    归属核验走 ``chat_conversations`` 而不是 ``customer_sessions``：Worker 可能在客户令牌
    过期之后才处理一条已受理的轮次（``worker.py`` 的 ``_customer`` 是从 ``chat_turns``
    重建的，本来就没有令牌），按 2 小时令牌判定会让转人工直接失败。
    WebSocket 这条路径的令牌已经由 ``customer_for_token`` 校验过，这里只补会话归属。
    """
    with db() as conn:
        conn.begin()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT thread_id FROM chat_conversations WHERE tenant_id = %s "
                    "AND thread_id = %s AND status IN ('draft', 'active') "
                    "AND last_active_at >= DATE_SUB(NOW(), INTERVAL %s DAY) "
                    "LIMIT 1 FOR UPDATE",
                    (customer.tenant_id, customer.thread_id,
                     CONVERSATION_RETENTION_DAYS),
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


async def _raise_ticket(customer, reason, trace_id, client, *, pull_approval=False):
    """建/复用工单 → 通知客户 →（需要时）恢复旧审批。

    WebSocket 路径与 Worker 共用同一套恢复语义：``pull_approval`` 走
    :func:`business.chat.recovery.recover_for_ticket`，失败会记成工单待办并由后台重试，
    不会因为"没恢复成功"就把这条工单当成已处理完。
    """
    ticket, reused = await asyncio.to_thread(ensure_customer_ticket, customer, reason)
    if not reused:
        await asyncio.to_thread(save_message, customer.thread_id, "system",
                                "已提交人工客服，工单号：" + ticket["ticket_no"],
                                "text", ticket["id"])
        await asyncio.to_thread(assign_last_user_message, customer, ticket["id"])
        await ws_hub.send_admins(customer.tenant_id, {
            "type": "new_ticket", "ticket": {
                "id": ticket["id"], "ticket_no": ticket["ticket_no"],
                "thread_id": customer.thread_id,
                "user_id": customer.thread_id, "reason": reason,
                "status": ticket["status"],
                "created_at": iso(ticket["created_at"]),
            },
        })
    await client.send({"type": "ticket", "ticket_no": ticket["ticket_no"],
                       "reused": reused, "reason": reason, "trace_id": trace_id,
                       "msg": "已提交人工客服，请等待处理"})
    if pull_approval:
        result = await asyncio.to_thread(recovery.recover_for_ticket, ticket,
                                         trace_id=trace_id)
        if not result.settled:
            await ws_hub.send_admins(customer.tenant_id, {
                "type": "approval_recovery", "thread_id": customer.thread_id,
                "ticket_no": ticket["ticket_no"], "outcome": result.outcome.value,
                "error_code": result.error_code,
                "msg": "旧审批未恢复，已转入后台重试",
            })
    return ticket


async def _gate_reply(customer, response, trace_id, client) -> None:
    """处理 Agent 在建流前给出的 409/503（不是"回答失败"）。"""
    try:
        body = await response.aread()
        detail = (json.loads(body or b"{}") or {}).get("detail") or {}
    except (ValueError, TypeError):
        detail = {}
    code = str(detail.get("error_code") or "")
    if code == ERROR_APPROVAL_PENDING:
        reason = str((detail.get("pending_approval") or {}).get("reason")
                     or APPROVAL_REASON)
        await _raise_ticket(customer, reason, trace_id, client, pull_approval=True)
        return
    # 状态未知：可重试，不建单、不编答案
    await client.send({"type": "error", "trace_id": trace_id,
                       "msg": "会话状态确认中，请稍后重试"})


async def stream_agent_reply(customer, content, client):
    trace_id = secrets.token_hex(16)
    headers = {"X-Internal-Token": os.getenv("INTERNAL_API_TOKEN", ""),
               "X-Tenant-Id": customer.tenant_id, "X-Trace-Id": trace_id,
               "Accept": "text/event-stream"}
    payload = {"thread_id": customer.thread_id, "user_message": content}
    completed = False
    success = False
    async with httpx.AsyncClient(timeout=60) as transport:
        async with transport.stream("POST", AGENT_URL + "/agent/chat/stream",
                                    json=payload, headers=headers) as response:
            if response.status_code in (409, 503):
                # 闸门在**建立流之前**拒绝：409 = 线程上还有旧审批，503 = 状态未知。
                # 这两条以前会被 raise_for_status 变成"调用agent失败"，客户只会看到报错。
                await _gate_reply(customer, response, trace_id, client)
                return response.status_code == 409
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
                    answer_id = str(event.get("answer_id") or "")  # [ours] S1 轮次归因锚点
                    ticket = await asyncio.to_thread(handling_ticket, customer)
                    await asyncio.to_thread(save_message, customer.thread_id, "ai",
                                            answer, "text", ticket["id"] if ticket else None,
                                            answer_id)
                    await client.send({"type": "chat_end", "msg": answer,
                                       "trace_id": trace_id,
                                       "answer_id": answer_id})
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
                    reason = str(data.get("reason") or APPROVAL_REASON)
                    await _raise_ticket(customer, reason, trace_id, client,
                                        pull_approval=not data.get("direct_handoff"))
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
        started = time.monotonic()
        succeeded = False
        try:
            succeeded = await stream_agent_reply(customer, content, client)
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
    """Synchronous legacy customer API; identity comes from the signed session.

    分流与 :mod:`business.chat.worker` 是同一套（:mod:`business.chat.routing`）：人工接管 →
    媒体 → 点名转人工 → 工单排队 → 四 Agent。这条老接口以前把 ``has_pending_ticket`` 交给
    Agent 去决定走哪张图；收敛后 Agent 只有一个入口，所以判断必须在这里做完。
    """
    save_message(customer.thread_id, "user", content, "text")
    ticket = handling_ticket(customer)
    if ticket:
        assign_last_user_message(customer, ticket["id"])
        go_broadcast("admins_tenant", {
            "type": "user_msg", "thread_id": customer.thread_id,
            "user_id": customer.thread_id, "msg": content,
        }, tenant_id=customer.tenant_id)
        return {"code": 0, "msg": HUMAN_ANSWER, "is_human": True}
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

    if explicit_handoff(content):
        ticket, reused = ensure_customer_ticket(customer, HANDOFF_REASON)
        _open_ticket(customer, ticket, reason=HANDOFF_REASON, reused=reused,
                     trace_id=trace_id, resume_old_approval=True)
        answer = TICKET_ANSWER.format(ticket_no=ticket["ticket_no"])
        save_message(customer.thread_id, "system", answer, "text", ticket["id"])
        assign_last_user_message(customer, ticket["id"])
        return {"code": 0, "msg": answer, "is_human_wait": True,
                "ticket_no": ticket["ticket_no"], "reason": HANDOFF_REASON}

    queued = waiting_ticket(customer)
    if queued:
        # 排队期间不再问 Agent：客户已经被告知"已提交"，这里只重申状态。
        answer = WAITING_ANSWER.format(ticket_no=queued["ticket_no"])
        save_message(customer.thread_id, "system", answer, "text", queued["id"])
        assign_last_user_message(customer, queued["id"])
        return {"code": 0, "msg": answer, "is_human_wait": True,
                "ticket_no": queued["ticket_no"], "reason": queued.get("reason") or ""}

    started = time.monotonic()
    succeeded = False
    try:
        response = httpx.post(
            AGENT_URL + "/agent/chat",
            json={"thread_id": customer.thread_id, "user_message": content},
            headers={"X-Internal-Token": os.getenv("INTERNAL_API_TOKEN", ""),
                     "X-Tenant-Id": customer.tenant_id, "X-Trace-Id": trace_id},
            timeout=60,
        )
        if response.status_code in (409, 503):
            detail = (response.json() or {}).get("detail") or {}
            code = detail.get("error_code")
            if code == ERROR_APPROVAL_PENDING:
                reason = str((detail.get("pending_approval") or {}).get("reason")
                             or APPROVAL_REASON)
                ticket, reused = ensure_customer_ticket(customer, reason)
                _open_ticket(customer, ticket, reason=reason, reused=reused,
                             trace_id=trace_id, resume_old_approval=True)
                answer = TICKET_ANSWER.format(ticket_no=ticket["ticket_no"])
                save_message(customer.thread_id, "system", answer, "text", ticket["id"])
                assign_last_user_message(customer, ticket["id"])
                return {"code": 0, "msg": answer, "is_human_wait": True,
                        "ticket_no": ticket["ticket_no"], "reason": reason,
                        "trace_id": trace_id}
            # 审批状态未知：可重试的基础设施错误，不建单、不编答案。
            soft(503, "会话状态确认中，请稍后重试")
        response.raise_for_status()
        data = response.json()
        succeeded = bool(data.get("success"))
    finally:
        metrics.observe_agent("/agent/chat", succeeded,
                              time.monotonic() - started)
    if not data.get("success"):
        soft(503, "智能客服暂时不可用，请稍后再试")
    answer = data.get("content") or ""
    ticket = handling_ticket(customer)
    save_message(customer.thread_id, "ai", answer, "text",
                 ticket["id"] if ticket else None,
                 str(data.get("answer_id") or ""))  # [ours] S1 轮次归因锚点
    return {"code": 0, "msg": answer,
            "trace_id": data.get("trace_id") or trace_id,
            "answer_id": str(data.get("answer_id") or "")}


def _open_ticket(customer, ticket, *, reason, reused, trace_id,
                 resume_old_approval=False):
    """新工单 → 广播给管理员 →（需要时）恢复仍停着的旧审批。

    只在**新建**时广播：复用已有工单不应该再弹一次"新工单"。恢复失败不是"处理完了"：
    结果写回工单待办（``approval_resume_state``），由后台线程继续，并把这件事推给管理员。
    """
    if not reused:
        go_broadcast("admins_tenant", {"type": "new_ticket", "ticket": {
            "id": ticket["id"], "ticket_no": ticket["ticket_no"],
            "thread_id": customer.thread_id, "user_id": customer.thread_id,
            "reason": reason, "status": ticket["status"],
            "created_at": iso(ticket["created_at"]),
        }}, tenant_id=customer.tenant_id)
    if resume_old_approval:
        result = recovery.recover_for_ticket(ticket, trace_id=trace_id)
        if not result.settled:
            realtime_log.error("approval resume pending thread=%s outcome=%s",
                               customer.thread_id, result.outcome.value)
            go_broadcast("admins_tenant", {
                "type": "approval_recovery", "thread_id": customer.thread_id,
                "ticket_no": ticket["ticket_no"], "outcome": result.outcome.value,
                "error_code": result.error_code,
                "msg": "旧审批未恢复，已转入后台重试",
            }, tenant_id=customer.tenant_id)


@router.post("/api/user/chat")
async def customer_http_chat(request: Request,
                             customer: Customer = Depends(current_customer)):
    """**兼容保留**的同步聊天接口（前端实际走 `/ws/chat`）。

    它与 WebSocket 路径共用同一套分流与恢复语义（``_open_ticket`` →
    :mod:`business.chat.recovery`），但**没有**持久轮次与租约：重复请求会重复落消息。
    新客户端一律用 WebSocket；这个端点只为老调用方保留，不再新增能力。
    """
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
