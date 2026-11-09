"""Tenant administration, model providers and platform menus."""

import hashlib
import json
import secrets
import threading
import time
from datetime import datetime, timezone

import pymysql
from fastapi import APIRouter, Depends, Query, Request

from business.admin.auth import Admin, current_admin, write_handler_audit
from business.admin.catalog import go_parse_uint
from business.admin.ops import (agent_call, get_business_model_config,
                                model_update_scope, require_platform,
                                update_model_config)
from business.admin.rbac import (go_time_text, go_uints, iso_zero,
                                 transaction)
from business.core.errors import deny, soft
from business.core.requests import body_json
from business.customer.routes import iso
from business.infra.runtime import ws_hub
from business.infra.storage import cache, many, one, write

router = APIRouter()

# ==================== 平台域（租户 / 模型提供商 / 菜单） ====================
# 全部要求平台管理员权限：非平台管理员一律 403「仅平台管理员可操作」。
# 本域有两套错误语义，不能混：
#   - 租户 / 模型提供商：软失败（HTTP 200 + body code），审计中间件仍记一条
#   - 菜单：真失败（HTTP 400/500），审计中间件跳过
# 因此前者用 soft()，后者用 deny()，保证 audit_logs 与响应语义一致。


def go_internal(method, path, body=None, timeout=5):
    """Compatibility hook retained for older contract tests; no gateway call."""
    return None


def reload_go_menu_caches():
    """Menus and permissions are queried from MySQL on each request."""
    return


def reload_go_tenants():
    """Authentication reads current tenant state from MySQL; no cache reload."""
    return


# ---------- 租户 ----------

TENANT_COLUMNS = (
    "id, tenant_id, name, app_key, is_platform_admin, status, created_at, "
    "model_provider, model_provider_key, llm_provider_id, model_name, model_api_base, "
    "chat_input_cache_price, chat_input_price, chat_output_price, embedding_price, "
    "embedding_provider, embedding_provider_key, embedding_provider_id, "
    "embedding_model_name, embedding_api_base, "
    "max_concurrent_sessions, daily_message_limit, max_kb_docs, max_kb_size_mb, "
    "memory_enabled"
)


def utc_day_start_local():
    """按 UTC 日界取整的等价逻辑。

    注意这里按「Unix 纪元起的绝对时间」取整，不是本地 00:00 ——
    容器 TZ=Asia/Shanghai 时实际得到**本地 08:00**。保持该行为，否则「今日消息数」
    在每天 0~8 点之间会相差一整天。
    """
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                  microsecond=0)
    return midnight.astimezone().replace(tzinfo=None)


def tenant_usage_map():
    """行为约定：MySQL 今日消息 → Agent 用量 → ws Hub 连接数。"""
    out = {}
    try:
        rows = many(
            "SELECT tenant_id, COUNT(*) AS cnt FROM messages "
            "WHERE role = 'user' AND created_at >= %s GROUP BY tenant_id",
            (utc_day_start_local(),))
    except pymysql.MySQLError:
        rows = []
    for r in rows:
        out[r["tenant_id"]] = {"active_sessions": 0, "today_messages": int(r["cnt"]),
                               "kb_docs": 0, "kb_size_mb": 0.0}

    data, _err = agent_call("GET", "/agent/tenant-usage", trace_id="tenant-usage")
    if data is not None:
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("code") == 0:
            for tid, u in (parsed.get("data") or {}).items():
                cur = out.setdefault(tid, {"active_sessions": 0, "today_messages": 0,
                                           "kb_docs": 0, "kb_size_mb": 0.0})
                cur["active_sessions"] = int(u.get("active_sessions") or 0)
                cur["kb_docs"] = int(u.get("kb_docs") or 0)
                cur["kb_size_mb"] = float(u.get("kb_size_bytes") or 0) / 1024 / 1024

    # 并发会话以 ws Hub 实时连接数为准（与配额校验同口径）
    conns = {"code": 0, "data": ws_hub.count_all_tenants()}
    if isinstance(conns, dict) and conns.get("code") == 0:
        for tid, n in (conns.get("data") or {}).items():
            cur = out.setdefault(tid, {"active_sessions": 0, "today_messages": 0,
                                       "kb_docs": 0, "kb_size_mb": 0.0})
            cur["active_sessions"] = int(n)
    return out


def tenant_list_item(row, usage):
    u = usage.get(row["tenant_id"]) or {"active_sessions": 0, "today_messages": 0,
                                        "kb_docs": 0, "kb_size_mb": 0.0}
    return {
        "tenant_id": row["tenant_id"],
        "name": row["name"] or "",
        "app_key": row["app_key"] or "",
        "status": int(row["status"] or 0),
        "created_at": go_time_text(row["created_at"]),
        "max_concurrent_sessions": int(row["max_concurrent_sessions"] or 0),
        "daily_message_limit": int(row["daily_message_limit"] or 0),
        "max_kb_docs": int(row["max_kb_docs"] or 0),
        "max_kb_size_mb": int(row["max_kb_size_mb"] or 0),
        # [ours] 缺列（旧库）按默认开处理
        "memory_enabled": int(row["memory_enabled"]) if row.get("memory_enabled") is not None else 1,
        "active_sessions": u["active_sessions"],
        "today_messages": u["today_messages"],
        "kb_docs": u["kb_docs"],
        "kb_size_mb": u["kb_size_mb"],
    }


def tenant_detail_json(row):
    """对齐 model.TenantRow 的 JSON 形态：app_secret_hash / deleted_at / rbac_seeded 均 json:"-"。"""
    return {
        "id": int(row["id"]),
        "tenant_id": row["tenant_id"],
        "name": row["name"] or "",
        "app_key": row["app_key"] or "",
        "is_platform_admin": bool(row["is_platform_admin"]),
        "status": int(row["status"] or 0),
        "created_at": iso(row["created_at"]),
        "model_provider": row["model_provider"] or "",
        "model_provider_key": row["model_provider_key"] or "",
        "llm_provider_id": int(row["llm_provider_id"] or 0),
        "model_name": row["model_name"] or "",
        "model_api_base": row["model_api_base"] or "",
        "chat_input_cache_price": float(row["chat_input_cache_price"] or 0),
        "chat_input_price": float(row["chat_input_price"] or 0),
        "chat_output_price": float(row["chat_output_price"] or 0),
        "embedding_price": float(row["embedding_price"] or 0),
        "embedding_provider": row["embedding_provider"] or "",
        "embedding_provider_key": row["embedding_provider_key"] or "",
        "embedding_provider_id": int(row["embedding_provider_id"] or 0),
        "embedding_model_name": row["embedding_model_name"] or "",
        "embedding_api_base": row["embedding_api_base"] or "",
        "max_concurrent_sessions": int(row["max_concurrent_sessions"] or 0),
        "daily_message_limit": int(row["daily_message_limit"] or 0),
        "max_kb_docs": int(row["max_kb_docs"] or 0),
        "max_kb_size_mb": int(row["max_kb_size_mb"] or 0),
        # [ours] 缺列（旧库）按默认开处理
        "memory_enabled": int(row["memory_enabled"]) if row.get("memory_enabled") is not None else 1,
    }


def tenant_row_or_none(tenant_id):
    return one(f"SELECT {TENANT_COLUMNS} FROM tenants "
               f"WHERE tenant_id = %s AND deleted_at = 0 LIMIT 1", (tenant_id,))


def prewarm_tenant_async(tenant_id):
    """异步预热新租户 Agent（失败仅记日志）。"""
    def run():
        try:
            agent_call("POST", "/agent/tenants/prewarm",
                       body=json.dumps({"tenant_id": tenant_id}).encode(),
                       content_type="application/json", trace_id="prewarm")
        except Exception:                          # noqa: BLE001 - best effort
            pass
    threading.Thread(target=run, daemon=True).start()


def cleanup_tenant_async(tenant_id):
    """异步清理 Agent 侧数据。"""
    def run():
        try:
            agent_call("POST", "/agent/tenant/cleanup", body=None,
                       content_type="application/json", tenant_id=tenant_id)
        except Exception:                          # noqa: BLE001 - best effort
            pass
    threading.Thread(target=run, daemon=True).start()


@router.get("/api/admin/tenants")
def admin_tenant_list(name: str = "", app_key: str = "", status: str = "",
                      sort: str = "", admin: Admin = Depends(require_platform())):
    where, args = ["is_platform_admin = 0", "deleted_at = 0"], []
    if name:
        where.append("name LIKE %s")
        args.append("%" + name + "%")
    if app_key:
        where.append("app_key LIKE %s")
        args.append("%" + app_key + "%")
    # 只在 status 恰为 "0"/"1" 时过滤，"01"/"" 等一律视为未传
    if status in ("0", "1"):
        where.append("status = %s")
        args.append(int(status))
    order = {"created_at_asc": "created_at ASC", "name": "name ASC"}.get(
        sort, "created_at DESC")
    try:
        rows = many(f"SELECT {TENANT_COLUMNS} FROM tenants WHERE " + " AND ".join(where)
                    + f" ORDER BY {order}", tuple(args))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    usage = tenant_usage_map()
    return {"code": 0, "data": [tenant_list_item(r, usage) for r in rows]}


@router.get("/api/admin/tenants/{tid}")
def admin_tenant_detail(tid: str, admin: Admin = Depends(require_platform())):
    row = tenant_row_or_none(tid)
    if not row:
        soft(404, "租户不存在")
    return {"code": 0, "data": tenant_detail_json(row)}


@router.post("/api/admin/tenants")
async def admin_tenant_create(request: Request,
                              admin: Admin = Depends(require_platform())):
    body = await body_json(request)
    if not isinstance(body, dict):
        soft(400, "name 不能为空")
    name, app_key = body.get("name"), body.get("app_key") or ""
    if not isinstance(name, str) or name == "" or not isinstance(app_key, str):
        soft(400, "name 不能为空")
    if not app_key:
        app_key = "tk_" + secrets.token_hex(8)
    # 唯一索引包含软删行，所以这里先做一次包含软删行的预查，
    # 命中时直接返回可读错误，而不是把数据库 1062 错误暴露给调用方。
    if one("SELECT id FROM tenants WHERE app_key = %s LIMIT 1", (app_key,)):
        soft(500, "app_key 已被使用（含已删除租户）")
    app_secret = secrets.token_hex(16)
    tenant_id = "t_" + app_key
    try:
        # 必须写全业务需要的零值列：tenants 表大量列默认值是 NULL（不是 0/空串）。
        # 漏写任意一列都会出事：
        #   - deleted_at=NULL   → 新建租户立刻查不到（详情/配额/删除全 404）
        #   - created_at=NULL   → 详情 null、列表时间错乱
        #   - is_platform_admin=NULL → 列表的 `is_platform_admin = 0` 过滤把它剔掉
        write(
            "INSERT INTO tenants (tenant_id, name, app_key, app_secret_hash, "
            "is_platform_admin, status, rbac_seeded, "
            "model_provider, model_provider_key, llm_provider_id, model_name, "
            "model_api_key, model_api_base, chat_input_cache_price, chat_input_price, "
            "chat_output_price, embedding_price, "
            "embedding_provider, embedding_provider_key, embedding_provider_id, "
            "embedding_model_name, embedding_api_key, embedding_api_base, "
            "max_concurrent_sessions, daily_message_limit, max_kb_docs, max_kb_size_mb, "
            "created_at, deleted_at) "
            "VALUES (%s, %s, %s, %s, 0, 1, 0, "
            "'platform', '', 0, '', '', '', 0, 0, 0, 0, "
            "'platform', '', 0, '', '', '', "
            "100, 1000, 100, 100, NOW(3), 0)",
            (tenant_id, name, app_key,
             hashlib.sha256(app_secret.encode()).hexdigest()))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    reload_go_tenants()
    prewarm_tenant_async(tenant_id)
    return {"code": 0, "data": {
        "tenant_id": tenant_id, "name": name, "app_key": app_key,
        "app_secret": app_secret, "tip": "请立即保存 app_secret，仅此一次显示"}}


@router.put("/api/admin/tenants/{tid}")
async def admin_tenant_update(tid: str, request: Request,
                              admin: Admin = Depends(require_platform())):
    body = await body_json(request)
    if not isinstance(body, dict):
        soft(400, "name 不能为空")
    name = body.get("name")
    if not isinstance(name, str) or name == "":
        soft(400, "name 不能为空")
    if not tenant_row_or_none(tid):
        soft(404, "租户不存在")
    try:
        write("UPDATE tenants SET name = %s WHERE tenant_id = %s", (name, tid))
    except pymysql.MySQLError:
        soft(404, "租户不存在")
    reload_go_tenants()
    return {"code": 0, "msg": "租户已更新",
            "data": {"tenant_id": tid, "name": name}}


@router.put("/api/admin/tenants/{tid}/quota")
async def admin_tenant_quota(tid: str, request: Request,
                             admin: Admin = Depends(require_platform())):
    body = await body_json(request)
    if not isinstance(body, dict):
        soft(400, "参数错误")
    values = {}
    for key in ("max_concurrent_sessions", "daily_message_limit",
                "max_kb_docs", "max_kb_size_mb"):
        value = body.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            soft(400, "参数错误")
        values[key] = 0 if value < 0 else value        # 负数视为不限制
    # [ours] 租户级长对话记忆开关；body 不传该键时保持库中现值不动
    memory_enabled = body.get("memory_enabled")
    if memory_enabled is not None:
        memory_enabled = 1 if memory_enabled in (1, True, "1", "on", "true") else 0
    if not tenant_row_or_none(tid):
        soft(404, "租户不存在")
    try:
        if memory_enabled is None:
            write("UPDATE tenants SET max_concurrent_sessions = %s, daily_message_limit = %s, "
                  "max_kb_docs = %s, max_kb_size_mb = %s WHERE tenant_id = %s",
                  (values["max_concurrent_sessions"], values["daily_message_limit"],
                   values["max_kb_docs"], values["max_kb_size_mb"], tid))
        else:
            write("UPDATE tenants SET max_concurrent_sessions = %s, daily_message_limit = %s, "
                  "max_kb_docs = %s, max_kb_size_mb = %s, memory_enabled = %s WHERE tenant_id = %s",
                  (values["max_concurrent_sessions"], values["daily_message_limit"],
                   values["max_kb_docs"], values["max_kb_size_mb"], memory_enabled, tid))
    except pymysql.MySQLError:
        soft(404, "租户不存在")
    reload_go_tenants()
    return {"code": 0, "msg": "租户配额已更新"}


@router.post("/api/admin/tenants/{tid}/reset-secret")
def admin_tenant_reset_secret(tid: str, admin: Admin = Depends(require_platform())):
    if not tenant_row_or_none(tid):
        soft(404, "租户不存在")
    app_secret = secrets.token_hex(16)
    try:
        write("UPDATE tenants SET app_secret_hash = %s WHERE tenant_id = %s",
              (hashlib.sha256(app_secret.encode()).hexdigest(), tid))
    except pymysql.MySQLError:
        soft(404, "租户不存在")
    reload_go_tenants()
    return {"code": 0, "data": {
        "tenant_id": tid, "app_secret": app_secret,
        "tip": "请立即保存新的 app_secret，仅此一次显示"}}


@router.post("/api/admin/tenants/{tid}/toggle")
def admin_tenant_toggle(tid: str, admin: Admin = Depends(require_platform())):
    row = one("SELECT status FROM tenants WHERE tenant_id = %s AND deleted_at = 0 LIMIT 1",
              (tid,))
    if not row:
        soft(404, "租户不存在")
    new_status = 0 if int(row["status"] or 0) == 1 else 1
    try:
        write("UPDATE tenants SET status = %s WHERE tenant_id = %s", (new_status, tid))
    except pymysql.MySQLError:
        soft(404, "租户不存在")
    reload_go_tenants()
    return {"code": 0, "msg": "租户" + ("已启用" if new_status == 1 else "已禁用"),
            "data": {"tenant_id": tid, "status": new_status}}


@router.delete("/api/admin/tenants/{tid}")
def admin_tenant_delete(tid: str, admin: Admin = Depends(require_platform())):
    if tid == "t_admin":
        soft(403, "不能删除平台管理员租户")
    if not tenant_row_or_none(tid):
        soft(404, "租户不存在")
    now = int(time.time())                         # 该表软删统一存 unix 秒
    try:
        with transaction() as cur:
            cur.execute("UPDATE tickets SET deleted_at = %s "
                        "WHERE tenant_id = %s AND deleted_at = 0", (now, tid))
            # messages 表没有 deleted_at → 硬删除
            cur.execute("DELETE FROM messages WHERE tenant_id = %s", (tid,))
            cur.execute("UPDATE tenants SET deleted_at = %s WHERE tenant_id = %s",
                        (now, tid))
    except pymysql.MySQLError:
        soft(404, "租户不存在")
    reload_go_tenants()
    cleanup_tenant_async(tid)
    time.sleep(0.1)                                # 保留 100ms 等待，确保清理顺序稳定
    return {"code": 0,
            "msg": "已删除租户 " + tid + "（含工单/消息/Prompt/知识库数据）"}


@router.get("/api/admin/tenants/{tid}/model-config")
def admin_tenant_model_config(tid: str, admin: Admin = Depends(require_platform())):
    result = get_business_model_config(tid)
    if result is None:
        soft(404, "租户不存在")
    return {"code": 0, "data": result}


@router.put("/api/admin/tenants/{tid}/model-config")
async def admin_tenant_model_config_update(tid: str, request: Request,
                                           admin: Admin = Depends(require_platform())):
    body = await body_json(request)
    if not isinstance(body, dict):
        soft(400, "EOF")
    # 模型配置更新在租户不存在时返回 500 record not found，不是 404 ——
    # 与同文件其它端点的 404 语义不同，这里保持该行为。
    if not tenant_row_or_none(tid):
        soft(500, "record not found")
    try:
        updates = update_model_config(tid, body)
    except LookupError:
        soft(500, "record not found")
    except ValueError as exc:
        soft(500, str(exc))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    if updates is None:
        soft(400, "没有需要更新的字段")
    # 平台默认配置（t_admin）影响所有「跟随平台」的租户 → 广播 tenant_id="" 清全部缓存
    target = "" if tid == "t_admin" else tid
    try:
        cache().publish("cs:model:cache:invalidate", json.dumps(
            {"tenant_id": target, "scope": model_update_scope(updates)},
            ensure_ascii=False))
    except Exception:                              # noqa: BLE001 - best effort
        pass
    return {"code": 0, "msg": "模型配置已保存",
            "data": {"tenant_id": tid, "updated": updates}}


# ---------- 模型提供商（平台 CRUD；API Key 不在本表，存 tenants.*_api_key） ----------

MODEL_PROVIDER_COLUMNS = (
    "id, provider_key, provider_name, default_model, default_api_base, "
    "provider_type, sort_order, status, created_at, updated_at"
)


def model_provider_json(row):
    return {
        "id": int(row["id"]),
        "provider_key": row["provider_key"] or "",
        "provider_name": row["provider_name"] or "",
        "default_model": row["default_model"] or "",
        "default_api_base": row["default_api_base"] or "",
        "provider_type": row["provider_type"] or "",
        "sort_order": int(row["sort_order"] or 0),
        "status": int(row["status"] or 0),
        "created_at": iso_zero(row["created_at"]),
        "updated_at": iso_zero(row["updated_at"]),
    }


def required_field_error(*fields):
    """gin/validator 的 required 失败文案。

    注意：`validator.ValidationErrors.Error()` 会把**所有**失败项用 "\n" 串起来
    （不是只给第一条），所以缺两个必填字段时要拼两段。
    """
    return "\n".join(
        "Key: '" + f + "' Error:Field validation for '" + f + "' failed on the 'required' tag"
        for f in fields)


@router.get("/api/admin/model-providers")
def admin_model_provider_list(admin: Admin = Depends(require_platform())):
    try:
        rows = many(f"SELECT {MODEL_PROVIDER_COLUMNS} FROM model_providers "
                    "ORDER BY sort_order ASC, id ASC")
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "data": [model_provider_json(r) for r in rows]}


@router.get("/api/admin/available-model-providers")
def admin_available_model_providers(admin: Admin = Depends(current_admin),
                                    provider_type: str = Query(default="", alias="type")):
    """行为约定：只看启用项，type 过滤含 both。

    注意它在 **管理分组**（不是平台管理分组）：只要求登录，平台管理员与租户管理员
    都能读 —— 模型提供商是公共配置数据，不属于任一租户的业务数据。
    """
    where, args = ["status = 1"], []
    if provider_type:
        where.append("(provider_type = %s OR provider_type = 'both')")
        args.append(provider_type)
    try:
        rows = many(f"SELECT {MODEL_PROVIDER_COLUMNS} FROM model_providers WHERE "
                    + " AND ".join(where) + " ORDER BY sort_order ASC, id ASC",
                    tuple(args))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "data": [model_provider_json(r) for r in rows]}


@router.post("/api/admin/model-providers")
async def admin_model_provider_create(request: Request,
                                      admin: Admin = Depends(require_platform())):
    body = await body_json(request)
    if not isinstance(body, dict):
        soft(400, "EOF")
    provider_key, provider_name = body.get("provider_key"), body.get("provider_name")
    missing = []
    if not isinstance(provider_key, str) or provider_key == "":
        missing.append("ProviderKey")
    if not isinstance(provider_name, str) or provider_name == "":
        missing.append("ProviderName")
    if missing:
        soft(400, required_field_error(*missing))
    provider_type = body.get("provider_type") or "both"
    sort_order = body.get("sort_order") or 0
    if isinstance(sort_order, bool) or not isinstance(sort_order, int):
        soft(400, "json: cannot unmarshal value into Go struct field "
                  ".sort_order of type int")
    try:
        with transaction() as cur:
            cur.execute(
                "INSERT INTO model_providers (provider_key, provider_name, default_model, "
                "default_api_base, provider_type, sort_order, status, created_at, "
                "updated_at) VALUES (%s, %s, %s, %s, %s, %s, 1, NOW(), NOW())",
                (provider_key, provider_name, body.get("default_model") or "",
                 body.get("default_api_base") or "", provider_type, sort_order))
            new_id = cur.lastrowid
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    row = one(f"SELECT {MODEL_PROVIDER_COLUMNS} FROM model_providers WHERE id = %s",
              (new_id,))
    return {"code": 0, "msg": "模型提供商已创建", "data": model_provider_json(row)}


@router.put("/api/admin/model-providers/{id}")
async def admin_model_provider_update(id: str, request: Request,
                                      admin: Admin = Depends(require_platform())):
    provider_id = go_parse_uint(id)
    if provider_id == 0:
        soft(400, "无效的 ID")
    if not one("SELECT id FROM model_providers WHERE id = %s", (provider_id,)):
        soft(404, "模型提供商不存在")
    body = await body_json(request)
    if not isinstance(body, dict):
        soft(400, "EOF")
    updates = {}
    for key in ("provider_name", "default_model", "default_api_base", "provider_type"):
        if body.get(key) is not None:
            updates[key] = body[key]
    for key in ("sort_order", "status"):
        if body.get(key) is not None:
            value = body[key]
            if isinstance(value, bool) or not isinstance(value, int):
                soft(400, "json: cannot unmarshal value into Go struct field "
                          "." + key + " of type int")
            updates[key] = value
    if not updates:
        soft(400, "没有需要更新的字段")
    try:
        write("UPDATE model_providers SET " + ", ".join(k + " = %s" for k in updates)
              + ", updated_at = NOW() WHERE id = %s",
              tuple(updates.values()) + (provider_id,))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "msg": "模型提供商已更新", "data": updates}


@router.delete("/api/admin/model-providers/{id}")
def admin_model_provider_delete(id: str, admin: Admin = Depends(require_platform())):
    provider_id = go_parse_uint(id)
    if provider_id == 0:
        soft(400, "无效的 ID")
    if not one("SELECT id FROM model_providers WHERE id = %s", (provider_id,)):
        soft(404, "模型提供商不存在")
    ref = one("SELECT COUNT(*) AS cnt FROM tenants "
              "WHERE (llm_provider_id = %s OR embedding_provider_id = %s) "
              "AND deleted_at = 0", (provider_id, provider_id))
    # 被租户引用时不硬删，避免租户配置悬空
    if ref and int(ref["cnt"] or 0) > 0:
        soft(403, "有 " + str(int(ref["cnt"])) +
             " 个租户正在使用该提供商，请先调整租户配置")
    try:
        write("DELETE FROM model_providers WHERE id = %s", (provider_id,))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "msg": "模型提供商已删除"}


# ---------- 菜单管理（平台级全量；menus/tree 的身份视图已在 3-5 实现） ----------
# 与租户域相反：菜单 handler 用真实 400/500 状态码 → 审计中间件跳过，因此这里用 deny()。

# 行为约定：
# (id, parent_id, sort, visible, status, is_builtin, group_name, name, path, component, perm, type)
BUILTIN_MENUS = (
    (1, 0, 1, 1, 1, 1, "业务中心", "会话管理", "", "", "", 1),
    (2, 1, 1, 1, 1, 1, "业务中心", "运营看板", "/ops", "views/Ops.vue", "dashboard:view", 2),
    (10, 1, 2, 1, 1, 1, "业务中心", "会话列表", "/sessions", "views/Sessions.vue", "session:view", 2),
    (11, 10, 1, 1, 1, 1, "业务中心", "编辑客户信息", "", "", "session:profile:edit", 3),
    (12, 10, 2, 1, 1, 1, "业务中心", "清理归档", "", "", "session:cleanup", 3),
    (13, 1, 3, 1, 1, 1, "业务中心", "客户列表", "/customers", "views/Customers.vue", "customer:view", 2),
    (40, 0, 2, 1, 1, 1, "业务中心", "工单管理", "", "", "", 1),
    (3, 40, 1, 1, 1, 1, "业务中心", "工单列表", "/tickets", "views/Tickets.vue", "ticket:view", 2),
    (4, 3, 1, 1, 1, 1, "业务中心", "处理工单", "", "", "ticket:handle", 3),
    (5, 1, 4, 1, 1, 1, "业务中心", "快捷回复", "/quick-replies", "views/QuickReplies.vue", "quickreply:view", 2),
    (6, 5, 1, 1, 1, 1, "业务中心", "新增快捷回复", "", "", "quickreply:add", 3),
    (7, 5, 2, 1, 1, 1, "业务中心", "修改快捷回复", "", "", "quickreply:edit", 3),
    (8, 5, 3, 1, 1, 1, "业务中心", "删除快捷回复", "", "", "quickreply:remove", 3),
    (9, 1, 5, 1, 1, 1, "业务中心", "满意度评价", "/ratings", "views/Ratings.vue", "rating:view", 2),
    (42, 0, 3, 1, 1, 1, "业务中心", "知识库管理", "", "", "", 1),
    (18, 42, 1, 1, 1, 1, "业务中心", "知识库", "/knowledge", "views/Knowledge.vue", "knowledge:view", 2),
    (19, 18, 1, 1, 1, 1, "业务中心", "新增文档", "", "", "knowledge:add", 3),
    (20, 18, 2, 1, 1, 1, "业务中心", "修改文档", "", "", "knowledge:edit", 3),
    (21, 18, 3, 1, 1, 1, "业务中心", "删除文档", "", "", "knowledge:remove", 3),
    (43, 0, 4, 1, 1, 1, "业务中心", "模型管理", "", "", "", 1),
    (22, 43, 1, 1, 1, 1, "业务中心", "模型配置", "/model-config", "views/TenantModelConfig.vue", "model:view", 2),
    (23, 22, 1, 1, 1, 1, "业务中心", "配置模型", "", "", "model:edit", 3),
    (14, 43, 2, 1, 1, 1, "业务中心", "Prompt 管理", "/prompts", "views/Prompt.vue", "prompt:view", 2),
    (15, 14, 1, 1, 1, 1, "业务中心", "新增 Prompt", "", "", "prompt:add", 3),
    (16, 14, 2, 1, 1, 1, "业务中心", "修改 Prompt", "", "", "prompt:edit", 3),
    (17, 14, 3, 1, 1, 1, "业务中心", "删除 Prompt", "", "", "prompt:remove", 3),
    (44, 0, 5, 1, 1, 1, "业务中心", "统计管理", "", "", "", 1),
    (24, 44, 1, 1, 1, 1, "业务中心", "用量统计", "/token-usage", "views/TokenUsage.vue", "usage:view", 2),
    (25, 44, 2, 1, 1, 1, "业务中心", "审计日志", "/audit-logs", "views/AuditLogs.vue", "audit:view", 2),
    (45, 0, 6, 1, 1, 1, "业务中心", "用户管理", "", "", "", 1),
    (26, 45, 1, 1, 1, 1, "业务中心", "用户列表", "/members", "views/Users.vue", "member:view", 2),
    (27, 26, 1, 1, 1, 1, "业务中心", "新增用户", "", "", "member:add", 3),
    (28, 26, 2, 1, 1, 1, "业务中心", "修改用户", "", "", "member:edit", 3),
    (29, 26, 3, 1, 1, 1, "业务中心", "删除用户", "", "", "member:remove", 3),
    (30, 45, 2, 1, 1, 1, "业务中心", "角色权限", "/roles", "views/Roles.vue", "role:view", 2),
    (31, 30, 1, 1, 1, 1, "业务中心", "新增角色", "", "", "role:add", 3),
    (32, 30, 2, 1, 1, 1, "业务中心", "修改角色", "", "", "role:edit", 3),
    (33, 30, 3, 1, 1, 1, "业务中心", "删除角色", "", "", "role:remove", 3),
    (46, 0, 7, 1, 1, 1, "业务中心", "租户管理", "", "", "", 1),
    (47, 46, 1, 1, 1, 1, "业务中心", "可复用经验", "/experience", "views/Experience.vue", "experience:view", 2),
    (48, 47, 1, 1, 1, 1, "业务中心", "维护经验", "", "", "experience:edit", 3),
    (110, 0, 1, 1, 1, 1, "系统管理", "租户管理", "", "", "", 1),
    (100, 110, 1, 1, 1, 1, "系统管理", "租户列表", "/tenants", "views/Tenants.vue", "", 2),
    (113, 0, 2, 1, 1, 1, "系统管理", "模型管理", "", "", "", 1),
    (101, 113, 1, 1, 1, 1, "系统管理", "模型提供商", "/model-providers", "views/ModelProviders.vue", "", 2),
    (102, 113, 2, 1, 1, 1, "系统管理", "平台模型配置", "/platform-model", "views/PlatformModelConfig.vue", "", 2),
    (103, 113, 3, 1, 1, 1, "系统管理", "Prompt 管理", "/prompts", "views/Prompt.vue", "", 2),
    (111, 0, 3, 1, 1, 1, "系统管理", "统计管理", "", "", "", 1),
    (104, 111, 1, 1, 1, 1, "系统管理", "用量统计", "/token-usage", "views/TokenUsage.vue", "", 2),
    (105, 111, 2, 1, 1, 1, "系统管理", "审计日志", "/audit-logs", "views/AuditLogs.vue", "", 2),
    (112, 0, 4, 1, 1, 1, "系统管理", "平台配置", "", "", "", 1),
    (106, 112, 1, 1, 1, 1, "系统管理", "平台设置", "/platform-settings", "views/PlatformSettings.vue", "", 2),
    (107, 112, 2, 1, 1, 1, "系统管理", "菜单权限", "/menus", "views/MenuPermissions.vue", "", 2),
)

MENU_COLUMNS = ("id, parent_id, group_name, name, type, path, component, perm, icon, "
                "sort, visible, status, is_builtin, created_at, updated_at")


def menu_json(row):
    return {
        "id": int(row["id"]),
        "parent_id": int(row["parent_id"] or 0),
        "group_name": row["group_name"] or "",
        "name": row["name"] or "",
        "type": int(row["type"] or 0),
        "path": row["path"] or "",
        "component": row["component"] or "",
        "perm": row["perm"],                       # DB NULL → JSON null
        "icon": row["icon"] or "",
        "sort": int(row["sort"] or 0),
        "visible": int(row["visible"] or 0),
        "status": int(row["status"] or 0),
        "is_builtin": int(row["is_builtin"] or 0),
        "created_at": iso_zero(row["created_at"]),
        "updated_at": iso_zero(row["updated_at"]),
    }


def normalize_perm(value):
    """权限归一化：空串/纯空白统一为 NULL（唯一索引允许多个 NULL）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def is_duplicate_error(exc):
    return "Duplicate entry" in str(exc)


def menu_descendant_ids(root_id):
    """行为约定：后序收集（子节点在前、根节点在后）。"""
    collected, stack = [], [root_id]
    while stack:
        current = stack.pop()
        for r in many("SELECT id FROM menus WHERE parent_id = %s ORDER BY id ASC",
                      (current,)):
            child = int(r["id"])
            collected.append(child)
            stack.append(child)
    collected.append(root_id)
    return collected


@router.get("/api/admin/menus")
def admin_menu_list(admin: Admin = Depends(require_platform())):
    try:
        rows = many(f"SELECT {MENU_COLUMNS} FROM menus "
                    "ORDER BY group_name ASC, sort ASC, id ASC")
    except pymysql.MySQLError:
        deny(500, "查询菜单失败")
    return {"code": 0, "data": [menu_json(r) for r in rows]}


@router.post("/api/admin/menus")
async def admin_menu_create(request: Request, admin: Admin = Depends(require_platform())):
    body = await body_json(request)
    if not isinstance(body, dict):
        deny(400, "参数错误")
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        deny(400, "参数错误")
    name = name.strip()
    parent_id = body.get("parent_id") or 0
    if isinstance(parent_id, bool) or not isinstance(parent_id, int) or parent_id < 0:
        deny(400, "参数错误")
    menu_type = body.get("type") or 0
    if isinstance(menu_type, bool) or not isinstance(menu_type, int):
        deny(400, "参数错误")
    if menu_type == 0:
        menu_type = 2
    if parent_id > 0 and not one("SELECT id FROM menus WHERE id = %s", (parent_id,)):
        deny(400, "父菜单不存在")
    try:
        with transaction() as cur:
            cur.execute(
                "INSERT INTO menus (parent_id, group_name, name, type, path, component, "
                "perm, icon, sort, visible, status, is_builtin, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, NOW(), NOW())",
                (parent_id, body.get("group_name") or "", name, menu_type,
                 body.get("path") or "", body.get("component") or "",
                 normalize_perm(body.get("perm")), body.get("icon") or "",
                 body.get("sort") or 0, body.get("visible") or 0,
                 body.get("status") or 0))
            new_id = cur.lastrowid
    except pymysql.MySQLError as exc:
        if is_duplicate_error(exc):
            deny(400, "权限码已存在，请更换")
        deny(500, "创建菜单失败")
    try:
        write_handler_audit(admin, request, "create", "menu", name, "新增菜单")
    except Exception:                              # noqa: BLE001 - 审计 best-effort
        pass
    reload_go_menu_caches()
    return {"code": 0, "msg": "菜单已创建", "data": {"id": new_id}}


@router.put("/api/admin/menus/{id}")
async def admin_menu_update(id: str, request: Request,
                            admin: Admin = Depends(require_platform())):
    menu_id = go_parse_uint(id)
    body = await body_json(request)
    if not isinstance(body, dict) or menu_id == 0:
        deny(400, "参数错误")
    old = one(f"SELECT {MENU_COLUMNS} FROM menus WHERE id = %s", (menu_id,))
    if not old:
        deny(400, "菜单不存在")
    fields = {}
    if body.get("parent_id") is not None:
        parent_id = body["parent_id"]
        if isinstance(parent_id, bool) or not isinstance(parent_id, int) or parent_id < 0:
            deny(400, "参数错误")
        # 只有「换成另一个非 0 父节点」才校验存在性（把自己设为自己的父节点不校验）
        if parent_id > 0 and parent_id != menu_id:
            if not one("SELECT id FROM menus WHERE id = %s", (parent_id,)):
                deny(400, "父菜单不存在")
        fields["parent_id"] = parent_id
    if body.get("group_name") is not None:
        fields["group_name"] = body["group_name"]
    if body.get("name") is not None and str(body["name"]).strip():
        fields["name"] = str(body["name"]).strip()
    for key in ("type", "sort", "visible", "status"):
        if body.get(key) is not None:
            value = body[key]
            if isinstance(value, bool) or not isinstance(value, int):
                deny(400, "参数错误")
            fields[key] = value
    for key in ("path", "component", "icon"):
        if body.get(key) is not None:
            fields[key] = body[key]
    if body.get("perm") is not None:
        fields["perm"] = normalize_perm(body["perm"])
    try:
        write("UPDATE menus SET " + ", ".join(k + " = %s" for k in fields)
              + ", updated_at = NOW() WHERE id = %s",
              tuple(fields.values()) + (menu_id,))
    except pymysql.MySQLError as exc:
        if is_duplicate_error(exc):
            deny(400, "权限码已存在，请更换")
        deny(500, "更新菜单失败")
    try:
        write_handler_audit(admin, request, "update", "menu", old["name"], "更新菜单")
    except Exception:                              # noqa: BLE001 - 审计 best-effort
        pass
    reload_go_menu_caches()
    return {"code": 0, "msg": "菜单已更新"}


@router.delete("/api/admin/menus/{id}")
def admin_menu_delete(id: str, request: Request,
                      admin: Admin = Depends(require_platform())):
    menu_id = go_parse_uint(id)
    if menu_id == 0:
        deny(400, "参数错误")
    old = one(f"SELECT {MENU_COLUMNS} FROM menus WHERE id = %s", (menu_id,))
    if not old:
        deny(400, "菜单不存在")
    ids = menu_descendant_ids(menu_id)
    placeholders = ",".join(["%s"] * len(ids))
    try:
        with transaction() as cur:
            cur.execute(f"SELECT {MENU_COLUMNS} FROM menus WHERE id IN ({placeholders}) "
                        "ORDER BY id ASC", tuple(ids))
            removed = cur.fetchall()
            cur.execute(f"DELETE FROM sys_role_menu WHERE menu_id IN ({placeholders})",
                        tuple(ids))
            cur.execute(f"DELETE FROM menus WHERE id IN ({placeholders})", tuple(ids))
    except pymysql.MySQLError:
        deny(500, "删除菜单失败")
    perms = [r["perm"] for r in removed if r["perm"]]
    try:
        write_handler_audit(admin, request, "delete", "menu", old["name"], "删除菜单")
    except Exception:                              # noqa: BLE001 - 审计 best-effort
        pass
    reload_go_menu_caches()
    return {"code": 0, "msg": "菜单已删除", "data": {"removed_perms": perms}}


@router.post("/api/admin/menus/batch-update")
async def admin_menu_batch_update(request: Request,
                                 admin: Admin = Depends(require_platform())):
    body = await body_json(request)
    if not isinstance(body, dict):
        deny(400, "参数错误")
    ids = go_uints(body.get("ids"))
    if ids is None or not ids:
        deny(400, "参数错误")
    fields = {}
    if body.get("status") is not None:
        if body["status"] not in (0, 1):
            deny(400, "状态取值非法")
        fields["status"] = body["status"]
    if body.get("visible") is not None:
        if body["visible"] not in (0, 1):
            deny(400, "可见性取值非法")
        fields["visible"] = body["visible"]
    if body.get("sort") is not None:
        value = body["sort"]
        if isinstance(value, bool) or not isinstance(value, int):
            deny(400, "参数错误")
        fields["sort"] = value
    if not fields:
        deny(400, "缺少更新字段")
    placeholders = ",".join(["%s"] * len(ids))
    try:
        write("UPDATE menus SET " + ", ".join(k + " = %s" for k in fields)
              + ", updated_at = NOW() WHERE id IN (" + placeholders + ")",
              tuple(fields.values()) + tuple(ids))
    except pymysql.MySQLError:
        deny(500, "批量更新菜单失败")
    try:
        write_handler_audit(admin, request, "update", "menu", "批量菜单", "批量更新菜单")
    except Exception:                              # noqa: BLE001 - 审计 best-effort
        pass
    reload_go_menu_caches()
    return {"code": 0, "msg": "已批量更新 " + str(len(ids)) + " 条菜单"}


@router.post("/api/admin/menus/reset-builtin")
def admin_menu_reset_builtin(request: Request,
                            admin: Admin = Depends(require_platform())):
    try:
        with transaction() as cur:
            cur.execute("SELECT id FROM menus WHERE is_builtin = 1 ORDER BY id ASC")
            builtin_ids = [int(r["id"]) for r in cur.fetchall()]
            if builtin_ids:
                placeholders = ",".join(["%s"] * len(builtin_ids))
                cur.execute("DELETE FROM sys_role_menu WHERE menu_id IN ("
                            + placeholders + ")", tuple(builtin_ids))
                cur.execute("DELETE FROM menus WHERE is_builtin = 1")
            for item in BUILTIN_MENUS:
                (menu_id, parent_id, sort, visible, status, builtin,
                 group_name, name, path, component, perm, menu_type) = item
                cur.execute(
                    "INSERT INTO menus (id, parent_id, group_name, name, type, path, "
                    "component, perm, icon, sort, visible, status, is_builtin, "
                    "created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '', %s, %s, %s, %s, "
                    "NOW(), NOW())",
                    (menu_id, parent_id, group_name, name, menu_type, path, component,
                     perm or None, sort, visible, status, builtin))
            # 内置菜单被删时角色关联一并清空 → 给各租户 admin 角色补回业务中心全权
            cur.execute("SELECT id FROM roles WHERE code = 'admin' ORDER BY id ASC")
            admin_role_ids = [int(r["id"]) for r in cur.fetchall()]
            if admin_role_ids:
                cur.execute("SELECT id FROM menus WHERE group_name = '业务中心' "
                            "ORDER BY id ASC")
                biz_ids = [int(r["id"]) for r in cur.fetchall()]
                for role_id in admin_role_ids:
                    for menu_id in biz_ids:
                        cur.execute("INSERT INTO sys_role_menu (role_id, menu_id) "
                                    "VALUES (%s, %s)", (role_id, menu_id))
    except pymysql.MySQLError:
        deny(500, "重置内置菜单失败")
    try:
        write_handler_audit(admin, request, "update", "menu", "内置菜单", "重置内置菜单")
    except Exception:                              # noqa: BLE001 - 审计 best-effort
        pass
    reload_go_menu_caches()
    return {"code": 0, "msg": "内置菜单已重置为初始状态"}


