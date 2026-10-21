"""Trace events stay bounded, avoid raw content and enforce tenant isolation."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api.trace import agent_trace
from app.services import trace


class TraceRunTest(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_untrusted_id_and_omits_raw_content(self):
        run = trace.new_trace("../bad", "t_demo", "t_demo:guest1")
        self.assertNotEqual(run.trace_id, "../bad")
        token = trace.bind(run)
        try:
            trace.record("tool", "kb", query="private user question",
                         api_key="secret", count=2, evidence_ids=["doc-1"])
        finally:
            trace.reset(token)
        self.assertEqual(run.events[-1]["attributes"],
                         {"count": 2, "evidence_ids": ["doc-1"]})
        app = SimpleNamespace(state=SimpleNamespace(db_pool=object()))
        with patch("app.services.trace.save_trace_run", new_callable=AsyncMock) as save:
            await run.finish(app, "completed")
        save.assert_awaited_once_with(app, run)
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.events[-1]["name"], "finished")

    async def test_event_budget_is_bounded(self):
        run = trace.new_trace("12345678", "t_demo", "t_demo:guest1")
        for i in range(100):
            run.event("tool", "read", index=i)
        self.assertEqual(len(run.events), 64)


class TraceAccessTest(unittest.IsolatedAsyncioTestCase):
    async def test_internal_token_and_tenant_are_required(self):
        request = SimpleNamespace(headers={"X-Tenant-Id": "t_demo"}, app=object())
        with patch("app.api.trace.INTERNAL_API_TOKEN", "secret"):
            with self.assertRaises(HTTPException) as err:
                await agent_trace("12345678", request)
        self.assertEqual(err.exception.status_code, 403)

    async def test_other_tenant_trace_is_not_visible(self):
        request = SimpleNamespace(headers={"X-Tenant-Id": "t_other",
                                           "X-Internal-Token": "secret"}, app=object())
        with patch("app.api.trace.INTERNAL_API_TOKEN", "secret"), \
                patch("app.api.trace.get_trace_run", new_callable=AsyncMock,
                      return_value=None) as read:
            with self.assertRaises(HTTPException) as err:
                await agent_trace("12345678", request)
        self.assertEqual(err.exception.status_code, 404)
        read.assert_awaited_once_with(request.app, "t_other", "12345678")


if __name__ == "__main__":
    unittest.main()
