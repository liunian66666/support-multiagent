package middleware

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	"customer_service/internal/model"
	"customer_service/internal/pkg/auth"
	"customer_service/internal/pkg/crypto"
	"customer_service/internal/pkg/permission"
	"customer_service/internal/repository"

	"github.com/gin-gonic/gin"
)

// ============ 令牌类型边界：经过真实路由 + 中间件链的验收 ============
//
// 覆盖复核要求：
//   - 客户令牌访问真实管理路由被拒
//   - 管理/子账号令牌访问客户会话路由被拒
//   - 未知类型令牌两个入口都被拒
//   - 客户令牌在服务端会话有效时才放行，身份以会话记录为准

const (
	testDemoTenant  = "t_demo"
	testAdminTenant = "t_admin"
	testDemoSecret  = "demo_secret"
	testAdminSecret = "admin_secret"
)

// setupBoundaryTenants 预设测试租户表（无 DB 依赖，InitAuth 的 nil-DB 分支保留该表）
func setupBoundaryTenants(t *testing.T) {
	t.Helper()
	auth.TokenBlacklistChecker = func(string) bool { return false }
	auth.TenantsList = []auth.RuntimeTenant{
		{TenantID: testDemoTenant, Name: "演示租户", AppKey: "demo_key",
			AppSecretHash: crypto.HashSecret(testDemoSecret), Status: 1},
		{TenantID: testAdminTenant, Name: "平台管理员", AppKey: "admin_key",
			AppSecretHash: crypto.HashSecret(testAdminSecret), Status: 1, IsPlatformAdmin: true},
	}
	auth.InitAuth(true, "2h", nil)
}

// echoCtx 回显中间件注入的身份（uid 走 UserIDOf，客户身份走 CustomerOf/ThreadOf）
func echoCtx(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{
		"code": 0,
		"data": gin.H{
			"tenant_id":   TenantOf(c),
			"uid":         UserIDOf(c),
			"customer_id": CustomerOf(c),
			"thread_id":   ThreadOf(c),
		},
	})
}

// newBoundaryRouter 搭建与生产一致的真实路由/中间件链
func newBoundaryRouter() *gin.Engine {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	// 管理路由：AdminAuth + RBAC 权限（uid=0 隐式管理员放行）
	r.GET("/api/admin/token-usage", AdminAuth(), RequirePerm(permission.UsageView), echoCtx)
	// 平台管理员专属路由
	r.GET("/api/admin/tenants", AdminAuth(), PlatformAdminOnly(), echoCtx)
	// 用户侧路由：UserAuth（客户会话令牌）
	r.GET("/ws/chat", UserAuth(), echoCtx)
	r.GET("/api/user/messages", UserAuth(), echoCtx)
	return r
}

func doReq(t *testing.T, r *gin.Engine, method, path, token string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, nil)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	out := httptest.NewRecorder()
	r.ServeHTTP(out, req)
	return out
}

// issueTokens 签发三类令牌：app_key 管理员(uid=0) / 子账号(uid=42) / 客户会话
func issueTokens(t *testing.T) (adminTok, subTok, custTok string) {
	t.Helper()
	setupBoundaryTenants(t)
	var err error
	// IssueUserToken(tenant, 0) 即 uid=0 的管理员令牌（与 app_key 登录等价，
	// 均走 TenantInfo/TenantsList，无需 DB 的 tenantByKey）
	if adminTok, _, err = auth.IssueUserToken(testDemoTenant, 0); err != nil {
		t.Fatalf("IssueUserToken(admin): %v", err)
	}
	if subTok, _, err = auth.IssueUserToken(testDemoTenant, 42); err != nil {
		t.Fatalf("IssueUserToken: %v", err)
	}
	if custTok, _, err = auth.IssueCustomerToken(testDemoTenant, testDemoTenant+":buyer1", "sess-boundary-1"); err != nil {
		t.Fatalf("IssueCustomerToken: %v", err)
	}
	return
}

// TestAdminRouteRejectsCustomerToken 客户令牌访问真实管理路由 → 401
// （提权路径 1：直接进入管理接口）
func TestAdminRouteRejectsCustomerToken(t *testing.T) {
	adminTok, _, custTok := issueTokens(t)
	r := newBoundaryRouter()

	if out := doReq(t, r, http.MethodGet, "/api/admin/token-usage", custTok); out.Code != http.StatusUnauthorized {
		t.Fatalf("客户令牌应被管理路由拒绝: status=%d body=%s", out.Code, out.Body.String())
	}
	// 平台管理员专属路由同样拒绝
	if out := doReq(t, r, http.MethodGet, "/api/admin/tenants", custTok); out.Code != http.StatusUnauthorized {
		t.Fatalf("客户令牌应被平台路由拒绝: status=%d body=%s", out.Code, out.Body.String())
	}
	// 对照：管理员令牌可过
	if out := doReq(t, r, http.MethodGet, "/api/admin/token-usage", adminTok); out.Code != http.StatusOK {
		t.Fatalf("管理员令牌应放行: status=%d body=%s", out.Code, out.Body.String())
	}
}

// TestUserRouteRejectsAdminAndSubTokens 管理/子账号令牌访问客户会话路由 → 401
// （防止管理令牌反向冒用客户会话路由）
func TestUserRouteRejectsAdminAndSubTokens(t *testing.T) {
	adminTok, subTok, _ := issueTokens(t)
	r := newBoundaryRouter()

	for _, tok := range []string{adminTok, subTok} {
		for _, path := range []string{"/ws/chat", "/api/user/messages"} {
			if out := doReq(t, r, http.MethodGet, path, tok); out.Code != http.StatusUnauthorized {
				t.Fatalf("管理/子账号令牌不应进入客户路由 %s: status=%d", path, out.Code)
			}
		}
	}
}

// TestUnknownKindRejectedBothEntries 未知类型令牌：管理端与客户入口都拒绝
func TestUnknownKindRejectedBothEntries(t *testing.T) {
	_, _, _ = issueTokens(t)
	// 手工构造一个签名有效但 kind 未知的令牌
	pl := []byte(`{"tid":"t_demo","exp":` + strconv.FormatInt(time.Now().Add(time.Hour).Unix(), 10) + `,"nonce":"n","kind":"weird_kind"}`)
	unknownTok := encodeSigned(t, testDemoSecret, pl)

	r := newBoundaryRouter()
	if out := doReq(t, r, http.MethodGet, "/api/admin/token-usage", unknownTok); out.Code != http.StatusUnauthorized {
		t.Fatalf("未知类型令牌应被管理路由拒绝: status=%d", out.Code)
	}
	if out := doReq(t, r, http.MethodGet, "/ws/chat", unknownTok); out.Code != http.StatusUnauthorized {
		t.Fatalf("未知类型令牌应被客户路由拒绝: status=%d", out.Code)
	}
}

// TestAdminRouteSubAccountUID 子账号过管理中间件后 uid 保留（刷新不丢身份的前置）
func TestAdminRouteSubAccountUID(t *testing.T) {
	_, subTok, _ := issueTokens(t)
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/api/admin/whoami", AdminAuth(), echoCtx)

	out := doReq(t, r, http.MethodGet, "/api/admin/whoami", subTok)
	if out.Code != http.StatusOK {
		t.Fatalf("子账号应过 AdminAuth: status=%d body=%s", out.Code, out.Body.String())
	}
	if !containsBody(out.Body.String(), `"uid":42`) {
		t.Fatalf("子账号 uid 应为 42: %s", out.Body.String())
	}
}

// TestPlatformAdminOnly 非平台租户令牌不可访问平台路由
func TestPlatformAdminOnly(t *testing.T) {
	adminTok, _, _ := issueTokens(t)
	r := newBoundaryRouter()
	if out := doReq(t, r, http.MethodGet, "/api/admin/tenants", adminTok); out.Code != http.StatusForbidden {
		t.Fatalf("普通租户令牌应被平台路由拒绝: status=%d", out.Code)
	}
}

// TestUserRouteAcceptsCustomerTokenWithSession 客户令牌 + 有效服务端会话 → 放行，
// 且身份（tenant/customer/thread）全部来自会话记录而非客户端传入
func TestUserRouteAcceptsCustomerTokenWithSession(t *testing.T) {
	_, _, custTok := issueTokens(t)

	orig := repository.FindCustomerSessionByToken
	defer func() { repository.FindCustomerSessionByToken = orig }()
	repository.FindCustomerSessionByToken = func(token string) (model.CustomerSession, error) {
		if token == "sess-boundary-1" {
			return model.CustomerSession{
				TenantID: testDemoTenant, CustomerID: testDemoTenant + ":buyer1",
				ThreadID: testDemoTenant + ":buyer1:20261019_abcd", ExpiresAt: time.Now().Add(time.Hour),
			}, nil
		}
		return model.CustomerSession{}, errNotFound
	}

	r := newBoundaryRouter()
	out := doReq(t, r, http.MethodGet, "/api/user/messages", custTok)
	if out.Code != http.StatusOK {
		t.Fatalf("有效客户会话应放行: status=%d body=%s", out.Code, out.Body.String())
	}
	body := out.Body.String()
	for _, want := range []string{`"tenant_id":"t_demo"`, `"customer_id":"t_demo:buyer1"`, `"thread_id":"t_demo:buyer1:20261019_abcd"`} {
		if !containsBody(body, want) {
			t.Fatalf("身份应来自服务端会话，缺少 %s: %s", want, body)
		}
	}
	// URL/query 里的 user_id 不得成为身份来源（路由不读 query user_id，仅此断言）
	if containsBody(body, "guest_") {
		t.Fatalf("不应出现客户端自造身份: %s", body)
	}
}

// TestUserRouteRejectsCustomerTokenWithoutSession 客户令牌验签通过但会话记录
// 不存在/已过期（服务端可吊销）→ fail-closed 401
func TestUserRouteRejectsCustomerTokenWithoutSession(t *testing.T) {
	_, _, custTok := issueTokens(t)

	orig := repository.FindCustomerSessionByToken
	defer func() { repository.FindCustomerSessionByToken = orig }()
	repository.FindCustomerSessionByToken = func(string) (model.CustomerSession, error) {
		return model.CustomerSession{}, errNotFound
	}

	r := newBoundaryRouter()
	if out := doReq(t, r, http.MethodGet, "/ws/chat", custTok); out.Code != http.StatusUnauthorized {
		t.Fatalf("无有效会话记录的客户令牌应被拒: status=%d", out.Code)
	}
}

var errNotFound = &notFoundErr{}

type notFoundErr struct{}

func (e *notFoundErr) Error() string { return "record not found" }

// encodeSigned 用租户 secret 签名载荷（与 auth.signPayload 同算法，模拟平台签发）
func encodeSigned(t *testing.T, secret string, pl []byte) string {
	t.Helper()
	mac := hmac.New(sha256.New, []byte(crypto.HashSecret(secret)))
	mac.Write(pl)
	return base64.RawURLEncoding.EncodeToString(pl) + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func containsBody(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
