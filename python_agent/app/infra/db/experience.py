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


async def find_distill_candidates(app, limit: int = 10):
    """[ours] S3 提炼候选：高评分(>=4)反馈 join 当轮 trace 轨迹（answer_id 锚点）。

    只返回同时满足以下条件的轮次：trace 状态 completed、事件里带 kb 证据
    （evidence/kb_retrieval 的 count>0）、尚未提炼过（experience_items 无同
    source_answer_id 条目）。返回列：tenant_id, thread_id, trace_id, answer_id,
    route, specialist, events, score。
    """
    async with app.state.db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT f.tenant_id, f.thread_id, t.trace_id, t.answer_id,
                       t.route, t.specialist, t.events,
                       (f.value->>'score')::int AS score
                FROM experience_feedback f
                JOIN agent_trace_runs t
                  ON t.tenant_id = f.tenant_id AND t.answer_id = f.answer_id
                WHERE f.result_type = 'rating_submitted'
                  AND (f.value->>'score')::int >= 4
                  AND t.status = 'completed'
                  AND t.answer_id <> ''
                  AND EXISTS (
                      SELECT 1 FROM jsonb_array_elements(t.events) e
                      -- search_kb=search_kb 工具事件名（主链路）、kb_retrieval=compound 链路
                      WHERE e->>'stage' = 'evidence'
                        AND e->>'name' IN ('kb_retrieval', 'search_kb')
                        AND COALESCE((e->'attributes'->>'count')::int, 0) > 0)
                  AND NOT EXISTS (
                      SELECT 1 FROM experience_items i
                      WHERE i.tenant_id = t.tenant_id AND i.source_answer_id = t.answer_id)
                ORDER BY f.occurred_at DESC
                LIMIT %s
            """, (limit,))
            rows = await cur.fetchall()
    return [dict(zip(("tenant_id", "thread_id", "trace_id", "answer_id",
                      "route", "specialist", "events", "score"), r)) for r in rows]


async def insert_experience_item(app, item: dict) -> int:
    """[ours] S3 写入候选经验（candidate 态）。

    以 (tenant_id, source_answer_id) 部分唯一索引兜底去重，返回新行 id；
    冲突返回 0（并发/重跑安全）。
    """
    async with app.state.db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO experience_items
                    (tenant_id, source_trace_id, source_answer_id, route,
                     content, applicability, knowledge_version, valid_until,
                     review_status, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'candidate', %s)
                ON CONFLICT (tenant_id, source_answer_id)
                    WHERE source_answer_id <> '' DO NOTHING
                RETURNING id
            """, (item["tenant_id"], item.get("source_trace_id", ""),
                  item.get("source_answer_id", ""), item.get("route", ""),
                  item["content"], Jsonb(item.get("applicability") or {}),
                  item.get("knowledge_version", ""), item.get("valid_until"),
                  float(item.get("confidence") or 0)))
            row = await cur.fetchone()
            return row[0] if row else 0


async def ensure_experience_indexes(conn):
    """[ours] S3 补充索引/约束（幂等）：提炼去重键 + 召回过滤索引。"""
    async with conn.cursor() as cur:
        await cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uni_exp_items_tenant_answer
            ON experience_items (tenant_id, source_answer_id)
            WHERE source_answer_id <> ''
        """)
        await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_exp_items_recall
            ON experience_items (tenant_id, review_status, valid_until)
        """)


async def list_recallable_experiences(app, tenant_id: str, limit: int = 20):
    """[ours] S3 召回池：仅 published 且未过期条目（租户硬隔离在 SQL 层）。"""
    async with app.state.db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT id, route, content, applicability, confidence
                FROM experience_items
                WHERE tenant_id = %s AND review_status = 'published'
                  AND (valid_until IS NULL OR valid_until > now())
                ORDER BY confidence DESC, id DESC
                LIMIT %s
            """, (tenant_id, limit))
            rows = await cur.fetchall()
    return [dict(zip(("id", "route", "content", "applicability", "confidence"), r))
            for r in rows]


async def list_experience_items(app, tenant_id: str, *, page: int = 1, size: int = 20,
                                query: str = "", status: str = "") -> dict:
    """Tenant-scoped management view; never return another tenant's rows."""
    where = "tenant_id = %s"
    args: list = [tenant_id]
    if status:
        where += " AND review_status = %s"
        args.append(status)
    if query:
        where += " AND (content ILIKE %s OR applicability::text ILIKE %s)"
        args.extend([f"%{query}%", f"%{query}%"])
    async with app.state.db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM experience_items WHERE " + where, args)
            total = (await cur.fetchone())[0]
            await cur.execute("""
                SELECT id, route, content, applicability, review_status, confidence,
                       source_answer_id, valid_until, created_at, updated_at
                FROM experience_items WHERE """ + where + " ORDER BY id DESC LIMIT %s OFFSET %s",
                [*args, size, (page - 1) * size])
            rows = await cur.fetchall()
    keys = ("id", "route", "content", "applicability", "review_status", "confidence",
            "source_answer_id", "valid_until", "created_at", "updated_at")
    return {"items": [dict(zip(keys, row)) for row in rows], "total": total}


async def update_experience_item(app, tenant_id: str, experience_id: int, *,
                                 content: str, keywords: list[str], route: str) -> bool:
    """An edited published item leaves the recall pool until reviewed again."""
    async with app.state.db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                UPDATE experience_items
                SET content = %s, route = %s, applicability = %s,
                    review_status = CASE WHEN review_status = 'published'
                                         THEN 'suspended' ELSE review_status END,
                    updated_at = now()
                WHERE id = %s AND tenant_id = %s AND review_status <> 'expired'
                RETURNING id
            """, (content, route, Jsonb({"keywords": keywords}), experience_id, tenant_id))
            return await cur.fetchone() is not None


async def set_experience_status(app, tenant_id: str, experience_id: int,
                                action: str) -> bool:
    """[ours] S3/S4 状态迁移：publish / suspend（candidate→published→suspended）。"""
    target = {"publish": "published", "suspend": "suspended"}.get(action)
    if not target:
        return False
    async with app.state.db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                UPDATE experience_items
                SET review_status = %s, updated_at = now()
                WHERE id = %s AND tenant_id = %s
                  AND review_status <> 'expired'
                RETURNING id
            """, (target, experience_id, tenant_id))
            return await cur.fetchone() is not None


# ---------------------------------------------------------------------------
# [ours] S5 策略版本：候选→发布→回滚（strategy_versions 的读写面）
# ---------------------------------------------------------------------------

_STRATEGY_STATES = ("candidate", "shadowing", "published", "superseded", "rolled_back")
_RECALL_MODES = ("off", "shadow", "inject")
# 已发布策略的进程内缓存（召回热路径每轮都查，30s 收敛窗口足够）
_strategy_cache: dict = {}
_STRATEGY_TTL = 30.0


def _invalidate_strategy_cache(tenant_id: str) -> None:
    _strategy_cache.pop(tenant_id, None)


async def create_strategy_version(app, tenant_id: str, params: dict,
                                  parent_version: int | None = None,
                                  eval_status: str = "candidate") -> int:
    """新建策略版本（默认 candidate，不进召回）。返回新版本 id。"""
    if not isinstance(params, dict):
        raise ValueError("params 必须为对象")
    mode = params.get("recall_mode", "off")
    if mode not in _RECALL_MODES:
        raise ValueError(f"recall_mode 必须是 {'|'.join(_RECALL_MODES)}")
    if eval_status not in _STRATEGY_STATES:
        raise ValueError("非法 eval_status")
    async with app.state.db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO strategy_versions
                    (tenant_id, parent_version, params_snapshot, eval_status)
                VALUES (%s, %s, %s, %s)
                RETURNING id
            """, (tenant_id, parent_version, Jsonb(params), eval_status))
            return (await cur.fetchone())[0]


async def list_strategy_versions(app, tenant_id: str, limit: int = 20):
    async with app.state.db_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT id, parent_version, params_snapshot, eval_status,
                       published_by, published_at, created_at
                FROM strategy_versions
                WHERE tenant_id = %s
                ORDER BY id DESC
                LIMIT %s
            """, (tenant_id, limit))
            rows = await cur.fetchall()
    return [dict(zip(("id", "parent_version", "params", "eval_status",
                      "published_by", "published_at", "created_at"), r))
            for r in rows]


async def get_active_strategy(app, tenant_id: str) -> dict | None:
    """当轮生效策略：published 行（最新一次发布），带 30s TTL 进程缓存。

    召回热路径调用；查询失败返回 None（fail-open 到 env 级开关）。
    """
    import time
    now = time.time()
    cached = _strategy_cache.get(tenant_id)
    if cached and now - cached[0] < _STRATEGY_TTL:
        return cached[1]
    payload = None
    try:
        async with app.state.db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT id, params_snapshot
                    FROM strategy_versions
                    WHERE tenant_id = %s AND eval_status = 'published'
                    ORDER BY published_at DESC NULLS LAST, id DESC
                    LIMIT 1
                """, (tenant_id,))
                row = await cur.fetchone()
        if row:
            params = row[1] if isinstance(row[1], dict) else {}
            payload = {"id": row[0], "params": params,
                       "recall_mode": params.get("recall_mode", "off")}
    except Exception:
        return None  # 不缓存失败结果，下一轮重试
    _strategy_cache[tenant_id] = (now, payload)
    return payload


async def _transition(app, tenant_id: str, target_id: int,
                      allow_from: tuple, actor: str) -> dict | None:
    """单事务状态迁移：目标行 allow_from→published；原 published→superseded。

    返回发布后的行；目标不存在/租户不符/状态不符返回 None。
    """
    async with app.state.db_pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                # One publisher per tenant at a time; two admins cannot leave two active modes.
                await cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                                  ("experience_mode:" + tenant_id,))
                await cur.execute("""
                    SELECT id, eval_status FROM strategy_versions
                    WHERE id = %s AND tenant_id = %s FOR UPDATE
                """, (target_id, tenant_id))
                row = await cur.fetchone()
                if not row or row[1] not in allow_from:
                    return None
                await cur.execute("""
                    UPDATE strategy_versions SET eval_status = 'superseded'
                    WHERE tenant_id = %s AND eval_status = 'published' AND id <> %s
                """, (tenant_id, target_id))
                await cur.execute("""
                    UPDATE strategy_versions
                    SET eval_status = 'published', published_by = %s,
                        published_at = now()
                    WHERE id = %s RETURNING id, params_snapshot
                """, (actor, target_id))
                done = await cur.fetchone()
    _invalidate_strategy_cache(tenant_id)
    return {"id": done[0], "params": done[1]} if done else None


async def publish_strategy_version(app, tenant_id: str, version_id: int,
                                   actor: str = "") -> dict | None:
    """发布候选策略（配对评测达标后人工触发）。只有 candidate 可发布。"""
    return await _transition(app, tenant_id, version_id, ("candidate",), actor)


async def rollback_strategy_version(app, tenant_id: str, version_id: int,
                                    actor: str = "") -> dict | None:
    """回滚：把历史版本（superseded/rolled_back）重新发布，当前发布行转 superseded，
    生效路径与发布同一条 SQL，保证"一键回滚"与发布语义一致。"""
    return await _transition(app, tenant_id, version_id,
                             ("superseded", "rolled_back"), actor)


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
