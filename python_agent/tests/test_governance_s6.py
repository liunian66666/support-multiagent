# -*- coding: utf-8 -*-
"""[ours] S6 治理小件单测：图预算 config + checkpoint 探活降频（无 PG）。

收敛后只剩旧图（``/agent/resume``）会跑 LangGraph 的图，所以 ``AGENT_RECURSION_LIMIT``
的注入点跟着搬到 :mod:`app.agent.legacy_approval.approval`；聊天入口不再构造图配置。
"""
import unittest
from unittest.mock import patch

from app.agent.legacy_approval import approval as approval_mod
from app.agent.legacy_approval.approval import legacy_config
from app.infra.checkpoint import SafeAsyncPostgresSaver


class GraphConfigTest(unittest.TestCase):
    def test_default_keeps_langgraph_behavior(self):
        with patch.object(approval_mod, "_RECURSION_LIMIT", 0):
            cfg = legacy_config("t_demo:u1")
        self.assertEqual(cfg, {"configurable": {"thread_id": "t_demo:u1",
                                                "checkpoint_ns": ""}})
        self.assertNotIn("recursion_limit", cfg)  # 0=不注入，沿用框架默认

    def test_explicit_limit_injected(self):
        with patch.object(approval_mod, "_RECURSION_LIMIT", 12):
            cfg = legacy_config("t_demo:u1")
        self.assertEqual(cfg["recursion_limit"], 12)
        self.assertEqual(cfg["configurable"]["thread_id"], "t_demo:u1")


class _NoPingSaver(SafeAsyncPostgresSaver):
    """跳过真 PG：只验证 _ensure_connection 的降频逻辑。"""

    def __init__(self):
        self._last_ping_ts = 0.0
        self._ping_ttl = 5.0
        self.pings = 0
        self.rebuilds = 0

    def _conn_ok(self):
        return True

    async def _ping(self):
        self.pings += 1
        return True

    async def _rebuild(self):
        self.rebuilds += 1


class PingThrottleTest(unittest.IsolatedAsyncioTestCase):
    async def test_within_ttl_skips_ping(self):
        s = _NoPingSaver()
        await s._ensure_connection()  # 首次必探测
        await s._ensure_connection()  # TTL 窗口内跳过
        self.assertEqual(s.pings, 1)
        self.assertEqual(s.rebuilds, 0)

    async def test_ttl_expired_pings_again(self):
        s = _NoPingSaver()
        await s._ensure_connection()
        import time
        s._last_ping_ts = time.monotonic() - 10
        await s._ensure_connection()
        self.assertEqual(s.pings, 2)

    async def test_ttl_zero_keeps_old_behavior(self):
        s = _NoPingSaver()
        s._ping_ttl = 0.0
        await s._ensure_connection()
        await s._ensure_connection()
        self.assertEqual(s.pings, 2)  # 每操作都探测（旧行为）

    async def test_ping_failure_rebuilds(self):
        s = _NoPingSaver()
        s._ping = lambda: _async_false()
        await s._ensure_connection()
        self.assertEqual(s.rebuilds, 1)


async def _async_false():
    return False


if __name__ == "__main__":
    unittest.main()
