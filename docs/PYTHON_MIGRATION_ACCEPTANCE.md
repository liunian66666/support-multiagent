# Go 业务服务迁移至 Python：验收记录

本记录对应 2026-10-21 的本地工作区和 Docker Compose 环境。运行入口为 Nginx → Python FastAPI 业务层 → Python Agent；`go_service/` 已从当前源码移除，历史源码仍在 Git 历史中。客户页面、SDK 与 Vue 商家后台保留原有入口。

## 迁移范围

| 阶段 | Python 接管的能力 | 验收结果 |
|---|---|---|
| 业务接口 | 客户会话、只读订单、商家认证与管理、工单、Agent 内部配置和通知 | 业务层单元测试 329 项通过；公开网关路由检查通过 |
| 实时聊天 | 客户及商家 WebSocket、FAQ 流式回复、转人工通知 | 经 Nginx 实测 FAQ 得到 `chat_end`；转人工产生工单，同租户商家收到通知，异租户商家未收到 |
| 持久化 | 客户消息和会话、重启后恢复、每日消息归档 | 重启业务容器后使用同一访客会话读取历史成功；维护服务归档任务启动成功 |
| 部署 | Nginx、Agent、业务服务、维护服务、MySQL、PostgreSQL、Qdrant、Redis | `docker compose config --quiet` 通过；运行容器中没有 Go 服务 |
| 初始化 | 新库租户、权限菜单、演示数据 | 隔离测试库中首次初始化和重复初始化通过；重复运行不覆盖租户密钥；临时库已删除 |

Agent 单元测试 66 项通过。客户订单归属实测：`buyer1` 可读本人演示订单，`buyer2` 查询同一订单被拒。客户页面实测通用 FAQ 有回复。当前 `t_admin` 和 `t_demo` 实际对话模型均为 `qwen3.8-flash`；浏览器问“单笔订单满多少元包邮？”后返回 99 元门槛及例外条件。

这些结果是本地实测，不代表 GitLab CI 已运行，也不代表真实商家订单接口已接入。页面及路由检查覆盖了迁移主链路，不等于所有管理功能都做了逐项人工验收。

## 保留的业务边界

- 订单为演示数据；真实商家接入需实现可信客户身份映射和真实订单只读适配器。
- 退款和支付执行工具保持关闭。开放前需要真实业务写接口、订单归属复核、幂等控制、独立审批和审计。
- 演示客户登录仅用于虚构数据环境，不能证明访问者是某个真实买家。
- `docs/GO_RETIREMENT.md` 保留为迁移前历史分析，其中路由与组件状态不代表当前部署。

## 可重复检查

```bash
docker compose config --quiet
docker compose run --rm --no-deps business python -m unittest discover -s tests -q
docker compose exec -T business python verify_gateway_routes.py
docker compose exec -T business python verify_order_ownership.py
docker compose exec -T -e CHAT_BACKEND=http://nginx business python verify_realtime_live.py
docker compose exec -T -e CHAT_BACKEND=http://nginx business python verify_handoff_live.py
docker compose exec -T business python verify_session_restart.py prepare
docker compose restart business
docker compose exec -T business python verify_session_restart.py check
```

最后两条会重启业务容器，需在允许短暂中断的本地或测试环境执行。
