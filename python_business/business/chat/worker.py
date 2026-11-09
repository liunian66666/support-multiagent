"""Independent MySQL-backed customer chat worker for business-maintenance.

分流规则见 :mod:`business.chat.routing`：业务状态先行（人工接管 → 媒体 → 用户点名转人工 →
工单排队 → 四 Agent），Agent 只处理最后一种。这里是**执行**那一层，负责三件容易出错的事：

* ``approval_pending``（409）不是故障：服务端还停着一条旧审批 → 建/复用工单 + resume +
  把本轮提交为 ``ticket``；已有工单就直接提示等待。绝不能让它走"失败重试三次"。
* ``approval_state_unknown``（503）是可重试的基础设施错误：不建工单、不当作已回答。
* 幂等：``turn_id`` 贯穿 HTTP 与幂等键，工单靠 ``ensure_customer_ticket`` 的
  ``FOR UPDATE`` 复用，轮次提交靠租约栅栏——重试不会多出一条答复或第二张工单。
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import threading
import time

import httpx

from business.admin.sessions import extract_from_content, upsert_extracted_profile
from business.admin.tickets import AGENT_URL
from business.chat import recovery
from business.chat.realtime import ensure_customer_ticket, handling_ticket, waiting_ticket
from business.chat.routing import (APPROVAL_REASON, ERROR_APPROVAL_PENDING,
                                   ERROR_APPROVAL_UNKNOWN, HANDOFF_REASON, HUMAN_ANSWER,
                                   MEDIA_ANSWER, ROUTE_AGENT, ROUTE_ANSWER_THEN_HANDOFF,
                                   ROUTE_HANDOFF, ROUTE_HUMAN,
                                   ROUTE_MEDIA, ROUTE_PENDING, TICKET_ANSWER,
                                   WAITING_ANSWER, answer_then_handoff,
                                   explicit_handoff, route_message, tidy_handoff_reply)
from business.chat.turns import (HEARTBEAT_SECONDS, claim_next, complete_turn,
                                 fail_expired_leases, heartbeat, lease_held,
                                 queue_stats, record_failure)
from business.customer.routes import Customer, iso
from business.infra.storage import cache

logger = logging.getLogger("business.chat.worker")
BUSINESS_URL = os.getenv("CS_BUSINESS_URL", "http://business:8001")

__all__ = ["AgentGateError", "LeaseKeeper", "explicit_handoff", "process_claim",
           "reviewed_agent_reply", "run_loop", "run_once", "send_reviewed_answer"]


class AgentGateError(RuntimeError):
    """Agent 在建立流之前就以 409/503 拒绝了本轮（不是"回答失败"）。

    ``code`` 是 ``detail.error_code``：``approval_pending``（该建单）与
    ``approval_state_unknown``（该重试）。两者都不该被当成"Agent 挂了"。
    """

    def __init__(self, code: str, payload: dict | None = None, status: int | None = None):
        super().__init__(code)
        self.code = code
        self.payload = payload or {}
        self.status = status


def _headers(tenant_id: str, turn_id: str):
    return {"X-Internal-Token": os.getenv("INTERNAL_API_TOKEN", ""),
            "X-Tenant-Id": tenant_id, "X-Trace-Id": turn_id}


def notify(row, payload, *, target="user"):
    """Best effort after commit; a closed page can always recover from MySQL."""
    body = {"target": target, "payload": payload}
    if target == "user":
        body["key"] = row["thread_id"]
    else:
        body["tenant_id"] = row["tenant_id"]
    try:
        response = httpx.post(
            BUSINESS_URL + "/api/agent/ws/broadcast", json=body,
            headers=_headers(row["tenant_id"], row["turn_id"]), timeout=5,
        )
        response.raise_for_status()
    except (httpx.HTTPError, ValueError):
        logger.warning("chat turn notification unavailable turn=%s", row["turn_id"])


def send_reviewed_answer(row, answer: str) -> None:
    """Reveal only the committed, reviewed answer; chat_end repairs missed chunks."""
    if len(answer) >= 80:
        chunk_size = max(40, math.ceil(len(answer) / 24))
        for seq, offset in enumerate(range(0, len(answer), chunk_size)):
            notify(row, {"type": "chat_chunk", "turn_id": row["turn_id"],
                         "seq": seq, "chunk": answer[offset:offset + chunk_size]})
            time.sleep(0.025)
    notify(row, {"type": "chat_end", "turn_id": row["turn_id"],
                 "msg": answer, "trace_id": row["turn_id"],
                 "answer_id": row["turn_id"]})


def _turn_update(row, status: str, *, error: str = ""):
    return {"type": "turn_update", "turn_id": row["turn_id"], "status": status,
            "user_message_id": row["user_message_id"],
            "attempt_count": row["attempt_count"], "error": error}


def _gate_detail(response) -> dict:
    """409/503 的 ``detail``（FastAPI 把 dict 放在 ``detail`` 里）。"""
    try:
        body = json.loads(response.read() or b"{}")
    except (ValueError, TypeError):
        return {}
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, dict) else {}


def reviewed_agent_reply(row, content: str) -> str:
    """Forward phase events, then accept only the reviewed terminal SSE answer.

    Agent 在**建立流之前**拒绝本轮时（409 未完成审批 / 503 状态未知）抛
    :class:`AgentGateError`，让调用方按业务状态处理，而不是当成"回答失败"重试。
    """
    phases = {"searching", "checking", "reviewing", "finalizing"}
    with httpx.stream(
        "POST", AGENT_URL + "/agent/chat/stream",
        json={"thread_id": row["thread_id"], "user_message": content,
              # 与 JSON 分支同一个幂等键：SSE 断开后按轮次状态恢复结果，
              # 而不是让重试变成第二条回答。
              "turn_id": row["turn_id"]},
        headers=_headers(row["tenant_id"], row["turn_id"]), timeout=75,
    ) as response:
        if response.status_code in (409, 503):
            detail = _gate_detail(response)
            raise AgentGateError(str(detail.get("error_code") or "agent_gate"),
                                 detail, response.status_code)
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "progress" and event.get("phase") in phases:
                notify(row, {**_turn_update(row, "running"), "phase": event["phase"]})
            elif event.get("type") == "interrupt":
                # 主图当前不产生中断；留这条是为了别把"要人"误读成"回答失败"。
                raise AgentGateError("interrupt", event.get("data") or {})
            elif event.get("type") == "end":
                answer = str(event.get("content") or "").strip()
                if answer:
                    return answer
                raise RuntimeError("Agent 返回空回答")
            elif event.get("type") == "error":
                raise RuntimeError("Agent 未完成回答")
    raise RuntimeError("Agent 未发送结束事件")


def _customer(row):
    return Customer(row["tenant_id"], row["customer_id"], row["thread_id"],
                    "", False, False)


def _finish_ticket(row, customer, reason: str, *, check_approval: bool = False,
                   notice: str | None = None) -> None:
    """建/复用工单 →（需要时）恢复旧审批 → 把本轮提交为 ``ticket`` → 通知。

    三条容易做错的约束：

    1. **先验租约再动手**：建单和 resume 都在数据库之外，失去租约的 Worker 再做一次就是
       重复工单 + 重复 resume。``lease_held`` 挡在第一步。
    2. **恢复失败不静默结案**：``recover_for_ticket`` 会把失败写成工单待办
       （``approval_resume_state``），由后台线程继续；这里既不能假装恢复成功，也不能
       因为恢复失败就不建单（客户确实需要人工）。
    3. 幂等：``ensure_customer_ticket`` 在会话行上 ``FOR UPDATE`` 复用同一张工单；
       ``complete_turn`` 用租约栅栏挡住重复提交（返回 ``False`` 就不再通知）。
    """
    if not lease_held(row):
        logger.warning("chat turn lost its lease before ticket side effects turn=%s",
                       row["turn_id"])
        return
    ticket, reused = ensure_customer_ticket(customer, reason)
    result = None
    if check_approval:
        result = recovery.recover_for_ticket(ticket, trace_id=row["turn_id"])
        if not result.settled:
            logger.warning("approval resume pending turn=%s ticket=%s outcome=%s",
                           row["turn_id"], ticket["ticket_no"], result.outcome.value)
    ticket_answer = TICKET_ANSWER.format(ticket_no=ticket["ticket_no"])
    # 回答归回答、工单归工单：Agent 的答复原样落成 ai 消息，工单号另落一条 system 提示。
    # 拼成一段的话，客户会在同一段话末尾读到"已提交人工客服，工单号：TK-xxx"，
    # 分不清哪句是解答、哪句是系统状态。
    replied = bool(notice and notice.strip())
    if replied:
        # 这一轮下面就会出现"🎫 已创建工单"，答复里再教客户"输入「转人工」/打热线"是自相矛盾的：
        # 模型不知道业务层会自动建单，所以在这里把那段引导收敛掉，只留业务结论。
        answer = tidy_handoff_reply(notice)
        replied = bool(answer)  # 整理完只剩开场白 ⇒ 等同于没有答复，退回纯转人工
    if not replied:
        answer = ticket_answer
    notice_text = ticket_answer if replied else ""
    if complete_turn(row, answer=answer, result_type="ticket",
                     ticket_id=ticket["id"], ticket_no=ticket["ticket_no"],
                     notice=notice_text,
                     answer_role="ai" if replied else ""):
        if not reused:
            notify(row, {"type": "new_ticket", "ticket": {
                "id": ticket["id"], "ticket_no": ticket["ticket_no"],
                "thread_id": row["thread_id"], "user_id": row["thread_id"],
                "reason": reason, "status": ticket["status"],
                "created_at": iso(ticket["created_at"]),
            }}, target="admins_tenant")
        notify(row, {"type": "ticket", "turn_id": row["turn_id"],
                     "ticket_no": ticket["ticket_no"], "reused": reused,
                     "reason": reason,
                     "msg": answer if replied else "已提交人工客服，请等待处理",
                     "answer": answer if replied else ""})
        if result is not None and not result.settled:
            # 让运维侧看得见"这张工单还欠一次恢复"，而不是等客户投诉才发现
            notify(row, {"type": "approval_recovery", "turn_id": row["turn_id"],
                         "ticket_no": ticket["ticket_no"],
                         "outcome": result.outcome.value,
                         "error_code": result.error_code,
                         "msg": "旧审批未恢复，已转入后台重试"}, target="admins_tenant")


def _waiting_notice(row, customer, ticket) -> None:
    """已有待处理工单：给确定提示，**不再询问 Agent**。"""
    answer = WAITING_ANSWER.format(ticket_no=ticket["ticket_no"])
    if complete_turn(row, answer=answer, result_type="ticket",
                     ticket_id=ticket["id"], ticket_no=ticket["ticket_no"]):
        notify(row, {"type": "ticket", "turn_id": row["turn_id"],
                     "ticket_no": ticket["ticket_no"], "reused": True,
                     "reason": ticket.get("reason") or "",
                     "msg": "已提交人工客服，请等待人工接入"})


def _retry_later(row, code: str, message: str) -> None:
    """可重试的基础设施错误：不进"Agent 挂了"的计数，也不给客户编答案。"""
    status = record_failure(row, code, message)
    if status:
        notify(row, _turn_update(row, status,
                                 error="处理失败，可重试" if status == "failed" else ""))


def process_claim(row):
    """Process one fenced lease, commit its result, then notify live clients."""
    customer = _customer(row)
    content = row["user_message"]
    try:
        if row["msg_type"] == "text":
            try:
                upsert_extracted_profile(row["thread_id"], row["tenant_id"],
                                         extract_from_content(content))
            except Exception:
                logger.exception("chat profile extraction failed turn=%s", row["turn_id"])

        active_ticket = handling_ticket(customer)
        queued = None if active_ticket else waiting_ticket(customer)
        route = route_message(msg_type=row["msg_type"], handling=bool(active_ticket),
                              pending=bool(queued), handoff=explicit_handoff(content),
                              answer_then_transfer=answer_then_handoff(content))
        logger.info("chat route=%s turn=%s tenant=%s", route, row["turn_id"],
                    row["tenant_id"])

        if route == ROUTE_HUMAN:
            answer = HUMAN_ANSWER
            if complete_turn(row, answer=answer, result_type="human",
                             ticket_id=active_ticket["id"],
                             ticket_no=active_ticket["ticket_no"]):
                notify(row, {"type": "user_msg", "thread_id": row["thread_id"],
                             "user_id": row["thread_id"], "msg": content,
                             "msg_type": row["msg_type"]}, target="admins_tenant")
                notify(row, {"type": "human_notice", "turn_id": row["turn_id"],
                             "msg": answer})
            return

        if route == ROUTE_MEDIA:
            if complete_turn(row, answer=MEDIA_ANSWER, result_type="notice"):
                notify(row, {"type": "notice", "turn_id": row["turn_id"],
                             "msg": MEDIA_ANSWER})
            return

        if route == ROUTE_HANDOFF:
            # 客户点名转人工也要看一眼旧审批：这条路径不经过 Agent 闸门，
            # 若线程上还停着旧审批，光建单不收口，旧审批就永远留在检查点里。
            _finish_ticket(row, customer, HANDOFF_REASON, check_approval=True)
            return

        if route == ROUTE_PENDING:
            _waiting_notice(row, customer, queued)
            return

        if route not in (ROUTE_AGENT, ROUTE_ANSWER_THEN_HANDOFF):
            # route_message 只返回上面五个值；真出现第六种，宁可报错也别猜着回答
            raise AssertionError(f"unknown route: {route}")

        # ROUTE_AGENT：四 Agent 是唯一执行体（旧图只从 /agent/resume 进）。
        try:
            answer = reviewed_agent_reply(row, content)
        except AgentGateError as gate:
            if gate.code == ERROR_APPROVAL_PENDING:
                reason = str((gate.payload.get("pending_approval") or {}).get("reason")
                             or APPROVAL_REASON)
                _finish_ticket(row, customer, reason, check_approval=True)
                return
            if gate.code == ERROR_APPROVAL_UNKNOWN:
                _retry_later(row, ERROR_APPROVAL_UNKNOWN, "会话状态确认中，请稍后重试")
                return
            raise

        if route == ROUTE_ANSWER_THEN_HANDOFF:
            _finish_ticket(row, customer, HANDOFF_REASON, notice=answer)
            return

        if complete_turn(row, answer=answer, result_type="ai"):
            send_reviewed_answer(row, answer)
    except Exception as exc:
        logger.exception("chat turn processing failed turn=%s error_type=%s",
                         row["turn_id"], type(exc).__name__)
        status = record_failure(row, "agent_unavailable",
                                "处理失败，请稍后重试" if row["attempt_count"] >= 3
                                else "正在重试")
        if status:
            notify(row, _turn_update(row, status,
                                     error="处理失败，可重试" if status == "failed" else ""))


class LeaseKeeper:
    """在一轮处理期间持续续租，避免"跑得久"被误判成"Worker 崩了"。

    没有续租时，``LEASE_SECONDS`` 是硬上限：一轮耗时超过它，``claim_next`` 会把同一轮
    再发给另一个 Worker（``attempt_count`` +1），同一条消息被两个线程各跑一遍。加了续租，
    回收只针对真的死掉的线程。心跳失败（``rowcount=0``）说明这轮已经归别人，立刻停手：
    后续的 ``complete_turn`` / ``lease_held`` 会再挡一次。
    """

    def __init__(self, row, interval: float | None = None):
        self.row = row
        self.interval = HEARTBEAT_SECONDS if interval is None else max(0.0, interval)
        self.lost = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        if self.interval <= 0:
            return self
        self._thread = threading.Thread(
            target=self._beat, daemon=True,
            name="lease-" + str(self.row["turn_id"])[:8])
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        return False

    def _beat(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                alive = heartbeat(self.row)
            except Exception:
                # 一次续租失败不代表丢租约（数据库抖动）；下次再试，真丢了 complete_turn 会挡
                logger.warning("chat turn heartbeat failed turn=%s",
                               self.row["turn_id"], exc_info=True)
                continue
            if not alive:
                if self._stop.is_set():
                    return  # 正常收尾：这轮刚好在我们续租前提交完了
                self.lost = True
                logger.warning("chat turn lease ended early turn=%s "
                               "(taken over by another worker)",
                               self.row["turn_id"])
                return


_reclaim_lock = threading.Lock()
_last_reclaim = 0.0


def _reclaim_expired(force: bool = False):
    """回收僵尸租约。多 Worker 共用进程时降频，避免每个线程每秒扫一遍同一张表。"""
    global _last_reclaim
    interval = float(os.getenv("CHAT_TURN_RECLAIM_SECONDS", "10"))
    with _reclaim_lock:
        if not force and interval > 0 and time.monotonic() - _last_reclaim < interval:
            return []
        _last_reclaim = time.monotonic()
    expired = fail_expired_leases()
    for row in expired:
        notify(row, {"type": "turn_update", "turn_id": row["turn_id"],
                     "status": "failed", "error": "处理失败，可重试"})
    return expired


def _recovery_pass(interval: float):
    """旧审批恢复扫描：多 Worker 下只让一个线程做（Redis 短租约选主）。

    为什么不是每个线程都扫：``claim_due`` 虽然是 ``SKIP LOCKED`` 安全的，但 N 个线程每
    15 秒同时抢同一批行只会空转。Redis 不可用时**退化为每线程都扫**——重复扫描只是浪费，
    漏扫会让工单永远卡住，两者的代价不对称。
    """
    lock = None
    try:
        lock = cache().lock("cs:business:resume-leader",
                            timeout=max(30, int(interval) * 4), blocking_timeout=0)
        if not lock.acquire(blocking=False):
            return None
    except Exception:
        logger.warning("resume leader election unavailable; scanning anyway")
        lock = None
    try:
        return recovery.recover_due()
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass


_queue_logged = 0.0


def _log_queue(worker_id: int) -> None:
    """周期打印队列快照：客户到底等了多久，只有 '最老 queued' 能回答。"""
    global _queue_logged
    interval = float(os.getenv("CHAT_QUEUE_STATS_SECONDS", "60"))
    if interval <= 0 or time.monotonic() - _queue_logged < interval:
        return
    _queue_logged = time.monotonic()
    try:
        stats = queue_stats()
    except Exception:
        logger.warning("chat queue stats unavailable", exc_info=True)
        return
    if not stats["queued"] and not stats["running"]:
        return
    logger.info("chat queue worker=%d queued=%d running=%d stale=%d retried=%d "
                "oldest_queued=%.1fs", worker_id, stats["queued"], stats["running"],
                stats["stale_running"], stats["retried"],
                stats["oldest_queued_seconds"])


def run_once(worker_id: int = 0):
    _reclaim_expired()
    row = claim_next()
    if not row:
        return False
    logger.info("chat turn claimed worker=%d turn=%s attempt=%s tenant=%s",
                worker_id, row["turn_id"], row["attempt_count"], row["tenant_id"])
    notify(row, _turn_update(row, "running"))
    with LeaseKeeper(row) as keeper:
        process_claim(row)
    if keeper.lost:
        logger.warning("chat turn finished after losing its lease worker=%d turn=%s",
                       worker_id, row["turn_id"])
    return True


def run_loop(worker_id: int = 0):
    """常驻取轮循环。``worker_id`` 只用于日志分辨线程，不参与任何语义。

    多线程下的三条约束：

    * 空转退避带抖动：N 个线程都 ``sleep(1)`` 会变成每秒 N 次 ``claim_next`` 的空扫；
    * 有活就连着取，不 sleep —— 队列积压时吞吐才是要紧的；
    * 恢复扫描选主（:func:`_recovery_pass`），不让 N 个线程各扫一遍。
    """
    interval = max(0.2, float(os.getenv("CHAT_TURN_POLL_SECONDS", "1")))
    recovery_interval = float(os.getenv("CHAT_RESUME_RETRY_SECONDS", "15"))
    last_recovery = 0.0
    while True:
        try:
            # 旧审批恢复不依赖客户再发消息：排队中的会话永远走不到 Agent 的 409，
            # 所以必须由这里按工单待办独立重试。
            if recovery_interval > 0 and time.monotonic() - last_recovery >= recovery_interval:
                last_recovery = time.monotonic()
                stats = _recovery_pass(recovery_interval)
                if stats and stats["claimed"]:
                    logger.info("approval resume retry %s", stats)
            _log_queue(worker_id)
            worked = run_once(worker_id)
        except Exception:
            logger.exception("chat turn worker cycle failed worker=%d", worker_id)
            worked = False
        if not worked:
            time.sleep(interval * random.uniform(0.8, 1.2))
