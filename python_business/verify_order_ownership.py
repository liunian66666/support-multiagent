"""Demo-only public order ownership check; never prints tokens or order data."""

import json
import os
import sys
import urllib.error
import urllib.request


BASE = os.getenv("GATEWAY_URL", "http://nginx")


def call(path, *, token="", payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(BASE + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def login(customer_id):
    status, body = call("/api/auth/customer-token", payload={
        "app_key": os.getenv("DEMO_CUSTOMER_APP_KEY", "demo_buyer_key"),
        "app_secret": os.environ["DEMO_CUSTOMER_DEMO_BUYER_KEY_SECRET"],
        "customer_id": customer_id,
    })
    if status != 200 or body.get("code") != 0:
        raise RuntimeError("演示买家令牌签发失败")
    return body["data"]["token"]


def main():
    buyer1 = login("buyer1")
    buyer2 = login("buyer2")
    path = "/api/customer/orders/ORD-2024-001"
    own_status, own = call(path, token=buyer1)
    other_status, other = call(path, token=buyer2)
    absent_status, absent = call(
        "/api/customer/orders/ORD-NOT-FOUND", token=buyer2)
    own_ok = own_status == 200 and own.get("data", {}).get("order_id") == "ORD-2024-001"
    isolated = other_status == absent_status and other.get("msg") == absent.get("msg")
    print(f"buyer1 own order: {'passed' if own_ok else 'FAILED'}")
    print(f"buyer2 cross-customer isolation: {'passed' if isolated else 'FAILED'}")
    return 0 if own_ok and isolated else 1


if __name__ == "__main__":
    sys.exit(main())
