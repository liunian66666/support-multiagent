package repository

import (
	"time"

	"customer_service/internal/config"
	"customer_service/internal/model"
)

// ============ 客户会话与订单查询（订单归属链路，测试可注入 fake） ============

// FindCustomerSessionByThread 按 thread_id + 租户查找未过期客户会话。
// 订单查询的客户身份经此反查（customer_id 以服务端会话记录为准）。
var FindCustomerSessionByThread = func(threadID, tenantID string) (model.CustomerSession, error) {
	var sess model.CustomerSession
	err := config.DB.Where("thread_id = ? AND tenant_id = ? AND expires_at > ?",
		threadID, tenantID, time.Now()).First(&sess).Error
	return sess, err
}

// FindCustomerSessionByToken 按会话令牌查找未过期客户会话（UserAuth 身份注入用）。
var FindCustomerSessionByToken = func(token string) (model.CustomerSession, error) {
	var sess model.CustomerSession
	err := config.DB.Where("token = ? AND expires_at > ?", token, time.Now()).First(&sess).Error
	return sess, err
}

// FindOrderForCustomer 三条件限定查询订单：租户 + 稳定客户标识 + 订单号。
// 未命中（不存在/不属于该客户）由调用方统一返回相同提示，不暴露订单存在性。
var FindOrderForCustomer = func(tenantID, customerID, orderID string) (model.Order, error) {
	var order model.Order
	err := config.DB.Where("tenant_id = ? AND customer_id = ? AND order_id = ?",
		tenantID, customerID, orderID).First(&order).Error
	return order, err
}

// ListOrdersForCustomer 只返回当前租户、当前客户的最近订单，限制条数避免无限量读取。
var ListOrdersForCustomer = func(tenantID, customerID string) ([]model.Order, error) {
	var orders []model.Order
	err := config.DB.Where("tenant_id = ? AND customer_id = ?", tenantID, customerID).
		Order("id DESC").Limit(20).Find(&orders).Error
	return orders, err
}

// CreateCustomerSession 落库客户会话（演示客户登录用；测试可注入 fake）
var CreateCustomerSession = func(sess *model.CustomerSession) error {
	return config.DB.Create(sess).Error
}

// GetUserWithRoles 查子账号及其角色列表（刷新令牌身份解析用；测试可注入 fake）
var GetUserWithRoles = func(userID uint64) (*model.User, []RoleBrief, error) {
	user, err := GetUserByID(userID)
	if err != nil {
		return nil, nil, err
	}
	return user, GetUserRoles(userID), nil
}
