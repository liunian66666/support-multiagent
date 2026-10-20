package auth

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strconv"
	"strings"
	"time"

	"customer_service/internal/config"
	"customer_service/internal/model"
	"customer_service/internal/pkg/cache"
	"customer_service/internal/pkg/crypto"
)

var randRead = rand.Read

// RuntimeTenant 运行时租户（来自 config.DB）
type RuntimeTenant struct {
	TenantID        string
	Name            string
	AppKey          string
	AppSecretHash   string
	IsPlatformAdmin bool
	Status          int
}

var (
	authEnabled bool
	authTTL     = 24 * time.Hour
	tenantByKey = map[string]RuntimeTenant{}
	TenantsList = []RuntimeTenant{}

	// Token 黑名单（主动登出）：Redis SETEX（TTL 自动过期，多实例全局一致，强制依赖）
	// Redis 查询失败时按"已拉黑"处理（fail-closed）：登出的 token 绝不允许复活
)

const tokenBlacklistKeyPrefix = "cs:tok:bl:"

// BlacklistToken 将 Token 加入黑名单（主动登出）
func BlacklistToken(token string) {
	ttl := 24 * time.Hour
	// 解析 token 的过期时间，作为黑名单条目的过期时间
	parts := strings.Split(token, ".")
	if len(parts) == 2 {
		if pl, err := base64.RawURLEncoding.DecodeString(parts[0]); err == nil {
			var p tokenPayload
			if json.Unmarshal(pl, &p) == nil {
				ttl = time.Until(time.Unix(p.Exp, 0))
				if ttl <= 0 {
					ttl = time.Minute
				}
			}
		}
	}
	if rdb := cache.Client(); rdb != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		rdb.Set(ctx, tokenBlacklistKeyPrefix+token, "1", ttl)
	}
}

// IsTokenBlacklisted 检查 Token 是否在黑名单中
// 强制 Redis：查询失败 → 按已拉黑处理（fail-closed，登出的 token 不允许复活）
func IsTokenBlacklisted(token string) bool {
	rdb := cache.Client()
	if rdb == nil {
		return true
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	n, err := rdb.Exists(ctx, tokenBlacklistKeyPrefix+token).Result()
	if err != nil {
		return true
	}
	return n > 0
}

// TokenBlacklistChecker 黑名单判定钩子（测试注入用，生产恒为 IsTokenBlacklisted）
// Redis 查询失败时按"已拉黑"处理（fail-closed）：登出的 token 绝不允许复活
var TokenBlacklistChecker = IsTokenBlacklisted

// CleanupBlacklist 定期清理过期的黑名单条目（Redis TTL 自动清理，保留空实现兼容调用）
func CleanupBlacklist() {}

// RefreshToken 刷新 Token：仅刷新管理/子账号令牌（Kind 为空）。
// 客户会话令牌在此被拒（VerifyTokenFull 类型边界），到期后由前端重新走客户登录，
// 绝不把客户令牌转换成管理令牌；子账号刷新后保留原 uid 与权限身份。
// 返回 (newToken, tenantID, uid, expiresIn, error)
func RefreshToken(oldToken string) (string, string, uint64, int64, error) {
	tenantID, uid, err := VerifyTokenFull(oldToken)
	if err != nil {
		return "", "", 0, 0, err
	}
	// 查找租户信息，签发新 Token
	for _, t := range TenantsList {
		if t.TenantID == tenantID {
			p := tokenPayload{TenantID: t.TenantID, Exp: time.Now().Add(authTTL).Unix(), Nonce: generateNonce(), UID: uid}
			pl, _ := json.Marshal(p)
			newToken := base64.RawURLEncoding.EncodeToString(pl) + "." + signPayload(t.AppSecretHash, pl)
			// 旧 Token 加入黑名单，防止重复使用
			BlacklistToken(oldToken)
			return newToken, t.TenantID, uid, int64(authTTL.Seconds()), nil
		}
	}
	return "", "", 0, 0, errors.New("租户不存在")
}

// InitAuth 初始化鉴权（enabled=false 演示模式全部放行）
func InitAuth(enabled bool, ttl string, _ []model.Tenant) {
	authEnabled = enabled
	if ttl != "" {
		if d, err := time.ParseDuration(ttl); err == nil {
			authTTL = d
		}
	}
	// DB 未初始化（纯单元测试）时保留调用方预设的 TenantsList，不 panic
	if config.DB != nil {
		if err := RefreshTenants(); err != nil {
			panic("load tenants from db: " + err.Error())
		}
	}
}

// RefreshTenants 从 config.DB 重新加载全部租户（租户管理接口增删改后调用）
func RefreshTenants() error {
	var rows []model.TenantRow
	if err := config.DB.Model(&model.TenantRow{}).Order("id ASC").Find(&rows).Error; err != nil {
		return err
	}
	tenantByKey = map[string]RuntimeTenant{}
	TenantsList = []RuntimeTenant{}
	for _, r := range rows {
		t := RuntimeTenant{
			TenantID:        r.TenantID,
			Name:            r.Name,
			AppKey:          r.AppKey,
			AppSecretHash:   r.AppSecretHash,
			IsPlatformAdmin: r.IsPlatformAdmin,
			Status:          r.Status,
		}
		tenantByKey[r.AppKey] = t
		TenantsList = append(TenantsList, t)
	}
	return nil
}

// AuthEnabled 是否启用鉴权
func AuthEnabled() bool { return authEnabled }

// GenSecret 生成随机 app_secret（16 字节 hex）
func GenSecret() string {
	b := make([]byte, 16)
	if _, err := randRead(b); err != nil {
		return time.Now().Format("20060102150405") + "cs"
	}
	return hex.EncodeToString(b)
}

// GenAppKey 生成随机 app_key（tk_ + 8 字节 hex）
func GenAppKey() string {
	b := make([]byte, 8)
	if _, err := randRead(b); err != nil {
		return "tk_" + time.Now().Format("150405")
	}
	return "tk_" + hex.EncodeToString(b)
}

type tokenPayload struct {
	TenantID string `json:"tid"`
	Exp      int64  `json:"exp"`
	Nonce    string `json:"nonce,omitempty"` // 随机数，确保每次签发的 Token 都不同
	UID      uint64 `json:"uid,omitempty"`   // RBAC 子账号用户 ID（app_key 登录为空）
	// Kind：令牌类型。TokenKindCustomer=客户会话令牌（演示客户登录签发），
	// 空=管理员/子账号令牌。客户会话令牌额外携带 CustomerID 与 SessionID，
	// 身份以服务端 customer_sessions 记录为准。
	Kind       string `json:"kind,omitempty"`
	CustomerID string `json:"cid,omitempty"`  // 稳定客户标识（客户会话令牌）
	SessionID  string `json:"sess,omitempty"` // 关联 customer_sessions.token，用于查库确认会话有效
}

// TokenKindCustomer 客户会话令牌类型标识。
// 管理端验签（VerifyTokenFull）对任何非空 Kind 一律拒绝，客户验签
// （VerifyCustomerToken）只接受本类型——两个方向都不做跨类型兼容。
const TokenKindCustomer = "customer"

// 客户会话令牌 TTL：短于管理员令牌（2 小时，到期后前端重新登录）
const customerTokenTTL = 2 * time.Hour

// IssueCustomerToken 签发客户会话令牌（演示客户登录）。
// 租户校验通过后由调用方生成随机 session token 传入，随身份信息一起写入 payload 签名。
// 调用方负责把 (tenantID, customerID, sessionToken, expiresAt) 落库到 customer_sessions。
func IssueCustomerToken(tenantID, customerID, sessionToken string) (token string, expiresAt time.Time, err error) {
	t, ok := TenantInfo(tenantID)
	if !ok {
		return "", time.Time{}, errors.New("租户不存在")
	}
	if t.Status != 1 {
		return "", time.Time{}, errors.New("租户已停用")
	}
	expiresAt = time.Now().Add(customerTokenTTL)
	p := tokenPayload{
		TenantID:   t.TenantID,
		Exp:        expiresAt.Unix(),
		Nonce:      generateNonce(),
		Kind:       TokenKindCustomer,
		CustomerID: customerID,
		SessionID:  sessionToken,
	}
	pl, _ := json.Marshal(p)
	return base64.RawURLEncoding.EncodeToString(pl) + "." + signPayload(t.AppSecretHash, pl),
		expiresAt, nil
}

// CustomerTokenClaims 客户会话令牌解析结果
type CustomerTokenClaims struct {
	TenantID   string
	CustomerID string
	SessionID  string
}

// VerifyCustomerToken 校验客户会话令牌：底层共用校验 + 类型必须是 kind=customer。
// 纯验签不查库；调用方还需确认 customer_sessions 中 SessionID 对应记录未过期（fail-closed）。
// 管理/子账号令牌（空 Kind）即使签名有效也一律拒绝，防止管理令牌冒用客户会话路由。
func VerifyCustomerToken(token string) (CustomerTokenClaims, error) {
	_, p, err := verifyTokenCommon(token)
	if err != nil {
		return CustomerTokenClaims{}, err
	}
	if p.Kind != TokenKindCustomer {
		return CustomerTokenClaims{}, errors.New("不是客户会话令牌")
	}
	if p.CustomerID == "" || p.SessionID == "" {
		return CustomerTokenClaims{}, errors.New("客户会话令牌缺少身份信息")
	}
	return CustomerTokenClaims{TenantID: p.TenantID, CustomerID: p.CustomerID, SessionID: p.SessionID}, nil
}

func signPayload(secretHash string, pl []byte) string {
	m := hmac.New(sha256.New, []byte(secretHash))
	m.Write(pl)
	return base64.RawURLEncoding.EncodeToString(m.Sum(nil))
}

// generateNonce 生成随机 Nonce 字符串（16字节 hex）
func generateNonce() string {
	b := make([]byte, 16)
	if _, err := randRead(b); err != nil {
		// 随机数生成失败时用时间戳兜底
		return strconv.FormatInt(time.Now().UnixNano(), 16)
	}
	return hex.EncodeToString(b)
}

// IssueToken 用 app_key + app_secret 换取访问令牌（secret 哈希比对）
func IssueToken(appKey, appSecret string) (token, tenantID string, expiresIn int64, err error) {
	t, ok := tenantByKey[appKey]
	if !ok {
		return "", "", 0, errors.New("无效的 app_key 或 app_secret")
	}
	if t.Status != 1 || t.AppSecretHash != crypto.HashSecret(appSecret) {
		return "", "", 0, errors.New("无效的 app_key 或 app_secret")
	}
	p := tokenPayload{TenantID: t.TenantID, Exp: time.Now().Add(authTTL).Unix(), Nonce: generateNonce()}
	pl, _ := json.Marshal(p)
	return base64.RawURLEncoding.EncodeToString(pl) + "." + signPayload(t.AppSecretHash, pl),
		t.TenantID, int64(authTTL.Seconds()), nil
}

// IssueUserToken 用用户名密码校验通过后，为租户内子账号签发访问令牌（token 携带 uid）
func IssueUserToken(tenantID string, userID uint64) (token string, expiresIn int64, err error) {
	t, ok := TenantInfo(tenantID)
	if !ok {
		return "", 0, errors.New("租户不存在")
	}
	if t.Status != 1 {
		return "", 0, errors.New("租户已停用")
	}
	p := tokenPayload{TenantID: t.TenantID, Exp: time.Now().Add(authTTL).Unix(), Nonce: generateNonce(), UID: userID}
	pl, _ := json.Marshal(p)
	return base64.RawURLEncoding.EncodeToString(pl) + "." + signPayload(t.AppSecretHash, pl),
		int64(authTTL.Seconds()), nil
}

// verifyTokenCommon 令牌底层共用校验：黑名单 → 格式 → 载荷 → 过期 → 签名。
// 只回答"是不是本平台签发的、有没有过期/被吊销"，不判定令牌类型；
// 类型边界由两个入口各自强制：管理端只认空 Kind，客户入口只认 kind=customer。
func verifyTokenCommon(token string) (string, tokenPayload, error) {
	// 检查 Token 是否在黑名单中（主动登出）
	if TokenBlacklistChecker(token) {
		return "", tokenPayload{}, errors.New("token 已失效（已登出）")
	}
	parts := strings.Split(token, ".")
	if len(parts) != 2 {
		return "", tokenPayload{}, errors.New("token 格式错误")
	}
	pl, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return "", tokenPayload{}, errors.New("token 解码失败")
	}
	var p tokenPayload
	if err := json.Unmarshal(pl, &p); err != nil {
		return "", tokenPayload{}, errors.New("token 载荷无效")
	}
	if p.Exp < time.Now().Unix() {
		return "", tokenPayload{}, errors.New("token 已过期")
	}
	for _, t := range TenantsList {
		if t.TenantID == p.TenantID {
			if !hmac.Equal([]byte(signPayload(t.AppSecretHash, pl)), []byte(parts[1])) {
				return "", tokenPayload{}, errors.New("token 签名无效")
			}
			return p.TenantID, p, nil
		}
	}
	return "", tokenPayload{}, errors.New("租户不存在")
}

// VerifyTokenFull 管理端验签：返回租户 ID 与子账号用户 ID（uid=0 表示 app_key 管理员）。
//
// 类型边界（P0）：只接受管理/子账号令牌（Kind 为空）。客户会话令牌与任何未知类型
// 一律拒绝，绝不做"空 kind 兼容"猜测——否则客户令牌会以 uid=0 冒充管理员，
// 直接进入管理接口，或经 /api/auth/refresh 刷新成管理令牌。
// 管理端、刷新接口、通用租户校验全部经过本函数，是唯一的类型 choke point。
func VerifyTokenFull(token string) (string, uint64, error) {
	tid, p, err := verifyTokenCommon(token)
	if err != nil {
		return "", 0, err
	}
	if p.Kind == TokenKindCustomer {
		return "", 0, errors.New("客户会话令牌不可用于管理端")
	}
	if p.Kind != "" {
		return "", 0, errors.New("未知令牌类型，已拒绝")
	}
	return tid, p.UID, nil
}

// VerifyToken 校验令牌，返回租户 ID（兼容既有调用，忽略 uid）
func VerifyToken(token string) (string, error) {
	tid, _, err := VerifyTokenFull(token)
	return tid, err
}

// TenantInfo 当前租户运行时信息（管理端上下文）
func TenantInfo(tenantID string) (RuntimeTenant, bool) {
	for _, t := range TenantsList {
		if t.TenantID == tenantID {
			return t, true
		}
	}
	return RuntimeTenant{}, false
}

// UserIDBelongsToTenant 校验 user_id 是否属于指定租户（未开启鉴权时放行）
func UserIDBelongsToTenant(tenantID, userID string) bool {
	if !authEnabled {
		return true
	}
	return tenantID != "" && strings.HasPrefix(userID, tenantID+":")
}

// TenantFromUserID 从 user_id 提取租户 ID
func TenantFromUserID(userID string) string {
	if i := strings.Index(userID, ":"); i > 0 {
		return userID[:i]
	}
	return ""
}

// TenantFromThreadID 从 thread_id 提取租户 ID（thread_id 格式与 user_id 一致：{tenant_id}:{uid}）
func TenantFromThreadID(threadID string) string {
	return TenantFromUserID(threadID)
}

// AllTenants 返回全部运行时租户（含禁用）
func AllTenants() []RuntimeTenant {
	return TenantsList
}
