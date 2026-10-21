"""Live guest-to-human handoff through Nginx, with tenant-scoped admin notice."""

import asyncio
import json
import os
import urllib.parse
import urllib.request

import websockets

import main


BASE = "http://nginx"


def login(path, payload):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.load(response)
    assert data.get("code") == 0, path
    return data["data"]["token"]


async def main_check():
    customer = login("/api/auth/guest-token", {})
    admin = login("/api/auth/token", {
        "app_key": "demo_key",
        "app_secret": os.environ["TENANT_T_DEMO_SECRET"],
    })
    other_admin = login("/api/auth/token", {
        "app_key": "admin_key",
        "app_secret": os.environ["TENANT_T_ADMIN_SECRET"],
    })
    admin_url = "ws://nginx/ws/admin?token=" + urllib.parse.quote(admin, safe="")
    other_url = "ws://nginx/ws/admin?token=" + urllib.parse.quote(other_admin, safe="")
    customer_url = "ws://nginx/ws/chat?token=" + urllib.parse.quote(customer, safe="")
    async with websockets.connect(admin_url) as admin_socket:
        async with websockets.connect(other_url) as other_socket:
            async with websockets.connect(customer_url) as customer_socket:
                history = json.loads(await asyncio.wait_for(customer_socket.recv(), 10))
                assert history.get("type") == "history"
                await customer_socket.send(json.dumps(
                    {"type": "chat", "msg": "转人工客服"}, ensure_ascii=False))
                ticket_frame = None
                for _ in range(30):
                    frame = json.loads(await asyncio.wait_for(customer_socket.recv(), 30))
                    if frame.get("type") == "ticket":
                        ticket_frame = frame
                        break
                    assert frame.get("type") not in ("error", "quota"), frame.get("type")
                assert ticket_frame and ticket_frame.get("ticket_no"), "no handoff ticket"
                notice = json.loads(await asyncio.wait_for(admin_socket.recv(), 10))
                assert notice.get("type") == "new_ticket", notice.get("type")
                assert notice["ticket"]["ticket_no"] == ticket_frame["ticket_no"]
                try:
                    await asyncio.wait_for(other_socket.recv(), 1)
                    raise AssertionError("ticket leaked to a different tenant")
                except asyncio.TimeoutError:
                    pass
                row = main.one("SELECT tenant_id, thread_id FROM tickets "
                               "WHERE ticket_no = %s LIMIT 1",
                               (ticket_frame["ticket_no"],))
                assert row and row["tenant_id"] == "t_demo"
                assert row["thread_id"].startswith("t_demo:")
                request = urllib.request.Request(
                    BASE + "/api/admin/tickets/" + str(notice["ticket"]["id"]),
                    headers={"Authorization": "Bearer " + admin},
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    detail = json.load(response)
                assert detail.get("code") == 0
                assert detail["data"]["ticket_no"] == ticket_frame["ticket_no"]
                print("customer handoff, scoped notice and merchant ticket API: passed")


if __name__ == "__main__":
    asyncio.run(main_check())
