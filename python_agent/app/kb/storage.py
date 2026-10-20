"""知识库文件落盘、URL 生成、文本读取与索引入队（从 routers_kb.py 拆出）。"""
import json
import logging
import os

from app.infra.db.settings import get_platform_setting
from app.infra.db_pool import _pool_conn
from app.kb.parser import extract_text

logger = logging.getLogger("agent.routers_kb")

DEFAULT_MAX_FILE_MB = 10

# 知识库原文件存储目录（共享卷，nginx 以 /kb_files/ 静态服务）
KB_FILE_DIR = os.getenv("KB_FILE_DIR", "/app/data/kb_files")
KB_FILE_URL_PREFIX = "/kb_files/"
# Redis 异步索引队列
KB_INDEX_QUEUE = "cs:kb:index:queue"


async def _kb_max_file_mb(app) -> int:
    """读取知识库单文件大小上限（MB），平台设置优先，env KB_MAX_FILE_MB 兜底"""
    try:
        async with _pool_conn(app) as conn:
            v = await get_platform_setting(conn, "kb_max_file_mb", "")
        if v and v.strip().isdigit():
            return max(1, int(v))
    except Exception:
        pass
    try:
        return max(1, int(os.getenv("KB_MAX_FILE_MB", DEFAULT_MAX_FILE_MB)))
    except Exception:
        return DEFAULT_MAX_FILE_MB


def _file_ext(file_name: str) -> str:
    """从文件名取小写扩展名（含点，无扩展名返回 ''）"""
    name = (file_name or "").strip()
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[1].lower()


def kb_file_url(tenant_id: str, doc_id: str, file_name: str) -> str:
    """生成知识库原文件静态下载 URL（nginx /kb_files/ 直接服务）"""
    ext = _file_ext(file_name)
    if not ext:
        return ""
    return f"{KB_FILE_URL_PREFIX}{tenant_id}/{doc_id}{ext}"


def save_kb_file(tenant_id: str, doc_id: str, file_name: str, raw: bytes) -> str:
    """落盘原文件到共享卷，返回绝对路径"""
    ext = _file_ext(file_name) or ".bin"
    d = os.path.join(KB_FILE_DIR, tenant_id)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, doc_id + ext)
    with open(path, "wb") as f:
        f.write(raw)
    return path


def delete_kb_file(tenant_id: str, doc_id: str, file_name: str) -> None:
    """删除共享卷中的原文件（尽力而为）"""
    try:
        ext = _file_ext(file_name)
        if ext:
            path = os.path.join(KB_FILE_DIR, tenant_id, doc_id + ext)
            if os.path.exists(path):
                os.remove(path)
    except Exception:
        pass


def read_kb_file_text(tenant_id: str, doc_id: str, file_name: str) -> str:
    """从共享卷读取原文件并提取文本（无文件/解析失败返回 ''）"""
    try:
        ext = _file_ext(file_name) or ".bin"
        p = os.path.join(KB_FILE_DIR, tenant_id, doc_id + ext)
        if not os.path.exists(p):
            return ""
        with open(p, "rb") as f:
            raw = f.read()
        return extract_text(file_name, raw) or ""
    except Exception:
        return ""


async def enqueue_index(app, doc_id: str, tenant_id: str) -> bool:
    """推入 Redis 异步索引队列；Redis 不可用返回 False（强制依赖）"""
    r = getattr(app.state, "redis_client", None)
    if r is None:
        return False
    try:
        await r.lpush(KB_INDEX_QUEUE, json.dumps({"doc_id": doc_id, "tenant_id": tenant_id}))
        return True
    except Exception as e:
        logger.warning("KB enqueue failed doc_id=%s: %s", doc_id, e)
        return False
