"""Prompt and knowledge-base proxy endpoints plus model diagnostics."""

import json
import os
import time

import httpx
from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import JSONResponse

from business.admin.auth import Admin, current_admin, require_perm
from business.admin.ops import (AGENT_URL, agent_payload_response,
                                decrypt_api_key, proxy_to_agent,
                                require_platform, tenant_config_row)
from business.admin.platform import required_field_error
from business.core.errors import code_to_status, soft
from business.core.requests import body_json
from business.infra.storage import one

router = APIRouter()

# ============================================================================
# 阶段 3-7：代理域（Prompt / 知识库 / 平台设置 / 模型可用性测试）
#
# 这一批和前五批性质不同：19 条路由里 18 条在 Go 侧只是「鉴权 + 审计 + 原样转发
# 给 Python Agent」，业务逻辑本来就在 Agent 里。所以迁移重点是**把 Go 的转发语义
# 逐一复刻**，而不是重写业务：
#
#   1. 请求侧：原样透传 raw query 与 raw body（含 Content-Type），
#      附加 X-Trace-Id / X-Tenant-Id / X-Internal-Token 三个头
#   2. 响应侧：**不看 Agent 的 HTTP 状态码**，拿到什么字节就回写 200 + application/json
#   3. 失败侧：连不上 Agent → HTTP 200 + {"code":500,"msg":"调用agent失败:<err>"}
#      （注意是 200 而不是 5xx —— 前端统一按 body.code 判断）
#   4. 特殊分支：ProxyPrompt 的 ?scope=platform、ProxyKbUpload 的 multipart 重组
#
# 唯一有本地逻辑的是 POST /api/admin/model-config/test：它不走 Agent，
# 直接用表单里的模型名/Base/Key 打一次第三方 OpenAI 兼容接口。
# ============================================================================

PLATFORM_SCOPE = "__platform__"


async def proxy_prompt(request, method, agent_path, admin):
    """对齐 Go `ProxyPrompt`：`?scope=platform` 时仅平台管理员可操作平台级 Prompt。

    平台级 Prompt 在库里是 `tenant_id=''` 的全局行，Agent 侧用特殊租户标识
    `__platform__` 映射过去；普通租户仍走原租户隔离逻辑。
    """
    if request.query_params.get("scope") == "platform":
        if not admin.is_platform_admin:
            return JSONResponse(status_code=code_to_status(403), content={
                "code": 403, "msg": "仅平台管理员可管理平台级（全局）Prompt"})
        return await proxy_to_agent(request, method, agent_path, PLATFORM_SCOPE)
    return await proxy_to_agent(request, method, agent_path, admin.tenant_id)


async def proxy_kb_upload(request, admin):
    """对齐 Go `ProxyKbUpload`：multipart(file+title+category) 重组后转发，否则 JSON 透传。

    Go 侧不是「原样透传 multipart 字节」，而是**重新构造一份** multipart，只带
    file / title / category 三个 part，且 title、category 为空时也照写空串。
    这里用 httpx 的 files/data 复刻同样的形状（boundary 必然不同，Agent 按 part 解析）。
    """
    form = await request.form()
    file = form.get("file")
    if isinstance(file, UploadFile) or hasattr(file, "read"):
        title = form.get("title") or ""
        category = form.get("category") or ""
        content = await file.read()
        filename = getattr(file, "filename", "") or ""
        data, err = agent_call_bytes(
            "POST", "/agent/kb/upload", "",
            files={"file": (filename, content)},
            form_fields={"title": str(title), "category": str(category)},
            trace_id=request.headers.get("X-Trace-Id", ""),
            tenant_id=admin.tenant_id)
        if err is not None:
            return JSONResponse(status_code=500,
                                content={"code": 500, "msg": "调用agent失败:" + err})
        return agent_payload_response(data)
    return await proxy_to_agent(request, "POST", "/agent/kb/upload", admin.tenant_id)


def agent_call_bytes(method, path, query="", files=None, form_fields=None,
                     trace_id="", tenant_id="", timeout=120):
    """带 multipart 的 agent 调用（`agent_call` 的字节体版本装不下 files）。"""
    url = AGENT_URL + path + (("?" + query) if query else "")
    headers = {}
    if trace_id:
        headers["X-Trace-Id"] = trace_id
    if tenant_id:
        headers["X-Tenant-Id"] = tenant_id
    token = os.getenv("INTERNAL_API_TOKEN", "")
    if token:
        headers["X-Internal-Token"] = token
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method, url, files=files, data=form_fields,
                                 headers=headers)
        return resp.content, None
    except Exception as exc:                      # noqa: BLE001 - 透传底层错误文本
        return None, str(exc)


# ---------- Prompt（biz 组，按权限码细分） ----------

@router.get("/api/admin/prompts")
async def admin_prompts(request: Request,
                        admin: Admin = Depends(require_perm("prompt:view"))):
    return await proxy_prompt(request, "GET", "/agent/prompts", admin)


@router.get("/api/admin/prompts/active")
async def admin_prompts_active(request: Request,
                               admin: Admin = Depends(require_perm("prompt:view"))):
    return await proxy_prompt(request, "GET", "/agent/prompts/active", admin)


@router.post("/api/admin/prompts")
async def admin_prompts_create(request: Request,
                               admin: Admin = Depends(require_perm("prompt:add"))):
    return await proxy_prompt(request, "POST", "/agent/prompts", admin)


@router.post("/api/admin/prompts/{key}/activate")
async def admin_prompt_activate(key: str, request: Request,
                                admin: Admin = Depends(require_perm("prompt:edit"))):
    return await proxy_prompt(request, "POST", "/agent/prompts/" + key + "/activate",
                              admin)


@router.delete("/api/admin/prompts/{key}")
async def admin_prompt_delete(key: str, request: Request,
                              admin: Admin = Depends(require_perm("prompt:remove"))):
    return await proxy_prompt(request, "DELETE", "/agent/prompts/" + key, admin)


@router.post("/api/admin/prompts/{key}/versions")
async def admin_prompt_versions_create(key: str, request: Request,
                                      admin: Admin = Depends(require_perm("prompt:edit"))):
    return await proxy_prompt(request, "POST", "/agent/prompts/" + key + "/versions",
                              admin)


@router.get("/api/admin/prompts/{key}/versions")
async def admin_prompt_versions(key: str, request: Request,
                                admin: Admin = Depends(require_perm("prompt:view"))):
    return await proxy_prompt(request, "GET", "/agent/prompts/" + key + "/versions",
                              admin)


@router.post("/api/admin/prompts/{key}/versions/{vid}/rollback")
async def admin_prompt_rollback(key: str, vid: str, request: Request,
                                admin: Admin = Depends(require_perm("prompt:edit"))):
    return await proxy_prompt(
        request, "POST",
        "/agent/prompts/" + key + "/versions/" + vid + "/rollback", admin)


# ---------- 知识库（biz 组） ----------

@router.post("/api/admin/kb/upload")
async def admin_kb_upload(request: Request,
                          admin: Admin = Depends(require_perm("knowledge:add"))):
    return await proxy_kb_upload(request, admin)


@router.get("/api/admin/kb/documents")
async def admin_kb_documents(request: Request,
                             admin: Admin = Depends(require_perm("knowledge:view"))):
    return await proxy_to_agent(request, "GET", "/agent/kb/documents", admin.tenant_id)


@router.delete("/api/admin/kb/documents/{doc_id}")
async def admin_kb_document_delete(doc_id: str, request: Request,
                                   admin: Admin = Depends(require_perm("knowledge:remove"))):
    return await proxy_to_agent(request, "DELETE", "/agent/kb/documents/" + doc_id,
                                admin.tenant_id)


@router.post("/api/admin/kb/documents/{doc_id}/category")
async def admin_kb_document_category(doc_id: str, request: Request,
                                     admin: Admin = Depends(require_perm("knowledge:edit"))):
    return await proxy_to_agent(request, "POST",
                                "/agent/kb/documents/" + doc_id + "/category",
                                admin.tenant_id)


@router.post("/api/admin/kb/documents/{doc_id}/policy")
async def admin_kb_document_policy(doc_id: str, request: Request,
                                   admin: Admin = Depends(require_perm("knowledge:edit"))):
    return await proxy_to_agent(request, "POST",
                                "/agent/kb/documents/" + doc_id + "/policy",
                                admin.tenant_id)


@router.post("/api/admin/kb/documents/{doc_id}/reindex")
async def admin_kb_document_reindex(doc_id: str, request: Request,
                                    admin: Admin = Depends(require_perm("knowledge:edit"))):
    return await proxy_to_agent(request, "POST",
                                "/agent/kb/documents/" + doc_id + "/reindex",
                                admin.tenant_id)


@router.post("/api/admin/kb/index")
async def admin_kb_index(request: Request,
                         admin: Admin = Depends(require_perm("knowledge:edit"))):
    return await proxy_to_agent(request, "POST", "/agent/kb/index", admin.tenant_id)


@router.get("/api/admin/kb/categories")
async def admin_kb_categories(request: Request,
                              admin: Admin = Depends(require_perm("knowledge:view"))):
    return await proxy_to_agent(request, "GET", "/agent/kb/categories", admin.tenant_id)


@router.post("/api/admin/kb/batch")
async def admin_kb_batch(request: Request,
                         admin: Admin = Depends(require_perm("knowledge:edit"))):
    return await proxy_to_agent(request, "POST", "/agent/kb/batch", admin.tenant_id)


# ---------- 平台设置（GET 挂 admin 组，PUT 挂 plat 组） ----------

@router.get("/api/admin/platform-settings")
async def admin_platform_settings(request: Request,
                                  admin: Admin = Depends(current_admin)):
    return await proxy_to_agent(request, "GET", "/agent/platform-settings",
                                admin.tenant_id)


@router.put("/api/admin/platform-settings")
async def admin_platform_settings_update(request: Request,
                                         admin: Admin = Depends(require_platform())):
    return await proxy_to_agent(request, "PUT", "/agent/platform-settings",
                                admin.tenant_id)


# ---------- 模型可用性测试：本层唯一有本地逻辑的一条 ----------

def _provider_by_id(provider_id):
    """对齐 Go `loadProviderByID`：id=0 或已停用都算「取不到」。"""
    if not provider_id:
        return None
    row = one("SELECT id, provider_key, default_model, default_api_base, "
              "provider_type, status FROM model_providers WHERE id = %s LIMIT 1",
              (provider_id,))
    if not row or int(row["status"] or 0) != 1:
        return None
    return row


def first_enabled_provider(kind):
    """对齐 Go `firstEnabledProvider`：status=1 且 (provider_type=kind 或 both)。"""
    return one(
        "SELECT id, provider_key, default_model, default_api_base, provider_type, "
        "status FROM model_providers WHERE status = 1 "
        "AND (provider_type = %s OR provider_type = 'both') "
        "ORDER BY sort_order ASC, id ASC LIMIT 1", (kind,))


def resolve_chat_config(tid, depth=0):
    """对齐 Go `resolveChatConfig`：返回 (provider_key, model_name, api_base, api_key)。

    四级解析：provider_id 关联 → 旧版手填 custom → 跟随平台(t_admin) → 首个启用提供商兜底。
    取不到时抛 ValueError（文案与 Go 的 error 一致），调用方按「静默忽略」处理。
    """
    if depth > 3:
        raise ValueError("模型配置解析递归过深（跟随平台循环）")
    row = tenant_config_row(tid)
    if not row:
        raise ValueError("record not found")
    if int(row["llm_provider_id"] or 0) > 0:
        provider = _provider_by_id(int(row["llm_provider_id"]))
        if provider is not None:
            model = provider["default_model"] or ""
            base = provider["default_api_base"] or ""
            if row["model_name"]:
                model = row["model_name"]
            if row["model_api_base"]:
                base = row["model_api_base"]
            key = decrypt_api_key(row["model_api_key"])
            if key:
                return provider["provider_key"] or "", model, base, key
            # 本行无 Key → 视为未配置，继续后续分支
    if row["model_provider"] == "custom":
        key = decrypt_api_key(row["model_api_key"])
        if key and (row["model_name"] or row["model_api_base"]):
            return (row["model_provider_key"] or "", row["model_name"] or "",
                    row["model_api_base"] or "", key)
    if tid != "t_admin":
        return resolve_chat_config("t_admin", depth + 1)
    provider = first_enabled_provider("chat")
    if provider is None:
        raise ValueError("未配置默认对话模型提供商，请在「模型提供商」中启用至少一个")
    key = decrypt_api_key(row["model_api_key"])
    if not key:
        raise ValueError("平台默认对话模型未配置 API Key，请以平台管理员登录后"
                         "在「平台模型配置」中配置（Key 存于 t_admin 租户）")
    return (provider["provider_key"] or "", provider["default_model"] or "",
            provider["default_api_base"] or "", key)


def resolve_emb_config(tid, depth=0):
    """对齐 Go `resolveEmbConfig`：逻辑同 resolve_chat_config，字段换成 embedding_*。"""
    if depth > 3:
        raise ValueError("模型配置解析递归过深（跟随平台循环）")
    row = tenant_config_row(tid)
    if not row:
        raise ValueError("record not found")
    if int(row["embedding_provider_id"] or 0) > 0:
        provider = _provider_by_id(int(row["embedding_provider_id"]))
        if provider is not None:
            model = provider["default_model"] or ""
            base = provider["default_api_base"] or ""
            if row["embedding_model_name"]:
                model = row["embedding_model_name"]
            if row["embedding_api_base"]:
                base = row["embedding_api_base"]
            key = decrypt_api_key(row["embedding_api_key"])
            if key:
                return provider["provider_key"] or "", model, base, key
    if row["embedding_provider"] == "custom":
        key = decrypt_api_key(row["embedding_api_key"])
        if key and (row["embedding_model_name"] or row["embedding_api_base"]):
            return (row["embedding_provider_key"] or "",
                    row["embedding_model_name"] or "",
                    row["embedding_api_base"] or "", key)
    if tid != "t_admin":
        return resolve_emb_config("t_admin", depth + 1)
    provider = first_enabled_provider("embedding")
    if provider is None:
        raise ValueError("未配置默认 Embedding 模型提供商，请在「模型提供商」中启用至少一个")
    key = decrypt_api_key(row["embedding_api_key"])
    if not key:
        raise ValueError("平台默认 Embedding 模型未配置 API Key，请以平台管理员登录后"
                         "在「平台模型配置」中配置（Key 存于 t_admin 租户）")
    return (provider["provider_key"] or "", provider["default_model"] or "",
            provider["default_api_base"] or "", key)


def agent_model_config(tid):
    """对齐 Go `GetAgentModelConfig` 里 AdminModelTest 用到的部分。

    注意 Go 的 quirk：`use_custom` 取的是**对话侧**是否自有配置（`chatOwn`），
    但 embedding 分支的回填也用这同一个开关 —— 也就是「对话跟随平台、Embedding 自有」
    的租户，测 embedding 时不会回填。这里照抄，不做「修正」。
    """
    row = tenant_config_row(tid)
    if not row:
        return None
    chat_own = bool(decrypt_api_key(row["model_api_key"])) and (
        int(row["llm_provider_id"] or 0) > 0
        or (row["model_provider"] == "custom"
            and bool(row["model_name"] or row["model_api_base"])))
    try:
        chat = resolve_chat_config(tid)
    except ValueError:
        chat = ("", "", "", "")
    try:
        emb = resolve_emb_config(tid)
    except ValueError:
        emb = ("", "", "", "")
    return {
        "use_custom": chat_own,
        "model_name": chat[1], "api_base": chat[2], "api_key": chat[3],
        "embedding": {"model_name": emb[1], "api_base": emb[2], "api_key": emb[3]},
    }


def truncate_str(text, limit):
    return text if len(text) <= limit else text[:limit] + "..."


@router.post("/api/admin/model-config/test")
async def admin_model_config_test(request: Request,
                                  tenant_id: str = "",
                                  admin: Admin = Depends(current_admin)):
    """对齐 Go `AdminModelTest`：用表单值打一次真实调用，只测不落库。

    表单留空（典型是「Key 已配置、前端只回显掩码」）时回退读该租户的已存配置。
    """
    body = await body_json(request)
    if not isinstance(body, dict):
        body = {}
    kind = body.get("type") or ""
    if not kind:
        soft(400, required_field_error("Type"))
    model_name = body.get("model_name") or ""
    api_base = body.get("api_base") or ""
    api_key = body.get("api_key") or ""
    if not model_name or not api_base or not api_key:
        target = tenant_id or "t_admin"
        cfg = agent_model_config(target)
        if cfg and cfg["use_custom"]:
            if kind == "chat":
                model_name = model_name or cfg["model_name"]
                api_base = api_base or cfg["api_base"]
                api_key = api_key or cfg["api_key"]
            else:
                model_name = model_name or cfg["embedding"]["model_name"]
                api_base = api_base or cfg["embedding"]["api_base"]
                api_key = api_key or cfg["embedding"]["api_key"]
    if not api_key:
        soft(400, "请填写 API Key 后再测试（或该租户尚未配置 API Key）")
    if not model_name or not api_base:
        soft(400, "模型名称与 API Base 不能为空（表单留空且未读到已存配置）")
    if kind == "chat":
        url = api_base.rstrip("/") + "/chat/completions"
        payload = json.dumps({"model": model_name, "max_tokens": 5,
                              "messages": [{"role": "user", "content": "ping"}]})
    elif kind == "embedding":
        url = api_base.rstrip("/") + "/embeddings"
        payload = json.dumps({"model": model_name, "input": "test"})
    else:
        soft(400, "type 必须是 chat 或 embedding")
    started = time.monotonic()
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, content=payload, headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
            })
    except Exception as exc:                      # noqa: BLE001 - 文案与 Go 一致
        return {"code": 0, "data": {"ok": False, "reason": "连接失败: " + str(exc)}}
    raw = resp.text or ""
    latency = int((time.monotonic() - started) * 1000)
    if resp.status_code != 200:
        return {"code": 0, "data": {"ok": False, "reason": "HTTP " + str(resp.status_code)
                                    + ": " + truncate_str(raw, 200)}}
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        return {"code": 0, "data": {"ok": False,
                                    "reason": "响应解析失败: " + truncate_str(raw, 200)}}
    if kind == "chat":
        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices:
            return {"code": 0, "data": {"ok": False,
                                        "reason": "响应无 choices 字段，模型可能不支持该接口"}}
        return {"code": 0, "data": {"ok": True, "latency_ms": latency}}
    data_arr = parsed.get("data")
    if not isinstance(data_arr, list) or not data_arr:
        return {"code": 0, "data": {"ok": False,
                                    "reason": "响应无 data 字段，模型可能不支持该接口"}}
    first = data_arr[0] if isinstance(data_arr[0], dict) else {}
    embedding = first.get("embedding")
    if not isinstance(embedding, list) or not embedding:
        return {"code": 0, "data": {"ok": False, "reason": "embedding 结果为空"}}
    return {"code": 0, "data": {"ok": True, "latency_ms": latency,
                                "dim": len(embedding)}}


# ---------- Agent internal APIs (parallel with Go until WebSocket cutover) ----------

