# -*- coding: utf-8 -*-
"""一次性触发提炼轮（不等待30分钟worker）——RSI 演示/评测专用。

在 cs_agent 容器内运行：
    python /app/evaluation/rsi_distill_once.py
自建最小 app 状态（PG 池 + httpx client），复用生产同一函数
find_distill_candidates → _distill_one → experience_items(candidate)。
不落任何锁（单实例演示环境可接受），输出本轮候选 id 列表。
"""
import asyncio
import types

import httpx
from psycopg_pool import AsyncConnectionPool

from app.infra.config import POSTGRES_DSN
from app.workers.distill import find_distill_candidates, _distill_one


async def main():
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    app.state.db_pool = AsyncConnectionPool(POSTGRES_DSN, min_size=1, max_size=2,
                                            open=True,
                                            kwargs={"autocommit": True})
    app.state.http_client = httpx.AsyncClient(timeout=15.0)
    try:
        cands = await find_distill_candidates(app, 20)
        print(f"candidates_found={len(cands)}")
        made = 0
        for c in cands:
            ok = await _distill_one(app, c)
            made += int(bool(ok))
            print(f"answer={c.get('answer_id','')} score={c.get('score')} -> {'OK' if ok else 'skip'}")
        print(f"distilled={made}")
    finally:
        await app.state.http_client.aclose()
        await app.state.db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
