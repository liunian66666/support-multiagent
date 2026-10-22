"""Daily business-data maintenance formerly performed by the Go gateway."""

import logging
import os
import time

from business.admin.ops import archive_old_messages
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


def main():
    while True:
        try:
            archive_once()
        except Exception:
            logger.exception("message archive failed; retrying next interval")
        time.sleep(24 * 60 * 60)


if __name__ == "__main__":
    main()
