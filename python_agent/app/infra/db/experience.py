"""[ours] S2 经验层：PG 四表 schema 与反馈事件幂等写入。

设计口径（docs/support-multiAgent能力优化设计方案.md §4.7）：
- experience_items        可信经验条目（审核状态机 candidate→review→published→expired）
- experience_feedback     逐轮反馈事件（event_id 幂等、不可覆盖，only-insert）
- strategy_versions       策略版本快照（候选/影子/发布/回滚，S4 使用）
- experience_review_flags 失效/漂移复核标记（政策停用批量挂起，S4 使用）

S2 仅建表 + 打通 experience_feedback 写入链路（经 MySQL feedback_outbox 异步同步）；
items/versions/flags 的读写面分别属 S3（提炼与影子召回）和 S4（发布与回滚）。
"""

from psycopg.types.json import Jsonb


async def ensure_experience_schema(conn):
    """幂等建表：与 ensure_trace_table 同一生命周期挂点，重复启动零副作用。"""
    async with conn.cursor() as cur:
        await cur.execute("""
            CREATE TABLE IF NOT EXISTS experience_items (
                id bigserial PRIMARY KEY,
                tenant_id varchar(64) NOT NULL,
                source_trace_id varchar(64) NOT NULL DEFAULT '',
                source_answer_id varchar(64) NOT NULL DEFAULT '',
                route varchar(32) NOT NULL DEFAULT '',
                content text NOT NULL DEFAULT '',
                applicability jsonb NOT NULL DEFAULT '{}'::jsonb,
                doc_id varchar(64),
                chunk_id varchar(64),
                knowledge_version varchar(64) NOT NULL DEFAULT '',
                valid_until timestamptz,
                review_status varchar(16) NOT NULL DEFAULT 'candidate',
                confidence real NOT NULL DEFAULT 0,
                version integer NOT NULL DEFAULT 1,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_exp_items_tenant_status
            ON experience_items (tenant_id, review_status)
        """)
        await cur.execute("""
            CREATE TABLE IF NOT EXISTS experience_feedback (
                event_id varchar(96) PRIMARY KEY,
                tenant_id varchar(64) NOT NULL,
                thread_id varchar(255) NOT NULL DEFAULT '',
                trace_id varchar(64) NOT NULL DEFAULT '',
                answer_id varchar(64) NOT NULL DEFAULT '',
                experience_id bigint,
                result_type varchar(32) NOT NULL,
                value jsonb NOT NULL DEFAULT '{}'::jsonb,
                source varchar(32) NOT NULL DEFAULT '',
                occurred_at timestamptz NOT NULL DEFAULT now(),
                created_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_exp_feedback_answer
            ON experience_feedback (tenant_id, answer_id)
        """)
        await cur.execute("""
            CREATE TABLE IF NOT EXISTS strategy_versions (
                id bigserial PRIMARY KEY,
                tenant_id varchar(64) NOT NULL,
                parent_version integer,
                params_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
                eval_status varchar(16) NOT NULL DEFAULT 'candidate',
                published_by varchar(64) NOT NULL DEFAULT '',
                published_at timestamptz,
                created_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_strategy_versions_tenant
            ON strategy_versions (tenant_id, eval_status)
        """)
        await cur.execute("""
            CREATE TABLE IF NOT EXISTS experience_review_flags (
                id bigserial PRIMARY KEY,
                experience_id bigint NOT NULL,
                reason varchar(32) NOT NULL DEFAULT '',
                policy_ref varchar(128) NOT NULL DEFAULT '',
                handled_note text NOT NULL DEFAULT '',
                handled_by varchar(64) NOT NULL DEFAULT '',
                created_at timestamptz NOT NULL DEFAULT now(),
                handled_at timestamptz
            )
        """)
        await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_review_flags_exp
            ON experience_review_flags (experience_id)
        """)


async def save_feedback_event(app, event: dict):
    """幂等写入一条反馈事件；event_id 冲突视为已同步（Outbox 重试安全）。

    返回 True=本次新写入，False=此前已存在。
    occurred_ts 为事件在 MySQL 侧的发生时间（unix 秒），缺省用 PG now()。
    """
    occurred_ts = event.get("occurred_ts")
    async with app.state.db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO experience_feedback
                    (event_id, tenant_id, thread_id, trace_id, answer_id,
                     experience_id, result_type, value, source, occurred_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        COALESCE(to_timestamp(%s::double precision), now()))
                ON CONFLICT (event_id) DO NOTHING
            """, (event["event_id"], event["tenant_id"],
                  event.get("thread_id", ""), event.get("trace_id", ""),
                  event.get("answer_id", ""), event.get("experience_id"),
                  event["result_type"], Jsonb(event.get("value") or {}),
                  event.get("source", ""), occurred_ts))
            return cur.rowcount > 0
