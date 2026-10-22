"""Stage 3-5 contract tests: RBAC 域（菜单树 / 权限目录 / 成员 / 角色）。

锁的重点：
1. 三种登录身份的菜单树差异（平台=系统管理 / 租户 app_key=业务中心全量 / 子账号=按权限过滤保留父链）
2. 成员与角色的 JSON 形态（时间格式、nil→[] vs null、is_admin 只看权限集合）
3. 写路径的 Go 怪癖：审计双写、状态接口不校验成员存在、软删除存 unix 秒、自操作保护
"""

import hashlib
import os
import time
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

import main_compat as main
from patch_business import patch_business

SECRET_HASH = hashlib.sha256(b"demo-secret").hexdigest()


def menu_row(**overrides):
    row = {"id": 1, "parent_id": 0, "group_name": "业务中心", "name": "会话管理", "type": 1,
           "path": "", "component": "", "perm": None, "icon": "", "sort": 1, "visible": 1,
           "status": 1, "is_builtin": 1, "created_at": None,
           "updated_at": datetime(2026, 10, 18, 13, 5, 0)}
    row.update(overrides)
    return row


def user_row(**overrides):
    row = {"id": 5, "tenant_id": "t_demo", "username": "kefu01", "nickname": "小王",
           "phone": "13800000000", "email": "k@example.com", "remark": "", "status": 1,
           "last_login_at": None, "created_at": datetime(2026, 10, 1, 9, 0, 0),
           "updated_at": datetime(2026, 10, 1, 9, 0, 0)}
    row.update(overrides)
    return row


class FakeCursor:
    """假游标：记录语句，并把预设结果喂给 fetchone/fetchall。"""

    def __init__(self, harness):
        self.harness = harness
        self.lastrowid = None
        self._rows = None

    def execute(self, sql, args=()):
        self.harness.statements.append((sql, args))
        if sql.startswith("INSERT INTO roles"):
            self.harness.role_seq += 1
            self.lastrowid = 900 + self.harness.role_seq
        elif sql.startswith("INSERT INTO users"):
            self.lastrowid = 77
        self._rows = self.harness.cursor_result(sql, args)

    def fetchone(self):
        if isinstance(self._rows, list):
            return self._rows[0] if self._rows else None
        return self._rows

    def fetchall(self):
        if self._rows is None:
            return []
        return self._rows if isinstance(self._rows, list) else [self._rows]


class Stage35Test(unittest.TestCase):
    def setUp(self):
        self.queries = []
        self.writes = []
        self.statements = []
        self.tenant_platform = 0
        self.user = None                    # require_perm 的账号行
        self.perms = set()
        self.menus = []
        self.users = []
        self.roles = []
        self.role_by_id = {}
        self.role_menu_ids = {}
        self.role_perm_rows = {}
        self.user_role_rels = {}
        self.user_by_id = {}
        self.existing_username = None
        self.role_count = 0
        self.tenant_seeded = 0
        self.role_seq = 0
        self.cursor_hook = None
        self.audit_rows_written = []

        def one(sql, args=()):
            self.queries.append((sql, args))
            if "app_secret_hash" in sql:
                return {"tenant_id": "t_demo", "app_secret_hash": SECRET_HASH,
                        "is_platform_admin": self.tenant_platform}
            if sql.startswith("SELECT id, tenant_id, status FROM users"):
                return self.user
            if "FROM users WHERE id = %s AND deleted_at = 0" in sql:
                return self.user_by_id.get(args[0])
            if "SELECT id FROM users WHERE tenant_id = %s AND username = %s" in sql:
                return self.existing_username
            if sql.startswith("SELECT id, tenant_id, code, name, description"):
                return self.role_by_id.get(args[0])
            if sql.startswith("SELECT id, name, code FROM roles"):
                role = self.role_by_id.get(args[0])
                return role
            return None

        def many(sql, args=()):
            self.queries.append((sql, args))
            if "JOIN sys_role_menu rm" in sql and "ur.user_id" in sql:
                return [{"perm": p} for p in sorted(self.perms)]
            if sql.startswith("SELECT id, parent_id, group_name"):
                return list(self.menus)
            if "FROM users WHERE" in sql:
                return list(self.users)
            if sql.startswith("SELECT id, tenant_id, code, name, description"):
                return list(self.roles)
            if sql.startswith("SELECT menu_id FROM sys_role_menu"):
                return [{"menu_id": m} for m in self.role_menu_ids.get(args[0], [])]
            if sql.startswith("SELECT DISTINCT m.perm FROM sys_role_menu"):
                return [{"perm": p} for p in self.role_perm_rows.get(args[0], [])]
            if sql.startswith("SELECT user_id, role_id FROM user_roles"):
                return [{"user_id": args[0], "role_id": r}
                        for r in self.user_role_rels.get(args[0], [])]
            return []

        def write(sql, args):
            self.writes.append((sql, args))
            if "INSERT INTO audit_logs" in sql:
                self.audit_rows_written.append(args)

        @contextmanager
        def transaction():
            yield FakeCursor(self)

        class FakeCache:
            def exists(self, key):
                return 0

        self.patches = (
            patch_business(main, "one", side_effect=one),
            patch_business(main, "many", side_effect=many),
            patch_business(main, "write", side_effect=write),
            patch_business(main, "transaction", side_effect=transaction),
            patch_business(main, "cache", return_value=FakeCache()),
            patch.dict(os.environ, {"INTERNAL_API_TOKEN": "internal-test"}),
        )
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        self.client = TestClient(main.app)

    # ---------- 工具 ----------

    def cursor_result(self, sql, args):
        if self.cursor_hook:
            return self.cursor_hook(sql, args)
        return None

    def token(self, **overrides):
        payload = {"tid": "t_demo", "exp": int(time.time()) + 3600, "nonce": "n1", "uid": 0}
        payload.update(overrides)
        return main.signed_token(payload, SECRET_HASH)

    def auth(self, **overrides):
        return {"Authorization": "Bearer " + self.token(**overrides)}

    def as_subaccount(self, uid=5, perms=("member:view", "member:add", "member:edit",
                                        "member:remove", "role:view", "role:add",
                                        "role:edit", "role:remove")):
        self.user = {"id": uid, "tenant_id": "t_demo", "status": 1}
        self.user_by_id.setdefault(uid, user_row(id=uid))
        self.perms = set(perms)
        return self.auth(uid=uid)

    def writes_matching(self, needle):
        return [w for w in self.writes if needle in w[0]]

    def stmts_matching(self, needle):
        return [s for s in self.statements if needle in s[0]]

    # ---------- 菜单树 ----------

    def test_menu_tree_tenant_admin_scope_keeps_all_biz_nodes(self):
        self.menus = [
            menu_row(id=1, parent_id=0, sort=1),
            menu_row(id=2, parent_id=1, sort=1, name="运营看板", type=2, path="/ops",
                     component="views/Ops.vue", perm="dashboard:view"),
            menu_row(id=110, parent_id=0, group_name="系统管理", sort=1, name="租户管理"),
        ]
        payload = self.client.get("/api/admin/menus/tree", headers=self.auth()).json()
        groups = payload["data"]
        self.assertEqual([g["group"] for g in groups], ["业务中心"])
        self.assertEqual(len(groups[0]["children"]), 1)
        child = groups[0]["children"][0]
        self.assertEqual(child["name"], "会话管理")
        self.assertEqual([n["id"] for n in child["children"]], [2])
        # 排序依赖 GROUP BY 的顺序，必须与 Go cachedMenus 一致
        sql = [q for q in self.queries if "FROM menus" in q[0]][0][0]
        self.assertIn("ORDER BY group_name ASC, sort ASC, id ASC", sql)

    def test_menu_tree_platform_admin_sees_system_group_only(self):
        self.tenant_platform = 1
        self.menus = [
            menu_row(id=1, parent_id=0),
            menu_row(id=110, parent_id=0, group_name="系统管理", name="租户管理"),
            menu_row(id=100, parent_id=110, group_name="系统管理", name="租户列表", type=2,
                     path="/tenants", perm=""),
        ]
        data = self.client.get("/api/admin/menus/tree", headers=self.auth()).json()["data"]
        self.assertEqual([g["group"] for g in data], ["系统管理"])
        self.assertEqual([n["id"] for n in data[0]["children"]], [110])

    def test_menu_tree_subaccount_filters_and_backfills_parents(self):
        self.menus = [
            menu_row(id=1, parent_id=0),
            menu_row(id=2, parent_id=1, name="运营看板", type=2, perm="dashboard:view"),
            menu_row(id=10, parent_id=1, sort=2, name="会话列表", type=2, perm="session:view"),
            menu_row(id=11, parent_id=10, sort=1, name="编辑客户信息", type=3,
                     perm="session:profile:edit"),
        ]
        headers = self.as_subaccount(perms=("session:view",))
        data = self.client.get("/api/admin/menus/tree", headers=headers).json()["data"]
        roots = data[0]["children"]
        self.assertEqual([n["id"] for n in roots], [1])
        kept = roots[0]["children"]
        self.assertEqual([n["id"] for n in kept], [10])          # dashboard:view 被过滤掉
        self.assertNotIn("children", kept[0])                    # 叶子节点省略 children

    def test_menu_tree_empty_is_null(self):
        self.menus = []
        payload = self.client.get("/api/admin/menus/tree", headers=self.auth()).json()
        self.assertIsNone(payload["data"])                       # Go return nil → null

    def test_menu_node_serialization(self):
        self.menus = [menu_row(id=1, created_at=None, perm=None,
                               updated_at=datetime(2026, 10, 18, 13, 5, 0))]
        node = self.client.get("/api/admin/menus/tree",
                               headers=self.auth()).json()["data"][0]["children"][0]
        self.assertIsNone(node["perm"])
        self.assertEqual(node["created_at"], "0001-01-01T00:00:00Z")   # 非指针 time 零值
        self.assertEqual(node["updated_at"], "2026-10-18T13:05:00+08:00")
        self.assertEqual(set(node), {"id", "parent_id", "group_name", "name", "type", "path",
                                     "component", "perm", "icon", "sort", "visible", "status",
                                     "is_builtin", "created_at", "updated_at"})

    # ---------- 权限目录 ----------

    def test_permission_catalog_skips_empty_perm(self):
        self.menus = [
            menu_row(id=2, name="运营看板", perm="dashboard:view", sort=1),
            menu_row(id=1, name="会话管理", perm=None, sort=1),
            menu_row(id=3, name="工单列表", perm="", sort=1, type=2),
        ]
        data = self.client.get("/api/admin/permissions", headers=self.auth()).json()["data"]
        self.assertEqual(data, [{"code": "dashboard:view", "name": "运营看板",
                                 "group": "业务中心"}])

    def test_permission_catalog_requires_role_view(self):
        headers = self.as_subaccount(perms=("member:view",))
        response = self.client.get("/api/admin/permissions", headers=headers)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["msg"], "无权限执行该操作")

    # ---------- 成员列表 ----------

    def test_member_list_scoped_and_seeds_roles(self):
        self.users = [user_row()]
        self.user_role_rels = {5: [1, 2]}
        self.role_by_id = {1: {"id": 1, "name": "管理员", "code": "admin"},
                           2: {"id": 2, "name": "客服", "code": "staff"}}
        self.role_perm_rows = {1: set(main.PERMISSION_CODES), 2: {"session:view"}}
        data = self.client.get("/api/admin/members", headers=self.auth()).json()["data"]
        self.assertEqual(len(data), 1)
        row = data[0]
        self.assertEqual(row["username"], "kefu01")
        self.assertNotIn("password_hash", row)
        self.assertIsNone(row["last_login_at"])
        self.assertEqual(row["created_at"], "2026-10-01T09:00:00+08:00")
        self.assertEqual([r["code"] for r in row["roles"]], ["admin", "staff"])
        self.assertEqual([r["is_admin"] for r in row["roles"]], [True, False])
        sql, args = [q for q in self.queries if "FROM users WHERE" in q[0]][0]
        self.assertEqual(args, ("t_demo",))
        self.assertIn("ORDER BY id ASC", sql)

    def test_member_list_filters(self):
        self.users = []
        self.client.get("/api/admin/members?q=%20wang%20&role_id=2&status=1",
                        headers=self.auth())
        sql, args = [q for q in self.queries if "FROM users WHERE" in q[0]][0]
        self.assertIn("username LIKE %s", sql)
        self.assertIn("id IN (SELECT user_id FROM user_roles WHERE role_id = %s)", sql)
        self.assertIn("status = %s", sql)
        self.assertEqual(args, ("t_demo", "%wang%", 2, 1))

    def test_member_list_ignores_illegal_status_and_role(self):
        self.client.get("/api/admin/members?status=9&role_id=abc", headers=self.auth())
        sql, args = [q for q in self.queries if "FROM users WHERE" in q[0]][0]
        self.assertEqual(args, ("t_demo",))

    def test_member_list_platform_admin_blocked(self):
        self.tenant_platform = 1
        response = self.client.get("/api/admin/members", headers=self.auth())
        self.assertEqual(response.status_code, 403)
        self.assertIn("平台管理员不可访问租户业务数据", response.json()["msg"])

    # ---------- 成员创建 ----------

    def test_member_create_validation_order(self):
        cases = [
            ({}, "参数错误（至少分配一个角色）"),
            ({"username": "ab", "password": "12345678", "role_ids": [1]},
             "用户名长度需 3-32 个字符"),
            ({"username": "kefu01", "password": "12345678", "role_ids": [1],
              "phone": "12345"}, "手机号格式不正确"),
            ({"username": "kefu01", "password": "12345678", "role_ids": [1],
              "email": "kf.example.com"}, "邮箱格式不正确"),
            ({"username": "kefu01", "password": "1234567", "role_ids": [1]},
             "密码长度至少 8 位"),
            ({"username": "kefu01", "password": "12345678", "role_ids": []},
             "参数错误（至少分配一个角色）"),
            ({"username": ["x"], "password": "12345678", "role_ids": [1]},
             "参数错误（至少分配一个角色）"),
        ]
        for body, message in cases:
            with self.subTest(body=body):
                response = self.client.post("/api/admin/members", json=body,
                                            headers=self.auth())
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["msg"], message)

    def test_member_create_success_trims_and_hashes(self):
        self.user_by_id[77] = user_row(id=77, username="kefu01")
        response = self.client.post(
            "/api/admin/members",
            json={"username": "kefu01", "password": "pass12345", "role_ids": [1, 0],
                  "nickname": " 小王 ", "phone": "13800000000", "email": " k@a.com",
                  "remark": " 备注 "},
            headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"code": 0, "msg": "成员已创建",
                                           "data": {"id": 77}})
        insert = self.stmts_matching("INSERT INTO users")[0]
        expected = hashlib.sha256(b"t_demo|kefu01|pass12345").hexdigest()
        self.assertEqual(insert[1][2], expected)                 # 租户|用户名|密码 加盐
        self.assertEqual(insert[1][3:7], ("小王", "13800000000", "k@a.com", "备注"))
        links = self.stmts_matching("INSERT INTO user_roles")
        self.assertEqual([l[1] for l in links], [(77, 1)])       # role_id=0 被跳过
        # Go 侧 handler 手写审计 + AuditMiddleware 自动审计，两条都会落库 → 这里同样双写
        self.assertEqual(len(self.audit_rows_written), 2)
        self.assertEqual(self.audit_rows_written[0][3:7],
                         ("create", "member", "kefu01", "添加成员"))
        # 第二条来自中间件：object_type 按路径推导（members→member 不在白名单 → other）
        self.assertEqual(self.audit_rows_written[1][3:7],
                         ("create", "other", "members", ""))

    def test_member_create_duplicate_username(self):
        self.existing_username = {"id": 9}
        response = self.client.post("/api/admin/members",
                                    json={"username": "kefu01", "password": "12345678",
                                          "role_ids": [1]}, headers=self.auth())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "用户名已存在")
        self.assertEqual(self.stmts_matching("INSERT INTO users"), [])

    def test_member_create_role_ids_wrong_type(self):
        response = self.client.post("/api/admin/members",
                                    json={"username": "kefu01", "password": "12345678",
                                          "role_ids": ["1"]}, headers=self.auth())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "参数错误（至少分配一个角色）")

    # ---------- 成员角色覆盖 ----------

    def test_member_roles_rejects_self(self):
        headers = self.as_subaccount(uid=5)
        response = self.client.put("/api/admin/members/5/roles",
                                   json={"role_ids": [1]}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "不能修改自己的角色")

    def test_member_roles_requires_at_least_one(self):
        headers = self.as_subaccount()
        response = self.client.put("/api/admin/members/7/roles",
                                   json={"role_ids": []}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "至少分配一个角色")

    def test_member_roles_drops_foreign_roles(self):
        headers = self.as_subaccount()
        self.user_by_id[7] = user_row(id=7)

        def hook(sql, args):
            if sql.startswith("SELECT id FROM roles WHERE tenant_id"):
                return [{"id": 1}]
            return None

        self.cursor_hook = hook
        response = self.client.put("/api/admin/members/7/roles",
                                   json={"role_ids": [1, 99]}, headers=headers)
        self.assertEqual(response.status_code, 200)
        links = self.stmts_matching("INSERT INTO user_roles")
        self.assertEqual([l[1] for l in links], [(7, 1)])        # 99 非本租户 → 丢弃
        self.assertEqual(self.audit_rows_written[0][3:6], ("update", "member", "7"))

    def test_member_roles_missing_member(self):
        headers = self.as_subaccount()
        response = self.client.put("/api/admin/members/7/roles",
                                   json={"role_ids": [1]}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "成员不存在")

    # ---------- 成员编辑 ----------

    def test_member_update_rejects_self(self):
        headers = self.as_subaccount(uid=5)
        response = self.client.put("/api/admin/members/5", json={"nickname": "x"},
                                   headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "不能修改自己的账号信息")

    def test_member_update_requires_some_field(self):
        headers = self.as_subaccount()
        response = self.client.put("/api/admin/members/7", json={}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "参数错误")

    def test_member_update_applies_only_changed_fields(self):
        headers = self.as_subaccount()
        self.user_by_id[7] = user_row(id=7, nickname="小王", phone="13800000000")
        response = self.client.put(
            "/api/admin/members/7",
            json={"nickname": "小王", "phone": "13900000000", "remark": " 新备注 "},
            headers=headers)
        self.assertEqual(response.status_code, 200)
        sql, args = self.stmts_matching("UPDATE users SET")[0]
        self.assertIn("phone = %s", sql)
        self.assertIn("remark = %s", sql)
        self.assertNotIn("nickname = %s", sql)                   # 值未变 → 不更新
        self.assertEqual(args[:2], ("13900000000", "新备注"))

    def test_member_update_username_conflict(self):
        headers = self.as_subaccount()
        self.user_by_id[7] = user_row(id=7, username="oldname")
        self.existing_username = {"id": 8}
        response = self.client.put("/api/admin/members/7", json={"username": "newname"},
                                   headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "用户名已存在")

    def test_member_update_phone_invalid(self):
        headers = self.as_subaccount()
        self.user_by_id[7] = user_row(id=7, phone="13800000000")
        response = self.client.put("/api/admin/members/7", json={"phone": "12345"},
                                   headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "手机号格式不正确")

    # ---------- 成员状态 ----------

    def test_member_status_missing_field_defaults_to_disable(self):
        headers = self.as_subaccount()
        response = self.client.put("/api/admin/members/7/status", json={}, headers=headers)
        self.assertEqual(response.status_code, 200)
        sql, args = self.writes_matching("UPDATE users SET status")[0]
        self.assertIn("deleted_at = 0", sql)
        self.assertEqual(args[0], 0)
        self.assertEqual(self.audit_rows_written[0][3:5], ("disable", "member"))

    def test_member_status_enable_action(self):
        headers = self.as_subaccount()
        self.client.put("/api/admin/members/7/status", json={"status": 1}, headers=headers)
        self.assertEqual(self.audit_rows_written[0][3], "enable")

    def test_member_status_rejects_string_and_self(self):
        headers = self.as_subaccount(uid=5)
        bad = self.client.put("/api/admin/members/7/status", json={"status": "1"},
                              headers=headers)
        self.assertEqual(bad.status_code, 400)
        mine = self.client.put("/api/admin/members/5/status", json={"status": 1},
                               headers=headers)
        self.assertEqual(mine.json()["msg"], "不能操作自己的账号")

    def test_member_status_nonexistent_member_still_ok(self):
        """Go 的 AdminMemberStatus 不校验成员是否存在：id 不存在也回 200 并写审计。"""
        headers = self.as_subaccount()
        response = self.client.put("/api/admin/members/999/status", json={"status": 1},
                                   headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["msg"], "已更新")

    def test_member_status_bad_id(self):
        headers = self.as_subaccount()
        response = self.client.put("/api/admin/members/abc/status", json={"status": 1},
                                   headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "参数错误")

    # ---------- 成员密码 ----------

    def test_member_password_short(self):
        headers = self.as_subaccount()
        response = self.client.put("/api/admin/members/7/password",
                                   json={"password": "1234567"}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "密码长度至少 8 位")

    def test_member_password_hashes_stored_username(self):
        headers = self.as_subaccount()
        self.user_by_id[7] = user_row(id=7, username="kefu02")
        response = self.client.put("/api/admin/members/7/password",
                                   json={"password": "newpass123"}, headers=headers)
        self.assertEqual(response.status_code, 200)
        sql, args = self.writes_matching("UPDATE users SET password_hash")[0]
        self.assertEqual(args[0], hashlib.sha256(
            b"t_demo|kefu02|newpass123").hexdigest())
        self.assertEqual(self.audit_rows_written[0][3:6],
                         ("reset_password", "member", "kefu02"))

    def test_member_password_missing_member(self):
        headers = self.as_subaccount()
        response = self.client.put("/api/admin/members/7/password",
                                   json={"password": "newpass123"}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "成员不存在")

    # ---------- 成员删除 ----------

    def test_member_delete_soft_deletes_with_unix_seconds(self):
        headers = self.as_subaccount()
        self.user_by_id[7] = user_row(id=7, username="kefu03")
        before = int(time.time())
        response = self.client.delete("/api/admin/members/7", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["msg"], "成员已删除")
        self.assertEqual(len(self.stmts_matching("DELETE FROM user_roles")), 1)
        sql, args = self.stmts_matching("UPDATE users SET deleted_at")[0]
        self.assertGreaterEqual(args[0], before)
        self.assertLess(args[0], before + 10)                    # unix 秒，不是毫秒
        self.assertEqual(self.audit_rows_written[0][3:6], ("delete", "member", "kefu03"))

    def test_member_delete_rejects_self(self):
        headers = self.as_subaccount(uid=5)
        response = self.client.delete("/api/admin/members/5", headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "不能删除自己的账号")

    # ---------- 角色 ----------

    def test_role_list_shape(self):
        self.roles = [{"id": 1, "tenant_id": "t_demo", "code": "admin", "name": "管理员",
                       "description": "租户全部权限",
                       "created_at": datetime(2026, 10, 20, 22, 6, 8),
                       "updated_at": datetime(2026, 10, 20, 22, 6, 8)}]
        self.role_menu_ids = {1: [1, 2]}
        self.role_perm_rows = {1: set(main.PERMISSION_CODES)}
        data = self.client.get("/api/admin/roles", headers=self.auth()).json()["data"]
        self.assertEqual(data[0]["menu_ids"], [1, 2])
        self.assertTrue(data[0]["is_admin"])
        self.assertEqual(data[0]["created_at"], "2026-10-20 22:06:08")
        self.assertEqual(set(data[0]), {"id", "tenant_id", "code", "name", "description",
                                        "menu_ids", "is_admin", "created_at", "updated_at"})

    def test_role_is_admin_requires_full_permission_set(self):
        self.roles = [{"id": 2, "tenant_id": "t_demo", "code": "staff", "name": "客服",
                       "description": "", "created_at": datetime(2026, 10, 20, 22, 6, 8),
                       "updated_at": datetime(2026, 10, 20, 22, 6, 8)}]
        self.role_perm_rows = {2: set(main.PERMISSION_CODES) - {"audit:view"}}
        data = self.client.get("/api/admin/roles", headers=self.auth()).json()["data"]
        self.assertFalse(data[0]["is_admin"])

    def test_role_list_seeds_builtin_roles_for_fresh_tenant(self):
        self.roles = []
        hook_rows = {"SELECT COUNT(*) AS c FROM roles": {"c": 0},
                     "SELECT rbac_seeded FROM tenants": {"rbac_seeded": 0}}
        menus = [{"id": 1}, {"id": 2}]

        def hook(sql, args):
            for needle, value in hook_rows.items():
                if needle in sql:
                    return value
            if sql.startswith("SELECT id FROM menus"):
                return menus
            return None

        self.cursor_hook = hook
        self.client.get("/api/admin/roles", headers=self.auth())
        inserts = [s for s in self.statements if s[0].startswith("INSERT INTO roles")]
        self.assertEqual(len(inserts), 2)                        # admin + staff
        self.assertEqual(inserts[0][1][1], "admin")
        self.assertEqual(inserts[1][1][1], "staff")
        self.assertEqual(len(self.stmts_matching("INSERT INTO sys_role_menu")), 4)
        self.assertEqual(len(self.stmts_matching("UPDATE tenants SET rbac_seeded")), 1)

    def test_role_create_success(self):
        # 租户已有角色 → EnsureTenantRoles 只读跳过，不会混入种子角色
        self.cursor_hook = lambda sql, args: (
            {"c": 2} if "SELECT COUNT(*) AS c FROM roles" in sql else None)
        response = self.client.post("/api/admin/roles",
                                    json={"name": " 客服组长 ", "description": "带班",
                                          "menu_ids": [1, 2]}, headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["msg"], "角色已创建")
        self.assertEqual(response.json()["data"], {"id": 901})
        insert = self.stmts_matching("INSERT INTO roles")[0]
        self.assertTrue(insert[1][1].startswith("custom_"))
        self.assertEqual(len(insert[1][1]), len("custom_") + 12)
        self.assertEqual(insert[1][2], "客服组长")               # name 去空格
        self.assertEqual([s[1] for s in self.stmts_matching("INSERT INTO sys_role_menu")],
                         [(901, 1), (901, 2)])
        self.assertEqual(self.audit_rows_written[0][3:6], ("create", "role", "客服组长"))

    def test_role_create_requires_name(self):
        response = self.client.post("/api/admin/roles", json={"name": "  "},
                                    headers=self.auth())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "参数错误")

    def test_role_create_duplicate_menu_ids_fails(self):
        """Go CreateRole 不去重：重复 menu_id 撞主键 → 500（照抄）。"""

        def hook(sql, args):
            if sql.startswith("INSERT INTO sys_role_menu") and args[1] == 1:
                raise main.pymysql.MySQLError("Duplicate entry")
            return None

        self.cursor_hook = hook
        response = self.client.post("/api/admin/roles", json={"name": "重复", "menu_ids": [1, 1]},
                                    headers=self.auth())
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["msg"], "创建角色失败")
        self.assertEqual(self.audit_rows_written, [])

    def test_role_update_tenant_mismatch(self):
        self.role_by_id[3] = {"id": 3, "tenant_id": "t_other", "name": "别人的角色"}
        response = self.client.put("/api/admin/roles/3", json={"name": "改"},
                                   headers=self.auth())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "角色不存在")

    def test_role_update_dedups_menu_ids(self):
        self.role_by_id[3] = {"id": 3, "tenant_id": "t_demo", "name": "旧名"}
        response = self.client.put("/api/admin/roles/3",
                                   json={"name": "新名", "menu_ids": [2, 2, 0]},
                                   headers=self.auth())
        self.assertEqual(response.status_code, 200)
        inserts = [s[1] for s in self.stmts_matching("INSERT INTO sys_role_menu")]
        self.assertEqual(inserts, [(3, 2)])                      # 去重 + 跳过 0
        self.assertEqual(self.audit_rows_written[0][3:6], ("update", "role", "旧名"))

    def test_role_delete_unlinks_members_and_soft_deletes(self):
        self.role_by_id[3] = {"id": 3, "tenant_id": "t_demo", "name": "临时角色"}
        before = int(time.time())
        response = self.client.delete("/api/admin/roles/3", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["msg"], "角色已删除")
        self.assertEqual(len(self.stmts_matching("DELETE FROM user_roles WHERE role_id")), 1)
        self.assertEqual(len(self.stmts_matching("DELETE FROM sys_role_menu")), 1)
        sql, args = self.stmts_matching("UPDATE roles SET deleted_at")[0]
        self.assertGreaterEqual(args[0], before)
        self.assertEqual(self.audit_rows_written[0][3:6], ("delete", "role", "临时角色"))

    def test_role_delete_missing_role(self):
        response = self.client.delete("/api/admin/roles/3", headers=self.auth())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["msg"], "角色不存在")

    def test_role_endpoints_require_permissions(self):
        headers = self.as_subaccount(perms=("member:view",))
        self.assertEqual(self.client.get("/api/admin/roles", headers=headers).status_code,
                         403)
        self.assertEqual(self.client.post("/api/admin/roles", json={"name": "x"},
                                          headers=headers).status_code, 403)
        self.assertEqual(self.client.put("/api/admin/roles/1", json={"name": "x"},
                                         headers=headers).status_code, 403)
        self.assertEqual(self.client.delete("/api/admin/roles/1",
                                            headers=headers).status_code, 403)


if __name__ == "__main__":
    unittest.main()
