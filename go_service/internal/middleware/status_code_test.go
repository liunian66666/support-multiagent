package middleware

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
)

// ============ StatusCode 中间件回归 ============
//
// 重点锁定无路由（gin NoRoute）场景：
// gin 的 serveError 直接写 c.writermem.status（不经 statusWriter 的
// WriteHeader），且中间件链跑在 serveError 内部的 c.Next() 中、默认错误页
// 正文在链退栈之后才写。若 StatusCode 后处理一律按 200 flush，无路由 404
// 会被误改写为 200 空体（下线 admin 组路由后该路径成为主验证路径）。

func newStatusCodeEngine() *gin.Engine {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(StatusCode())
	return r
}

func doGet(t *testing.T, r *gin.Engine, path string) *httptest.ResponseRecorder {
	t.Helper()
	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, path, nil)
	r.ServeHTTP(w, req)
	return w
}

// body.code>=400 的正常改写行为必须保持
func TestStatusCodeJSONCodeRewrite(t *testing.T) {
	r := newStatusCodeEngine()
	r.GET("/ok", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"code": 500, "msg": "boom"}) })
	r.GET("/bad", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"code": 403, "msg": "denied"}) })
	r.GET("/zero", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"code": 0, "data": "x"}) })

	if w := doGet(t, r, "/ok"); w.Code != http.StatusInternalServerError {
		t.Fatalf("code=500 期望 HTTP 500，实际 %d", w.Code)
	}
	if w := doGet(t, r, "/bad"); w.Code != http.StatusForbidden {
		t.Fatalf("code=403 期望 HTTP 403，实际 %d", w.Code)
	}
	if w := doGet(t, r, "/zero"); w.Code != http.StatusOK {
		t.Fatalf("code=0 期望 HTTP 200，实际 %d", w.Code)
	}
}

// handler 直接用真实状态码的响应必须原样透传
func TestStatusCodeHandlerRealStatusPassthrough(t *testing.T) {
	r := newStatusCodeEngine()
	r.GET("/gone", func(c *gin.Context) {
		c.JSON(http.StatusGone, gin.H{"code": 410, "msg": "公开订单查询已停用"})
	})
	r.GET("/text404", func(c *gin.Context) { c.String(http.StatusNotFound, "not found here") })

	w := doGet(t, r, "/gone")
	if w.Code != http.StatusGone {
		t.Fatalf("期望 410，实际 %d", w.Code)
	}
	if w.Body.String() == "" {
		t.Fatal("410 响应体不应为空")
	}

	w = doGet(t, r, "/text404")
	if w.Code != http.StatusNotFound || w.Body.String() != "not found here" {
		t.Fatalf("期望 404 + 原正文，实际 %d %q", w.Code, w.Body.String())
	}
}

// 200 纯文本响应保持 200 + 原正文
func TestStatusCodePlainText200(t *testing.T) {
	r := newStatusCodeEngine()
	r.GET("/text", func(c *gin.Context) { c.String(http.StatusOK, "hello") })

	w := doGet(t, r, "/text")
	if w.Code != http.StatusOK || w.Body.String() != "hello" {
		t.Fatalf("期望 200 + hello，实际 %d %q", w.Code, w.Body.String())
	}
}

// 核心修复：无路由请求必须返回 404 且带 gin 默认错误页正文，
// 不能被改写为 200 空体
func TestStatusCodeNoRoute404WithBody(t *testing.T) {
	r := newStatusCodeEngine()
	r.GET("/exists", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"code": 0}) })

	w := doGet(t, r, "/api/admin/tenants")
	if w.Code != http.StatusNotFound {
		t.Fatalf("无路由期望 HTTP 404，实际 %d（疑似被 StatusCode 改写）", w.Code)
	}
	if body := w.Body.String(); body != "404 page not found" {
		t.Fatalf("无路由期望默认错误页正文，实际 %q", body)
	}
}

// 外层中间件（注册在 StatusCode 之前，如 RequestLogger）在退栈时
// 必须观测到 404，而不是被提前刷新成的 200
func TestStatusCodeNoRouteObservedByOuterMiddleware(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	var observed int
	r.Use(func(c *gin.Context) {
		c.Next()
		observed = c.Writer.Status()
	})
	r.Use(StatusCode())

	w := doGet(t, r, "/api/admin/menus/tree")
	if observed != http.StatusNotFound {
		t.Fatalf("外层中间件应观测到 404，实际 %d", observed)
	}
	if w.Code != http.StatusNotFound {
		t.Fatalf("客户端应收到 404，实际 %d", w.Code)
	}
}

// /ws/ 与 /metrics 跳过改写的既有行为保持
func TestStatusCodeSkipsWSAndMetrics(t *testing.T) {
	r := newStatusCodeEngine()
	r.GET("/ws/chat", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"code": 500}) })

	w := doGet(t, r, "/ws/chat")
	if w.Code != http.StatusOK {
		t.Fatalf("/ws/ 路径应跳过改写保持 200，实际 %d", w.Code)
	}
}
