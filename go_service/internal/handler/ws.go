package handler

import (
	"context"
	"customer_service/internal/config"
	"customer_service/internal/middleware"
	"customer_service/internal/pkg/agent"
	"customer_service/internal/pkg/auth"
	"customer_service/internal/pkg/ws"
	"customer_service/internal/repository"
	"customer_service/internal/service"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
)

var upgrader = websocket.Upgrader{
	// 开发环境放行跨域；生产由网关层处理
	CheckOrigin: func(r *http.Request) bool { return true },
}

// handleUserWS 用户端 WebSocket：/ws/chat
// 身份来自服务端客户会话（middleware.UserAuth 注入 thread_id），不信任 query 传入；
// 未启用鉴权的演示模式回退 query user_id（兼容旧前端/测试）
// 客户端消息协议：{"type":"chat","msg":"..."} / {"type":"ping"}
func HandleUserWS() gin.HandlerFunc {
	return func(c *gin.Context) {
		userID := middleware.ThreadOf(c)
		if userID == "" {
			userID = c.Query("user_id") // 鉴权关闭时的演示回退
		}
		if userID == "" {
			c.JSON(http.StatusBadRequest, gin.H{"code": 400, "msg": "缺少会话身份"})
			return
		}

		// P0-2：WebSocket 连接数限制（单 IP + 单 user_id）
		clientIP := c.ClientIP()
		allowed, reason := ws.GlobalWSLimiter.TryAcquireConn(clientIP, userID)
		if !allowed {
			c.JSON(http.StatusTooManyRequests, gin.H{"code": 429, "msg": reason})
			return
		}
		defer ws.GlobalWSLimiter.ReleaseConn(clientIP, userID)

		conn, err := upgrader.Upgrade(c.Writer, c.Request, nil)
		if err != nil {
			return
		}

		// P1-2：租户并发会话配额（实时连接数）校验，超限拒绝并通知前端停止重连
		tenantID := auth.TenantFromThreadID(userID)
		if quota, qErr := repository.GetTenantQuota(tenantID); qErr == nil && quota.MaxConcurrentSessions > 0 {
			current := ws.WSHub.CountTenantConnsExcluding(tenantID, userID)
			if current >= quota.MaxConcurrentSessions {
				msg := "{\"type\":\"error\",\"code\":\"concurrency_full\",\"msg\":\"当前在线会话数已达上限（" + strconv.Itoa(quota.MaxConcurrentSessions) + "），请稍后再试\"}"
				_ = conn.WriteMessage(websocket.TextMessage, []byte(msg))
				_ = conn.Close()
				return
			}
		}

		cl := ws.WSHub.RegisterUser(userID, tenantID, conn)
		defer ws.WSHub.UnregisterUser(userID, cl)
		go cl.WriteLoop()

		// 读超时：客户端异常断开（未发 close 帧）时及时释放连接与租户配额
		// 前端每 30s 心跳，90s 未收到任何消息视为断开
		_ = conn.SetReadDeadline(time.Now().Add(90 * time.Second))

		// 连接建立后统一推送历史消息（空历史也发，前端据此渲染"暂无历史/欢迎语"）
		if msgs, err := repository.ListMessages(userID); err == nil {
			ws.WSHub.SendToUser(userID, map[string]interface{}{
				"type": "history", "messages": msgs,
			})
		}

		for {
			_ = conn.SetReadDeadline(time.Now().Add(90 * time.Second))
			_, data, err := conn.ReadMessage()
			if err != nil {
				return
			}
			var msg struct {
				Type    string `json:"type"`
				Msg     string `json:"msg"`
				MsgType string `json:"msg_type"` // text / image（image 时 msg 为图片 URL）
			}
			if err := json.Unmarshal(data, &msg); err != nil {
				continue
			}
			switch msg.Type {
			case "ping":
				ws.WSHub.SendToUser(userID, map[string]interface{}{"type": "pong"})
			case "read":
				// P1-1：用户上报已读，标记该会话所有非用户消息为已读
				// 并通过管理端 WebSocket 推送已读回执
				if err := repository.MarkMessagesRead(userID); err == nil {
					tenantID := auth.TenantFromThreadID(userID)
					ws.WSHub.SendToAdminsByTenant(tenantID, map[string]interface{}{
						"type":      "read_receipt",
						"thread_id": userID,
						"timestamp": time.Now().Unix(),
					})
				}
			case "chat":
				if msg.Msg == "" {
					continue
				}
				// P0-2：消息发送频率限制（每秒最多 5 条）
				allowed, reason := ws.GlobalWSLimiter.TryAcquireMsg(userID)
				if !allowed {
					ws.WSHub.SendToUser(userID, map[string]interface{}{
						"type": "rate_limit", "msg": reason,
					})
					continue
				}
				traceID := c.GetString(config.TraceIDKey)

				// P1/P1.5：用户发送图片/视频消息（msg_type=image/video，msg 为上传后的 URL）
				// AI 阶段当前模型不支持识图/看视频：落库（客服历史可见）+ 提示引导转人工，不调用 agent；
				// 人工接管中：直接转人工客服查看
				if msg.MsgType == "image" || msg.MsgType == "video" {
					_ = repository.SaveMessage(userID, "user", msg.Msg, msg.MsgType)
					htk, _ := repository.GetHandlingTicket(userID)
					if htk != nil {
						_ = repository.AssignLastUserMessage(userID, htk.ID)
						ws.WSHub.SendToAdminsByTenant(auth.TenantFromThreadID(userID), map[string]interface{}{
							"type": "user_msg", "thread_id": userID, "user_id": userID, "msg": msg.Msg, "msg_type": msg.MsgType,
						})
						ws.WSHub.SendToUser(userID, map[string]interface{}{
							"type": "human_notice", "msg": "您当前由人工客服接待，文件已转给人工客服",
						})
					} else {
						ws.WSHub.SendToUser(userID, map[string]interface{}{
							"type": "notice", "msg": "图片/视频已收到，当前 AI 暂不支持查看内容，如需发送请转人工客服",
						})
					}
					continue
				}

				// P2-1：流式输出处理
				// 1. 落库用户消息
				_ = repository.SaveMessage(userID, "user", msg.Msg, "text")

				// 2. 人工接管中：消息归属当前工单，直接转人工
				htk, _ := repository.GetHandlingTicket(userID)
				if htk != nil {
					_ = repository.AssignLastUserMessage(userID, htk.ID)
					ws.WSHub.SendToAdminsByTenant(auth.TenantFromThreadID(userID), map[string]interface{}{
						"type": "user_msg", "thread_id": userID, "user_id": userID, "msg": msg.Msg,
					})
					ws.WSHub.SendToUser(userID, map[string]interface{}{
						"type": "human_notice", "msg": "您当前由人工客服接待，消息已转给人工客服，请稍候",
					})
					continue
				}

				// 3. 线程级串行锁
				if !ws.WSHub.TryLockThread(userID) {
					ws.WSHub.SendToUser(userID, map[string]interface{}{
						"type": "notice", "msg": "上一条消息还在处理中，请稍候再发送",
					})
					continue
				}

				// P1-2：每日消息数配额校验（超限直接拒绝，需先释放线程锁）
				if quota, qErr := repository.CheckDailyMessageQuota(auth.TenantFromUserID(userID)); qErr == nil && !quota.OK {
					ws.WSHub.UnlockThread(userID)
					ws.WSHub.SendToUser(userID, map[string]interface{}{
						"type": "quota", "msg": "今日咨询量已达上限，请明天再试或联系人工客服",
					})
					continue
				}

				fullContent := ""
				pending, _ := repository.HasPendingTicket(userID)

				// ✅ 继承WS请求的context，连接断开自动取消SSE请求
				ctx := c.Request.Context()

				// ✅ 新开goroutine跑SSE流式，**不阻塞WS主循环**
				// ✅ 线程锁在子协程内释放：流式结束/异常/中断后自动解锁，避免占用后续消息
				go func() {
					// 子协程内执行SSE
					defer ws.WSHub.UnlockThread(userID)
					streamErr := agent.CallAgentChatStream(ctx, userID, msg.Msg, traceID, pending,
						// onChunk: 逐 token 推送给前端
						func(chunk string) {
							fullContent += chunk
							ws.WSHub.SendToUser(userID, map[string]interface{}{
								"type": "chat_chunk", "chunk": chunk,
							})
						},
						// onEnd: 流式结束，推送完整消息并落库
						func(content string) {
							if htk, _ := repository.GetHandlingTicket(userID); htk != nil {
								_ = repository.SaveTicketMessage(userID, "ai", content, "text", htk.ID)
							} else {
								_ = repository.SaveMessage(userID, "ai", content, "text")
							}
							ws.WSHub.SendToUser(userID, map[string]interface{}{
								"type": "chat_end", "msg": content,
							})
						},
						// onError: Agent业务侧返回SSE error事件，仅这里推送业务错误
						func(err error) {
							ws.WSHub.SendToUser(userID, map[string]interface{}{
								"type": "error", "msg": "调用agent失败:" + err.Error(),
							})
						},
						// onInterrupt: 人工接管中断，复用/创建工单（事务+行锁，与 HTTP 通道共用）
						func(interruptData map[string]interface{}) {
							reason := ""
							if interruptData != nil {
								if r, ok := interruptData["reason"].(string); ok {
									reason = r
								}
							}
							t, reused, err := service.EnsureTicket(userID, userID, reason)
							if err != nil {
								ws.WSHub.SendToUser(userID, map[string]interface{}{
									"type": "error", "msg": "创建工单失败:" + err.Error(),
								})
								return
							}
							if !reused {
								_ = repository.SaveTicketMessage(userID, "system", "已提交人工客服，工单号："+t.TicketNo, "text", t.ID)
								// 转接诉求消息归属新工单
								_ = repository.AssignLastUserMessage(userID, t.ID)
								ws.WSHub.SendToAdminsByTenant(auth.TenantFromUserID(userID), map[string]interface{}{
									"type": "new_ticket",
									"ticket": map[string]interface{}{
										"id": t.ID, "ticket_no": t.TicketNo, "thread_id": userID,
										"user_id": userID, "reason": reason, "status": t.Status,
										"created_at": t.CreatedAt.Format(time.RFC3339),
									},
								})
							}
							ws.WSHub.SendToUser(userID, map[string]interface{}{
								"type": "ticket", "ticket_no": t.TicketNo,
								"reused": reused, "reason": reason,
								"msg": "已提交人工客服，请等待处理",
							})
							direct, _ := interruptData["direct_handoff"].(bool)
							if !direct {
								_, _ = agent.CallAgentResume(agent.ResumeReq{
									ThreadID: userID, Confirmed: true, WaitTime: 3, TicketID: t.TicketNo,
								}, traceID)
							}
						},
					)
					// ✅ 只处理【网络层面错误】，**排除ctx取消（用户主动关闭页面，不需要推送错误）**
					if streamErr != nil {
						// context取消属于正常关闭，不向前端报错
						if !errors.Is(streamErr, context.Canceled) && !errors.Is(streamErr, context.DeadlineExceeded) {
							ws.WSHub.SendToUser(userID, map[string]interface{}{
								"type": "error", "msg": "agent流式连接异常:" + streamErr.Error(),
							})
						}
					}
				}()

			}
		}
	}
}

// handleAdminWS 管理端 WebSocket：/ws/admin?token=xxx
// 用于实时接收：新工单(new_ticket)、工单状态变更(ticket_update)、用户消息(user_msg)、人工回复(human_reply)
// 按 token 租户订阅：仅收到本租户的推送
func HandleAdminWS() gin.HandlerFunc {
	return func(c *gin.Context) {
		conn, err := upgrader.Upgrade(c.Writer, c.Request, nil)
		if err != nil {
			return
		}
		tid := middleware.TenantOf(c)
		uid := middleware.UserIDOf(c)
		// 子账号客服上线：置在线（app_key 管理员 uid=0 不进在线池，不参与自动分配）
		if uid > 0 {
			_ = repository.UpsertAgentStatus(tid, uid, 1)
		}
		cl := ws.WSHub.RegisterAdmin(conn, tid)
		defer func() {
			ws.WSHub.UnregisterAdmin(cl)
			if uid > 0 {
				_ = repository.UpsertAgentStatus(tid, uid, 0)
			}
		}()
		go cl.WriteLoop()

		// 管理端只接收心跳/忽略客户端消息；60s 无任何消息判定离线（前端每 25s ping 保活）
		for {
			_ = conn.SetReadDeadline(time.Now().Add(60 * time.Second))
			if _, _, err := conn.ReadMessage(); err != nil {
				return
			}
		}
	}
}

// AgentWSBroadcast 内部接口：POST /api/agent/ws/broadcast（InternalAuth）
//
// 并行迁移期 FastAPI 业务层已接管部分工单写操作，但客户/管理端的 WebSocket 长连接
// 仍由本进程的 Hub 持有，因此由 FastAPI 完成落库后回调本接口转发推送，
// 保证「只有一个 WS Hub、只有一份推送语义」。
//
// body: {"target":"user"|"admins"|"admins_tenant","key":"<thread_id>",
//        "tenant_id":"<tid>","payload":{"type":"...",...}}
func AgentWSBroadcast(c *gin.Context) {
	var body struct {
		Target   string                 `json:"target"`
		Key      string                 `json:"key"`
		TenantID string                 `json:"tenant_id"`
		Payload  map[string]interface{} `json:"payload"`
	}
	if err := c.ShouldBindJSON(&body); err != nil || body.Payload == nil {
		c.JSON(http.StatusBadRequest, gin.H{"code": 400, "msg": "参数错误"})
		return
	}
	switch body.Target {
	case "user":
		if body.Key == "" {
			c.JSON(http.StatusBadRequest, gin.H{"code": 400, "msg": "参数错误：key 不能为空"})
			return
		}
		ws.WSHub.SendToUser(body.Key, body.Payload)
	case "admins":
		ws.WSHub.SendToAdmins(body.Payload)
	case "admins_tenant":
		ws.WSHub.SendToAdminsByTenant(body.TenantID, body.Payload)
	default:
		c.JSON(http.StatusBadRequest, gin.H{"code": 400, "msg": "参数错误：target 非法"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"code": 0, "msg": "ok"})
}

// AgentExtractRulesReload 内部接口：POST /api/agent/extract-rules/reload
// 并行期专用 —— 提取规则改由 FastAPI 层写入后，Go 侧进程内缓存不会自动感知，
// 由 FastAPI 在写入后调用本接口主动失效，保证两边读到同一份规则。
func AgentExtractRulesReload(c *gin.Context) {
	repository.InvalidateExtractRulesCache()
	c.JSON(http.StatusOK, gin.H{"code": 0, "msg": "ok"})
}

// AgentMenusReload 内部接口：POST /api/agent/menus/reload
// 并行期专用 —— 菜单与角色权限改由 FastAPI 层写入后，Go 侧的 menuCache /
// rolePermsCache 不会自动感知，由 FastAPI 在写入后调用本接口主动失效。
func AgentMenusReload(c *gin.Context) {
	repository.InvalidateMenuCaches()
	c.JSON(http.StatusOK, gin.H{"code": 0, "msg": "ok"})
}

// AgentWSConns 内部接口：GET /api/agent/ws/conns
// 返回各租户当前 WebSocket 连接数，供 FastAPI 侧拼装租户列表的 active_sessions
// （Go 的 fetchTenantUsage 用 ws hub 实时连接数覆盖 Agent 统计值，口径必须一致）。
func AgentWSConns(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"code": 0, "data": ws.WSHub.CountAllTenantConns()})
}

// AgentTenantsReload 内部接口：POST /api/agent/tenants/reload
// 并行期专用 —— 租户的增删改/启停/重置密钥改由 FastAPI 层写入后，Go 侧的
// auth 内存缓存（tenantByKey / TenantsList）仍是旧快照，会导致：
//   1) 新建租户的 app_key 登录失败（缓存里没有）
//   2) 已禁用租户的旧 token 仍能通过校验
// 由 FastAPI 在写完租户后调用本接口强制重载。
func AgentTenantsReload(c *gin.Context) {
	if err := auth.RefreshTenants(); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"code": 500, "msg": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"code": 0, "msg": "ok"})
}
