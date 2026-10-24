"""[ours] S2 经验层内部接口：接收业务侧 feedback_outbox 同步批次。

与 app.api.deps._require_trusted_chat 同规：INTERNAL_API_TOKEN 常量（容器启动即最终值，
与 .env 同步）+ X-Internal-Token + X-Tenant-Id 三方一致才放行；批次内租户必须一致，
防止跨租户投递。写入幂等（event_id 主键冲突即视为已同步），业务侧可安全重试。
"""

import os

from fastapi import APIRouter, HTTPException, Request

from app.infra.db.experience import (create_strategy_version,
                                     get_active_strategy,
                                     list_strategy_versions,
                                     publish_strategy_version,
                                     rollback_strategy_version,
                                     save_feedback_event,
                                     set_experience_status)

router = APIRouter()

_MAX_BATCH = 200


def _guard(request: Request, tenant_id: str):
    # 与 business internal_guard 一致：运行时读环境，随时可轮换
    expected = os.getenv("INTERNAL_API_TOKEN", "")
    supplied = request.headers.get("X-Internal-Token", "")
    if (not expected or supplied != expected or not tenant_id or
            request.headers.get("X-Tenant-Id") != tenant_id):
        raise HTTPException(status_code=403,
                            detail="Experience feedback identity verification failed")


@router.post("/agent/experience/feedback")
async def ingest_feedback(request: Request):
    tenant_id = request.headers.get("X-Tenant-Id", "")
    _guard(request, tenant_id)
    body = await request.json()
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list) or not events:
        raise HTTPException(status_code=400, detail="events 必须为非空列表")
    if len(events) > _MAX_BATCH:
        raise HTTPException(status_code=400,
                            detail=f"单批最多 {_MAX_BATCH} 条")
    for ev in events:
        if (not isinstance(ev, dict) or not ev.get("event_id")
                or not ev.get("result_type")):
            raise HTTPException(status_code=400,
                                detail="事件缺少 event_id/result_type")
        # 批次内租户一致性：事件租户必须等于头部租户
        if ev.get("tenant_id") != tenant_id:
            raise HTTPException(status_code=403,
                                detail="事件租户与请求租户不一致")
    stored = 0
    for ev in events:
        if await save_feedback_event(request.app, ev):
            stored += 1
    return {"code": 0, "data": {"received": len(events), "stored": stored}}


@router.post("/agent/experience/review")
async def review_experience(request: Request):
    """[ours] S3 审核接口（供 S4 管理页调用）：publish / suspend 单条经验。

    与投喂接口同款内部鉴权；租户隔离在 SQL 层（UPDATE 带 tenant_id 条件），
    跨租户 ID 一律 404。published 是经验进入召回池的唯一入口。
    """
    tenant_id = request.headers.get("X-Tenant-Id", "")
    _guard(request, tenant_id)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="载荷必须为对象")
    experience_id = body.get("experience_id")
    action = body.get("action")
    if not isinstance(experience_id, int) or action not in ("publish", "suspend"):
        raise HTTPException(status_code=400,
                            detail="experience_id(int)/action(publish|suspend) 必填")
    ok = await set_experience_status(request.app, tenant_id, experience_id, action)
    if not ok:
        raise HTTPException(status_code=404, detail="经验不存在或不属于该租户")
    return {"code": 0, "data": {"experience_id": experience_id,
                                "review_status": action}}


# ---------------------------------------------------------------------------
# [ours] S5 策略版本：候选→发布→回滚（一键，生效路径唯一）
# ---------------------------------------------------------------------------

@router.post("/agent/experience/strategy")
async def create_strategy(request: Request):
    """登记候选策略版本（配对评测达标前只是快照，不影响线上）。"""
    tenant_id = request.headers.get("X-Tenant-Id", "")
    _guard(request, tenant_id)
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("params"), dict):
        raise HTTPException(status_code=400, detail="params(对象) 必填")
    try:
        version_id = await create_strategy_version(
            request.app, tenant_id, body["params"],
            parent_version=body.get("parent_version"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"code": 0, "data": {"version_id": version_id}}


@router.get("/agent/experience/strategy")
async def list_strategies(request: Request):
    tenant_id = request.headers.get("X-Tenant-Id", "")
    _guard(request, tenant_id)
    rows = await list_strategy_versions(request.app, tenant_id)
    for r in rows:
        for key in ("published_at", "created_at"):
            if r.get(key) is not None:
                r[key] = r[key].isoformat()
    return {"code": 0, "data": {"versions": rows}}


@router.post("/agent/experience/strategy/publish")
async def publish_strategy(request: Request):
    """人工发布 candidate 版本；原 published 转 superseded（同事务）。"""
    tenant_id = request.headers.get("X-Tenant-Id", "")
    _guard(request, tenant_id)
    return await _transition_strategy(request, tenant_id, publish_strategy_version)


@router.post("/agent/experience/strategy/rollback")
async def rollback_strategy(request: Request):
    """一键回滚：重新发布历史版本（≤1 分钟生效——策略缓存 TTL 30s）。"""
    tenant_id = request.headers.get("X-Tenant-Id", "")
    _guard(request, tenant_id)
    return await _transition_strategy(request, tenant_id, rollback_strategy_version)


async def _transition_strategy(request: Request, tenant_id: str, fn):
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("version_id"), int):
        raise HTTPException(status_code=400, detail="version_id(int) 必填")
    done = await fn(request.app, tenant_id, body["version_id"],
                    actor=str(body.get("actor") or "internal"))
    if not done:
        raise HTTPException(status_code=404,
                            detail="版本不存在、不属于该租户、或当前状态不允许该迁移")
    return {"code": 0, "data": {"version_id": done["id"],
                                "eval_status": "published"}}


@router.get("/agent/experience/strategy/active")
async def active_strategy(request: Request):
    """当前生效策略（召回开关第三级的真实读取值，审计/看板用）。"""
    tenant_id = request.headers.get("X-Tenant-Id", "")
    _guard(request, tenant_id)
    # 绕过缓存读最新值不适合热路径，但审计接口必须读到发布后的立即状态：
    # 复用 get_active_strategy（30s 窗口可接受，发布接口本身同进程会即时失效）
    strategy = await get_active_strategy(request.app, tenant_id)
    return {"code": 0, "data": strategy}
