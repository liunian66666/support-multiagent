package middleware

import (
	"bytes"
	"encoding/json"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
)

// maxBufferSize 缓冲上限：超过则放弃改写直接透传（避免大响应内存占用）
const maxBufferSize = 1 << 20 // 1MB

// statusWriter 包装 gin.ResponseWriter：缓冲响应体，依据 body.code 改写 HTTP 状态码
type statusWriter struct {
	gin.ResponseWriter
	buf        *bytes.Buffer
	written    bool   // 是否已直接写入底层（非 200 或超限）
	passthrough bool  // 透传模式：不缓冲不改写，后续 Write 直接写底层（gin 错误页场景）
	statusCode int    // handler 写入的状态码
	limit      int64
}

func (w *statusWriter) WriteHeader(code int) {
	w.statusCode = code
	if code != http.StatusOK {
		// handler 已用真实状态码，直接透传（后续 Write 也直写底层）
		w.written = true
		w.ResponseWriter.WriteHeader(code)
	}
	// code==200 时暂不写，等待 body 判断
}

func (w *statusWriter) Write(b []byte) (int, error) {
	if w.written || w.passthrough {
		return w.ResponseWriter.Write(b)
	}
	if int64(w.buf.Len()+len(b)) > w.limit {
		// 超限：放弃改写，按 200 透传
		w.flushAs(http.StatusOK)
		return w.ResponseWriter.Write(b)
	}
	return w.buf.Write(b)
}

// flushAs 按指定状态码把缓冲内容写入底层
func (w *statusWriter) flushAs(code int) {
	if w.written {
		return
	}
	w.written = true
	if w.statusCode != http.StatusOK && code == http.StatusOK {
		code = w.statusCode
	}
	w.ResponseWriter.WriteHeader(code)
	_, _ = w.ResponseWriter.Write(w.buf.Bytes())
}

// codeToStatus 业务 code → 真实 HTTP 状态码
func codeToStatus(code float64) int {
	switch {
	case code >= 500:
		return http.StatusInternalServerError
	case code >= 400:
		// 400/401/403/404/405/409/429 直接透传；其余 4xx 统一 400
		switch int(code) {
		case http.StatusBadRequest, http.StatusUnauthorized, http.StatusForbidden,
			http.StatusNotFound, http.StatusMethodNotAllowed, http.StatusConflict,
			http.StatusTooManyRequests:
			return int(code)
		default:
			return http.StatusBadRequest
		}
	default:
		return 0 // 不改写
	}
}

// StatusCode 响应状态码归一化中间件（P2-1）：
// 将历史上 handler 返回的 "200 + body.code=4xx/5xx" 规范为真实 HTTP 状态码，
// 业务方仍按 body.code 判断，HTTP 语义同时准确（前端 fetch 可感知 4xx/5xx）。
func StatusCode() gin.HandlerFunc {
	return func(c *gin.Context) {
		p := c.Request.URL.Path
		// WebSocket 升级与 metrics 不适用 JSON 改写
		if strings.HasPrefix(p, "/ws/") || p == "/metrics" {
			c.Next()
			return
		}
		w := &statusWriter{ResponseWriter: c.Writer, buf: &bytes.Buffer{}, statusCode: http.StatusOK, limit: maxBufferSize}
		c.Writer = w
		c.Next()

		if w.written {
			return
		}
		// 解析 body.code 改写状态码；解析失败（非 JSON/无 code）按原状态透传
		var payload struct {
			Code float64 `json:"code"`
		}
		if err := json.Unmarshal(w.buf.Bytes(), &payload); err != nil {
			// 非 JSON 体：按底层真实状态透传。
			// 注意 gin 的 NoRoute/405 流程：serveError 先直接写 c.writermem.status
			// （不经本包装器的 WriteHeader），整个中间件链在 serveError 内部的
			// c.Next() 中运行，默认错误页正文在链退栈之后才写入。若此处一律按
			// 200 flush，会把无路由 404 误改写为 200 空体，且 gin 随后认为响应
			// 已写入而跳过错误页。因此：
			//   - 底层非 200 且正文未到（gin 稍后补写）→ 进透传模式，不 flush
			//   - 底层非 200 且已有正文 → 按底层状态连正文一起落盘
			if st := w.ResponseWriter.Status(); st != http.StatusOK {
				if w.buf.Len() == 0 {
					w.passthrough = true
					return
				}
				w.flushAs(st)
				return
			}
			w.flushAs(http.StatusOK)
			return
		}
		if st := codeToStatus(payload.Code); st > 0 {
			w.flushAs(st)
			return
		}
		w.flushAs(http.StatusOK)
	}
}
