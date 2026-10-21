"""Live, non-mutating customer chat acceptance (apart from demo messages/ticket)."""

import json
import os
import sys
import urllib.error
import urllib.request


GATEWAY = os.getenv("GATEWAY_URL", "http://nginx")
AGENT = os.getenv("AGENT_URL", "http://agent:8000")


def request(base, path, *, payload=None, token="", internal_tenant=""):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if internal_tenant:
        headers["X-Internal-Token"] = os.environ["INTERNAL_API_TOKEN"]
        headers["X-Tenant-Id"] = internal_tenant
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def customer_token(customer_id):
    status, body = request(GATEWAY, "/api/auth/customer-token", payload={
        "app_key": os.getenv("DEMO_CUSTOMER_APP_KEY", "demo_buyer_key"),
        "app_secret": os.environ["DEMO_CUSTOMER_DEMO_BUYER_KEY_SECRET"],
        "customer_id": customer_id,
    })
    assert status == 200 and body.get("code") == 0, "demo buyer login failed"
    return body["data"]["token"]


def chat(token, question, expected_role):
    status, body = request(GATEWAY, "/api/user/chat", token=token,
                           payload={"msg": question})
    assert status == 200 and body.get("code") == 0, "customer chat failed"
    trace_id = body.get("trace_id")
    assert trace_id, "missing customer-visible trace id"
    status, detail = request(AGENT, "/agent/traces/" + trace_id,
                             internal_tenant="t_demo")
    assert status == 200, f"trace lookup failed: {status}"
    run = detail["data"]
    assert run["specialist"] == expected_role, (run["specialist"], expected_role)
    assert run["status"] in ("completed", "interrupted")
    assert any(event["stage"] == "route" for event in run["events"])
    assert any(event["name"] == "finished" for event in run["events"])
    other_status, _ = request(AGENT, "/agent/traces/" + trace_id,
                              internal_tenant="t_admin")
    assert other_status == 404, "trace leaked across tenants"
    return body, run


def main():
    buyer1 = customer_token("buyer1")
    own, own_trace = chat(buyer1, "查询订单 ORD-2024-001 的状态", "order")
    assert "ORD-2024-001" in own["msg"]
    assert "订单不属于" not in own["msg"]
    print("owned order and tenant-scoped trace: passed")

    compound, compound_trace = chat(
        buyer1, "查订单 ORD-2024-001 状态并结合退货政策说明下一步", "composer")
    assert compound["msg"] and compound_trace["route"] == "order"
    assert any(event["name"] == "parallel_reads"
               for event in compound_trace["events"])
    assert any(event["stage"] == "evidence"
               for event in compound_trace["events"])
    print("compound order + tenant knowledge with trace: passed")

    status, guest = request(GATEWAY, "/api/auth/guest-token", payload={})
    assert status == 200 and guest.get("code") == 0
    guest_token = guest["data"]["token"]
    answer, faq_trace = chat(guest_token, "满99元包邮吗？", "faq")
    assert answer["msg"] and faq_trace["status"] == "completed"
    assert any(event["stage"] == "evidence" for event in faq_trace["events"])
    print("guest FAQ and tenant-scoped trace: passed")

    action, action_trace = chat(
        guest_token, "请给订单 ORD-2024-001 退款并解释政策", "case")
    assert action.get("is_human_wait") and action.get("ticket_no")
    assert action_trace["route"] == "handoff"
    print("refund action held for human ticket: passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
