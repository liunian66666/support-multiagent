"""Configure only t_demo's chat model, keeping an ignored local rollback file.

Run inside cs_business with PYTHONPATH=/app. Embeddings and other tenants are
unchanged. The old encrypted key stays in /app/data (host bind, Git-ignored).
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

from business.admin.ops import encrypt_api_key, tenant_config_row
from business.infra.storage import db

TENANT = "t_demo"
BASE = "http://20.222.143.1:1030/v1"
BACKUP = Path("/app/data/model-backups/t_demo_before_qwen_20261025.json")
COLUMNS = ("model_provider", "llm_provider_id", "model_provider_key",
           "model_name", "model_api_base", "model_api_key",
           "chat_input_cache_price", "chat_input_price", "chat_output_price")


def invalidate():
    req = urllib.request.Request(
        "http://agent:8000/agent/cache/clear?tenant_id=t_demo&scope=chat",
        data=b"{}", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        data = json.load(response)
    if data.get("code") != 0:
        raise RuntimeError("Agent cache clear failed")


def update(values):
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE tenants SET " + ", ".join(c + "=%s" for c in COLUMNS) +
                    " WHERE tenant_id=%s AND deleted_at=0",
                    tuple(values[c] for c in COLUMNS) + (TENANT,))
        if cur.rowcount != 1:
            raise RuntimeError("t_demo model update failed")
    invalidate()


def status():
    row = tenant_config_row(TENANT)
    print(json.dumps({"tenant": TENANT, "model": row["model_name"],
                      "base": row["model_api_base"],
                      "key_configured": bool(row["model_api_key"]),
                      "prices": [str(row[c]) for c in COLUMNS[-3:]],
                      "rollback_backup": BACKUP.exists()}, ensure_ascii=False))


def switch():
    if BACKUP.exists():
        raise RuntimeError("rollback backup already exists; inspect status first")
    row = tenant_config_row(TENANT)
    old = {c: row[c] for c in COLUMNS}
    BACKUP.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with BACKUP.open("x", encoding="utf-8") as output:
        json.dump(old, output, ensure_ascii=False, default=str)
    BACKUP.chmod(0o600)
    new = {**old, "model_provider": "custom", "llm_provider_id": 0,
           "model_provider_key": "custom", "model_name": "qwen",
           "model_api_base": BASE, "model_api_key": encrypt_api_key("eval-probe"),
           "chat_input_cache_price": 0, "chat_input_price": 0,
           "chat_output_price": 0}
    update(new)
    status()


def restore():
    old = json.loads(BACKUP.read_text(encoding="utf-8"))
    update(old)
    BACKUP.unlink()
    status()


if __name__ == "__main__":
    command = sys.argv[1]
    if command == "status":
        status()
    elif command == "switch":
        switch()
    elif command == "restore":
        restore()
    else:
        raise SystemExit("usage: configure_demo_qwen.py status|switch|restore")
