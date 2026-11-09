# 中文文案必然包含全角标点
"""旧审批恢复的闭环：探测 → 恢复 → 失败记账 → 后台重试。

为什么不能"调一次 ``/agent/resume``、拿到 content 就当恢复成功"：

* ``/agent/resume`` 在**没有待审批**时返回 ``success=false`` + ``no_pending_approval``；
  在**检查点读不到**时返回 ``success=false`` + ``approval_state_unknown``；网络超时则是
  第三种情况——服务端可能已经恢复完了。三种都不是"已恢复"，也都不该被当成普通文本。
* 恢复失败之后，客户端不会自动重试：排队中的工单让后续消息直接拿到"等待接入"提示，
  根本走不到 Agent 的 409。所以"下次客户发消息自动修好"是不成立的，必须有独立的重试。

因此本模块把恢复拆成四态（:class:`ResumeOutcome`），并把失败落成工单上的**待办记录**
（``tickets.approval_resume_state``），由 :func:`recover_due` 周期重试。恢复动作本身是
幂等的：每次重试先探测状态，已经是 ``clear`` 就直接结案，不会对已恢复的审批再调一次。

写库的部分（``_store_*`` / ``claim_due``）都用模块级函数，便于测试替换成内存台账。
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

import httpx

from business.admin.sessions import tenant_of_thread
from business.core.constants import AGENT_URL as _AGENT_URL
from business.infra.storage import many, one, write

logger = logging.getLogger("business.chat.recovery")

AGENT_URL = _AGENT_URL
RESUME_TIMEOUT = float(os.getenv("CHAT_RESUME_TIMEOUT", "30"))
PROBE_TIMEOUT = float(os.getenv("CHAT_PROBE_TIMEOUT", "5"))
MAX_ATTEMPTS = max(1, int(os.getenv("CHAT_RESUME_MAX_ATTEMPTS", "8")))
LEASE_SECONDS = 60

# 工单上的待办状态
RESUME_NONE = 0        # 从没有过待恢复的旧审批
RESUME_PENDING = 1     # 有旧审批没恢复完，后台要继续做
RESUME_DONE = 2        # 已确认恢复（或确认本来就没有待审批）
RESUME_EXHAUSTED = 3   # 重试次数用尽，需要人工介入


class ResumeOutcome(str, Enum):
    """一次恢复尝试的四种结果。"""

    RECOVERED = "recovered"
    """服务端确认恢复完成（或超时后复查确认已经恢复）。"""

    CLEAR = "clear"
    """确认这条线程本来就没有待审批 —— 无需恢复。"""

    UNKNOWN = "unknown"
    """状态未知（检查点读不到 / 服务端 5xx）。可重试，但**不能**当作已恢复。"""

    UNAVAILABLE = "unavailable"
    """网络层面没通（连接失败、超时）。服务端可能已经做完了，必须先复查再重试。"""


@dataclass(frozen=True)
class ResumeResult:
    outcome: ResumeOutcome
    content: str = ""
    error_code: str = ""
    status: int = 0
    detail: dict = field(default_factory=dict)

    @property
    def settled(self) -> bool:
        """是否可以认为"这条线程不再卡在旧审批上"。"""
        return self.outcome in (ResumeOutcome.RECOVERED, ResumeOutcome.CLEAR)

    @property
    def retryable(self) -> bool:
        return self.outcome in (ResumeOutcome.UNKNOWN, ResumeOutcome.UNAVAILABLE)


def _headers(thread_id: str, trace_id: str = "") -> dict:
    headers = {"X-Internal-Token": os.getenv("INTERNAL_API_TOKEN", ""),
               "X-Tenant-Id": tenant_of_thread(thread_id)}
    if trace_id:
        headers["X-Trace-Id"] = trace_id
    return headers


# ---------------------------------------------------------------------------
# 与 Agent 的两次往返
# ---------------------------------------------------------------------------

def probe_state(thread_id: str, trace_id: str = "") -> str:
    """读服务端三态：``pending`` / ``clear`` / ``unknown``。

    任何异常都返回 ``unknown`` —— 探测失败不能退化成"没有审批"。
    """
    try:
        response = httpx.get(AGENT_URL + "/agent/approval/" + thread_id,
                             headers=_headers(thread_id, trace_id),
                             timeout=PROBE_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning("approval probe failed thread=%s error=%s",
                       thread_id, type(exc).__name__)
        return "unknown"
    if response.status_code == 404:
        # 老版本 Agent 没有这个端点：状态不可知，别猜
        return "unknown"
    if response.status_code != 200:
        return "unknown"
    try:
        body = response.json() or {}
    except ValueError:
        return "unknown"
    state = str((body.get("data") or body).get("approval_state") or "").lower()
    return state if state in ("pending", "clear", "unknown") else "unknown"


def resume_once(thread_id: str, *, confirmed: bool = True, wait_time: int = 3,
                ticket_no: str = "", trace_id: str = "") -> ResumeResult:
    """调一次 ``/agent/resume``，按状态码 + ``success`` + ``error_code`` 判结果。

    **不做盲重试**：超时意味着服务端可能已经恢复，再调一次虽然幂等（服务端会先校验
    有没有待审批），但会多跑一轮旧图；正确做法是交给 :func:`recover_thread` 先复查。
    """
    payload = {"thread_id": thread_id, "confirmed": confirmed,
               "wait_time": wait_time, "ticket_id": ticket_no}
    try:
        response = httpx.post(AGENT_URL + "/agent/resume", json=payload,
                              headers=_headers(thread_id, trace_id),
                              timeout=RESUME_TIMEOUT)
    except httpx.TimeoutException:
        return ResumeResult(ResumeOutcome.UNAVAILABLE, error_code="resume_timeout",
                            detail={"timeout": True})
    except httpx.HTTPError as exc:
        return ResumeResult(ResumeOutcome.UNAVAILABLE,
                            error_code="resume_transport_" + type(exc).__name__)

    status = response.status_code
    if status == 503:
        # 闸门自己的"状态未知"，与 5xx 崩溃不是一回事：语义上仍是 UNKNOWN
        return ResumeResult(ResumeOutcome.UNKNOWN, status=status,
                            error_code="approval_state_unknown")
    if status >= 500:
        return ResumeResult(ResumeOutcome.UNAVAILABLE, status=status,
                            error_code="resume_http_%d" % status)
    try:
        body = response.json() or {}
    except ValueError:
        return ResumeResult(ResumeOutcome.UNKNOWN, status=status,
                            error_code="resume_bad_json")
    if not isinstance(body, dict):
        return ResumeResult(ResumeOutcome.UNKNOWN, status=status,
                            error_code="resume_bad_body")

    code = str(body.get("error_code") or "")
    if body.get("success"):
        return ResumeResult(ResumeOutcome.RECOVERED, content=str(body.get("content") or ""),
                            status=status)
    if code == "no_pending_approval":
        return ResumeResult(ResumeOutcome.CLEAR, error_code=code, status=status)
    if code == "approval_state_unknown":
        return ResumeResult(ResumeOutcome.UNKNOWN, error_code=code, status=status)
    return ResumeResult(ResumeOutcome.UNKNOWN, status=status,
                        error_code=code or "resume_failed",
                        content=str(body.get("content") or ""))


def recover_thread(thread_id: str, *, confirmed: bool = True, wait_time: int = 3,
                   ticket_no: str = "", trace_id: str = "") -> ResumeResult:
    """完整的一次恢复：先探测，需要时才恢复，失败后复查。

    复查这一步专门处理"**服务端已恢复、客户端超时**"：超时后状态已经是 ``clear``，
    再 resume 一次只会白跑一轮旧图，所以直接用复查结果结案。
    """
    state = probe_state(thread_id, trace_id)
    if state == "clear":
        return ResumeResult(ResumeOutcome.CLEAR, error_code="already_clear")
    if state == "unknown":
        return ResumeResult(ResumeOutcome.UNKNOWN, error_code="probe_unknown")

    result = resume_once(thread_id, confirmed=confirmed, wait_time=wait_time,
                         ticket_no=ticket_no, trace_id=trace_id)
    if result.settled:
        return result

    # 失败后一定要复查：恢复可能已经在服务端生效（超时/连接被掐断都是这种情况）
    after = probe_state(thread_id, trace_id)
    if after == "clear":
        return ResumeResult(ResumeOutcome.RECOVERED, content=result.content,
                            error_code=result.error_code or "verified_after_failure",
                            status=result.status,
                            detail={**result.detail, "verified_after_failure": True})
    if after == "pending":
        return result
    return ResumeResult(ResumeOutcome.UNKNOWN,
                        error_code=result.error_code or "resume_state_unknown",
                        status=result.status)


# ---------------------------------------------------------------------------
# 待办记账（工单上的 approval_resume_*）
# ---------------------------------------------------------------------------

def _backoff_seconds(attempts: int) -> int:
    return min(2 ** max(1, attempts), 300)


def _store_mark_pending(ticket_id, error_code: str, attempts: int) -> None:
    """把失败记成待办；调用方已算好下次重试时间。"""
    write(
        "UPDATE tickets SET approval_resume_state = %s, approval_resume_attempts = %s, "
        "approval_resume_next_at = DATE_ADD(NOW(3), INTERVAL %s SECOND), "
        "approval_resume_error = %s, approval_resume_lease = NULL, "
        "approval_resume_lease_until = NULL WHERE id = %s",
        (RESUME_PENDING if attempts < MAX_ATTEMPTS else RESUME_EXHAUSTED,
         attempts, _backoff_seconds(attempts), error_code[:64], ticket_id),
    )


def _store_mark_settled(ticket_id, outcome: str) -> None:
    write(
        "UPDATE tickets SET approval_resume_state = %s, approval_resume_next_at = NULL, "
        "approval_resume_error = %s, approval_resume_lease = NULL, "
        "approval_resume_lease_until = NULL WHERE id = %s",
        (RESUME_DONE, str(outcome)[:64], ticket_id),
    )


def _store_attempts(ticket_id) -> int:
    row = one("SELECT approval_resume_attempts AS n FROM tickets WHERE id = %s", (ticket_id,))
    return int((row or {}).get("n") or 0)


def record(ticket_id, result: ResumeResult) -> None:
    """按结果更新待办。写库失败只记日志：**不能**因为记账失败把恢复结果吞掉。"""
    try:
        if result.settled:
            _store_mark_settled(ticket_id, result.outcome.value)
        else:
            attempts = _store_attempts(ticket_id) + 1
            _store_mark_pending(ticket_id, result.error_code or result.outcome.value, attempts)
    except Exception:
        logger.exception("approval resume record failed ticket=%s outcome=%s",
                         ticket_id, result.outcome.value)


def recover_for_ticket(ticket, *, confirmed: bool = True, wait_time: int = 3,
                       trace_id: str = "", record_result: bool = True) -> ResumeResult:
    """工单视角的一次恢复：探测 + 恢复，并把结果写回工单待办。

    恢复过程中任何**未预期**的异常都收敛成 ``UNKNOWN``：工单已经建好，不能让一次客户端
    异常把"还欠一次恢复"这件事丢掉（那正是客户再也回不到 AI 的那条路）。
    """
    try:
        result = recover_thread(ticket["thread_id"], confirmed=confirmed,
                                wait_time=wait_time, ticket_no=ticket["ticket_no"],
                                trace_id=trace_id)
    except Exception:
        logger.exception("approval resume crashed ticket=%s", ticket.get("ticket_no"))
        result = ResumeResult(ResumeOutcome.UNKNOWN, error_code="resume_crashed")
    if record_result:
        record(ticket["id"], result)
    logger.info("approval resume ticket=%s outcome=%s code=%s",
                ticket.get("ticket_no"), result.outcome.value, result.error_code)
    return result


# ---------------------------------------------------------------------------
# 后台重试
# ---------------------------------------------------------------------------

def claim_due(limit: int = 20):
    """领一批到点的待办（多实例安全：``SKIP LOCKED`` + 短租约）。"""
    lease = uuid.uuid4().hex
    with _db() as conn:
        conn.begin()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, thread_id, ticket_no, approval_resume_attempts "
                    "FROM tickets WHERE approval_resume_state = %s AND deleted_at = 0 "
                    "AND (approval_resume_next_at IS NULL OR approval_resume_next_at <= NOW(3)) "
                    "AND (approval_resume_lease_until IS NULL "
                    "     OR approval_resume_lease_until < NOW(3)) "
                    "ORDER BY approval_resume_next_at IS NULL, approval_resume_next_at, id "
                    "LIMIT %s FOR UPDATE SKIP LOCKED",
                    (RESUME_PENDING, limit),
                )
                rows = list(cur.fetchall())
                for row in rows:
                    cur.execute(
                        "UPDATE tickets SET approval_resume_lease = %s, "
                        "approval_resume_lease_until = DATE_ADD(NOW(3), INTERVAL %s SECOND) "
                        "WHERE id = %s",
                        (lease, LEASE_SECONDS, row["id"]),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return rows


def recover_due(limit: int = 20) -> dict:
    """重试所有到点的待办；返回统计，供日志与测试断言。"""
    stats = {"claimed": 0, "recovered": 0, "still_pending": 0}
    for ticket in claim_due(limit):
        stats["claimed"] += 1
        try:
            result = recover_for_ticket(ticket, trace_id="recovery")
        except Exception:
            logger.exception("approval resume retry failed ticket=%s", ticket["ticket_no"])
            stats["still_pending"] += 1
            continue
        if result.settled:
            stats["recovered"] += 1
        else:
            stats["still_pending"] += 1
    return stats


def recovery_loop(interval: float | None = None):
    """常驻重试：容器重启后靠数据库里的待办继续做，不依赖客户再发消息。"""
    interval = float(interval if interval is not None
                     else os.getenv("CHAT_RESUME_RETRY_SECONDS", "15"))
    if interval <= 0:
        logger.info("approval resume retry disabled")
        return
    while True:
        try:
            stats = recover_due()
            if stats["claimed"]:
                logger.info("approval resume retry %s", stats)
        except Exception:
            logger.exception("approval resume retry cycle failed")
        time.sleep(interval)


def _db():
    from business.infra.storage import db
    return db()


# ---------------------------------------------------------------------------
# 建表：老库补列（幂等）
# ---------------------------------------------------------------------------

_COLUMNS = {
    "approval_resume_state": "tinyint(1) NOT NULL DEFAULT 0",
    "approval_resume_attempts": "int NOT NULL DEFAULT 0",
    "approval_resume_next_at": "datetime(3) DEFAULT NULL",
    "approval_resume_error": "varchar(64) DEFAULT NULL",
    "approval_resume_lease": "varchar(64) DEFAULT NULL",
    "approval_resume_lease_until": "datetime(3) DEFAULT NULL",
}


def ensure_resume_schema() -> list[str]:
    """给已存在的 ``tickets`` 表补上待办列；返回本次新增的列名。"""
    existing = {row["COLUMN_NAME"] for row in many(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tickets'")}
    added = []
    for name, ddl in _COLUMNS.items():
        if name in existing:
            continue
        write(f"ALTER TABLE tickets ADD COLUMN {name} {ddl}", ())
        added.append(name)
    if added:
        write("CREATE INDEX idx_tickets_resume_due ON tickets "
              "(approval_resume_state, approval_resume_next_at)", ())
        logger.info("tickets.approval_resume_* added: %s", ",".join(added))
    return added


__all__ = [
    "MAX_ATTEMPTS",
    "RESUME_DONE",
    "RESUME_EXHAUSTED",
    "RESUME_NONE",
    "RESUME_PENDING",
    "ResumeOutcome",
    "ResumeResult",
    "claim_due",
    "ensure_resume_schema",
    "probe_state",
    "record",
    "recover_due",
    "recover_for_ticket",
    "recover_thread",
    "recovery_loop",
    "resume_once",
]
