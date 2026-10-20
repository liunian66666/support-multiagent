package auth

import (
	"encoding/base64"
	"strconv"
	"strings"
	"testing"
	"time"

	"customer_service/internal/pkg/cache"
	"customer_service/internal/pkg/crypto"
)

// 测试用租户表（避免 DB 依赖）
func setupTestTenants(t *testing.T) {
	t.Helper()
	authEnabled = true
	authTTL = time.Hour
	// 测试环境无 Redis：黑名单判定按"未拉黑"处理（黑名单语义单独由 Redis 集成验证）
	TokenBlacklistChecker = func(string) bool { return false }
	tenantByKey = map[string]RuntimeTenant{
		"t1_key": {TenantID: "t_alpha", Name: "Alpha", AppKey: "t1_key", AppSecretHash: crypto.HashSecret("secret_alpha"), Status: 1},
		"t2_key": {TenantID: "t_beta", Name: "Beta", AppKey: "t2_key", AppSecretHash: crypto.HashSecret("secret_beta"), Status: 1},
		"t3_key": {TenantID: "t_disabled", Name: "Disabled", AppKey: "t3_key", AppSecretHash: crypto.HashSecret("secret_disabled"), Status: 0},
	}
	TenantsList = []RuntimeTenant{}
	for _, v := range tenantByKey {
		TenantsList = append(TenantsList, v)
	}
}

// ============ IssueToken / VerifyToken ============

func TestIssueAndVerifyToken(t *testing.T) {
	setupTestTenants(t)
	tok, tid, exp, err := IssueToken("t1_key", "secret_alpha")
	if err != nil {
		t.Fatalf("IssueToken 失败: %v", err)
	}
	if tid != "t_alpha" {
		t.Errorf("tenant_id 应为 t_alpha, 实际 %s", tid)
	}
	if exp <= 0 {
		t.Errorf("expires_in 应为正数, 实际 %d", exp)
	}
	gotTid, err := VerifyToken(tok)
	if err != nil {
		t.Fatalf("VerifyToken 失败: %v", err)
	}
	if gotTid != "t_alpha" {
		t.Errorf("VerifyToken 返回租户 %s, 期望 t_alpha", gotTid)
	}
}

func TestIssueTokenRejectsBadSecret(t *testing.T) {
	setupTestTenants(t)
	if _, _, _, err := IssueToken("t1_key", "wrong_secret"); err == nil {
		t.Error("错误 secret 应返回 error")
	}
}

func TestIssueTokenRejectsUnknownKey(t *testing.T) {
	setupTestTenants(t)
	if _, _, _, err := IssueToken("nope_key", "whatever"); err == nil {
		t.Error("未知 app_key 应返回 error")
	}
}

func TestIssueTokenRejectsDisabledTenant(t *testing.T) {
	setupTestTenants(t)
	if _, _, _, err := IssueToken("t3_key", "secret_disabled"); err == nil {
		t.Error("禁用租户应返回 error")
	}
}

func TestVerifyTokenRejectsTampered(t *testing.T) {
	setupTestTenants(t)
	tok, _, _, err := IssueToken("t1_key", "secret_alpha")
	if err != nil {
		t.Fatal(err)
	}
	parts := strings.Split(tok, ".")
	tampered := parts[0] + "." + "A" + parts[1][1:]
	if _, err := VerifyToken(tampered); err == nil {
		t.Error("篡改 token 应校验失败")
	}
}

func TestVerifyTokenRejectsGarbage(t *testing.T) {
	setupTestTenants(t)
	if _, err := VerifyToken("not.a.token"); err == nil {
		t.Error("垃圾 token 应校验失败")
	}
	if _, err := VerifyToken(""); err == nil {
		t.Error("空 token 应校验失败")
	}
}

func TestVerifyTokenExpired(t *testing.T) {
	setupTestTenants(t)
	pl := tokenPayload{TenantID: "t_alpha", Exp: time.Now().Add(-time.Minute).Unix(), Nonce: "n"}
	raw := `{"tenant_id":"t_alpha","exp":` + strconv.FormatInt(pl.Exp, 10) + `,"nonce":"n"}`
	expired := base64.RawURLEncoding.EncodeToString([]byte(raw)) + "." + signPayload(crypto.HashSecret("secret_alpha"), []byte(raw))
	if _, err := VerifyToken(expired); err == nil {
		t.Error("过期 token 应校验失败")
	}
}

func TestCrossTenantTokenInvalid(t *testing.T) {
	setupTestTenants(t)
	raw := `{"tenant_id":"t_alpha","exp":` + strconv.FormatInt(time.Now().Add(time.Hour).Unix(), 10) + `,"nonce":"n"}`
	sig := signPayload(crypto.HashSecret("secret_beta"), []byte(raw)) // 用错误租户签名
	tok := base64.RawURLEncoding.EncodeToString([]byte(raw)) + "." + sig
	if _, err := VerifyToken(tok); err == nil {
		t.Error("跨租户伪造 token 应校验失败")
	}
}

// ============ RBAC 子账号 Token（uid） ============

func TestIssueUserTokenCarriesUID(t *testing.T) {
	setupTestTenants(t)
	tok, _, err := IssueUserToken("t_alpha", 42)
	if err != nil {
		t.Fatalf("IssueUserToken 失败: %v", err)
	}
	tid, uid, err := VerifyTokenFull(tok)
	if err != nil {
		t.Fatalf("VerifyTokenFull 失败: %v", err)
	}
	if tid != "t_alpha" {
		t.Errorf("租户 %s, 期望 t_alpha", tid)
	}
	if uid != 42 {
		t.Errorf("uid = %d, 期望 42", uid)
	}
}

func TestAppKeyTokenHasNoUID(t *testing.T) {
	setupTestTenants(t)
	tok, _, _, err := IssueToken("t1_key", "secret_alpha")
	if err != nil {
		t.Fatal(err)
	}
	_, uid, err := VerifyTokenFull(tok)
	if err != nil {
		t.Fatal(err)
	}
	if uid != 0 {
		t.Errorf("app_key 登录 token 不应携带 uid, got %d", uid)
	}
}

// ============ 黑名单 ============

func TestBlacklistAndVerify(t *testing.T) {
	if cache.Client() == nil {
		t.Skip("需要 Redis 环境")
	}
	setupTestTenants(t)
	TokenBlacklistChecker = IsTokenBlacklisted // 黑名单语义测试用真实实现
	tok, _, _, err := IssueToken("t1_key", "secret_alpha")
	if err != nil {
		t.Fatal(err)
	}
	BlacklistToken(tok)
	if !IsTokenBlacklisted(tok) {
		t.Error("黑名单中的 token 应返回 true")
	}
	if _, err := VerifyToken(tok); err == nil {
		t.Error("黑名单 token 校验应失败")
	}
}

// ============ RefreshToken ============

func TestRefreshToken(t *testing.T) {
	setupTestTenants(t)
	tok, _, _, err := IssueToken("t1_key", "secret_alpha")
	if err != nil {
		t.Fatal(err)
	}
	newTok, tid, uid, _, err := RefreshToken(tok)
	if err != nil {
		t.Fatalf("RefreshToken 失败: %v", err)
	}
	if tid != "t_alpha" {
		t.Errorf("刷新后租户 %s, 期望 t_alpha", tid)
	}
	if uid != 0 {
		t.Errorf("app_key 令牌刷新后 uid 应为 0, got %d", uid)
	}
	if _, err := VerifyToken(newTok); err != nil {
		t.Errorf("新 token 校验失败: %v", err)
	}
	if cache.Client() == nil {
		// 无 Redis：黑名单写入不生效，旧 token 失效语义由 Redis 集成验证
		t.Log("无 Redis 环境，跳过旧 token 失效断言")
		return
	}
	if _, err := VerifyToken(tok); err == nil {
		t.Error("刷新后旧 token 应失效")
	}
}

// ============ 令牌类型边界（P0：客户令牌不得进入管理端/刷新接口） ============

// issueCustomerTokenForTest 签发一个客户会话令牌（不落库，仅验签测试用）
func issueCustomerTokenForTest(t *testing.T, tenantID, customerID, sessionID string) string {
	t.Helper()
	tok, _, err := IssueCustomerToken(tenantID, customerID, sessionID)
	if err != nil {
		t.Fatalf("IssueCustomerToken 失败: %v", err)
	}
	return tok
}

// TestVerifyTokenFullRejectsCustomerToken 客户令牌过管理端验签必须被拒
//（两条提权路径共用 VerifyTokenFull：直接进管理接口、经刷新变成管理令牌）
func TestVerifyTokenFullRejectsCustomerToken(t *testing.T) {
	setupTestTenants(t)
	tok := issueCustomerTokenForTest(t, "t_alpha", "t_alpha:buyer1", "sess-001")
	if _, _, err := VerifyTokenFull(tok); err == nil {
		t.Error("客户会话令牌不应通过管理端验签 VerifyTokenFull")
	}
	if _, err := VerifyToken(tok); err == nil {
		t.Error("客户会话令牌不应通过通用租户校验 VerifyToken")
	}
}

// TestRefreshTokenRejectsCustomerToken 客户令牌刷新必须被拒（不得转换成管理令牌）
func TestRefreshTokenRejectsCustomerToken(t *testing.T) {
	setupTestTenants(t)
	tok := issueCustomerTokenForTest(t, "t_alpha", "t_alpha:buyer1", "sess-002")
	if _, _, _, _, err := RefreshToken(tok); err == nil {
		t.Error("客户会话令牌不应允许刷新（不得转换成管理令牌）")
	}
}

// TestVerifyCustomerTokenRejectsAdminToken 管理令牌不得冒用客户会话路由
func TestVerifyCustomerTokenRejectsAdminToken(t *testing.T) {
	setupTestTenants(t)
	adminTok, _, _, err := IssueToken("t1_key", "secret_alpha")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := VerifyCustomerToken(adminTok); err == nil {
		t.Error("管理端令牌不应通过客户会话验签")
	}
	subTok, _, err := IssueUserToken("t_alpha", 7)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := VerifyCustomerToken(subTok); err == nil {
		t.Error("子账号令牌不应通过客户会话验签")
	}
}

// TestVerifyRejectsUnknownKind 未知类型令牌两个入口都必须拒绝（不当管理令牌放行）
func TestVerifyRejectsUnknownKind(t *testing.T) {
	setupTestTenants(t)
	raw := `{"tid":"t_alpha","exp":` + strconv.FormatInt(time.Now().Add(time.Hour).Unix(), 10) +
		`,"nonce":"n","kind":"weird_future_kind"}`
	sig := signPayload(crypto.HashSecret("secret_alpha"), []byte(raw))
	tok := base64.RawURLEncoding.EncodeToString([]byte(raw)) + "." + sig
	if _, _, err := VerifyTokenFull(tok); err == nil {
		t.Error("未知类型令牌不应通过管理端验签")
	}
	if _, err := VerifyCustomerToken(tok); err == nil {
		t.Error("未知类型令牌不应通过客户会话验签")
	}
}

// TestCustomerTokenExpiredAndForgedRejected 客户令牌过期/伪造必须被拒
func TestCustomerTokenExpiredAndForgedRejected(t *testing.T) {
	setupTestTenants(t)
	// 过期客户令牌
	rawExp := `{"tid":"t_alpha","exp":` + strconv.FormatInt(time.Now().Add(-time.Minute).Unix(), 10) +
		`,"nonce":"n","kind":"customer","cid":"t_alpha:buyer1","sess":"s1"}`
	tokExp := base64.RawURLEncoding.EncodeToString([]byte(rawExp)) + "." +
		signPayload(crypto.HashSecret("secret_alpha"), []byte(rawExp))
	if _, err := VerifyCustomerToken(tokExp); err == nil {
		t.Error("过期客户令牌应被拒")
	}
	// 伪造签名客户令牌
	raw := `{"tid":"t_alpha","exp":` + strconv.FormatInt(time.Now().Add(time.Hour).Unix(), 10) +
		`,"nonce":"n","kind":"customer","cid":"t_alpha:buyer1","sess":"s1"}`
	forged := base64.RawURLEncoding.EncodeToString([]byte(raw)) + "." +
		signPayload(crypto.HashSecret("secret_beta"), []byte(raw)) // 错误租户密钥签名
	if _, err := VerifyCustomerToken(forged); err == nil {
		t.Error("伪造签名的客户令牌应被拒")
	}
}

// TestRefreshTokenPreservesUID 子账号刷新后 uid 必须保留（权限身份不丢失）
func TestRefreshTokenPreservesUID(t *testing.T) {
	setupTestTenants(t)
	tok, _, err := IssueUserToken("t_alpha", 42)
	if err != nil {
		t.Fatal(err)
	}
	newTok, tid, uid, _, err := RefreshToken(tok)
	if err != nil {
		t.Fatalf("RefreshToken 失败: %v", err)
	}
	if tid != "t_alpha" || uid != 42 {
		t.Fatalf("刷新后身份丢失: tid=%s uid=%d", tid, uid)
	}
	gotTid, gotUID, err := VerifyTokenFull(newTok)
	if err != nil {
		t.Fatalf("新令牌校验失败: %v", err)
	}
	if gotTid != "t_alpha" || gotUID != 42 {
		t.Errorf("刷新后 uid 不一致: got (%s,%d), want (t_alpha,42)", gotTid, gotUID)
	}
}

// TestAppKeyRefreshHasNoUID app_key 管理员令牌刷新后 uid 仍为 0
func TestAppKeyRefreshHasNoUID(t *testing.T) {
	setupTestTenants(t)
	tok, _, _, err := IssueToken("t1_key", "secret_alpha")
	if err != nil {
		t.Fatal(err)
	}
	_, _, uid, _, err := RefreshToken(tok)
	if err != nil {
		t.Fatalf("RefreshToken 失败: %v", err)
	}
	if uid != 0 {
		t.Errorf("app_key 管理员刷新后 uid 应为 0, got %d", uid)
	}
}

// ============ 租户隔离（纯函数） ============

func TestUserIDBelongsToTenant(t *testing.T) {
	setupTestTenants(t)
	cases := []struct {
		tenantID, userID string
		want             bool
	}{
		{"t_alpha", "t_alpha:u1", true},
		{"t_alpha", "t_beta:u1", false},
		{"t_alpha", "t_alpha1:u1", false}, // 前缀撞车（t_alpha1 非 t_alpha）
		{"t_alpha", "", false},
		{"", "t_alpha:u1", false},
		{"t_beta", "t_beta:abc", true},
	}
	for _, c := range cases {
		if got := UserIDBelongsToTenant(c.tenantID, c.userID); got != c.want {
			t.Errorf("UserIDBelongsToTenant(%q,%q)=%v, want %v", c.tenantID, c.userID, got, c.want)
		}
	}
}

func TestTenantFromUserID(t *testing.T) {
	cases := []struct{ in, want string }{
		{"t_demo:user123", "t_demo"},
		{"t_demo:user:with:colon", "t_demo"},
		{"nocolon", ""},
		{"", ""},
	}
	for _, c := range cases {
		if got := TenantFromUserID(c.in); got != c.want {
			t.Errorf("TenantFromUserID(%q)=%q, want %q", c.in, got, c.want)
		}
	}
}
