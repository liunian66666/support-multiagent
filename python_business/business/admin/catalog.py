"""Ratings and quick-reply administration."""

import re
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from business.admin.auth import Admin, require_perm
from business.core.errors import deny
from business.infra.storage import many, one, write

router = APIRouter()

def to_int(value, default=None):
    """宽松解析：整数解析忽略错误（筛选参数非法即视为未传）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def go_parse_uint(value):
    """只认纯十进制数字，其余取 0。

    注意与 int() 的差别：不接受前导/尾随空格、正负号、小数点，
    `/api/admin/members/ 5` 这类路径一律落到 id=0 分支。
    """
    text = value if isinstance(value, str) else ""
    return int(text) if re.fullmatch(r"[0-9]{1,20}", text or "") else 0


@router.get("/api/admin/ratings")
def admin_ratings(score: str = "", q: str = "", start: str = "", end: str = "",
                  page: str = "1", page_size: str = "10",
                  admin: Admin = Depends(require_perm("rating:view"))):
    where, args = ["tenant_id = %s"], [admin.tenant_id]
    score = score.strip()
    if score:
        v = to_int(score)
        if v is not None and 1 <= v <= 5:
            where.append("score = %s")
            args.append(v)
    q = q.strip()
    if q:
        where.append("(thread_id LIKE %s OR tags LIKE %s OR comment LIKE %s)")
        args.extend(["%" + q + "%"] * 3)
    if start.strip():
        where.append("created_at >= %s")
        args.append(start.strip())
    if end.strip():
        where.append("created_at <= %s")
        args.append(end.strip() + " 23:59:59")
    p = max(1, to_int(page, 1))
    size = to_int(page_size, 10)
    if size < 1 or size > 100:
        size = 10
    clause = " AND ".join(where)
    total = one(f"SELECT COUNT(*) AS n FROM satisfaction_ratings WHERE {clause}", tuple(args))["n"]
    rows = many(
        f"SELECT id, tenant_id, thread_id, ticket_id, user_key, score, tags, comment, "
        f"created_at, updated_at FROM satisfaction_ratings WHERE {clause} "
        f"ORDER BY created_at DESC LIMIT %s OFFSET %s",
        tuple(args) + (size, (p - 1) * size),
    )
    return {"code": 0, "data": {"list": list(rows), "total": total}}


@router.get("/api/admin/ratings/stats")
def admin_rating_stats(admin: Admin = Depends(require_perm("rating:view"))):
    dist = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    total, score_sum = 0, 0
    for row in many(
        "SELECT score, COUNT(*) AS n FROM satisfaction_ratings "
        "WHERE tenant_id = %s GROUP BY score", (admin.tenant_id,),
    ):
        n = int(row["n"])
        total += n
        score_sum += int(row["score"] or 0) * n
        if row["score"] in dist:
            dist[row["score"]] = n
    avg = score_sum / total if total else 0.0
    satisfied = dist[4] + dist[5]
    rate = satisfied / total * 100 if total else 0.0
    days = []
    for i in range(6, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        nxt = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        row = one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(score), 0) AS s FROM satisfaction_ratings "
            "WHERE tenant_id = %s AND created_at >= %s AND created_at < %s",
            (admin.tenant_id, day, nxt),
        )
        n = int(row["n"] or 0)
        days.append({"date": day, "count": n,
                     "avg": (float(row["s"] or 0) / n) if n else 0.0})
    return {"code": 0, "data": {"total": total, "avg_score": avg, "dist": dist,
                                "satisfied": satisfied, "satisfy_rate": rate,
                                "last7days": days}}


QUICK_REPLY_COLUMNS = ("id, tenant_id, category, title, content, sort, is_active, "
                       "created_at, updated_at")


@router.get("/api/admin/quick-replies")
def admin_quick_replies(q: str = "", category: str = "", active: str = "",
                        admin: Admin = Depends(require_perm("quickreply:view"))):
    where, args = ["tenant_id = %s", "(deleted_at = 0 OR deleted_at IS NULL)"], [admin.tenant_id]
    q, category = q.strip(), category.strip()
    if q:
        where.append("(title LIKE %s OR content LIKE %s)")
        args.extend(["%" + q + "%"] * 2)
    if category:
        where.append("category = %s")
        args.append(category)
    active = active.strip()
    if active:
        v = to_int(active)
        if v in (0, 1):
            where.append("is_active = %s")
            args.append(v)
    rows = many(
        f"SELECT {QUICK_REPLY_COLUMNS} FROM quick_replies WHERE {' AND '.join(where)} "
        f"ORDER BY sort ASC, id DESC",
        tuple(args),
    )
    return {"code": 0, "data": list(rows)}


class QuickReplyCreate(BaseModel):
    category: str = ""
    title: str = ""
    content: str = ""
    sort: int = 0
    is_active: int | None = None


class QuickReplyPatch(BaseModel):
    category: str | None = None
    title: str | None = None
    content: str | None = None
    sort: int | None = None
    is_active: int | None = None


@router.post("/api/admin/quick-replies")
def admin_quick_reply_create(body: QuickReplyCreate, request: Request,
                             admin: Admin = Depends(require_perm("quickreply:add"))):
    title, content = body.title.strip(), body.content.strip()
    if not title or not content:
        deny(400, "标题和内容不能为空")
    is_active = body.is_active if body.is_active in (0, 1) else 1
    category = body.category.strip() or "通用"
    write(
        "INSERT INTO quick_replies (tenant_id, category, title, content, sort, is_active, "
        "created_at, updated_at, deleted_at) VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW(), 0)",
        (admin.tenant_id, category, title, content, body.sort, is_active),
    )
    row = one(
        f"SELECT {QUICK_REPLY_COLUMNS} FROM quick_replies WHERE tenant_id = %s "
        f"ORDER BY id DESC LIMIT 1",
        (admin.tenant_id,),
    )
    return {"code": 0, "data": row}

@router.put("/api/admin/quick-replies/{reply_id}")
def admin_quick_reply_update(reply_id: int, body: QuickReplyPatch, request: Request,
                             admin: Admin = Depends(require_perm("quickreply:edit"))):
    row = one(
        "SELECT id, title FROM quick_replies WHERE tenant_id = %s AND id = %s "
        "AND (deleted_at = 0 OR deleted_at IS NULL) LIMIT 1",
        (admin.tenant_id, reply_id),
    )
    if not row:
        deny(404, "快捷回复不存在")
    fields, args = [], []
    if body.category is not None:
        fields.append("category = %s")
        args.append(body.category.strip() or "通用")
    if body.title is not None:
        value = body.title.strip()
        if not value:
            deny(400, "标题不能为空")
        fields.append("title = %s")
        args.append(value)
    if body.content is not None:
        value = body.content.strip()
        if not value:
            deny(400, "内容不能为空")
        fields.append("content = %s")
        args.append(value)
    if body.sort is not None:
        fields.append("sort = %s")
        args.append(body.sort)
    if body.is_active in (0, 1):
        fields.append("is_active = %s")
        args.append(body.is_active)
    if fields:
        fields.append("updated_at = NOW()")
        write(f"UPDATE quick_replies SET {', '.join(fields)} WHERE tenant_id = %s AND id = %s",
              tuple(args) + (admin.tenant_id, reply_id))
    return {"code": 0, "msg": "已更新"}


@router.delete("/api/admin/quick-replies/{reply_id}")
def admin_quick_reply_delete(reply_id: int, request: Request,
                             admin: Admin = Depends(require_perm("quickreply:remove"))):
    row = one(
        "SELECT id, title FROM quick_replies WHERE tenant_id = %s AND id = %s "
        "AND (deleted_at = 0 OR deleted_at IS NULL) LIMIT 1",
        (admin.tenant_id, reply_id),
    )
    if not row:
        deny(404, "快捷回复不存在")
    write(
        "UPDATE quick_replies SET deleted_at = %s, updated_at = NOW() "
        "WHERE tenant_id = %s AND id = %s",
        (int(time.time()), admin.tenant_id, reply_id),
    )
    return {"code": 0, "msg": "已删除"}


