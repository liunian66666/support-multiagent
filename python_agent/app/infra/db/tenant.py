"""租户解析与向量集合命名（从 db_ops.py 拆出）。"""
from fastapi import Request

def tenant_from_thread(thread_id: str) -> str:
    """从 thread_id / user_id 提取租户 ID（格式 {tenant_id}:{uid}）"""
    if ":" in thread_id:
        return thread_id.split(":", 1)[0]
    return ""


def tenant_from_request(request: Request) -> str:
    """从管理端透传头 X-Tenant-Id 提取租户"""
    return (request.headers.get("X-Tenant-Id") or "").strip()


def collection_name(tenant_id: str) -> str:
    """Qdrant 集合按租户隔离：kb_{tenant_id}"""
    return f"kb_{tenant_id}" if tenant_id else "kb_default"
