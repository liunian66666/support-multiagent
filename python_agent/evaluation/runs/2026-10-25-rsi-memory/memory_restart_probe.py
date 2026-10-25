"""Two-phase local-only check that conversation context survives Agent restart."""
import json
import os
import sys
import urllib.request

BASE = "http://nginx"
SECRET_PATH = "/tmp/memory_restart_token.json"


def post(path, body, token=""):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


if sys.argv[1] == "prepare":
    login = post("/api/auth/customer-token", {
        "app_key": os.getenv("DEMO_CUSTOMER_APP_KEY", "demo_buyer_key"),
        "app_secret": os.environ["DEMO_CUSTOMER_DEMO_BUYER_KEY_SECRET"],
        "customer_id": "buyer1",
    })["data"]
    result = post("/api/user/chat", {"msg": "请查询订单ORD-2024-001的状态"}, login["token"])
    with open(SECRET_PATH, "w", encoding="utf-8") as output:
        json.dump({"token": login["token"], "thread_id": login["thread_id"]}, output)
    os.chmod(SECRET_PATH, 0o600)
    print(json.dumps({"phase": "prepare", "thread_id": login["thread_id"],
                      "answer": result.get("msg"), "trace_id": result.get("trace_id")}, ensure_ascii=False))
elif sys.argv[1] == "verify":
    with open(SECRET_PATH, encoding="utf-8") as source:
        session = json.load(source)
    try:
        result = post("/api/user/chat", {"msg": "刚才那个订单现在是什么状态？"}, session["token"])
        print(json.dumps({"phase": "verify", "thread_id": session["thread_id"],
                          "answer": result.get("msg"), "trace_id": result.get("trace_id")}, ensure_ascii=False))
    finally:
        os.unlink(SECRET_PATH)
else:
    raise ValueError("phase must be prepare or verify")
