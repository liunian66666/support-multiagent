"""Prompt CRUD（从 db_ops.py 拆出）。"""
import logging
from typing import Optional

import psycopg

from app.infra.config import DEFAULT_SYSTEM_PROMPT

logger = logging.getLogger("agent.db_ops")

async def load_active_prompt(conn: psycopg.AsyncConnection, tenant_id: str = "") -> str:
    """读取指定租户当前激活的 Prompt；租户无则回退全局（tenant_id=''）；再无则返回默认"""
    async with conn.cursor() as cur:
        if tenant_id:
            await cur.execute(
                "SELECT content FROM prompts WHERE tenant_id = %s AND is_active = true ORDER BY updated_at DESC LIMIT 1",
                (tenant_id,))
            row = await cur.fetchone()
            if row:
                return row[0]
        await cur.execute("SELECT content FROM prompts WHERE tenant_id = '' AND is_active = true ORDER BY updated_at DESC LIMIT 1")
        row = await cur.fetchone()
    return row[0] if row else DEFAULT_SYSTEM_PROMPT


async def seed_default_prompt(conn: psycopg.AsyncConnection):
    """全局 Prompt 为空时写入默认 Prompt 并激活（租户级未设置时回退使用）"""
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM prompts WHERE tenant_id = ''")
        (cnt,) = await cur.fetchone()
        if cnt == 0:
            await cur.execute(
                "INSERT INTO prompts (tenant_id, key, title, content, is_active) VALUES ('', %s, %s, %s, true)",
                ("default", "默认客服Prompt", DEFAULT_SYSTEM_PROMPT),
            )
            logger.info("✅ seeded default prompt")


async def upsert_prompt(conn: psycopg.AsyncConnection, tenant_id: str, key: str, title: str, content: str, is_active: bool):
    async with conn.cursor() as cur:
        await cur.execute("""
            INSERT INTO prompts (tenant_id, key, title, content, is_active, updated_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (tenant_id, key) DO UPDATE SET
                title = EXCLUDED.title,
                content = EXCLUDED.content,
                is_active = EXCLUDED.is_active,
                updated_at = now()
        """, (tenant_id, key, title, content, is_active))
        if is_active:
            await cur.execute("UPDATE prompts SET is_active = false WHERE tenant_id = %s AND key <> %s", (tenant_id, key))


async def activate_prompt(conn: psycopg.AsyncConnection, tenant_id: str, key: str) -> Optional[str]:
    """激活指定租户的 Prompt 并返回其内容；不存在返回 None"""
    async with conn.cursor() as cur:
        await cur.execute("UPDATE prompts SET is_active = false WHERE tenant_id = %s", (tenant_id,))
        await cur.execute(
            "UPDATE prompts SET is_active = true, updated_at = now() WHERE tenant_id = %s AND key = %s",
            (tenant_id, key))
        await cur.execute("SELECT content FROM prompts WHERE tenant_id = %s AND key = %s", (tenant_id, key))
        row = await cur.fetchone()
    return row[0] if row else None


async def delete_prompt(conn: psycopg.AsyncConnection, tenant_id: str, key: str) -> bool:
    """删除指定租户的 Prompt，返回是否删除了激活项（需重建 Agent）"""
    async with conn.cursor() as cur:
        await cur.execute("SELECT is_active FROM prompts WHERE tenant_id = %s AND key = %s", (tenant_id, key))
        row = await cur.fetchone()
        if not row:
            return False
        was_active = bool(row[0])
        await cur.execute("DELETE FROM prompts WHERE tenant_id = %s AND key = %s", (tenant_id, key))
    return was_active
