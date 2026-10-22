"""Customer identity and owned-order boundary tests for the parallel API."""

import os
import hashlib
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main_compat as main
from patch_business import patch_business
from business.customer import routes as customer_routes


class FakeRedis:
    def __init__(self):
        self.values = {}

    def exists(self, _key):
        return int(_key in self.values)

    def eval(self, _script, _keys, _key):
        return 1

    def set(self, key, value, ex=None):
        self.values[key] = value


class CustomerContractTest(unittest.TestCase):
    def setUp(self):
        self.queries = []
        self.writes = []
        self.redis = FakeRedis()
        self.customer_id = "t_demo:buyer1"
        self.session = {
            "tenant_id": "t_demo", "customer_id": self.customer_id,
            "thread_id": "t_demo:buyer1:session", "token": "session-id",
            "is_guest": 0, "is_preview": 0,
        }

        def one(sql, args=()):
            self.queries.append((sql, args))
            if "FROM tenants" in sql:
                if "WHERE app_key" in sql:
                    return {"tenant_id": "t_demo", "app_secret_hash":
                            hashlib.sha256(b"preview-secret").hexdigest(),
                            "status": 1, "is_platform_admin": 0}
                return {"tenant_id": "t_demo", "app_secret_hash": "hash"}
            if "FROM customer_sessions" in sql:
                return self.session
            if "FROM orders" in sql and args == ("t_demo", "t_demo:buyer1", "ORD-1"):
                return {"order_id": "ORD-1", "item": "课程", "amount": 99,
                        "status": "已发货", "create_date": "2024-01-01"}
            return None

        def many(sql, args=()):
            self.queries.append((sql, args))
            if args == ("t_demo", "t_demo:buyer1"):
                return [{"order_id": "ORD-1", "item": "课程",
                         "status": "已发货", "create_date": "2024-01-01"}]
            return []

        self.patches = (
            patch_business(main, "one", side_effect=one),
            patch_business(main, "many", side_effect=many),
            patch_business(main, "write", side_effect=lambda sql, args: self.writes.append((sql, args))),
            patch_business(main, "cache", return_value=self.redis),
            patch.object(customer_routes, "one", side_effect=one),
            patch.object(customer_routes, "many", side_effect=many),
            patch.object(customer_routes, "write", side_effect=lambda sql, args: self.writes.append((sql, args))),
            patch.object(customer_routes, "cache", return_value=self.redis),
            patch.dict(os.environ, {"INTERNAL_API_TOKEN": "internal-test"}),
        )
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        self.client = TestClient(main.app)

    def token(self, **overrides):
        payload = {"tid": "t_demo", "exp": int(time.time()) + 3600,
                   "kind": "customer", "cid": "t_demo:buyer1", "sess": "session-id"}
        payload.update(overrides)
        return main.signed_token(payload, "hash")

    def test_signed_customer_token_reads_only_session_owner(self):
        response = self.client.get("/api/customer/orders",
                                   headers={"Authorization": "Bearer " + self.token()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["orders"][0]["order_id"], "ORD-1")
        self.assertTrue(any("FROM orders" in sql and args == ("t_demo", "t_demo:buyer1")
                            for sql, args in self.queries))

    def test_admin_tampered_and_expired_tokens_are_rejected(self):
        for token in (self.token(kind=""), self.token() + "x",
                      self.token(exp=int(time.time()) - 1)):
            with self.subTest(token=token[-5:]):
                response = self.client.get("/api/customer/orders",
                                           headers={"Authorization": "Bearer " + token})
                self.assertEqual(response.status_code, 401)
        self.assertFalse(any("FROM orders" in sql for sql, _ in self.queries))

    def test_guest_and_preview_cannot_read_orders(self):
        for flag, status in (("is_guest", 401), ("is_preview", 404)):
            with self.subTest(flag=flag):
                self.session[flag] = 1
                response = self.client.get("/api/customer/orders",
                                           headers={"Authorization": "Bearer " + self.token()})
                self.assertEqual(response.status_code, status)
                self.session[flag] = 0
        self.assertFalse(any("FROM orders" in sql for sql, _ in self.queries))

    def test_detail_is_owner_scoped_and_unknown_is_indistinguishable(self):
        headers = {"Authorization": "Bearer " + self.token()}
        own = self.client.get("/api/customer/orders/ORD-1", headers=headers)
        missing = self.client.get("/api/customer/orders/ORD-2", headers=headers)
        self.assertEqual(own.json()["data"]["status"], "已发货")
        self.assertEqual(missing.status_code, 404)
        self.assertTrue(any("FROM orders" in sql and
                            args == ("t_demo", "t_demo:buyer1", "ORD-2")
                            for sql, args in self.queries))

    def test_internal_adapter_requires_token_and_server_session(self):
        params = {"tenant_id": "t_demo", "thread_id": "t_demo:buyer1:session"}
        denied = self.client.get("/api/agent/orders", params=params)
        self.assertEqual(denied.status_code, 403)
        headers = {"X-Internal-Token": "internal-test"}
        accepted = self.client.get("/api/agent/orders", params=params, headers=headers)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["data"]["orders"][0]["order_id"], "ORD-1")
        cross = self.client.get("/api/agent/orders",
                                params={"tenant_id": "t_other",
                                        "thread_id": "t_demo:buyer1:session"},
                                headers=headers)
        self.assertEqual(cross.status_code, 403)

    def test_guest_and_preview_issue_ownerless_sessions(self):
        with patch.dict(os.environ, {
            "CS_AUTH_DEMO_GUEST_CHAT": "true", "CS_AUTH_DEMO_GUEST_TENANT_ID": "t_demo",
            "CS_AUTH_MERCHANT_PREVIEW_LOGIN": "true",
        }):
            guest = self.client.post("/api/auth/guest-token")
            preview = self.client.post("/api/auth/merchant-preview-token", json={
                "app_key": "demo_key", "app_secret": "preview-secret",
            })
        self.assertEqual(guest.status_code, 200)
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(self.writes[0][1][5])  # is_guest
        self.assertTrue(self.writes[1][1][4])  # is_preview
        self.assertNotEqual(guest.json()["data"]["customer_id"],
                            preview.json()["data"]["customer_id"])

    def test_logout_revokes_session(self):
        token = self.token()
        response = self.client.post("/api/auth/logout", headers={
            "Authorization": "Bearer " + token,
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("DELETE FROM customer_sessions" in sql for sql, _ in self.writes))
        self.assertEqual(self.client.get("/api/customer/session", headers={
            "Authorization": "Bearer " + token,
        }).status_code, 401)


if __name__ == "__main__":
    unittest.main()
