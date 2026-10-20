//go:build integration

// ============================================================================
// 订单归属链路：真实 MySQL 集成验收（需显式开启，不随普通 go test 运行）
//
// 用法（在能连到测试库的环境执行；CS_TEST_MYSQL_DSN 的库名必须包含 "test"，
// 防止误连生产库；测试只写入/删除带 ITEST- 前缀的数据）：
//
//   CS_TEST_MYSQL_DSN='root:123456@tcp(127.0.0.1:3306)/cs_biz_test?charset=utf8mb4&parseTime=True&loc=Local' \
//     go test -tags=integration ./internal/handler/ -run TestOrderOwnership -v
//
// 覆盖：本人命中 / 同租户另一客户拒绝 / 跨租户拒绝 / 订单不存在 /
// 会话过期 fail-closed / UserAuth 真实会话查询。
// ============================================================================

package handler

import (
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"customer_service/internal/config"
	"customer_service/internal/middleware"
	"customer_service/internal/model"
	"customer_service/internal/pkg/auth"
	"customer_service/internal/pkg/crypto"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/mysql"
	"gorm.io/gorm"
)

const itestPrefix = "ITEST-"

// openItestDB 连接测试库（库名必须包含 test，双重保险）
func openItestDB(t *testing.T) *gorm.DB {
	t.Helper()
	dsn := os.Getenv("CS_TEST_MYSQL_DSN")
	if dsn == "" {
		t.Skip("未设置 CS_TEST_MYSQL_DSN，跳过真实 MySQL 集成测试")
	}
	slash := strings.Index(dsn, "/")
	question := strings.Index(dsn, "?")
	if slash < 0 || question < 0 || question < slash {
		t.Fatal("CS_TEST_MYSQL_DSN 格式应为 user:pwd@tcp(host:port)/dbname?params")
	}
	dbName := dsn[slash+1 : question]
	if !strings.Contains(strings.ToLower(dbName), "test") {
		t.Fatalf("拒绝连接：测试库名必须包含 \"test\"（当前 %q），防止误连生产库", dbName)
	}
	db, err := gorm.Open(mysql.Open(dsn), &gorm.Config{})
	if err != nil {
		t.Fatalf("连接测试库失败: %v", err)
	}
	if err := db.AutoMigrate(&model.CustomerSession{}, &model.Order{}); err != nil {
		t.Fatalf("AutoMigrate 失败: %v", err)
	}
	return db
}

// setupItest 预置租户表 + 种子数据（全部带 ITEST- 前缀），返回清理函数
func setupItest(t *testing.T, db *gorm.DB) {
	t.Helper()
	auth.TokenBlacklistChecker = func(string) bool { return false }
	auth.TenantsList = []auth.RuntimeTenant{
		{TenantID: "t_demo", Name: "演示租户", AppKey: "demo_key",
			AppSecretHash: crypto.HashSecret("demo_secret"), Status: 1},
	}
	auth.InitAuth(true, "2h", nil)
	oldDB := config.DB
	config.DB = db
	oldTok := config.GlobalConfig.InternalAPIToken
	config.GlobalConfig.InternalAPIToken = "itest-internal-token"

	// 清理上一次运行的残留（只删本测试前缀数据）
	db.Exec("DELETE FROM orders WHERE order_id LIKE ?", itestPrefix+"%")
	db.Exec("DELETE FROM customer_sessions WHERE thread_id LIKE ?", "t_demo:buyer%:itest%")

	// 种子：buyer1 / buyer2 各一个有效会话；buyer3 一个已过期会话
	now := time.Now()
	for _, s := range []model.CustomerSession{
		{TenantID: "t_demo", CustomerID: "t_demo:buyer1", ThreadID: "t_demo:buyer1:itest_a", Token: "itest_sess_b1", ExpiresAt: now.Add(time.Hour)},
		{TenantID: "t_demo", CustomerID: "t_demo:buyer2", ThreadID: "t_demo:buyer2:itest_b", Token: "itest_sess_b2", ExpiresAt: now.Add(time.Hour)},
		{TenantID: "t_demo", CustomerID: "t_demo:buyer3", ThreadID: "t_demo:buyer3:itest_c", Token: "itest_sess_b3", ExpiresAt: now.Add(-time.Hour)},
	} {
		if err := db.Create(&s).Error; err != nil {
			t.Fatalf("种子会话失败: %v", err)
		}
	}
	// 种子订单：ITEST-ORD-1 属 buyer1，ITEST-ORD-2 属 buyer2
	for _, o := range []model.Order{
		{OrderID: itestPrefix + "ORD-1", TenantID: "t_demo", CustomerID: "t_demo:buyer1", Item: "ITEST 会员", Amount: 799, Status: "已完成", CreateDate: "2026-10-01"},
		{OrderID: itestPrefix + "ORD-2", TenantID: "t_demo", CustomerID: "t_demo:buyer2", Item: "ITEST 课程", Amount: 199, Status: "配送中", CreateDate: "2026-10-02"},
	} {
		if err := db.Create(&o).Error; err != nil {
			t.Fatalf("种子订单失败: %v", err)
		}
	}

	t.Cleanup(func() {
		db.Exec("DELETE FROM orders WHERE order_id LIKE ?", itestPrefix+"%")
		db.Exec("DELETE FROM customer_sessions WHERE thread_id LIKE ?", "t_demo:buyer%:itest%")
		config.DB = oldDB
		config.GlobalConfig.InternalAPIToken = oldTok
	})
}

func itestOrderReq(t *testing.T, tenantID, threadID, orderID string) *httptest.ResponseRecorder {
	t.Helper()
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/api/agent/order", BusinessOrder)
	req := httptest.NewRequest(http.MethodGet,
		"/api/agent/order?tenant_id="+tenantID+"&thread_id="+threadID+"&order_id="+orderID, nil)
	req.Header.Set("X-Internal-Token", "itest-internal-token")
	out := httptest.NewRecorder()
	r.ServeHTTP(out, req)
	return out
}

// TestOrderOwnershipAgainstMySQL 六场景真实库验收
func TestOrderOwnershipAgainstMySQL(t *testing.T) {
	db := openItestDB(t)
	setupItest(t, db)

	// 1) 本人命中
	out := itestOrderReq(t, "t_demo", "t_demo:buyer1:itest_a", itestPrefix+"ORD-1")
	if out.Code != http.StatusOK || !contains(out.Body.String(), `"code":0`) {
		t.Fatalf("本人命中失败: status=%d body=%s", out.Code, out.Body.String())
	}
	if !contains(out.Body.String(), "ITEST 会员") || !contains(out.Body.String(), "已完成") {
		t.Fatalf("本人命中应返回订单详情: %s", out.Body.String())
	}

	// 2) 同租户另一客户 → 与"订单不存在"完全相同的提示
	other := itestOrderReq(t, "t_demo", "t_demo:buyer2:itest_b", itestPrefix+"ORD-1")
	missing := itestOrderReq(t, "t_demo", "t_demo:buyer1:itest_a", itestPrefix+"ORD-999")
	if other.Body.String() != missing.Body.String() {
		t.Fatalf("归属拒绝与不存在提示不一致:\n other=%s\n missing=%s", other.Body.String(), missing.Body.String())
	}

	// 3) 跨租户 thread 前缀 → 403
	if out := itestOrderReq(t, "t_other", "t_demo:buyer1:itest_a", itestPrefix+"ORD-1"); out.Code != http.StatusForbidden {
		t.Fatalf("跨租户应 403: status=%d", out.Code)
	}

	// 4) 订单不存在（本人）→ 统一提示
	if out := itestOrderReq(t, "t_demo", "t_demo:buyer1:itest_a", itestPrefix+"ORD-404"); !contains(out.Body.String(), "未找到订单或订单不属于您") {
		t.Fatalf("不存在订单应统一提示: %s", out.Body.String())
	}

	// 5) 会话已过期 → fail-closed 统一提示
	if out := itestOrderReq(t, "t_demo", "t_demo:buyer3:itest_c", itestPrefix+"ORD-1"); !contains(out.Body.String(), "未找到订单或订单不属于您") {
		t.Fatalf("过期会话应 fail-closed: %s", out.Body.String())
	}

	// 6) buyer2 查自己的订单应命中（隔离不等于拒绝本人）
	if out := itestOrderReq(t, "t_demo", "t_demo:buyer2:itest_b", itestPrefix+"ORD-2"); !contains(out.Body.String(), "ITEST 课程") {
		t.Fatalf("buyer2 本人订单应命中: %s", out.Body.String())
	}
}

// TestUserAuthAgainstMySQL UserAuth 经真实 customer_sessions 查询注入身份
func TestUserAuthAgainstMySQL(t *testing.T) {
	db := openItestDB(t)
	setupItest(t, db)

	tok, _, err := auth.IssueCustomerToken("t_demo", "t_demo:buyer1", "itest_sess_b1")
	if err != nil {
		t.Fatal(err)
	}
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/api/user/messages", middleware.UserAuth(), func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"tenant_id": middleware.TenantOf(c), "thread_id": middleware.ThreadOf(c)})
	})
	req := httptest.NewRequest(http.MethodGet, "/api/user/messages", nil)
	req.Header.Set("Authorization", "Bearer "+tok)
	out := httptest.NewRecorder()
	r.ServeHTTP(out, req)
	if out.Code != http.StatusOK {
		t.Fatalf("真实会话应放行: status=%d body=%s", out.Code, out.Body.String())
	}
	if !contains(out.Body.String(), "t_demo:buyer1:itest_a") {
		t.Fatalf("thread_id 应来自服务端会话记录: %s", out.Body.String())
	}
}
