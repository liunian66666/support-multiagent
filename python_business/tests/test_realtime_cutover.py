"""WebSocket identity, tenant isolation, and browser protocol checks."""

import asyncio
import json
import os
import time
import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import main_compat as main
from patch_business import patch_business
from business.customer import routes as customer_routes
from realtime import Hub


class FakeRedis:
    def exists(self, _key):
        return 0


class RealtimeCutoverTest(unittest.TestCase):
    def setUp(self):
        self.session = {"tenant_id": "t_demo", "customer_id": "t_demo:buyer1",
                        "thread_id": "t_demo:buyer1:session", "token": "session-id",
                        "is_guest": 0, "is_preview": 0}
        self.writes = []

        def one(sql, args=()):
            if "app_secret_hash FROM tenants" in sql:
                return {"tenant_id": "t_demo", "app_secret_hash": "hash"}
            if "FROM customer_sessions" in sql:
                return self.session
            if "max_concurrent_sessions FROM tenants" in sql:
                return {"max_concurrent_sessions": 2}
            if "daily_message_limit FROM tenants" in sql:
                return {"daily_message_limit": 0}
            return None

        self.patches = [
            patch.dict(os.environ, {"INTERNAL_API_TOKEN": "internal-test"}),
            patch_business(main, "one", side_effect=one),
            patch_business(main, "many", return_value=[]),
            patch_business(main, "write", side_effect=lambda sql, args:
                         self.writes.append((sql, args))),
            patch_business(main, "cache", return_value=FakeRedis()),
            patch.object(customer_routes, "one", side_effect=one),
            patch.object(customer_routes, "many", return_value=[]),
            patch.object(customer_routes, "write", side_effect=lambda sql, args:
                         self.writes.append((sql, args))),
            patch.object(customer_routes, "cache", return_value=FakeRedis()),
            patch_business(main, "ws_hub", Hub()),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])

    def customer_token(self, **overrides):
        claims = {"tid": "t_demo", "cid": "t_demo:buyer1", "sess": "session-id",
                  "exp": int(time.time()) + 600, "kind": "customer"}
        claims.update(overrides)
        return main.signed_token(claims, "hash")

    def admin_token(self):
        claims = {"tid": "t_demo", "uid": 0, "exp": int(time.time()) + 600}
        return main.signed_token(claims, "hash")

    def test_customer_socket_uses_server_session_and_reports_live_count(self):
        with TestClient(main.app) as client:
            with client.websocket_connect(
                "/ws/chat?user_id=t_demo:buyer2:forged&token=" + self.customer_token()
            ) as socket:
                self.assertEqual(socket.receive_json(),
                                 {"type": "history", "messages": []})
                socket.send_json({"type": "ping"})
                self.assertEqual(socket.receive_json(), {"type": "pong"})
                socket.send_json({"type": "read"})
                count = client.get(
                    "/api/agent/tenant-concurrency",
                    params={"tenant_id": "t_demo"},
                    headers={"X-Internal-Token": "internal-test"},
                )
                self.assertEqual(count.json()["data"]["concurrent_sessions"], 1)
                self.assertEqual(self.writes[-1][1],
                                 ("t_demo", "t_demo:buyer1:session"))

    def test_forged_customer_claims_cannot_connect(self):
        with TestClient(main.app) as client:
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect(
                    "/ws/chat?token=" + self.customer_token(cid="t_demo:buyer2")
                ) as socket:
                    socket.receive_json()

    def test_admin_broadcast_only_reaches_matching_tenant(self):
        with patch_business(main, "admin_tenant", return_value={
            "tenant_id": "t_demo", "app_secret_hash": "hash",
            "is_platform_admin": 0,
        }):
            with TestClient(main.app) as client:
                with client.websocket_connect(
                    "/ws/admin?token=" + self.admin_token()
                ) as socket:
                    denied = client.post(
                        "/api/agent/ws/broadcast",
                        json={"target": "admins_tenant", "tenant_id": "t_demo",
                              "payload": {"type": "new_ticket"}},
                    )
                    self.assertEqual(denied.status_code, 403)
                    sent = client.post(
                        "/api/agent/ws/broadcast",
                        headers={"X-Internal-Token": "internal-test"},
                        json={"target": "admins_tenant", "tenant_id": "t_demo",
                              "payload": {"type": "new_ticket", "ticket": {"id": 1}}},
                    )
                    self.assertEqual(sent.status_code, 200)
                    self.assertEqual(socket.receive_json()["ticket"]["id"], 1)

    def test_text_chat_stream_keeps_browser_event_contract(self):
        async def fake_stream(_customer, _content, _pending, client):
            await client.send({"type": "chat_chunk", "chunk": "您好"})
            await client.send({"type": "chat_end", "msg": "您好"})

        with patch_business(main, "handling_ticket", return_value=None), \
                patch_business(main, "pending_ticket", return_value=False), \
                patch_business(main, "save_message"), \
                patch_business(main, "stream_agent_reply", side_effect=fake_stream):
            with TestClient(main.app) as client:
                with client.websocket_connect(
                    "/ws/chat?token=" + self.customer_token()
                ) as socket:
                    socket.receive_json()  # history
                    socket.send_json({"type": "chat", "msg": "怎么退货"})
                    self.assertEqual(socket.receive_json(),
                                     {"type": "chat_chunk", "chunk": "您好"})
                    self.assertEqual(socket.receive_json(),
                                     {"type": "chat_end", "msg": "您好"})

    def test_http_chat_uses_signed_thread_for_agent(self):
        class AgentResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"success": True, "content": "退货说明"}

        with patch_business(main, "handling_ticket", return_value=None), \
                patch_business(main, "pending_ticket", return_value=False), \
                patch_business(main, "save_message") as save, \
                patch.object(main.httpx, "post", return_value=AgentResponse()) as agent:
            with TestClient(main.app) as client:
                response = client.post(
                    "/api/user/chat",
                    headers={"Authorization": "Bearer " + self.customer_token()},
                    json={"user_id": "t_demo:buyer2:forged", "msg": "退货政策"},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["msg"], "退货说明")
            self.assertEqual(agent.call_args.kwargs["json"]["thread_id"],
                             "t_demo:buyer1:session")
            self.assertEqual(save.call_args_list[0].args[:2],
                             ("t_demo:buyer1:session", "user"))

    def test_metrics_export_http_and_live_socket_gauges(self):
        with TestClient(main.app) as client:
            client.get("/api/agent/ws/conns")
            with client.websocket_connect(
                "/ws/chat?token=" + self.customer_token()
            ) as socket:
                socket.receive_json()
                response = client.get("/metrics")
                self.assertEqual(response.status_code, 200)
                self.assertIn("cs_active_sessions 1", response.text)
                self.assertIn("cs_websocket_connections 1", response.text)
                self.assertIn("cs_http_requests_total", response.text)

    def test_agent_interrupt_creates_ticket_and_preserves_reason(self):
        events = []

        class Client:
            async def send(self, event):
                events.append(event)

        class StreamResponse:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def raise_for_status(self):
                pass

            async def aiter_lines(self):
                yield "data: " + json.dumps({
                    "type": "interrupt", "data": {
                        "reason": "需要人工核验", "direct_handoff": True}},
                    ensure_ascii=False)

        class Transport:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return StreamResponse()

        ticket = {"id": 17, "ticket_no": "TK-test", "status": 0,
                  "created_at": datetime.now()}
        customer = main.Customer("t_demo", "t_demo:buyer1",
                                 "t_demo:buyer1:session", "session-id", False, False)
        with patch.object(main.httpx, "AsyncClient", Transport), \
                patch_business(main, "ensure_customer_ticket", return_value=(ticket, False)) as create, \
                patch_business(main, "save_message") as save, \
                patch_business(main, "assign_last_user_message") as assign, \
                patch_business(main, "agent_resume") as resume:
            completed = asyncio.run(main.stream_agent_reply(
                customer, "转人工", False, Client()))
        self.assertTrue(completed)
        self.assertEqual(events[-1]["type"], "ticket")
        self.assertEqual(events[-1]["reason"], "需要人工核验")
        self.assertEqual(create.call_args.args[1], "需要人工核验")
        self.assertEqual(save.call_args.args[4], ticket["id"])
        assign.assert_called_once()
        resume.assert_not_called()


class HubLimitsTest(unittest.TestCase):
    def test_connection_and_message_limits(self):
        hub = Hub()
        self.assertTrue(all(hub.acquire_connection("ip", f"thread-{i}")
                            for i in range(10)))
        self.assertFalse(hub.acquire_connection("ip", "eleventh"))
        for i in range(10):
            hub.release_connection("ip", f"thread-{i}")
        self.assertEqual(hub.count_all_tenants(), {})
        self.assertTrue(all(hub.allow_message("thread") for _ in range(5)))
        self.assertFalse(hub.allow_message("thread"))
        self.assertTrue(hub.try_lock_thread("thread"))
        self.assertFalse(hub.try_lock_thread("thread"))
        hub.unlock_thread("thread")
        self.assertTrue(hub.try_lock_thread("thread"))


if __name__ == "__main__":
    unittest.main()
