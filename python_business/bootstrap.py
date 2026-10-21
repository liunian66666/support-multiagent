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


def main():
    with database() as conn:
        try:
            seeded = seed_empty_database(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    print("business tenant bootstrap: seeded" if seeded else
          "business tenant bootstrap: existing tenants preserved")


if __name__ == "__main__":
    main()
