"""Durable, tenant-scoped customer chat turns backed by MySQL.

The accepted user message and turn are committed together. A lease token fences
workers after a crash; the graph receives the same turn ID on every attempt.
"""

from __future__ import annotations

import logging
import os
import re
import uuid

import pymysql

from business.infra.storage import db, many, one, write

logger = logging.getLogger("business.chat.turns")
TURN_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_ATTEMPTS = 3


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(os.getenv(name, str(default))), high))
    except (TypeError, ValueError):
        return default


# Agent HTTP 超时 65s；租约默认给到它的近两倍，再由心跳续期，回收只针对真僵尸。
LEASE_SECONDS = _env_int("CHAT_TURN_LEASE_SECONDS", 120, 30, 3600)
# 续租间隔必须明显小于租约，否则一次 GC/慢查询就会把自己判死。
HEARTBEAT_SECONDS = _env_int("CHAT_TURN_HEARTBEAT_SECONDS", 30, 0, LEASE_SECONDS // 2 or 1)


class TurnConflict(ValueError):
    """A request ID already belongs to another message or customer."""


def canonical_turn_id(value: str | None) -> str:
    if not value:
        return str(uuid.uuid4())  # Compatibility for older clients.
    try:
        normalized = str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("无效的请求 ID") from exc
    if not TURN_ID.fullmatch(normalized):
        raise ValueError("无效的请求 ID")
    return normalized


def _owned(row, customer, content: str | None = None, msg_type: str | None = None):
    if (row["tenant_id"] != customer.tenant_id or
        row["customer_id"] != customer.customer_id or
        row["thread_id"] != customer.thread_id or
        (content is not None and row["user_message"] != content) or
        (msg_type is not None and row["msg_type"] != msg_type)):
        raise TurnConflict("请求 ID 与当前会话不匹配")
    return row


def accept_turn(customer, content: str, msg_type: str, request_id: str | None):
    """Return (turn, created). Both rows commit or neither row exists."""
    turn_id = canonical_turn_id(request_id)
    if not content or len(content) > 4000:
        raise ValueError("消息长度不合法")
    with db() as conn:
        conn.begin()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM chat_turns WHERE turn_id = %s FOR UPDATE",
                            (turn_id,))
                existing = cur.fetchone()
                if existing:
                    _owned(existing, customer, content, msg_type)
                    conn.commit()
                    return existing, False
                cur.execute(
                    "SELECT token FROM customer_sessions WHERE token = %s "
                    "AND tenant_id = %s AND customer_id = %s AND thread_id = %s "
                    "AND expires_at > NOW() LIMIT 1 FOR UPDATE",
                    (customer.session_id, customer.tenant_id,
                     customer.customer_id, customer.thread_id),
                )
                if not cur.fetchone():
                    raise PermissionError("客户会话无效或已过期")
                cur.execute(
                    "INSERT INTO messages (thread_id, role, content, msg_type, tenant_id, "
                    "ticket_id, is_read, answer_id, created_at) "
                    "VALUES (%s, 'user', %s, %s, %s, NULL, 1, '', NOW(3))",
                    (customer.thread_id, content, msg_type, customer.tenant_id),
                )
                user_message_id = cur.lastrowid
                # 会话索引：受理成功才把 draft 转 active，并刷新活跃时间。
                # 这一句是"最后一次成功受理客户消息"的唯一写入点，7 天清理按它判定。
                cur.execute(
                    "INSERT INTO chat_conversations "
                    "(thread_id, tenant_id, customer_id, is_guest, is_preview, status, "
                    "created_at, last_active_at) "
                    "VALUES (%s, %s, %s, %s, %s, 'active', NOW(3), NOW(3)) "
                    "ON DUPLICATE KEY UPDATE status = 'active', last_active_at = NOW(3)",
                    (customer.thread_id, customer.tenant_id, customer.customer_id,
                     int(customer.is_guest), int(customer.is_preview)),
                )
                cur.execute(
                    "INSERT INTO chat_turns (turn_id, tenant_id, customer_id, thread_id, "
                    "user_message_id, user_message, msg_type, status, trace_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued', %s)",
                    (turn_id, customer.tenant_id, customer.customer_id,
                     customer.thread_id, user_message_id, content, msg_type, turn_id),
                )
                cur.execute("SELECT * FROM chat_turns WHERE id = %s", (cur.lastrowid,))
                row = cur.fetchone()
            conn.commit()
            return row, True
        except pymysql.IntegrityError:
            conn.rollback()
            existing = one("SELECT * FROM chat_turns WHERE turn_id = %s", (turn_id,))
            if existing:
                return _owned(existing, customer, content, msg_type), False
            raise
        except Exception:
            conn.rollback()
            raise


def owned_turn(customer, turn_id: str):
    return one(
        "SELECT * FROM chat_turns WHERE turn_id = %s AND tenant_id = %s "
        "AND customer_id = %s AND thread_id = %s",
        (canonical_turn_id(turn_id), customer.tenant_id,
         customer.customer_id, customer.thread_id),
    )


def retry_turn(customer, turn_id: str):
    with db() as conn:
        conn.begin()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM chat_turns WHERE turn_id = %s AND tenant_id = %s "
                    "AND customer_id = %s AND thread_id = %s FOR UPDATE",
                    (canonical_turn_id(turn_id), customer.tenant_id,
                     customer.customer_id, customer.thread_id),
                )
                row = cur.fetchone()
                if not row:
                    raise LookupError("轮次不存在")
                if row["status"] == "failed":
                    cur.execute(
                        "UPDATE chat_turns SET status = 'queued', attempt_count = 0, "
                        "next_attempt_at = NOW(3), lease_until = NULL, lease_token = NULL, "
                        "error_code = '', error_message = '' WHERE id = %s",
                        (row["id"],),
                    )
                    row["status"] = "queued"
                    row["attempt_count"] = 0
            conn.commit()
            return row
        except Exception:
            conn.rollback()
            raise


def list_turns(customer, limit: int = 200):
    limit = max(1, min(int(limit), 200))
    rows = list(many(
        "SELECT turn_id, user_message_id, answer_message_id, status, result_type, "
        "attempt_count, error_code, error_message, ticket_no, created_at, updated_at "
        "FROM chat_turns WHERE tenant_id = %s AND customer_id = %s "
        "AND thread_id = %s ORDER BY id DESC LIMIT %s",
        (customer.tenant_id, customer.customer_id, customer.thread_id, limit),
    ))
    rows.reverse()
    for row in rows:
        for key in ("created_at", "updated_at"):
            if row[key] is not None:
                row[key] = row[key].isoformat()
    return rows


def fail_expired_leases():
    """Turn exhausted worker crashes into an explicit terminal failure."""
    failed = []
    with db() as conn:
        conn.begin()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, turn_id, tenant_id, thread_id FROM chat_turns "
                    "WHERE status = 'running' AND lease_until < NOW(3) "
                    "AND attempt_count >= %s ORDER BY id LIMIT 20 FOR UPDATE SKIP LOCKED",
                    (MAX_ATTEMPTS,),
                )
                failed = list(cur.fetchall())
                for row in failed:
                    cur.execute(
                        "UPDATE chat_turns SET status = 'failed', lease_token = NULL, "
                        "lease_until = NULL, error_code = 'worker_restart', "
                        "error_message = '处理多次中断，请点击重试' WHERE id = %s",
                        (row["id"],),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return failed


def claim_next():
    """Claim the earliest ready turn in a session; older work blocks later turns."""
    with db() as conn:
        conn.begin()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT t.* FROM chat_turns t WHERE t.attempt_count < %s AND "
                    "((t.status = 'queued' AND t.next_attempt_at <= NOW(3)) OR "
                    "(t.status = 'running' AND t.lease_until < NOW(3))) "
                    "AND NOT EXISTS (SELECT 1 FROM chat_turns older WHERE "
                    "older.tenant_id = t.tenant_id AND older.thread_id = t.thread_id "
                    "AND older.id < t.id AND older.status IN ('queued', 'running')) "
                    "ORDER BY t.id LIMIT 1 FOR UPDATE SKIP LOCKED",
                    (MAX_ATTEMPTS,),
                )
                row = cur.fetchone()
                if not row:
                    conn.commit()
                    return None
                lease_token = uuid.uuid4().hex
                cur.execute(
                    "UPDATE chat_turns SET status = 'running', attempt_count = attempt_count + 1, "
                    "lease_token = %s, lease_until = DATE_ADD(NOW(3), INTERVAL %s SECOND), "
                    "error_code = '', error_message = '' WHERE id = %s",
                    (lease_token, LEASE_SECONDS, row["id"]),
                )
                row["status"] = "running"
                row["attempt_count"] += 1
                row["lease_token"] = lease_token
            conn.commit()
            return row
        except Exception:
            conn.rollback()
            raise


def complete_turn(row, *, answer: str, result_type: str = "ai",
                  ticket_id=None, ticket_no: str = "", notice: str = "",
                  answer_role: str = ""):
    """Commit the answer and terminal turn state atomically; stale leases cannot write.

    ``notice`` 是**系统提示**（目前只有转人工后的工单号），单独落一条 ``role=system``
    消息：客户读到的是"回答归回答、工单归工单"，而不是在一段解答后面追加工单号。
    两条消息共用同一个 ``answer_id`` 锚点，历史按 ``id`` 正序取，先后不会颠倒。

    ``answer_role`` 用来把"这一轮的结果类型"和"答复属于谁"解耦：``result_type='ticket'``
    表示这一轮以建单收口，但**先答后转**时那段答复确实是 Agent 写的，必须落 ``ai``，
    否则刷新后历史会把回答渲染成系统提示（实时气泡与历史样式对不上）。
    """
    role = answer_role or ("ai" if result_type == "ai" else "system")
    with db() as conn:
        conn.begin()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT status, lease_token, answer_message_id FROM chat_turns "
                            "WHERE id = %s FOR UPDATE", (row["id"],))
                current = cur.fetchone()
                if not current or current["status"] != "running" or current["lease_token"] != row["lease_token"]:
                    conn.commit()
                    return False
                cur.execute(
                    "INSERT INTO messages (thread_id, role, content, msg_type, tenant_id, "
                    "ticket_id, is_read, answer_id, created_at) "
                    "VALUES (%s, %s, %s, 'text', %s, %s, 0, %s, NOW(3))",
                    (row["thread_id"], role, answer, row["tenant_id"], ticket_id,
                     row["turn_id"]),
                )
                answer_message_id = cur.lastrowid
                if notice.strip():
                    # 系统提示另起一条：拼进 answer 会让客户在同一段话里既读解答又读工单号
                    cur.execute(
                        "INSERT INTO messages (thread_id, role, content, msg_type, tenant_id, "
                        "ticket_id, is_read, answer_id, created_at) "
                        "VALUES (%s, 'system', %s, 'text', %s, %s, 0, %s, NOW(3))",
                        (row["thread_id"], notice.strip(), row["tenant_id"], ticket_id,
                         row["turn_id"]),
                    )
                if ticket_id:
                    cur.execute("UPDATE messages SET ticket_id = %s WHERE id = %s "
                                "AND tenant_id = %s AND thread_id = %s",
                                (ticket_id, row["user_message_id"],
                                 row["tenant_id"], row["thread_id"]),
                    )
                cur.execute(
                    "UPDATE chat_turns SET status = 'completed', result_type = %s, "
                    "answer_message_id = %s, ticket_no = %s, lease_until = NULL, "
                    "lease_token = NULL, error_code = '', error_message = '' WHERE id = %s",
                    (result_type, answer_message_id, ticket_no, row["id"]),
                )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def lease_held(row) -> bool:
    """这条租约是否仍然属于自己 —— 外部副作用（建单、resume）之前必须先问一次。

    ``complete_turn`` 的栅栏能挡住别人写答复，但挡不住我们先去建了一张工单、调了一次
    ``/agent/resume``：那个动作在数据库之外，收不回来。所以凡是"不可回滚"的步骤都先读
    一次租约，确认自己还是这轮的持有者。
    """
    current = one("SELECT status, lease_token FROM chat_turns WHERE id = %s", (row["id"],))
    return bool(current) and current["status"] == "running" \
        and current["lease_token"] == row["lease_token"]


def heartbeat(row) -> bool:
    """把租约往后推 ``LEASE_SECONDS``；返回 ``False`` 表示这轮已经不归自己。

    为什么需要：一轮真要跑到 65s 以上（Agent 重试 + 慢模型）时，租约会自然到期，别的
    Worker 会把它当僵尸领走，同一条消息被两个线程各跑一遍。续租把"超时回收"重新变回
    "只针对真崩掉的 Worker"。
    """
    changed = write(
        "UPDATE chat_turns SET lease_until = DATE_ADD(NOW(3), INTERVAL %s SECOND) "
        "WHERE id = %s AND status = 'running' AND lease_token = %s",
        (LEASE_SECONDS, row["id"], row["lease_token"]),
    )
    return changed == 1


def queue_stats() -> dict:
    """队列快照：``/metrics`` 与 Worker 周期日志共用。

    ``oldest_queued_seconds`` 是唯一值得告警的数字 —— 它等于"客户实际等了多久"，
    其它指标（深度、running 数）只用来解释它为什么涨。
    """
    row = one(
        "SELECT SUM(status = 'queued') AS queued, SUM(status = 'running') AS running, "
        "SUM(status = 'running' AND lease_until < NOW(3)) AS stale_running, "
        "SUM(attempt_count > 1) AS retried "
        "FROM chat_turns WHERE status IN ('queued', 'running')",
    ) or {}
    age = one(
        "SELECT TIMESTAMPDIFF(SECOND, MIN(created_at), NOW(3)) AS age "
        "FROM chat_turns WHERE status = 'queued'",
    ) or {}
    return {
        "queued": int(row.get("queued") or 0),
        "running": int(row.get("running") or 0),
        "stale_running": int(row.get("stale_running") or 0),
        "retried": int(row.get("retried") or 0),
        "oldest_queued_seconds": float(age.get("age") or 0),
    }


def record_failure(row, code: str, message: str):
    """Retry transient failures, then expose a terminal failure to the customer."""
    terminal = int(row["attempt_count"]) >= MAX_ATTEMPTS
    status = "failed" if terminal else "queued"
    delay = 0 if terminal else min(2 ** int(row["attempt_count"]), 30)
    with db() as conn:
        conn.begin()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE chat_turns SET status = %s, lease_until = NULL, "
                    "lease_token = NULL, next_attempt_at = DATE_ADD(NOW(3), INTERVAL %s SECOND), "
                    "error_code = %s, error_message = %s "
                    "WHERE id = %s AND status = 'running' AND lease_token = %s",
                    (status, delay, code[:64], message[:255],
                     row["id"], row["lease_token"]),
                )
                changed = cur.rowcount == 1
            conn.commit()
            return status if changed else None
        except Exception:
            conn.rollback()
            raise
