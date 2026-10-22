# Python 业务层模块结构

`python_business/main.py` 保留 `uvicorn main:app` 入口；`business/server.py` 组装路由、中间件、健康检查和指标。业务代码按职责存放：

| 目录 | 职责 |
|---|---|
| `business/core/` | 身份格式常量、错误响应、JSON 请求解析 |
| `business/infra/` | MySQL/Redis 访问与进程内实时状态 |
| `business/customer/` | 客户令牌、本人订单、消息、评价、客户工单 |
| `business/admin/auth.py` | 管理令牌、登录、权限检查与审计辅助函数 |
| `business/admin/catalog.py` | 评价与快捷回复 |
| `business/admin/sessions.py` | 会话、客服状态、客户资料与上传 |
| `business/admin/tickets.py` | 人工工单与审批决策 |
| `business/admin/ops.py` | 运营统计、归档、模型配置与 Agent 代理 |
| `business/admin/rbac.py` | 角色、成员与权限菜单 |
| `business/admin/platform.py` | 租户、模型提供商与平台菜单 |
| `business/admin/content.py` | Prompt、知识库代理与模型诊断 |
| `business/agent/internal.py` | Agent 调用的内部业务接口 |
| `business/chat/realtime.py` | 客户与商家 WebSocket、HTTP 聊天、转人工 |

模块从 `core`/`infra` 向业务层单向引用；认证路由调用 RBAC 与平台函数时采用函数内导入，以避免模块初始化阶段的循环引用。`maintenance.py` 直接引用归档服务，不依赖应用入口。旧契约测试通过 `tests/main_compat.py` 引用各模块中的真实函数，生产代码不使用该适配器。

验收约束：原有 131 条 HTTP/WebSocket 路由的方法、路径、名称和注册顺序由 `tests/test_route_manifest.py` 锁定。启动命令与 Dockerfile 保持 `uvicorn main:app`，服务持久化目录和 Compose 数据卷未改变。
