"""Collect demo-only customer replies through the real gateway.

Run in the business container, where demo login settings are available. Tokens
and secrets are never written to the report. Creates demo sessions/messages.
"""

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path


def post(path, payload, token=""):
    base = os.getenv("GATEWAY_URL", "http://nginx")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(base + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.status, json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = [json.loads(line) for line in args.cases.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    rows = []
    for case in cases:
        identity = case.get("customer", "guest")
        # Independent gold cases must not share dialog history or learn from
        # earlier model replies. A multi-turn case needs its own explicit runner.
        if identity == "guest":
            status, body = post("/api/auth/guest-token", {})
        elif identity == "buyer1":
            status, body = post("/api/auth/customer-token", {
                "app_key": os.getenv("DEMO_CUSTOMER_APP_KEY", "demo_buyer_key"),
                "app_secret": os.environ["DEMO_CUSTOMER_DEMO_BUYER_KEY_SECRET"],
                "customer_id": "buyer1"})
        else:
            raise ValueError("only demo guest and buyer1 are supported")
        if status != 200 or body.get("code") != 0:
            raise RuntimeError(f"demo login failed: {identity}")
        token = body["data"]["token"]
        started = time.perf_counter()
        status, body = post("/api/user/chat", {"msg": case["query"]}, token)
        rows.append({"id": case["id"], "http_status": status, "code": body.get("code"),
                     "answer": body.get("msg", ""), "trace_id": body.get("trace_id", ""),
                     "latency_ms": round((time.perf_counter() - started) * 1000, 2)})
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                           encoding="utf-8")


if __name__ == "__main__":
    main()
