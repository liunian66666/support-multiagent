# -*- coding: utf-8 -*-
"""一次性触发一轮会话摘要（不等 worker 定时器）——长对话记忆评测专用。

在 cs_agent 容器内运行：
    PYTHONPATH=/app python /app/evaluation/summary_once.py [--thread <thread_id>]
复用生产 _process_one（滚动合并→水位回写）；--thread 过滤单会话，
不带参数则整轮（等同 worker 一轮）。空闲阈值 SESSION_SUMMARY_IDLE_MINUTES（评测设 0）。
"""
import argparse
import asyncio
import os
import types

import httpx
from psycopg_pool import AsyncConnectionPool

from app.infra.config import BUSINESS_API, INTERNAL_API_TOKEN, POSTGRES_DSN
from app.workers.summary import IDLE_MINUTES, _process_one


async def main(thread: str):
    os.environ.setdefault("SESSION_SUMMARY_IDLE_MINUTES", "0")
    idle = int(os.environ["SESSION_SUMMARY_IDLE_MINUTES"])
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    app.state.db_pool = AsyncConnectionPool(POSTGRES_DSN, min_size=1, max_size=2,
                                            open=True, kwargs={"autocommit": True})
    app.state.http_client = httpx.AsyncClient(timeout=60.0)
    app.state.redis_client = None
    try:
        params = {"limit": 20, "idle_minutes": idle}
        if thread:
            params["thread_id"] = thread
        resp = await app.state.http_client.get(
            f"{BUSINESS_API}/api/agent/session-summaries/pending",
            params=params,
            headers={"X-Internal-Token": INTERNAL_API_TOKEN})
        cands = resp.json().get("data") or []
        print(f"candidates_found={len(cands)}")
        done = 0
        for cand in cands:
            if await _process_one(app, cand):
                done += 1
        print(f"summarized={done}/{len(cands)}")
    finally:
        await app.state.http_client.aclose()
        await app.state.db_pool.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--thread", default="")
    args = ap.parse_args()
    asyncio.run(main(args.thread))
