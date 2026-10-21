"""Parallel FastAPI customer business API. Orders are read-only."""

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pymysql
import httpx
import redis
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import (Depends, FastAPI, File, Form, Header, HTTPException, Query,
                     Request, UploadFile)
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

app = FastAPI(title="Customer Support Business API")
TENANT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ORDER_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
TOKEN_TTL = 7200


def db():
    return pymysql.connect(
        host=os.environ["MYSQL_HOST"], port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"], password=os.environ["MYSQL_PASSWORD"],
        database=os.environ["MYSQL_DATABASE"], charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, autocommit=True,
        connect_timeout=3, read_timeout=5, write_timeout=5,
    )


def cache():
    return redis.Redis.from_url(os.environ["REDIS_URL"], socket_timeout=3)


def one(sql, args=()):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()


def many(sql, args=()):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def write(sql, args):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, args)


def deny(code, message):
    raise HTTPException(status_code=code, detail=message)


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    # Go 里只有 handler 直接写真实状态码（c.JSON(403, ...)、中间件 abort）才算「真失败」，
    # 审计中间件据此跳过；这里用同一个标记表达，供审计中间件判断。
    request.state.denied = True
    return JSONResponse(status_code=exc.status_code,
                        content={"code": exc.status_code, "msg": str(exc.detail)})


def code_to_status(code):
    """对齐 Go middleware.codeToStatus：业务 code → 真实 HTTP 状态码。

    Go 侧大量 handler 写的是 `c.JSON(http.StatusOK, gin.H{"code": 500, ...})`，
    但全局 StatusCode 中间件会按 body.code 把 HTTP 状态改写成真实值
    （>=500 → 500；400/401/403/404/405/409/429 原样；其余 4xx → 400；<400 → 不改）。
    所以最终对外看到的状态码与 body.code 一致，FastAPI 侧直接照此对齐。
    """
    if code >= 500:
        return 500
    if code >= 400:
        return code if code in (400, 401, 403, 404, 405, 409, 429) else 400
    return 200


class SoftError(Exception):
    """Go 侧部分 handler 用 body code 表达业务失败（未经真实 4xx 写入）。

    与 deny() 的区别只在审计：Go 的审计中间件读到的是尚未改写的 200，
    因此这类「软失败」的写操作仍会被记审计日志 —— 必须保留这个行为。
    """

    def __init__(self, code, message):
        self.code, self.message = code, message


def soft(code, message):
    raise SoftError(code, message)


@app.exception_handler(SoftError)
async def soft_error(_request, exc):
    return JSONResponse(status_code=code_to_status(exc.code),
                        content={"code": exc.code, "msg": exc.message})


async def body_json(request: Request):
    """Go ShouldBindJSON 的等价物：空体 / 非法 JSON / 非对象一律返回 None。"""
    try:
        raw = await request.body()
    except Exception:                       # 连接中断等，视为无法解析
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


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
    token = authorization.removeprefix("Bearer ").strip()
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


@app.get("/health")
def health():
    try:
        one("SELECT 1 AS ok")
        cache().ping()
    except (pymysql.MySQLError, redis.RedisError, OSError, KeyError):
        deny(503, "业务依赖未就绪")
    return {"status": "ok"}


@app.post("/api/auth/customer-token")
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


@app.post("/api/auth/guest-token")
def guest_token(request: Request):
    if os.getenv("CS_AUTH_DEMO_GUEST_CHAT", "false").lower() not in {"true", "1"}:
        deny(403, "访客咨询未启用")
    allow_login(request, purpose="guest", limit=12)
    tenant_id = os.getenv("CS_AUTH_DEMO_GUEST_TENANT_ID", "")
    if not tenant_id:
        deny(503, "访客咨询租户未配置")
    return issue_session(tenant_id, f"{tenant_id}:guest_{secrets.token_hex(8)}",
                         is_guest=True)


@app.post("/api/auth/merchant-preview-token")
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


@app.post("/api/auth/logout")
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


@app.get("/api/customer/session")
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


@app.get("/api/customer/orders")
def customer_orders(customer: Customer = Depends(current_customer)):
    return list_orders(owned(customer))


@app.get("/api/customer/orders/{order_id}")
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


@app.get("/api/customer/messages")
@app.get("/api/user/messages")
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


@app.post("/api/customer/rating")
@app.post("/api/user/rating")
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


@app.get("/api/customer/ticket")
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


@app.get("/api/agent/orders")
def agent_orders(request: Request, tenant_id: str, thread_id: str):
    return list_orders(internal_customer(request, tenant_id, thread_id))


@app.get("/api/agent/order")
def agent_order(request: Request, tenant_id: str, thread_id: str, order_id: str):
    return order_detail(internal_customer(request, tenant_id, thread_id), order_id)


def internal_guard(request):
    """Authenticate non-order Agent callbacks before reading business data."""
    expected = os.getenv("INTERNAL_API_TOKEN", "")
    supplied = request.headers.get("X-Internal-Token", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        deny(403, "内部接口身份校验失败")


# ==================== 阶段 3：管理端（鉴权基座 + 评价/话术库） ====================
# 管理令牌与客户令牌同一签名格式，差别只在 kind：客户令牌 kind="customer"，
# 管理/子账号令牌 kind 缺省。这里沿用 Go VerifyTokenFull 的类型边界：
# kind 非空一律拒绝，绝不做「空 kind 兼容」猜测 —— 否则客户令牌会以 uid=0 冒充管理员。

BLACKLIST_PREFIX = "cs:tok:bl:"


@dataclass(frozen=True)
class Admin:
    tenant_id: str
    user_id: int                 # 0 = app_key 管理员（隐式全权限）
    is_platform_admin: bool


def admin_tenant(tenant_id):
    if not isinstance(tenant_id, str) or not TENANT_ID.fullmatch(tenant_id):
        deny(401, "令牌无效或已过期")
    row = one(
        "SELECT tenant_id, app_secret_hash, is_platform_admin FROM tenants "
        "WHERE tenant_id = %s AND status = 1 AND deleted_at = 0 LIMIT 1",
        (tenant_id,),
    )
    if not row or not row["app_secret_hash"]:
        deny(401, "租户不存在或已停用")
    return row


def decode_admin_token(token):
    if not token or len(token) > 4096 or token.count(".") != 1:
        deny(401, "令牌无效或已过期")
    encoded, signature = token.split(".", 1)
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if len(raw) > 2048:
            deny(401, "令牌无效或已过期")
        claims = json.loads(raw)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        deny(401, "令牌无效或已过期")
    if not isinstance(claims, dict):
        deny(401, "令牌无效或已过期")
    kind = claims.get("kind") or ""
    if kind:
        # 对齐 Go VerifyTokenFull：管理令牌 kind 必须缺省，任何非空 kind 一律视为无效令牌，
        # 且 Go 只回统一文案「令牌无效或已过期」（不区分 customer / 未知类型），
        # 这里同样不细分 —— 文案差异会让双端比对出现假阳性。
        deny(401, "令牌无效或已过期")
    tid, uid, exp = claims.get("tid"), claims.get("uid") or 0, claims.get("exp")
    if not isinstance(tid, str) or not isinstance(uid, int) or uid < 0:
        deny(401, "令牌无效或已过期")
    if not isinstance(exp, int) or exp <= time.time():
        deny(401, "令牌无效或已过期")
    secret_hash = admin_tenant(tid)["app_secret_hash"]
    expected = base64.urlsafe_b64encode(
        hmac.new(secret_hash.encode(), raw, hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    if not hmac.compare_digest(expected, signature):
        deny(401, "令牌签名无效")
    return claims


def current_admin(request: Request, authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        deny(401, "缺少访问令牌 token")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        if cache().exists(BLACKLIST_PREFIX + token):
            deny(401, "token 已失效（已登出）")
    except redis.RedisError:
        deny(503, "会话校验暂时不可用")
    claims = decode_admin_token(token)
    row = admin_tenant(claims["tid"])
    admin = Admin(claims["tid"], int(claims.get("uid") or 0),
                  bool(row["is_platform_admin"]))
    request.state.admin = admin
    return admin


def user_perms(user_id):
    rows = many(
        "SELECT DISTINCT m.perm FROM user_roles ur "
        "JOIN sys_role_menu rm ON rm.role_id = ur.role_id "
        "JOIN menus m ON m.id = rm.menu_id "
        "WHERE ur.user_id = %s AND m.status = 1 AND m.perm IS NOT NULL AND m.perm <> ''",
        (user_id,),
    )
    return {r["perm"] for r in rows}


def require_perm(perm, *, biz_scope=True):
    """对齐 Go RequirePerm + BusinessOnly。

    - uid=0：app_key 管理员，隐式全权限
    - uid>0：账号状态正常且角色含该权限码
    - biz_scope=True：平台管理员拒绝访问（Go 的 biz 组语义）；平台级端点传 False

    注意 Go `BusinessOnly()` 的白名单：平台管理员访问 `/api/admin/prompts` 前缀的路径
    会被**放行**（源码注释「平台管理员仅可访问平台级 Prompt 管理接口」）。这不是笔误 ——
    平台级 Prompt 存在 `tenant_id=''` 的全局行，只有平台管理员能管，若一并 403，
    `ProxyPrompt` 里 `?scope=platform` 的整条分支就不可达了。
    """
    def guard(request: Request,
              admin: Admin = Depends(current_admin)) -> Admin:
        if biz_scope and admin.is_platform_admin:
            if not request.url.path.startswith("/api/admin/prompts"):
                deny(403, "平台管理员不可访问租户业务数据，请使用对应租户账号")
        if admin.user_id == 0:
            return admin
        row = one(
            "SELECT id, tenant_id, status FROM users WHERE id = %s AND deleted_at = 0 LIMIT 1",
            (admin.user_id,),
        )
        if not row or row["tenant_id"] != admin.tenant_id:
            deny(403, "账号不存在或已删除")
        if row["status"] != 1:
            deny(403, "账号已禁用，请联系管理员")
        if perm not in user_perms(admin.user_id):
            deny(403, "无权限执行该操作")
        return admin
    return guard


AUDIT_OBJECT_TYPES = {
    "ticket": "ticket",
    "prompt": "prompt",
    "knowledge": "knowledge",
    "kb-doc": "knowledge",
    "knowledge-doc": "knowledge",
    "tenant": "tenant",
    "model-config": "model_config",
    "model-provider": "model_provider",
    "platform-config": "platform_config",
    "session": "session",
}


def audit_object_type(path):
    """逐字对齐 Go `auditObjectType`：取首段、去掉一个尾缀 s，命中白名单否则 other。

    注意 Go 的去尾 s 是「去掉一个 s」而非英语单数化，
    因此 quick-replies → quick-replie、ratings → rating、agent-status → agent-statu，
    前两个里只有 ratings 恰好命中（但 ratings 没有写接口），quick-replies 落 other。
    """
    segment = path.removeprefix("/api/admin/").split("/", 1)[0]
    if segment.endswith("s"):
        segment = segment[:-1]
    return AUDIT_OBJECT_TYPES.get(segment, "other")


def audit_object_id(request: Request):
    """对齐 Go：优先 `:id` 路由参数，否则取路径最后一段。"""
    value = request.path_params.get("id")
    if value is not None and str(value):
        return str(value)
    return request.url.path.rstrip("/").rsplit("/", 1)[-1]


def client_ip(request: Request):
    return (request.headers.get("X-Real-IP") or
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or
            (request.client.host if request.client else ""))


def write_audit(admin, request, action, object_id, path, detail=""):
    """对齐 Go `repository.WriteAuditLog`：best-effort，失败不影响主流程。"""
    operator = admin.tenant_id
    if admin.user_id > 0:
        row = one("SELECT username FROM users WHERE id = %s LIMIT 1", (admin.user_id,))
        if row:
            operator = row["username"]
    write(
        "INSERT INTO audit_logs (tenant_id, operator, user_id, action, object_type, "
        "object_id, detail, ip, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())",
        (admin.tenant_id, operator, admin.user_id, action, audit_object_type(path),
         object_id, detail, client_ip(request)),
    )


def operator_name(admin):
    """对齐 Go `currentOperator`：子账号记用户名；app_key 管理员记空串（历史约定）。"""
    if admin.user_id > 0:
        row = one("SELECT username FROM users WHERE id = %s LIMIT 1", (admin.user_id,))
        return (row["username"] or "") if row else ""
    return ""


def write_handler_audit(admin, request, action, object_type, object_id, detail):
    """handler 内手写的审计（object_type 由调用方指定，不按路径推导）。

    Go 侧 RBAC/菜单/话术等 handler 除 AuditMiddleware 之外还会额外写一条带 detail 的审计，
    两条都会落库 —— 这里同样双写，且 operator 语义取 currentOperator（uid=0 为空串）。
    """
    write(
        "INSERT INTO audit_logs (tenant_id, operator, user_id, action, object_type, "
        "object_id, detail, ip, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())",
        (admin.tenant_id, operator_name(admin), admin.user_id, action, object_type,
         object_id, detail, client_ip(request)),
    )


# ==================== 阶段 3-10：认证域（Go /api/auth/* 剩余 6 条） ====================
# 迁自 Go handler/auth.go + pkg/auth + pkg/crypto（docs/GO_RETIREMENT.md 步骤 2）：
#   GET  /api/auth/public-key   RSA 公钥下发（PKIX/SPKI PEM，前端加密 app_secret）
#   POST /api/auth/token        app_key + app_secret 登录（encrypted=true 时 RSA-OAEP 解密）
#   POST /api/auth/user-token   子账号登录（租户 + 用户名 + 密码）
#   POST /api/auth/refresh      管理/子账号令牌刷新（旧令牌进黑名单）
#   PUT  /api/auth/password     修改自己的登录密钥（需登录态）
#   GET  /api/auth/tenants      公开租户列表（status=1 且非平台管理员）
#
# 四条并行期不变量（双端共存期间必须成立，任一破坏都会出现「一边登录一边 401」）：
#   1. 令牌签发/验签算法与 Go 逐字节一致 —— signed_token 已被既有单测锁定
#   2. 黑名单同一 Redis 键空间 cs:tok:bl: —— 双端互认登出/刷新吊销
#   3. 登录限流同一 Redis 键空间 cs:login:fail: —— 防「一边锁完换一边试」
#   4. RSA 私钥同一文件 —— compose 把 ./data 挂进 go 与 business 两个容器，
#      任一端先启动都会生成/读到同一把密钥；只有同一把，前端加密的 secret 双端才解得开
#
# 与客户令牌（kind="customer"）的边界沿用 Go VerifyTokenFull：管理令牌 kind 必须缺省。

# 对齐 Go config.yaml auth.token_ttl: "24h"（Go 无环境变量覆盖，这里留口子便于测试）
ADMIN_TOKEN_TTL = int(os.getenv("AUTH_TOKEN_TTL", "86400"))
# 对齐 Go main.go：RSA_PRIVATE_KEY_PATH 缺省 data/rsa_private.pem（容器内 /app/data）
RSA_KEY_PATH = os.getenv("RSA_PRIVATE_KEY_PATH", "/app/data/rsa_private.pem")

# 登录防爆破：对齐 Go pkg/auth/login_limiter.go（5 次 / 10 分钟，Redis fail-closed）
LOGIN_FAIL_PREFIX = "cs:login:fail:"
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 600


def go_minutes(seconds):
    """Go time.Duration.Round(time.Minute).String() 的等价物（只可能是整分或 0）。"""
    minutes = int((seconds + 30) // 60)          # Go Round：半数舍入到远侧
    return "0s" if minutes <= 0 else f"{minutes}m0s"


def check_login_allowed(key):
    """对齐 Go auth.CheckLoginAllowed：返回 (allowed, wait_seconds)。

    Redis 查询失败 → 拒绝（fail-closed：防爆破失效比拒绝一次登录更严重），不计失败。
    """
    try:
        raw = cache().get(LOGIN_FAIL_PREFIX + key)
    except redis.RedisError:
        return False, 60
    if raw is None:
        return True, 0
    try:
        fails = int(raw)
    except (TypeError, ValueError):
        return True, 0
    if fails >= LOGIN_MAX_FAILS:
        try:
            ttl = cache().ttl(LOGIN_FAIL_PREFIX + key)
        except redis.RedisError:
            ttl = LOGIN_WINDOW
        if not isinstance(ttl, int) or ttl < 0:
            ttl = LOGIN_WINDOW
        return False, ttl
    return True, 0


def record_login_fail(key):
    """对齐 Go auth.RecordLoginFail：INCR + 首次 EXPIRE（窗口 10 分钟）。"""
    try:
        fails = cache().incr(LOGIN_FAIL_PREFIX + key)
        if fails == 1:
            cache().expire(LOGIN_FAIL_PREFIX + key, LOGIN_WINDOW)
    except redis.RedisError:
        pass


def clear_login_fails(key):
    """对齐 Go auth.ClearLoginFails。"""
    try:
        cache().delete(LOGIN_FAIL_PREFIX + key)
    except redis.RedisError:
        pass


def write_auth_audit(tenant_id, operator, action, object_id, detail, ip, user_id=0):
    """对齐 Go repository.WriteAuditLog（认证域专用）：best-effort，tenant_id 空 → system。

    登录/改密的审计 object_type 恒为 "auth"、object_id 由 Go handler 写死
    （app_key / user / app_secret），不走 AuditMiddleware 的路径推断。
    """
    write(
        "INSERT INTO audit_logs (tenant_id, operator, user_id, action, object_type, "
        "object_id, detail, ip, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())",
        (tenant_id or "system", operator, user_id, action, "auth",
         object_id, detail, ip),
    )


# ---------- RSA（对齐 go_service/internal/pkg/crypto/rsa.go） ----------

_RSA_LOCK = threading.Lock()
_RSA_KEY = None


def rsa_private_key():
    """懒加载 RSA 私钥；文件不存在则生成 2048 位并以 PKCS1 PEM 持久化。

    与 Go 共用同一路径（compose 将 ./data 挂入两个容器）：两边都遵循
    「文件在则加载、不在则生成」，先启动的一端落盘，后启动的一端读取同一把。
    任何解析/IO 失败都向上抛，由调用方转 500（Go 的 GetRSAKey 同样返回 err）。
    """
    global _RSA_KEY
    with _RSA_LOCK:
        if _RSA_KEY is not None:
            return _RSA_KEY
        try:
            with open(RSA_KEY_PATH, "rb") as fh:
                data = fh.read()
        except OSError:
            data = None
        if data:
            _RSA_KEY = serialization.load_pem_private_key(data, password=None)
        else:
            _RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            directory = os.path.dirname(RSA_KEY_PATH)
            if directory:
                os.makedirs(directory, exist_ok=True)
            pem = _RSA_KEY.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,   # PKCS1 "RSA PRIVATE KEY"，Go 同格式
                serialization.NoEncryption(),
            )
            with open(RSA_KEY_PATH, "wb") as fh:
                fh.write(pem)
            try:
                os.chmod(RSA_KEY_PATH, 0o600)
            except OSError:
                pass
        return _RSA_KEY


def rsa_public_pem():
    """对齐 Go crypto.PublicKeyPEM：PKIX/SPKI 格式公钥 PEM（前端 Web Crypto/JSEncrypt 通用）。

    与 Go 的 x509.MarshalPKIXPublicKey + pem "PUBLIC KEY" 输出逐字节一致
    （DER 确定性编码），compare39 直接比对两端 PEM 文本即可证明「同一把密钥」。
    """
    return rsa_private_key().public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def rsa_decrypt_oaep(cipher_b64):
    """对齐 Go crypto.DecryptRSAOAEP：base64(Std) + RSA-OAEP(SHA-256) 解密。"""
    data = base64.b64decode(cipher_b64)
    plain = rsa_private_key().decrypt(
        data,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                     algorithm=hashes.SHA256(),
                     label=None),
    )
    return plain.decode()


def extract_bearer(request):
    """对齐 Go middleware.ExtractToken：支持 "Bearer <token>" 与裸 token 两种格式。"""
    header = request.headers.get("authorization") or ""
    if not header:
        return ""
    if header.startswith("Bearer "):
        return header[len("Bearer "):].strip()
    return header


class AdminTokenError(Exception):
    """携带 Go 侧原文的令牌校验失败（refresh 专用，见 verify_admin_token_strict）。"""


def verify_admin_token_strict(token):
    """对齐 Go auth.RefreshToken → VerifyTokenFull 的完整错误链（仅 refresh 使用）。

    为什么不用 decode_admin_token：那条链的文案对齐的是 AdminAuth 中间件
    （「令牌无效或已过期」），而 Go 的刷新失败原文是 "token 刷新失败: <具体原因>"，
    且租户查找不过滤 status（TenantsList 含禁用租户，禁用租户令牌在 Go 仍可刷新）。
    刷新是双端互验的关键场景，文案与语义都必须逐字一致，故单独实现。
    返回 (tenant_id, uid)；失败抛 AdminTokenError（消息为 Go 的错误原文）。
    """
    try:
        if cache().exists(BLACKLIST_PREFIX + token):
            raise AdminTokenError("token 已失效（已登出）")
    except redis.RedisError:
        raise AdminTokenError("token 已失效（已登出）")     # fail-closed，对齐 Go
    if not token or len(token) > 4096 or token.count(".") != 1:
        raise AdminTokenError("token 格式错误")
    encoded, signature = token.split(".", 1)
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if len(raw) > 2048:
            raise AdminTokenError("token 解码失败")
        claims = json.loads(raw)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise AdminTokenError("token 解码失败")
    if not isinstance(claims, dict):
        raise AdminTokenError("token 载荷无效")
    tid, exp, uid, kind = (claims.get("tid"), claims.get("exp"),
                           claims.get("uid"), claims.get("kind"))
    # Go json.Unmarshal 的零值语义：null 字段留零值不报错，类型不符才报「载荷无效」
    if (isinstance(exp, bool) or isinstance(uid, bool)
            or (exp is not None and not isinstance(exp, int))
            or (uid is not None and not isinstance(uid, int))
            or (uid is not None and uid < 0)
            or (kind is not None and not isinstance(kind, str))
            or (tid is not None and not isinstance(tid, str))):
        raise AdminTokenError("token 载荷无效")
    tid, exp, uid, kind = tid or "", exp or 0, uid or 0, kind or ""
    if exp < time.time():
        raise AdminTokenError("token 已过期")
    row = one(
        "SELECT tenant_id, app_secret_hash FROM tenants "
        "WHERE tenant_id = %s AND deleted_at = 0 LIMIT 1",
        (tid,),
    )
    if not row or not row["app_secret_hash"]:
        raise AdminTokenError("租户不存在")
    expected = base64.urlsafe_b64encode(
        hmac.new(row["app_secret_hash"].encode(), raw, hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        raise AdminTokenError("token 签名无效")
    if kind == "customer":
        raise AdminTokenError("客户会话令牌不可用于管理端")
    if kind:
        raise AdminTokenError("未知令牌类型，已拒绝")
    return tid, uid or 0


def blacklist_admin_token(token):
    """对齐 Go auth.BlacklistToken：TTL 取令牌剩余有效期（到期边缘兜底 60 秒）。"""
    ttl = 60
    try:
        encoded = token.split(".", 1)[0]
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        claims = json.loads(raw)
        remain = int(claims.get("exp") or 0) - int(time.time())
        ttl = remain if remain > 0 else 60
    except (ValueError, UnicodeDecodeError, binascii.Error, TypeError):
        ttl = 60
    try:
        cache().set(BLACKLIST_PREFIX + token, "1", ex=ttl)
    except redis.RedisError:
        pass


def admin_token_payload(tenant_id, uid=0):
    """对齐 Go tokenPayload 的字段顺序（tid, exp, nonce[, uid]）——signed_token 逐字节一致。

    Go 的 UID 带 omitempty：uid=0（app_key 管理员）不出现在载荷里。
    """
    payload = {"tid": tenant_id, "exp": int(time.time()) + ADMIN_TOKEN_TTL,
               "nonce": secrets.token_hex(16)}
    if uid:
        payload["uid"] = uid
    return payload


def login_denied(limiter, audit_args, status, message):
    """登录失败统一出口：记防爆破 + 写审计 + 按 Go 的状态码拒绝。"""
    record_login_fail(limiter)
    try:
        write_auth_audit(*audit_args)
    except Exception:
        pass
    deny(status, message)


@app.get("/api/auth/public-key")
def auth_public_key():
    try:
        pem = rsa_public_pem()
    except Exception:
        deny(500, "RSA 密钥未就绪")
    return {"code": 0, "data": {"public_key": pem}}


@app.post("/api/auth/token")
async def auth_token(request: Request):
    """对齐 Go handler.IssueTokenHandler：app_key + app_secret 换管理令牌。"""
    body = await body_json(request)
    if not isinstance(body, dict):
        deny(400, "参数错误")
    app_key, app_secret = body.get("app_key"), body.get("app_secret")
    encrypted = body.get("encrypted", False)
    if (not isinstance(app_key, str) or not app_key
            or not isinstance(app_secret, str) or not app_secret
            or not isinstance(encrypted, bool)):
        deny(400, "参数错误")
    ip = client_ip(request)
    limiter = ip + ":" + app_key
    allowed, wait = check_login_allowed(limiter)
    if not allowed:
        deny(429, "尝试过于频繁，请 " + go_minutes(wait) + " 后再试")
    secret = app_secret
    if encrypted:
        try:
            secret = rsa_decrypt_oaep(app_secret)
        except Exception:
            login_denied(limiter, ("", app_key, "login_fail", "app_key", "解密失败", ip),
                         401, "app_secret 解密失败")
    row = one(
        "SELECT tenant_id, name, app_secret_hash, status, is_platform_admin "
        "FROM tenants WHERE app_key = %s AND deleted_at = 0 LIMIT 1",
        (app_key,),
    )
    provided = hashlib.sha256(secret.encode()).hexdigest()
    if (not row or row["status"] != 1 or not row["app_secret_hash"]
            or not hmac.compare_digest(provided.encode(),
                                       row["app_secret_hash"].encode())):
        login_denied(limiter, ("", app_key, "login_fail", "app_key",
                               "无效的 app_key 或 app_secret", ip),
                     401, "无效的 app_key 或 app_secret")
    clear_login_fails(limiter)
    token = signed_token(admin_token_payload(row["tenant_id"]),
                         row["app_secret_hash"])
    try:
        write_auth_audit(row["tenant_id"], app_key, "login_ok", "app_key",
                         "登录成功", ip)
    except Exception:
        pass
    return {"code": 0, "data": {
        "token": token, "tenant_id": row["tenant_id"],
        "expires_in": ADMIN_TOKEN_TTL,
        "is_platform_admin": bool(row["is_platform_admin"]),
        "is_admin": True,                       # app_key 管理员全权（uid=0）
        "nickname": row["tenant_id"],           # app_key 无成员昵称，右上角展示租户 ID
        "role_name": "管理员",
        "tenant_name": row["name"] or row["tenant_id"],
    }}


@app.post("/api/auth/user-token")
async def auth_user_token(request: Request):
    """对齐 Go handler.UserTokenHandler：子账号（uid>0）登录。"""
    body = await body_json(request)
    if not isinstance(body, dict):
        deny(400, "参数错误")
    tenant_id, username = body.get("tenant_id"), body.get("username")
    password, encrypted = body.get("password"), body.get("encrypted", False)
    if (not isinstance(tenant_id, str) or not tenant_id
            or not isinstance(username, str) or not username
            or not isinstance(password, str) or not password
            or not isinstance(encrypted, bool)):
        deny(400, "参数错误")
    ip = client_ip(request)
    limiter = ip + ":" + username
    allowed, wait = check_login_allowed(limiter)
    if not allowed:
        deny(429, "尝试过于频繁，请 " + go_minutes(wait) + " 后再试")
    plain = password
    if encrypted:
        try:
            plain = rsa_decrypt_oaep(password)
        except Exception:
            login_denied(limiter, (tenant_id, username, "login_fail", "user",
                                   "密码解密失败", ip),
                         401, "密码解密失败")
    try:
        ensure_tenant_roles(tenant_id)
    except pymysql.MySQLError:
        deny(500, "角色初始化失败")
    user = one(
        "SELECT id, tenant_id, username, nickname, password_hash, status "
        "FROM users WHERE tenant_id = %s AND username = %s AND deleted_at = 0 LIMIT 1",
        (tenant_id, username),
    )
    if not user:
        login_denied(limiter, (tenant_id, username, "login_fail", "user",
                               "用户名或密码错误", ip),
                     401, "用户名或密码错误")
    if user["status"] != 1:
        login_denied(limiter, (tenant_id, username, "login_fail", "user",
                               "账号已禁用", ip, user["id"]),
                     403, "账号已禁用，请联系管理员")
    if not hmac.compare_digest((user["password_hash"] or "").encode(),
                               hash_user_password(tenant_id, username, plain).encode()):
        login_denied(limiter, (tenant_id, username, "login_fail", "user",
                               "用户名或密码错误", ip, user["id"]),
                     401, "用户名或密码错误")
    clear_login_fails(limiter)
    write("UPDATE users SET last_login_at = NOW(), updated_at = NOW() WHERE id = %s",
          (user["id"],))
    row = one(
        "SELECT tenant_id, name, app_secret_hash, status FROM tenants "
        "WHERE tenant_id = %s AND deleted_at = 0 LIMIT 1",
        (tenant_id,),
    )
    # Go IssueUserToken：租户缺失 → 401 租户不存在；租户停用 → 401 租户已停用
    # （注意这两次拒绝发生在 last_login 已更新之后，与 Go 的顺序一致）
    if not row:
        deny(401, "租户不存在")
    if row["status"] != 1:
        deny(401, "租户已停用")
    token = signed_token(admin_token_payload(tenant_id, user["id"]),
                         row["app_secret_hash"])
    roles = get_user_roles(user["id"])
    role_name = roles[0]["name"] if roles else ""
    role_code = roles[0]["code"] if roles else ""
    try:
        write_auth_audit(tenant_id, username, "login_ok", "user",
                         "账号登录成功", ip, user["id"])
    except Exception:
        pass
    return {"code": 0, "data": {
        "token": token, "tenant_id": tenant_id,
        "expires_in": ADMIN_TOKEN_TTL,
        "is_platform_admin": False, "user_id": user["id"],
        "is_admin": is_full_perms(user_perms(user["id"])),
        "nickname": user["nickname"] or username,
        "role_name": role_name,
        "tenant_name": row["name"] or tenant_id,
        "role": role_name, "role_code": role_code,
        "permissions": sorted(user_perms(user["id"])),
        "roles": roles,
    }}


@app.post("/api/auth/refresh")
def auth_refresh(request: Request):
    """对齐 Go handler.RefreshTokenHandler：只刷新管理/子账号令牌（kind 缺省）。

    客户会话令牌在此被拒（Go 的 VerifyTokenFull 类型边界），到期后由前端重新走
    客户登录，绝不转换令牌类型；子账号刷新后保留原 uid 与权限身份。
    """
    old_token = extract_bearer(request)
    if not old_token:
        deny(401, "未提供 token")
    try:
        tid, uid = verify_admin_token_strict(old_token)
    except AdminTokenError as exc:
        deny(401, "token 刷新失败: " + str(exc))
    row = one(
        "SELECT tenant_id, name, app_secret_hash, is_platform_admin FROM tenants "
        "WHERE tenant_id = %s AND deleted_at = 0 LIMIT 1",
        (tid,),
    )
    if not row or not row["app_secret_hash"]:
        deny(401, "token 刷新失败: 租户不存在")
    new_token = signed_token(admin_token_payload(tid, uid), row["app_secret_hash"])
    blacklist_admin_token(old_token)          # 旧令牌立即失效，防重复使用
    data = {
        "token": new_token, "tenant_id": tid, "expires_in": ADMIN_TOKEN_TTL,
        "is_platform_admin": bool(row["is_platform_admin"]),
        "is_admin": True, "nickname": tid, "role_name": "管理员",
        "tenant_name": row["name"] or tid,
    }
    if uid > 0:
        # 子账号：按真实账号与角色解析身份；账号不存在/已删除 → 拒绝刷新（对齐 Go）
        user = load_user(uid)
        if not user:
            deny(401, "账号不存在或已禁用，请重新登录")
        roles = get_user_roles(uid)
        data["user_id"] = user["id"]
        data["is_admin"] = is_full_perms(user_perms(uid))
        data["nickname"] = user["nickname"] or user["username"]
        data["role_name"] = roles[0]["name"] if roles else ""
    return {"code": 0, "data": data}


@app.put("/api/auth/password")
async def auth_password(request: Request,
                        admin: Admin = Depends(current_admin)):
    """对齐 Go handler.ChangePasswordHandler：修改自己的登录密钥（app_secret）。

    路由在 Go 侧挂 `middleware.AdminAuth()`——**中间件先于 handler 执行**，
    因此「无令牌 + 非法请求体」返回的是 401 而不是 400，这里用 Depends 复刻同一顺序。
    旧密钥支持 RSA 加密传输；更新后旧令牌因签名密钥变化双端同时失效。
    """
    body = await body_json(request)
    if not isinstance(body, dict):
        deny(400, "参数错误")
    old_secret, new_secret = body.get("old_app_secret"), body.get("new_app_secret")
    encrypted = body.get("encrypted", False)
    if (not isinstance(old_secret, str) or not old_secret
            or not isinstance(new_secret, str) or not new_secret
            or not isinstance(encrypted, bool)):
        deny(400, "参数错误")
    if encrypted:
        try:
            old_secret = rsa_decrypt_oaep(old_secret)
        except Exception:
            deny(400, "旧密钥解密失败")
        try:
            new_secret = rsa_decrypt_oaep(new_secret)
        except Exception:
            deny(400, "新密钥解密失败")
    if len(new_secret) < 8:
        deny(400, "新密钥长度至少 8 位")
    row = one(
        "SELECT tenant_id, app_secret_hash FROM tenants "
        "WHERE tenant_id = %s AND deleted_at = 0 LIMIT 1",
        (admin.tenant_id,),
    )
    failure = None
    if not row:
        failure = "租户不存在"
    elif not hmac.compare_digest((row["app_secret_hash"] or "").encode(),
                                 hashlib.sha256(old_secret.encode()).hexdigest().encode()):
        failure = "旧密钥不正确"
    if failure is None:
        try:
            write("UPDATE tenants SET app_secret_hash = %s WHERE tenant_id = %s",
                  (hashlib.sha256(new_secret.encode()).hexdigest(), admin.tenant_id))
        except pymysql.MySQLError:
            failure = "密钥保存失败"
    if failure is not None:
        try:
            write_auth_audit(admin.tenant_id, "", "change_password_fail",
                             "app_secret", failure, client_ip(request))
        except Exception:
            pass
        deny(401, failure)
    try:
        write_auth_audit(admin.tenant_id, "", "change_password", "app_secret",
                         "修改登录密钥成功", client_ip(request))
    except Exception:
        pass
    # Go 的 ChangeTenantSecret 末尾 RefreshTenants()；本层无内存租户缓存，
    # 但 Go 进程内有（TenantsList 供 WS/auth 验签）——不重载会出现
    # 「新密钥签发的令牌过不了 WS 握手、旧密钥签发的反而还能用」的割裂。
    reload_go_tenants()
    return {"code": 0, "msg": "密码修改成功，请重新登录"}


@app.get("/api/auth/tenants")
def auth_tenants():
    """对齐 Go handler.PublicTenants：status=1 的公开租户（平台管理员租户不开放）。"""
    try:
        rows = many(
            "SELECT tenant_id, name, app_key, is_platform_admin FROM tenants "
            "WHERE status = 1 AND deleted_at = 0 ORDER BY id ASC")
    except pymysql.MySQLError as exc:
        soft(500, str(exc))               # Go: c.JSON(200, {code:500})，StatusCode 改写为 500
    return {"code": 0, "data": [
        {"tenant_id": r["tenant_id"], "name": r["name"], "app_key": r["app_key"]}
        for r in rows if not r["is_platform_admin"]
    ]}


@app.middleware("http")
async def audit_writes(request: Request, call_next):
    """对齐 Go `middleware.AuditMiddleware`：挂在 /api/admin 分组上的写操作审计。

    - 只记录 POST/PUT/DELETE，GET/OPTIONS 不记
    - handler 用真实 4xx/5xx 提前中止（deny/鉴权失败）时不记；
      body code 形式的「软失败」照记 —— Go 侧审计读到的是尚未被 StatusCode
      中间件改写的 200，这个次序是刻意复刻的
    - detail 恒为空，object_type/object_id 由路径推断
    """
    response = await call_next(request)
    path = request.url.path
    if (request.method not in ("POST", "PUT", "DELETE")
            or not path.startswith("/api/admin")
            or getattr(request.state, "denied", False)):
        return response
    admin = getattr(request.state, "admin", None)
    if admin is not None:
        try:
            write_audit(admin, request, {"POST": "create", "PUT": "update",
                                         "DELETE": "delete"}[request.method],
                        audit_object_id(request), path)
        except Exception:                   # best-effort：审计失败不改变业务响应
            pass
    return response


def to_int(value, default=None):
    """宽松解析：对齐 Go 的 strconv.Atoi 忽略错误语义（筛选参数非法即视为未传）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def go_parse_uint(value):
    """对齐 Go `strconv.ParseUint(v, 10, 64)` 的忽略错误语义：只认纯十进制数字，其余取 0。

    注意与 int() 的差别：Go 不接受前导/尾随空格、正负号、小数点，
    `/api/admin/members/ 5` 这类路径在 Go 里一律落到 id=0 分支。
    """
    text = value if isinstance(value, str) else ""
    return int(text) if re.fullmatch(r"[0-9]{1,20}", text or "") else 0


@app.get("/api/admin/ratings")
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


@app.get("/api/admin/ratings/stats")
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


@app.get("/api/admin/quick-replies")
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


@app.post("/api/admin/quick-replies")
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

@app.put("/api/admin/quick-replies/{reply_id}")
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


@app.delete("/api/admin/quick-replies/{reply_id}")
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


# ==========================================================================
# 阶段 3-2：在线客服状态 / 会话记录 / 客户档案 / 客户名册
# 全部为租户业务数据（Go 的 biz 分组），平台管理员访问一律 403。
# 说明：Go 侧大量失败分支是 HTTP 200 + body code（gin.H{"code":500"}), 这里用 soft() 原样保留。
# ==========================================================================

MSG_FILE_ROOT = "/app/msg_files"
MSG_IMAGE_MAX_MB = 5
MSG_VIDEO_MAX_MB = 50
MSG_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MSG_VIDEO_EXTS = {".mp4", ".webm"}


def tenant_of_thread(thread_id):
    """对齐 Go auth.TenantFromThreadID：首个冒号前的段，无冒号返回空串。"""
    head, sep, _ = (thread_id or "").partition(":")
    return head if sep else ""


def thread_in_tenant(thread_id, tenant_id):
    """对齐 Go threadOwnedByTenant（auth 已启用，租户非空时校验 thread 前缀）。"""
    if not tenant_id:
        return
    if tenant_of_thread(thread_id) != tenant_id:
        deny(403, "无权访问该租户会话")


# ---------- 在线客服状态 ----------

AGENT_STATUS_COLUMNS = ("a.id, a.tenant_id, a.user_id, a.status, a.handling_count, "
                        "a.max_concurrent, a.updated_at, u.nickname, u.username")


def list_agent_status(tenant_id):
    rows = many(
        f"SELECT {AGENT_STATUS_COLUMNS} FROM agent_status AS a "
        "LEFT JOIN users u ON u.id = a.user_id AND u.deleted_at = 0 "
        "WHERE a.tenant_id = %s "
        "ORDER BY a.status DESC, a.handling_count ASC, a.id ASC",
        (tenant_id,),
    )
    data = []
    for row in rows:
        row["updated_at"] = iso(row["updated_at"])
        row["nickname"] = row["nickname"] or ""
        row["username"] = row["username"] or ""
        data.append(row)
    return data


@app.get("/api/admin/agent-status")
def admin_agent_status(admin: Admin = Depends(require_perm("ticket:view"))):
    try:
        return {"code": 0, "data": list_agent_status(admin.tenant_id)}
    except pymysql.MySQLError:
        deny(500, "查询客服状态失败")


@app.put("/api/admin/agent-status")
async def admin_agent_status_update(request: Request,
                                    admin: Admin = Depends(require_perm("ticket:handle"))):
    if admin.user_id == 0:
        deny(403, "app_key 管理员无需设置状态")
    body = await body_json(request)
    status = body.get("status") if isinstance(body, dict) else None
    if isinstance(status, bool) or not isinstance(status, int) or status not in (0, 1, 2):
        soft(400, "参数错误")
    try:
        write("UPDATE agent_status SET status = %s, updated_at = NOW() "
              "WHERE tenant_id = %s AND user_id = %s",
              (status, admin.tenant_id, admin.user_id))
    except pymysql.MySQLError:
        deny(500, "更新状态失败")
    return {"code": 0, "msg": "已更新"}


# ---------- 会话列表 ----------

SESSION_COLUMNS = (
    "m.thread_id, MAX(m.tenant_id) AS tenant_id, COUNT(*) AS message_count, "
    "COALESCE(SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END), 0) AS user_count, "
    "COALESCE(SUM(CASE WHEN m.role != 'user' AND m.is_read = 0 THEN 1 ELSE 0 END), 0) "
    "AS unread_count, MAX(m.created_at) AS last_active_at, "
    "COALESCE(MAX(CASE WHEN t.status IN (0,1) THEN 1 ELSE 0 END), 0) AS has_open_ticket, "
    "MAX(ss.summary) AS summary"
)

SESSION_JOINS = (
    "FROM messages m "
    "LEFT JOIN tickets t ON t.thread_id = m.thread_id AND t.status IN (0,1) "
    "LEFT JOIN customer_profiles cp ON cp.thread_id = m.thread_id "
    "LEFT JOIN session_summaries ss ON ss.thread_id = m.thread_id "
    "AND ss.tenant_id = m.tenant_id"
)

HANDOFF_HAVING = " HAVING COALESCE(MAX(CASE WHEN t.status IN (0,1) THEN 1 ELSE 0 END), 0) = 1"


def session_filter(tenant_id, keyword, date_from, date_to, tag):
    """对齐 Go ListSessions 的动态 where（COUNT 与主查询共用，tag 子查询在后）。"""
    where, args = ["m.tenant_id = %s"], [tenant_id]
    if keyword:
        where.append("(m.thread_id LIKE %s OR cp.name LIKE %s OR cp.phone LIKE %s "
                     "OR cp.email LIKE %s OR cp.address LIKE %s)")
        args.extend(["%" + keyword + "%"] * 5)
    if date_from:
        where.append("m.created_at >= %s")
        args.append(date_from + " 00:00:00")
    if date_to:
        where.append("m.created_at < %s")
        args.append(date_to + " 23:59:59")
    if tag:
        where.append("m.thread_id IN (SELECT thread_id FROM session_tags "
                     "WHERE tenant_id = %s AND tags LIKE %s)")
        args.extend([tenant_id, "%" + tag + "%"])
    return " AND ".join(where), tuple(args)


def list_sessions(tenant_id, page, size, keyword, date_from, date_to, handoff, tag):
    clause, args = session_filter(tenant_id, keyword, date_from, date_to, tag)
    # 已修复：Go 侧的 total 用 Distinct().Count()，只带 where、不带 handoffFilter，
    # 导致 handoff=1 时 total 远大于实际行数（分页控件显示错乱）。这里把 HAVING
    # 包进子查询，让 total 与列表行数口径一致。
    # 注意：HAVING 引用 t.status，子查询必须带上与主查询相同的 tickets JOIN，
    # 否则 MySQL 直接报 "Unknown column 't.status' in 'having clause'"。
    total = one(
        "SELECT COUNT(*) AS n FROM ("
        "SELECT m.thread_id FROM messages m "
        "LEFT JOIN tickets t ON t.thread_id = m.thread_id AND t.status IN (0,1) "
        "LEFT JOIN customer_profiles cp ON cp.thread_id = m.thread_id "
        "WHERE " + clause + " GROUP BY m.thread_id"
        + (HANDOFF_HAVING if handoff == 1 else "")
        + ") x",
        args,
    )["n"]
    profiles = {
        row["thread_id"]: row for row in many(
            "SELECT thread_id, name, phone, address, email, remark, extract_status "
            "FROM customer_profiles WHERE tenant_id = %s AND deleted_at = 0", (tenant_id,),
        )
    }
    rows = many(
        f"SELECT {SESSION_COLUMNS} {SESSION_JOINS} WHERE {clause} GROUP BY m.thread_id"
        + (HANDOFF_HAVING if handoff == 1 else "")
        + " ORDER BY last_active_at DESC LIMIT %s OFFSET %s",
        args + (size, (page - 1) * size),
    )
    tags = {
        row["thread_id"]: row["tags"] for row in many(
            "SELECT thread_id, tags FROM session_tags WHERE tenant_id = %s", (tenant_id,),
        )
    }
    items = []
    for row in rows:
        # Go 侧按会话逐条补最后一条消息内容（分页量小，可接受）
        last = one("SELECT content FROM messages WHERE thread_id = %s "
                   "ORDER BY id DESC LIMIT 1", (row["thread_id"],))
        profile = profiles.get(row["thread_id"]) or {}
        items.append({
            "thread_id": row["thread_id"],
            "tenant_id": row["tenant_id"],
            "message_count": int(row["message_count"] or 0),
            "user_count": int(row["user_count"] or 0),
            "unread_count": int(row["unread_count"] or 0),
            "last_content": (last or {}).get("content") or "",
            "last_active_at": iso(row["last_active_at"]),
            "has_open_ticket": bool(row["has_open_ticket"]),
            "name": profile.get("name") or "",
            "phone": profile.get("phone") or "",
            "address": profile.get("address") or "",
            "email": profile.get("email") or "",
            "remark": profile.get("remark") or "",
            "extract_status": int(profile.get("extract_status") or 0),
            "tags": tags.get(row["thread_id"]) or "",
            "summary": row["summary"] or "",
        })
    return items, total


@app.get("/api/admin/sessions")
def admin_sessions(page: str = "1", size: str = "20", q: str = "", start: str = "",
                   end: str = "", handoff: str = "", tag: str = "",
                   admin: Admin = Depends(require_perm("session:view"))):
    p = max(1, to_int(page, 0))          # Go: Atoi 忽略错误 → 0 → 收敛为 1
    limit = to_int(size, 0)
    if limit < 1 or limit > 100:
        limit = 20
    try:
        items, total = list_sessions(
            admin.tenant_id, p, limit, q.strip(), start.strip(), end.strip(),
            1 if handoff == "1" else 0, tag.strip(),
        )
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "data": {"list": items, "total": total, "page": p, "size": limit}}


# ---------- 会话聊天记录（含归档） ----------

MESSAGE_ARCHIVE_SQL = (
    "SELECT id, thread_id, role, content, msg_type, tenant_id, ticket_id, is_read, "
    "created_at, 0 AS archived FROM messages WHERE thread_id = %s "
    "UNION ALL "
    "SELECT id, thread_id, role, content, msg_type, tenant_id, ticket_id, is_read, "
    "created_at, 1 AS archived FROM messages_archive WHERE thread_id = %s "
    "ORDER BY created_at ASC, id ASC"
)


def customer_profile(thread_id):
    """对齐 Go GetCustomerProfile：查不到返回 None（JSON null），不报错。"""
    row = one(
        "SELECT id, thread_id, tenant_id, name, phone, address, email, remark, "
        "extract_status, created_at, updated_at FROM customer_profiles "
        "WHERE thread_id = %s AND deleted_at = 0 LIMIT 1",
        (thread_id,),
    )
    if not row:
        return None
    for field in ("name", "phone", "address", "email", "remark"):
        row[field] = row[field] or ""
    row["extract_status"] = int(row["extract_status"] or 0)
    row["created_at"] = iso(row["created_at"])
    row["updated_at"] = iso(row["updated_at"])
    return row


def message_with_archive(thread_id):
    data = []
    for row in many(MESSAGE_ARCHIVE_SQL, (thread_id, thread_id)):
        item = {
            "id": row["id"],
            "thread_id": row["thread_id"],
            "role": row["role"],
            "content": row["content"],
            "msg_type": row["msg_type"],
            "tenant_id": row["tenant_id"],
            "is_read": int(row["is_read"] or 0),
            "created_at": iso(row["created_at"]),
            "archived": bool(row["archived"]),
        }
        # Go 侧 ticket_id 带 omitempty：NULL 时整个字段省略
        if row["ticket_id"] is not None:
            item["ticket_id"] = row["ticket_id"]
        data.append(item)
    return data


@app.get("/api/admin/sessions/{thread_id}/messages")
def admin_session_messages(thread_id: str,
                           admin: Admin = Depends(require_perm("session:view"))):
    thread_in_tenant(thread_id, admin.tenant_id)
    try:
        messages = message_with_archive(thread_id)
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "data": {"messages": messages,
                                "profile": customer_profile(thread_id)}}


# ---------- 客户档案（人工填写 / 重新提取） ----------

class CustomerProfileBody(BaseModel):
    name: str = ""
    phone: str = ""
    address: str = ""
    email: str = ""
    remark: str = ""


def save_customer_profile(tenant_id, thread_id, body):
    """对齐 Go SaveCustomerProfile：thread_id 唯一键 upsert，人工填写标记 2。"""
    values = (body.name, body.phone, body.address, body.email, body.remark)
    write(
        "INSERT INTO customer_profiles (thread_id, tenant_id, name, phone, address, email, "
        "remark, extract_status, created_at, updated_at, deleted_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 2, NOW(), NOW(), 0) "
        "ON DUPLICATE KEY UPDATE name = %s, phone = %s, address = %s, email = %s, "
        "remark = %s, extract_status = 2, updated_at = NOW()",
        (thread_id, tenant_id) + values + values,
    )


@app.put("/api/admin/sessions/{thread_id}/profile")
async def admin_session_profile(thread_id: str, request: Request,
                                admin: Admin = Depends(require_perm("session:profile:edit"))):
    thread_in_tenant(thread_id, admin.tenant_id)
    body = await body_json(request)
    if not isinstance(body, dict):
        soft(400, "参数错误: EOF")
    try:
        save_customer_profile(admin.tenant_id, thread_id, CustomerProfileBody(**{
            key: str(body.get(key) or "") for key in
            ("name", "phone", "address", "email", "remark")
        }))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "msg": "客户信息已保存"}


# Go 默认提取规则（零配置可用）：platform_config 中无 cs_extract_rule_* 时生效
DEFAULT_EXTRACT_RULES = (
    ("phone", r"(?:\+?86[\s-]?)?1[3-9]\d[\s-]?\d{4}[\s-]?\d{4}"),
    ("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)


def extract_rules():
    rows = many(
        "SELECT config_key, config_value FROM platform_config "
        "WHERE config_key LIKE %s", ("cs_extract_rule_%",),
    )
    rules = []
    for row in rows:
        field = (row["config_key"] or "").removeprefix("cs_extract_rule_")
        if field and row["config_value"]:
            rules.append((field, row["config_value"]))
    return tuple(rules) or DEFAULT_EXTRACT_RULES


def clean_phone(text):
    for token in (" ", "-", "\u00a0"):
        text = text.replace(token, "")
    text = text.strip()
    if text.startswith("+86"):
        return text[3:]
    return text[2:] if text.startswith("86") else text


def extract_from_content(content):
    """对齐 Go extractFromContent：只取每条规则的首个命中，phone 额外校数字边界。"""
    found = {}
    for field, pattern in extract_rules():
        try:
            # re.ASCII：Go RE2 的 \d \s \w 只认 ASCII，不能让它按 Unicode 语义放宽
            match = re.search(pattern, content or "", re.ASCII)
        except re.error:                    # 保存时已校验，这里仅防御
            continue
        if not match:
            continue
        value = match.group(0)
        if field == "phone":
            # RE2 不支持 lookbehind，Go 用代码校验边界，防止从长数字串里截 11 位
            before = content[match.start() - 1] if match.start() > 0 else ""
            after = content[match.end()] if match.end() < len(content) else ""
            if (before.isdigit() and before.isascii()) or (after.isdigit() and after.isascii()):
                continue
            value = clean_phone(value)
        if value:
            found[field] = value
    return found


def upsert_extracted_profile(thread_id, tenant_id, extracted):
    """对齐 Go upsertExtractedProfile：只写 phone/email，保留人工填写字段。"""
    if not extracted:
        return
    row = one("SELECT id, phone, email FROM customer_profiles "
              "WHERE thread_id = %s AND deleted_at = 0 LIMIT 1", (thread_id,))
    phone = extracted.get("phone", row["phone"] if row else "")
    email = extracted.get("email", row["email"] if row else "")
    if row:
        write("UPDATE customer_profiles SET phone = %s, email = %s, extract_status = 1, "
              "updated_at = NOW() WHERE id = %s", (phone, email, row["id"]))
    else:
        write("INSERT INTO customer_profiles (thread_id, tenant_id, phone, email, "
              "extract_status, created_at, updated_at, deleted_at) "
              "VALUES (%s, %s, %s, %s, 1, NOW(), NOW(), 0)",
              (thread_id, tenant_id, phone, email))


@app.post("/api/admin/sessions/{thread_id}/reextract")
def admin_session_reextract(thread_id: str,
                            admin: Admin = Depends(require_perm("session:profile:edit"))):
    thread_in_tenant(thread_id, admin.tenant_id)
    try:
        merged = {}
        for row in many("SELECT content FROM messages WHERE thread_id = %s AND role = %s "
                        "ORDER BY id ASC", (thread_id, "user")):
            for field, value in extract_from_content(row["content"]).items():
                if value:
                    merged[field] = value
        upsert_extracted_profile(thread_id, tenant_of_thread(thread_id), merged)
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "msg": "重新提取完成"}


# ---------- 会话标签 ----------

def get_session_tags(tenant_id, thread_id):
    row = one("SELECT tags FROM session_tags WHERE tenant_id = %s AND thread_id = %s "
              "LIMIT 1", (tenant_id, thread_id))
    return (row["tags"] if row else "") or ""


@app.get("/api/admin/sessions/{thread_id}/tags")
def admin_session_tags(thread_id: str,
                       admin: Admin = Depends(require_perm("session:view"))):
    thread_in_tenant(thread_id, admin.tenant_id)
    return {"code": 0, "data": {"thread_id": thread_id,
                                "tags": get_session_tags(admin.tenant_id, thread_id)}}


@app.put("/api/admin/sessions/{thread_id}/tags")
async def admin_session_tags_update(thread_id: str, request: Request,
                                    admin: Admin = Depends(require_perm("session:profile:edit"))):
    thread_in_tenant(thread_id, admin.tenant_id)
    body = await body_json(request)
    if not isinstance(body, dict):
        soft(400, "参数错误: EOF")
    tags = str(body.get("tags") or "").strip()
    try:
        # session_tags 无软删除，Go 侧是「有则更新、无则插入」的 upsert
        write("INSERT INTO session_tags (tenant_id, thread_id, tags, updated_at) "
              "VALUES (%s, %s, %s, NOW()) "
              "ON DUPLICATE KEY UPDATE tags = %s, updated_at = NOW()",
              (admin.tenant_id, thread_id, tags, tags))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "msg": "标签已保存"}


# ---------- 会话图文上传 ----------

def media_kind(filename):
    suffix = os.path.splitext(filename or "")[1].lower()
    if suffix in MSG_IMAGE_EXTS:
        return "image"
    if suffix in MSG_VIDEO_EXTS:
        return "video"
    return ""


async def save_session_media(file: UploadFile, tenant_id: str):
    """Store customer and admin media under the authenticated tenant directory."""
    if file is None or not file.filename:
        soft(400, "请选择要上传的文件")
    kind = media_kind(file.filename)
    if not kind:
        soft(400, "仅支持图片(jpg/png/gif/webp)或视频(mp4/webm)")
    data = await file.read()
    limit = MSG_IMAGE_MAX_MB if kind == "image" else MSG_VIDEO_MAX_MB
    if len(data) > limit * 1024 * 1024:
        soft(400, f"{'图片' if kind == 'image' else '视频'}大小不能超过 {limit}MB")
    month = datetime.now().strftime("%Y%m")
    directory = os.path.join(MSG_FILE_ROOT, tenant_id, month)
    name = f"{time.time_ns()}{os.path.splitext(file.filename)[1].lower()}"
    try:
        os.makedirs(directory, mode=0o755, exist_ok=True)
        os.chmod(directory, 0o755)          # 容器 umask 可能是 0027，nginx 需可读
        with open(os.path.join(directory, name), "wb") as handle:
            handle.write(data)
    except OSError:
        soft(400, "文件保存失败")
    return {"code": 0, "data": {"url": f"/msg_files/{tenant_id}/{month}/{name}",
                                "msg_type": kind}}


@app.post("/api/admin/sessions/upload")
async def admin_session_upload(file: UploadFile = File(default=None),
                               admin: Admin = Depends(require_perm("ticket:handle"))):
    return await save_session_media(file, admin.tenant_id)


@app.post("/api/user/upload")
async def customer_session_upload(file: UploadFile = File(default=None),
                                  customer: Customer = Depends(current_customer)):
    return await save_session_media(file, customer.tenant_id)


# ---------- 客户名册 ----------

CUSTOMER_SQL = (
    "SELECT p.thread_id, p.tenant_id, p.name, p.phone, p.address, p.email, p.remark, "
    "p.extract_status, p.created_at, p.updated_at, m.last_active_at, "
    "(SELECT COUNT(*) FROM messages WHERE thread_id = p.thread_id) "
    "+ (SELECT COUNT(*) FROM messages_archive WHERE thread_id = p.thread_id) "
    "AS message_count "
    "FROM customer_profiles p "
    "LEFT JOIN (SELECT thread_id, MAX(created_at) AS last_active_at "
    "FROM (SELECT thread_id, created_at FROM messages WHERE tenant_id = %s "
    "UNION ALL SELECT thread_id, created_at FROM messages_archive WHERE tenant_id = %s) u "
    "GROUP BY thread_id) m ON m.thread_id = p.thread_id "
    "WHERE "
)


def list_customers(tenant_id, page, size, keyword):
    where, args = ["p.tenant_id = %s"], [tenant_id]
    if keyword:
        where.append("(p.name LIKE %s OR p.phone LIKE %s OR p.email LIKE %s "
                     "OR p.address LIKE %s OR p.remark LIKE %s)")
        args.extend(["%" + keyword + "%"] * 5)
    clause = " AND ".join(where)
    total = one("SELECT COUNT(*) AS n FROM customer_profiles p WHERE " + clause,
                tuple(args))["n"]
    rows = many(CUSTOMER_SQL + clause + " ORDER BY COALESCE(m.last_active_at, p.updated_at) "
                "DESC LIMIT %s OFFSET %s",
                (tenant_id, tenant_id) + tuple(args) + (size, (page - 1) * size))
    items = []
    for row in rows:
        items.append({
            "thread_id": row["thread_id"],
            "tenant_id": row["tenant_id"],
            "name": row["name"] or "",
            "phone": row["phone"] or "",
            "address": row["address"] or "",
            "email": row["email"] or "",
            "remark": row["remark"] or "",
            "extract_status": int(row["extract_status"] or 0),
            "last_active_at": iso(row["last_active_at"]),
            # 已修复：Go 侧 query 取 msg_count 而 model.CustomerItem 字段映射 message_count，
            # GORM Scan 匹配不上 → 恒 0。这里按 thread 统计 messages + messages_archive。
            "message_count": int(row["message_count"] or 0),
            "created_at": iso(row["created_at"]),
            "updated_at": iso(row["updated_at"]),
        })
    return items, total


@app.get("/api/admin/customers")
def admin_customers(page: str = "1", size: str = "20", q: str = "",
                    admin: Admin = Depends(require_perm("customer:view"))):
    p = max(1, to_int(page, 0))
    limit = to_int(size, 0)
    if limit < 1 or limit > 100:
        limit = 20
    keyword = q.strip()
    try:
        items, total = list_customers(admin.tenant_id, p, limit, keyword)
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    # Go 侧 `var items []CustomerItem` 为空时序列化成 null，不是 []（会话列表相反）
    return {"code": 0, "data": {"list": items or None, "total": total,
                                "page": p, "size": limit, "q": keyword}}


# ==========================================================================
# 阶段 3-3：工单域（12 条）
# 这批端点不是纯 SQL：接管/回复/关闭/转交要推 WS、待处理接管还要唤醒 Agent 的 HITL 中断。
# 客户与管理端的长连接仍握在 Go 进程里，所以推送走 Go 的内部桥
# POST /api/agent/ws/broadcast（本轮新增，仅转发不落库），保证只有一个 WS Hub。
# ==========================================================================

GO_INTERNAL_URL = os.getenv("GO_INTERNAL_URL", "http://go:8080")
AGENT_URL = os.getenv("CS_AGENT_URL", "http://agent:8000")

TICKET_STATUS_PENDING = 0
TICKET_STATUS_HANDLING = 1
TICKET_STATUS_CANCELED = 2
TICKET_STATUS_CLOSED = 3

TICKET_COLUMNS = ("id, thread_id, ticket_no, user_id, reason, status, priority, tenant_id, "
                  "handler_uid, handler_name, closed_at, created_at")
TICKET_LOG_COLUMNS = ("id, ticket_id, thread_id, tenant_id, action, old_status, new_status, "
                      "old_priority, new_priority, operator, remark, created_at")

# 权限目录全量（Go permission.AllCodes()），用于 IsAdminUser 判定
ALL_PERMISSIONS = {
    "session:view", "session:profile:edit", "session:cleanup",
    "ticket:view", "ticket:handle", "customer:view", "dashboard:view",
    "knowledge:view", "knowledge:add", "knowledge:edit", "knowledge:remove",
    "prompt:view", "prompt:add", "prompt:edit", "prompt:remove",
    "model:view", "model:edit", "usage:view", "audit:view",
    "quickreply:view", "quickreply:add", "quickreply:edit", "quickreply:remove",
    "rating:view",
    "member:view", "member:add", "member:edit", "member:remove",
    "role:view", "role:add", "role:edit", "role:remove",
}


def go_broadcast(target, payload, key="", tenant_id=""):
    """对齐 Go 的 ws.WSHub 推送：借 Go 进程的 Hub 发一条消息。

    Go 侧推送是进程内调用、不会失败；跨进程后可能失败，这里按 Go 的语义处理 ——
    推送失败不影响已经落库的结果（响应仍然成功），因此只做一次重试后放弃。
    """
    body = {"target": target, "payload": payload}
    if key:
        body["key"] = key
    if tenant_id:
        body["tenant_id"] = tenant_id
    headers = {"X-Internal-Token": os.getenv("INTERNAL_API_TOKEN", "")}
    for _ in range(2):
        try:
            httpx.post(GO_INTERNAL_URL + "/api/agent/ws/broadcast", json=body,
                       headers=headers, timeout=5)
            return
        except httpx.HTTPError:
            continue


def reload_go_extract_rules():
    """通知 Go 失效提取规则内存缓存（并行期专用，失败不影响主流程）。"""
    headers = {"X-Internal-Token": os.getenv("INTERNAL_API_TOKEN", "")}
    for _ in range(2):
        try:
            httpx.post(GO_INTERNAL_URL + "/api/agent/extract-rules/reload",
                       headers=headers, timeout=5)
            return
        except httpx.HTTPError:
            continue


def agent_resume(thread_id, confirmed, wait_time, ticket_no, trace_id=""):
    """对齐 Go agent.CallAgentResume：POST {agent}/agent/resume，传输失败重试一次。"""
    payload = {"thread_id": thread_id, "confirmed": confirmed,
               "wait_time": wait_time, "ticket_id": ticket_no}
    headers = {"X-Internal-Token": os.getenv("INTERNAL_API_TOKEN", ""),
               "X-Tenant-Id": tenant_of_thread(thread_id),
               "X-Trace-Id": trace_id}
    error = None
    for _ in range(2):
        try:
            response = httpx.post(AGENT_URL + "/agent/resume", json=payload,
                                  headers=headers, timeout=60)
            return (response.json() or {}).get("content") or ""
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            error = exc
    raise RuntimeError(str(error))


def save_message(thread_id, role, content, msg_type="", ticket_id=None):
    """对齐 Go repository.saveMessage：用户消息默认已读，其余未读；文本消息触发客户信息提取。"""
    if not content:
        return
    msg_type = msg_type or "text"
    write(
        "INSERT INTO messages (thread_id, role, content, msg_type, tenant_id, ticket_id, "
        "is_read, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())",
        (thread_id, role, content, msg_type, tenant_of_thread(thread_id), ticket_id,
         1 if role == "user" else 0),
    )
    if msg_type == "text" and role == "user":
        upsert_extracted_profile(thread_id, tenant_of_thread(thread_id),
                                 extract_from_content(content))


def add_ticket_log(ticket, action, old_status, new_status, old_priority, new_priority,
                   operator, remark):
    write(
        "INSERT INTO ticket_logs (ticket_id, thread_id, tenant_id, action, old_status, "
        "new_status, old_priority, new_priority, operator, remark, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
        (ticket["id"], ticket["thread_id"], ticket["tenant_id"], action, old_status,
         new_status, old_priority, new_priority, operator, remark),
    )


def adjust_handling(tenant_id, user_id, delta):
    """对齐 Go AdjustHandlingCount：达上限转忙碌(2)，否则在线(1)；离线(0)不联动。"""
    if not tenant_id or not user_id:
        return
    row = one("SELECT id, status, handling_count, max_concurrent FROM agent_status "
              "WHERE tenant_id = %s AND user_id = %s LIMIT 1", (tenant_id, user_id))
    if not row:
        return
    count = int(row["handling_count"] or 0) + delta
    if count < 0:
        count = 0
    status = int(row["status"] or 0)
    if status != 0:
        status = 2 if count >= int(row["max_concurrent"] or 3) else 1
    write("UPDATE agent_status SET handling_count = %s, status = %s, updated_at = NOW() "
          "WHERE id = %s", (count, status, row["id"]))


def update_ticket_status(ticket, status):
    """对齐 Go UpdateTicketStatus：关闭/取消时补 closed_at（工单消息窗口上界）。"""
    ticket["status"] = status
    if status in (TICKET_STATUS_CANCELED, TICKET_STATUS_CLOSED):
        write("UPDATE tickets SET status = %s, closed_at = NOW() WHERE id = %s",
              (status, ticket["id"]))
        ticket["closed_at"] = datetime.now()
    else:
        write("UPDATE tickets SET status = %s WHERE id = %s", (status, ticket["id"]))


def ticket_json(ticket, unread=0):
    item = {
        "id": ticket["id"],
        "thread_id": ticket["thread_id"],
        "ticket_no": ticket["ticket_no"],
        "user_id": ticket["user_id"],
        "reason": ticket["reason"],
        "status": int(ticket["status"] or 0),
        "priority": int(ticket["priority"] or 0),
        "tenant_id": ticket["tenant_id"],
        "handler_uid": int(ticket["handler_uid"] or 0),
        "handler_name": ticket["handler_name"] or "",
    }
    if ticket.get("closed_at"):
        item["closed_at"] = iso(ticket["closed_at"])   # Go: *time.Time + omitempty
    item["created_at"] = iso(ticket["created_at"])
    # Go 的 Ticket.UnreadCount 是 gorm:"-" 字段，详情/推送里恒为 0，列表里才填充
    item["unread_count"] = unread
    return item


def ticket_id_of(raw):
    """对齐 Go strconv.ParseUint：非数字/负数/越界都算参数错误。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        soft(400, "参数错误")
    if value < 0 or value > 0xFFFFFFFFFFFFFFFF:
        soft(400, "参数错误")
    return value


def load_ticket(raw_id, tenant_id):
    ticket = one(f"SELECT {TICKET_COLUMNS} FROM tickets WHERE id = %s AND deleted_at = 0 "
                 f"LIMIT 1", (ticket_id_of(raw_id),))
    if not ticket:
        soft(404, "工单不存在")
    if ticket["tenant_id"] != tenant_id:
        deny(403, "无权访问该租户工单")
    return ticket


def can_handle_ticket(admin, ticket):
    """对齐 Go canHandleTicket：app_key 管理员 / 全权限管理员 / 本人工单。"""
    if admin.user_id == 0:
        return True
    if ALL_PERMISSIONS <= user_perms(admin.user_id):
        return True
    return int(ticket["handler_uid"] or 0) == admin.user_id


def handler_label(name):
    return name or "-"


def current_handler_name(admin):
    """对齐 Go currentHandlerName：子账号昵称优先，无则用户名；app_key 管理员记租户 ID。"""
    if admin.user_id > 0:
        row = one("SELECT nickname, username FROM users WHERE id = %s AND deleted_at = 0 "
                  "LIMIT 1", (admin.user_id,))
        if row:
            return row["nickname"] or row["username"] or admin.tenant_id
    return admin.tenant_id


# ---------- 列表 / 详情 / 消息 / 日志 / 已读 ----------

def list_tickets(tenant_id, status, page, size, handler_uid, keyword, priority):
    if page < 1:
        page = 1
    if size < 1 or size > 100:
        size = 20
    where, args = ["tenant_id = %s", "deleted_at = 0"], [tenant_id]
    if status >= 0:
        where.append("status = %s")
        args.append(status)
    if handler_uid > 0:
        where.append("handler_uid = %s")
        args.append(handler_uid)
    if keyword:
        where.append("(ticket_no LIKE %s OR user_id LIKE %s OR handler_name LIKE %s)")
        args.extend(["%" + keyword + "%"] * 3)
    if priority >= 0:
        where.append("priority = %s")
        args.append(priority)
    clause = " AND ".join(where)
    total = one(f"SELECT COUNT(*) AS n FROM tickets WHERE {clause}", tuple(args))["n"]
    rows = many(
        f"SELECT {TICKET_COLUMNS} FROM tickets WHERE {clause} "
        f"ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
        tuple(args) + (size, (page - 1) * size),
    )
    items = []
    for row in rows:
        unread = one("SELECT COUNT(*) AS n FROM messages WHERE thread_id = %s "
                     "AND role != %s AND is_read = 0", (row["thread_id"], "user"))["n"]
        items.append(ticket_json(row, int(unread or 0)))
    return items, int(total or 0), page, size


@app.get("/api/admin/tickets")
def admin_tickets(status: str = "", page: str = "1", size: str = "20", q: str = "",
                  priority: str = "", scope: str = "",
                  admin: Admin = Depends(require_perm("ticket:view"))):
    state = -1
    if status:
        value = to_int(status)
        if value is not None:
            state = value
    p, limit = to_int(page, 0), to_int(size, 0)
    level = -1
    if priority:
        value = to_int(priority)
        if value is not None and 0 <= value <= 3:
            level = value
    handler_uid = admin.user_id if scope == "mine" else 0
    try:
        items, total, p, limit = list_tickets(admin.tenant_id, state, p, limit,
                                              handler_uid, q.strip(), level)
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "data": {"list": items, "total": total, "page": p, "size": limit}}


@app.get("/api/admin/tickets/transfer-targets")
def admin_ticket_transfer_targets(admin: Admin = Depends(require_perm("ticket:handle"))):
    try:
        rows = many("SELECT id, nickname, username FROM users WHERE tenant_id = %s "
                    "AND status = 1 AND deleted_at = 0 ORDER BY id ASC",
                    (admin.tenant_id,))
    except pymysql.MySQLError:
        deny(500, "查询客服失败")
    return {"code": 0, "data": [{"id": row["id"],
                                 "nickname": row["nickname"] or row["username"],
                                 "username": row["username"]} for row in rows]}


@app.get("/api/admin/tickets/{id}")
def admin_ticket_detail(id: str, admin: Admin = Depends(require_perm("ticket:view"))):
    return {"code": 0, "data": ticket_json(load_ticket(id, admin.tenant_id))}


def ticket_message(row, *, archived=False):
    item = {
        "id": row["id"],
        "thread_id": row["thread_id"],
        "role": row["role"],
        "content": row["content"],
        # Go 合并归档时构造 model.Message 但没带 MsgType → 归档消息 msg_type 恒为空串
        "msg_type": "" if archived else row["msg_type"],
        "tenant_id": row["tenant_id"],
        "is_read": int(row["is_read"] or 0),
        "created_at": iso(row["created_at"]),
    }
    if row["ticket_id"] is not None:
        item["ticket_id"] = row["ticket_id"]
    return item


def ticket_messages(ticket):
    """对齐 Go GetTicketMessages 的窗口上界：待处理=创建+2s；接管中=now；已关闭/取消=关闭+2s。"""
    status = int(ticket["status"] or 0)
    if status == TICKET_STATUS_PENDING:
        upper = ticket["created_at"] + timedelta(seconds=2)
    elif status == TICKET_STATUS_HANDLING:
        upper = datetime.now()
    elif ticket["closed_at"]:
        upper = ticket["closed_at"] + timedelta(seconds=2)
    else:
        upper = ticket["created_at"] + timedelta(seconds=2)
    rows = many("SELECT id, thread_id, role, content, msg_type, tenant_id, ticket_id, "
                "is_read, created_at FROM messages WHERE thread_id = %s AND created_at <= %s "
                "ORDER BY id ASC", (ticket["thread_id"], upper))
    items = [ticket_message(row) for row in rows]
    archived = many("SELECT id, thread_id, role, content, tenant_id, ticket_id, is_read, "
                    "created_at FROM messages_archive WHERE thread_id = %s "
                    "AND created_at <= %s ORDER BY id ASC", (ticket["thread_id"], upper))
    if archived:
        merged = items + [ticket_message(row, archived=True) for row in archived]
        merged.sort(key=lambda m: (m["created_at"], m["id"]))
        return merged
    return items


@app.get("/api/admin/tickets/{id}/messages")
def admin_ticket_messages(id: str, admin: Admin = Depends(require_perm("ticket:view"))):
    ticket = load_ticket(id, admin.tenant_id)
    try:
        return {"code": 0, "data": ticket_messages(ticket)}
    except pymysql.MySQLError as exc:
        soft(500, str(exc))


@app.get("/api/admin/tickets/{id}/logs")
def admin_ticket_logs(id: str, admin: Admin = Depends(require_perm("ticket:view"))):
    ticket = load_ticket(id, admin.tenant_id)
    try:
        rows = many(f"SELECT {TICKET_LOG_COLUMNS} FROM ticket_logs WHERE ticket_id = %s "
                    f"ORDER BY id ASC", (ticket["id"],))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    data = []
    for row in rows:
        row["created_at"] = iso(row["created_at"])
        for field in ("old_status", "new_status", "old_priority", "new_priority"):
            row[field] = int(row[field] or 0)
        row["operator"] = row["operator"] or ""
        row["remark"] = row["remark"] or ""
        data.append(row)
    return {"code": 0, "data": data}


@app.post("/api/admin/tickets/{id}/read")
def admin_ticket_read(id: str, admin: Admin = Depends(require_perm("ticket:view"))):
    ticket = load_ticket(id, admin.tenant_id)
    try:
        write("UPDATE messages SET is_read = 1 WHERE thread_id = %s AND role != %s "
              "AND is_read = 0", (ticket["thread_id"], "user"))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "msg": "ok"}


# ---------- 状态流转 ----------

def takeover_ticket(ticket, wait_time, trace_id, handler_uid, handler_name):
    if int(ticket["status"] or 0) == TICKET_STATUS_HANDLING:
        return "该工单已在人工接管中"
    msg = ""
    if int(ticket["status"] or 0) == TICKET_STATUS_PENDING:
        # 线程处于 HITL 中断，先恢复 Agent 会话（best effort，失败不阻断人工接管）
        try:
            msg = agent_resume(ticket["thread_id"], True, wait_time, ticket["ticket_no"],
                               trace_id)
        except RuntimeError as exc:
            msg = "接管成功，但恢复Agent会话失败:" + str(exc)
    handler_name = handler_name or "system"
    ticket["handler_uid"] = handler_uid
    ticket["handler_name"] = handler_name
    write("UPDATE tickets SET handler_uid = %s, handler_name = %s WHERE id = %s",
          (handler_uid, handler_name, ticket["id"]))
    adjust_handling(ticket["tenant_id"], handler_uid, 1)
    update_ticket_status(ticket, TICKET_STATUS_HANDLING)
    add_ticket_log(ticket, "takeover", TICKET_STATUS_PENDING, TICKET_STATUS_HANDLING,
                   int(ticket["priority"] or 0), int(ticket["priority"] or 0),
                   handler_name, "人工接管工单")
    notice = "人工客服已接管对话，工单号：" + ticket["ticket_no"]
    save_message(ticket["thread_id"], "system", notice, "text", ticket["id"])
    go_broadcast("user", {"type": "human_notice", "msg": notice}, key=ticket["thread_id"])
    go_broadcast("admins_tenant", {"type": "ticket_update", "ticket": ticket_json(ticket)},
                 tenant_id=tenant_of_thread(ticket["user_id"] or ""))
    return msg or "已接管"


@app.post("/api/admin/tickets/{id}/takeover")
async def admin_ticket_takeover(id: str, request: Request,
                                admin: Admin = Depends(require_perm("ticket:handle"))):
    ticket = load_ticket(id, admin.tenant_id)
    body = await body_json(request)
    wait_time = body.get("wait_time") if isinstance(body, dict) else None
    if isinstance(wait_time, bool) or not isinstance(wait_time, int):
        wait_time = 0                     # Go 忽略 bind 错误，WaitTime 保持零值
    status = int(ticket["status"] or 0)
    if status == TICKET_STATUS_CLOSED:
        soft(400, "工单已关闭")
    if status == TICKET_STATUS_CANCELED:
        soft(400, "工单已取消")
    if status == TICKET_STATUS_HANDLING and not can_handle_ticket(admin, ticket):
        deny(403, "该工单由 " + handler_label(ticket["handler_name"]) + " 接管中，无权接管")
    try:
        msg = takeover_ticket(ticket, wait_time,
                              request.headers.get("X-Trace-Id", ""),
                              admin.user_id, current_handler_name(admin))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "msg": msg}


@app.post("/api/admin/tickets/{id}/reply")
async def admin_ticket_reply(id: str, request: Request,
                             admin: Admin = Depends(require_perm("ticket:handle"))):
    body = await body_json(request)
    content = body.get("content") if isinstance(body, dict) else None
    if not isinstance(content, str) or not content:
        soft(400, "content 不能为空")
    msg_type = body.get("msg_type") or ""
    ticket = load_ticket(id, admin.tenant_id)
    if int(ticket["status"] or 0) != TICKET_STATUS_HANDLING:
        soft(400, "该工单未处于人工接管中，无法回复")
    if not can_handle_ticket(admin, ticket):
        deny(403, "该工单由 " + handler_label(ticket["handler_name"]) + " 接管中，无权回复")
    msg_type = msg_type if isinstance(msg_type, str) else ""
    try:
        save_message(ticket["thread_id"], "human", content, msg_type, ticket["id"])
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    msg_type = msg_type or "text"
    go_broadcast("user", {"type": "human_reply", "msg": content, "msg_type": msg_type,
                          "ticket_no": ticket["ticket_no"]}, key=ticket["thread_id"])
    go_broadcast("admins_tenant", {"type": "human_reply", "thread_id": ticket["thread_id"],
                                   "msg": content, "msg_type": msg_type},
                 tenant_id=tenant_of_thread(ticket["user_id"] or ""))
    return {"code": 0, "msg": "ok"}


def close_ticket(ticket, operator):
    if int(ticket["status"] or 0) == TICKET_STATUS_CLOSED:
        return
    operator = operator or "system"
    # 已修复（Go 侧缺陷）：Go 写死 old_status=Handling，关闭一个「待处理」工单时
    # 日志会显示 handling → closed，与实际状态流转不符。这里取关闭前的真实状态。
    old_status = int(ticket["status"] or 0)
    update_ticket_status(ticket, TICKET_STATUS_CLOSED)
    adjust_handling(ticket["tenant_id"], int(ticket["handler_uid"] or 0), -1)
    add_ticket_log(ticket, "close", old_status, TICKET_STATUS_CLOSED,
                   int(ticket["priority"] or 0), int(ticket["priority"] or 0),
                   operator, "关闭工单，转回智能客服")
    save_message(ticket["thread_id"], "system", "本次人工会话已结束，转回智能客服",
                 "text", ticket["id"])
    go_broadcast("user", {"type": "human_notice",
                          "msg": "本次人工会话已结束，智能客服继续为您服务"},
                 key=ticket["thread_id"])
    go_broadcast("admins_tenant", {"type": "ticket_update", "ticket": ticket_json(ticket)},
                 tenant_id=tenant_of_thread(ticket["user_id"] or ""))


@app.post("/api/admin/tickets/{id}/close")
def admin_ticket_close(id: str, admin: Admin = Depends(require_perm("ticket:handle"))):
    ticket = load_ticket(id, admin.tenant_id)
    if int(ticket["status"] or 0) == TICKET_STATUS_CLOSED:
        return {"code": 0, "msg": "工单已关闭"}
    if (int(ticket["status"] or 0) == TICKET_STATUS_HANDLING
            and not can_handle_ticket(admin, ticket)):
        deny(403, "该工单由 " + handler_label(ticket["handler_name"]) + " 接管中，无权关闭")
    try:
        close_ticket(ticket, current_handler_name(admin))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "msg": "已关闭"}


@app.post("/api/admin/tickets/{id}/priority")
async def admin_ticket_priority(id: str, request: Request,
                                admin: Admin = Depends(require_perm("ticket:handle"))):
    body = await body_json(request)
    priority = body.get("priority") if isinstance(body, dict) else None
    # Go 的 binding:"required" 对 int 生效 → 0 也被判为未提供
    if isinstance(priority, bool) or not isinstance(priority, int) or priority == 0:
        soft(400, "参数错误：priority 必填")
    remark = body.get("remark") or ""
    ticket = load_ticket(id, admin.tenant_id)
    if priority < 0 or priority > 3:
        soft(400, "priority 取值范围：0-3")
    old_priority = int(ticket["priority"] or 0)
    try:
        write("UPDATE tickets SET priority = %s WHERE id = %s", (priority, ticket["id"]))
        ticket["priority"] = priority
        add_ticket_log(ticket, "priority", int(ticket["status"] or 0),
                       int(ticket["status"] or 0), old_priority, priority,
                       current_handler_name(admin), remark if isinstance(remark, str) else "")
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    go_broadcast("admins_tenant", {"type": "ticket_update", "ticket": ticket_json(ticket)},
                 tenant_id=ticket["tenant_id"])
    return {"code": 0, "msg": "优先级设置成功", "data": {"priority": priority}}


@app.post("/api/admin/tickets/{id}/transfer")
async def admin_ticket_transfer(id: str, request: Request,
                                admin: Admin = Depends(require_perm("ticket:handle"))):
    body = await body_json(request)
    to_user_id = body.get("to_user_id") if isinstance(body, dict) else None
    if isinstance(to_user_id, bool) or not isinstance(to_user_id, int) or to_user_id <= 0:
        soft(400, "to_user_id 不能为空")
    ticket = load_ticket(id, admin.tenant_id)
    status = int(ticket["status"] or 0)
    if status not in (TICKET_STATUS_HANDLING, TICKET_STATUS_PENDING):
        soft(400, "仅待处理或接管中的工单可转交")
    if not can_handle_ticket(admin, ticket):
        deny(403, "该工单由 " + handler_label(ticket["handler_name"]) + " 接管中，无权转交")
    target = one("SELECT id, tenant_id, nickname, username, status FROM users "
                 "WHERE id = %s AND deleted_at = 0 LIMIT 1", (to_user_id,))
    if not target:
        soft(400, "目标客服不存在")
    if target["tenant_id"] != ticket["tenant_id"]:
        soft(400, "目标客服不属于当前租户")
    if int(target["status"] or 0) != 1:
        soft(400, "目标客服已禁用，不能转交")
    to_name = target["nickname"] or target["username"] or ""
    operator = current_handler_name(admin)
    old_uid, old_status = int(ticket["handler_uid"] or 0), status
    new_status = TICKET_STATUS_HANDLING if status == TICKET_STATUS_PENDING else status
    try:
        ticket["handler_uid"], ticket["handler_name"] = to_user_id, to_name
        ticket["status"] = new_status
        write("UPDATE tickets SET handler_uid = %s, handler_name = %s, status = %s "
              "WHERE id = %s", (to_user_id, to_name, new_status, ticket["id"]))
        adjust_handling(ticket["tenant_id"], old_uid, -1)
        adjust_handling(ticket["tenant_id"], to_user_id, 1)
        add_ticket_log(ticket, "transfer", old_status, new_status,
                       int(ticket["priority"] or 0), int(ticket["priority"] or 0),
                       operator, "转交工单给客服 " + to_name)
        save_message(ticket["thread_id"], "system",
                     "工单已转交给客服 " + to_name + "，请继续为您服务", "text", ticket["id"])
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    # Go 这里除中间件自动审计外，还额外手写了一条带 detail 的审计，两条都要有
    try:
        write_audit(admin, request, "update", str(ticket["id"]),
                    "/api/admin/tickets",
                    "转交工单：" + ticket["ticket_no"] + " → " + to_name)
    except Exception:
        pass
    go_broadcast("admins_tenant", {"type": "ticket_update", "ticket": ticket_json(ticket)},
                 tenant_id=ticket["tenant_id"])
    return {"code": 0, "msg": "已转交"}


@app.post("/api/admin/ticket/approve")
async def admin_ticket_approve(request: Request,
                               admin: Admin = Depends(require_perm("ticket:handle"))):
    body = await body_json(request)
    ticket_no = body.get("ticket_no") if isinstance(body, dict) else None
    if not isinstance(ticket_no, str) or not ticket_no:
        soft(400, "参数错误")
    confirmed = body.get("confirmed", False)
    wait_time = body.get("wait_time", 0)
    # Go 的 bool/int 强类型绑定：类型不符直接算绑定失败
    if (not isinstance(confirmed, bool) or isinstance(wait_time, bool)
            or not isinstance(wait_time, int)):
        soft(400, "参数错误")
    ticket = one(f"SELECT {TICKET_COLUMNS} FROM tickets WHERE ticket_no = %s "
                 f"AND deleted_at = 0 LIMIT 1", (ticket_no,))
    if not ticket:
        soft(404, "工单不存在")
    if ticket["tenant_id"] != admin.tenant_id:
        deny(403, "无权访问该租户工单")
    trace_id = request.headers.get("X-Trace-Id", "")
    if not confirmed:
        # 取消转接：先改状态，再唤醒 Agent（与 Go 的次序一致 —— Agent 失败时状态已改）
        try:
            update_ticket_status(ticket, TICKET_STATUS_CANCELED)
            add_ticket_log(ticket, "cancel", TICKET_STATUS_PENDING, TICKET_STATUS_CANCELED,
                           int(ticket["priority"] or 0), int(ticket["priority"] or 0),
                           "system", "取消转接申请，智能客服继续服务")
            content = agent_resume(ticket["thread_id"], False, wait_time,
                                   ticket["ticket_no"], trace_id)
        except RuntimeError as exc:
            soft(500, str(exc))
        except pymysql.MySQLError as exc:
            soft(500, str(exc))
        save_message(ticket["thread_id"], "system", "转接申请已取消，智能客服继续服务",
                     "text", ticket["id"])
        go_broadcast("user", {"type": "human_notice",
                              "msg": "转接申请已取消，智能客服继续为您服务"},
                     key=ticket["thread_id"])
        go_broadcast("admins_tenant", {"type": "ticket_update",
                                       "ticket": ticket_json(ticket)},
                     tenant_id=tenant_of_thread(ticket["user_id"] or ""))
        return {"code": 0, "msg": content}
    try:
        msg = takeover_ticket(ticket, wait_time, trace_id, 0, "system")
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "msg": msg}


# ============================================================================
# 阶段 3-4：聚合运维域（看板 / 审计日志 / 用量 / 会话运维 / 归档 / 模型配置 / 提取规则）
# ============================================================================

def require_platform():
    """对齐 Go PlatformAdminOnly：仅平台管理员，非平台管理员 403 仅平台管理员可操作。"""
    def guard(admin: Admin = Depends(current_admin)) -> Admin:
        if not admin.is_platform_admin:
            deny(403, "仅平台管理员可操作")
        return admin
    return guard


# ---------- 代理到 Agent（Go ProxyToAgent 的等价物） ----------

def agent_call(method, path, query="", body=None, content_type="", trace_id="",
               tenant_id="", timeout=60):
    """对齐 Go agent.AgentRequest：透传 query/body/Content-Type/Trace/Tenant + 内部 token。

    返回 (bytes, None) 或 (None, err_text)。Go 侧不检查 Agent 的 HTTP 状态，
    拿到什么原样回写 200，这里保持一致。

    timeout 对齐 Go 的两个 client：业务读写 60s（httpClient），
    管理台透传 120s（adminHTTPClient，Prompt/知识库可能耗时较长）。
    """
    url = AGENT_URL + path + (("?" + query) if query else "")
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    if trace_id:
        headers["X-Trace-Id"] = trace_id
    if tenant_id:
        headers["X-Tenant-Id"] = tenant_id
    token = os.getenv("INTERNAL_API_TOKEN", "")
    if token:
        headers["X-Internal-Token"] = token
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method, url, content=body or None, headers=headers)
        return resp.content, None
    except Exception as exc:                      # noqa: BLE001 - 透传底层错误文本
        return None, str(exc)


def agent_payload_response(data):
    """把 Agent 的原始字节回写给前端，并按 body.code 改写 HTTP 状态码。

    Go 侧这里有两层：`ProxyToAgent` 写的是 `c.Data(http.StatusOK, "application/json", data)`，
    随后**全局 StatusCode 中间件**按 body.code 把状态改写（403→403、400→400、500→500）。
    所以端到端看到的状态码是 body.code 映射出来的，不是固定的 200。
    非 JSON 的响应体（例如 Agent 直接回的 "Internal Server Error" 纯文本）不改写，仍是 200。
    """
    status = 200
    try:
        parsed = json.loads(data)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        code = parsed.get("code")
        if isinstance(code, int) and not isinstance(code, bool):
            status = code_to_status(code)
    return Response(content=data, media_type="application/json",
                    status_code=status)


async def proxy_to_agent(request, method, agent_path, tenant_id, timeout=120):
    raw = await request.body()
    data, err = agent_call(method, agent_path, request.url.query, raw,
                           request.headers.get("content-type", ""),
                           request.headers.get("X-Trace-Id", ""), tenant_id,
                           timeout)
    if err is not None:
        return JSONResponse(status_code=500,
                            content={"code": 500, "msg": "调用agent失败:" + err})
    return agent_payload_response(data)


@app.get("/api/admin/session/stats")
async def admin_session_stats(request: Request,
                              admin: Admin = Depends(require_perm("session:view"))):
    return await proxy_to_agent(request, "GET", "/agent/session/stats", admin.tenant_id)


@app.get("/api/admin/session/cleanup-count")
async def admin_session_cleanup_count(request: Request,
                                      admin: Admin = Depends(require_perm("session:cleanup"))):
    return await proxy_to_agent(request, "GET", "/agent/session/cleanup-count",
                                admin.tenant_id)


@app.post("/api/admin/session/cleanup")
async def admin_session_cleanup(request: Request,
                                admin: Admin = Depends(require_perm("session:cleanup"))):
    return await proxy_to_agent(request, "POST", "/agent/session/cleanup", admin.tenant_id)


@app.get("/api/admin/token-usage")
async def admin_token_usage(request: Request,
                            admin: Admin = Depends(require_perm("usage:view",
                                                                biz_scope=False))):
    # 平台管理员可透传 tenant_id/days/group_by 全览；普通租户强制绑定自身 tenant_id
    query = {k: v for k, v in request.query_params.items()}
    if not admin.is_platform_admin:
        query["tenant_id"] = admin.tenant_id
    from urllib.parse import urlencode
    data, err = agent_call("GET", "/agent/token-usage", urlencode(sorted(query.items())),
                           None, "", request.headers.get("X-Trace-Id", ""),
                           admin.tenant_id)
    if err is not None:
        return JSONResponse(status_code=200,
                            content={"code": 500, "msg": "调用agent失败:" + err})
    return Response(content=data, media_type="application/json")


# ---------- 运营看板 ----------

def build_dashboard(tenant_id):
    """逐字对齐 Go repository.BuildDashboard。"""
    ov = one(
        "SELECT "
        " (SELECT COUNT(DISTINCT m.thread_id) FROM messages m WHERE m.tenant_id = %s) AS total_sessions,"
        " (SELECT COUNT(*) FROM messages m WHERE m.tenant_id = %s) AS total_messages,"
        " (SELECT COALESCE(SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END), 0)"
        "    FROM messages m WHERE m.tenant_id = %s) AS user_messages,"
        " (SELECT COUNT(*) FROM tickets t WHERE t.tenant_id = %s AND t.status IN (0,1)) AS open_tickets,"
        " (SELECT COUNT(DISTINCT t.thread_id) FROM tickets t WHERE t.tenant_id = %s"
        "    AND t.status IN (0,1)) AS handoff_sessions,"
        " (SELECT COALESCE(AVG(r.score), 0) FROM satisfaction_ratings r WHERE r.tenant_id = %s) AS avg_rating,"
        " (SELECT COUNT(*) FROM satisfaction_ratings r WHERE r.tenant_id = %s) AS rating_count,"
        " (SELECT COALESCE(SUM(CASE WHEN r.score >= 4 THEN 1 ELSE 0 END), 0) * 100.0"
        "    / NULLIF((SELECT COUNT(*) FROM satisfaction_ratings r WHERE r.tenant_id = %s), 0)"
        "    FROM satisfaction_ratings r WHERE r.tenant_id = %s) AS satisfaction_rate",
        (tenant_id,) * 9,
    ) or {}
    overview = {
        "total_sessions": int(ov.get("total_sessions") or 0),
        "total_messages": int(ov.get("total_messages") or 0),
        "user_messages": int(ov.get("user_messages") or 0),
        "open_tickets": int(ov.get("open_tickets") or 0),
        "handoff_sessions": int(ov.get("handoff_sessions") or 0),
        "avg_rating": float(ov.get("avg_rating") or 0),
        # 无评价时 Go 侧是 NULL，float64 零值输出 0
        "satisfaction_rate": float(ov["satisfaction_rate"])
        if ov.get("satisfaction_rate") is not None else 0.0,
        "rating_count": int(ov.get("rating_count") or 0),
    }

    start = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d") + " 00:00:00"
    rows = many(
        "SELECT DATE(created_at) AS day, COUNT(DISTINCT thread_id) AS sessions, "
        "COUNT(*) AS messages FROM messages WHERE tenant_id = %s AND created_at >= %s "
        "GROUP BY DATE(created_at)", (tenant_id, start))
    by_date = {}
    for row in rows:
        day = row["day"]
        key = day.strftime("%Y-%m-%d") if isinstance(day, (datetime, date)) else str(day)[:10]
        by_date[key] = {"date": key, "sessions": int(row["sessions"] or 0),
                        "messages": int(row["messages"] or 0)}
    trend = []
    for offset in range(6, -1, -1):
        key = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
        trend.append(by_date.get(key) or {"date": key, "sessions": 0, "messages": 0})

    names = {0: "待处理", 1: "接管中", 2: "已取消", 3: "已关闭"}
    tickets = [
        {"status": int(row["status"]), "name": names.get(int(row["status"]), ""),
         "count": int(row["count"] or 0)}
        for row in many("SELECT status, COUNT(*) AS count FROM tickets WHERE tenant_id = %s "
                        "GROUP BY status", (tenant_id,))
    ]

    agents = [
        {"handler_name": row["handler_name"], "total": int(row["total"] or 0),
         "open": int(row["open"] or 0)}
        for row in many(
            "SELECT handler_name, COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN status IN (0,1) THEN 1 ELSE 0 END), 0) AS open "
            "FROM tickets WHERE tenant_id = %s AND handler_name != '' "
            "GROUP BY handler_name ORDER BY total DESC", (tenant_id,))
    ]
    return {"overview": overview, "trend": trend, "tickets": tickets,
            # Go 这里是 nil slice（无数据时 JSON 为 null），不是空数组
            "agents": agents or None}


@app.get("/api/admin/ops/dashboard")
def admin_ops_dashboard(admin: Admin = Depends(require_perm("dashboard:view"))):
    if not admin.tenant_id:
        deny(401, "未获取到租户信息")
    try:
        data = build_dashboard(admin.tenant_id)
    except pymysql.MySQLError as exc:
        soft(500, "统计失败: " + str(exc))
    return {"code": 0, "data": data}


# ---------- 审计日志 ----------

@app.get("/api/admin/audit-logs")
def admin_audit_logs(page: str = "1", page_size: str = "20", action: str = "",
                     object_type: str = "", operator: str = "", ip: str = "",
                     time_from: str = "", time_to: str = "",
                     admin: Admin = Depends(require_perm("audit:view",
                                                         biz_scope=False))):
    # 注意：Go 里 page/pageSize 既用于查询也用于回显，但钳制发生在 repository 内部，
    # handler 回显的是 Atoi 得到的原始值（非法 → 0；page_size=999 也原样回显 999）
    echo_page = to_int(page, 0)
    echo_size = to_int(page_size, 0)
    p, size = echo_page, echo_size
    if p < 1:
        p = 1
    if size < 1 or size > 200:
        size = 20
    where, args = [], []
    # 平台管理员全览所有租户；租户管理端仅本租户
    if not admin.is_platform_admin:
        where.append("tenant_id = %s")
        args.append(admin.tenant_id)
    if action:
        where.append("action = %s")
        args.append(action)
    if object_type:
        where.append("object_type = %s")
        args.append(object_type)
    if operator:
        where.append("operator LIKE %s")
        args.append("%" + operator + "%")
    if ip:
        where.append("ip LIKE %s")
        args.append("%" + ip + "%")
    if time_from:
        where.append("created_at >= %s")
        args.append(time_from)
    if time_to:
        where.append("created_at <= %s")
        args.append(time_to)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        total = int(one(f"SELECT COUNT(*) AS n FROM audit_logs{clause}", tuple(args))["n"] or 0)
        rows = many(
            "SELECT id, tenant_id, operator, user_id, action, object_type, object_id, "
            f"detail, ip, created_at FROM audit_logs{clause} ORDER BY id DESC "
            "LIMIT %s OFFSET %s", tuple(args) + (size, (p - 1) * size))
    except pymysql.MySQLError as exc:
        soft(500, "查询审计日志失败:" + str(exc))
    items = [{
        "id": row["id"], "tenant_id": row["tenant_id"], "operator": row["operator"],
        "user_id": int(row["user_id"] or 0), "action": row["action"],
        "object_type": row["object_type"], "object_id": row["object_id"],
        "detail": row["detail"], "ip": row["ip"], "created_at": iso(row["created_at"]),
    } for row in rows]
    # GORM 的 Find 会把目标切片初始化为空切片，所以无数据时是 []（不是 null）
    return {"code": 0, "data": {"list": items, "total": total, "page": echo_page,
                                "page_size": echo_size}}


# ---------- 消息归档 ----------

ARCHIVE_BATCH_SIZE = 500
ARCHIVABLE_WHERE = ("m.created_at < %s AND (m.ticket_id IS NULL OR t.status IN (2, 3))")


def archive_days(raw):
    days = to_int(raw, 0)
    if days <= 0:
        days = 90
    if days > 365:
        days = 365
    return days


def count_archivable_messages(days, tenant_id):
    sql = ("SELECT COUNT(*) AS n FROM messages AS m "
           "LEFT JOIN tickets t ON m.ticket_id = t.id "
           f"WHERE {ARCHIVABLE_WHERE}")
    args = [datetime.now() - timedelta(days=days)]
    if tenant_id:
        sql += " AND m.tenant_id = %s"
        args.append(tenant_id)
    return int(one(sql, tuple(args))["n"] or 0)


def archive_old_messages(days, tenant_id):
    """对齐 Go ArchiveOldMessages：分批（500）迁移，单批事务，失败即停并返回已归档数。"""
    cutoff = datetime.now() - timedelta(days=days)
    total = 0
    while True:
        sql = ("SELECT m.id, m.thread_id, m.role, m.content, m.tenant_id, m.ticket_id, "
               "m.is_read, m.created_at FROM messages AS m "
               "LEFT JOIN tickets t ON m.ticket_id = t.id "
               f"WHERE {ARCHIVABLE_WHERE}")
        args = [cutoff]
        if tenant_id:
            sql += " AND m.tenant_id = %s"
            args.append(tenant_id)
        sql += " ORDER BY m.id ASC LIMIT %s"
        args.append(ARCHIVE_BATCH_SIZE)
        batch = many(sql, tuple(args))
        if not batch:
            break
        now = datetime.now()
        try:
            with db() as conn:
                conn.begin()
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO messages_archive (thread_id, role, content, tenant_id, "
                        "ticket_id, is_read, created_at, archived_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        [(r["thread_id"], r["role"], r["content"], r["tenant_id"],
                          r["ticket_id"], r["is_read"], r["created_at"], now)
                         for r in batch])
                    cur.execute(
                        "DELETE FROM messages WHERE id IN ("
                        + ",".join(["%s"] * len(batch)) + ")",
                        tuple(r["id"] for r in batch))
                conn.commit()
        except pymysql.MySQLError:
            break
        total += len(batch)
    return total


@app.get("/api/admin/messages/archive-count")
def admin_archive_count(days: str = "90",
                        admin: Admin = Depends(require_perm("session:cleanup"))):
    # 已知矛盾（Go 侧）：handler 注释写明「平台管理员全览」，tenant_id 传空即归档
    # 全部租户；但这两条路由挂在 Go 的 biz 组（BusinessOnly），平台管理员在进
    # handler 之前就被 403，空租户分支当前不可达。保持鉴权不变（改分组会让已验收
    # 的「平台管理员 403」行为漂移），分支代码保留，待 Go 下线时统一处理。
    tenant_id = "" if admin.is_platform_admin else admin.tenant_id
    try:
        count = count_archivable_messages(archive_days(days), tenant_id)
    except pymysql.MySQLError as exc:
        soft(500, "查询失败:" + str(exc))
    return {"code": 0, "data": {"count": count}}


@app.post("/api/admin/messages/archive")
def admin_archive_messages(days: str = "90",
                           admin: Admin = Depends(require_perm("session:cleanup"))):
    # 注意：Go 从 query 读 days（不是 body），POST 也一样
    # 平台管理员全租户分支当前不可达，原因见 admin_archive_count 上方注释
    tenant_id = "" if admin.is_platform_admin else admin.tenant_id
    archived = archive_old_messages(archive_days(days), tenant_id)
    return {"code": 0, "msg": "归档完成", "data": {"archived": archived}}


# ---------- API Key 加密（与 Go crypto 包二进制兼容） ----------

MODEL_KEY_DEV = "cs_prod_default_encrypt_key_2026!"


def model_key_bytes():
    key = os.getenv("MODEL_KEY_ENCRYPT_KEY", "")
    if not key:
        key = MODEL_KEY_DEV
    return key.encode()[:32].ljust(32, b"\x00")


def encrypt_api_key(plain):
    if not plain:
        return ""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    sealed = AESGCM(model_key_bytes()).encrypt(nonce, plain.encode(), None)
    return base64.b64encode(nonce + sealed).decode()


def decrypt_api_key(encrypted):
    """解密失败一律返回空串 —— Go 侧调用方全部忽略 err，行为等价。"""
    if not encrypted:
        return ""
    try:
        data = base64.b64decode(encrypted)
        if len(data) < 12:
            return ""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(model_key_bytes()).decrypt(data[:12], data[12:], None).decode()
    except Exception:                              # noqa: BLE001 - 与 Go 一致：坏密文当空
        return ""


def mask_api_key(plain):
    if not plain:
        return ""
    if len(plain) <= 4:
        return "****"
    return "..." + plain[-4:]


def is_masked_key(value, stored_plain=""):
    """判断前端回传的 API Key 是否为掩码（掩码 = 用户未修改，保留原值）。

    掩码有两种形态，来自 mask_api_key()：
      - 原 Key <= 4 位 → "****"（含星号）
      - 原 Key >  4 位 → "..." + 后 4 位（**不含星号**）

    只查星号会把 "...abcd" 当成真实 Key 写进库：用户打开模型配置页只改了模型名、
    没动 Key，保存后 Key 就变成掩码字面量，之后所有 LLM 调用都会失败。
    stored_plain 传入已存 Key 明文时，额外做一次精确比对兜底。
    """
    if not isinstance(value, str) or not value:
        return False
    if "*" in value:
        return True
    if value.startswith("...") and len(value) == 7:
        return True
    return bool(stored_plain) and value == stored_plain


def infer_provider_key(stored_key, model_name, api_base):
    if stored_key:
        return stored_key
    if not model_name and not api_base:
        return "custom"
    row = one("SELECT provider_key FROM model_providers WHERE status = 1 "
              "AND provider_key != 'custom' AND default_model = %s "
              "ORDER BY sort_order ASC, id ASC LIMIT 1", (model_name,))
    if row and row["provider_key"]:
        return row["provider_key"]
    if api_base:
        row = one("SELECT provider_key FROM model_providers WHERE status = 1 "
                  "AND provider_key != 'custom' AND default_api_base = %s "
                  "ORDER BY sort_order ASC, id ASC LIMIT 1", (api_base,))
        if row and row["provider_key"]:
            return row["provider_key"]
    return "custom"


TENANT_CONFIG_COLUMNS = (
    "tenant_id, model_provider, llm_provider_id, model_provider_key, model_name, "
    "model_api_base, model_api_key, chat_input_cache_price, chat_input_price, "
    "chat_output_price, embedding_price, embedding_provider, embedding_provider_id, "
    "embedding_provider_key, embedding_model_name, embedding_api_base, embedding_api_key"
)


def tenant_config_row(tenant_id):
    return one(f"SELECT {TENANT_CONFIG_COLUMNS} FROM tenants "
               f"WHERE tenant_id = %s AND deleted_at = 0 LIMIT 1", (tenant_id,))


def get_business_model_config(tenant_id):
    row = tenant_config_row(tenant_id)
    if not row:
        return None
    chat_key = decrypt_api_key(row["model_api_key"])
    emb_key = decrypt_api_key(row["embedding_api_key"])
    return {
        "tenant_id": tenant_id,
        "chat_input_cache_price": float(row["chat_input_cache_price"] or 0),
        "chat_input_price": float(row["chat_input_price"] or 0),
        "chat_output_price": float(row["chat_output_price"] or 0),
        "embedding_price": float(row["embedding_price"] or 0),
        "model_config": {
            "model_provider": row["model_provider"] or "",
            "provider_id": int(row["llm_provider_id"] or 0),
            "provider_key": infer_provider_key(row["model_provider_key"] or "",
                                               row["model_name"] or "",
                                               row["model_api_base"] or ""),
            "model_name": row["model_name"] or "",
            "model_api_base": row["model_api_base"] or "",
            "api_key_configured": bool(row["model_api_key"]),
            "api_key_masked": mask_api_key(chat_key),
        },
        "embedding_config": {
            "embedding_provider": row["embedding_provider"] or "",
            "provider_id": int(row["embedding_provider_id"] or 0),
            "provider_key": infer_provider_key(row["embedding_provider_key"] or "",
                                               row["embedding_model_name"] or "",
                                               row["embedding_api_base"] or ""),
            "embedding_model_name": row["embedding_model_name"] or "",
            "embedding_api_base": row["embedding_api_base"] or "",
            "api_key_configured": bool(row["embedding_api_key"]),
            "api_key_masked": mask_api_key(emb_key),
        },
    }


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def update_model_config(tenant_id, body):
    """对齐 Go service.UpdateModelConfig：逐字段与库中现值比较，只把真正变化的写入。"""
    row = tenant_config_row(tenant_id)
    if not row:
        raise LookupError("租户不存在")
    updates = {}

    mc = body.get("model_config")
    if isinstance(mc, dict):
        provider = mc.get("model_provider") or ""
        if provider and provider != (row["model_provider"] or ""):
            updates["model_provider"] = provider
        provider_id = mc.get("provider_id") or 0
        if not isinstance(provider_id, int) or isinstance(provider_id, bool):
            provider_id = 0
        if provider_id != int(row["llm_provider_id"] or 0):
            updates["llm_provider_id"] = provider_id
        provider_key = mc.get("provider_key") or ""
        if provider_key and provider_key != (row["model_provider_key"] or ""):
            updates["model_provider_key"] = provider_key
        api_key = mc.get("model_api_key") or ""
        clear_key = bool(mc.get("clear_api_key"))
        if provider == "custom" and not api_key and not clear_key:
            if not decrypt_api_key(row["model_api_key"]):
                raise ValueError("自定义对话模型必须填写 API Key")
        model_name = mc.get("model_name") or ""
        if model_name and model_name != (row["model_name"] or ""):
            updates["model_name"] = model_name
        if _num(mc.get("chat_input_cache_price")) != float(row["chat_input_cache_price"] or 0):
            updates["chat_input_cache_price"] = _num(mc.get("chat_input_cache_price"))
        if _num(mc.get("chat_input_price")) != float(row["chat_input_price"] or 0):
            updates["chat_input_price"] = _num(mc.get("chat_input_price"))
        if _num(mc.get("chat_output_price")) != float(row["chat_output_price"] or 0):
            updates["chat_output_price"] = _num(mc.get("chat_output_price"))
        api_base = mc.get("model_api_base") or ""
        if api_base and api_base != (row["model_api_base"] or ""):
            updates["model_api_base"] = api_base
        if clear_key:
            if row["model_api_key"]:
                updates["model_api_key"] = ""
        elif api_key and not is_masked_key(api_key, decrypt_api_key(row["model_api_key"])):
            if api_key != decrypt_api_key(row["model_api_key"]):
                updates["model_api_key"] = encrypt_api_key(api_key)

    ec = body.get("embedding_config")
    if isinstance(ec, dict):
        provider = ec.get("embedding_provider") or ""
        if provider and provider != (row["embedding_provider"] or ""):
            updates["embedding_provider"] = provider
        provider_id = ec.get("provider_id") or 0
        if not isinstance(provider_id, int) or isinstance(provider_id, bool):
            provider_id = 0
        if provider_id != int(row["embedding_provider_id"] or 0):
            updates["embedding_provider_id"] = provider_id
        provider_key = ec.get("provider_key") or ""
        if provider_key and provider_key != (row["embedding_provider_key"] or ""):
            updates["embedding_provider_key"] = provider_key
        api_key = ec.get("embedding_api_key") or ""
        clear_key = bool(ec.get("clear_api_key"))
        if provider == "custom" and not api_key and not clear_key:
            if not decrypt_api_key(row["embedding_api_key"]):
                raise ValueError("自定义 Embedding 模型必须填写 API Key")
        model_name = ec.get("embedding_model_name") or ""
        if model_name and model_name != (row["embedding_model_name"] or ""):
            updates["embedding_model_name"] = model_name
        if _num(ec.get("embedding_price")) != float(row["embedding_price"] or 0):
            updates["embedding_price"] = _num(ec.get("embedding_price"))
        api_base = ec.get("embedding_api_base") or ""
        if api_base and api_base != (row["embedding_api_base"] or ""):
            updates["embedding_api_base"] = api_base
        if clear_key:
            if row["embedding_api_key"]:
                updates["embedding_api_key"] = ""
        elif api_key and not is_masked_key(api_key,
                                           decrypt_api_key(row["embedding_api_key"])):
            if api_key != decrypt_api_key(row["embedding_api_key"]):
                updates["embedding_api_key"] = encrypt_api_key(api_key)

    if not updates:
        return None
    assignments = ", ".join(f"{key} = %s" for key in updates)
    write(f"UPDATE tenants SET {assignments} WHERE tenant_id = %s",
          tuple(updates.values()) + (tenant_id,))
    return updates


def model_update_scope(updates):
    has_chat = has_emb = False
    for key in updates:
        if key.startswith("model_") or key == "llm_provider_id" or key.startswith("chat_"):
            has_chat = True
        if key.startswith("embedding_") or key == "embedding_provider_id":
            has_emb = True
    if has_chat and not has_emb:
        return "chat"
    if not has_chat and has_emb:
        return "embedding"
    return "all"


@app.get("/api/admin/model-config")
def admin_model_config(admin: Admin = Depends(require_perm("model:view"))):
    if not admin.tenant_id:
        soft(401, "未登录或 token 无效")
    result = get_business_model_config(admin.tenant_id)
    if result is None:
        soft(404, "租户不存在")
    return {"code": 0, "data": result}


@app.put("/api/admin/model-config")
async def admin_model_config_update(request: Request,
                                    admin: Admin = Depends(require_perm("model:edit"))):
    if not admin.tenant_id:
        soft(401, "未登录或 token 无效")
    body = await body_json(request)
    if not isinstance(body, dict):
        soft(400, "EOF")
    try:
        updates = update_model_config(admin.tenant_id, body)
    except LookupError:
        soft(404, "租户不存在")
    except ValueError as exc:
        soft(500, str(exc))
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    if updates is None:
        soft(400, "没有需要更新的字段")
    # 通知 Agent 清除该租户模型配置缓存（失败不影响主流程，同 Go）
    try:
        cache().publish("cs:model:cache:invalidate", json.dumps(
            {"tenant_id": admin.tenant_id, "scope": model_update_scope(updates)},
            ensure_ascii=False))
    except Exception:                              # noqa: BLE001 - best effort
        pass
    return {"code": 0, "msg": "模型配置已保存",
            "data": {"tenant_id": admin.tenant_id, "updated": updates}}


# ---------- 客户信息提取规则（平台级） ----------

def default_extract_rules_json():
    """复用阶段 3-2 的 DEFAULT_EXTRACT_RULES（(field, pattern) 元组），转成 API 的 JSON 形态。"""
    return [{"field": field, "pattern": pattern} for field, pattern in DEFAULT_EXTRACT_RULES]


# Go uses RE2 (regexp.Compile): no lookaround, no backreferences.
RE2_UNSUPPORTED = (("(?=", "(?="), ("(?!", "(?!"), ("(?<=", "(?<="), ("(?<!", "(?<!"),
                   ("\\1", "\\1"))


def compile_extract_pattern(pattern):
    """用 Go 的 RE2 语义校验；不通过时抛 ValueError，文案对齐 Go regexp 包。"""
    for token, _ in RE2_UNSUPPORTED:
        if token in pattern:
            raise ValueError("error parsing regexp: invalid or unsupported Perl syntax: `"
                             + token + "`")
    try:
        re.compile(pattern)
    except re.error as exc:
        text = str(exc)
        pos = getattr(exc, "pos", None) or 0
        tail = pattern[pos:] or pattern
        if "unterminated character set" in text:
            raise ValueError(f"error parsing regexp: missing closing ]: `{tail}`") from None
        if "unterminated subpattern" in text:
            raise ValueError(f"error parsing regexp: missing closing ): `{tail}`") from None
        if "nothing to repeat" in text:
            raise ValueError("error parsing regexp: missing argument to repetition "
                             f"operator: `{tail[:1]}`") from None
        if "bad escape" in text:
            raise ValueError(f"error parsing regexp: invalid escape sequence: `{pattern}`") \
                from None
        raise ValueError("error parsing regexp: " + text) from None


@app.get("/api/admin/extract-rules")
def admin_extract_rules(_admin: Admin = Depends(require_platform())):
    rows = many("SELECT config_key, config_value FROM platform_config "
                "WHERE config_key LIKE %s ORDER BY id ASC", ("cs_extract_rule_%",))
    rules = []
    for row in rows:
        field = row["config_key"].removeprefix("cs_extract_rule_")
        if field and row["config_value"]:
            rules.append({"field": field, "pattern": row["config_value"]})
    return {"code": 0, "data": rules or default_extract_rules_json()}


@app.put("/api/admin/extract-rules")
async def admin_extract_rules_update(request: Request,
                                     _admin: Admin = Depends(require_platform())):
    body = await body_json(request)
    if not isinstance(body, list):
        soft(400, "参数错误: json: cannot unmarshal object into Go value of type "
                  "[]model.ExtractRule")
    # 已修复（Go 侧缺陷）：Go 先 DELETE 全表再逐条校验，任一条 field/pattern 非法
    # 都会留下「旧规则已删、新规则只写了一部分」的中间态。这里先把整批校验完，
    # 全部通过才落库，失败时保持原规则不变。
    rules = []
    for item in body:
        if not isinstance(item, dict):
            soft(400, "参数错误: json: cannot unmarshal value into Go struct field "
                      "ExtractRule.field of type string")
        field = item.get("field") or ""
        pattern = item.get("pattern") or ""
        if not isinstance(field, str) or not isinstance(pattern, str):
            soft(400, "参数错误: json: cannot unmarshal value into Go struct field "
                      "ExtractRule.field of type string")
        if not field or not pattern:
            continue
        try:
            compile_extract_pattern(pattern)
        except ValueError as exc:
            soft(500, str(exc))
        rules.append((field, pattern))
    write("DELETE FROM platform_config WHERE config_key LIKE %s", ("cs_extract_rule_%",))
    now = datetime.now()
    for field, pattern in rules:
        write("INSERT INTO platform_config (config_key, config_value, description, "
              "created_at, updated_at) VALUES (%s, %s, %s, %s, %s)",
              ("cs_extract_rule_" + field, pattern, "客户信息提取规则-" + field, now, now))
    # Go 侧 extractRulesCache 是进程内缓存，不会感知本层的写入 → 主动失效（best effort）
    reload_go_extract_rules()
    return {"code": 0, "msg": "提取规则已更新"}


# ==================== 阶段 3-5：RBAC（菜单树 / 成员 / 角色） ====================
# Go 侧数据模型：menus（菜单=权限载体）→ sys_role_menu（角色-菜单）→ roles → user_roles → users。
# 权限码只从 menus.perm（status=1）推导，角色「是否管理员」也只看权限集合是否覆盖全目录
# （repository.isFullPerms），与角色名/内置标记解耦 —— 这里必须照抄，否则 admin 角色改名后两边判定不同。

MENU_BIZ_GROUP = "业务中心"
MENU_SYS_GROUP = "系统管理"
ZERO_TIME_RFC3339 = "0001-01-01T00:00:00Z"   # Go 非指针 time.Time 的零值（DB NULL）

# 逐字对齐 Go permission.Catalog（仅取 code 用于 isFullPerms，名称/分组只作可读注释）
PERMISSION_CODES = (
    "session:view", "session:profile:edit", "session:cleanup",
    "ticket:view", "ticket:handle",
    "customer:view",
    "dashboard:view",
    "knowledge:view", "knowledge:add", "knowledge:edit", "knowledge:remove",
    "prompt:view", "prompt:add", "prompt:edit", "prompt:remove",
    "model:view", "model:edit",
    "usage:view", "audit:view",
    "quickreply:view", "quickreply:add", "quickreply:edit", "quickreply:remove",
    "rating:view",
    "member:view", "member:add", "member:edit", "member:remove",
    "role:view", "role:add", "role:edit", "role:remove",
)

# 对齐 Go permission.StaffPerms：内置「客服」角色的初始授权集合
STAFF_PERMS = (
    "session:view", "session:profile:edit",
    "ticket:view", "ticket:handle",
    "customer:view",
    "dashboard:view",
    "knowledge:view",
    "quickreply:view",
    "rating:view",
)


@contextmanager
def transaction():
    """显式事务（db() 是 autocommit，这里 begin/commit 覆盖）。"""
    conn = db()
    try:
        conn.begin()
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def iso_zero(value):
    """Go 非指针 time.Time：DB NULL 读成零值，序列化为 0001-01-01T00:00:00Z。"""
    return iso(value) if value else ZERO_TIME_RFC3339


def go_time_text(value):
    """Go `time.Format("2006-01-02 15:04:05")`：角色列表的时间字段用这个格式。"""
    if not value:
        return "0001-01-01 00:00:00"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def go_uints(value):
    """Go `[]uint64` 的 JSON 绑定语义：非数组 / 含负值 / 含非整数 → None（绑定失败）。"""
    if not isinstance(value, list):
        return None
    out = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            return None
        out.append(item)
    return out


def go_int_field(body, key, default=0):
    """Go `int` 字段的绑定语义：缺失取 0；存在但不是整数 → None（绑定失败）。"""
    if key not in body:
        return default
    value = body[key]
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def rune_len(text):
    """Go `utf8.RuneCountInString`：按码点计数（Python len 对 str 等价）。"""
    return len(text)


# ---------- 菜单 ----------

MENU_COLUMNS = ("id, parent_id, group_name, name, type, path, component, perm, icon, sort, "
                "visible, status, is_builtin, created_at, updated_at")


def all_menus():
    """对齐 Go cachedMenus 的排序：group_name ASC, sort ASC, id ASC（权限目录顺序依赖它）。"""
    return many(f"SELECT {MENU_COLUMNS} FROM menus ORDER BY group_name ASC, sort ASC, id ASC")


def menu_json(row):
    return {
        "id": row["id"], "parent_id": row["parent_id"] or 0,
        "group_name": row["group_name"] or "", "name": row["name"] or "",
        "type": row["type"], "path": row["path"] or "", "component": row["component"] or "",
        "perm": row["perm"] or None, "icon": row["icon"] or "",
        "sort": row["sort"] or 0, "visible": row["visible"], "status": row["status"],
        "is_builtin": row["is_builtin"],
        "created_at": iso_zero(row["created_at"]), "updated_at": iso_zero(row["updated_at"]),
    }


def menu_tree(perms, scope):
    """对齐 Go repository.MenuTreeForUser（含子账号可见集合与父链保留）。

    scope：platform（平台管理员→系统管理组）/ tenant-admin（租户 app_key→业务中心全量）/
    tenant-user（租户子账号→业务中心按权限过滤）/ all（无租户上下文的演示模式）。
    """
    active = [m for m in all_menus() if m["status"] == 1]
    if scope == "platform":
        scoped = [m for m in active if m["group_name"] == MENU_SYS_GROUP]
    elif scope in ("tenant-admin", "tenant-user"):
        scoped = [m for m in active if m["group_name"] == MENU_BIZ_GROUP]
    else:
        scoped = active
    if not scoped:
        return None                      # Go return nil → JSON null

    visible = set()
    if scope == "tenant-user":
        by_id = {m["id"]: m for m in scoped}

        def mark(menu_id):
            if menu_id in visible:
                return
            visible.add(menu_id)
            parent = (by_id.get(menu_id) or {}).get("parent_id") or 0
            if parent > 0:
                mark(parent)

        for m in scoped:
            perm = m["perm"]
            # type != 1：目录节点不参与匹配，只由命中的叶子节点反向补齐父链
            if m["status"] == 1 and m["type"] != 1 and perm and perm in perms:
                mark(m["id"])

    def keep(m):
        return scope != "tenant-user" or m["id"] in visible

    group_order = []
    for m in scoped:
        if keep(m) and m["group_name"] not in group_order:
            group_order.append(m["group_name"])

    nodes = {g: [] for g in group_order}
    for m in scoped:
        if keep(m):
            nodes[m["group_name"]].append(menu_json(m))

    groups = []
    for g in group_order:
        by_id = {n["id"]: n for n in nodes[g]}
        roots = []
        for node in nodes[g]:
            parent = node["parent_id"]
            if parent > 0 and parent in by_id:
                by_id[parent].setdefault("children", []).append(node)
                continue
            roots.append(node)
        groups.append({"title": g, "group": g, "children": roots or None})
    return groups


@app.get("/api/admin/menus/tree")
def admin_menu_tree(admin: Admin = Depends(current_admin)):
    """侧边栏/动态路由数据源：按登录身份隔离（平台/租户管理员/子账号三种视图）。"""
    if admin.is_platform_admin:
        scope, perms = "platform", []
    elif admin.user_id == 0:
        scope, perms = "tenant-admin", []
    else:
        scope, perms = "tenant-user", user_perms(admin.user_id)
    return {"code": 0, "data": menu_tree(perms, scope)}


@app.get("/api/admin/permissions")
def admin_permission_catalog(admin: Admin = Depends(require_perm("role:view"))):
    """权限目录：从 menus 表推导（菜单=权限载体），与菜单管理页始终一致。"""
    items = [{"code": m["perm"], "name": m["name"] or "", "group": m["group_name"] or ""}
             for m in all_menus() if m["perm"]]
    return {"code": 0, "data": items}


# ---------- 角色 ----------

def role_menu_ids(role_id):
    rows = many("SELECT menu_id FROM sys_role_menu WHERE role_id = %s", (role_id,))
    return [r["menu_id"] for r in rows]


def role_perms(role_id):
    """对齐 PermsOfMenuIDs(RoleMenuIDs(role_id))：只取启用且 perm 非空的菜单。"""
    rows = many(
        "SELECT DISTINCT m.perm FROM sys_role_menu rm JOIN menus m ON m.id = rm.menu_id "
        "WHERE rm.role_id = %s AND m.status = 1 AND m.perm IS NOT NULL AND m.perm <> ''",
        (role_id,))
    return {r["perm"] for r in rows}


def is_full_perms(perms):
    if not perms:
        return False
    return all(code in perms for code in PERMISSION_CODES)


def is_admin_role(role_id):
    return is_full_perms(role_perms(role_id))


def load_role(role_id):
    return one("SELECT id, tenant_id, code, name, description, created_at, updated_at "
               "FROM roles WHERE id = %s AND deleted_at = 0 LIMIT 1", (role_id,))


def role_json(row):
    return {
        "id": row["id"], "tenant_id": row["tenant_id"], "code": row["code"] or "",
        "name": row["name"] or "", "description": row["description"] or "",
        "menu_ids": role_menu_ids(row["id"]), "is_admin": is_admin_role(row["id"]),
        "created_at": go_time_text(row["created_at"]),
        "updated_at": go_time_text(row["updated_at"]),
    }


def ensure_tenant_roles(tenant_id):
    """对齐 Go repository.EnsureTenantRoles：一次性种入 admin/staff 预置角色。

    语义要点（新项目口径，不做存量兼容）：
    - 已有角色 → 直接跳过（预置角色可删可改，删光也不补种）
    - rbac_seeded=1 且无角色 → 同样跳过
    - 只有「从未初始化过」的租户才会写入，因此本函数对既有租户是纯读
    """
    now = datetime.now()
    with transaction() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM roles WHERE tenant_id = %s AND deleted_at = 0",
                    (tenant_id,))
        if (cur.fetchone() or {}).get("c", 0) > 0:
            return
        cur.execute("SELECT rbac_seeded FROM tenants WHERE tenant_id = %s AND deleted_at = 0 "
                    "ORDER BY id ASC LIMIT 1 FOR UPDATE", (tenant_id,))
        tenant_row = cur.fetchone()
        locked = tenant_row is not None
        if locked and tenant_row["rbac_seeded"]:
            return
        builtins = (("admin", "管理员", "租户全部权限"),
                    ("staff", "客服", "会话/工单/客户/运营看板/知识库查看"))
        for code, name, description in builtins:
            cur.execute(
                "INSERT INTO roles (tenant_id, code, name, description, created_at, updated_at, "
                "deleted_at) VALUES (%s, %s, %s, %s, %s, %s, 0)",
                (tenant_id, code, name, description, now, now))
            role_id = cur.lastrowid
            if code == "admin":
                cur.execute("SELECT id FROM menus WHERE group_name = %s ORDER BY id ASC",
                            (MENU_BIZ_GROUP,))
            else:
                holders = ", ".join(["%s"] * len(STAFF_PERMS))
                cur.execute(f"SELECT id FROM menus WHERE perm IN ({holders}) AND status = 1 "
                            f"ORDER BY id ASC", STAFF_PERMS)
            for menu_row in cur.fetchall():
                cur.execute("INSERT INTO sys_role_menu (role_id, menu_id) VALUES (%s, %s)",
                            (role_id, menu_row["id"]))
        if locked:
            cur.execute("UPDATE tenants SET rbac_seeded = 1 WHERE tenant_id = %s", (tenant_id,))


def gen_role_code():
    """租户内唯一角色编码：custom_<12 位 hex>（创建后不可改）。"""
    return "custom_" + secrets.token_hex(6)


def set_role_menus(cur, role_id, menu_ids):
    cur.execute("DELETE FROM sys_role_menu WHERE role_id = %s", (role_id,))
    seen = set()
    for menu_id in menu_ids:
        if menu_id > 0 and menu_id not in seen:
            seen.add(menu_id)
            cur.execute("INSERT INTO sys_role_menu (role_id, menu_id) VALUES (%s, %s)",
                        (role_id, menu_id))


@app.get("/api/admin/roles")
def admin_roles(admin: Admin = Depends(require_perm("role:view"))):
    try:
        ensure_tenant_roles(admin.tenant_id)
    except pymysql.MySQLError as exc:
        deny(500, "角色初始化失败: " + str(exc))
    rows = many("SELECT id, tenant_id, code, name, description, created_at, updated_at "
                "FROM roles WHERE tenant_id = %s AND deleted_at = 0 ORDER BY id ASC",
                (admin.tenant_id,))
    return {"code": 0, "data": [role_json(row) for row in rows]}


@app.post("/api/admin/roles")
async def admin_role_create(request: Request, admin: Admin = Depends(require_perm("role:add"))):
    body = await body_json(request)
    if not isinstance(body, dict):
        deny(400, "参数错误")
    name = body.get("name")
    menu_ids = go_uints(body.get("menu_ids") or [])
    if not isinstance(name, str) or not name.strip() or menu_ids is None:
        deny(400, "参数错误")
    if body.get("description") is not None and not isinstance(body.get("description"), str):
        deny(400, "参数错误")
    description = body.get("description") or ""
    # 已修复（Go 侧缺陷）：Go 的 handler 写死 Code: genRoleCode()，请求体里的 code
    # 被直接丢弃，调用方无法指定稳定编码（对接外部系统时需要按 code 引用角色）。
    # 这里放开：传了就用（限长 32，与 roles.code 列宽一致），没传才自动生成。
    code = body.get("code")
    if code is not None and (not isinstance(code, str) or not code.strip()):
        deny(400, "参数错误")
    if isinstance(code, str):
        code = code.strip()
        if len(code) > 32:
            deny(400, "角色编码长度不能超过 32 个字符")
    try:
        ensure_tenant_roles(admin.tenant_id)
    except pymysql.MySQLError:
        deny(500, "角色初始化失败")
    role_name = name.strip()
    try:
        with transaction() as cur:
            now = datetime.now()
            cur.execute(
                "INSERT INTO roles (tenant_id, code, name, description, created_at, updated_at, "
                "deleted_at) VALUES (%s, %s, %s, %s, %s, %s, 0)",
                (admin.tenant_id, code or gen_role_code(), role_name, description, now, now))
            role_id = cur.lastrowid
            # 注意：Go CreateRole 不去重，重复 menu_id 会撞主键 → 整体失败（照抄该行为）
            for menu_id in menu_ids:
                cur.execute("INSERT INTO sys_role_menu (role_id, menu_id) VALUES (%s, %s)",
                            (role_id, menu_id))
    except pymysql.MySQLError:
        deny(500, "创建角色失败")
    try:
        write_handler_audit(admin, request, "create", "role", role_name, "新建角色")
    except Exception:
        pass
    return {"code": 0, "msg": "角色已创建", "data": {"id": role_id}}


@app.put("/api/admin/roles/{id}")
async def admin_role_update(id: str, request: Request,
                            admin: Admin = Depends(require_perm("role:edit"))):
    body = await body_json(request)
    if not isinstance(body, dict):
        body = {}
    name = body.get("name")
    role_id = go_parse_uint(id)
    if (not isinstance(name, str) or not name.strip() or role_id == 0
            or body.get("description") is not None and not isinstance(body.get("description"), str)):
        deny(400, "参数错误")
    menu_ids = go_uints(body.get("menu_ids") or [])
    if menu_ids is None:
        deny(400, "参数错误")
    role = load_role(role_id)
    if not role or role["tenant_id"] != admin.tenant_id:
        deny(400, "角色不存在")
    try:
        with transaction() as cur:
            cur.execute("UPDATE roles SET name = %s, description = %s, updated_at = %s "
                        "WHERE id = %s", (name.strip(), body.get("description") or "",
                                          datetime.now(), role_id))
            set_role_menus(cur, role_id, menu_ids)
    except pymysql.MySQLError:
        deny(500, "更新角色失败")
    try:
        write_handler_audit(admin, request, "update", "role", role["name"] or "",
                            "更新角色权限")
    except Exception:
        pass
    return {"code": 0, "msg": "角色已更新"}


@app.delete("/api/admin/roles/{id}")
def admin_role_delete(id: str, request: Request,
                      admin: Admin = Depends(require_perm("role:remove"))):
    role_id = go_parse_uint(id)
    if role_id == 0:
        deny(400, "参数错误")
    role = load_role(role_id)
    if not role or role["tenant_id"] != admin.tenant_id:
        deny(400, "角色不存在")
    try:
        with transaction() as cur:
            # 成员引用自动解绑（成员保留，只是失去该角色权限）+ 清菜单授权 + 软删角色
            cur.execute("DELETE FROM user_roles WHERE role_id = %s", (role_id,))
            cur.execute("DELETE FROM sys_role_menu WHERE role_id = %s", (role_id,))
            cur.execute("UPDATE roles SET deleted_at = %s WHERE id = %s",
                        (int(time.time()), role_id))
    except pymysql.MySQLError as exc:
        deny(400, str(exc))
    try:
        write_handler_audit(admin, request, "delete", "role", role["name"] or "", "删除角色")
    except Exception:
        pass
    return {"code": 0, "msg": "角色已删除"}


# ---------- 成员 ----------

USER_COLUMNS = ("id, tenant_id, username, nickname, phone, email, remark, status, "
                "last_login_at, created_at, updated_at")


def hash_user_password(tenant_id, username, password):
    """对齐 Go crypto.HashUserPassword：sha256(tenant_id|username|password) hex。"""
    return hashlib.sha256(f"{tenant_id}|{username}|{password}".encode()).hexdigest()


def get_user_roles(user_id):
    """成员角色摘要（多角色权限取并集）。顺序沿用 user_roles 主键顺序（role_id ASC）。"""
    rels = many("SELECT user_id, role_id FROM user_roles WHERE user_id = %s", (user_id,))
    items = []
    for rel in rels:
        role = one("SELECT id, name, code FROM roles WHERE id = %s AND deleted_at = 0 LIMIT 1",
                   (rel["role_id"],))
        if not role:
            continue
        items.append({"id": role["id"], "name": role["name"] or "", "code": role["code"] or "",
                      "is_admin": is_admin_role(role["id"])})
    return items


def user_json(row):
    return {
        "id": row["id"], "tenant_id": row["tenant_id"], "username": row["username"] or "",
        "nickname": row["nickname"] or "", "phone": row["phone"] or "",
        "email": row["email"] or "", "remark": row["remark"] or "", "status": row["status"],
        "last_login_at": iso(row["last_login_at"]),
        "created_at": iso_zero(row["created_at"]), "updated_at": iso_zero(row["updated_at"]),
        "roles": get_user_roles(row["id"]),
    }


def load_user(user_id):
    return one(f"SELECT {USER_COLUMNS} FROM users WHERE id = %s AND deleted_at = 0 LIMIT 1",
               (user_id,))


def set_user_roles(cur, tenant_id, user_id, role_ids):
    """覆盖式设置成员角色；只接受归属本租户的角色（其余静默丢弃）。"""
    cur.execute("SELECT id FROM roles WHERE tenant_id = %s AND deleted_at = 0", (tenant_id,))
    valid = {r["id"] for r in cur.fetchall()}
    cur.execute("DELETE FROM user_roles WHERE user_id = %s", (user_id,))
    for role_id in role_ids:
        if role_id > 0 and role_id in valid:
            cur.execute("INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)",
                        (user_id, role_id))


@app.get("/api/admin/members")
def admin_members(q: str = "", role_id: str = "", status: str = "",
                  admin: Admin = Depends(require_perm("member:view"))):
    try:
        ensure_tenant_roles(admin.tenant_id)
    except pymysql.MySQLError:
        deny(500, "角色初始化失败")
    keyword = (q or "").strip()
    role_filter = go_parse_uint(role_id)
    status_filter = -1
    raw_status = (status or "").strip()
    if raw_status:
        parsed = to_int(raw_status, None)
        if parsed in (0, 1):
            status_filter = parsed
    where = "tenant_id = %s AND deleted_at = 0"
    args = [admin.tenant_id]
    if keyword:
        where += " AND username LIKE %s"
        args.append("%" + keyword + "%")
    if role_filter > 0:
        where += " AND id IN (SELECT user_id FROM user_roles WHERE role_id = %s)"
        args.append(role_filter)
    if status_filter >= 0:
        where += " AND status = %s"
        args.append(status_filter)
    rows = many(f"SELECT {USER_COLUMNS} FROM users WHERE {where} ORDER BY id ASC", tuple(args))
    return {"code": 0, "data": [user_json(row) for row in rows]}


@app.post("/api/admin/members")
async def admin_member_create(request: Request,
                              admin: Admin = Depends(require_perm("member:add"))):
    body = await body_json(request)
    if not isinstance(body, dict):
        body = {}
    username = body.get("username")
    password = body.get("password")
    role_ids = go_uints(body.get("role_ids") or [])
    if (not isinstance(username, str) or not isinstance(password, str) or not role_ids
            or role_ids is None):
        deny(400, "参数错误（至少分配一个角色）")
    nickname = body.get("nickname")
    phone = body.get("phone")
    email = body.get("email")
    remark = body.get("remark")
    for value in (nickname, phone, email, remark):
        if value is not None and not isinstance(value, str):
            deny(400, "参数错误（至少分配一个角色）")
    if rune_len(username) < 3 or rune_len(username) > 32:
        deny(400, "用户名长度需 3-32 个字符")
    if phone and not re.fullmatch(r"1[3-9]\d{9}", phone):
        deny(400, "手机号格式不正确")
    if email and "@" not in email:
        deny(400, "邮箱格式不正确")
    if len(password) < 8:
        deny(400, "密码长度至少 8 位")
    try:
        ensure_tenant_roles(admin.tenant_id)
    except pymysql.MySQLError:
        deny(500, "角色初始化失败")
    if one("SELECT id FROM users WHERE tenant_id = %s AND username = %s AND deleted_at = 0 "
           "LIMIT 1", (admin.tenant_id, username)):
        deny(400, "用户名已存在")
    try:
        with transaction() as cur:
            now = datetime.now()
            cur.execute(
                "INSERT INTO users (tenant_id, username, password_hash, nickname, phone, email, "
                "remark, status, created_at, updated_at, deleted_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s, 0)",
                (admin.tenant_id, username, hash_user_password(admin.tenant_id, username, password),
                 (nickname or "").strip(), (phone or "").strip(), (email or "").strip(),
                 (remark or "").strip(), now, now))
            user_id = cur.lastrowid
            for role_id in role_ids:
                if role_id:
                    cur.execute("INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)",
                                (user_id, role_id))
    except pymysql.MySQLError:
        deny(500, "创建成员失败")
    try:
        write_handler_audit(admin, request, "create", "member", username, "添加成员")
    except Exception:
        pass
    return {"code": 0, "msg": "成员已创建", "data": {"id": user_id}}


@app.put("/api/admin/members/{id}/roles")
async def admin_member_roles(id: str, request: Request,
                             admin: Admin = Depends(require_perm("member:edit"))):
    user_id = go_parse_uint(id)
    if user_id == 0:
        deny(400, "参数错误")
    if user_id == admin.user_id:
        deny(400, "不能修改自己的角色")
    body = await body_json(request)
    role_ids = go_uints((body or {}).get("role_ids") or []) if isinstance(body, dict) else None
    if not role_ids:
        deny(400, "至少分配一个角色")
    user = load_user(user_id)
    if not user or user["tenant_id"] != admin.tenant_id:
        deny(400, "成员不存在")
    try:
        with transaction() as cur:
            set_user_roles(cur, admin.tenant_id, user_id, role_ids)
    except pymysql.MySQLError:
        deny(500, "更新角色失败")
    try:
        write_handler_audit(admin, request, "update", "member", str(user_id), "变更成员角色")
    except Exception:
        pass
    return {"code": 0, "msg": "角色已更新"}


@app.put("/api/admin/members/{id}")
async def admin_member_update(id: str, request: Request,
                              admin: Admin = Depends(require_perm("member:edit"))):
    user_id = go_parse_uint(id)
    if user_id == 0:
        deny(400, "参数错误")
    if user_id == admin.user_id:
        deny(400, "不能修改自己的账号信息")
    body = await body_json(request)
    if not isinstance(body, dict):
        deny(400, "参数错误")
    username = body.get("username")
    nickname = body.get("nickname")
    phone = body.get("phone")
    email = body.get("email")
    remark = body.get("remark")
    role_ids = go_uints(body.get("role_ids") or [])
    if role_ids is None:
        deny(400, "参数错误")
    for value in (username, nickname, phone, email, remark):
        if value is not None and not isinstance(value, str):
            deny(400, "参数错误")
    username, nickname, phone = username or "", nickname or "", phone or ""
    email, remark = email or "", remark or ""
    if (not username.strip() and not nickname and not phone and not email and not remark
            and not role_ids):
        deny(400, "参数错误")
    user = load_user(user_id)
    if not user or user["tenant_id"] != admin.tenant_id:
        deny(400, "成员不存在")

    updates = {}
    if username.strip() and username != (user["username"] or ""):
        name = username.strip()
        if rune_len(name) < 3 or rune_len(name) > 32:
            deny(400, "用户名长度需 3-32 个字符")
        exist = one("SELECT id FROM users WHERE tenant_id = %s AND username = %s "
                    "AND deleted_at = 0 LIMIT 1", (admin.tenant_id, name))
        if exist and exist["id"] != user_id:
            deny(400, "用户名已存在")
        updates["username"] = name
    if nickname and nickname != (user["nickname"] or ""):
        updates["nickname"] = nickname.strip()
    if phone and phone != (user["phone"] or ""):
        if not re.fullmatch(r"1[3-9]\d{9}", phone):
            deny(400, "手机号格式不正确")
        updates["phone"] = phone.strip()
    if email and email != (user["email"] or ""):
        if "@" not in email:
            deny(400, "邮箱格式不正确")
        updates["email"] = email.strip()
    if remark and remark != (user["remark"] or ""):
        updates["remark"] = remark.strip()

    try:
        with transaction() as cur:
            if updates:
                sets = ", ".join(f"{key} = %s" for key in updates)
                cur.execute(f"UPDATE users SET {sets}, updated_at = %s WHERE id = %s "
                            f"AND tenant_id = %s AND deleted_at = 0",
                            (*updates.values(), datetime.now(), user_id, admin.tenant_id))
            if role_ids:
                set_user_roles(cur, admin.tenant_id, user_id, role_ids)
    except pymysql.MySQLError:
        deny(500, "更新成员信息失败")
    try:
        write_handler_audit(admin, request, "update", "member", str(user_id), "编辑成员信息")
    except Exception:
        pass
    return {"code": 0, "msg": "成员已更新"}


@app.put("/api/admin/members/{id}/status")
async def admin_member_status(id: str, request: Request,
                              admin: Admin = Depends(require_perm("member:edit"))):
    user_id = go_parse_uint(id)
    if user_id == 0:
        deny(400, "参数错误")
    if user_id == admin.user_id:
        deny(400, "不能操作自己的账号")
    body = await body_json(request)
    if not isinstance(body, dict):
        body = {}
    status = go_int_field(body, "status", 0)
    if status not in (0, 1):
        deny(400, "参数错误")
    # 注意：Go 这里不校验成员是否存在，id 不存在也会回 200「已更新」并记审计（照抄）
    try:
        write("UPDATE users SET status = %s, updated_at = %s WHERE id = %s AND tenant_id = %s "
              "AND deleted_at = 0", (status, datetime.now(), user_id, admin.tenant_id))
    except pymysql.MySQLError:
        deny(500, "更新失败")
    try:
        write_handler_audit(admin, request, "enable" if status == 1 else "disable", "member",
                            str(user_id), "变更成员状态")
    except Exception:
        pass
    return {"code": 0, "msg": "已更新"}


@app.put("/api/admin/members/{id}/password")
async def admin_member_password(id: str, request: Request,
                                admin: Admin = Depends(require_perm("member:edit"))):
    user_id = go_parse_uint(id)
    if user_id == 0:
        deny(400, "参数错误")
    body = await body_json(request)
    password = (body or {}).get("password") if isinstance(body, dict) else None
    if not isinstance(password, str) or len(password) < 8:
        deny(400, "密码长度至少 8 位")
    user = load_user(user_id)
    if not user or user["tenant_id"] != admin.tenant_id:
        deny(400, "成员不存在")
    try:
        write("UPDATE users SET password_hash = %s, updated_at = %s WHERE id = %s "
              "AND tenant_id = %s AND deleted_at = 0",
              (hash_user_password(admin.tenant_id, user["username"], password),
               datetime.now(), user_id, admin.tenant_id))
    except pymysql.MySQLError:
        deny(500, "重置失败")
    try:
        write_handler_audit(admin, request, "reset_password", "member", user["username"] or "",
                            "重置成员密码")
    except Exception:
        pass
    return {"code": 0, "msg": "密码已重置"}


@app.delete("/api/admin/members/{id}")
def admin_member_delete(id: str, request: Request,
                        admin: Admin = Depends(require_perm("member:remove"))):
    user_id = go_parse_uint(id)
    if user_id == 0:
        deny(400, "参数错误")
    if user_id == admin.user_id:
        deny(400, "不能删除自己的账号")
    user = load_user(user_id)
    if not user or user["tenant_id"] != admin.tenant_id:
        deny(400, "成员不存在")
    try:
        with transaction() as cur:
            cur.execute("DELETE FROM user_roles WHERE user_id = %s", (user_id,))
            # 软删除：deleted_at 存 unix 秒（与该表其它软删除记录一致）
            cur.execute("UPDATE users SET deleted_at = %s WHERE id = %s AND tenant_id = %s",
                        (int(time.time()), user_id, admin.tenant_id))
    except pymysql.MySQLError:
        deny(500, "删除失败")
    try:
        write_handler_audit(admin, request, "delete", "member", user["username"] or "", "删除成员")
    except Exception:
        pass
    return {"code": 0, "msg": "成员已删除"}



# ==================== 阶段 3-6：平台域（租户 / 模型提供商 / 菜单） ====================
# 全部挂 Go 的 plat 组（PlatformAdminOnly）：非平台管理员一律 403「仅平台管理员可操作」。
# 注意本域两套错误语义不能混：
#   - 租户 / 模型提供商：Go 写的是 c.JSON(http.StatusOK, {code:N}) → 软失败，审计中间件仍记一条
#   - 菜单：Go 写的是 c.JSON(http.StatusBadRequest/500) → 真失败，审计中间件跳过
# 因此前者一律用 soft()，后者一律用 deny()，否则 audit_logs 会与 Go 不一致。


def go_internal(method, path, body=None, timeout=5):
    """调 Go 内部接口（并行期专用）。失败返回 None，调用方按 best-effort 处理。"""
    headers = {"X-Internal-Token": os.getenv("INTERNAL_API_TOKEN", "")}
    try:
        resp = httpx.request(method, GO_INTERNAL_URL + path, json=body,
                             headers=headers, timeout=timeout)
        return resp.json()
    except (httpx.HTTPError, ValueError):
        return None


def reload_go_menu_caches():
    """通知 Go 失效 menuCache + rolePermsCache（并行期专用）。"""
    for _ in range(2):
        if go_internal("POST", "/api/agent/menus/reload") is not None:
            return


def reload_go_tenants():
    """通知 Go 重载 auth 内存缓存（并行期专用）。

    Go 把 tenants 全量缓存在进程内（tenantByKey / TenantsList）用于令牌校验与登录。
    本层写库后不重载的话会出现两个真实故障：
      1) 新建租户用 app_key + secret 登录失败（缓存里查不到）
      2) 已禁用租户的旧令牌在 Go 侧仍然有效
    因此租户的增删改 / 启停 / 重置密钥之后都必须调用一次。
    """
    for _ in range(2):
        if go_internal("POST", "/api/agent/tenants/reload") is not None:
            return


# ---------- 租户 ----------

TENANT_COLUMNS = (
    "id, tenant_id, name, app_key, is_platform_admin, status, created_at, "
    "model_provider, model_provider_key, llm_provider_id, model_name, model_api_base, "
    "chat_input_cache_price, chat_input_price, chat_output_price, embedding_price, "
    "embedding_provider, embedding_provider_key, embedding_provider_id, "
    "embedding_model_name, embedding_api_base, "
    "max_concurrent_sessions, daily_message_limit, max_kb_docs, max_kb_size_mb"
)


def utc_day_start_local():
    """Go `time.Now().Truncate(24 * time.Hour)` 的等价物。

    注意 Truncate 是按「Unix 纪元起的绝对时间」取整，不是本地 00:00 ——
    容器 TZ=Asia/Shanghai 时实际得到**本地 08:00**。照抄，否则「今日消息数」
    在每天 0~8 点之间会与 Go 相差一整天。
    """
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                  microsecond=0)
    return midnight.astimezone().replace(tzinfo=None)


def tenant_usage_map():
    """对齐 Go service.fetchTenantUsage：MySQL 今日消息 → Agent 用量 → ws Hub 连接数。"""
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

    # 并发会话以 ws Hub 实时连接数为准（与 Go 侧配额校验同口径）
    conns = go_internal("GET", "/api/agent/ws/conns")
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
    }


def tenant_row_or_none(tenant_id):
    return one(f"SELECT {TENANT_COLUMNS} FROM tenants "
               f"WHERE tenant_id = %s AND deleted_at = 0 LIMIT 1", (tenant_id,))


def prewarm_tenant_async(tenant_id):
    """Go `go agent.PrewarmTenant(...)`：异步预热新租户 Agent（失败仅记日志）。"""
    def run():
        try:
            agent_call("POST", "/agent/tenants/prewarm",
                       body=json.dumps({"tenant_id": tenant_id}).encode(),
                       content_type="application/json", trace_id="prewarm")
        except Exception:                          # noqa: BLE001 - best effort
            pass
    threading.Thread(target=run, daemon=True).start()


def cleanup_tenant_async(tenant_id):
    """Go `go agent.AgentRequest(... /agent/tenant/cleanup ...)`：异步清理 Agent 侧数据。"""
    def run():
        try:
            agent_call("POST", "/agent/tenant/cleanup", body=None,
                       content_type="application/json", tenant_id=tenant_id)
        except Exception:                          # noqa: BLE001 - best effort
            pass
    threading.Thread(target=run, daemon=True).start()


@app.get("/api/admin/tenants")
def admin_tenant_list(name: str = "", app_key: str = "", status: str = "",
                      sort: str = "", admin: Admin = Depends(require_platform())):
    where, args = ["is_platform_admin = 0", "deleted_at = 0"], []
    if name:
        where.append("name LIKE %s")
        args.append("%" + name + "%")
    if app_key:
        where.append("app_key LIKE %s")
        args.append("%" + app_key + "%")
    # Go 只在 status 恰为 "0"/"1" 时过滤，"01"/"" 等一律视为未传
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


@app.get("/api/admin/tenants/{tid}")
def admin_tenant_detail(tid: str, admin: Admin = Depends(require_platform())):
    row = tenant_row_or_none(tid)
    if not row:
        soft(404, "租户不存在")
    return {"code": 0, "data": tenant_detail_json(row)}


@app.post("/api/admin/tenants")
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
    # Go 用 Unscoped 预查（唯一索引含软删行），命中直接给可读错误而不是 1062
    if one("SELECT id FROM tenants WHERE app_key = %s LIMIT 1", (app_key,)):
        soft(500, "app_key 已被使用（含已删除租户）")
    app_secret = secrets.token_hex(16)
    tenant_id = "t_" + app_key
    try:
        # 必须写全 GORM 会写入的零值列：tenants 表大量列默认值是 NULL（不是 0/空串），
        # 而 Go 的 Create 会把无 default tag 的字段全部按零值写进去。漏写任意一列都会出事：
        #   - deleted_at=NULL   → 新建租户立刻查不到（详情/配额/删除全 404）
        #   - created_at=NULL   → 详情 null、列表时间错乱
        #   - is_platform_admin=NULL → 列表的 `is_platform_admin = 0` 过滤把它剔掉
        # 这里逐列对齐 model.TenantRow 的字段与零值。
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


@app.put("/api/admin/tenants/{tid}")
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


@app.put("/api/admin/tenants/{tid}/quota")
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
    if not tenant_row_or_none(tid):
        soft(404, "租户不存在")
    try:
        write("UPDATE tenants SET max_concurrent_sessions = %s, daily_message_limit = %s, "
              "max_kb_docs = %s, max_kb_size_mb = %s WHERE tenant_id = %s",
              (values["max_concurrent_sessions"], values["daily_message_limit"],
               values["max_kb_docs"], values["max_kb_size_mb"], tid))
    except pymysql.MySQLError:
        soft(404, "租户不存在")
    reload_go_tenants()
    return {"code": 0, "msg": "租户配额已更新"}


@app.post("/api/admin/tenants/{tid}/reset-secret")
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


@app.post("/api/admin/tenants/{tid}/toggle")
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


@app.delete("/api/admin/tenants/{tid}")
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
            # messages 表没有 deleted_at（Go 的 model.Message 无 DeletedAt）→ 硬删除
            cur.execute("DELETE FROM messages WHERE tenant_id = %s", (tid,))
            cur.execute("UPDATE tenants SET deleted_at = %s WHERE tenant_id = %s",
                        (now, tid))
    except pymysql.MySQLError:
        soft(404, "租户不存在")
    reload_go_tenants()
    cleanup_tenant_async(tid)
    time.sleep(0.1)                                # 照抄 Go 的 100ms 等待
    return {"code": 0,
            "msg": "已删除租户 " + tid + "（含工单/消息/Prompt/知识库数据）"}


@app.get("/api/admin/tenants/{tid}/model-config")
def admin_tenant_model_config(tid: str, admin: Admin = Depends(require_platform())):
    result = get_business_model_config(tid)
    if result is None:
        soft(404, "租户不存在")
    return {"code": 0, "data": result}


@app.put("/api/admin/tenants/{tid}/model-config")
async def admin_tenant_model_config_update(tid: str, request: Request,
                                           admin: Admin = Depends(require_platform())):
    body = await body_json(request)
    if not isinstance(body, dict):
        soft(400, "EOF")
    # Go 走的是 service.UpdateModelConfig 的 GORM 错误：租户不存在时是 500 record not found，
    # 不是 404 —— 与同文件其它端点的 404 语义不同，这里必须照抄
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


@app.get("/api/admin/model-providers")
def admin_model_provider_list(admin: Admin = Depends(require_platform())):
    try:
        rows = many(f"SELECT {MODEL_PROVIDER_COLUMNS} FROM model_providers "
                    "ORDER BY sort_order ASC, id ASC")
    except pymysql.MySQLError as exc:
        soft(500, str(exc))
    return {"code": 0, "data": [model_provider_json(r) for r in rows]}


@app.get("/api/admin/available-model-providers")
def admin_available_model_providers(admin: Admin = Depends(current_admin),
                                    provider_type: str = Query(default="", alias="type")):
    """对齐 Go ListModelProvidersForTenant：只看启用项，type 过滤含 both。

    注意它在 Go 的 **admin 组**（不是 plat 组）：只要求登录，平台管理员与租户管理员
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


@app.post("/api/admin/model-providers")
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


@app.put("/api/admin/model-providers/{id}")
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


@app.delete("/api/admin/model-providers/{id}")
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

# 逐字对齐 Go repository.builtinMenus：
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
    """Go normalizePerm：空串/纯空白统一为 nil（唯一索引允许多个 NULL）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def is_duplicate_error(exc):
    return "Duplicate entry" in str(exc)


def menu_descendant_ids(root_id):
    """对齐 Go collectChildren：后序收集（子节点在前、根节点在后）。"""
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


@app.get("/api/admin/menus")
def admin_menu_list(admin: Admin = Depends(require_platform())):
    try:
        rows = many(f"SELECT {MENU_COLUMNS} FROM menus "
                    "ORDER BY group_name ASC, sort ASC, id ASC")
    except pymysql.MySQLError:
        deny(500, "查询菜单失败")
    return {"code": 0, "data": [menu_json(r) for r in rows]}


@app.post("/api/admin/menus")
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


@app.put("/api/admin/menus/{id}")
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


@app.delete("/api/admin/menus/{id}")
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


@app.post("/api/admin/menus/batch-update")
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


@app.post("/api/admin/menus/reset-builtin")
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

@app.get("/api/admin/prompts")
async def admin_prompts(request: Request,
                        admin: Admin = Depends(require_perm("prompt:view"))):
    return await proxy_prompt(request, "GET", "/agent/prompts", admin)


@app.get("/api/admin/prompts/active")
async def admin_prompts_active(request: Request,
                               admin: Admin = Depends(require_perm("prompt:view"))):
    return await proxy_prompt(request, "GET", "/agent/prompts/active", admin)


@app.post("/api/admin/prompts")
async def admin_prompts_create(request: Request,
                               admin: Admin = Depends(require_perm("prompt:add"))):
    return await proxy_prompt(request, "POST", "/agent/prompts", admin)


@app.post("/api/admin/prompts/{key}/activate")
async def admin_prompt_activate(key: str, request: Request,
                                admin: Admin = Depends(require_perm("prompt:edit"))):
    return await proxy_prompt(request, "POST", "/agent/prompts/" + key + "/activate",
                              admin)


@app.delete("/api/admin/prompts/{key}")
async def admin_prompt_delete(key: str, request: Request,
                              admin: Admin = Depends(require_perm("prompt:remove"))):
    return await proxy_prompt(request, "DELETE", "/agent/prompts/" + key, admin)


@app.post("/api/admin/prompts/{key}/versions")
async def admin_prompt_versions_create(key: str, request: Request,
                                      admin: Admin = Depends(require_perm("prompt:edit"))):
    return await proxy_prompt(request, "POST", "/agent/prompts/" + key + "/versions",
                              admin)


@app.get("/api/admin/prompts/{key}/versions")
async def admin_prompt_versions(key: str, request: Request,
                                admin: Admin = Depends(require_perm("prompt:view"))):
    return await proxy_prompt(request, "GET", "/agent/prompts/" + key + "/versions",
                              admin)


@app.post("/api/admin/prompts/{key}/versions/{vid}/rollback")
async def admin_prompt_rollback(key: str, vid: str, request: Request,
                                admin: Admin = Depends(require_perm("prompt:edit"))):
    return await proxy_prompt(
        request, "POST",
        "/agent/prompts/" + key + "/versions/" + vid + "/rollback", admin)


# ---------- 知识库（biz 组） ----------

@app.post("/api/admin/kb/upload")
async def admin_kb_upload(request: Request,
                          admin: Admin = Depends(require_perm("knowledge:add"))):
    return await proxy_kb_upload(request, admin)


@app.get("/api/admin/kb/documents")
async def admin_kb_documents(request: Request,
                             admin: Admin = Depends(require_perm("knowledge:view"))):
    return await proxy_to_agent(request, "GET", "/agent/kb/documents", admin.tenant_id)


@app.delete("/api/admin/kb/documents/{doc_id}")
async def admin_kb_document_delete(doc_id: str, request: Request,
                                   admin: Admin = Depends(require_perm("knowledge:remove"))):
    return await proxy_to_agent(request, "DELETE", "/agent/kb/documents/" + doc_id,
                                admin.tenant_id)


@app.post("/api/admin/kb/documents/{doc_id}/category")
async def admin_kb_document_category(doc_id: str, request: Request,
                                     admin: Admin = Depends(require_perm("knowledge:edit"))):
    return await proxy_to_agent(request, "POST",
                                "/agent/kb/documents/" + doc_id + "/category",
                                admin.tenant_id)


@app.post("/api/admin/kb/documents/{doc_id}/reindex")
async def admin_kb_document_reindex(doc_id: str, request: Request,
                                    admin: Admin = Depends(require_perm("knowledge:edit"))):
    return await proxy_to_agent(request, "POST",
                                "/agent/kb/documents/" + doc_id + "/reindex",
                                admin.tenant_id)


@app.post("/api/admin/kb/index")
async def admin_kb_index(request: Request,
                         admin: Admin = Depends(require_perm("knowledge:edit"))):
    return await proxy_to_agent(request, "POST", "/agent/kb/index", admin.tenant_id)


@app.get("/api/admin/kb/categories")
async def admin_kb_categories(request: Request,
                              admin: Admin = Depends(require_perm("knowledge:view"))):
    return await proxy_to_agent(request, "GET", "/agent/kb/categories", admin.tenant_id)


@app.post("/api/admin/kb/batch")
async def admin_kb_batch(request: Request,
                         admin: Admin = Depends(require_perm("knowledge:edit"))):
    return await proxy_to_agent(request, "POST", "/agent/kb/batch", admin.tenant_id)


# ---------- 平台设置（GET 挂 admin 组，PUT 挂 plat 组） ----------

@app.get("/api/admin/platform-settings")
async def admin_platform_settings(request: Request,
                                  admin: Admin = Depends(current_admin)):
    return await proxy_to_agent(request, "GET", "/agent/platform-settings",
                                admin.tenant_id)


@app.put("/api/admin/platform-settings")
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


@app.post("/api/admin/model-config/test")
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

@app.get("/api/agent/tenant-quota")
def agent_tenant_quota(request: Request, tenant_id: str = ""):
    internal_guard(request)
    if not TENANT_ID.fullmatch(tenant_id):
        soft(400, "缺少或无效的 tenant_id")
    row = one(
        "SELECT max_concurrent_sessions, daily_message_limit, max_kb_docs, "
        "max_kb_size_mb FROM tenants WHERE tenant_id = %s AND deleted_at = 0 "
        "LIMIT 1", (tenant_id,),
    )
    if not row:
        soft(404, "租户不存在")
    return {"code": 0, "data": {"tenant_id": tenant_id,
            **{key: int(row[key] or 0) for key in (
                "max_concurrent_sessions", "daily_message_limit",
                "max_kb_docs", "max_kb_size_mb")}}}


@app.get("/api/agent/model-config")
def agent_internal_model_config(request: Request, tenant_id: str = ""):
    internal_guard(request)
    if not TENANT_ID.fullmatch(tenant_id):
        soft(400, "tenant_id 不能为空")
    row = tenant_config_row(tenant_id)
    if not row:
        soft(404, "租户不存在")
    try:
        chat = resolve_chat_config(tenant_id)
        embedding = resolve_emb_config(tenant_id)
    except ValueError as exc:
        soft(404, str(exc))
    chat_own = bool(decrypt_api_key(row["model_api_key"])) and (
        int(row["llm_provider_id"] or 0) > 0 or
        (row["model_provider"] == "custom" and
         bool(row["model_name"] or row["model_api_base"])))
    emb_own = bool(decrypt_api_key(row["embedding_api_key"])) and (
        int(row["embedding_provider_id"] or 0) > 0 or
        (row["embedding_provider"] == "custom" and
         bool(row["embedding_model_name"] or row["embedding_api_base"])))
    prices = {key: float(row[key] or 0) for key in (
        "chat_input_cache_price", "chat_input_price", "chat_output_price",
        "embedding_price")}
    if tenant_id != "t_admin" and (not chat_own or not emb_own):
        platform = tenant_config_row("t_admin")
        if platform:
            for key in prices:
                if (key == "embedding_price" and not emb_own or
                    key != "embedding_price" and not chat_own):
                    prices[key] = float(platform[key] or 0)
    signature = "|".join((chat[0], chat[1], chat[3], chat[2],
                          embedding[0], embedding[1], embedding[3], embedding[2]))
    return {"code": 0, "data": {
        "tenant_id": tenant_id, "use_custom": chat_own,
        "model_provider": chat[0], "model_name": chat[1],
        "api_key": chat[3], "api_base": chat[2],
        **prices,
        "config_version": hashlib.sha256(signature.encode()).hexdigest()[:16],
        "embedding": {"use_custom": emb_own, "provider": embedding[0],
                      "model_name": embedding[1], "api_key": embedding[3],
                      "api_base": embedding[2]},
    }}


@app.get("/api/agent/session-summaries/pending")
def agent_summary_pending(request: Request, limit: int = 20):
    internal_guard(request)
    limit = limit if 1 <= limit <= 100 else 20
    threads = many(
        "SELECT m.thread_id, m.tenant_id, MAX(m.created_at) AS last_active "
        "FROM messages m LEFT JOIN session_summaries s ON "
        "s.thread_id = m.thread_id AND s.tenant_id = m.tenant_id "
        "WHERE s.thread_id IS NULL AND m.created_at < "
        "DATE_SUB(NOW(), INTERVAL 30 MINUTE) "
        "GROUP BY m.thread_id, m.tenant_id ORDER BY last_active DESC LIMIT %s",
        (limit,),
    )
    candidates = []
    for item in threads:
        messages = list(many(
            "SELECT role, content FROM messages WHERE thread_id = %s "
            "AND tenant_id = %s ORDER BY id DESC LIMIT 40",
            (item["thread_id"], item["tenant_id"]),
        ))
        messages.reverse()
        candidates.append({"thread_id": item["thread_id"],
                           "tenant_id": item["tenant_id"], "messages": messages})
    return {"code": 0, "data": candidates}


@app.post("/api/agent/session-summaries")
async def agent_summary_save(request: Request):
    internal_guard(request)
    body = await body_json(request)
    if not isinstance(body, dict) or not all(
        isinstance(body.get(key), str) and body[key]
        for key in ("thread_id", "tenant_id", "summary")
    ):
        soft(400, "thread_id/tenant_id/summary 不能为空")
    if (not TENANT_ID.fullmatch(body["tenant_id"]) or
        not body["thread_id"].startswith(body["tenant_id"] + ":")):
        deny(403, "会话不属于该租户")
    write(
        "INSERT INTO session_summaries (tenant_id, thread_id, summary) "
        "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE summary = VALUES(summary)",
        (body["tenant_id"], body["thread_id"], body["summary"]),
    )
    return {"code": 0, "msg": "会话摘要已保存"}
