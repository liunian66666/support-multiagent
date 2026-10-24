"""[ours] S2 经验层内部接口：接收业务侧 feedback_outbox 同步批次。

与 app.api.deps._require_trusted_chat 同规：INTERNAL_API_TOKEN 常量（容器启动即最终值，
与 .env 同步）+ X-Internal-Token + X-Tenant-Id 三方一致才放行；批次内租户必须一致，
防止跨租户投递。写入幂等（event_id 主键冲突即视为已同步），业务侧可安全重试。
"""

import os

from fastapi import APIRouter, HTTPException, Request

from app.infra.db.experience import save_feedback_event

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
