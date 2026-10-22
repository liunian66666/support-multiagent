"""Admin identity, permission and audit contract tests (stage 3)."""

import hashlib
import os
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main_compat as main
from patch_business import patch_business

SECRET_HASH = hashlib.sha256(b"demo-secret").hexdigest()


class FakeRedis:
    def __init__(self):
        self.values = {}

    def exists(self, key):
        return int(key in self.values)


class AdminContractTest(unittest.TestCase):
    def setUp(self):
        self.queries = []
        self.writes = []
        self.redis = FakeRedis()
        self.tenant_platform = 0
        self.user = None          # {"id":5,"tenant_id":"t_demo","status":1}
        self.perms = {"quickreply:view", "quickreply:add", "rating:view"}
        self.replies = [{"id": 9, "tenant_id": "t_demo", "category": "通用",
                         "title": "退货", "content": "7 天无理由", "sort": 0,
                         "is_active": 1, "created_at": None, "updated_at": None}]

        def one(sql, args=()):
            self.queries.append((sql, args))
            if "FROM tenants" in sql:
                return {"tenant_id": "t_demo", "app_secret_hash": SECRET_HASH,
                        "is_platform_admin": self.tenant_platform}
            if "FROM users" in sql and "SELECT id, tenant_id, status" in sql:
                if not self.user:
                    return None
                return self.user
            if "SELECT username" in sql:
                return {"username": "agent01"}
            if "COUNT(*) AS n FROM satisfaction_ratings" in sql and "GROUP BY" not in sql:
                return {"n": 6}
            if "GROUP BY score" in sql:
                return [{"score": 5, "n": 5}, {"score": 4, "n": 1}]
            if "created_at >= %s AND created_at < %s" in sql:
                return {"n": 0, "s": 0}
            if "FROM quick_replies" in sql and "AND id = %s" in sql:
                return self.replies[0] if args[1] == 9 else None
            if "FROM quick_replies" in sql and "ORDER BY id DESC" in sql:
                return self.replies[0]
            return None

        def many(sql, args=()):
            self.queries.append((sql, args))
            if "FROM user_roles" in sql:
                return [{"perm": p} for p in sorted(self.perms)]
            if "GROUP BY score" in sql:
                return [{"score": 5, "n": 5}, {"score": 4, "n": 1}]
            if "FROM satisfaction_ratings" in sql:
                return [{"id": 1, "tenant_id": "t_demo", "thread_id": "t", "ticket_id": 0,
                         "user_key": "u", "score": 5, "tags": "", "comment": "",
                         "created_at": None, "updated_at": None}]
            if "FROM quick_replies" in sql:
                return self.replies
            return []

        self.patches = (
            patch_business(main, "one", side_effect=one),
            patch_business(main, "many", side_effect=many),
            patch_business(main, "write",
                         side_effect=lambda sql, args: self.writes.append((sql, args))),
            patch_business(main, "cache", return_value=self.redis),
            patch.dict(os.environ, {"INTERNAL_API_TOKEN": "internal-test"}),
        )
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        self.client = TestClient(main.app)

    def token(self, **overrides):
        payload = {"tid": "t_demo", "exp": int(time.time()) + 3600,
                   "nonce": "n1", "uid": 0}
        payload.update(overrides)
        return main.signed_token(payload, SECRET_HASH)

    def auth(self, **overrides):
        return {"Authorization": "Bearer " + self.token(**overrides)}

    # ---------- 令牌类型边界 ----------

    def test_admin_token_reads_tenant_scoped_list(self):
        response = self.client.get("/api/admin/quick-replies", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["title"], "退货")
        sql, args = [q for q in self.queries if "FROM quick_replies" in q[0]][0]
        self.assertIn("tenant_id = %s", sql)
        self.assertEqual(args[0], "t_demo")

    def test_customer_token_cannot_act_as_admin(self):
        token = self.token(kind="customer", cid="t_demo:buyer1", sess="s1")
        response = self.client.get("/api/admin/quick-replies",
                                   headers={"Authorization": "Bearer " + token})
        self.assertEqual(response.status_code, 401)
        self.assertIn("令牌无效或已过期", response.json()["msg"])
        self.assertFalse(any("FROM quick_replies" in s for s, _ in self.queries))

    def test_unknown_kind_is_rejected(self):
        # Go VerifyTokenFull 对任何非空 kind 都回同一句文案，不区分类型
        token = self.token(kind="merchant")
        response = self.client.get("/api/admin/quick-replies",
                                   headers={"Authorization": "Bearer " + token})
        self.assertEqual(response.status_code, 401)
        self.assertIn("令牌无效或已过期", response.json()["msg"])

    def test_forged_expired_and_missing_tokens(self):
        cases = {
            "tampered": self.token() + "x",
            "expired": self.token(exp=int(time.time()) - 1),
            "wrong-secret": main.signed_token(
                {"tid": "t_demo", "exp": int(time.time()) + 3600}, "other-hash"),
        }
        for name, token in cases.items():
            with self.subTest(name=name):
                response = self.client.get("/api/admin/quick-replies",
                                           headers={"Authorization": "Bearer " + token})
                self.assertEqual(response.status_code, 401)
        missing = self.client.get("/api/admin/quick-replies")
        self.assertEqual(missing.status_code, 401)
        self.assertFalse(any("FROM quick_replies" in s for s, _ in self.queries))

    def test_blacklisted_token_is_rejected(self):
        token = self.token()
        self.redis.values[main.BLACKLIST_PREFIX + token] = "1"
        response = self.client.get("/api/admin/quick-replies",
                                   headers={"Authorization": "Bearer " + token})
        self.assertEqual(response.status_code, 401)
        self.assertIn("已登出", response.json()["msg"])

    # ---------- RBAC ----------

    def test_app_key_admin_bypasses_permission_lookup(self):
        response = self.client.get("/api/admin/quick-replies", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(any("FROM user_roles" in s for s, _ in self.queries))

    def test_sub_account_requires_matching_permission(self):
        self.user = {"id": 5, "tenant_id": "t_demo", "status": 1}
        allowed = self.client.get("/api/admin/quick-replies", headers=self.auth(uid=5))
        self.assertEqual(allowed.status_code, 200)
        self.perms = {"quickreply:view"}          # 有查看，无新增
        denied = self.client.post("/api/admin/quick-replies", headers=self.auth(uid=5),
                                  json={"title": "x", "content": "y"})
        self.assertEqual(denied.status_code, 403)
        self.assertIn("无权限", denied.json()["msg"])
        self.assertFalse(any("INSERT INTO quick_replies" in s for s, _ in self.writes))

    def test_disabled_or_foreign_sub_account_is_denied(self):
        self.user = {"id": 5, "tenant_id": "t_demo", "status": 0}
        disabled = self.client.get("/api/admin/quick-replies", headers=self.auth(uid=5))
        self.assertEqual(disabled.status_code, 403)
        self.assertIn("账号已禁用", disabled.json()["msg"])
        self.user = {"id": 5, "tenant_id": "t_other", "status": 1}
        foreign = self.client.get("/api/admin/quick-replies", headers=self.auth(uid=5))
        self.assertEqual(foreign.status_code, 403)
        self.assertIn("账号不存在", foreign.json()["msg"])

    def test_platform_admin_cannot_touch_tenant_business_data(self):
        self.tenant_platform = 1
        for path in ("/api/admin/quick-replies", "/api/admin/ratings",
                     "/api/admin/ratings/stats"):
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.auth())
                self.assertEqual(response.status_code, 403)
                self.assertIn("平台管理员不可访问租户业务数据", response.json()["msg"])

    # ---------- 数据语义 ----------

    def test_quick_reply_create_normalizes_and_audits(self):
        response = self.client.post("/api/admin/quick-replies", headers=self.auth(),
                                    json={"category": "  ", "title": "  新话术 ",
                                          "content": " 正文 ", "sort": 3})
        self.assertEqual(response.status_code, 200)
        sql, args = [w for w in self.writes if "INSERT INTO quick_replies" in w[0]][0]
        self.assertEqual(args[:5], ("t_demo", "通用", "新话术", "正文", 3))
        self.assertEqual(args[5], 1)                      # 未传 is_active → 启用
        audit = [w for w in self.writes if "INSERT INTO audit_logs" in w[0]]
        self.assertEqual(len(audit), 1)
        # object_type 由路径推断：Go auditObjectType 去一个尾缀 s 后 quick-replie 落 other
        self.assertEqual(audit[0][1][:5], ("t_demo", "t_demo", 0, "create", "other"))
        self.assertEqual(audit[0][1][5], "quick-replies")  # 无 :id 参数 → 路径末段

    def test_quick_reply_create_rejects_blank_fields(self):
        for body in ({"title": "", "content": "x"}, {"title": "x", "content": "   "}):
            with self.subTest(body=body):
                response = self.client.post("/api/admin/quick-replies", headers=self.auth(),
                                            json=body)
                self.assertEqual(response.status_code, 400)
        self.assertFalse(any("INSERT INTO quick_replies" in s for s, _ in self.writes))

    def test_quick_reply_delete_is_soft_and_scoped(self):
        response = self.client.delete("/api/admin/quick-replies/9", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        sql, args = [w for w in self.writes if w[0].startswith("UPDATE quick_replies")][0]
        self.assertIn("deleted_at = %s", sql)
        self.assertEqual(args[1:], ("t_demo", 9))
        self.assertIsInstance(args[0], int)
        audit = [w for w in self.writes if "INSERT INTO audit_logs" in w[0]]
        self.assertEqual(audit[0][1][3], "delete")

    def test_quick_reply_missing_id_is_404(self):
        for method in ("put", "delete"):
            with self.subTest(method=method):
                kwargs = {"json": {"title": "x"}} if method == "put" else {}
                response = getattr(self.client, method)(
                    "/api/admin/quick-replies/77", headers=self.auth(), **kwargs)
                self.assertEqual(response.status_code, 404)
                self.assertIn("快捷回复不存在", response.json()["msg"])

    def test_quick_reply_list_scopes_and_orders(self):
        self.client.get("/api/admin/quick-replies?q=退&category=通用&active=1",
                        headers=self.auth())
        sql, args = [q for q in self.queries if "FROM quick_replies" in q[0]][0]
        self.assertIn("(deleted_at = 0 OR deleted_at IS NULL)", sql)
        self.assertIn("ORDER BY sort ASC, id DESC", sql)
        self.assertEqual(args, ("t_demo", "%退%", "%退%", "通用", 1))

    def test_quick_reply_list_ignores_invalid_filters(self):
        self.client.get("/api/admin/quick-replies?active=7", headers=self.auth())
        sql, args = [q for q in self.queries if "FROM quick_replies" in q[0]][0]
        self.assertNotIn("is_active = %s", sql)
        self.assertEqual(args, ("t_demo",))

    def test_rating_list_paginates_and_counts(self):
        response = self.client.get("/api/admin/ratings?page=2&page_size=5",
                                   headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["total"], 6)
        sql, args = [q for q in self.queries if "FROM satisfaction_ratings" in q[0]
                     and "ORDER BY created_at DESC" in q[0]][0]
        self.assertEqual(args, ("t_demo", 5, 5))

    def test_rating_page_size_is_clamped(self):
        self.client.get("/api/admin/ratings?page_size=999", headers=self.auth())
        sql, args = [q for q in self.queries if "ORDER BY created_at DESC" in q[0]][0]
        self.assertEqual(args[-2:], (10, 0))

    def test_rating_stats_shape_matches_go(self):
        data = self.client.get("/api/admin/ratings/stats", headers=self.auth()).json()["data"]
        self.assertEqual(data["total"], 6)
        # JSON 会把 map[int]int 的 key 序列化成字符串，Go 侧行为一致
        self.assertEqual(data["dist"], {"1": 0, "2": 0, "3": 0, "4": 1, "5": 5})
        self.assertEqual(data["satisfied"], 6)
        self.assertAlmostEqual(data["avg_score"], 5 - 1 / 6)
        self.assertEqual(len(data["last7days"]), 7)
        self.assertEqual(set(data["last7days"][0]), {"date", "count", "avg"})


if __name__ == "__main__":
    unittest.main()
