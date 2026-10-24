"""[ours] S5 策略版本：发布/回滚端点契约 + 服务层参数校验（无 PG，全 mock）。"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api.experience import (active_strategy, create_strategy,
                                publish_strategy, rollback_strategy)
from app.infra.db.experience import create_strategy_version

TOKEN = "tok"


def req(body, tenant="t_shop", token=TOKEN, method="json"):
    return SimpleNamespace(
        headers={"X-Internal-Token": token, "X-Tenant-Id": tenant},
        app=SimpleNamespace(),
        json=AsyncMock(return_value=body))


class ServiceValidationTest(unittest.TestCase):
    """create_strategy_version 的参数校验在触库前应拒绝。"""

    def test_bad_recall_mode_rejected(self):
        with self.assertRaises(ValueError):
            _call(create_strategy_version(None, "t", {"recall_mode": "yolo"}))

    def test_bad_eval_status_rejected(self):
        with self.assertRaises(ValueError):
            _call(create_strategy_version(None, "t", {"recall_mode": "inject"},
                                          eval_status="ghost"))

    def test_params_must_be_dict(self):
        with self.assertRaises(ValueError):
            _call(create_strategy_version(None, "t", "not-a-dict"))


def _call(coro):
    """同步驱动协程：校验发生在取连接之前，不会真正 await 到 DB。"""
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    coro.close()
    raise AssertionError("协程在校验前挂起（不应发生）")


class StrategyApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"INTERNAL_API_TOKEN": TOKEN})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_endpoints_require_token(self):
        cases = (create_strategy, publish_strategy, rollback_strategy, active_strategy)
        for fn in cases:
            with self.subTest(fn=fn.__name__):
                with self.assertRaises(HTTPException) as ctx:
                    await fn(req({"version_id": 1}, token="wrong"))
                self.assertEqual(ctx.exception.status_code, 403)

    async def test_create_delegates_and_validates(self):
        with self.assertRaises(HTTPException) as ctx:
            await create_strategy(req({"params": "bad"}))
        self.assertEqual(ctx.exception.status_code, 400)

        async def fake_create(app, tenant_id, params, parent_version=None,
                              eval_status="candidate"):
            return 42
        with patch("app.api.experience.create_strategy_version", fake_create):
            out = await create_strategy(req({"params": {"recall_mode": "shadow"}}))
        self.assertEqual(out["data"], {"version_id": 42})

    async def test_create_maps_value_error_to_400(self):
        async def bad(app, tenant_id, params, parent_version=None,
                      eval_status="candidate"):
            raise ValueError("recall_mode 非法")
        with patch("app.api.experience.create_strategy_version", bad):
            with self.assertRaises(HTTPException) as ctx:
                await create_strategy(req({"params": {"recall_mode": "yolo"}}))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_publish_transition_contract(self):
        calls = []

        async def fake_publish(app, tenant_id, version_id, actor=""):
            calls.append((tenant_id, version_id, actor))
            return {"id": version_id, "params": {}}
        with patch("app.api.experience.publish_strategy_version", fake_publish):
            out = await publish_strategy(req({"version_id": 7, "actor": "ops"}))
        self.assertEqual(out["data"], {"version_id": 7,
                                       "eval_status": "published"})
        self.assertEqual(calls, [("t_shop", 7, "ops")])

    async def test_publish_unknown_version_404(self):
        async def none(app, tenant_id, version_id, actor=""):
            return None
        with patch("app.api.experience.publish_strategy_version", none):
            with self.assertRaises(HTTPException) as ctx:
                await publish_strategy(req({"version_id": 999}))
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_rollback_uses_rollback_fn(self):
        async def fake_rb(app, tenant_id, version_id, actor=""):
            return {"id": version_id, "params": {}}
        with patch("app.api.experience.rollback_strategy_version", fake_rb):
            out = await rollback_strategy(req({"version_id": 3}))
        self.assertEqual(out["data"]["version_id"], 3)

    async def test_version_id_type_enforced(self):
        for body in ({}, {"version_id": "3"}, {"version_id": None}):
            with self.subTest(body=body):
                with self.assertRaises(HTTPException) as ctx:
                    await publish_strategy(req(body))
                self.assertEqual(ctx.exception.status_code, 400)

    async def test_active_returns_current_strategy(self):
        strategy = {"id": 5, "params": {"recall_mode": "inject"},
                    "recall_mode": "inject"}

        async def fake_active(app, tenant_id):
            return strategy
        with patch("app.api.experience.get_active_strategy", fake_active):
            out = await active_strategy(req({}))
        self.assertEqual(out["data"], strategy)


if __name__ == "__main__":
    unittest.main()
