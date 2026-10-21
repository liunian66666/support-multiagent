"""Single-worker WebSocket hub for the FastAPI cutover.

The Docker service runs one Uvicorn worker. If workers are increased, live
connection accounting and broadcasts must move to a shared Redis channel.
"""

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass(eq=False)
class Client:
    websocket: object
    tenant_id: str
    thread_id: str = ""
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active: bool = True

    async def send(self, payload):
        if not self.active:
            return
        try:
            async with self.send_lock:
                if self.active:
                    await self.websocket.send_json(payload)
        except Exception:
            self.active = False


class Hub:
    def __init__(self):
        self.users = {}
        self.admins = set()
        self.ip_counts = defaultdict(int)
        self.thread_counts = defaultdict(int)
        self.message_times = defaultdict(list)
        self.busy_threads = set()
        self.loop = None

    def acquire_connection(self, ip, thread_id):
        if self.ip_counts[ip] >= 10:
            return False
        if self.thread_counts[thread_id] >= 3:
            return False
        self.ip_counts[ip] += 1
        self.thread_counts[thread_id] += 1
        return True

    def release_connection(self, ip, thread_id):
        self.ip_counts[ip] = max(0, self.ip_counts[ip] - 1)
        self.thread_counts[thread_id] = max(0, self.thread_counts[thread_id] - 1)
        if not self.ip_counts[ip]:
            self.ip_counts.pop(ip, None)
        if not self.thread_counts[thread_id]:
            self.thread_counts.pop(thread_id, None)

    def allow_message(self, thread_id):
        now = time.monotonic()
        recent = [value for value in self.message_times[thread_id]
                  if now - value < 1]
        if len(recent) >= 5:
            self.message_times[thread_id] = recent
            return False
        recent.append(now)
        self.message_times[thread_id] = recent
        return True

    def try_lock_thread(self, thread_id):
        if thread_id in self.busy_threads:
            return False
        self.busy_threads.add(thread_id)
        return True

    def unlock_thread(self, thread_id):
        self.busy_threads.discard(thread_id)

    def count_tenant(self, tenant_id, *, excluding=""):
        return sum(client.tenant_id == tenant_id and thread_id != excluding
                   for thread_id, client in self.users.items())

    def count_all_tenants(self):
        counts = defaultdict(int)
        for client in self.users.values():
            counts[client.tenant_id] += 1
        return dict(counts)

    async def register_user(self, websocket, tenant_id, thread_id):
        self.loop = asyncio.get_running_loop()
        old = self.users.get(thread_id)
        if old:
            await old.send({"type": "kicked", "msg": "连接已在其他窗口建立"})
            old.active = False
            try:
                await old.websocket.close()
            except Exception:
                pass
        client = Client(websocket, tenant_id, thread_id)
        self.users[thread_id] = client
        return client

    def unregister_user(self, thread_id, client):
        client.active = False
        if self.users.get(thread_id) is client:
            self.users.pop(thread_id, None)

    def register_admin(self, websocket, tenant_id):
        self.loop = asyncio.get_running_loop()
        client = Client(websocket, tenant_id)
        self.admins.add(client)
        return client

    def unregister_admin(self, client):
        client.active = False
        self.admins.discard(client)

    async def send_user(self, thread_id, payload):
        client = self.users.get(thread_id)
        if client:
            await client.send(payload)

    async def send_admins(self, tenant_id, payload):
        for client in tuple(self.admins):
            if not tenant_id or client.tenant_id == tenant_id:
                await client.send(payload)

    async def broadcast(self, target, payload, *, key="", tenant_id=""):
        if target == "user":
            await self.send_user(key, payload)
        elif target in ("admins", "admins_tenant"):
            await self.send_admins(tenant_id if target == "admins_tenant" else "",
                                   payload)

    def broadcast_from_thread(self, target, payload, *, key="", tenant_id=""):
        if self.loop and self.loop.is_running():
            operation = self.broadcast(target, payload, key=key,
                                       tenant_id=tenant_id)
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if current is self.loop:
                current.create_task(operation)
            else:
                asyncio.run_coroutine_threadsafe(operation, self.loop)
