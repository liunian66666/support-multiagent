# support-multiAgent 能力优化设计方案（RSI + 记忆机制 + 运行时治理）

> 2026-10-24。对象：本仓库（多租户智能客服 SaaS，FastAPI 业务层 + LangGraph Agent + MySQL/PostgreSQL/Qdrant/Redis）。
> **本轮为设计文档，未修改代码、未测得任何优化收益。** 现状事实来自对 `python_agent/`、`python_business/`、`deploy/` 的只读核查（文件:行号见附录）。
> 配套决策文档：《技术热点优化方案-support-multiAgent与LabPilot.md》。本方案将其方向一（ReMe 记忆 + Agent Lightning 式优化循环）细化为可实施设计。

## 0. 设计立场

把"可信经验"做成**现有 Agent 的受控决策参考层**，不另起一套客服系统。复用既有资产：PostgreSQL 逐轮 `trace_id` 决策轨迹、MySQL 客户评价、知识库审核状态/适用范围/有效期/稳定分片 ID、Prompt 版本回滚、evaluation/ 评测 CLI。
生产控制面（租户权限、政策权威状态、发布/回滚）始终由本项目掌握；ReMe / Agent Lightning 只作为提炼工作流与评测闭环的**参考实现**，不接管线上决策。

## 1. 现状分析

### 1.1 实现结构（与本次优化相关的骨架）

| 层 | 位置 | 现状一句话 |
| --- | --- | --- |
| 请求入口 | `python_agent/app/api/chat.py` | 三个端点：`/agent/chat`（JSON）、`/agent/chat/stream`（SSE）、`/agent/resume`；后两者有并发配额校验，JSON 通道**没有**（可绕过） |
| 路由 | `app/chat/intent.py` + `plan.py` + 规则层 | 纯正则三层路由，无 LLM 路由、无置信度阈值、新增意图要改代码发版；demo 期 support-agent 的 confidence<0.45 短路模式**未落地** |
| 专家 | `app/agent/{faq_agent,intake_agent,builder}`、`app/services/{dialog,specialists,compound}` | supervisor 分发 order/kb/case 专家；复合问题 `compound.py` 并行取证后 composer 汇总；`_read_kb` 与 `tools.search_kb` 门控逻辑**双份实现** |
| 检索/门控 | `app/agent/retrieval.py`、`app/kb/evidence.py` | 同步 Qdrant 客户端直接在事件循环内调用；证据门控四要素（审核状态/适用范围/有效期/稳定 chunk ID）齐备，是经验层可复用的关键件 |
| 状态 | `app/infra/checkpoint.py` | `SafeAsyncPostgresSaver`：进程内**全局单锁**串行化所有 checkpoint 读写 + 每操作 `SELECT 1` 探活；锁只防同实例，不防跨实例写同一 thread |
| 记忆 | `app/workers/summary.py` | 定时把会话压成 ≤80 字摘要经内部接口落 MySQL；**全仓无任何代码回读**；摘要 LLM 调用不计入 token_usage |
| 评价 | `python_business/business/customer/routes.py` | 满意度按**会话级**保存且可覆盖，无法归因到某一轮回答 |
| 轨迹 | `app/services/trace.py` → PG `agent_trace_runs` | 路由/工具/证据/用量白名单入库、脱敏（用户原话不入库）；已实测基线见 `docs/EVALUATION_BASELINE.md` |

### 1.2 已暴露的问题（核查确认，非猜测）

**运行时（python_agent）**
1. 吞吐：checkpoint 全局单锁 + 每操作 ping，是全链路串行点；同步 Qdrant client 在 async 路径调用，大文档索引/检索阻塞整个事件循环（SSE 心跳、其他请求全卡）。
2. 假流式：FAQ 专家流式路径丢弃全部 chunk，终帧一次性推结果。
3. 错误处理：LLM `max_retries=0` 且应用层无退避，瞬时限流直接打到用户；非 PROD_MODE 时异常原文回吐客户端。
4. 计费/状态丢失：usage 落库在 SSE end 帧之前串行执行，此刻客户端断开则该轮计费+checkpoint 双丢，无补偿。
5. 预算缺失：`recursion_limit` 未设置、无 token/费用熔断；`daily_message_limit` 字段存在但**全仓无代码执行**。
6. 一致性：kb_cache 失效只清本进程（他实例最长 300s 吐旧答案）；summary/cleanup 的 Redis 锁释放不校验持有者，超时即误删他实例锁。
7. 配额绕过：`/agent/chat` 不查并发配额；租户 `deleted_at=0` 与 `status=1` 两套判据不一致，停用租户仍可从 agent 侧过配额校验。

**业务/数据（python_business）**
8. 评价粒度错位（会话级、可覆盖）→ 满意度无法归因到轮次，是经验闭环的**第一断点**。
9. `completed` ≠ 业务成功：trace 只记录"跑完了"，不记录"问题解决了没有"。
10. ws_hub 内存计数是并发配额唯一事实源，多实例/重启即失准。
11. compose 备份段被注释、observability 挂载目录缺失——已规划能力开箱不可用。
12. `pyproject.toml` 包名仍是 `ai-customer-service`；chroma 双依赖、死配置等卫生问题。

**共同断点（RSI 与记忆的关键缺口）**
13. 逐轮轨迹与业务结果**无可靠关联**：评价没有轮次 ID，工单结果没回填 trace。
14. 摘要是"落库即死"的数据，没有任何召回路径——这是把 ReMe 接进来的确切缝隙。
15. 线上质量无闭环：evaluation/ 是离线脚本，与运行时 trace/评价未打通；`EVALUATION_BASELINE.md` 自证拒检 0/2。

## 2. 优化目标

| 维度 | 目标 | 度量 |
| --- | --- | --- |
| 记忆闭环 | 经验（情景+任务双轨）按租户可审核、可召回、可失效；同类二次请求命中经验 | 经验命中率、命中后省去的路由/检索/生成调用数 |
| RSI 闭环 | 反馈→归因→候选策略→同案例配对评测→影子→人工发布→漂移回滚 | 冻结案例集上 before/after 对照（目标：成功率 +5pp、错误经验复用 ≤2%，**均为待验证目标**） |
| 运行时治理 | 消除吞吐串行点、配额绕过、计费丢失；超时/取消/预算成体系 | P95 延迟、事件循环阻塞告警 0、计费丢失 0 |
| 可观测 | trace 事件与轮次结果、经验命中贯通；线上指标接 Prometheus | 任一轮答复可按 trace_id 回放决策链与结果归属 |
| 兼容 | 现有 131 条业务路由契约、SDK/iframe/API/WS 四接入、前端零改动 | `route_baseline.json` 与全部现有测试持续通过 |

## 3. 总体架构

```
客户请求 ──► 现有路由与业务工具 ──► PostgreSQL 决策轨迹 (agent_trace_runs)
                    ▲                        │
                    │                        ▼
        ┌───────────┴────────┐      脱敏经验候选 (experience_candidates)
        │ 受控经验参考层      │               │
        │ 硬过滤→相关性→可信度 │◄── 人工审核 ◄──┘
        └────────────────────┘      (experience_items: 审核状态/适用条件/有效期)
                    ▲
   按租户+条件召回注入 │
                    │
评价/工单结果/人工核验 ──► 逐轮反馈事件 (experience_feedback, 幂等不可覆盖)
                              │
                              ▼
              可信度计算 + 候选策略配对评测 (evaluation/ 扩展)
                              │
                        影子运行 → 人工发布 (strategy_versions) ──► 召回层
                              │
              政策停用/到期/漂移 ──► 停用召回/复核/回滚 (experience_review_flags)
```

不变式（硬边界）：
- **订单事实永远现查业务工具**；经验不得充当订单状态或资格证明。
- 召回先硬过滤（租户、政策发布状态、商品范围、知识版本、有效期、人工审核），再相关性排序，再乘可信度/时效衰减。
- 单个低分评价只触发"提示复核"，不直接判经验错误；明确业务结果（工单解决/复检通过）权重高于会话满意度。
- 演示客户身份（t_demo/buyer*）不得进入真实订单路径或跨客户经验池；归因必须业务层核对 `trace_id` 属于该客户该会话，不信任客户端提交的 trace_id。

## 4. 关键模块改造点

### 4.1 多 Agent 职责划分与编排（L1：路由可运营化）

现状是三文件硬编码正则。改造**不引入 LLM 全权路由**（保持确定性优势），改为三层：

| 层 | 职责 | 改造 |
| --- | --- | --- |
| L0 规则层（现正则） | 高频确定意图（订单号紧邻、转人工关键词） | 保留；正则表外置为租户级配置（DB 表 `route_rules`，含启用状态/优先级），改规则=改数据不发版 |
| L1 置信度层 | 规则未命中时的兜底判定 | 复用 demo 期验证过的模式：结构化输出 `RouteDecision{route, confidence, reason}`，`confidence < τ`（默认 0.45，租户可调）→ ESCALATE。τ 与规则表都纳入 strategy_versions 的评测面 |
| L2 专家层 | order/kb/case 专家 + composer | 消除 `_read_kb` 与 `tools.search_kb` 双份实现，统一走 `kb/evidence.py` 门控入口（唯一实现，供 tools/compound/经验层三方复用） |

专家返回契约统一为 `{reply, trace_events, usage, routed_confidence, experience_hits}`，composer 汇总时透传 trace 字段。

### 4.2 交互协议与状态共享

- **轮次锚点**：每轮答复生成 `answer_id`（uuid），与 `trace_id` 一起随 SSE 终帧/JSON 响应返回前端；SDK 评价接口改为携带 `answer_id`（新增可选字段，旧客户端不传则回落会话级弱信号——兼容策略见 §6）。
- **上下文传递**：注入顺序固定为 `系统 Prompt(版本) → 经验召回块 → 会话摘要块 → 最近 12 条原文`。摘要块改为回读 summary worker 产物（现在"只写不读"），经验块与摘要各设 token 上限（env 可配），超限按可信度从低到高截断。
- **经验块格式**：结构化 JSON（经验文本、来源 trace、适用条件、知识版本、有效期、可信度），prompt 模板明确"经验是参考不是事实；与工具查询结果冲突时以工具为准"。

### 4.3 任务编排与调度（经验生命周期 worker）

新增 `app/workers/experience.py`（复用 summary worker 的 Redis 锁+互斥模式，但锁释放改为 Lua 校验持有者）：
1. 定时拉取"已闭环且工单结果明确"的会话轨迹 → LLM 提炼经验候选（租户隔离，脱敏沿用 trace 白名单，禁止落用户原话/凭证）；
2. 候选写入 `experience_candidates`（默认草稿态，不进召回）；
3. 管理端审核接口（admin-app 增加"经验库"页）批准 → `experience_items`（带适用条件/知识版本/有效期）。
提炼模型调用接入 `usage` 记账（修复 summary worker 不记账的同类问题）。

### 4.4 错误处理与重试、超时与取消（L2 治理包）

| 项 | 改造 |
| --- | --- |
| LLM 重试 | `llm_config` 增加租户级 `max_retries=2` + 指数退避（仅对 429/5xx/超时白名单错误）；重试事件写 trace |
| 错误文案 | 生产/非生产统一返回错误码，不回吐 `str(e)`；resume 同步修改 |
| 计费不丢 | usage 落库移到"生成完成即写"，SSE end 帧只负责通知；客户端断开路径 catch 后仍写 usage（修复计费+checkpoint 双丢） |
| 预算 | 显式 `recursion_limit`（默认 12）；每请求 token 预算与租户日限额（`daily_message_limit` 真正执行）；超限走确定性安全答复而非静默截断 |
| 超时 | 检索、工具、内部业务调用各设 timeout（env），超时作为 Observation 回喂模型一次 |
| 取消 | 客户端断开 → SSE 生成器 aclose → 图任务取消（asyncio task cancelled 传播），已产生用量仍记账 |
| JSON 通道配额 | `/agent/chat` 补 `check_concurrent_sessions`；租户判据统一为 `status=1 AND deleted_at=0`（业务与 agent 两侧同步） |

### 4.5 并发控制与性能瓶颈

| 瓶颈（实测确认） | 改造 | 预期 |
| --- | --- | --- |
| checkpoint 全局单锁 | 锁粒度改为 **thread 级锁 dict**（同 thread 串行、跨 thread 并行）；`SELECT 1` 探活降频为连接归还时探测 | 跨租户吞吐不再互相串行 |
| 同步 Qdrant client 在事件循环内 | 全部包 `asyncio.to_thread` 或换 AsyncQdrantClient；索引 worker 本就在线程内可不动 | 消除事件循环阻塞（心跳/SSE 卡顿根因） |
| kb_cache 失效不广播 | 复用配置失效的 Redis pub/sub 通道，`kb_updated:{tenant}` 事件 | 多实例一致性窗口从 300s → 秒级 |
| Redis 锁误删 | Lua 脚本校验 value 持有者再删；summary 一轮限额按耗时预算 | 双实例并跑消除 |
| ws_hub 内存配额计数 | 计数迁 Redis（`ws:conns:{tenant}` 计数 + TTL 心跳） | 多实例一致 |
| FAQ 假流式 | 专家 chunk 直发，finalize 只做增量修补 | 首字延迟下降 |

### 4.6 可观测性与日志追踪

- trace 事件补三类：`experience_hit`（经验 ID/得分）、`llm_retry`、`cancel/timeout`；`agent_trace_runs` 增加 `answer_id`、`feedback_summary`（物化列，非原话）。
- 暴露 Prometheus `/metrics`：请求/生成时延直方图、事件循环 lag、经验命中率、重试/取消计数、checkpoint 锁等待；补齐 `deploy/observability/` Grafana 面板文件与备份脚本（当前缺失）。
- 评价链路：MySQL 评价写入同事务追加 **outbox 事件** → 异步幂等同步到 PG `experience_feedback`（不依赖前端二次提交）。

### 4.7 数据结构（PostgreSQL 新增四表 + MySQL 一改动）

```
experience_items        租户、来源 trace、路由、经验文本、适用条件(JSONB)、
                        文档/chunk ID、知识版本、有效期、审核状态、可信度、版本
experience_feedback     幂等事件 ID、tenant、trace_id、answer_id、experience_id(可空)、
                        结果类型(工单解决/复检通过/满意度/人工标注)、值、来源、发生时间(不可覆盖)
strategy_versions       租户、父版本、参数快照(规则表/τ/召回权重/prompt 版本)、
                        评测状态(候选/影子/发布/回滚)、发布人、发布时间
experience_review_flags 经验 ID、失效/漂移原因、政策事件引用、处理记录、处理人
MySQL: 评价表保留原粒度以兼容前端，新增 answer_id 列 + feedback_outbox 表
```

## 5. 评测与 RSI 闭环（复用 evaluation/ CLI）

1. **冻结基线**：以 `EVALUATION_BASELINE.md` 已注册门槛扩容（检索 ≥100 题、无答案/反条件各 ≥20；端到端从 7 条扩至 ≥50 条，含跨租户与政策变更例）。
2. **同案例配对评测**：候选策略（新规则表/新 τ/新召回权重）与在用策略跑同一冻结集，逐例对照；口径沿用既有诚实规范（分母与硬件标出、不作无依据百分比结论）。
3. **影子运行**：候选策略只记录不投放，diff 记录进 strategy_versions。
4. **人工发布 + 漂移回滚**：管理员一键发布/回滚（复用 Prompt 版本机制的交互模式）；政策停用事件 → `experience_review_flags` 批量挂起关联经验。
5. Agent Lightning 仅**参考其"可编辑面+基准+门槛"的循环形态**实现自研轻量 CLI（`python -m evaluation.strategy_eval`）；ReMe 仅参考其 Draft-Retrieve-Deduplicate 提炼流程。线上权限与发布不经外部组件。

## 6. 兼容与迁移策略

| 面 | 策略 |
| --- | --- |
| 131 条业务路由契约 | 全部新增走新路径；`route_baseline.json` 与现有 329+88 测试作为回归门槛，任何阶段不破 |
| SDK/iframe/API/WS | 响应体新增 `answer_id` 为**附加字段**；评价接口 `answer_id` 可选，缺失时按会话级弱信号入库（不报错），旧前端零改动 |
| 数据库 | 新表 + 新列全部 additive；Alembic 迁移脚本分三次（锚点列与 outbox → 四张经验表 → 观测列），每步可独立回滚（drop 即回退，无数据迁移） |
| 经验召回开关 | 三级开关：全局 env → 租户配置 → 策略版本；默认 **off**，影子期只记录不注入，注入异常自动降级为无经验路径（fail-open 到现行为，绝不 fail-close 卡客服） |
| 部署 | 无新服务；worker 挂在现有 agent 容器 lifespan 内（与 summary 同模式），多实例行为靠持锁互斥 |

## 7. 实施步骤（严格串行，每步独立可验收）

| 阶段 | 内容 | 出口判据 |
| --- | --- | --- |
| S1 归因锚点（P0 前置） | answer_id 贯通生成→响应→评价→outbox→feedback；trace 补 answer_id；冻结评测基线扩容 | 任一轮答复可查到其轮次级反馈事件；旧评价通路回归通过 |
| S2 经验库与影子召回（P0） | 四表 + 提炼 worker + 审核接口 + 管理页；召回只影子记录不注入 | 候选→审核→上架全流程可演示；影子命中日志可见、注入路径零流量 |
| S3 反馈归因与失效（P0） | 可信度计算、业务结果权重、政策/到期失效联动挂起 | 专项测试：跨租户泄漏、未审核经验使用、过期政策复用、经验冒充订单事实——**四类均为 0 例** |
| S4 小流量发布与闭环（P0） | 配对评测 CLI、strategy_versions 发布/回滚、1 个租户试点注入 | 冻结案例 before/after 报告 + 回滚演练成功 |
| S5 运行时治理包（P1，可与 S2-S4 并行） | 4.4/4.5 全部：重试、预算、thread 锁、Qdrant 异步化、锁校验、配额补齐 | P95 不劣化下压测吞吐提升实测；事件循环告警 0；计费丢失 0 |
| S6 观测与收尾（P2） | Prometheus 指标 + Grafana/备份文件补齐 + 包名改 `support-multiagent` + 依赖卫生 | 指标上线；compose 开箱可用 |

## 8. 风险点

| 风险 | 缓解 |
| --- | --- |
| 归因被伪造/错位（客户端伪造 trace_id 污染经验池） | 业务层核对 trace_id 归属租户+会话+客户后才入库；feedback 事件幂等且不可覆盖，异常来源单独隔离标记 |
| 经验污染与错误放大 | 候选默认草稿态、人工审核后才可召回；单低分只挂"复核"不判死；订单事实永远现查工具 |
| RSI 基准偏→优化跑偏 | 冻结集先于任何策略改动发布（S1 产）；配对评测同集同硬件；未达门槛只进影子不发版 |
| 注入经验引发新错误（prompt 注入面） | 经验文本入库前过滤指令式语句；prompt 中经验块显式声明"参考非事实、冲突以工具为准"；三级开关秒级关闭回退现行为 |
| checkpoint thread 锁改造引入竞态 | 保留全局锁为 env 可回退项；改造后跑并发回归（同 thread 交替读写断言） |
| AGPL-3.0 上游约束 | 新表/新代码在本仓库内实现，不引入上游没有的闭源依赖；外部组件仅 Apache/MIT 且只做离线参考 |
| 范围膨胀 | P2 项（Jaeger span、pyproject 改名等）明确不入 S1-S4 出口判据 |

## 9. 验收标准

**安全与正确性硬前提（任何一条不满足即不通过）**
1. 专项测试四类 0 例：跨租户经验泄漏 / 未审核经验被召回 / 过期政策经验被使用 / 经验冒充订单事实。
2. `route_baseline.json` 131 条契约 + 现有全部单测（业务 329 + agent 88）通过；旧版 SDK 评价请求（不带 answer_id）行为不变。
3. 回滚演练：一次真实发布后一键回滚 ≤1 分钟生效，期间无 5xx 尖峰。

**效果性目标（冻结案例集+真实业务结果检验；均为待验证目标，不得用现有 24 条检索题或 7 条端到端问答宣称达成）**
4. 试点租户同类二次请求经验命中率与节省调用数如实报告（首月不设及格线，只建曲线）。
5. 冻结端到端集：目标成功率 +5pp 且错误经验复用率 ≤2%，同时成本与 P95 不劣化；未达标则保留影子态并出消融报告（诚实空结果也算交付）。

**治理目标**
6. 压测（同硬件同模型）：checkpoint 锁等待与事件循环阻塞显著下降（报告实测数，不预设百分比）；计费丢失 0；`daily_message_limit` 执行验证通过。
7. 任一试点轮次可用 trace_id 完整回放：路由决策→经验命中→工具调用→重试/取消→轮次反馈。

## 附录：现状证据索引（节选）

- 配额绕过：`python_agent/app/api/chat.py:51-131`（无校验）vs `:189-193,:283-287`（有校验）
- checkpoint 全局锁：`app/infra/checkpoint.py:45,104`；同实例锁不防跨实例
- Qdrant 同步调用：`app/agent/retrieval.py:32,41`、`app/workers/kb_index.py:63-105`、lifespan `:92`
- 摘要只写不读：`app/workers/summary.py:74-84`（且不记账）；回读点缺失（`faq_agent.py:46-57` 仅取最近 12 条原文）
- 计费在 end 帧前：`chat.py:240-244`；LLM max_retries=0：`llm_config.py:100,259`；错误透出：`chat/reply.py:19`
- 日限额未执行：`infra/db/quota.py:22`；kb_cache 不广播：`workers/kb_index.py:116`；Redis 锁无持有者校验：`summary.py:45-52`、`infra/db/session.py:121-128`
- 评价会话级可覆盖：`python_business/business/customer/routes.py`；租户判据不一致：`business/agent/internal.py:22-24` vs `business/chat/realtime.py:109-111`
- ws_hub 内存计数：`realtime.py:79-87,32-40`；备份/observability 缺失：`docker-compose.yaml:242-272,286,325-326`
- 评测现状与已注册门槛：`python_agent/evaluation/`、`docs/EVALUATION_BASELINE.md`
