# 客户聊天断线基线（2026-10-29）

测试环境：本机 Docker Compose，现有 `main` 提交 `fb5d89b`；演示客户 `t_demo:buyer1` 的新会话 `t_demo:buyer1:20261029014620_cb3b261b`。客户 WebSocket 发出“ORD-2024-003 这个订单的全部内容”后 0.4 秒关闭连接，3 秒后用同一客户会话查询消息。

- 登录 HTTP 200；初始 WebSocket 事件为 `history`；发送后主动关闭。
- `GET /api/customer/messages` HTTP 200；该会话 `user_messages=1`、`ai_messages=0`，最后一条为用户消息。
- 容器日志（令牌已遮盖）：`POST /api/auth/customer-token HTTP/1.1 200 OK`，`WebSocket /ws/chat?token=[REDACTED] [accepted]`，`GET /api/customer/messages HTTP/1.1 200 OK`。当前代码未记录取消事件，因此日志本身无法区分取消与 Agent 耗时。
- 隔离取消探针：`accepted_user_messages=1 agent_task_cancelled=True answer_saved=False`。探针在模拟 Agent 正在等待时取消 `process_ws_chat`，捕获 `CancelledError`；这与 `business/chat/realtime.py` 的 WebSocket `finally` 对连接内任务逐个 `cancel()` 的路径一致。

此基线确认：接收消息与回答处理不独立，断开连接可能留下只有提问没有回答或失败状态的历史记录。
