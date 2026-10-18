package handler

import (
	"net/http"
	"strings"

	"customer_service/internal/config"
	"customer_service/internal/model"
	"customer_service/internal/pkg/chat"
	"customer_service/internal/repository"

	"github.com/gin-gonic/gin"
)

// ============ 业务内部接口：给Python Agent调用查询订单 ============

// BusinessOrder is the read-only order adapter for the authenticated Agent.
//
// 订单归属校验：order.customer_id 必须等于请求 thread_id（即 tid:visitor），
// 且 order.tenant_id 必须等于请求 tenant_id。命中返回状态，不命中返回 fail-closed
// 文本，不暴露订单存在性给非归属用户。演示订单数据已明确标注为演示用，
// 不构成真实商家业务答复。
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

	var order model.Order
	err := config.DB.Where("order_id = ?", orderID).First(&order).Error
	if err != nil {
		// 订单不存在：fail-closed，不区分"不存在"与"不属于您"
		c.JSON(http.StatusOK, gin.H{
			"code": 404, "msg": "未找到订单或订单不属于您，请确认订单号或转人工客服。",
		})
		return
	}
	// 归属校验：tenant + customer 双重比对
	if order.TenantID != tenantID || order.CustomerID != threadID {
		c.JSON(http.StatusOK, gin.H{
			"code": 403, "msg": "订单不属于您，无法核验状态；如需协助请转人工客服。",
		})
		return
	}
	// 命中：返回订单状态
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
func UserChat(c *gin.Context) {
	var body UserChatBody
	if err := c.ShouldBindJSON(&body); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"code": 400, "msg": "参数错误"})
		return
	}
	threadID := body.UserID
	res := chat.ProcessUserMessage(threadID, body.UserID, body.Msg, c.GetString(config.TraceIDKey))
	switch res.Kind {
	case "reply":
		c.JSON(http.StatusOK, gin.H{"code": 0, "msg": res.Msg})
	case "ticket":
		c.JSON(http.StatusOK, gin.H{
			"code": 0, "msg": "已提交人工客服，请等待处理",
			"is_human_wait": true, "ticket_no": res.TicketNo,
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
func UserMessages(c *gin.Context) {
	threadID := c.Query("thread_id")
	if threadID == "" {
		c.JSON(http.StatusOK, gin.H{"code": 400, "msg": "缺少 thread_id"})
		return
	}
	msgs, err := repository.ListMessages(threadID)
	if err != nil {
		c.JSON(http.StatusOK, gin.H{"code": 500, "msg": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"code": 0, "data": msgs})
}
