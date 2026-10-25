"""Compare an order follow-up with and without prior conversation context."""
import json
import os
import sys
import time
import urllib.request

BASE = os.getenv("GATEWAY_URL", "http://nginx")
AGENT = os.getenv("AGENT_URL", "http://agent:8000")
CASES = [
    ("ORD-2024-001", "刚才那个订单现在是什么状态？"),
    ("ORD-2024-002", "之前那个订单的物流状态呢？"),
    ("ORD-2024-003", "刚才那个订单现在是什么状态？"),
]


def post(path, payload, token=""):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        BASE + path, data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


def login():
    body = post("/api/auth/customer-token", {
        "app_key": os.getenv("DEMO_CUSTOMER_APP_KEY", "demo_buyer_key"),
        "app_secret": os.environ["DEMO_CUSTOMER_DEMO_BUYER_KEY_SECRET"],
        "customer_id": "buyer1",
    })
    if body.get("code") != 0:
        raise RuntimeError("buyer login failed")
    return body["data"]


def chat(identity, question):
    started = time.perf_counter()
    body = post("/api/user/chat", {"msg": question}, identity["token"])
    return {
        "question": question,
        "answer": body.get("msg", ""),
        "code": body.get("code"),
        "trace_id": body.get("trace_id", ""),
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "thread_id": identity["thread_id"],
    }


def trace_route(trace_id):
    if not trace_id:
        return {}
    headers = {
        "X-Internal-Token": os.environ["INTERNAL_API_TOKEN"],
        "X-Tenant-Id": "t_demo",
    }
    req = urllib.request.Request(AGENT + "/agent/traces/" + trace_id, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as response:
        body = json.load(response)
    data = body.get("data") or {}
    events = data.get("events") or []
    decisions = [
        e.get("attributes", {}) for e in events
        if e.get("stage") == "route" and e.get("name") == "decision"
    ]
    return {
        "route": data.get("route"),
        "route_source": decisions[-1].get("source") if decisions else None,
    }


def main(output):
    with open(output, "w", encoding="utf-8") as out:
        for order_id, followup in CASES:
            same = login()
            fresh = login()
            primer = chat(same, "请查询订单" + order_id + "的状态")
            memory = chat(same, followup)
            control = chat(fresh, followup)
            for arm, row in (("primer", primer), ("memory", memory), ("fresh", control)):
                row.update({"case_id": order_id, "arm": arm})
                try:
                    row.update(trace_route(row["trace_id"]))
                except Exception as exc:
                    row["trace_error"] = type(exc).__name__
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
            print(order_id, "primer", primer["code"], "memory", memory["code"], "fresh", control["code"], flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
