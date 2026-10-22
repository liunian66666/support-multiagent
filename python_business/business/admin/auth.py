"""Management login, token verification and write audit."""

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass

import pymysql
import redis
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import APIRouter, Depends, Header, Request

from business.core.constants import TENANT_ID
from business.core.errors import deny, soft
from business.core.requests import body_json
from business.infra.storage import cache, many, one, write
from business.customer.routes import signed_token

router = APIRouter()

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
    admin = admin_for_token(authorization.removeprefix("Bearer ").strip())
    request.state.admin = admin
    return admin


def admin_for_token(token):
    """Shared HTTP and WebSocket admin identity verification."""
    try:
        if cache().exists(BLACKLIST_PREFIX + token):
            deny(401, "token 已失效（已登出）")
    except redis.RedisError:
        deny(503, "会话校验暂时不可用")
    claims = decode_admin_token(token)
    row = admin_tenant(claims["tid"])
    admin = Admin(claims["tid"], int(claims.get("uid") or 0),
                  bool(row["is_platform_admin"]))
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


@router.get("/api/auth/public-key")
def auth_public_key():
    try:
        pem = rsa_public_pem()
    except Exception:
        deny(500, "RSA 密钥未就绪")
    return {"code": 0, "data": {"public_key": pem}}


@router.post("/api/auth/token")
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


@router.post("/api/auth/user-token")
async def auth_user_token(request: Request):
    """对齐 Go handler.UserTokenHandler：子账号（uid>0）登录。"""
    from business.admin.rbac import (ensure_tenant_roles, hash_user_password,
                                     get_user_roles, is_full_perms)
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


@router.post("/api/auth/refresh")
def auth_refresh(request: Request):
    """
对齐 Go handler.RefreshTokenHandler：只刷新管理/子账号令牌（kind 缺省）。

    客户会话令牌在此被拒（Go 的 VerifyTokenFull 类型边界），到期后由前端重新走
    客户登录，绝不转换令牌类型；子账号刷新后保留原 uid 与权限身份。
    """
    from business.admin.rbac import load_user, get_user_roles, is_full_perms

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


@router.put("/api/auth/password")
async def auth_password(request: Request,
                        admin: Admin = Depends(current_admin)):
    """对齐 Go handler.ChangePasswordHandler：修改自己的登录密钥（app_secret）。

    路由在 Go 侧挂 `middleware.AdminAuth()`——**中间件先于 handler 执行**，
    因此「无令牌 + 非法请求体」返回的是 401 而不是 400，这里用 Depends 复刻同一顺序。
    旧密钥支持 RSA 加密传输；更新后旧令牌因签名密钥变化双端同时失效。
    """
    from business.admin.platform import reload_go_tenants

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


@router.get("/api/auth/tenants")
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


