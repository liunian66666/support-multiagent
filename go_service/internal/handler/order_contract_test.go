package handler

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"customer_service/internal/config"
	"github.com/gin-gonic/gin"
)

func TestBusinessOrderFailsClosedWithoutOwnership(t *testing.T) {
	old := config.GlobalConfig.InternalAPIToken
	config.GlobalConfig.InternalAPIToken = "test-internal-token"
	defer func() { config.GlobalConfig.InternalAPIToken = old }()
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/api/agent/order", BusinessOrder)

	req := httptest.NewRequest(http.MethodGet, "/api/agent/order?tenant_id=t_demo&thread_id=t_demo%3Abuyer1&order_id=ORD-2024-001", nil)
	req.Header.Set("X-Internal-Token", "test-internal-token")
	out := httptest.NewRecorder()
	r.ServeHTTP(out, req)
	if out.Code != http.StatusServiceUnavailable || !strings.Contains(out.Body.String(), "无法核验") || strings.Contains(out.Body.String(), "799") {
		t.Fatalf("order data should be withheld: status=%d body=%s", out.Code, out.Body.String())
	}
	req.Header.Set("X-Internal-Token", "wrong")
	out = httptest.NewRecorder()
	r.ServeHTTP(out, req)
	if out.Code != http.StatusForbidden {
		t.Fatalf("wrong token: status=%d", out.Code)
	}
	req.Header.Set("X-Internal-Token", "test-internal-token")
	req = httptest.NewRequest(http.MethodGet, "/api/agent/order?tenant_id=t_other&thread_id=t_demo%3Abuyer1&order_id=ORD-2024-001", nil)
	req.Header.Set("X-Internal-Token", "test-internal-token")
	out = httptest.NewRecorder()
	r.ServeHTTP(out, req)
	if out.Code != http.StatusForbidden {
		t.Fatalf("cross-tenant: status=%d", out.Code)
	}
}
