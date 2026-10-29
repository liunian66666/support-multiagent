"""Role-based access control, members and permission menus."""

import hashlib
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime

import pymysql
from fastapi import APIRouter, Depends, Request

from business.admin.auth import (Admin, current_admin, require_perm,
                                 user_perms, write_handler_audit)
from business.admin.catalog import go_parse_uint, to_int
from business.core.errors import deny
from business.core.requests import body_json
from business.customer.routes import iso
from business.infra.storage import db, many, one, write

router = APIRouter()

# ==================== RBAC（菜单树 / 成员 / 角色） ====================
# 数据模型：menus（菜单=权限载体）→ sys_role_menu（角色-菜单）→ roles → user_roles → users。
# 权限码只从 menus.perm（status=1）推导，角色「是否管理员」也只看权限集合是否覆盖全目录
# （isFullPerms），与角色名/内置标记解耦 —— admin 角色改名不影响权限判定。

MENU_BIZ_GROUP = "业务中心"
MENU_SYS_GROUP = "系统管理"
ZERO_TIME_RFC3339 = "0001-01-01T00:00:00Z"   # 非指针时间的零值（DB NULL）

# 权限目录（仅取 code 用于 isFullPerms，名称/分组只作可读注释）
PERMISSION_CODES = (
    "session:view", "session:profile:edit", "session:cleanup",
    "ticket:view", "ticket:handle",
    "customer:view",
    "dashboard:view",
    "knowledge:view", "knowledge:add", "knowledge:edit", "knowledge:remove",
    "prompt:view", "prompt:add", "prompt:edit", "prompt:remove",
    "model:view", "model:edit",
    "usage:view", "audit:view",
    "quickreply:view", "quickreply:add", "quickreply:edit", "quickreply:remove",
    "rating:view",
    "member:view", "member:add", "member:edit", "member:remove",
    "role:view", "role:add", "role:edit", "role:remove",
)

# 行为约定：内置「客服」角色的初始授权集合
STAFF_PERMS = (
    "session:view", "session:profile:edit",
    "ticket:view", "ticket:handle",
    "customer:view",
    "dashboard:view",
    "knowledge:view",
    "quickreply:view",
    "rating:view",
)


@contextmanager
def transaction():
    """显式事务（db() 是 autocommit，这里 begin/commit 覆盖）。"""
    conn = db()
    try:
        conn.begin()
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def iso_zero(value):
    """非指针时间：DB NULL 读成零值，序列化为 0001-01-01T00:00:00Z。"""
    return iso(value) if value else ZERO_TIME_RFC3339


def go_time_text(value):
    """时间格式化：角色列表的时间字段用 `2006-01-02 15:04:05` 格式。"""
    if not value:
        return "0001-01-01 00:00:00"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def go_uints(value):
    """无符号整数数组的 JSON 绑定语义：非数组 / 含负值 / 含非整数 → None（绑定失败）。"""
    if not isinstance(value, list):
        return None
    out = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            return None
        out.append(item)
    return out


def go_int_field(body, key, default=0):
    """整数字段的绑定语义：缺失取 0；存在但不是整数 → None（绑定失败）。"""
    if key not in body:
        return default
    value = body[key]
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def rune_len(text):
    """按 Unicode 码点计数（等价于 Python 的 len(str)）。"""
    return len(text)


# ---------- 菜单 ----------

MENU_COLUMNS = ("id, parent_id, group_name, name, type, path, component, perm, icon, sort, "
                "visible, status, is_builtin, created_at, updated_at")


def all_menus():
    """行为约定：group_name ASC, sort ASC, id ASC（权限目录顺序依赖它）。"""
    return many(f"SELECT {MENU_COLUMNS} FROM menus ORDER BY group_name ASC, sort ASC, id ASC")


def menu_json(row):
    return {
        "id": row["id"], "parent_id": row["parent_id"] or 0,
        "group_name": row["group_name"] or "", "name": row["name"] or "",
        "type": row["type"], "path": row["path"] or "", "component": row["component"] or "",
        "perm": row["perm"] or None, "icon": row["icon"] or "",
        "sort": row["sort"] or 0, "visible": row["visible"], "status": row["status"],
        "is_builtin": row["is_builtin"],
        "created_at": iso_zero(row["created_at"]), "updated_at": iso_zero(row["updated_at"]),
    }


def menu_tree(perms, scope):
    """用户菜单树（含子账号可见集合与父链保留）。

    scope：platform（平台管理员→系统管理组）/ tenant-admin（租户 app_key→业务中心全量）/
    tenant-user（租户子账号→业务中心按权限过滤）/ all（无租户上下文的演示模式）。
    """
    active = [m for m in all_menus() if m["status"] == 1]
    if scope == "platform":
        scoped = [m for m in active if m["group_name"] == MENU_SYS_GROUP]
    elif scope in ("tenant-admin", "tenant-user"):
        scoped = [m for m in active if m["group_name"] == MENU_BIZ_GROUP]
    else:
        scoped = active
    if not scoped:
        return None                      # 空值返回 JSON null

    visible = set()
    if scope == "tenant-user":
        by_id = {m["id"]: m for m in scoped}

        def mark(menu_id):
            if menu_id in visible:
                return
            visible.add(menu_id)
            parent = (by_id.get(menu_id) or {}).get("parent_id") or 0
            if parent > 0:
                mark(parent)

        for m in scoped:
            perm = m["perm"]
            # type != 1：目录节点不参与匹配，只由命中的叶子节点反向补齐父链
            if m["status"] == 1 and m["type"] != 1 and perm and perm in perms:
                mark(m["id"])

    def keep(m):
        return scope != "tenant-user" or m["id"] in visible

    group_order = []
    for m in scoped:
        if keep(m) and m["group_name"] not in group_order:
            group_order.append(m["group_name"])

    nodes = {g: [] for g in group_order}
    for m in scoped:
        if keep(m):
            nodes[m["group_name"]].append(menu_json(m))

    groups = []
    for g in group_order:
        by_id = {n["id"]: n for n in nodes[g]}
        roots = []
        for node in nodes[g]:
            parent = node["parent_id"]
            if parent > 0 and parent in by_id:
                by_id[parent].setdefault("children", []).append(node)
                continue
            roots.append(node)
        groups.append({"title": g, "group": g, "children": roots or None})
    return groups


@router.get("/api/admin/menus/tree")
def admin_menu_tree(admin: Admin = Depends(current_admin)):
    """侧边栏/动态路由数据源：按登录身份隔离（平台/租户管理员/子账号三种视图）。"""
    if admin.is_platform_admin:
        scope, perms = "platform", []
    elif admin.user_id == 0:
        scope, perms = "tenant-admin", []
    else:
        scope, perms = "tenant-user", user_perms(admin.user_id)
    return {"code": 0, "data": menu_tree(perms, scope)}


@router.get("/api/admin/permissions")
def admin_permission_catalog(admin: Admin = Depends(require_perm("role:view"))):
    """权限目录：从 menus 表推导（菜单=权限载体），与菜单管理页始终一致。"""
    items = [{"code": m["perm"], "name": m["name"] or "", "group": m["group_name"] or ""}
             for m in all_menus() if m["perm"]]
    return {"code": 0, "data": items}


# ---------- 角色 ----------

def role_menu_ids(role_id):
    rows = many("SELECT menu_id FROM sys_role_menu WHERE role_id = %s", (role_id,))
    return [r["menu_id"] for r in rows]


def role_perms(role_id):
    """对齐 PermsOfMenuIDs(RoleMenuIDs(role_id))：只取启用且 perm 非空的菜单。"""
    rows = many(
        "SELECT DISTINCT m.perm FROM sys_role_menu rm JOIN menus m ON m.id = rm.menu_id "
        "WHERE rm.role_id = %s AND m.status = 1 AND m.perm IS NOT NULL AND m.perm <> ''",
        (role_id,))
    return {r["perm"] for r in rows}


def is_full_perms(perms):
    if not perms:
        return False
    return all(code in perms for code in PERMISSION_CODES)


def is_admin_role(role_id):
    return is_full_perms(role_perms(role_id))


def load_role(role_id):
    return one("SELECT id, tenant_id, code, name, description, created_at, updated_at "
               "FROM roles WHERE id = %s AND deleted_at = 0 LIMIT 1", (role_id,))


def role_json(row):
    return {
        "id": row["id"], "tenant_id": row["tenant_id"], "code": row["code"] or "",
        "name": row["name"] or "", "description": row["description"] or "",
        "menu_ids": role_menu_ids(row["id"]), "is_admin": is_admin_role(row["id"]),
        "created_at": go_time_text(row["created_at"]),
        "updated_at": go_time_text(row["updated_at"]),
    }


def ensure_tenant_roles(tenant_id):
    """行为约定：一次性种入 admin/staff 预置角色。

    语义要点（新项目口径，不做存量兼容）：
    - 已有角色 → 直接跳过（预置角色可删可改，删光也不补种）
    - rbac_seeded=1 且无角色 → 同样跳过
    - 只有「从未初始化过」的租户才会写入，因此本函数对既有租户是纯读
    """
    now = datetime.now()
    with transaction() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM roles WHERE tenant_id = %s AND deleted_at = 0",
                    (tenant_id,))
        if (cur.fetchone() or {}).get("c", 0) > 0:
            return
        cur.execute("SELECT rbac_seeded FROM tenants WHERE tenant_id = %s AND deleted_at = 0 "
                    "ORDER BY id ASC LIMIT 1 FOR UPDATE", (tenant_id,))
        tenant_row = cur.fetchone()
        locked = tenant_row is not None
        if locked and tenant_row["rbac_seeded"]:
            return
        builtins = (("admin", "管理员", "租户全部权限"),
                    ("staff", "客服", "会话/工单/客户/运营看板/知识库查看"))
        for code, name, description in builtins:
            cur.execute(
                "INSERT INTO roles (tenant_id, code, name, description, created_at, updated_at, "
                "deleted_at) VALUES (%s, %s, %s, %s, %s, %s, 0)",
                (tenant_id, code, name, description, now, now))
            role_id = cur.lastrowid
            if code == "admin":
                cur.execute("SELECT id FROM menus WHERE group_name = %s ORDER BY id ASC",
                            (MENU_BIZ_GROUP,))
            else:
                holders = ", ".join(["%s"] * len(STAFF_PERMS))
                cur.execute(f"SELECT id FROM menus WHERE perm IN ({holders}) AND status = 1 "
                            f"ORDER BY id ASC", STAFF_PERMS)
            for menu_row in cur.fetchall():
                cur.execute("INSERT INTO sys_role_menu (role_id, menu_id) VALUES (%s, %s)",
                            (role_id, menu_row["id"]))
        if locked:
            cur.execute("UPDATE tenants SET rbac_seeded = 1 WHERE tenant_id = %s", (tenant_id,))


def gen_role_code():
    """租户内唯一角色编码：custom_<12 位 hex>（创建后不可改）。"""
    return "custom_" + secrets.token_hex(6)


def set_role_menus(cur, role_id, menu_ids):
    cur.execute("DELETE FROM sys_role_menu WHERE role_id = %s", (role_id,))
    seen = set()
    for menu_id in menu_ids:
        if menu_id > 0 and menu_id not in seen:
            seen.add(menu_id)
            cur.execute("INSERT INTO sys_role_menu (role_id, menu_id) VALUES (%s, %s)",
                        (role_id, menu_id))


@router.get("/api/admin/roles")
def admin_roles(admin: Admin = Depends(require_perm("role:view"))):
    try:
        ensure_tenant_roles(admin.tenant_id)
    except pymysql.MySQLError as exc:
        deny(500, "角色初始化失败: " + str(exc))
    rows = many("SELECT id, tenant_id, code, name, description, created_at, updated_at "
                "FROM roles WHERE tenant_id = %s AND deleted_at = 0 ORDER BY id ASC",
                (admin.tenant_id,))
    return {"code": 0, "data": [role_json(row) for row in rows]}


@router.post("/api/admin/roles")
async def admin_role_create(request: Request, admin: Admin = Depends(require_perm("role:add"))):
    body = await body_json(request)
    if not isinstance(body, dict):
        deny(400, "参数错误")
    name = body.get("name")
    menu_ids = go_uints(body.get("menu_ids") or [])
    if not isinstance(name, str) or not name.strip() or menu_ids is None:
        deny(400, "参数错误")
    if body.get("description") is not None and not isinstance(body.get("description"), str):
        deny(400, "参数错误")
    description = body.get("description") or ""
    # 已修复的历史缺陷：曾写死 Code 生成逻辑，请求体里的 code 被直接丢弃，
    # 调用方无法指定稳定编码（对接外部系统时需要按 code 引用角色）。
    # 这里放开：传了就用（限长 32，与 roles.code 列宽一致），没传才自动生成。
    code = body.get("code")
    if code is not None and (not isinstance(code, str) or not code.strip()):
        deny(400, "参数错误")
    if isinstance(code, str):
        code = code.strip()
        if len(code) > 32:
            deny(400, "角色编码长度不能超过 32 个字符")
    try:
        ensure_tenant_roles(admin.tenant_id)
    except pymysql.MySQLError:
        deny(500, "角色初始化失败")
    role_name = name.strip()
    try:
        with transaction() as cur:
            now = datetime.now()
            cur.execute(
                "INSERT INTO roles (tenant_id, code, name, description, created_at, updated_at, "
                "deleted_at) VALUES (%s, %s, %s, %s, %s, %s, 0)",
                (admin.tenant_id, code or gen_role_code(), role_name, description, now, now))
            role_id = cur.lastrowid
            # 注意：创建角色不去重，重复 menu_id 会撞主键 → 整体失败（保持该行为）
            for menu_id in menu_ids:
                cur.execute("INSERT INTO sys_role_menu (role_id, menu_id) VALUES (%s, %s)",
                            (role_id, menu_id))
    except pymysql.MySQLError:
        deny(500, "创建角色失败")
    try:
        write_handler_audit(admin, request, "create", "role", role_name, "新建角色")
    except Exception:
        pass
    return {"code": 0, "msg": "角色已创建", "data": {"id": role_id}}


@router.put("/api/admin/roles/{id}")
async def admin_role_update(id: str, request: Request,
                            admin: Admin = Depends(require_perm("role:edit"))):
    body = await body_json(request)
    if not isinstance(body, dict):
        body = {}
    name = body.get("name")
    role_id = go_parse_uint(id)
    if (not isinstance(name, str) or not name.strip() or role_id == 0
            or body.get("description") is not None and not isinstance(body.get("description"), str)):
        deny(400, "参数错误")
    menu_ids = go_uints(body.get("menu_ids") or [])
    if menu_ids is None:
        deny(400, "参数错误")
    role = load_role(role_id)
    if not role or role["tenant_id"] != admin.tenant_id:
        deny(400, "角色不存在")
    try:
        with transaction() as cur:
            cur.execute("UPDATE roles SET name = %s, description = %s, updated_at = %s "
                        "WHERE id = %s", (name.strip(), body.get("description") or "",
                                          datetime.now(), role_id))
            set_role_menus(cur, role_id, menu_ids)
    except pymysql.MySQLError:
        deny(500, "更新角色失败")
    try:
        write_handler_audit(admin, request, "update", "role", role["name"] or "",
                            "更新角色权限")
    except Exception:
        pass
    return {"code": 0, "msg": "角色已更新"}


@router.delete("/api/admin/roles/{id}")
def admin_role_delete(id: str, request: Request,
                      admin: Admin = Depends(require_perm("role:remove"))):
    role_id = go_parse_uint(id)
    if role_id == 0:
        deny(400, "参数错误")
    role = load_role(role_id)
    if not role or role["tenant_id"] != admin.tenant_id:
        deny(400, "角色不存在")
    try:
        with transaction() as cur:
            # 成员引用自动解绑（成员保留，只是失去该角色权限）+ 清菜单授权 + 软删角色
            cur.execute("DELETE FROM user_roles WHERE role_id = %s", (role_id,))
            cur.execute("DELETE FROM sys_role_menu WHERE role_id = %s", (role_id,))
            cur.execute("UPDATE roles SET deleted_at = %s WHERE id = %s",
                        (int(time.time()), role_id))
    except pymysql.MySQLError as exc:
        deny(400, str(exc))
    try:
        write_handler_audit(admin, request, "delete", "role", role["name"] or "", "删除角色")
    except Exception:
        pass
    return {"code": 0, "msg": "角色已删除"}


# ---------- 成员 ----------

USER_COLUMNS = ("id, tenant_id, username, nickname, phone, email, remark, status, "
                "last_login_at, created_at, updated_at")


def hash_user_password(tenant_id, username, password):
    """行为约定：sha256(tenant_id|username|password) hex。"""
    return hashlib.sha256(f"{tenant_id}|{username}|{password}".encode()).hexdigest()


def get_user_roles(user_id):
    """成员角色摘要（多角色权限取并集）。顺序沿用 user_roles 主键顺序（role_id ASC）。"""
    rels = many("SELECT user_id, role_id FROM user_roles WHERE user_id = %s", (user_id,))
    items = []
    for rel in rels:
        role = one("SELECT id, name, code FROM roles WHERE id = %s AND deleted_at = 0 LIMIT 1",
                   (rel["role_id"],))
        if not role:
            continue
        items.append({"id": role["id"], "name": role["name"] or "", "code": role["code"] or "",
                      "is_admin": is_admin_role(role["id"])})
    return items


def user_json(row):
    return {
        "id": row["id"], "tenant_id": row["tenant_id"], "username": row["username"] or "",
        "nickname": row["nickname"] or "", "phone": row["phone"] or "",
        "email": row["email"] or "", "remark": row["remark"] or "", "status": row["status"],
        "last_login_at": iso(row["last_login_at"]),
        "created_at": iso_zero(row["created_at"]), "updated_at": iso_zero(row["updated_at"]),
        "roles": get_user_roles(row["id"]),
    }


def load_user(user_id):
    return one(f"SELECT {USER_COLUMNS} FROM users WHERE id = %s AND deleted_at = 0 LIMIT 1",
               (user_id,))


def set_user_roles(cur, tenant_id, user_id, role_ids):
    """覆盖式设置成员角色；只接受归属本租户的角色（其余静默丢弃）。"""
    cur.execute("SELECT id FROM roles WHERE tenant_id = %s AND deleted_at = 0", (tenant_id,))
    valid = {r["id"] for r in cur.fetchall()}
    cur.execute("DELETE FROM user_roles WHERE user_id = %s", (user_id,))
    for role_id in role_ids:
        if role_id > 0 and role_id in valid:
            cur.execute("INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)",
                        (user_id, role_id))


@router.get("/api/admin/members")
def admin_members(q: str = "", role_id: str = "", status: str = "",
                  admin: Admin = Depends(require_perm("member:view"))):
    try:
        ensure_tenant_roles(admin.tenant_id)
    except pymysql.MySQLError:
        deny(500, "角色初始化失败")
    keyword = (q or "").strip()
    role_filter = go_parse_uint(role_id)
    status_filter = -1
    raw_status = (status or "").strip()
    if raw_status:
        parsed = to_int(raw_status, None)
        if parsed in (0, 1):
            status_filter = parsed
    where = "tenant_id = %s AND deleted_at = 0"
    args = [admin.tenant_id]
    if keyword:
        where += " AND username LIKE %s"
        args.append("%" + keyword + "%")
    if role_filter > 0:
        where += " AND id IN (SELECT user_id FROM user_roles WHERE role_id = %s)"
        args.append(role_filter)
    if status_filter >= 0:
        where += " AND status = %s"
        args.append(status_filter)
    rows = many(f"SELECT {USER_COLUMNS} FROM users WHERE {where} ORDER BY id ASC", tuple(args))
    return {"code": 0, "data": [user_json(row) for row in rows]}


@router.post("/api/admin/members")
async def admin_member_create(request: Request,
                              admin: Admin = Depends(require_perm("member:add"))):
    body = await body_json(request)
    if not isinstance(body, dict):
        body = {}
    username = body.get("username")
    password = body.get("password")
    role_ids = go_uints(body.get("role_ids") or [])
    if (not isinstance(username, str) or not isinstance(password, str) or not role_ids
            or role_ids is None):
        deny(400, "参数错误（至少分配一个角色）")
    nickname = body.get("nickname")
    phone = body.get("phone")
    email = body.get("email")
    remark = body.get("remark")
    for value in (nickname, phone, email, remark):
        if value is not None and not isinstance(value, str):
            deny(400, "参数错误（至少分配一个角色）")
    if rune_len(username) < 3 or rune_len(username) > 32:
        deny(400, "用户名长度需 3-32 个字符")
    if phone and not re.fullmatch(r"1[3-9]\d{9}", phone):
        deny(400, "手机号格式不正确")
    if email and "@" not in email:
        deny(400, "邮箱格式不正确")
    if len(password) < 8:
        deny(400, "密码长度至少 8 位")
    try:
        ensure_tenant_roles(admin.tenant_id)
    except pymysql.MySQLError:
        deny(500, "角色初始化失败")
    if one("SELECT id FROM users WHERE tenant_id = %s AND username = %s AND deleted_at = 0 "
           "LIMIT 1", (admin.tenant_id, username)):
        deny(400, "用户名已存在")
    try:
        with transaction() as cur:
            now = datetime.now()
            cur.execute(
                "INSERT INTO users (tenant_id, username, password_hash, nickname, phone, email, "
                "remark, status, created_at, updated_at, deleted_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s, 0)",
                (admin.tenant_id, username, hash_user_password(admin.tenant_id, username, password),
                 (nickname or "").strip(), (phone or "").strip(), (email or "").strip(),
                 (remark or "").strip(), now, now))
            user_id = cur.lastrowid
            for role_id in role_ids:
                if role_id:
                    cur.execute("INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)",
                                (user_id, role_id))
    except pymysql.MySQLError:
        deny(500, "创建成员失败")
    try:
        write_handler_audit(admin, request, "create", "member", username, "添加成员")
    except Exception:
        pass
    return {"code": 0, "msg": "成员已创建", "data": {"id": user_id}}


@router.put("/api/admin/members/{id}/roles")
async def admin_member_roles(id: str, request: Request,
                             admin: Admin = Depends(require_perm("member:edit"))):
    user_id = go_parse_uint(id)
    if user_id == 0:
        deny(400, "参数错误")
    if user_id == admin.user_id:
        deny(400, "不能修改自己的角色")
    body = await body_json(request)
    role_ids = go_uints((body or {}).get("role_ids") or []) if isinstance(body, dict) else None
    if not role_ids:
        deny(400, "至少分配一个角色")
    user = load_user(user_id)
    if not user or user["tenant_id"] != admin.tenant_id:
        deny(400, "成员不存在")
    try:
        with transaction() as cur:
            set_user_roles(cur, admin.tenant_id, user_id, role_ids)
    except pymysql.MySQLError:
        deny(500, "更新角色失败")
    try:
        write_handler_audit(admin, request, "update", "member", str(user_id), "变更成员角色")
    except Exception:
        pass
    return {"code": 0, "msg": "角色已更新"}


@router.put("/api/admin/members/{id}")
async def admin_member_update(id: str, request: Request,
                              admin: Admin = Depends(require_perm("member:edit"))):
    user_id = go_parse_uint(id)
    if user_id == 0:
        deny(400, "参数错误")
    if user_id == admin.user_id:
        deny(400, "不能修改自己的账号信息")
    body = await body_json(request)
    if not isinstance(body, dict):
        deny(400, "参数错误")
    username = body.get("username")
    nickname = body.get("nickname")
    phone = body.get("phone")
    email = body.get("email")
    remark = body.get("remark")
    role_ids = go_uints(body.get("role_ids") or [])
    if role_ids is None:
        deny(400, "参数错误")
    for value in (username, nickname, phone, email, remark):
        if value is not None and not isinstance(value, str):
            deny(400, "参数错误")
    username, nickname, phone = username or "", nickname or "", phone or ""
    email, remark = email or "", remark or ""
    if (not username.strip() and not nickname and not phone and not email and not remark
            and not role_ids):
        deny(400, "参数错误")
    user = load_user(user_id)
    if not user or user["tenant_id"] != admin.tenant_id:
        deny(400, "成员不存在")

    updates = {}
    if username.strip() and username != (user["username"] or ""):
        name = username.strip()
        if rune_len(name) < 3 or rune_len(name) > 32:
            deny(400, "用户名长度需 3-32 个字符")
        exist = one("SELECT id FROM users WHERE tenant_id = %s AND username = %s "
                    "AND deleted_at = 0 LIMIT 1", (admin.tenant_id, name))
        if exist and exist["id"] != user_id:
            deny(400, "用户名已存在")
        updates["username"] = name
    if nickname and nickname != (user["nickname"] or ""):
        updates["nickname"] = nickname.strip()
    if phone and phone != (user["phone"] or ""):
        if not re.fullmatch(r"1[3-9]\d{9}", phone):
            deny(400, "手机号格式不正确")
        updates["phone"] = phone.strip()
    if email and email != (user["email"] or ""):
        if "@" not in email:
            deny(400, "邮箱格式不正确")
        updates["email"] = email.strip()
    if remark and remark != (user["remark"] or ""):
        updates["remark"] = remark.strip()

    try:
        with transaction() as cur:
            if updates:
                sets = ", ".join(f"{key} = %s" for key in updates)
                cur.execute(f"UPDATE users SET {sets}, updated_at = %s WHERE id = %s "
                            f"AND tenant_id = %s AND deleted_at = 0",
                            (*updates.values(), datetime.now(), user_id, admin.tenant_id))
            if role_ids:
                set_user_roles(cur, admin.tenant_id, user_id, role_ids)
    except pymysql.MySQLError:
        deny(500, "更新成员信息失败")
    try:
        write_handler_audit(admin, request, "update", "member", str(user_id), "编辑成员信息")
    except Exception:
        pass
    return {"code": 0, "msg": "成员已更新"}


@router.put("/api/admin/members/{id}/status")
async def admin_member_status(id: str, request: Request,
                              admin: Admin = Depends(require_perm("member:edit"))):
    user_id = go_parse_uint(id)
    if user_id == 0:
        deny(400, "参数错误")
    if user_id == admin.user_id:
        deny(400, "不能操作自己的账号")
    body = await body_json(request)
    if not isinstance(body, dict):
        body = {}
    status = go_int_field(body, "status", 0)
    if status not in (0, 1):
        deny(400, "参数错误")
    # 注意：这里不校验成员是否存在，id 不存在也会回 200「已更新」并记审计（照抄）
    try:
        write("UPDATE users SET status = %s, updated_at = %s WHERE id = %s AND tenant_id = %s "
              "AND deleted_at = 0", (status, datetime.now(), user_id, admin.tenant_id))
    except pymysql.MySQLError:
        deny(500, "更新失败")
    try:
        write_handler_audit(admin, request, "enable" if status == 1 else "disable", "member",
                            str(user_id), "变更成员状态")
    except Exception:
        pass
    return {"code": 0, "msg": "已更新"}


@router.put("/api/admin/members/{id}/password")
async def admin_member_password(id: str, request: Request,
                                admin: Admin = Depends(require_perm("member:edit"))):
    user_id = go_parse_uint(id)
    if user_id == 0:
        deny(400, "参数错误")
    body = await body_json(request)
    password = (body or {}).get("password") if isinstance(body, dict) else None
    if not isinstance(password, str) or len(password) < 8:
        deny(400, "密码长度至少 8 位")
    user = load_user(user_id)
    if not user or user["tenant_id"] != admin.tenant_id:
        deny(400, "成员不存在")
    try:
        write("UPDATE users SET password_hash = %s, updated_at = %s WHERE id = %s "
              "AND tenant_id = %s AND deleted_at = 0",
              (hash_user_password(admin.tenant_id, user["username"], password),
               datetime.now(), user_id, admin.tenant_id))
    except pymysql.MySQLError:
        deny(500, "重置失败")
    try:
        write_handler_audit(admin, request, "reset_password", "member", user["username"] or "",
                            "重置成员密码")
    except Exception:
        pass
    return {"code": 0, "msg": "密码已重置"}


@router.delete("/api/admin/members/{id}")
def admin_member_delete(id: str, request: Request,
                        admin: Admin = Depends(require_perm("member:remove"))):
    user_id = go_parse_uint(id)
    if user_id == 0:
        deny(400, "参数错误")
    if user_id == admin.user_id:
        deny(400, "不能删除自己的账号")
    user = load_user(user_id)
    if not user or user["tenant_id"] != admin.tenant_id:
        deny(400, "成员不存在")
    try:
        with transaction() as cur:
            cur.execute("DELETE FROM user_roles WHERE user_id = %s", (user_id,))
            # 软删除：deleted_at 存 unix 秒（与该表其它软删除记录一致）
            cur.execute("UPDATE users SET deleted_at = %s WHERE id = %s AND tenant_id = %s",
                        (int(time.time()), user_id, admin.tenant_id))
    except pymysql.MySQLError:
        deny(500, "删除失败")
    try:
        write_handler_audit(admin, request, "delete", "member", user["username"] or "", "删除成员")
    except Exception:
        pass
    return {"code": 0, "msg": "成员已删除"}



