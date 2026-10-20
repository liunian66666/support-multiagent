"""内部接口鉴权与租户一致性校验（从 main.py 拆出）。"""
from fastapi import HTTPException, Request

from app.infra.config import INTERNAL_API_TOKEN
from app.infra.db.tenant import tenant_from_thread

def _require_trusted_chat(request: Request, thread_id: str) -> str:
    """The Go gateway is the only authenticated chat entry point."""
    tenant_id = tenant_from_thread(thread_id)
    if (not INTERNAL_API_TOKEN or
        request.headers.get("X-Internal-Token") != INTERNAL_API_TOKEN or
        not tenant_id or
        request.headers.get("X-Tenant-Id") != tenant_id):
        raise HTTPException(status_code=403, detail="Agent chat identity verification failed")
    return tenant_id
