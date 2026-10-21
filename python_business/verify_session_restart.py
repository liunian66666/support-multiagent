"""Two-phase demo session check around a Python business container restart.

Run `prepare`, restart only `business`, then run `check` from the compose network.
The temporary Redis record is deleted after verification; no token is printed.
"""

import asyncio
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

import redis
import websockets


BASE = os.getenv("GATEWAY_URL", "http://nginx")
KEY = "cs:acceptance:restart-session"


def api(path, *, token="", payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(BASE + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=75) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


async def history(token):
    address = BASE.replace("http://", "ws://").replace("https://", "wss://")
    address += "/ws/chat?token=" + urllib.parse.quote(token, safe="")
    async with websockets.connect(address, open_timeout=10) as socket:
        frame = json.loads(await asyncio.wait_for(socket.recv(), 10))
        if frame.get("type") != "history":
            raise RuntimeError("历史消息帧缺失")
        return frame["messages"]


def main(phase):
    store = redis.Redis.from_url(os.environ["REDIS_URL"])
    if phase == "prepare":
        status, body = api("/api/auth/guest-token", payload={})
        if status != 200 or body.get("code") != 0:
            raise RuntimeError("无法创建验收访客会话")
        token = body["data"]["token"]
        marker = "会话重启验收 " + secrets.token_hex(4)
        api("/api/user/chat", token=token, payload={"msg": marker})
        messages = asyncio.run(history(token))
        if not any(item.get("content") == marker for item in messages):
            raise RuntimeError("重启前消息未持久化")
        store.set(KEY, json.dumps({"token": token, "marker": marker}), ex=900)
        print("restart prepare: persisted message and token session verified")
        return 0
    if phase == "check":
        record = store.get(KEY)
        if not record:
            raise RuntimeError("待验收会话已过期")
        saved = json.loads(record)
        messages = asyncio.run(history(saved["token"]))
        ok = any(item.get("content") == saved["marker"] for item in messages)
        if ok:
            store.delete(KEY)
        print(f"restart check: {'passed' if ok else 'FAILED'}")
        return 0 if ok else 1
    raise ValueError("phase must be prepare or check")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
