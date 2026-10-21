package router

import (
	"net/http"
	"os"
	"strings"
	"time"

	"customer_service/internal/handler"
	"customer_service/internal/middleware"

	"github.com/gin-contrib/cors"
	"github.com/gin-gonic/gin"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// corsAllowedOrigins 从环境变量 CORS_ALLOW_ORIGINS（逗号分隔）读取跨域白名单；
// 未配置时默认仅放行本地开发地址（生产环境必须显式配置，同源访问不受 CORS 限制）
func corsAllowedOrigins() []string {
	raw := os.Getenv("CORS_ALLOW_ORIGINS")
	if raw == "" {
		return []string{"http://localhost:8080", "http://127.0.0.1:8080"}
	}
	var out []string
	for _, o := range strings.Split(raw, ",") {
		if o = strings.TrimSpace(o); o != "" {
			out = append(out, o)
		}
	}
	if len(out) == 0 {
		return []string{"http://localhost:8080", "http://127.0.0.1:8080"}
	}
	return out
}

// SetupRoutes 注册所有路由
func SetupRoutes(r *gin.Engine) {
	// ============ 全局中间件 ============
	// P0-3：CORS 收紧为白名单（同源 iframe 嵌入不受影响；第三方 XHR 直连需配置 CORS_ALLOW_ORIGINS）
	allowOrigins := corsAllowedOrigins()
	r.Use(cors.New(cors.Config{
		AllowOrigins:     allowOrigins,
		AllowMethods:     []string{"GET", "POST", "PUT", "DELETE", "OPTIONS"},
		AllowHeaders:     []string{"*"},
		ExposeHeaders:    []string{"X-Trace-Id"},
		MaxAge:           12 * time.Hour,
		AllowCredentials: false,
	}))
	r.Use(middleware.Tracing())
	r.Use(middleware.Trace())
	r.Use(middleware.PrometheusMetrics())
	r.Use(middleware.RequestLogger())
	// P2-1：响应状态码归一化（body.code>=400 改写真实 HTTP 状态码；WS/metrics 跳过）
	r.Use(middleware.StatusCode())
	// 下线观测：admin/biz/plat 三组共 93 条路由已 100% 迁移至 FastAPI（python_business）
	// 并从本文件摘除（见 docs/GO_RETIREMENT.md 步骤 1）。本中间件只记录不拦截：
	// 若仍有外部调用方直连 /api/admin/*，会打 RETIRED_ADMIN_PATH 日志，响应仍为 404。
	r.Use(middleware.RetiredAdminPaths())

	// ============ Prometheus metrics 端点 ============
	r.GET("/metrics", gin.WrapH(promhttp.Handler()))

	// ============ 业务内部接口：给Python Agent调用查询订单 ============
	// Preserve an explicit retirement response for old clients; no order data is returned.
	r.GET("/api/business/order", func(c *gin.Context) {
		c.JSON(http.StatusGone, gin.H{"code": 410, "msg": "公开订单查询已停用；请通过客服会话核验身份"})
	})

	// ============ 内部接口：给 Python Agent 调用查询租户模型配置 ============
	agentInternal := r.Group("/api/agent", middleware.InternalAuth())
	{
		agentInternal.GET("/model-config", handler.AgentModelConfig)
		agentInternal.GET("/order", handler.BusinessOrder)
		agentInternal.GET("/orders", handler.BusinessOrders)
		// P1-2：Agent 配额校验用（返回租户配额数值）
		agentInternal.GET("/tenant-quota", handler.AgentTenantQuota)
		agentInternal.GET("/tenant-concurrency", handler.AgentTenantConcurrency)
		// 会话摘要：agent 后台任务拉候选会话 / 回写摘要
		agentInternal.GET("/session-summaries/pending", handler.AgentSessionSummaryPending)
		agentInternal.POST("/session-summaries", handler.AgentSessionSummarySave)
		// 并行迁移期：FastAPI 业务层完成工单写操作后，借本进程 WS Hub 推送
		// （客户/管理端长连接只在本进程，不能由 FastAPI 自行推送）
		agentInternal.POST("/ws/broadcast", handler.AgentWSBroadcast)
		// 并行期专用：提取规则由 FastAPI 层写入后通知本进程失效内存缓存
		agentInternal.POST("/extract-rules/reload", handler.AgentExtractRulesReload)
		// 并行期专用：菜单/角色权限由 FastAPI 层写入后失效本进程的 menuCache + rolePermsCache
		agentInternal.POST("/menus/reload", handler.AgentMenusReload)
		// 并行期专用：租户列表的 active_sessions 取 ws hub 实时连接数（与 Go 口径一致）
		agentInternal.GET("/ws/conns", handler.AgentWSConns)
		// 并行期专用：租户写操作后重载 Go 的 auth 内存缓存（否则新租户登不进、禁用租户旧 token 仍有效）
		agentInternal.POST("/tenants/reload", handler.AgentTenantsReload)
	}

	// ============ WebSocket：用户聊天长连接 ============
	r.GET("/ws/chat", middleware.UserAuth(), handler.HandleUserWS())

	// ============ WebSocket：管理端实时推送 ============
	r.GET("/ws/admin", middleware.AdminAuth(), handler.HandleAdminWS())

	// ============ 鉴权：宿主/管理端换票 ============
	r.GET("/api/auth/public-key", handler.PublicKeyHandler)
	r.POST("/api/auth/token", handler.IssueTokenHandler)
	r.POST("/api/auth/user-token", handler.UserTokenHandler)
	r.POST("/api/auth/refresh", handler.RefreshTokenHandler)
	r.POST("/api/auth/logout", handler.LogoutHandler)
	// 演示客户登录（仅 config.Auth.DemoCustomerLogin 启用）：签发短期客户会话令牌
	r.POST("/api/auth/customer-token", handler.CustomerTokenHandler)
	r.POST("/api/auth/guest-token", handler.GuestTokenHandler)
	r.POST("/api/auth/merchant-preview-token", handler.MerchantPreviewTokenHandler)
	// 修改自己密码（平台管理员/租户管理员，需登录态）
	r.PUT("/api/auth/password", middleware.AdminAuth(), handler.ChangePasswordHandler)

	// ============ 公开租户列表 ============
	r.GET("/api/auth/tenants", handler.PublicTenants)

	// ============ 用户聊天对外接口 ============
	r.POST("/api/user/chat", middleware.UserAuth(), handler.UserChat)
	// P1：用户端上传图片/视频（发送 msg_type=image/video 消息前先上传拿 URL）
	r.POST("/api/user/upload", middleware.UserAuth(), handler.UserSessionMediaUpload)

	// ============ 用户历史消息 ============
	r.GET("/api/user/messages", middleware.UserAuth(), handler.UserMessages)

	// ============ 用户满意度评价 ============
	r.POST("/api/user/rating", middleware.UserAuth(), handler.UserSubmitRating)

	// ============ 人工客服完整后台（admin/biz/plat 共 93 条） ============
	// 已 100% 迁移至 FastAPI（python_business，阶段 3-1 ~ 3-7），路由注册已摘除；
	// 前端 admin-app/src/api.js 的 PY_ADMIN_PREFIXES 已把全部 admin 前缀
	// 改写到 /py-api/。观测中间件 RetiredAdminPaths 会记录直连旧路径的流量。
	// 下线依据与剩余步骤见 docs/GO_RETIREMENT.md。
}
