"""Exercise the new FastAPI chat socket with a disposable demo guest session.

This intentionally saves one demo conversation. Tokens and answer text are not
printed. Run from the compose network before changing Nginx routing.
"""

import asyncio
import json
import os
import sys
import urllib.parse
import urllib.request

import websockets


BUSINESS = os.getenv("PYTHON_BUSINESS_API", "http://business:8001")
CHAT_BACKEND = os.getenv("CHAT_BACKEND", BUSINESS)


def guest_token():
    request = urllib.request.Request(
        BUSINESS + "/api/auth/guest-token", data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
    if payload.get("code") != 0:
        raise RuntimeError("演示访客令牌签发失败")
    return payload["data"]["token"]


async def main():
    token = guest_token()
    address = CHAT_BACKEND.replace("http://", "ws://").replace("https://", "wss://")
    address += "/ws/chat?token=" + urllib.parse.quote(token, safe="")
    async with websockets.connect(address, open_timeout=10) as socket:
        history = json.loads(await asyncio.wait_for(socket.recv(), 10))
        if history.get("type") != "history":
            raise RuntimeError("连接后未收到历史消息帧")
        await socket.send(json.dumps({"type": "ping"}))
        pong = json.loads(await asyncio.wait_for(socket.recv(), 10))
        if pong.get("type") != "pong":
            raise RuntimeError("心跳响应异常")
        if os.getenv("CHECK_CHAT", "true").lower() == "false":
            print("guest socket: history and heartbeat passed")
            return 0
        await socket.send(json.dumps({"type": "chat", "msg": "满99元包邮吗？"},
                                     ensure_ascii=False))
        chunks = 0
        for _ in range(100):
            event = json.loads(await asyncio.wait_for(socket.recv(), 90))
            if event.get("type") == "chat_chunk":
                chunks += 1
            elif event.get("type") == "chat_end":
                if not event.get("trace_id"):
                    print("guest FAQ: missing trace_id in final WebSocket frame")
                    return 1
                print(f"guest FAQ: chat_end, chunks={chunks}, chars={len(event.get('msg') or '')}")
                return 0
            elif event.get("type") in ("ticket", "error", "quota"):
                message = event.get("msg") or ""
                source = ("business" if message.startswith("客服服务") else
                          "agent" if message.startswith("调用agent") else
                          "stream" if message.startswith("agent流式") else
                          "unknown")
                print(f"guest FAQ: unexpected terminal event={event['type']}, source={source}")
                return 1
        raise RuntimeError("实时回答未正常结束")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
