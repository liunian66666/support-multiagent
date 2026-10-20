# 多 Agent 架构梳理与设计（ai_customer_service × support-agent）

> **2026-10-19 结构重构提示**：`python_agent/` 已改为 `app/` 分包结构，本文正文沿用重构前的文件名与行号。
> 新旧路径对照：`main.py`(12 端点) → `app/api/{chat,session,tenant,usage,platform}.py` + `app/lifespan.py` + `app/server.py`；
> `multi_agent.py` → `app/chat/intent.py` + `app/agent/faq_agent.py`；`agent_factory.py` → `app/agent/{builder,middleware,llm_config,retrieval,faq_bridge}.py`；
> `db_ops.py` → `app/infra/db/{tenant,session,quota,usage,prompt,settings}.py` + `app/workers/cleanup.py`；
> `routers_kb.py` → `app/api/kb.py` + `app/kb/storage.py`；`routers_prompt.py` → `app/api/prompt.py`；
> `routers_cache.py` → `app/api/cache_admin.py` + `app/services/invalidation.py`；`support_faq.py` → `app/chat/faq.py`；
> `tools.py` → `app/agent/tools.py`；`models.py` → `app/schemas.py`；`cache.py`/`config.py`/`logger.py`/`db_pool.py`/
> `checkpoint_safe.py`/`redis_pubsub.py` → `app/infra/`；`kb_parser.py` → `app/kb/parser.py`；
> `kb_index_worker.py`/`session_summary.py` → `app/workers/`；`test_*.py` → `tests/`。

> 结论先行：当前项目已经完成"第一刀"——`multi_agent.py` 引入规则路由与 FAQ 专职 agent，方向与 support-agent 的 supervisor/specialist 一致；但**角色只切了一半**（缺置信度、缺 LLM 兜底、缺统一返回契约、缺工具授权），且**编排仍双份**在 `main.py` 两个入口里。下一阶段不应继续加 agent，而应先补齐"调度层契约"。

- 参照项目：`AGENT_exp/support-agent`（README 标题为 *AI Customer Support Agent (Multi-Agent)*，仓库内无名为 `super-agent` 的目录，本文以其为参照；若所指是另一仓库请指出）
- 本文范围：`python_agent/`（26 个 `.py`，其中 agent 相关 6 个）+ Go 网关的工单与身份边界

---

## 一、参照项目：support-agent 的编排模式

| 维度 | 做法 | 关键实现 |
|---|---|---|
| 路由 | 5 类固定路由 `ORDER / REFUND / PAYMENT / FAQ / ESCALATE` | `orchestrator.py:31` |
| 路由决策 | 结构化输出 `RouteDecision{route, confidence, reason}`，不再"回一个词再正则" | `orchestrator.py:71` |
| 路由不确定性 | `confidence < 0.45` 直接判为 ESCALATE（"该人来看了"） | `MIN_ROUTE_CONFIDENCE = 0.45` |
| 三级短路 | 能力问答规则 → 共享缓存（curated/exact/semantic）→ 规则正则 → LLM | `route_node()` |
| Specialist | 统一签名 `run_tool_agent(name, prompt, tools, msg, history)`，统一返回 `{reply, trace, messages, agent, usage}` | `agents/base.py` |
| 工具循环 | 最多 4 轮，重复调用签名去重，空回复回退到最后一次工具结果 | `agents/base.py` |
| 图 | `route → {order,refund,payment,faq,escalate}`，写操作经 `approval` 闸门 | `build_graph()` |
| HITL | `interrupt()` 暂停 + `Command(resume=...)` 恢复，审批 TTL 默认 900s | `approval_node()` |
| 写操作授权 | `mutation_scope`：审批时的 `route/order_id/method` 绑定成 scope，工具执行时 `reject()` 校验参数一致性 | `mutation_scope.py` |
| 失败处理 | `call_with_retry`（2 次 + 3s 退避）→ 仍失败则 `escalate_to_human` 而不是抛错 | `errors.py` |
| 工单 | 建单时快照 `customer_request / history / trace 中的业务操作 / context_complete` | `ticketing.py` |
| 记忆 | `ConversationMemory{turns, last_order_id, 待审消息暂存}`，跨 specialist 只传纯文本 history | `memory.py` |
| 可观测 | `tracing.span/record_event/trace_summary`，route_source、confidence、latency、usage 全记录 | `tracing.py` |
| 护栏 | 中文 FAQ 只走**保守语法**（词表 + 白名单），不认识的直接放弃；写入规则在工具层而非 prompt | `semantic_guard.py` |

**可直接迁移的三条原则**
1. **路由必须能表达不确定**（confidence + 阈值 + ESCALATE 兜底），否则误路由是静默的。
2. **Specialist 无状态、返回结构化结果**，跨轮状态只存在一处（checkpointer / memory），不在 agent 内部另建一份。
3. **写入权限在工具层校验**，不能只在 prompt 里"要求模型先检查资格"。

---

## 二、当前项目 agent 侧现状

### 2.1 模块与职责

| 模块 | 行数 | 当前职责 | 属于哪一层 |
|---|---|---|---|
| `main.py` | 931 | HTTP 契约（12 端点）、lifespan 资源装配、**编排逻辑**、确定性快答、检查点桥、用量记账 | 入口 + 编排（混装） |
| `multi_agent.py` | 83 | `route_message()` 规则路由；`faq_agent()` 构建仅含 `search_kb` 的独立 agent；`recent_messages()` 读 canonical 历史 | 调度层（雏形） |
| `agent_factory.py` | 556 | `create_agent`、`content_guard` 中间件、Qdrant 向量与检索器、BYOK 模型解析、prompt 热更新、配置签名 | agent 资产 + 基础设施 |
| `tools.py` | 179 | `search_kb` / `query_order_readonly` / `transfer_to_human`；模块级 `set_app` 注入 + ContextVar 取租户/线程 | 工具层 |
| `support_faq.py` | 118 | 流程 FAQ 确定性问答、转人工识别、订单号与指代提取 | 纯规则 |
| `test_multi_agent.py` | 66 | 路由契约测试（8 条）+ faq_agent 工具白名单 | 测试 |
| `db_ops.py` / `checkpoint_safe.py` / `session_summary.py` / `kb_*.py` | 1200+ | 配额、用量、提示词、检查点、摘要、知识库索引 | 基础设施 |

Go 侧边界：身份与会话（`CustomerSession`）、订单归属（三条件查询）、工单创建（`ws.go` / `chat.go` 消费 interrupt 后建单）。

### 2.2 当前调用流程

```
前端 ──WS/REST──> Go 网关（身份、会话、配额）
                    │  X-Internal-Token + X-Tenant-Id + thread_id
                    ▼
            POST /agent/chat 或 /agent/chat/stream
                    │
         _require_trusted_chat()  ← 只认网关，租户取自 thread 前缀
                    │
         route_message(msg, tenant_id, pending_ticket)
                    │
   ┌────────────┬───┴────────┬──────────────┬─────────────┐
   ▼            ▼            ▼              ▼             ▼
handoff      order       faq_fast         faq         general
(直接中断)  (只读工具)  (确定性答复)  (独立KB agent)  (全工具 agent)
   │            │            │              │             │
   └────────────┴────────────┴──────┬───────┴─────────────┘
                                    ▼
                    _record_to_checkpoint()（确定性路径回写历史）
                    _record_usage_after_chat()（记账）
                                    ▼
                    返回 AgentResp / SSE 事件
                                    ▼
            Go 收到 interrupt → 建工单 → 推送 new_ticket
```

转人工有**两条路径**：`direct_handoff`（Python 返回 interrupt，Go 建单）与 `transfer_to_human` 工具（LangGraph `interrupt()` 暂停，Go 建单后调 `/agent/resume` 恢复）。

### 2.3 模块边界现状评估

| 维度 | 现状 | 问题 |
|---|---|---|
| 路由 | 纯正则 + 顺序匹配，无置信度 | `_FAQ` 含"退款/退货/物流"，与 `_ACTION`、订单意图重叠；命中顺序即优先级，改一处就换行为；**没有 LLM 兜底**，未命中即 `general`，误路由不可见 |
| 角色 | 5 个分支（handoff/order/faq_fast/faq/general） | `faq_fast` 是规则不是 agent；`general` 与 `faq` 工具集不同但 prompt 来源不同（租户 prompt vs 固定 prompt），角色边界靠 if 判断 |
| 状态 | 唯一真相 = checkpointer（`faq_bridge`） | `faq_agent` 无 checkpointer，历史靠 `recent_messages()` 手工拼接；它的工具调用不入历史 → 长对话下 FAQ 侧无记忆 |
| 编排 | 两个入口各写一遍分支 | 双份代码已出现分叉：流式路径用 `tenant_from_thread` 重新推导租户、JSON 路径缺 `check_concurrent_sessions` |
| 工具授权 | `general` 拥有全部工具 | `query_order` 与 `transfer_to_human` 对 general 开放，`faq` 只给 `search_kb` —— 白名单在代码里隐式写死，无统一注册表 |
| 失败语义 | 工具内 fail-closed 文案 + 文本 marker 判定 | `_ORDER_LOOKUP_FAILURE_MARKERS` 靠"回复文本包含某句话"决定是否转人工，改文案即改行为（2026-10-19 已改为 `OrderLookup.code`） |
| 可观测 | 仅 logger | 无 route_source / confidence / latency / trace_id 贯通；误路由无法复盘 |
| 并发 | 无 thread 级互斥 | 同一 thread 的 JSON 与流式请求可交错写同一 checkpoint |

---

## 三、目标架构

### 3.1 角色划分

| 角色 | 职责 | 工具 | 可写 | 状态 | 触发条件 |
|---|---|---|---|---|---|
| **Supervisor**（`supervisor.py`） | 唯一入口；路由、预算控制、结果归一、失败兜底 | 无（不直接调业务工具） | 否 | 无 | 每个请求 |
| **OrderAgent** | 本人订单状态/物流查询，只读 | `query_order` | 否 | 无 | 命中订单号或指代回溯后 |
| **FAQAgent** | 租户知识库 RAG 问答（政策/课程/价格） | `search_kb` | 否 | 无 | 知识型问题、订单无关 |
| **ActionAgent**（预留，默认关闭） | 退款/支付等写操作 | `check_eligibility` / `initiate_refund` | **是（必须 HITL）** | 无 | 明确写意图且审批通过 |
| **HandoffAgent** | 转人工与工单上下文组装 | `create_handoff`（唯一建单入口） | 是（仅工单） | 无 | 用户要求 / 路由不确定 / specialist 失败 |
| *faq_fast* | 不是 agent——Supervisor 的确定性短路（规则命中直接答） | — | 否 | 无 | 流程 FAQ 精确匹配 |

设计要点：
- **ActionAgent 先不启用**，与 `multi_agent.py:38` 的现有注释一致：业务写接口、归属校验、独立审批三者齐备后再开。
- **Handoff 必须是唯一建单方**。现在 Go 两侧都能建单，是重复工单的根因。
- Specialist 一律**无状态、无 checkpointer**，历史由 Supervisor 注入快照（`recent_messages` 已具备该能力，应推广到 Order/Action）。

### 3.2 协作与通信

**同进程、单向调度，不做 agent 间对话。**

```python
@dataclass(frozen=True)
class SpecialistResult:
    reply: str
    agent: str
    trace: list[dict]          # [{tool, args, result, status, ms}]
    usage: dict                # input/output/total
    needs_clarification: bool = False
    handoff: HandoffRequest | None = None
    error: str | None = None
```

- Supervisor → Specialist：函数调用 + 只读上下文快照（`history`, `tenant_id`, `thread_id`, `last_order_id`）。
- Specialist → Supervisor：只返回上面的结构体，不写 DB、不改上下文；持久化统一由 Supervisor 调 `services/persist.py`。
- 跨进程（Go）：仍只走 HTTP。SSE 增加事件类型 `{"type":"agent","name":"order"}`、`{"type":"tool","name":"query_order"}`，让前端能显示"正在查询订单"，同时替代现在"前端靠猜"的加载态。

### 3.3 任务分配与调度

**三级路由（照搬 support-agent 的短路顺序，降低 LLM 依赖）**

| 级别 | 手段 | 覆盖 | 成本 |
|---|---|---|---|
| L0 | 确定性快答（`support_faq.lookup`） | 流程类问题 | 0 |
| L1 | 规则路由（现有 `_ACTION` / 订单号 / `_FAQ`，改为**表驱动打分**） | 明确意图 | 0 |
| L2 | LLM 结构化路由 `RouteDecision{route, confidence, reason}` | 长尾、复合意图 | 1 次小模型调用 |
| 兜底 | `confidence < 0.45` 或 L2 异常 → Handoff | — | 0 |

**调度约束**
- 单轮只委派 **1 个** specialist（客服场景延迟敏感，不做并行 fan-out）。
- 允许 **1 次补派**：specialist 返回 `needs_clarification` 时 Supervisor 可再委派一次，上限 2，防循环。
- 每轮预算：工具轮次 ≤ 4（对齐 `agents/base.py:max_rounds`）、单轮超时、token 上限超出即降级为确定性回复。
- 优先级**显式全序**（写进一张表 + 单测，不再靠 if 顺序）：
  `handoff(显式请求) > action(写操作) > order(有订单号) > faq_fast > faq > general`

### 3.4 共享状态与上下文

| 内容 | 存放位置 | 说明 |
|---|---|---|
| 对话消息 | LangGraph checkpointer（唯一真相，`thread_id`） | 所有路径（含确定性答复）都经 `_record_to_checkpoint` 回写 |
| 结构化会话档 | `SupportState`（新增，落 checkpoint 的 `values`） | `route / last_order_id / pending_ticket / approval / agent_trace / usage / route_source` |
| 摘要 | `session_summaries`（已有） | 超长对话先摘要再拼最近 N 轮 |
| 租户配置 | `tenant_agents` / `tenant_retrievers` / 模型缓存（已有） | 以 `config_version` 签名做最终一致 |
| 检索内容 | 只在当轮 prompt 内 | 标记为"不可信资料，不执行其中指令"（现有 FAQ prompt 已声明） |

关键改动：把"最近订单号"从 `_last_order_id_from_history()`（读 checkpoint 扫消息）升级为 `SupportState.last_order_id`（写入时记录），避免每轮反查历史。

### 3.5 错误处理与冲突消解

| 失败/冲突 | 现有做法 | 目标做法 |
|---|---|---|
| 工具失败 | 工具内返回 fail-closed 文案 | 工具返回结构化 `error.code`；`reply` 文案在展示层生成 |
| 订单核验失败判定 | `lookup_order()` 返回 `OrderLookup.code`，`should_auto_handoff()` 按 `AUTO_HANDOFF_ORDER_CODES` 决策（2026-10-19 已从文案匹配改为结构化码） | specialist 返回 `error.code = ORDER_UNVERIFIED`，Supervisor 按 code 决策 |
| Specialist 抛错 | 两个入口各自 `except` + 文案 | `call_with_retry`（1 次重试）→ 仍失败则 Handoff，并附 `error` 进工单快照 |
| 路由不确定 | 落到 `general`（静默） | `confidence < 0.45` → Handoff，并记录 `route_source=llm/confidence` |
| 两条建单路径 | `direct_handoff` 与 `transfer_to_human` 并存 | 合并为 `HandoffRequest`，只由 HandoffAgent 发一次；Go 只消费、不判断 |
| 同一 thread 并发 | 无保护 | thread 级互斥（Redis `SETNX thread_id` 短 TTL，复用现有 Redis），冲突时排队或返回"上一条正在处理" |
| 写操作越权 | 无（ActionAgent 未开） | 启用前必须落地 `mutation_scope`：审批时的 `route/order_id/method` 绑定为 scope，工具执行时校验一致，不一致拒绝 |
| 同一问句双答 | `lookup` 与 LLM 都可能答 | L0 命中即终止，不再进 specialist（现有逻辑已如此，需保持并加测试） |

### 3.6 可观测性（最低要求）

- `trace_id` 从 Go 透传（已有 `X-Trace-Id`），每个请求生成 `route_source / confidence / specialist / tools / usage / latency` 一条决策日志。
- 事件流：`route` → `delegate` → `tool` → `handoff/approval`；误路由复盘只看这一条链路。
- 按 agent 归集用量（`token_usage` 增加 `agent` 维度），否则多 agent 后成本无法归因。

---

## 四、改造涉及的主要模块

| 模块 | 改动 | 优先级 |
|---|---|---|
| `multi_agent.py` | 升级为 `supervisor.py`：`RouteDecision` + confidence + L2 LLM 路由 + 预算 + 兜底；`route_message` 收敛为 L1 规则层 | P1 |
| `agents/`（新增） | `base.py`（统一 `run_tool_agent` + `SpecialistResult`）、`order.py`、`faq.py`、`handoff.py`、`action.py`（默认关闭） | P2 |
| `services/dialog.py`（新增） | 抽两个入口共用的"模型调用前流程"，端点只负责 JSON/SSE 格式 | P1 |
| `services/persist.py`（新增） | 检查点回写 + 用量记账，从 `main.py` 迁出 | P2 |
| `tools.py` | 按角色切分工具集；`transfer_to_human` 改为 `create_handoff`（不再自行 `interrupt`）；补 `error.code` | P2 |
| `main.py`（931 行） | 只保留端点与资源装配，编排全部下沉 | P1–P2 |
| `agent_factory.py` | `create_agent` 增加 `tools` 白名单参数；prompt 按角色取（当前只有单条租户 prompt） | P2 |
| `db_ops.load_active_prompt` | 支持按 `role` 取 prompt（`prompt_versions` 已有表结构，加 `role` 维度） | P3 |
| `go_service/ws.go`、`pkg/chat/chat.go` | 工单只在一处创建；消费新增的 `agent`/`tool` 事件透传给前端 | P2 |
| `deploy/init/mysql/init.sql` + `token_usage` | 用量加 `agent` 列；工单加 `agent` / `route` 字段用于归因 | P3 |

---

## 五、实施路线

| 期 | 目标 | 内容 | 验收 |
|---|---|---|---|
| **P1 调度可信化**（不改角色） | 让路由可解释、可兜底 | `RouteDecision` + confidence + 表驱动优先级 + LLM 兜底 + 抽出 `services/dialog.py` 修双入口分叉 | 20 条路由用例全过；两入口同输入同输出；误路由会落到 handoff 而非 general |
| **P2 角色化** | 让 specialist 可插拔 | `agents/` 包 + `SpecialistResult` + 工具白名单注册表 + `faq_agent` 历史改由 Supervisor 注入 | 每个 specialist 有独立单测；`general` 不再拥有写工具 |
| **P3 治理** | 让写入与失败可控 | 单一 Handoff 路径、`mutation_scope`、thread 互斥、按 agent 记账、trace 事件 | 重复工单为 0；并发同 thread 有明确行为；能按 agent 出成本报表 |
| **P4（暂缓）** | 启用 ActionAgent | 退款/支付 specialist + 审批闸门 | 未通过审批绝不产生写操作 |

> 建议 P1 先做，且与"目录重构"错开：目录重构改的是文件位置，P1 改的是行为契约，两者同时做会让回归无法定位。

---

## 六、风险

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| 1 | 多一层 LLM 路由 | 首字节延迟 + 成本上升 | L0/L1 短路；路由用小模型 + 低 `max_tokens`；只有长尾才走 L2 |
| 2 | 路由误判漏转人工 | 用户问题被答错、投诉 | confidence 阈值 + specialist 可返回 `handoff` 主动上交 + 每轮可 `needs_clarification` |
| 3 | 状态分裂 | 长对话"忘了刚才说过什么" | specialist 全部无状态，唯一真相 = checkpointer；`SupportState` 落 `last_order_id` |
| 4 | 重复工单 | 客服重复处理、数据污染 | 只在 HandoffAgent 建单；Go 侧建单逻辑删除或降级为幂等 upsert |
| 5 | 并发写同一 checkpoint | 状态错乱、消息丢失 | Redis thread 锁；Go 侧对同 thread 请求串行化 |
| 6 | 工具权限收窄 | `general` 答不了订单问题 | 收窄同时给出路由兜底：未命中 specialist 时由 Supervisor 二次委派，而不是让 general 硬答 |
| 7 | 按角色拆 prompt | 租户现有自定义 prompt 失效 | `prompt_versions` 加 `role`，`role=""` 作为租户默认兜底；灰度切换 |
| 8 | 与目录重构叠加 | 回归定位困难 | 先 P1 再搬家；每步独立提交 + 基线用例 |
| 9 | 无 trace | 误路由无法复盘 | P1 就加决策日志与事件流，别等到 P3 |
| 10 | `_ACTION` 正则升级为 LLM 后乱开写权限 | 越权退款/支付 | ActionAgent 默认关闭 + 工具层 `mutation_scope` 双保险 |

---

## 七、一句话总结

`multi_agent.py` 已经把"路由 + 专职 agent"的骨架立起来了，但它是**规则版的 supervisor**：能分流，不能表达不确定，也没有统一的返回契约。下一步的核心不是增加角色数量，而是把 supervisor 做成"有置信度、有预算、有兜底、有统一结果结构"的调度器，并把 specialist 全部无状态化、工具白名单化——之后再加 ActionAgent 才是安全的。
