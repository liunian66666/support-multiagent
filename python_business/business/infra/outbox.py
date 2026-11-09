"""[ours] S2 反馈 Outbox 同步：MySQL feedback_outbox → Agent PG experience_feedback。

拓扑约束：agent 容器无 MySQL 凭据，business-maintenance 容器同时具备
MySQL 读写与 agent HTTP 通路，故同步循环节在 maintenance 进程。

可靠性设计：
- 拉取 status=0 批次（默认 100/批，按 id 升序保证同租户事件有序）；
- 推送成功才 UPDATE status=1（至多一次投递 + agent 侧 event_id 幂等 = 有效一次）；
- 推送失败保持 status=0，下轮自然重试；agent 重复收到同批直接幂等吞掉；
- OUTBOX_SYNC_INTERVAL=0 可整体关闭（默认 60s）。
"""

import json
import logging
import os
import time

import httpx

from business.core.constants import AGENT_URL as _AGENT_URL
from business.infra.storage import many, write

logger = logging.getLogger("business.outbox")

AGENT_URL = _AGENT_URL
BATCH_SIZE = 100


def fetch_pending(limit=BATCH_SIZE):
    return many(
        "SELECT id, event_type, tenant_id, thread_id, answer_id, payload, "
        "UNIX_TIMESTAMP(created_at) AS occurred_ts FROM feedback_outbox "
        "WHERE status = 0 ORDER BY id ASC LIMIT %s", (limit,))


def build_events(rows):
    """MySQL 行 → agent 经验层事件。event_id 用 outbox:{id}，天然幂等键。"""
    events = []
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (ValueError, TypeError):
            payload = {}
        events.append({
            "event_id": f"outbox:{row['id']}",
            "tenant_id": row["tenant_id"],
            "thread_id": row["thread_id"] or "",
            "trace_id": "",
            "answer_id": row["answer_id"] or "",
            "experience_id": None,
            "result_type": row["event_type"],
            "value": payload,
            "source": "feedback_outbox",
            "occurred_ts": float(row["occurred_ts"]) if row["occurred_ts"] else None,
        })
    return events


def push_batch(events):
    """按租户分组推送（agent 端点单批单租户）。返回成功入库的 event_id 集合。"""
    stored = set()
    by_tenant = {}
    for ev in events:
        by_tenant.setdefault(ev["tenant_id"], []).append(ev)
    headers = {"X-Internal-Token": os.getenv("INTERNAL_API_TOKEN", ""),
               "Content-Type": "application/json"}
    for tenant_id, batch in by_tenant.items():
        headers["X-Tenant-Id"] = tenant_id
        response = httpx.post(
            AGENT_URL + "/agent/experience/feedback",
            json={"events": batch}, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"agent 拒收反馈批次: {data}")
        # 整批受理即视为入库（含幂等吞掉的重复事件）
        stored.update(ev["event_id"] for ev in batch)
    return stored


def mark_published(ids):
    if not ids:
        return
    write("UPDATE feedback_outbox SET status = 1, "
          "published_at = NOW() WHERE id IN ("
          + ",".join(["%s"] * len(ids)) + ")", tuple(ids))


def sync_once():
    """单轮同步。返回 (拉取条数, 已标记条数)；异常向上抛由调用方决定节奏。"""
    rows = fetch_pending()
    if not rows:
        return 0, 0
    events = build_events(rows)
    push_batch(events)
    mark_published([row["id"] for row in rows])
    return len(rows), len(events)


def sync_loop(interval=None, sleep=time.sleep):
    """maintenance 进程内的常驻循环；interval<=0 表示关闭。"""
    interval = (int(os.getenv("OUTBOX_SYNC_INTERVAL", "60"))
                if interval is None else interval)
    if interval <= 0:
        logger.info("outbox sync disabled (OUTBOX_SYNC_INTERVAL<=0)")
        return
    while True:
        try:
            fetched, pushed = sync_once()
            if fetched:
                logger.info("outbox sync: fetched=%d pushed=%d", fetched, pushed)
        except Exception:
            logger.exception("outbox sync failed; retrying next interval")
        sleep(interval)
