package middleware

import (
	"strings"
	"time"

	"customer_service/internal/config"
	"customer_service/internal/pkg/logger"

	"github.com/gin-gonic/gin"
)

// retiredAdminPrefix 已摘除的 admin 组路由前缀。
// admin / biz / plat 三个分组共 93 条路由已全部迁移至 FastAPI（python_business），
// 并按 docs/GO_RETIREMENT.md 步骤 1 从 router.go 摘除。
const retiredAdminPrefix = "/api/admin"

// RetiredAdminPaths 观测中间件：只记录、不拦截、不改写响应。
//
// 背景：摘除 admin 组路由后，任何仍直连 /api/admin/* 的外部调用方会从 200 变成 404。
// 本中间件挂在全局链上（路由匹配不到时也会执行），命中已摘除前缀时打一条
// 带 RETIRED_ADMIN_PATH 标记的 Warn 日志，记录 method/path/status/client_ip/
// 是否携带令牌，用于观测期发现漏网流量；对响应本身零影响。
//
// 观测期结束（确认无外部流量）后，可随 admin 相关死代码一并删除。
func RetiredAdminPaths() gin.HandlerFunc {
	return func(c *gin.Context) {
		path := c.Request.URL.Path
		if path != retiredAdminPrefix && !strings.HasPrefix(path, retiredAdminPrefix+"/") {
			c.Next()
			return
		}
		start := time.Now()
		c.Next()

		fields := logger.Fields{
			"method":    c.Request.Method,
			"path":      path,
			"query":     c.Request.URL.RawQuery,
			"status":    c.Writer.Status(),
			"duration":  time.Since(start).Milliseconds(),
			"client_ip": c.ClientIP(),
			"has_auth":  c.GetHeader("Authorization") != "",
		}
		if traceID := c.GetString(config.TraceIDKey); traceID != "" {
			fields["trace_id"] = traceID
		}
		logger.Warn("RETIRED_ADMIN_PATH: admin组已迁移至FastAPI并在Go侧摘除，检测到旧路径访问", fields)
	}
}
