package handler

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"customer_service/internal/config"
	"customer_service/internal/model"
	"customer_service/internal/pkg/auth"
	"customer_service/internal/pkg/crypto"
	"customer_service/internal/repository"

	"github.com/gin-gonic/gin"
)

// ============ 演示客户登录：凭证分离 + 白名单 + thread_id 唯一性 ============

const (
	demoBuyerAppKey    = "demo_buyer_key"
	demoBuyerAppSecret = "demo_buyer_secret"
	demoAdminAppKey    = "demo_key"
	demoAdminAppSecret = "demo_secret"
)

// setupCustomerLoginTest 预设演示客户登录环境（无 DB/Redis 依赖）
func setupCustomerLoginTest(t *testing.T) func() {
	t.Helper()
	// 鉴权与租户表
	auth.TokenBlacklistChecker = func(string) bool { return false }
	auth.TenantsList = []auth.RuntimeTenant{
		{TenantID: "t_demo", Name: "演示租户", AppKey: demoAdminAppKey,
			AppSecretHash: crypto.HashSecret(demoAdminAppSecret), Status: 1},
	}
	auth.InitAuth(true, "2h", nil)

	// 演示客户登录开关与凭证（与管理端凭证分离）
	oldAuth := config.GlobalConfig.Auth
	oldCheck := checkDemoCustomerLoginAllowed
	checkDemoCustomerLoginAllowed = func(string) (bool, time.Duration) { return true, 0 }
	config.GlobalConfig.Auth.DemoCustomerLogin = true
	config.GlobalConfig.Auth.DemoCustomers = []config.DemoCustomer{
		{TenantID: "t_demo", AppKey: demoBuyerAppKey, AppSecret: demoBuyerAppSecret,
			Customers: []string{"buyer1", "buyer2"}},
	}

	// 会话落库注入 fake（捕获写入内容，不连 DB）
	origCreate := repository.CreateCustomerSession
	var captured []model.CustomerSession
	repository.CreateCustomerSession = func(sess *model.CustomerSession) error {
		captured = append(captured, *sess)
		return nil
	}

	return func() {
		config.GlobalConfig.Auth = oldAuth
		checkDemoCustomerLoginAllowed = oldCheck
		repository.CreateCustomerSession = origCreate
		captured = nil
	}
}

// doCustomerLogin 发起一次演示客户登录请求
func doCustomerLogin(t *testing.T, appKey, appSecret, customerID string) *httptest.ResponseRecorder {
	t.Helper()
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.POST("/api/auth/customer-token", CustomerTokenHandler)
	body, _ := json.Marshal(map[string]string{
		"app_key": appKey, "app_secret": appSecret, "customer_id": customerID,
	})
	req := httptest.NewRequest(http.MethodPost, "/api/auth/customer-token", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	out := httptest.NewRecorder()
	r.ServeHTTP(out, req)
	return out
}

func doMerchantPreviewLogin(t *testing.T, appKey, appSecret string) *httptest.ResponseRecorder {
	t.Helper()
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.POST("/api/auth/merchant-preview-token", MerchantPreviewTokenHandler)
	body, _ := json.Marshal(map[string]string{"app_key": appKey, "app_secret": appSecret})
	req := httptest.NewRequest(http.MethodPost, "/api/auth/merchant-preview-token", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	out := httptest.NewRecorder()
	r.ServeHTTP(out, req)
	return out
}

func doGuestLogin(t *testing.T, body string) *httptest.ResponseRecorder {
	t.Helper()
	r := gin.New()
	r.POST("/api/auth/guest-token", GuestTokenHandler)
	req := httptest.NewRequest(http.MethodPost, "/api/auth/guest-token", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	out := httptest.NewRecorder()
	r.ServeHTTP(out, req)
	return out
}

func TestGuestLoginIsScopedAndCustomerOnly(t *testing.T) {
	restore := setupCustomerLoginTest(t)
	defer restore()
	oldCheck := checkGuestAdmission
	checkGuestAdmission = func(string) bool { return true }
	defer func() { checkGuestAdmission = oldCheck }()
	config.GlobalConfig.Auth.DemoGuestChat = true
	config.GlobalConfig.Auth.DemoGuestTenantID = "t_demo"
	var created model.CustomerSession
	oldCreate := repository.CreateCustomerSession
	repository.CreateCustomerSession = func(sess *model.CustomerSession) error { created = *sess; return nil }
	defer func() { repository.CreateCustomerSession = oldCreate }()
	out := doGuestLogin(t, `{"tenant_id":"t_other","customer_id":"t_demo:buyer1"}`)
	if out.Code != http.StatusOK || !created.IsGuest || created.TenantID != "t_demo" || !strings.HasPrefix(created.CustomerID, "t_demo:guest_") {
		t.Fatalf("访客身份必须由服务端生成: %d %s", out.Code, out.Body.String())
	}
	var resp struct {
		Data struct {
			Token string `json:"token"`
		} `json:"data"`
	}
	if err := json.Unmarshal(out.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if _, err := auth.VerifyCustomerToken(resp.Data.Token); err != nil {
		t.Fatalf("访客必须获得客户令牌: %v", err)
	}
	if _, _, err := auth.VerifyTokenFull(resp.Data.Token); err == nil {
		t.Fatal("访客令牌不得进入管理端")
	}
}

func TestGuestLoginSwitchOffAndRateLimit(t *testing.T) {
	restore := setupCustomerLoginTest(t)
	defer restore()
	config.GlobalConfig.Auth.DemoGuestChat = false
	if out := doGuestLogin(t, `{}`); out.Code != http.StatusForbidden {
		t.Fatalf("关闭时应 403: %d", out.Code)
	}
	config.GlobalConfig.Auth.DemoGuestChat = true
	config.GlobalConfig.Auth.DemoGuestTenantID = "t_demo"
	oldCheck := checkGuestAdmission
	checkGuestAdmission = func(string) bool { return false }
	defer func() { checkGuestAdmission = oldCheck }()
	if out := doGuestLogin(t, `{}`); out.Code != http.StatusTooManyRequests {
		t.Fatalf("限流时应 429: %d", out.Code)
	}
}

func TestDemoBuyerLoginRateLimited(t *testing.T) {
	restore := setupCustomerLoginTest(t)
	defer restore()
	checkDemoCustomerLoginAllowed = func(string) (bool, time.Duration) { return false, time.Minute }
	out := doCustomerLogin(t, demoBuyerAppKey, demoBuyerAppSecret, "buyer1")
	if out.Code != http.StatusTooManyRequests {
		t.Fatalf("演示买家登录限流应 429: %d", out.Code)
	}
}

func TestMerchantPreviewTokenIsCustomerOnly(t *testing.T) {
	restore := setupCustomerLoginTest(t)
	defer restore()
	oldCheck := checkMerchantPreviewLoginAllowed
	checkMerchantPreviewLoginAllowed = func(string) (bool, time.Duration) { return true, 0 }
	defer func() { checkMerchantPreviewLoginAllowed = oldCheck }()
	config.GlobalConfig.Auth.MerchantPreviewLogin = true
	var created model.CustomerSession
	oldCreate := repository.CreateCustomerSession
	repository.CreateCustomerSession = func(sess *model.CustomerSession) error { created = *sess; return nil }
	defer func() { repository.CreateCustomerSession = oldCreate }()

	if out := doMerchantPreviewLogin(t, demoAdminAppKey, "wrong"); out.Code != http.StatusUnauthorized {
		t.Fatalf("错误密钥应拒绝: %d %s", out.Code, out.Body.String())
	}
	out := doMerchantPreviewLogin(t, demoAdminAppKey, demoAdminAppSecret)
	if out.Code != http.StatusOK || !created.IsPreview {
		t.Fatalf("预览会话应成功且标记预览: %d %s", out.Code, out.Body.String())
	}
	var resp struct {
		Data struct {
			Token string `json:"token"`
		} `json:"data"`
	}
	if err := json.Unmarshal(out.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if _, err := auth.VerifyCustomerToken(resp.Data.Token); err != nil {
		t.Fatalf("预览令牌应是客户令牌: %v", err)
	}
	if _, _, err := auth.VerifyTokenFull(resp.Data.Token); err == nil {
		t.Fatal("预览令牌不能用于管理接口或刷新成管理令牌")
	}
}

func TestMerchantPreviewSwitchOff(t *testing.T) {
	restore := setupCustomerLoginTest(t)
	defer restore()
	config.GlobalConfig.Auth.MerchantPreviewLogin = false
	if out := doMerchantPreviewLogin(t, demoAdminAppKey, demoAdminAppSecret); out.Code != http.StatusForbidden {
		t.Fatalf("预览开关关闭应拒绝: %d", out.Code)
	}
}

func TestCustomerLoginSwitchOff(t *testing.T) {
	restore := setupCustomerLoginTest(t)
	defer restore()
	config.GlobalConfig.Auth.DemoCustomerLogin = false

	out := doCustomerLogin(t, demoBuyerAppKey, demoBuyerAppSecret, "buyer1")
	if out.Code != http.StatusForbidden {
		t.Fatalf("开关关闭应 403: status=%d body=%s", out.Code, out.Body.String())
	}
}

// TestCustomerLoginRejectsAdminCredentials 管理端凭证不得用于客户登录（凭证分离）
func TestCustomerLoginRejectsAdminCredentials(t *testing.T) {
	restore := setupCustomerLoginTest(t)
	defer restore()

	out := doCustomerLogin(t, demoAdminAppKey, demoAdminAppSecret, "buyer1")
	if out.Code != http.StatusUnauthorized {
		t.Fatalf("管理端凭证应被拒绝: status=%d", out.Code)
	}
	if !contains(out.Body.String(), "管理端凭证不可用于客户登录") {
		t.Fatalf("应提示使用演示客户凭证: %s", out.Body.String())
	}
}

func TestCustomerLoginRejectsBadCredentials(t *testing.T) {
	restore := setupCustomerLoginTest(t)
	defer restore()

	// 未知 app_key
	if out := doCustomerLogin(t, "nope_key", "whatever", "buyer1"); out.Code != http.StatusUnauthorized {
		t.Fatalf("未知凭证应 401: status=%d", out.Code)
	}
	// 错误 secret
	if out := doCustomerLogin(t, demoBuyerAppKey, "wrong_secret", "buyer1"); out.Code != http.StatusUnauthorized {
		t.Fatalf("错误 secret 应 401: status=%d", out.Code)
	}
}

// TestCustomerLoginAllowlist customer_id 限白名单：非白名单/跨租户前缀拒绝
func TestCustomerLoginAllowlist(t *testing.T) {
	restore := setupCustomerLoginTest(t)
	defer restore()

	if out := doCustomerLogin(t, demoBuyerAppKey, demoBuyerAppSecret, "buyer3"); out.Code != http.StatusBadRequest {
		t.Fatalf("白名单外客户应 400: status=%d body=%s", out.Code, out.Body.String())
	}
	if out := doCustomerLogin(t, demoBuyerAppKey, demoBuyerAppSecret, "t_other:buyer1"); out.Code != http.StatusBadRequest {
		t.Fatalf("跨租户前缀应 400: status=%d", out.Code)
	}
}

// TestCustomerLoginHappyPath 白名单内客户登录成功，会话字段正确
func TestCustomerLoginHappyPath(t *testing.T) {
	restore := setupCustomerLoginTest(t)
	defer restore()

	out := doCustomerLogin(t, demoBuyerAppKey, demoBuyerAppSecret, "buyer2")
	if out.Code != http.StatusOK {
		t.Fatalf("buyer2 应登录成功: status=%d body=%s", out.Code, out.Body.String())
	}
	body := out.Body.String()
	for _, want := range []string{`"code":0`, `"customer_id":"t_demo:buyer2"`, `"tenant_id":"t_demo"`, `"token":"`} {
		if !contains(body, want) {
			t.Fatalf("响应缺少 %s: %s", want, body)
		}
	}
	// thread_id 形如 t_demo:buyer2:20261019183000_<8位hex>（服务端生成 + 随机后缀）
	var resp struct {
		Data struct {
			ThreadID string `json:"thread_id"`
			Token    string `json:"token"`
		} `json:"data"`
	}
	if err := json.Unmarshal(out.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(resp.Data.ThreadID, "t_demo:buyer2:") {
		t.Fatalf("thread_id 前缀错误: %s", resp.Data.ThreadID)
	}
	parts := strings.SplitN(resp.Data.ThreadID, ":", 3)
	suffix := parts[2]
	i := strings.LastIndex(suffix, "_")
	if i < 0 || len(suffix[i+1:]) != 8 {
		t.Fatalf("thread_id 应带 8 位随机后缀: %s", resp.Data.ThreadID)
	}
	if resp.Data.Token == "" {
		t.Fatal("应返回客户会话令牌")
	}
}

// TestCustomerLoginDefaultCustomer 缺省 customer_id → 白名单首位（buyer1）
func TestCustomerLoginDefaultCustomer(t *testing.T) {
	restore := setupCustomerLoginTest(t)
	defer restore()

	out := doCustomerLogin(t, demoBuyerAppKey, demoBuyerAppSecret, "")
	if out.Code != http.StatusOK || !contains(out.Body.String(), `"customer_id":"t_demo:buyer1"`) {
		t.Fatalf("缺省客户应为 buyer1: status=%d body=%s", out.Code, out.Body.String())
	}
}

// TestCustomerLoginThreadIDNoCollision 同一客户同一秒内多次登录，thread_id 不得重复
func TestCustomerLoginThreadIDNoCollision(t *testing.T) {
	restore := setupCustomerLoginTest(t)
	defer restore()

	seen := map[string]bool{}
	for i := 0; i < 5; i++ {
		out := doCustomerLogin(t, demoBuyerAppKey, demoBuyerAppSecret, "buyer1")
		if out.Code != http.StatusOK {
			t.Fatalf("第 %d 次登录失败: %s", i, out.Body.String())
		}
		var resp struct {
			Data struct {
				ThreadID string `json:"thread_id"`
			} `json:"data"`
		}
		if err := json.Unmarshal(out.Body.Bytes(), &resp); err != nil {
			t.Fatal(err)
		}
		if seen[resp.Data.ThreadID] {
			t.Fatalf("thread_id 重复: %s", resp.Data.ThreadID)
		}
		seen[resp.Data.ThreadID] = true
	}
}

// ============ 刷新接口：令牌类型边界与身份响应 ============

// issueRefreshTokens 签发管理员(uid=0)/子账号(uid=42)/客户三类令牌
func issueRefreshTokens(t *testing.T) (adminTok, subTok, custTok string) {
	t.Helper()
	auth.TokenBlacklistChecker = func(string) bool { return false }
	auth.TenantsList = []auth.RuntimeTenant{
		{TenantID: "t_demo", Name: "演示租户", AppKey: demoAdminAppKey,
			AppSecretHash: crypto.HashSecret(demoAdminAppSecret), Status: 1},
	}
	auth.InitAuth(true, "2h", nil)
	var err error
	// IssueUserToken(tenant, 0) 即 uid=0 的管理员令牌（与 app_key 登录等价）
	if adminTok, _, err = auth.IssueUserToken("t_demo", 0); err != nil {
		t.Fatal(err)
	}
	if subTok, _, err = auth.IssueUserToken("t_demo", 42); err != nil {
		t.Fatal(err)
	}
	if custTok, _, err = auth.IssueCustomerToken("t_demo", "t_demo:buyer1", "sess-refresh-1"); err != nil {
		t.Fatal(err)
	}
	return
}

func doRefresh(t *testing.T, token string) *httptest.ResponseRecorder {
	t.Helper()
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.POST("/api/auth/refresh", RefreshTokenHandler)
	req := httptest.NewRequest(http.MethodPost, "/api/auth/refresh", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	out := httptest.NewRecorder()
	r.ServeHTTP(out, req)
	return out
}

// TestRefreshRejectsCustomerToken 客户令牌刷新 → 401（不得转换成管理令牌）
func TestRefreshRejectsCustomerToken(t *testing.T) {
	_, _, custTok := issueRefreshTokens(t)
	if out := doRefresh(t, custTok); out.Code != http.StatusUnauthorized {
		t.Fatalf("客户令牌刷新应 401: status=%d body=%s", out.Code, out.Body.String())
	}
}

// TestRefreshAdminTokenIdentity app_key 管理员刷新：is_admin=true
func TestRefreshAdminTokenIdentity(t *testing.T) {
	adminTok, _, _ := issueRefreshTokens(t)
	out := doRefresh(t, adminTok)
	if out.Code != http.StatusOK {
		t.Fatalf("管理员刷新应 200: status=%d body=%s", out.Code, out.Body.String())
	}
	for _, want := range []string{`"code":0`, `"is_admin":true`, `"role_name":"管理员"`} {
		if !contains(out.Body.String(), want) {
			t.Fatalf("管理员刷新响应缺少 %s: %s", want, out.Body.String())
		}
	}
}

// TestRefreshSubAccountIdentity 子账号刷新：按真实身份响应，is_admin 不得一律 true
func TestRefreshSubAccountIdentity(t *testing.T) {
	_, subTok, _ := issueRefreshTokens(t)

	orig := repository.GetUserWithRoles
	origAdmin := repository.IsAdminUser
	defer func() { repository.GetUserWithRoles = orig; repository.IsAdminUser = origAdmin }()
	repository.GetUserWithRoles = func(userID uint64) (*model.User, []repository.RoleBrief, error) {
		if userID != 42 {
			t.Fatalf("应按 uid=42 查账号, got %d", userID)
		}
		return &model.User{ID: 42, Username: "agent_li", Nickname: "小李"},
			[]repository.RoleBrief{{Name: "客服主管", Code: "agent_lead"}}, nil
	}
	repository.IsAdminUser = func(uint64) bool { return false } // 该子账号非全权限

	out := doRefresh(t, subTok)
	if out.Code != http.StatusOK {
		t.Fatalf("子账号刷新应 200: status=%d body=%s", out.Code, out.Body.String())
	}
	body := out.Body.String()
	for _, want := range []string{`"user_id":42`, `"is_admin":false`, `"nickname":"小李"`, `"role_name":"客服主管"`} {
		if !contains(body, want) {
			t.Fatalf("子账号刷新响应缺少 %s: %s", want, body)
		}
	}
}

// TestRefreshSubAccountDeleted 子账号已删除 → 拒绝刷新
func TestRefreshSubAccountDeleted(t *testing.T) {
	_, subTok, _ := issueRefreshTokens(t)

	orig := repository.GetUserWithRoles
	defer func() { repository.GetUserWithRoles = orig }()
	repository.GetUserWithRoles = func(uint64) (*model.User, []repository.RoleBrief, error) {
		return nil, nil, errRecordGone
	}

	if out := doRefresh(t, subTok); out.Code != http.StatusUnauthorized {
		t.Fatalf("账号已删除应 401: status=%d", out.Code)
	}
}

var errRecordGone = &recordGoneErr{}

type recordGoneErr struct{}

func (e *recordGoneErr) Error() string { return "record not found" }

// 确保 time 包被使用（测试里构造过期时间戳备用）
var _ = time.Now
