"""Initialize the two demo tenants only when a new business database is empty.

MySQL's init.sql creates tables and public metadata. Tenant secrets are supplied
at runtime and only their SHA-256 hashes are stored. Existing tenants are never
changed, so restarting the business service cannot reset merchant settings.
"""

import hashlib
import os

import pymysql


SEED_TENANTS = (
    ("t_admin", "平台管理员", "admin_key", True, "TENANT_T_ADMIN_SECRET"),
    ("t_demo", "洛奇之路（演示）", "demo_key", False, "TENANT_T_DEMO_SECRET"),
)


def database():
    return pymysql.connect(
        host=os.environ["MYSQL_HOST"], port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"], password=os.environ["MYSQL_PASSWORD"],
        database=os.environ["MYSQL_DATABASE"], charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, autocommit=False,
        connect_timeout=5,
    )


def seed_empty_database(conn):
    with conn.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM tenants")
        if int(cursor.fetchone()["n"]) > 0:
            return False

        secrets = []
        for tenant_id, name, app_key, platform, variable in SEED_TENANTS:
            secret = os.getenv(variable, "")
            if not secret or secret == "CHANGE_ME":
                raise RuntimeError(f"{variable} must be configured before first start")
            secrets.append((tenant_id, name, app_key, int(platform),
                            hashlib.sha256(secret.encode()).hexdigest()))

        for tenant_id, name, app_key, platform, secret_hash in secrets:
            cursor.execute(
                "INSERT INTO tenants (tenant_id, name, app_key, app_secret_hash, "
                "is_platform_admin, status, rbac_seeded, model_provider, "
                "embedding_provider, created_at, deleted_at) "
                "VALUES (%s, %s, %s, %s, %s, 1, 0, 'platform', 'platform', "
                "NOW(3), 0)",
                (tenant_id, name, app_key, secret_hash, platform),
            )
    return True


def ensure_experience_menu(conn):
    """Add the tenant self-service page to existing databases without resetting roles."""
    entries = (
        (46, 0, "租户管理", 1, "", "", None, 7),
        (47, 46, "可复用经验", 2, "/experience", "views/Experience.vue", "experience:view", 1),
        (48, 47, "维护经验", 3, "", "", "experience:edit", 1),
    )
    with conn.cursor() as cursor:
        for menu_id, parent, name, kind, path, component, perm, sort in entries:
            cursor.execute(
                "INSERT IGNORE INTO menus (id,parent_id,group_name,name,type,path,component,"
                "perm,icon,sort,visible,status,is_builtin,created_at,updated_at) "
                "VALUES (%s,%s,'业务中心',%s,%s,%s,%s,%s,'',%s,1,1,1,NOW(),NOW())",
                (menu_id, parent, name, kind, path, component, perm, sort))
        cursor.execute("""
            INSERT IGNORE INTO sys_role_menu (role_id,menu_id)
            SELECT r.id,m.id FROM roles r JOIN menus m ON m.id IN (46,47,48)
            WHERE r.code = 'admin' AND r.deleted_at = 0
              AND m.group_name = '业务中心'
        """)


def main():
    with database() as conn:
        try:
            seeded = seed_empty_database(conn)
            ensure_experience_menu(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    print("business tenant bootstrap: seeded" if seeded else
          "business tenant bootstrap: existing tenants preserved")


if __name__ == "__main__":
    main()
