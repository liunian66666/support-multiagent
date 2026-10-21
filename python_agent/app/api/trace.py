"""Internal, tenant-scoped trace lookup for Agent acceptance and debugging."""

import re

from fastapi import APIRouter, HTTPException, Request

from app.infra.config import INTERNAL_API_TOKEN
from app.infra.db.trace import get_trace_run

router = APIRouter()
_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


@router.get("/agent/traces/{trace_id}")
async def agent_trace(trace_id: str, request: Request):
    tenant_id = request.headers.get("X-Tenant-Id", "")
    if (not INTERNAL_API_TOKEN or
            request.headers.get("X-Internal-Token") != INTERNAL_API_TOKEN or
            not tenant_id):
        raise HTTPException(status_code=403, detail="Agent trace identity verification failed")
    if not _ID.fullmatch(trace_id):
        raise HTTPException(status_code=400, detail="Invalid trace id")
    run = await get_trace_run(request.app, tenant_id, trace_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return {"code": 0, "data": run}
