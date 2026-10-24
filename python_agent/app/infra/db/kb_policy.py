"""Non-destructive KB publication metadata migration."""
async def ensure_kb_policy_schema(conn):
    async with conn.cursor() as cur:
        await cur.execute("""
            ALTER TABLE kb_documents
              ADD COLUMN IF NOT EXISTS policy_state varchar(16) NOT NULL DEFAULT 'unreviewed',
              ADD COLUMN IF NOT EXISTS product_scope varchar(16) NOT NULL DEFAULT 'unknown',
              ADD COLUMN IF NOT EXISTS valid_from timestamptz,
              ADD COLUMN IF NOT EXISTS valid_until timestamptz
        """)
        # Only the two checked-in demo seed documents are designated as demo material.
        await cur.execute("""
            UPDATE kb_documents SET policy_state = 'demo', product_scope = 'all'
            WHERE tenant_id = 't_demo' AND policy_state = 'unreviewed'
              AND ((file_name = 'demo-store-policy-v1.md'
                    AND title = '演示店铺服务政策 v1')
                   OR title = '客服通用流程（演示参考）')
        """)
