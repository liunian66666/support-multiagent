package handler

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"customer_service/internal/config"
	"customer_service/internal/model"
	"customer_service/internal/repository"

	"github.com/gin-gonic/gin"
)

// withOrderMocks 注入客户会话/订单查询 fake，返回还原函数。
// fake 语义：threadID=t_demo:buyer1:20261019 归属客户 t_demo:buyer1（会话有效）；
// 其余 threadID 一律视为无会话记录（fail-closed）。
func withOrderMocks(t *testing.T) func() {
	t.Helper()
	origSession := repository.FindCustomerSessionByThread
	origOrder := repository.FindOrderForCustomer
	repository.FindCustomerSessionByThread = func(threadID, tenantID string) (model.CustomerSession, error) {
		if tenantID == "t_demo" && threadID == "t_demo:buyer1:20261019" {
			return model.CustomerSession{
				TenantID:   "t_demo",
				CustomerID: "t_demo:buyer1",
				ThreadID:   threadID,
				ExpiresAt:  time.Now().Add(time.Hour),
			}, nil
		}
		return model.CustomerSession{}, errors.New("record not found")
	}
	repository.FindOrderForCustomer = func(tenantID, customerID, orderID string) (model.Order, error) {
		// 演示订单只归属 t_demo:buyer1
		if tenantID == "t_demo" && customerID == "t_demo:buyer1" {
			switch orderID {
			case "ORD-2024-001":
				return model.Order{OrderID: orderID, TenantID: tenantID, CustomerID: customerID,
					Item: "VIP 年费会员", Amount: 799, Status: "已完成", CreateDate: "2024-01-15"}, nil
			case "ORD-2024-002":
				return model.Order{OrderID: orderID, TenantID: tenantID, CustomerID: customerID,
					Item: "Python 实战课程", Amount: 199, Status: "配送中", CreateDate: "2024-03-20"}, nil
			}
		}
		return model.Order{}, errors.New("record not found")
	}
	return func() {
		repository.FindCustomerSessionByThread = origSession
		repository.FindOrderForCustomer = origOrder
	}
}

// doOrderReq 发起一次 BusinessOrder 请求；internalToken 传入错误值可覆盖头部
func doOrderReq(t *testing.T, query, internalToken string) *httptest.ResponseRecorder {
	t.Helper()
	old := config.GlobalConfig.InternalAPIToken
	config.GlobalConfig.InternalAPIToken = "test-internal-token"
	defer func() { config.GlobalConfig.InternalAPIToken = old }()
	if internalToken == "" {
		internalToken = "test-internal-token"
	}
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/api/agent/order", BusinessOrder)
	req := httptest.NewRequest(http.MethodGet, "/api/agent/order?"+query, nil)
	req.Header.Set("X-Internal-Token", internalToken)
	out := httptest.NewRecorder()
	r.ServeHTTP(out, req)
	return out
}

// TestBusinessOrderIdentityGuard 入口身份校验（不查库）：
// 错误令牌、跨租户 thread 前缀、缺订单号 → 403
func TestBusinessOrderIdentityGuard(t *testing.T) {
	defer withOrderMocks(t)()

	// 错误令牌 → 403
	if out := doOrderReq(t, "tenant_id=t_demo&thread_id=t_demo%3Abuyer1%3A20261019&order_id=ORD-2024-001", "wrong"); out.Code != http.StatusForbidden {
		t.Fatalf("wrong token: status=%d", out.Code)
	}

	// 跨租户（thread_id 前缀不匹配 tenant_id）→ 403
	if out := doOrderReq(t, "tenant_id=t_other&thread_id=t_demo%3Abuyer1%3A20261019&order_id=ORD-2024-001", ""); out.Code != http.StatusForbidden {
		t.Fatalf("cross-tenant: status=%d", out.Code)
	}

	// 缺订单号 → 403
	if out := doOrderReq(t, "tenant_id=t_demo&thread_id=t_demo%3Abuyer1%3A20261019", ""); out.Code != http.StatusForbidden {
		t.Fatalf("missing order_id: status=%d", out.Code)
	}
}

// TestBusinessOrderOwnerHit 本人命中：buyer1 的会话查自己的订单 → 返回状态
func TestBusinessOrderOwnerHit(t *testing.T) {
	defer withOrderMocks(t)()
	out := doOrderReq(t, "tenant_id=t_demo&thread_id=t_demo%3Abuyer1%3A20261019&order_id=ORD-2024-001", "")
	if out.Code != http.StatusOK {
		t.Fatalf("owner hit: status=%d body=%s", out.Code, out.Body.String())
	}
	body := out.Body.String()
	for _, want := range []string{`"code":0`, "ORD-2024-001", "VIP 年费会员", "已完成", "799"} {
		if !contains(body, want) {
			t.Fatalf("owner hit body missing %q: %s", want, body)
		}
	}
}

// TestBusinessOrderSameTenantOtherCustomer 同租户另一客户：buyer2 查 buyer1 的订单
// → 与"订单不存在"完全相同的提示（不暴露订单存在性）
func TestBusinessOrderSameTenantOtherCustomer(t *testing.T) {
	defer withOrderMocks(t)()
	other := doOrderReq(t, "tenant_id=t_demo&thread_id=t_demo%3Abuyer2%3A20261019&order_id=ORD-2024-001", "")
	missing := doOrderReq(t, "tenant_id=t_demo&thread_id=t_demo%3Abuyer1%3A20261019&order_id=ORD-9999", "")
	if other.Code != http.StatusOK || missing.Code != http.StatusOK {
		t.Fatalf("status: other=%d missing=%d", other.Code, missing.Code)
	}
	if other.Body.String() != missing.Body.String() {
		t.Fatalf("归属拒绝与不存在提示不一致:\n other=%s\n missing=%s", other.Body.String(), missing.Body.String())
	}
	if !contains(other.Body.String(), "未找到订单或订单不属于您") {
		t.Fatalf("unexpected body: %s", other.Body.String())
	}
}

// TestBusinessOrderNoSession 无会话记录的 thread_id（旧格式/伪造/过期会话）→ fail-closed
func TestBusinessOrderNoSession(t *testing.T) {
	defer withOrderMocks(t)()
	for _, q := range []string{
		"tenant_id=t_demo&thread_id=t_demo%3Abuyer1%3A99999999&order_id=ORD-2024-001", // 过期会话
		"tenant_id=t_demo&thread_id=t_demo%3Aguest_abc&order_id=ORD-2024-001",         // 旧格式 thread
		"tenant_id=t_demo&thread_id=t_demo%3Abuyer1%3Aforged&order_id=ORD-2024-001",   // 伪造会话
	} {
		out := doOrderReq(t, q, "")
		if out.Code != http.StatusOK || !contains(out.Body.String(), "未找到订单或订单不属于您") {
			t.Fatalf("no-session query=%s status=%d body=%s", q, out.Code, out.Body.String())
		}
	}
}

func TestBusinessOrderRejectsMerchantPreview(t *testing.T) {
	defer withOrderMocks(t)()
	repository.FindCustomerSessionByThread = func(threadID, tenantID string) (model.CustomerSession, error) {
		return model.CustomerSession{TenantID: tenantID, CustomerID: "t_demo:buyer1", ThreadID: threadID, IsPreview: true}, nil
	}
	called := false
	repository.FindOrderForCustomer = func(_, _, _ string) (model.Order, error) {
		called = true
		return model.Order{OrderID: "ORD-2024-001"}, nil
	}
	out := doOrderReq(t, "tenant_id=t_demo&thread_id=t_demo%3Abuyer1%3A20261019&order_id=ORD-2024-001", "")
	if called || !contains(out.Body.String(), "未找到订单或订单不属于您") {
		t.Fatalf("预览会话不能读取订单: %s", out.Body.String())
	}
}

func TestBusinessOrderRequiresGuestLogin(t *testing.T) {
	defer withOrderMocks(t)()
	repository.FindCustomerSessionByThread = func(threadID, tenantID string) (model.CustomerSession, error) {
		return model.CustomerSession{TenantID: tenantID, CustomerID: "t_demo:guest_1", ThreadID: threadID, IsGuest: true}, nil
	}
	called := false
	repository.FindOrderForCustomer = func(_, _, _ string) (model.Order, error) { called = true; return model.Order{}, nil }
	out := doOrderReq(t, "tenant_id=t_demo&thread_id=t_demo%3Aguest_1%3Asession&order_id=ORD-2024-001", "")
	if called || !contains(out.Body.String(), "查询个人订单请先登录") {
		t.Fatalf("访客不能读取订单: %s", out.Body.String())
	}
}

func contains(s, sub string) bool {
	return len(s) >= len(sub) && (func() bool {
		for i := 0; i+len(sub) <= len(s); i++ {
			if s[i:i+len(sub)] == sub {
				return true
			}
		}
		return false
	})()
}
