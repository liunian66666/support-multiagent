package handler

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"customer_service/internal/config"
	"github.com/gin-gonic/gin"
)

// TestBusinessOrderIdentityGuard 覆盖身份校验失败的路径（不查表）：
// 错误令牌、跨租户、缺订单号。命中与归属校验走端到端验证（连真实 MySQL）。
func TestBusinessOrderIdentityGuard(t *testing.T) {
	old := config.GlobalConfig.InternalAPIToken
	config.GlobalConfig.InternalAPIToken = "test-internal-token"
	defer func() { config.GlobalConfig.InternalAPIToken = old }()
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/api/agent/order", BusinessOrder)

	// 错误令牌 → 403
	req := httptest.NewRequest(http.MethodGet, "/api/agent/order?tenant_id=t_demo&thread_id=t_demo%3Abuyer1&order_id=ORD-2024-001", nil)
	req.Header.Set("X-Internal-Token", "wrong")
	out := httptest.NewRecorder()
	r.ServeHTTP(out, req)
	if out.Code != http.StatusForbidden {
		t.Fatalf("wrong token: status=%d", out.Code)
	}

	// 跨租户（thread_id 前缀不匹配 tenant_id）→ 403
	req = httptest.NewRequest(http.MethodGet, "/api/agent/order?tenant_id=t_other&thread_id=t_demo%3Abuyer1&order_id=ORD-2024-001", nil)
	req.Header.Set("X-Internal-Token", "test-internal-token")
	out = httptest.NewRecorder()
	r.ServeHTTP(out, req)
	if out.Code != http.StatusForbidden {
		t.Fatalf("cross-tenant: status=%d", out.Code)
	}

	// 缺订单号 → 403
	req = httptest.NewRequest(http.MethodGet, "/api/agent/order?tenant_id=t_demo&thread_id=t_demo%3Abuyer1", nil)
	req.Header.Set("X-Internal-Token", "test-internal-token")
	out = httptest.NewRecorder()
	r.ServeHTTP(out, req)
	if out.Code != http.StatusForbidden {
		t.Fatalf("missing order_id: status=%d", out.Code)
	}

	// 身份校验通过的请求会进入 DB 查询路径，测试镜像无 DB 连接会 error。
	// 此处只断言身份校验层；命中与归属校验由端到端验证覆盖（见 MERGE_ACCEPTANCE.md）。
}
