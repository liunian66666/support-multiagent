"""Daily business-data maintenance formerly performed by the Go gateway."""

import logging
import os
import threading
import time

from business.admin.ops import archive_old_messages
from business.infra.outbox import sync_once as outbox_sync_once
from business.infra.storage import cache


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("business.maintenance")


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


def main():
    # [ours] S2：分钟级经验反馈同步独立于日频归档线程
    threading.Thread(target=outbox_sync_loop, daemon=True).start()
    while True:
        try:
            archive_once()
        except Exception:
            logger.exception("message archive failed; retrying next interval")
        time.sleep(24 * 60 * 60)


if __name__ == "__main__":
    main()
