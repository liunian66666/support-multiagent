"""阶段 3-10：认证域（Go /api/auth/* 剩余 6 条迁到 FastAPI）回归测试。

对齐对象是 Go 的 handler/auth.go + pkg/auth + pkg/crypto。这批用例分三类：
  1. 算法/工具层：RSA 加解密、登录限流、Go 时长文案、令牌校验错误链
  2. 路由层：6 条接口的成功/失败分支（状态码、文案、审计、副作用）
  3. 双端不变量：黑名单/限流同一 Redis 键空间、载荷字段顺序（逐字节签发）

所有 DB/Redis 均为内存假造（沿用 stage38 的 FakeCursor 模式），不触真实环境。
"""

import base64
import hashlib
import json
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

import main

SECRET_HASH = hashlib.sha256(b"probe-secret").hexdigest()
PLATFORM_HASH = hashlib.sha256(b"plat-secret").hexdigest()
PASSWORD = "passw0rd!"
PASSWORD_HASH = main.hash_user_password("t_demo", "alice", PASSWORD)


def fake_encrypt(public_pem, plaintext):
    """前端视角：用公钥 RSA-OAEP(SHA-256) 加密后 base64（对齐 Go DecryptRSAOAEP 的输入）。"""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    key = serialization.load_pem_public_key(public_pem.encode())
    blob = key.encrypt(
        plaintext.encode(),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                     algorithm=hashes.SHA256(), label=None))
    return base64.b64encode(blob).decode()


class FakeCache:
    """内存 Redis：支持故障注入（boom=True 时所有操作抛 RedisError）。"""

    def __init__(self):
        self.store = {}
        self.ttls = {}
        self.boom = False

    def _check(self):
        if self.boom:
            import redis
            raise redis.RedisError("fake redis down")

    def get(self, key):
        self._check()
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self._check()
        self.store[key] = str(value)
        self.ttls[key] = ex

    def ttl(self, key):
        self._check()
        if key not in self.store:
            return -2
        return self.ttls.get(key, -1)

    def incr(self, key):
        self._check()
        value = int(self.store.get(key, "0")) + 1
        self.store[key] = str(value)
        return value

    def expire(self, key, ttl):
        self._check()
        self.ttls[key] = ttl
        return True

    def delete(self, key):
        self._check()
        self.store.pop(key, None)
        self.ttls.pop(key, None)

    def exists(self, key):
        self._check()
        return 1 if key in self.store else 0


class AuthDomainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.writes = []
        self.queries = []
        self.fake_cache = FakeCache()
        self.tenant_row = None          # /api/auth/token 的租户行
        self.user_row = None            # /api/auth/user-token 的成员行
        self.tenant_by_id = None        # refresh/password 按 tenant_id 查的行
        self.roles = []
        self.perms = set()
        self.reloaded = []
        self.ensure_calls = []

        def one(sql, args=()):
            self.queries.append((sql, args))
            if "FROM tenants WHERE app_key" in sql:
                return self.tenant_row
            if "FROM users" in sql:
                return self.user_row
            if "FROM tenants" in sql:
                # 按 WHERE 参数匹配 tenant_id，查无此行返回 None（模拟 Miss）
                if self.tenant_by_id and args and args[0] == self.tenant_by_id["tenant_id"]:
                    return self.tenant_by_id
                return None
            return None

        def many(sql, args=()):
            self.queries.append((sql, args))
            if sql.startswith("SELECT tenant_id, name, app_key, is_platform_admin"):
                return self.tenants_public
            return []

        def write(sql, args):
            self.writes.append((sql, args))

        self.tenants_public = []

        def cache():
            return self.fake_cache

        def ensure_roles(tenant_id):
            self.ensure_calls.append(tenant_id)

        def get_roles(user_id):
            return self.roles

        def perms(user_id):
            return self.perms

        def reload():
            self.reloaded.append(True)

        self.patches = (
            patch.object(main, "one", side_effect=one),
            patch.object(main, "many", side_effect=many),
            patch.object(main, "write", side_effect=write),
            patch.object(main, "cache", side_effect=cache),
            patch.object(main, "ensure_tenant_roles", side_effect=ensure_roles),
            patch.object(main, "get_user_roles", side_effect=get_roles),
            patch.object(main, "user_perms", side_effect=perms),
            patch.object(main, "reload_go_tenants", side_effect=reload),
        )
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        # RSA：指向临时文件，避免读写真实 /app/data；每个用例一把新密钥
        main.RSA_KEY_PATH = os.path.join(self.tmp.name, "rsa_test.pem")
        main._RSA_KEY = None
        self.addCleanup(self._reset_rsa)
        self.client = TestClient(main.app)

    def _reset_rsa(self):
        main._RSA_KEY = None
        main.RSA_KEY_PATH = os.getenv("RSA_PRIVATE_KEY_PATH",
                                      "/app/data/rsa_private.pem")
    # ----------  fixtures ----------

    def platform_tenant(self, **over):
        row = {"tenant_id": "t_admin", "name": "平台", "app_secret_hash": PLATFORM_HASH,
               "status": 1, "is_platform_admin": True}
        row.update(over)
        return row

    def demo_tenant(self, **over):
        row = {"tenant_id": "t_demo", "name": "演示租户",
               "app_secret_hash": SECRET_HASH, "status": 1,
               "is_platform_admin": False}
        row.update(over)
        return row

    def alice(self, **over):
        row = {"id": 7, "tenant_id": "t_demo", "username": "alice",
               "nickname": "爱丽丝", "password_hash": PASSWORD_HASH, "status": 1}
        row.update(over)
        return row

    def mint(self, tid="t_demo", uid=0, exp=None, secret_hash=SECRET_HASH, **claims):
        payload = {"tid": tid, "exp": exp if exp is not None else int(time.time()) + 3600,
                   "nonce": "n39"}
        if uid:
            payload["uid"] = uid
        payload.update(claims)
        return main.signed_token(payload, secret_hash)

    def audit_rows(self, action):
        return [a for s, a in self.writes
                if s.startswith("INSERT INTO audit_logs") and a[3] == action]

    # ----------  ① 工具层 ----------

    def test_go_minutes_matches_go_duration_string(self):
        self.assertEqual(main.go_minutes(600), "10m0s")
        self.assertEqual(main.go_minutes(570), "10m0s")     # 9m30s Round → 10m0s
        self.assertEqual(main.go_minutes(540), "9m0s")
        self.assertEqual(main.go_minutes(29), "0s")         # 半数下舍
        self.assertEqual(main.go_minutes(60), "1m0s")

    def test_rsa_public_pem_is_spki(self):
        pem = main.rsa_public_pem()
        self.assertTrue(pem.startswith("-----BEGIN PUBLIC KEY-----"))
        self.assertTrue(pem.rstrip().endswith("-----END PUBLIC KEY-----"))

    def test_rsa_roundtrip(self):
        blob = fake_encrypt(main.rsa_public_pem(), "s3cret!")
        self.assertEqual(main.rsa_decrypt_oaep(blob), "s3cret!")

    def test_rsa_decrypt_rejects_garbage(self):
        with self.assertRaises(Exception):
            main.rsa_decrypt_oaep(base64.b64encode(b"not-a-ciphertext").decode())

    def test_admin_token_payload_field_order_and_omitempty(self):
        """载荷字段顺序必须与 Go tokenPayload 一致；uid=0 不出现在载荷里。"""
        self.assertEqual(list(main.admin_token_payload("t_demo")),
                         ["tid", "exp", "nonce"])
        self.assertEqual(list(main.admin_token_payload("t_demo", 7)),
                         ["tid", "exp", "nonce", "uid"])
        token = main.signed_token(main.admin_token_payload("t_demo"), SECRET_HASH)
        encoded = token.split(".", 1)[0]
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        self.assertEqual(list(json.loads(raw)), ["tid", "exp", "nonce"])

    def test_blacklist_ttl_follows_token_expiry(self):
        token = self.mint(exp=int(time.time()) + 120)
        main.blacklist_admin_token(token)
        self.assertIn(main.BLACKLIST_PREFIX + token, self.fake_cache.store)
        ttl = self.fake_cache.ttls[main.BLACKLIST_PREFIX + token]
        self.assertTrue(100 <= ttl <= 120, ttl)
        expired = self.mint(exp=int(time.time()) - 5)
        main.blacklist_admin_token(expired)
        self.assertEqual(self.fake_cache.ttls[main.BLACKLIST_PREFIX + expired], 60)

    # ----------  ② 登录限流（同一 Redis 键空间） ----------

    def test_login_limiter_locks_after_five_fails(self):
        key = "1.2.3.4:tk_probe"
        self.assertEqual(main.check_login_allowed(key), (True, 0))
        for _ in range(main.LOGIN_MAX_FAILS):
            main.record_login_fail(key)
        allowed, wait = main.check_login_allowed(key)
        self.assertFalse(allowed)
        self.assertEqual(wait, main.LOGIN_WINDOW)
        self.assertEqual(main.LOGIN_FAIL_PREFIX, "cs:login:fail:")   # 与 Go 同键空间

    def test_login_limiter_clear_resets(self):
        key = "1.2.3.4:tk_probe"
        for _ in range(main.LOGIN_MAX_FAILS):
            main.record_login_fail(key)
        main.clear_login_fails(key)
        self.assertEqual(main.check_login_allowed(key), (True, 0))

    def test_login_limiter_fail_closed_on_redis_error(self):
        self.fake_cache.boom = True
        allowed, wait = main.check_login_allowed("ip:key")
        self.assertFalse(allowed)
        self.assertEqual(wait, 60)

    # ----------  ③ 令牌校验错误链（refresh 专用，文案逐字对齐 Go） ----------

    def test_strict_verify_accepts_admin_token(self):
        token = self.mint(uid=7)
        self.tenant_by_id = self.demo_tenant()
        self.assertEqual(main.verify_admin_token_strict(token), ("t_demo", 7))

    def test_strict_verify_error_chain(self):
        self.tenant_by_id = self.demo_tenant()
        cases = [
            (self.mint(exp=int(time.time()) - 1), "token 已过期"),
            (self.mint(tid="t_gone"), "租户不存在"),
            (self.mint()[:-2] + "AA", "token 签名无效"),
            (self.mint(kind="customer"), "客户会话令牌不可用于管理端"),
            (self.mint(kind="weird"), "未知令牌类型，已拒绝"),
            ("no-dot-token", "token 格式错误"),
            ("!!!.xxx", "token 解码失败"),
        ]
        for token, expected in cases:
            with self.assertRaises(main.AdminTokenError) as ctx:
                main.verify_admin_token_strict(token)
            self.assertEqual(str(ctx.exception), expected, token[:12])

    def test_strict_verify_payload_type_errors(self):
        self.tenant_by_id = self.demo_tenant()
        for claims in ({"tid": 1}, {"exp": "x"}, {"uid": -1}, {"uid": 1.5},
                       {"kind": 3}):
            payload = {"tid": "t_demo", "exp": int(time.time()) + 60,
                       "nonce": "n"}
            payload.update(claims)
            token = main.signed_token(payload, SECRET_HASH)
            with self.assertRaises(main.AdminTokenError) as ctx:
                main.verify_admin_token_strict(token)
            self.assertEqual(str(ctx.exception), "token 载荷无效", claims)

    def test_strict_verify_blacklisted_and_fail_closed(self):
        token = self.mint()
        self.fake_cache.set(main.BLACKLIST_PREFIX + token, "1", ex=60)
        with self.assertRaises(main.AdminTokenError) as ctx:
            main.verify_admin_token_strict(token)
        self.assertEqual(str(ctx.exception), "token 已失效（已登出）")
        self.fake_cache.boom = True
        with self.assertRaises(main.AdminTokenError) as ctx:
            main.verify_admin_token_strict(self.mint())
        self.assertEqual(str(ctx.exception), "token 已失效（已登出）")

    # ----------  ④ POST /api/auth/token ----------

    def test_token_login_success(self):
        self.tenant_row = self.demo_tenant()
        resp = self.client.post("/api/auth/token",
                                json={"app_key": "tk_demo", "app_secret": "probe-secret"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["code"], 0)
        data = body["data"]
        self.assertEqual(data["tenant_id"], "t_demo")
        self.assertEqual(data["expires_in"], main.ADMIN_TOKEN_TTL)
        self.assertTrue(data["is_admin"])
        self.assertFalse(data["is_platform_admin"])
        self.assertEqual(data["nickname"], "t_demo")
        self.assertEqual(data["role_name"], "管理员")
        self.assertEqual(data["tenant_name"], "演示租户")
        # 签发的令牌必须能过严格验签（双端互验的 FastAPI 侧）
        self.tenant_by_id = self.demo_tenant()
        self.assertEqual(main.verify_admin_token_strict(data["token"]),
                         ("t_demo", 0))
        # 审计 login_ok + 防爆破计数已清
        rows = self.audit_rows("login_ok")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:3], ("t_demo", "tk_demo", 0))
        self.assertEqual(rows[0][3:7], ("login_ok", "auth", "app_key", "登录成功"))
        self.assertEqual(self.fake_cache.store, {})

    def test_token_login_encrypted_secret(self):
        self.tenant_row = self.demo_tenant()
        blob = fake_encrypt(main.rsa_public_pem(), "probe-secret")
        resp = self.client.post("/api/auth/token",
                                json={"app_key": "tk_demo", "app_secret": blob,
                                      "encrypted": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["tenant_id"], "t_demo")

    def test_token_login_wrong_secret(self):
        self.tenant_row = self.demo_tenant()
        resp = self.client.post("/api/auth/token",
                                json={"app_key": "tk_demo", "app_secret": "nope"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["msg"], "无效的 app_key 或 app_secret")
        rows = self.audit_rows("login_fail")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "system")            # Go: tenantID 空 → system
        self.assertEqual(rows[0][6], "无效的 app_key 或 app_secret")
        self.assertEqual(self.fake_cache.store.get("cs:login:fail:testclient:tk_demo"),
                         "1")

    def test_token_login_disabled_tenant_rejected(self):
        self.tenant_row = self.demo_tenant(status=0)
        resp = self.client.post("/api/auth/token",
                                json={"app_key": "tk_demo", "app_secret": "probe-secret"})
        self.assertEqual(resp.status_code, 401)

    def test_token_login_bad_ciphertext(self):
        self.tenant_row = self.demo_tenant()
        resp = self.client.post("/api/auth/token",
                                json={"app_key": "tk_demo",
                                      "app_secret": base64.b64encode(b"junk").decode(),
                                      "encrypted": True})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["msg"], "app_secret 解密失败")

    def test_token_login_locked_returns_429(self):
        for _ in range(main.LOGIN_MAX_FAILS):
            main.record_login_fail("testclient:tk_demo")
        resp = self.client.post("/api/auth/token",
                                json={"app_key": "tk_demo", "app_secret": "x"})
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["msg"], "尝试过于频繁，请 10m0s 后再试")

    def test_token_login_param_errors(self):
        for payload in ({}, {"app_key": "tk"}, {"app_key": "tk", "app_secret": ""},
                        {"app_key": 1, "app_secret": "x"},
                        {"app_key": "tk", "app_secret": "x", "encrypted": "yes"}):
            resp = self.client.post("/api/auth/token", json=payload)
            self.assertEqual(resp.status_code, 400, payload)
            self.assertEqual(resp.json()["msg"], "参数错误")

    # ----------  ⑤ POST /api/auth/user-token ----------

    def test_user_token_success(self):
        self.user_row = self.alice()
        self.tenant_by_id = self.demo_tenant()
        self.roles = [{"id": 3, "name": "客服", "code": "staff", "is_admin": False}]
        self.perms = {"ticket:view", "session:view"}
        resp = self.client.post("/api/auth/user-token",
                                json={"tenant_id": "t_demo", "username": "alice",
                                      "password": PASSWORD})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["user_id"], 7)
        self.assertEqual(data["nickname"], "爱丽丝")
        self.assertEqual(data["role_name"], "客服")
        self.assertEqual(data["role"], "客服")
        self.assertEqual(data["role_code"], "staff")
        self.assertEqual(data["tenant_name"], "演示租户")
        self.assertFalse(data["is_admin"])
        self.assertFalse(data["is_platform_admin"])
        self.assertEqual(data["permissions"], ["session:view", "ticket:view"])
        self.assertEqual(data["roles"], self.roles)
        # last_login 已更新；令牌带 uid 且可过严格验签
        self.assertTrue(any("UPDATE users SET last_login_at" in s
                            for s, _ in self.writes))
        self.assertEqual(main.verify_admin_token_strict(data["token"]),
                         ("t_demo", 7))
        self.assertIn("t_demo", self.ensure_calls)
        rows = self.audit_rows("login_ok")
        self.assertEqual(rows[0][:3], ("t_demo", "alice", 7))
        self.assertEqual(rows[0][6], "账号登录成功")

    def test_user_token_full_perms_is_admin(self):
        self.user_row = self.alice()
        self.tenant_by_id = self.demo_tenant()
        self.roles = [{"id": 3, "name": "管理员", "code": "admin", "is_admin": True}]
        self.perms = set(main.PERMISSION_CODES)
        resp = self.client.post("/api/auth/user-token",
                                json={"tenant_id": "t_demo", "username": "alice",
                                      "password": PASSWORD})
        self.assertTrue(resp.json()["data"]["is_admin"])

    def test_user_token_wrong_password(self):
        self.user_row = self.alice()
        resp = self.client.post("/api/auth/user-token",
                                json={"tenant_id": "t_demo", "username": "alice",
                                      "password": "wrong"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["msg"], "用户名或密码错误")
        rows = self.audit_rows("login_fail")
        self.assertEqual(rows[0][:3], ("t_demo", "alice", 7))
        self.assertEqual(rows[0][6], "用户名或密码错误")

    def test_user_token_disabled_account(self):
        self.user_row = self.alice(status=0)
        resp = self.client.post("/api/auth/user-token",
                                json={"tenant_id": "t_demo", "username": "alice",
                                      "password": PASSWORD})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["msg"], "账号已禁用，请联系管理员")
        self.assertEqual(self.audit_rows("login_fail")[0][6], "账号已禁用")

    def test_user_token_unknown_user(self):
        self.user_row = None
        resp = self.client.post("/api/auth/user-token",
                                json={"tenant_id": "t_demo", "username": "ghost",
                                      "password": PASSWORD})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.audit_rows("login_fail")[0][:2], ("t_demo", "ghost"))

    def test_user_token_tenant_missing_or_disabled(self):
        self.user_row = self.alice()
        self.tenant_by_id = None
        resp = self.client.post("/api/auth/user-token",
                                json={"tenant_id": "t_demo", "username": "alice",
                                      "password": PASSWORD})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["msg"], "租户不存在")
        self.user_row = self.alice()
        self.tenant_by_id = self.demo_tenant(status=0)
        resp = self.client.post("/api/auth/user-token",
                                json={"tenant_id": "t_demo", "username": "alice",
                                      "password": PASSWORD})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["msg"], "租户已停用")

    def test_user_token_encrypted_password(self):
        self.user_row = self.alice()
        self.tenant_by_id = self.demo_tenant()
        blob = fake_encrypt(main.rsa_public_pem(), PASSWORD)
        resp = self.client.post("/api/auth/user-token",
                                json={"tenant_id": "t_demo", "username": "alice",
                                      "password": blob, "encrypted": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["user_id"], 7)

    # ----------  ⑥ POST /api/auth/refresh ----------

    def test_refresh_app_key_admin(self):
        self.tenant_by_id = self.platform_tenant()
        token = self.mint(tid="t_admin", secret_hash=PLATFORM_HASH)
        resp = self.client.post("/api/auth/refresh",
                                headers={"Authorization": "Bearer " + token})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertTrue(data["is_admin"])
        self.assertTrue(data["is_platform_admin"])
        self.assertEqual(data["nickname"], "t_admin")
        self.assertEqual(data["role_name"], "管理员")
        self.assertEqual(data["tenant_name"], "平台")
        self.assertNotIn("user_id", data)
        # 旧令牌已黑名单，新令牌可过严格验签
        self.assertIn(main.BLACKLIST_PREFIX + token, self.fake_cache.store)
        self.assertEqual(main.verify_admin_token_strict(data["token"]),
                         ("t_admin", 0))

    def test_refresh_sub_account(self):
        self.tenant_by_id = self.demo_tenant()
        self.user_row = self.alice()
        self.roles = [{"id": 3, "name": "客服", "code": "staff", "is_admin": False}]
        self.perms = {"ticket:view"}
        resp = self.client.post("/api/auth/refresh",
                                headers={"Authorization": "Bearer " + self.mint(uid=7)})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["user_id"], 7)
        self.assertEqual(data["nickname"], "爱丽丝")
        self.assertEqual(data["role_name"], "客服")
        self.assertFalse(data["is_admin"])

    def test_refresh_accepts_raw_token_without_bearer(self):
        self.tenant_by_id = self.demo_tenant()
        resp = self.client.post("/api/auth/refresh",
                                headers={"Authorization": self.mint()})
        self.assertEqual(resp.status_code, 200)

    def test_refresh_requires_token(self):
        resp = self.client.post("/api/auth/refresh")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["msg"], "未提供 token")

    def test_refresh_error_messages_match_go(self):
        self.tenant_by_id = self.demo_tenant()
        cases = [
            (self.mint(kind="customer"), "token 刷新失败: 客户会话令牌不可用于管理端"),
            (self.mint(exp=int(time.time()) - 1), "token 刷新失败: token 已过期"),
            (self.mint(tid="t_gone"), "token 刷新失败: 租户不存在"),
            ("broken", "token 刷新失败: token 格式错误"),
        ]
        for token, expected in cases:
            resp = self.client.post("/api/auth/refresh",
                                    headers={"Authorization": "Bearer " + token})
            self.assertEqual(resp.status_code, 401, token[:12])
            self.assertEqual(resp.json()["msg"], expected)

    def test_refresh_deleted_sub_account_rejected(self):
        self.tenant_by_id = self.demo_tenant()
        self.user_row = None
        resp = self.client.post("/api/auth/refresh",
                                headers={"Authorization": "Bearer " + self.mint(uid=7)})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["msg"], "账号不存在或已禁用，请重新登录")

    # ----------  ⑦ PUT /api/auth/password ----------

    def test_password_change_success(self):
        self.tenant_by_id = self.demo_tenant()
        resp = self.client.put(
            "/api/auth/password",
            headers={"Authorization": "Bearer " + self.mint()},
            json={"old_app_secret": "probe-secret", "new_app_secret": "new-secret-123"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["msg"], "密码修改成功，请重新登录")
        updates = [a for s, a in self.writes if s.startswith("UPDATE tenants SET")]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][0],
                         hashlib.sha256(b"new-secret-123").hexdigest())
        rows = self.audit_rows("change_password")
        self.assertEqual(rows[0][:2], ("t_demo", ""))       # operator 空串（Go 约定）
        self.assertEqual(rows[0][3:7], ("change_password", "auth", "app_secret",
                                        "修改登录密钥成功"))
        self.assertEqual(self.reloaded, [True])             # Go 租户缓存必须同步失效

    def test_password_change_wrong_old_secret(self):
        self.tenant_by_id = self.demo_tenant()
        resp = self.client.put(
            "/api/auth/password",
            headers={"Authorization": "Bearer " + self.mint()},
            json={"old_app_secret": "wrong", "new_app_secret": "new-secret-123"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["msg"], "旧密钥不正确")
        rows = self.audit_rows("change_password_fail")
        self.assertEqual(rows[0][6], "旧密钥不正确")
        self.assertFalse(any(s.startswith("UPDATE tenants SET") for s, _ in self.writes))
        self.assertEqual(self.reloaded, [])

    def test_password_change_short_new_secret(self):
        self.tenant_by_id = self.demo_tenant()
        resp = self.client.put(
            "/api/auth/password",
            headers={"Authorization": "Bearer " + self.mint()},
            json={"old_app_secret": "probe-secret", "new_app_secret": "short"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["msg"], "新密钥长度至少 8 位")

    def test_password_change_requires_auth(self):
        resp = self.client.put(
            "/api/auth/password",
            json={"old_app_secret": "probe-secret", "new_app_secret": "new-secret-123"})
        self.assertEqual(resp.status_code, 401)

    def test_password_change_auth_runs_before_body(self):
        """对齐 Go：路由挂 AdminAuth 中间件，先鉴权后校验请求体。

        无令牌 + 非法请求体 → 401（Go 同）；带有效令牌 + 非法请求体 → 400。
        """
        resp = self.client.put("/api/auth/password", json={"old_app_secret": "x"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["msg"], "缺少访问令牌 token")
        self.tenant_by_id = self.demo_tenant()
        resp = self.client.put(
            "/api/auth/password",
            headers={"Authorization": "Bearer " + self.mint()},
            json={"old_app_secret": "x"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["msg"], "参数错误")

    def test_password_change_encrypted(self):
        self.tenant_by_id = self.demo_tenant()
        old_blob = fake_encrypt(main.rsa_public_pem(), "probe-secret")
        new_blob = fake_encrypt(main.rsa_public_pem(), "new-secret-123")
        resp = self.client.put(
            "/api/auth/password",
            headers={"Authorization": "Bearer " + self.mint()},
            json={"old_app_secret": old_blob, "new_app_secret": new_blob,
                  "encrypted": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.reloaded, [True])

    # ----------  ⑧ GET /api/auth/tenants ----------

    def test_public_tenants_excludes_platform_and_disabled(self):
        self.tenants_public = [
            {"tenant_id": "t_admin", "name": "平台", "app_key": "k1",
             "is_platform_admin": True},
            {"tenant_id": "t_demo", "name": "演示租户", "app_key": "k2",
             "is_platform_admin": False},
            {"tenant_id": "t_x", "name": "X", "app_key": "k3",
             "is_platform_admin": False},
        ]
        resp = self.client.get("/api/auth/tenants")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"], [
            {"tenant_id": "t_demo", "name": "演示租户", "app_key": "k2"},
            {"tenant_id": "t_x", "name": "X", "app_key": "k3"},
        ])
        sql = self.queries[-1][0]
        self.assertIn("status = 1", sql)
        self.assertIn("deleted_at = 0", sql)
        self.assertIn("ORDER BY id ASC", sql)

    # ----------  ⑨ GET /api/auth/public-key ----------

    def test_public_key_endpoint(self):
        resp = self.client.get("/api/auth/public-key")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["data"]["public_key"]
                        .startswith("-----BEGIN PUBLIC KEY-----"))


if __name__ == "__main__":
    unittest.main()
