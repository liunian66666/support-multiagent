# 客服 Agent 当前实现与验收

本页记录 2026-10-21 实际运行的实现；[MULTI_AGENT_ARCHITECTURE.md](MULTI_AGENT_ARCHITECTURE.md) 是此前的设计讨论，不应作为当前接口契约。

## 单轮执行

客户身份由 FastAPI 业务层签发的会话决定。业务层向 Agent 传入受信任的租户、thread 和内部令牌；Agent 不从用户消息推断身份。JSON `/agent/chat` 与 SSE `/agent/chat/stream` 都调用 `services/supervisor.py:decide_turn`，然后调用 `services/specialists.py:execute_shortcut`。模型路径只在专职角色需要时启动。所有结果写回现有 PostgreSQL checkpointer，业务消息仍由业务层保存。

| 角色 | 触发与职责 | 能力边界 |
| --- | --- | --- |
| Supervisor | 规则判定、同线程订单号回溯、只读复合任务计划 | 不查业务数据、不建工单、不调用支付或退款写操作 |
| Intake Agent | 问候与模糊请求澄清 | 独立模型图，工具列表为空 |
| Knowledge Agent | 店铺政策与 FAQ | 独立模型图，仅有当前租户的 `search_kb`；检索结果来自 Qdrant |
| Order Specialist | 本人订单列表与指定订单只读查询 | 使用业务层的归属校验接口；访客与未命中按结构化错误码处理 |
| Composer | 至多一项订单读取及两项租户知识检索，再汇总答复 | 订单号从公开知识检索词中删除；无写工具 |
| Case Specialist | 明确转人工、退款或支付执行请求、不可自助核验的订单错误 | 只返回移交请求；FastAPI 业务层按会话创建或复用工单 |

“多 Agent”在这里是**有不同模型与工具权限的专职角色，加上确定性的订单、工单、汇总执行器**。不是六个模型互相自由对话。旧的全工具 `get_tenant_agent` 仍只供已有审批检查点的 `/agent/resume` 兼容使用；新聊天入口不使用它。退款和支付执行工具没有开放，相关请求转人工。

## 状态和追踪

- 会话由业务层的 MySQL/Redis 保存；跨轮 Agent 消息由 PostgreSQL checkpointer 保存。专职模型图本身无独立会话记忆，调用时读取最近消息。
- `agent_trace_runs` 存每轮路由、专职角色、状态、耗时和最多 64 个结构化事件，包括检索证据 ID、模型 token 用量。原问题、提示词、工具参数及密钥不写入事件。
- 客户 HTTP 回复与 WebSocket 终帧带 `trace_id`。内部接口 `GET /agent/traces/{trace_id}` 需要内部令牌及租户 ID，跨租户返回 404。
- SSE 的用量在 `end` 帧之前写入，避免客户收到结束帧后断开导致统计遗漏。

## 本地验收

保留已有数据卷，只重建和启动 Agent、business 镜像。以下验证在 2026-10-21 的本地 Compose 服务上通过：

- Agent 单测 76 个；FastAPI business 单测 329 个。
- `python_business/verify_order_ownership.py`：演示买家查本人订单成功，另一买家无法取得该订单。
- `python_business/verify_multi_agent_live.py`：本人订单、订单＋退货政策复合问题、访客 FAQ、退款执行请求移交；每轮 trace 可查、跨租户不可查。
- `python_business/verify_handoff_live.py`：客户建单、同会话重复转人工不重复建单、商家通知仅发所属租户。
- `python_business/verify_session_restart.py`：重启 business 与 Agent 后，既有客户令牌和聊天历史仍可恢复。

一次验收统计快照（21 条 run）中，订单快速答复约 48 ms（7 轮）、FAQ 模型答复约 5.4 s（2 轮）、复合任务约 21.0 s（3 轮）。同一组 trace 中，订单和工单分支没有模型用量事件；FAQ 两轮合计 2076 tokens，复合任务三轮合计 4560 tokens。这是不同任务的观测值，**不能解释为改造前后提速幅度**；现有记录缺少同一问题在旧架构下的可比基线。

## 当前限制

- 路由是保守规则，没有 LLM 路由置信度。模糊请求交给无工具的 Intake Agent 澄清，不直接放行订单或交易操作。
- 复合问题仍需模型汇总，时延高于单项只读订单查询；可在积累固定中文问题集后优化检索和摘要。
- 旧审批检查点的 `/agent/resume` 仍保留旧图。开始交易写操作前，必须另行完成真实业务接口、订单归属、独立审批和幂等范围校验。
