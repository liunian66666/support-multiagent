"""Bounded, tenant-scoped Agent decision records in the existing PostgreSQL DB."""

from psycopg.types.json import Jsonb


async def ensure_trace_table(conn):
    async with conn.cursor() as cur:
        await cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_trace_runs (
                trace_id varchar(64) PRIMARY KEY,
                tenant_id varchar(64) NOT NULL,
                thread_id varchar(255) NOT NULL,
                route varchar(32) NOT NULL DEFAULT '',
                specialist varchar(32) NOT NULL DEFAULT '',
                status varchar(24) NOT NULL,
                duration_ms integer NOT NULL DEFAULT 0,
                events jsonb NOT NULL DEFAULT '[]'::jsonb,
                created_at timestamptz NOT NULL DEFAULT now(),
                finished_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_trace_tenant_time
            ON agent_trace_runs (tenant_id, created_at DESC)
        """)


async def save_trace_run(app, run):
    async with app.state.db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO agent_trace_runs
                    (trace_id, tenant_id, thread_id, route, specialist, status,
                     duration_ms, events, created_at, finished_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, to_timestamp(%s), now())
                ON CONFLICT (trace_id) DO NOTHING
            """, (run.trace_id, run.tenant_id, run.thread_id, run.route,
                  run.specialist, run.status, run.duration_ms,
                  Jsonb(run.events), run.started_at))


async def get_trace_run(app, tenant_id: str, trace_id: str):
    async with app.state.db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT trace_id, tenant_id, thread_id, route, specialist,
                       status, duration_ms, events, created_at, finished_at
                FROM agent_trace_runs WHERE trace_id = %s AND tenant_id = %s
            """, (trace_id, tenant_id))
            row = await cur.fetchone()
    if row is None:
        return None
    return dict(zip(("trace_id", "tenant_id", "thread_id", "route",
                     "specialist", "status", "duration_ms", "events",
                     "created_at", "finished_at"), row))
