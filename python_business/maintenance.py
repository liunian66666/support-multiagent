"""Daily business-data maintenance for the Python business layer."""

import logging
import os
import threading
import time

import httpx

from business.admin.ops import archive_old_messages
from business.admin.tickets import AGENT_URL
from business.chat.recovery import ensure_resume_schema
from business.chat.worker import run_loop as chat_turn_loop
from business.core.constants import CONVERSATION_RETENTION_DAYS
from business.infra.outbox import sync_once as outbox_sync_once
from business.infra.storage import cache, many, write


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("business.maintenance")


def conversation_cleanup_once():
    """回收聊天会话：标记过期 → 让 agent 删检查点 → 只删成功的索引行。

    两条候选路径合在一起扫：

    * **用户主动删除**（``deleted_at`` 非空）—— 立即处理，不等 7 天；
    * **自然过期**（``last_active_at`` 超过保留期）—— 按 7 天判定。

    两条都必须排除有 ``queued``/``running`` 轮次、有未完成工单的会话：任务还没跑完
    或人工还没处理完时删掉检查点，Agent 就写不回来了。

    顺序不能反。MySQL 的 ``chat_conversations`` 是定位检查点的唯一线索，先删索引
    等于把检查点变成永远删不掉的孤儿。所以先把状态改成 ``expired``（列表不再返回、
    activate 返回 410），等 agent 确认删掉检查点，才物理删行。
    """
    days = CONVERSATION_RETENTION_DAYS
    lock = cache().lock("cs:business:conversation-cleanup", timeout=600,
                        blocking_timeout=0)
    if not lock.acquire(blocking=False):
        logger.info("conversation cleanup already running")
        return
    try:
        candidates = many(
            "SELECT c.thread_id, c.deleted_at FROM chat_conversations c "
            "WHERE (c.deleted_at IS NOT NULL "
            "       OR (c.status IN ('active', 'expired') "
            "           AND c.last_active_at < DATE_SUB(NOW(), INTERVAL %s DAY))) "
            "AND NOT EXISTS (SELECT 1 FROM chat_turns t WHERE t.tenant_id = c.tenant_id "
            " AND t.thread_id = c.thread_id AND t.status IN ('queued', 'running')) "
            "AND NOT EXISTS (SELECT 1 FROM tickets k WHERE k.tenant_id = c.tenant_id "
            " AND k.thread_id = c.thread_id AND k.status IN (0, 1) AND k.deleted_at = 0) "
            "ORDER BY c.deleted_at IS NULL, c.last_active_at ASC LIMIT 200",
            (days,),
        )
        thread_ids = [row["thread_id"] for row in candidates]
        if not thread_ids:
            logger.info("conversation cleanup: nothing to do")
            return
        marks = ",".join(["%s"] * len(thread_ids))
        write(f"UPDATE chat_conversations SET status = 'expired' "
              f"WHERE thread_id IN ({marks}) AND deleted_at IS NULL", tuple(thread_ids))

        response = httpx.post(
            AGENT_URL + "/agent/session/cleanup/batch",
            json={"threads": [{"thread_id": tid} for tid in thread_ids], "days": days},
            headers={"X-Internal-Token": os.getenv("INTERNAL_API_TOKEN", "")},
            timeout=120,
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        deleted = data.get("deleted_threads") or []
        if deleted:
            done = ",".join(["%s"] * len(deleted))
            write(f"DELETE FROM chat_conversations WHERE thread_id IN ({done})",
                  tuple(deleted))
        logger.info("conversation cleanup: candidates=%d deleted=%d failed=%d",
                    len(thread_ids), len(deleted), len(data.get("failed") or []))
    finally:
        lock.release()


def conversation_cleanup_loop():
    interval = int(os.getenv("CONVERSATION_CLEANUP_INTERVAL", "3600"))
    if interval <= 0:
        logger.info("conversation cleanup disabled")
        return
    while True:
        try:
            conversation_cleanup_once()
        except Exception:
            logger.exception("conversation cleanup failed; retrying next interval")
        time.sleep(interval)


def archive_once():
    days = max(1, min(int(os.getenv("MSG_RETENTION_DAYS", "90")), 365))
    lock = cache().lock("cs:business:message-archive", timeout=3600,
                        blocking_timeout=0)
    if not lock.acquire(blocking=False):
        logger.info("message archive already running")
        return
    try:
        count = archive_old_messages(days, "")
        logger.info("message archive completed: %d rows", count)
    finally:
        lock.release()


def outbox_sync_guarded():
    """[ours] S2：Outbox→agent 经验层同步（Redis 锁防多实例并发推同批）。"""
    lock = cache().lock("cs:business:feedback-outbox-sync", timeout=55,
                        blocking_timeout=0)
    if not lock.acquire(blocking=False):
        return
    try:
        outbox_sync_once()
    finally:
        lock.release()


def outbox_sync_loop():
    interval = int(os.getenv("OUTBOX_SYNC_INTERVAL", "60"))
    if interval <= 0:
        logger.info("feedback outbox sync disabled")
        return
    while True:
        try:
            outbox_sync_guarded()
        except Exception:
            logger.exception("feedback outbox sync failed; retrying next interval")
        time.sleep(interval)


def _worker_count():
    """聊天 Worker 数量。默认 4：够吃掉小店的并发，又不至于把 LLM 配额一次打满。

    上限 16 是刻意的 —— 真正的并发上限在 Agent 侧模型供应商的配额上，线程开过头只会
    把"在 MySQL 里排队"换成"在 provider 的 429 里排队"。
    """
    try:
        return max(1, min(int(os.getenv("CHAT_TURN_WORKERS", "4")), 16))
    except (TypeError, ValueError):
        return 4


def main():
    # 老库补列：approval_resume_* 是"旧审批待恢复"的唯一落点，缺列会让恢复失败无处记账
    try:
        ensure_resume_schema()
    except Exception:
        logger.exception("ticket approval-resume schema check failed")
    # [ours] S2：分钟级经验反馈同步独立于日频归档线程
    threading.Thread(target=outbox_sync_loop, daemon=True).start()
    # 会话回收由业务层驱动：只有这里能同时看到会话索引、进行中轮次和未完成工单
    threading.Thread(target=conversation_cleanup_loop, daemon=True,
                     name="conversation-cleanup").start()
    # 同一会话仍然串行（claim_next 的 NOT EXISTS 保证），不同会话并行。
    # 旧审批的恢复扫描由 worker 内部用 Redis 选主，只让一个线程做。
    workers = _worker_count()
    for index in range(workers):
        threading.Thread(target=chat_turn_loop, args=(index,), daemon=True,
                         name="chat-turn-%d" % index).start()
    logger.info("chat turn workers started: %d", workers)
    while True:
        try:
            archive_once()
        except Exception:
            logger.exception("message archive failed; retrying next interval")
        time.sleep(24 * 60 * 60)


if __name__ == "__main__":
    main()
