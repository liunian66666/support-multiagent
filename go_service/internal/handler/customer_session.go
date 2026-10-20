package handler

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"customer_service/internal/config"
	"customer_service/internal/model"
	"customer_service/internal/pkg/auth"
	"customer_service/internal/pkg/cache"
	"customer_service/internal/pkg/crypto"
	"customer_service/internal/repository"

	"github.com/gin-gonic/gin"
)

var checkMerchantPreviewLoginAllowed = auth.CheckLoginAllowed
var checkDemoCustomerLoginAllowed = auth.CheckLoginAllowed

var checkGuestAdmission = func(ip string) bool {
	rdb := cache.Client()
	if rdb == nil {
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	key := fmt.Sprintf("cs:guest:minute:%s:%d", ip, time.Now().Unix()/60)
	n, err := rdb.Incr(ctx, key).Result()
	if err != nil {
		return false
	}
	if n == 1 && rdb.Expire(ctx, key, 2*time.Minute).Err() != nil {
		return false
	}
	return n <= 12
}

// GuestTokenHandler 为本地演示签发免登录访客会话；租户由服务端配置决定。
// 访客只可咨询通用问题和转人工，订单适配器按 IsGuest 拒绝。
func GuestTokenHandler(c *gin.Context) {
	if !config.GlobalConfig.Auth.DemoGuestChat {
		c.JSON(http.StatusForbidden, gin.H{"code": 403, "msg": "访客咨询未启用"})
		return
	}
	tenantID := config.GlobalConfig.Auth.DemoGuestTenantID
	info, ok := auth.TenantInfo(tenantID)
	if tenantID == "" || !ok || info.Status != 1 || info.IsPlatformAdmin {
		c.JSON(http.StatusServiceUnavailable, gin.H{"code": 503, "msg": "访客咨询租户未就绪"})
		return
	}
	if !checkGuestAdmission(c.ClientIP()) {
		c.JSON(http.StatusTooManyRequests, gin.H{"code": 429, "msg": "访问过于频繁，请稍后再试"})
		return
	}
	customerID := tenantID + ":guest_" + randHex(8)
	threadID := customerID + ":" + randHex(8)
	sessionToken := randHex(16)
	token, expiresAt, err := auth.IssueCustomerToken(tenantID, customerID, sessionToken)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"code": 500, "msg": "访客会话签发失败"})
		return
	}
	sess := model.CustomerSession{TenantID: tenantID, CustomerID: customerID,
		ThreadID: threadID, Token: sessionToken, IsGuest: true, ExpiresAt: expiresAt}
	if err := repository.CreateCustomerSession(&sess); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"code": 500, "msg": "访客会话创建失败"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"code": 0, "data": gin.H{
		"token": token, "tenant_id": tenantID, "customer_id": customerID,
		"thread_id": threadID, "expires_in": int64(time.Until(expiresAt).Seconds()), "guest": true,
	}})
}

// MerchantPreviewTokenHandler 将旧版商家链接中的管理密钥仅用于一次身份校验，
// 返回客户权限的预览令牌。管理令牌从不进入浏览器聊天流程。
func MerchantPreviewTokenHandler(c *gin.Context) {
	if !config.GlobalConfig.Auth.MerchantPreviewLogin {
		c.JSON(http.StatusForbidden, gin.H{"code": 403, "msg": "商家预览入口未启用"})
		return
	}
	var b struct {
		AppKey    string `json:"app_key"`
		AppSecret string `json:"app_secret"`
	}
	if err := c.ShouldBindJSON(&b); err != nil || b.AppKey == "" || b.AppSecret == "" {
		c.JSON(http.StatusBadRequest, gin.H{"code": 400, "msg": "参数错误"})
		return
	}
	limiterKey := c.ClientIP() + ":" + b.AppKey
	if ok, _ := checkMerchantPreviewLoginAllowed(limiterKey); !ok {
		c.JSON(http.StatusTooManyRequests, gin.H{"code": 429, "msg": "尝试过于频繁，请稍后再试"})
		return
	}
	tenant, ok := tenantByAppKey(b.AppKey)
	providedHash := crypto.HashSecret(b.AppSecret)
	if !ok || tenant.Status != 1 || subtle.ConstantTimeCompare([]byte(providedHash), []byte(tenant.AppSecretHash)) != 1 {
		auth.RecordLoginFail(limiterKey)
		c.JSON(http.StatusUnauthorized, gin.H{"code": 401, "msg": "无效的商家凭证"})
		return
	}
	auth.ClearLoginFails(limiterKey)

	// 每次签发独立的预览客户与会话；预览身份不能匹配真实客户订单。
	customerID := tenant.TenantID + ":merchant_preview_" + randHex(8)
	threadID := customerID + ":" + randHex(8)
	sessionToken := randHex(16)
	token, expiresAt, err := auth.IssueCustomerToken(tenant.TenantID, customerID, sessionToken)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"code": 500, "msg": "预览会话签发失败"})
		return
	}
	sess := model.CustomerSession{
		TenantID: tenant.TenantID, CustomerID: customerID, ThreadID: threadID,
		Token: sessionToken, IsPreview: true, ExpiresAt: expiresAt,
	}
	if err := repository.CreateCustomerSession(&sess); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"code": 500, "msg": "预览会话创建失败"})
		return
	}
	repository.WriteAuditLog(tenant.TenantID, customerID, "merchant_preview_login", "auth", "customer", "商家预览客服", c.ClientIP())
	c.JSON(http.StatusOK, gin.H{"code": 0, "data": gin.H{
		"token": token, "tenant_id": tenant.TenantID, "customer_id": customerID,
		"thread_id": threadID, "expires_in": int64(time.Until(expiresAt).Seconds()), "preview": true,
	}})
}

// ============ 演示客户登录：POST /api/auth/customer-token ============
//
// 默认关闭（config.Auth.DemoCustomerLogin=false）；仅演示环境显式开启。
//
// 凭证边界（与管理端完全分离）：
//   - 只接受 auth.demo_customers 里的演示客户凭证（app_key+app_secret）
//   - 管理端租户凭证（可换取管理员令牌的 app_key/app_secret）一律拒绝
//   - customer_id 限该凭证的白名单，只能选择虚构客户（buyer1/buyer2…）
//
// 服务端签发客户会话令牌并落库 customer_sessions（tenant_id/customer_id/
// thread_id/过期时间）。前端只持有返回的 token；聊天/历史/WS/订单查询的身份
// 全部由服务端从会话记录解析，不再信任 URL/body 传入的 user_id。
//
// ⚠️ 本接口只适合严格隔离的虚构数据演示：它证明的是"服务端签发了这个选择"，
// 不能证明访问者就是 buyer1 本人。真实接入时关闭本开关，改为校验商家现有
// 登录凭证后再签发同样结构的会话。

// CustomerTokenBody 演示客户登录请求体
type CustomerTokenBody struct {
	AppKey     string `json:"app_key"`     // 演示客户凭证 app_key（与管理端凭证分离）
	AppSecret  string `json:"app_secret"`  // 演示客户凭证 secret
	CustomerID string `json:"customer_id"` // 虚构客户选择（限白名单），缺省白名单首位
	Encrypted  bool   `json:"encrypted"`   // app_secret RSA 加密传输（与管理员登录一致）
}

// CustomerTokenHandler 演示客户登录：签发客户会话令牌
func CustomerTokenHandler(c *gin.Context) {
	if !config.GlobalConfig.Auth.DemoCustomerLogin {
		c.JSON(http.StatusForbidden, gin.H{"code": 403, "msg": "演示客户登录未启用"})
		return
	}
	var b CustomerTokenBody
	if err := c.ShouldBindJSON(&b); err != nil || b.AppKey == "" || b.AppSecret == "" {
		c.JSON(http.StatusBadRequest, gin.H{"code": 400, "msg": "参数错误"})
		return
	}
	limiterKey := c.ClientIP() + ":" + b.AppKey
	if ok, _ := checkDemoCustomerLoginAllowed(limiterKey); !ok {
		c.JSON(http.StatusTooManyRequests, gin.H{"code": 429, "msg": "尝试过于频繁，请稍后再试"})
		return
	}

	secret := b.AppSecret
	if b.Encrypted {
		key, err := crypto.GetRSAKey()
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"code": 500, "msg": "RSA 密钥未就绪"})
			return
		}
		plain, err := crypto.DecryptRSAOAEP(key, b.AppSecret)
		if err != nil {
			auth.RecordLoginFail(limiterKey)
			c.JSON(http.StatusUnauthorized, gin.H{"code": 401, "msg": "app_secret 解密失败"})
			return
		}
		secret = plain
	}

	// 演示客户凭证校验（与管理端 app_key/app_secret 完全分离）：
	// 1) 命中管理端租户凭证 → 明确拒绝（管理密钥不得用于客户登录）
	// 2) 未配置/不匹配演示客户凭证 → 拒绝
	entry, ok := findDemoCustomer(b.AppKey)
	if !ok {
		auth.RecordLoginFail(limiterKey)
		if _, isAdminKey := tenantByAppKey(b.AppKey); isAdminKey {
			c.JSON(http.StatusUnauthorized, gin.H{"code": 401, "msg": "管理端凭证不可用于客户登录，请使用演示客户凭证"})
			return
		}
		c.JSON(http.StatusUnauthorized, gin.H{"code": 401, "msg": "无效的演示客户凭证"})
		return
	}
	if entry.AppSecret == "" || crypto.HashSecret(secret) != crypto.HashSecret(entry.AppSecret) {
		auth.RecordLoginFail(limiterKey)
		c.JSON(http.StatusUnauthorized, gin.H{"code": 401, "msg": "无效的演示客户凭证"})
		return
	}
	auth.ClearLoginFails(limiterKey)

	// 租户必须存在且启用
	info, ok := auth.TenantInfo(entry.TenantID)
	if !ok || info.Status != 1 {
		c.JSON(http.StatusUnauthorized, gin.H{"code": 401, "msg": "租户不存在或已停用"})
		return
	}
	tenantID := entry.TenantID

	// customer_id：限白名单内的虚构客户；裸名补租户前缀；跨租户前缀/非白名单拒绝
	customerID, ok := normalizeDemoCustomerID(tenantID, b.CustomerID, entry.Customers)
	if !ok {
		c.JSON(http.StatusBadRequest, gin.H{"code": 400, "msg": "customer_id 不在演示客户白名单内"})
		return
	}

	// thread_id 由服务端生成：只表示一段对话（客户 + 时刻 + 随机后缀，防同秒碰撞）
	threadID := fmt.Sprintf("%s:%s_%s", customerID, time.Now().Format("20060102150405"), randHex(4))

	// 随机 session token（16 字节 hex），与签发的客户令牌一一对应，可查库吊销
	sessionToken := randHex(16)
	if sessionToken == "" {
		c.JSON(http.StatusInternalServerError, gin.H{"code": 500, "msg": "会话生成失败"})
		return
	}

	custTok, expiresAt, err := auth.IssueCustomerToken(tenantID, customerID, sessionToken)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"code": 500, "msg": err.Error()})
		return
	}

	sess := model.CustomerSession{
		TenantID:   tenantID,
		CustomerID: customerID,
		ThreadID:   threadID,
		Token:      sessionToken,
		ExpiresAt:  expiresAt,
	}
	if err := repository.CreateCustomerSession(&sess); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"code": 500, "msg": "会话创建失败"})
		return
	}

	repository.WriteAuditLog(tenantID, customerID, "customer_login", "auth", "customer", "演示客户登录", c.ClientIP())

	c.JSON(http.StatusOK, gin.H{"code": 0, "data": gin.H{
		"token":       custTok,
		"tenant_id":   tenantID,
		"customer_id": customerID,
		"thread_id":   threadID,
		"expires_in":  int64(time.Until(expiresAt).Seconds()),
	}})
}

// findDemoCustomer 按 app_key 查找演示客户凭证
func findDemoCustomer(appKey string) (config.DemoCustomer, bool) {
	for _, e := range config.GlobalConfig.Auth.DemoCustomers {
		if e.AppKey != "" && e.AppKey == appKey {
			return e, true
		}
	}
	return config.DemoCustomer{}, false
}

// tenantByAppKey 判断 app_key 是否为管理端租户凭证（可换取管理员令牌）
func tenantByAppKey(appKey string) (auth.RuntimeTenant, bool) {
	for _, t := range auth.AllTenants() {
		if t.AppKey != "" && t.AppKey == appKey {
			return t, true
		}
	}
	return auth.RuntimeTenant{}, false
}

// normalizeDemoCustomerID 规范演示客户标识：必须落在签发租户内（{tenant_id}:{customer}），
// 且 customer 部分必须在该凭证的白名单内；白名单为空时一律拒绝（防止自选任意身份）。
func normalizeDemoCustomerID(tenantID, raw string, allow []string) (string, bool) {
	if len(allow) == 0 {
		return "", false
	}
	inAllow := func(name string) bool {
		for _, a := range allow {
			if a == name {
				return true
			}
		}
		return false
	}
	s := strings.TrimSpace(raw)
	if s == "" {
		s = allow[0] // 缺省白名单首位
	}
	if strings.HasPrefix(s, tenantID+":") {
		name := s[len(tenantID)+1:]
		if name == "" || !inAllow(name) {
			return "", false
		}
		return s, true
	}
	if strings.Contains(s, ":") {
		return "", false // 显式带其他租户前缀 → 拒绝
	}
	if !inAllow(s) {
		return "", false
	}
	return tenantID + ":" + s, true
}

// randHex 生成 n 字节随机 hex（随机源不可用时退化为纳秒时间戳，保证不重复）
func randHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return strconv.FormatInt(time.Now().UnixNano(), 16)
	}
	return hex.EncodeToString(b)
}
