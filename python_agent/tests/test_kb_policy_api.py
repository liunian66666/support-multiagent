import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.responses import JSONResponse
from app.api.kb import kb_set_policy


class Cursor:
    def __init__(self, existing=("真实政策", "policy.md")):
        self.existing = existing
        self.rowcount = 1
        self.executed = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, sql, params):
        self.executed.append((sql, params))

    async def fetchone(self):
        return self.existing


class Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def req(body, tenant="t_shop"):
    return SimpleNamespace(headers={"X-Tenant-Id": tenant}, app=SimpleNamespace(),
                           json=AsyncMock(return_value=body))


class PolicyApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_fields_rejected(self):
        for body in ({"policy_state": "published", "product_scope": "unknown"},
                     {"policy_state": "demo", "product_scope": "all"},
                     {"policy_state": "invalid", "product_scope": "all"}):
            with self.subTest(body=body):
                result = await kb_set_policy("d1", req(body))
                self.assertIsInstance(result, JSONResponse)
                self.assertEqual(result.status_code, 400)

    async def test_publication_is_tenant_scoped(self):
        cursor = Cursor()
        @asynccontextmanager
        async def pool(_app):
            yield Connection(cursor)
        with patch("app.api.kb._pool_conn", pool), patch("app.api.kb.clear_kb_cache") as clear:
            result = await kb_set_policy("d1", req({
                "policy_state": "published", "product_scope": "physical"}))
        self.assertEqual(result["code"], 0)
        self.assertEqual(cursor.executed[0][1], ("d1", "t_shop"))
        self.assertEqual(cursor.executed[1][1][4:6], ("d1", "t_shop"))
        clear.assert_called_once()

    async def test_demo_seed_cannot_be_relabelled_published(self):
        cursor = Cursor(existing=("演示店铺服务政策 v1", "demo-store-policy-v1.md"))
        @asynccontextmanager
        async def pool(_app):
            yield Connection(cursor)
        with patch("app.api.kb._pool_conn", pool):
            result = await kb_set_policy("d1", req({
                "policy_state": "published", "product_scope": "all"}, "t_demo"))
        self.assertEqual(result.status_code, 400)
        self.assertEqual(len(cursor.executed), 1)


if __name__ == "__main__":
    unittest.main()
