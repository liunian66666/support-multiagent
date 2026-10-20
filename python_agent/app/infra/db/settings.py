"""platform_settings 读写（从 db_ops.py 拆出）。"""
import psycopg


async def get_platform_setting(conn: psycopg.AsyncConnection, key: str, default: str = "") -> str:
    """读取平台设置，未配置时返回 default"""
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT value FROM platform_settings WHERE key = %s", (key,))
            row = await cur.fetchone()
        return row[0] if row and row[0] else default
    except Exception:
        return default


async def set_platform_setting(conn: psycopg.AsyncConnection, key: str, value: str) -> None:
    """写入/更新平台设置（UPSERT）"""
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO platform_settings (key, value, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, value))
