package handler

import (
	"net/http"
	"strings"

	"customer_service/internal/config"
	"customer_service/internal/middleware"
	"customer_service/internal/model"
	"customer_service/internal/pkg/chat"
	"customer_service/internal/repository"

	"github.com/gin-gonic/gin"
)

// ============ 业务内部接口：给Python Agent调用查询订单 ============

// BusinessOrder is the read-only order adapter for the authenticated Agent.
//
// 归属链路（客户身份不信任客户端传入，以服务端会话记录为准）：
//  1. X-Internal-Token 服务间校验 + thread_id 必须属于请求租户
//  2. 用 thread_id 反查 customer_sessions 得到稳定的 customer_id
//     （thread_id 只表示一段对话，不作为订单归属依据）
//  3. 查询限定 tenant_id + customer_id + order_id 三条件
//  4. 订单不存在与不属于该客户返回相同提示，不暴露订单存在性
func BusinessOrder(c *gin.Context) {
	tenantID := c.Query("tenant_id")
	threadID := c.Query("thread_id")
	orderID := c.Query("order_id")
	if config.GlobalConfig.InternalAPIToken == "" ||
		c.GetHeader("X-Internal-Token") != config.GlobalConfig.InternalAPIToken ||
		tenantID == "" || !strings.HasPrefix(threadID, tenantID+":") ||
		orderID == "" {
		c.JSON(http.StatusForbidden, gin.H{"code": 403, "msg": "订单查询身份校验失败"})
		return
	}

	// customer_id 从服务端会话记录反查：thread_id → customer_sessions → customer_id。
	// 旧格式 thread_id（无会话记录，如历史演示会话）fail-closed，不可查询订单。
	sess, err := repository.FindCustomerSessionByThread(threadID, tenantID)
	if err != nil || sess.TenantID != tenantID || sess.ThreadID != threadID ||
		!strings.HasPrefix(sess.CustomerID, tenantID+":") {
		c.JSON(http.StatusOK, gin.H{
			"code": 404, "msg": "未找到订单或订单不属于您，请确认订单号或转人工客服。",
		})
		return
	}
	// 管理员旧链接只用于体验聊天，不得借预览身份读取任何客户订单。
	if sess.IsPreview {
		c.JSON(http.StatusOK, gin.H{"code": 404, "msg": "未找到订单或订单不属于您，请确认订单号或转人工客服。"})
		return
	}
	if sess.IsGuest {
		c.JSON(http.StatusOK, gin.H{"code": 401, "msg": "查询个人订单请先登录客户账号。"})
		return
	}

	// 三条件限定查询；未命中统一提示（不区分不存在与不属于您）
	order, err := repository.FindOrderForCustomer(tenantID, sess.CustomerID, orderID)
	if err != nil {
		c.JSON(http.StatusOK, gin.H{
			"code": 404, "msg": "未找到订单或订单不属于您，请确认订单号或转人工客服。",
		})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"code": 0,
		"data": gin.H{
			"order_id":    order.OrderID,
			"item":        order.Item,
			"amount":      order.Amount,
			"status":      order.Status,
			"create_date": order.CreateDate,
		},
	})
}

// BusinessOrders lists only orders owned by the customer resolved from a server-side session.
// Preview and guest sessions cannot read personal order data.
func BusinessOrders(c *gin.Context) {
	tenantID := c.Query("tenant_id")
	threadID := c.Query("thread_id")
	if config.GlobalConfig.InternalAPIToken == "" ||
		c.GetHeader("X-Internal-Token") != config.GlobalConfig.InternalAPIToken ||
		tenantID == "" || !strings.HasPrefix(threadID, tenantID+":") {
		c.JSON(http.StatusForbidden, gin.H{"code": 403, "msg": "订单查询身份校验失败"})
		return
	}
	sess, err := repository.FindCustomerSessionByThread(threadID, tenantID)
	if err != nil || sess.TenantID != tenantID || sess.ThreadID != threadID ||
		!strings.HasPrefix(sess.CustomerID, tenantID+":") || sess.IsPreview {
		c.JSON(http.StatusOK, gin.H{"code": 404, "msg": "无法核验客户会话，请重新登录。"})
		return
	}
	if sess.IsGuest {
		c.JSON(http.StatusOK, gin.H{"code": 401, "msg": "查询个人订单请先登录客户账号。"})
		return
	}
	orders, err := repository.ListOrdersForCustomer(tenantID, sess.CustomerID)
	if err != nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"code": 503, "msg": "订单系统暂时不可用"})
		return
	}
	type orderSummary struct {
		OrderID    string `json:"order_id"`
		Item       string `json:"item"`
		Status     string `json:"status"`
		CreateDate string `json:"create_date"`
	}
	items := make([]orderSummary, 0, len(orders))
	for _, order := range orders {
		items = append(items, orderSummary{
			OrderID: order.OrderID, Item: order.Item,
			Status: order.Status, CreateDate: order.CreateDate,
		})
	}
	c.JSON(http.StatusOK, gin.H{"code": 0, "data": gin.H{"orders": items}})
}

// ============ 公开租户列表（用户端租户选择下拉） ============

// PublicTenants 获取公开租户列表：GET /api/auth/tenants
func PublicTenants(c *gin.Context) {
	var rows []model.TenantRow
	if err := config.DB.Model(&model.TenantRow{}).
		Where("status = 1").
		Order("id ASC").Find(&rows).Error; err != nil {
		c.JSON(http.StatusOK, gin.H{"code": 500, "msg": err.Error()})
		return
	}
	type item struct {
		TenantID string `json:"tenant_id"`
		Name     string `json:"name"`
		AppKey   string `json:"app_key"`
	}
	list := make([]item, 0, len(rows))
	for _, r := range rows {
		if r.IsPlatformAdmin {
			continue // 平台管理员租户不对用户端开放
		}
		list = append(list, item{TenantID: r.TenantID, Name: r.Name, AppKey: r.AppKey})
	}
	c.JSON(http.StatusOK, gin.H{"code": 0, "data": list})
}

// ============ 用户聊天对外接口（HTTP 同步，兼容老前端/测试） ============

// UserChatBody 用户聊天请求体
type UserChatBody struct {
	UserID string `json:"user_id"`
	Msg    string `json:"msg"`
}

// UserChat 用户聊天接口：POST /api/user/chat
// thread_id 从服务端客户会话注入（middleware.UserAuth），不信任 body 传入；
// 未启用鉴权的演示模式回退 body.user_id（兼容旧测试）
func UserChat(c *gin.Context) {
	var body UserChatBody
	if err := c.ShouldBindJSON(&body); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"code": 400, "msg": "参数错误"})
		return
	}
	threadID := middleware.ThreadOf(c)
	if threadID == "" {
		threadID = body.UserID // 鉴权关闭时的演示回退
	}
	res := chat.ProcessUserMessage(threadID, threadID, body.Msg, c.GetString(config.TraceIDKey))
	switch res.Kind {
	case "reply":
		c.JSON(http.StatusOK, gin.H{"code": 0, "msg": res.Msg})
	case "ticket":
		c.JSON(http.StatusOK, gin.H{
			"code": 0, "msg": "已提交人工客服，请等待处理",
			"is_human_wait": true, "ticket_no": res.TicketNo, "reason": res.Reason,
		})
	case "human":
		c.JSON(http.StatusOK, gin.H{"code": 0, "msg": res.Msg, "is_human": true})
	case "busy":
		c.JSON(http.StatusOK, gin.H{"code": 0, "msg": res.Msg})
	case "quota":
		c.JSON(http.StatusOK, gin.H{"code": 429, "msg": res.Msg})
	case "error":
		c.JSON(http.StatusOK, gin.H{"code": 500, "msg": "调用agent失败:" + res.Err.Error()})
	}
}

// ============ 用户历史消息 ============

// UserMessages 获取用户历史消息：GET /api/user/messages
// thread_id 从服务端客户会话注入，不信任 query 传入
func UserMessages(c *gin.Context) {
	threadID := middleware.ThreadOf(c)
	if threadID == "" {
		c.JSON(http.StatusOK, gin.H{"code": 400, "msg": "会话身份缺失，请重新登录"})
		return
	}
	msgs, err := repository.ListMessages(threadID)
	if err != nil {
		c.JSON(http.StatusOK, gin.H{"code": 500, "msg": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"code": 0, "data": msgs})
}
