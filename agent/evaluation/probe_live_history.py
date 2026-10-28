# ruff: noqa: RUF001
"""Read-only look at what the live service actually ran recently.

Before any old/new comparison is trusted, two things must be known from data rather
than assumption: which chat model the recorded baseline turns used, and whether the
old path is currently succeeding at all. Both are plain aggregates over the agent
service's own tables; nothing is written.
"""

from __future__ import annotations

import asyncio
import os

QUERIES = [
    ("trace_runs 近 7 天按路由/状态",
     "SELECT route, status, count(*) AS n, max(created_at) AS latest "
     "FROM trace_runs WHERE created_at > now() - interval '7 days' "
     "GROUP BY 1,2 ORDER BY 3 DESC LIMIT 12"),
    ("token_usage 近 7 天按模型",
     "SELECT model, source, count(*) AS n, sum(total_tokens) AS tokens, max(created_at) AS latest "
     "FROM token_usage WHERE created_at > now() - interval '7 days' GROUP BY 1,2 ORDER BY 3 DESC LIMIT 12"),
    ("agent 侧会话检查点线程近 7 天",
     "SELECT count(DISTINCT thread_id) AS threads FROM checkpoints "
     "WHERE checkpoint_id IS NOT NULL"),
]


async def main() -> None:
    import psycopg

    dsn = os.getenv("POSTGRES_DSN") or ""
    if not dsn:
        from app.infra.config import POSTGRES_DSN as configured
        dsn = configured
    print("dsn host:", dsn.split("@")[-1] if "@" in dsn else dsn[:40])
    async with await psycopg.AsyncConnection.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        for title, sql in QUERIES:
            print(f"\n== {title} ==")
            try:
                cur = await conn.execute(sql)
                rows = await cur.fetchall()
                if not rows:
                    print("(no rows)")
                for row in rows:
                    print({k: (str(v)[:19] if k.endswith("_at") else v) for k, v in row.items()})
            except Exception as exc:
                print("query skipped:", type(exc).__name__, str(exc)[:120])
                await conn.rollback()


if __name__ == "__main__":
    asyncio.run(main())
