"""Run a temporary t_demo RSI A/B test with the supplied OpenAI endpoint.

Run inside cs_business. The prior model config is kept inside the container and
restored in finally; no key or customer token is written to evaluation outputs.
"""
import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from business.admin.ops import encrypt_api_key
from business.infra.storage import db

TENANT = "t_demo"
BASE = "http://nginx"
AGENT = os.getenv("AGENT_URL", "http://agent:8000")
QWEN_BASE = "http://20.222.143.1:1030/v1"
BACKUP = Path("/tmp/t_demo_model_before_qwen_ablation.json")
MODEL_COLUMNS = ("model_provider", "llm_provider_id", "model_provider_key",
                 "model_name", "model_api_base", "model_api_key")
VERSIONS = {"off": 2, "inject": 3}


def api(path, body=None, token="", *, agent=False):
    headers = {"Content-Type": "application/json"}
    if agent:
        headers.update({"X-Internal-Token": os.environ["INTERNAL_API_TOKEN"],
                        "X-Tenant-Id": TENANT})
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        (AGENT if agent else BASE) + path,
        data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=90) as response:
        return response.status, json.load(response)


def clear_model_cache():
    status, body = api("/agent/cache/clear?tenant_id=t_demo&scope=chat", {}, agent=True)
    if status != 200 or body.get("code") != 0:
        raise RuntimeError("Agent chat model cache invalidation failed")


def switch_model():
    if BACKUP.exists():
        raise RuntimeError("existing model backup found; restore it before a new run")
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT " + ", ".join(MODEL_COLUMNS) +
                    " FROM tenants WHERE tenant_id=%s AND deleted_at=0", (TENANT,))
        row = cur.fetchone()
        if not row:
            raise RuntimeError("demo tenant missing")
        backup = {column: row[column] for column in MODEL_COLUMNS}
        with BACKUP.open("x", encoding="utf-8") as out:
            json.dump(backup, out, ensure_ascii=False)
        BACKUP.chmod(0o600)
        cur.execute(
            "UPDATE tenants SET model_provider=%s, llm_provider_id=%s, "
            "model_provider_key=%s, model_name=%s, model_api_base=%s, model_api_key=%s "
            "WHERE tenant_id=%s AND deleted_at=0",
            ("custom", 0, "custom", "qwen", QWEN_BASE,
             encrypt_api_key("eval-probe"), TENANT),
        )
        if cur.rowcount != 1:
            raise RuntimeError("demo model update failed")
    clear_model_cache()
    print("model switched to qwen endpoint", flush=True)


def restore_model():
    if not BACKUP.exists():
        return
    backup = json.loads(BACKUP.read_text(encoding="utf-8"))
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE tenants SET " +
                    ", ".join(column + "=%s" for column in MODEL_COLUMNS) +
                    " WHERE tenant_id=%s AND deleted_at=0",
                    tuple(backup[column] for column in MODEL_COLUMNS) + (TENANT,))
        if cur.rowcount != 1:
            raise RuntimeError("demo model restore failed; backup retained")
    clear_model_cache()
    BACKUP.unlink()
    print("original demo model restored", flush=True)


def strategy():
    _, body = api("/agent/experience/strategy/active", agent=True)
    data = body.get("data") or {}
    return data.get("id"), (data.get("params") or {}).get("recall_mode")


def set_strategy(mode):
    version_id = VERSIONS[mode]
    if strategy() == (version_id, mode):
        return
    _, body = api("/agent/experience/strategy/rollback",
                  {"version_id": version_id, "actor": "qwen_eval"}, agent=True)
    if body.get("code") != 0 or strategy() != (version_id, mode):
        raise RuntimeError("strategy transition failed: " + mode)
    print("strategy", mode, flush=True)


def login(identity):
    if identity == "guest":
        _, body = api("/api/auth/guest-token", {})
    elif identity == "buyer1":
        _, body = api("/api/auth/customer-token", {
            "app_key": os.getenv("DEMO_CUSTOMER_APP_KEY", "demo_buyer_key"),
            "app_secret": os.environ["DEMO_CUSTOMER_DEMO_BUYER_KEY_SECRET"],
            "customer_id": "buyer1",
        })
    else:
        raise ValueError("unsupported eval customer")
    if body.get("code") != 0:
        raise RuntimeError("evaluation login failed")
    return body["data"]["token"]


def collect(cases, mode, run_index, directory, pacing):
    path = directory / f"qwen_{mode}_{run_index}.jsonl"
    if path.exists():
        raise RuntimeError("evaluation output already exists: " + str(path))
    with path.open("x", encoding="utf-8") as out:
        for number, case in enumerate(cases, 1):
            if number > 1:
                time.sleep(pacing)
            token = login(case.get("customer", "guest"))
            started = time.perf_counter()
            try:
                status, body = api("/api/user/chat", {"msg": case["query"]}, token)
            except urllib.error.HTTPError as error:
                status, body = error.code, {"code": error.code, "msg": ""}
            row = {"id": case["id"], "http_status": status, "code": body.get("code"),
                   "answer": body.get("msg", ""), "trace_id": body.get("trace_id", ""),
                   "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            if number % 5 == 0 or number == len(cases):
                print(mode, run_index, number, "/", len(cases), "http", status, flush=True)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gold", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--modes", default="off,inject,off,inject")
    parser.add_argument("--pacing", type=float, default=6.2)
    args = parser.parse_args()
    modes = args.modes.split(",")
    if not modes or any(mode not in VERSIONS for mode in modes):
        parser.error("modes must be off/inject")
    cases = [json.loads(line) for line in args.gold.read_text(encoding="utf-8").splitlines()
             if line.strip()][:args.limit]
    if not cases:
        parser.error("gold file is empty")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    original_strategy = strategy()
    if original_strategy != (2, "off"):
        raise RuntimeError("expected initial t_demo strategy v2/off")
    metadata = {"started_at": datetime.now(timezone.utc).isoformat(),
                "requested_model": "qwen", "endpoint_base": QWEN_BASE,
                "initial_strategy": {"id": original_strategy[0], "mode": original_strategy[1]},
                "cases": len(cases), "modes": modes, "pacing_seconds": args.pacing}
    (args.output_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        switch_model()
        counters = {"off": 0, "inject": 0}
        for mode in modes:
            set_strategy(mode)
            counters[mode] += 1
            collect(cases, mode, counters[mode], args.output_dir, args.pacing)
    finally:
        try:
            set_strategy("off")
        finally:
            restore_model()


if __name__ == "__main__":
    main()
