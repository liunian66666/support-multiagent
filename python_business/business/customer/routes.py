"""Customer authentication, owned orders, messages and ticket view."""

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import redis
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel

from business.core.constants import ORDER_ID, TENANT_ID, TOKEN_TTL
from business.core.errors import deny
from business.infra.storage import cache, many, one, write

router = APIRouter()


def tenant(tenant_id):
    if not isinstance(tenant_id, str) or not TENANT_ID.fullmatch(tenant_id):
        deny(401, "无效的客户会话令牌")
    row = one(
        "SELECT tenant_id, app_secret_hash FROM tenants "
        "WHERE tenant_id = %s AND status = 1 AND deleted_at = 0 LIMIT 1",
        (tenant_id,),
    )
    if not row or not row["app_secret_hash"]:
        deny(401, "租户不存在或已停用")
    return row


def signed_token(payload, secret_hash):
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    signature = hmac.new(secret_hash.encode(), raw, hashlib.sha256).digest()
    return (base64.urlsafe_b64encode(raw).rstrip(b"=").decode() + "." +
            base64.urlsafe_b64encode(signature).rstrip(b"=").decode())


def decode_customer_token(token):
    if not token or len(token) > 4096 or token.count(".") != 1:
        deny(401, "无效的客户会话令牌")
    encoded, signature = token.split(".", 1)
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if len(raw) > 2048:
            deny(401, "无效的客户会话令牌")
        claims = json.loads(raw)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        deny(401, "无效的客户会话令牌")
    if not isinstance(claims, dict) or claims.get("kind") != "customer":
        deny(401, "无效的客户会话令牌")
    tid, cid, sid = claims.get("tid"), claims.get("cid"), claims.get("sess")
    if (not isinstance(tid, str) or not isinstance(cid, str) or
        not isinstance(sid, str) or not cid.startswith(tid + ":") or
        not isinstance(claims.get("exp"), int) or claims["exp"] <= time.time()):
        deny(401, "无效的客户会话令牌")
    secret_hash = tenant(tid)["app_secret_hash"]
    expected = base64.urlsafe_b64encode(
        hmac.new(secret_hash.encode(), raw, hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    if not hmac.compare_digest(expected, signature):
        deny(401, "无效的客户会话令牌")
    return claims


@dataclass(frozen=True)
class Customer:
    tenant_id: str
    customer_id: str
    thread_id: str
    session_id: str
    is_guest: bool
    is_preview: bool


def customer_from_row(row, *, tenant_id, customer_id=None, thread_id=None):
    if (not row or row["tenant_id"] != tenant_id or
        (customer_id is not None and row["customer_id"] != customer_id) or
        (thread_id is not None and row["thread_id"] != thread_id) or
        not row["customer_id"].startswith(tenant_id + ":") or
        not row["thread_id"].startswith(row["customer_id"] + ":")):
        deny(401, "客户会话无效或已过期")
    return Customer(row["tenant_id"], row["customer_id"], row["thread_id"],
                    row["token"], bool(row["is_guest"]), bool(row["is_preview"]))


def current_customer(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        deny(401, "缺少客户会话令牌")
    return customer_for_token(authorization.removeprefix("Bearer ").strip())


def customer_for_token(token):
    """Shared HTTP and WebSocket customer identity verification."""
    try:
        if cache().exists("cs:tok:bl:" + token):
            deny(401, "客户会话已登出")
    except redis.RedisError:
        deny(503, "会话校验暂时不可用")
    claims = decode_customer_token(token)
    row = one(
        "SELECT tenant_id, customer_id, thread_id, token, is_guest, is_preview "
        "FROM customer_sessions WHERE token = %s AND expires_at > NOW() LIMIT 1",
        (claims["sess"],),
    )
    return customer_from_row(row, tenant_id=claims["tid"], customer_id=claims["cid"])


def owned(customer):
    if customer.is_guest:
        deny(401, "查询个人订单请先登录客户账号")
    if customer.is_preview:
        deny(404, "未找到订单或订单不属于您")
    return customer


class DemoLogin(BaseModel):
    app_key: str
    app_secret: str
    customer_id: str = ""


class PreviewLogin(BaseModel):
    app_key: str
    app_secret: str


def allow_login(request, *, purpose, limit):
    """Rate limit through Redis; Nginx sets X-Real-IP for public requests."""
    ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )
    try:
        ip = str(ipaddress.ip_address(ip))
    except ValueError:
        ip = "unknown"
    key = "cs:biz:login:minute:" + hashlib.sha256(f"{purpose}:{ip}".encode()).hexdigest()
    try:
        attempts = cache().eval(
            "local n=redis.call('INCR', KEYS[1]); "
            "if n==1 then redis.call('EXPIRE', KEYS[1], 60) end; return n",
            1, key,
        )
    except redis.RedisError:
        deny(503, "登录服务暂时不可用")
    if attempts > limit:
        deny(429, "尝试过于频繁，请稍后再试")


def issue_session(tenant_id, customer_id, *, is_guest=False, is_preview=False):
    """Issue a Go-compatible customer token and persist its server-side session."""
    tenant_row = tenant(tenant_id)
    session_id = secrets.token_hex(16)
    thread_id = (f"{customer_id}:"
                 f"{datetime.now(timezone.utc):%Y%m%d%H%M%S}_{secrets.token_hex(4)}")
    payload = {"tid": tenant_id, "exp": int(time.time()) + TOKEN_TTL,
               "nonce": secrets.token_hex(16), "kind": "customer",
               "cid": customer_id, "sess": session_id}
    token = signed_token(payload, tenant_row["app_secret_hash"])
    write(
        "INSERT INTO customer_sessions "
        "(tenant_id, customer_id, thread_id, token, is_preview, is_guest, expires_at, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, DATE_ADD(NOW(), INTERVAL 2 HOUR), NOW())",
        (tenant_id, customer_id, thread_id, session_id, int(is_preview), int(is_guest)),
    )
    return {"code": 0, "data": {"token": token, "tenant_id": tenant_id,
            "customer_id": customer_id, "thread_id": thread_id,
            "expires_in": TOKEN_TTL, "guest": is_guest}}


@router.post("/api/auth/customer-token")
def customer_token(body: DemoLogin, request: Request):
    if os.getenv("CS_AUTH_DEMO_CUSTOMER_LOGIN", "false").lower() not in {"true", "1"}:
        deny(403, "演示客户登录未启用")
    key = os.getenv("DEMO_CUSTOMER_APP_KEY", "")
    secret = os.getenv("DEMO_CUSTOMER_DEMO_BUYER_KEY_SECRET", "")
    tid = os.getenv("DEMO_CUSTOMER_TENANT_ID", "")
    allow = tuple(x.strip() for x in os.getenv("DEMO_CUSTOMER_ALLOWED", "").split(",") if x.strip())
    if not key or not secret or not tid or not allow:
        deny(503, "演示客户登录未配置")
    allow_login(request, purpose="buyer", limit=15)
    if not (hmac.compare_digest(body.app_key, key) and
            hmac.compare_digest(body.app_secret, secret)):
        deny(401, "无效的演示客户凭证")
    name = body.customer_id or allow[0]
    if name.startswith(tid + ":"):
        name = name[len(tid) + 1:]
    if name not in allow or ":" in name:
        deny(400, "customer_id 不在演示客户白名单内")
    cid = f"{tid}:{name}"
    return issue_session(tid, cid)


@router.post("/api/auth/guest-token")
def guest_token(request: Request):
    if os.getenv("CS_AUTH_DEMO_GUEST_CHAT", "false").lower() not in {"true", "1"}:
        deny(403, "访客咨询未启用")
    allow_login(request, purpose="guest", limit=12)
    tenant_id = os.getenv("CS_AUTH_DEMO_GUEST_TENANT_ID", "")
    if not tenant_id:
        deny(503, "访客咨询租户未配置")
    return issue_session(tenant_id, f"{tenant_id}:guest_{secrets.token_hex(8)}",
                         is_guest=True)


@router.post("/api/auth/merchant-preview-token")
def merchant_preview_token(body: PreviewLogin, request: Request):
    if os.getenv("CS_AUTH_MERCHANT_PREVIEW_LOGIN", "false").lower() not in {"true", "1"}:
        deny(403, "商家预览入口未启用")
    allow_login(request, purpose="preview", limit=15)
    row = one(
        "SELECT tenant_id, app_secret_hash, status, is_platform_admin FROM tenants "
        "WHERE app_key = %s AND deleted_at = 0 LIMIT 1",
        (body.app_key,),
    )
    provided = hashlib.sha256(body.app_secret.encode()).hexdigest()
    if (not row or row["status"] != 1 or row["is_platform_admin"] or
        not hmac.compare_digest(provided, row["app_secret_hash"])):
        deny(401, "无效的商家凭证")
    tenant_id = row["tenant_id"]
    return issue_session(
        tenant_id, f"{tenant_id}:merchant_preview_{secrets.token_hex(8)}",
        is_preview=True,
    )


@router.post("/api/auth/logout")
def logout(authorization: str | None = Header(default=None),
           customer: Customer = Depends(current_customer)):
    token = authorization.removeprefix("Bearer ").strip()
    write("DELETE FROM customer_sessions WHERE token = %s AND tenant_id = %s",
          (customer.session_id, customer.tenant_id))
    try:
        cache().set("cs:tok:bl:" + token, "1", ex=TOKEN_TTL)
    except redis.RedisError:
        # The session row is already gone; both Go and FastAPI reject this token.
        pass
    return {"code": 0, "msg": "已登出"}


@router.get("/api/customer/session")
def customer_session(customer: Customer = Depends(current_customer)):
    return {"code": 0, "data": {"tenant_id": customer.tenant_id,
            "customer_id": customer.customer_id, "thread_id": customer.thread_id,
            "is_guest": customer.is_guest, "is_preview": customer.is_preview}}


def list_orders(customer):
    rows = many(
        "SELECT order_id, item, status, create_date FROM orders "
        "WHERE tenant_id = %s AND customer_id = %s ORDER BY id DESC LIMIT 20",
        (customer.tenant_id, customer.customer_id),
    )
    return {"code": 0, "data": {"orders": rows}}


def order_detail(customer, order_id):
    if not ORDER_ID.fullmatch(order_id):
        deny(404, "未找到订单或订单不属于您")
    row = one(
        "SELECT order_id, item, amount, status, create_date FROM orders "
        "WHERE tenant_id = %s AND customer_id = %s AND order_id = %s LIMIT 1",
        (customer.tenant_id, customer.customer_id, order_id),
    )
    if not row:
        deny(404, "未找到订单或订单不属于您")
    row["amount"] = float(row["amount"])
    return {"code": 0, "data": row}


@router.get("/api/customer/orders")
def customer_orders(customer: Customer = Depends(current_customer)):
    return list_orders(owned(customer))


@router.get("/api/customer/orders/{order_id}")
def customer_order(order_id: str, customer: Customer = Depends(current_customer)):
    return order_detail(owned(customer), order_id)


# ==========================================================================
# 阶段 2：聊天记录与工单。thread_id 一律取自服务端会话，绝不接受请求参数传入。
# ==========================================================================

MESSAGE_PAGE_MAX = 500
TICKET_STATUS = {0: "pending", 1: "handling", 2: "cancelled", 3: "closed"}


def iso(value):
    """对齐 Go `time.Time` 的 RFC3339 序列化。

    MySQL DATETIME 不带时区，容器 TZ=Asia/Shanghai 下按本地时区补偏移，
    结果形如 `2026-10-20T21:03:17.123+08:00`；Go 在微秒为 0 时省略小数位，
    这里同样裁剪，避免时间字段在新旧两层之间出现字符串差异。
    """
    if not value:
        return None
    if value.tzinfo is None:
        value = value.astimezone()
    text = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        text += "." + f"{value.microsecond:06d}".rstrip("0")
    offset = value.strftime("%z")
    return text + "Z" if not offset else text + offset[:3] + ":" + offset[3:]


def customer_messages(customer, limit, before_id):
    """按会话取聊天记录。

    Go 侧 `GET /api/user/messages` 一次性返回全部消息，长会话会把整表塞给前端；
    这里保留相同字段，但默认只取最近 200 条、可用 before_id 往前翻页。
    """
    if limit is None or limit < 1:
        limit = 200
    if limit > MESSAGE_PAGE_MAX:
        limit = MESSAGE_PAGE_MAX
    sql = ("SELECT id, thread_id, role, content, msg_type, tenant_id, ticket_id, "
           "is_read, created_at FROM messages WHERE tenant_id = %s AND thread_id = %s")
    args = [customer.tenant_id, customer.thread_id]
    if before_id:
        sql += " AND id < %s"
        args.append(before_id)
    sql += " ORDER BY id DESC LIMIT %s"
    args.append(limit)
    rows = list(many(sql, tuple(args)))  # pymysql.fetchall() 返回 tuple，需转列表才能 reverse
    rows.reverse()  # 返回给前端时按时间正序，与 Go 的 ORDER BY id ASC 一致
    for row in rows:
        row["created_at"] = iso(row["created_at"])
    return {"code": 0, "data": rows}


@router.get("/api/customer/messages")
@router.get("/api/user/messages")
def list_messages(customer: Customer = Depends(current_customer),
                  limit: int | None = None, before_id: int | None = None):
    return customer_messages(customer, limit, before_id)


class RatingBody(BaseModel):
    score: int
    tags: str = ""
    comment: str = ""


def upsert_rating(customer, body):
    if not 1 <= body.score <= 5:
        deny(400, "评分需为 1-5 星")
    tags = (body.tags or "").strip()[:255]
    comment = (body.comment or "").strip()[:500]
    existing = one(
        "SELECT id FROM satisfaction_ratings "
        "WHERE tenant_id = %s AND thread_id = %s LIMIT 1",
        (customer.tenant_id, customer.thread_id),
    )
    if existing:
        # 同一会话重复评价覆盖旧值，与 Go 侧语义一致
        write(
            "UPDATE satisfaction_ratings SET score = %s, tags = %s, comment = %s, "
            "updated_at = NOW() WHERE id = %s",
            (body.score, tags, comment, existing["id"]),
        )
    else:
        write(
            "INSERT INTO satisfaction_ratings "
            "(tenant_id, thread_id, ticket_id, user_key, score, tags, comment, "
            "created_at, updated_at) VALUES (%s, %s, 0, %s, %s, %s, %s, NOW(), NOW())",
            (customer.tenant_id, customer.thread_id, customer.thread_id,
             body.score, tags, comment),
        )
    return {"code": 0, "msg": "评价成功"}


@router.post("/api/customer/rating")
@router.post("/api/user/rating")
def submit_rating(body: RatingBody, customer: Customer = Depends(current_customer)):
    return upsert_rating(customer, body)


def customer_ticket(customer):
    """当前会话最近一张工单的只读状态。

    客户侧此前只能从 WS 的 `ticket` 事件里看到一次工单号，刷新后无从查询；
    这里补一个只读入口，供前端展示「待接入 / 已接管」以及刷新后回显。
    """
    row = one(
        "SELECT ticket_no, status, priority, reason, handler_name, created_at, closed_at "
        "FROM tickets WHERE tenant_id = %s AND thread_id = %s AND deleted_at = 0 "
        "ORDER BY id DESC LIMIT 1",
        (customer.tenant_id, customer.thread_id),
    )
    if not row:
        return {"code": 0, "data": None}
    row["status_text"] = TICKET_STATUS.get(row["status"], "unknown")
    row["created_at"] = iso(row["created_at"])
    row["closed_at"] = iso(row["closed_at"])
    return {"code": 0, "data": row}


@router.get("/api/customer/ticket")
def get_customer_ticket(customer: Customer = Depends(current_customer)):
    return customer_ticket(customer)


def internal_customer(request, tenant_id, thread_id):
    expected = os.getenv("INTERNAL_API_TOKEN", "")
    supplied = request.headers.get("X-Internal-Token", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        deny(403, "订单查询身份校验失败")
    if not TENANT_ID.fullmatch(tenant_id) or not thread_id.startswith(tenant_id + ":"):
        deny(403, "订单查询身份校验失败")
    row = one(
        "SELECT tenant_id, customer_id, thread_id, token, is_guest, is_preview "
        "FROM customer_sessions WHERE tenant_id = %s AND thread_id = %s "
        "AND expires_at > NOW() LIMIT 1",
        (tenant_id, thread_id),
    )
    if not row:
        deny(404, "未找到订单或订单不属于您")
    return owned(customer_from_row(row, tenant_id=tenant_id, thread_id=thread_id))


@router.get("/api/agent/orders")
def agent_orders(request: Request, tenant_id: str, thread_id: str):
    return list_orders(internal_customer(request, tenant_id, thread_id))


@router.get("/api/agent/order")
def agent_order(request: Request, tenant_id: str, thread_id: str, order_id: str):
    return order_detail(internal_customer(request, tenant_id, thread_id), order_id)


@router.get("/api/business/order")
def retired_public_order():
    deny(410, "公开订单查询已停用；请通过客服会话核验身份")


def internal_guard(request):
    """Authenticate non-order Agent callbacks before reading business data."""
    expected = os.getenv("INTERNAL_API_TOKEN", "")
    supplied = request.headers.get("X-Internal-Token", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        deny(403, "内部接口身份校验失败")


