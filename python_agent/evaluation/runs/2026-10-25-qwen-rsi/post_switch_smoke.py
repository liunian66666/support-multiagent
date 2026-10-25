"""One read-only guest FAQ through the public gateway after the demo model switch."""
import json
import urllib.request


def post(path, body, token=""):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        "http://nginx" + path,
        json.dumps(body, ensure_ascii=False).encode(),
        headers,
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.status, json.load(response)


login_status, login = post("/api/auth/guest-token", {})
assert login_status == 200 and login.get("code") == 0, "guest login failed"
token = login["data"]["token"]
status, chat = post("/api/user/chat", {"msg": "退货运费由谁承担？"}, token)
answer = chat.get("msg", "")
result = {
    "login_http": login_status,
    "chat_http": status,
    "chat_code": chat.get("code"),
    "trace_id": chat.get("trace_id", ""),
    "answer_nonempty": bool(answer.strip()),
    "answer_excerpt": answer[:240],
}
print(json.dumps(result, ensure_ascii=False))
assert status == 200 and chat.get("code") == 0 and answer.strip(), "chat failed"
