# 两天速过 support-multiAgent（重点：多 Agent）

> 目标：两天后能独立讲清「一条客户消息如何在四 Agent 主图里流转、被校验、被记忆」，并能改 fakes 做实验。
> 环境已验证：Python 3.13.14，langgraph 1.2.11 / langchain 1.3.18，`tests/four_agent` 116 项全绿（19s，无需真实模型）。

## 0. 一句话本质与心智模型

这是一个**多租户智能客服 SaaS**：`python_business`（FastAPI，鉴权/租户/工单/WS）+ `python_agent`（FastAPI + LangGraph，四 Agent 对话）+ MySQL（业务）/ PostgreSQL（checkpoint、trace、prompt、用量）/ Qdrant（知识库向量）/ Redis（限流、锁、广播）。

多 Agent 的核心不是「四个模型聊天」，而是**一张有状态的图（StateGraph）**：

```
START → open_turn → supervisor → review → ┬(should_continue)→ supervisor   # 有界重试
                                          └→ finalize → compact → END
```

- **Supervisor**：一个 `create_agent`，把三个专职 Agent 当作工具调用（`consult_faq/order/general`）——官方称 subagents-as-tools。
- **三个专职 Agent**：FAQ（知识库 RAG）、Order（本人订单只读核验）、General（问候/澄清）。
- **Reviewer**：确定性规则节点，**无模型调用、不是第五个 Agent**，只回答「这条草稿能不能发给客户」，分 hard/soft 两级。
- **记忆**：messages 永远 append-only，模型上下文 = 摘要横幅 + 水位线之后的原文；锚点（单号/数值/抬头/约束）回补。
- **只有最外层图挂 checkpointer**（AsyncPostgresSaver），子 Agent 无状态、只吃快照。

四个必须带走的工程思想：① 子 Agent 即工具；② 规则校验用确定性代码而非再问一次模型；③ 状态通道 + reducer 实现幂等；④ 安全边界（订单归属、身份）放在可信服务端，prompt 无法越权。

---

# Day 1：全局 + 状态 + 主图骨架（约 6–7h）

## 模块 0｜项目全景与请求链路（1h）
- 读：`README.md`（架构图、能力边界、端口表）、`docs/MULTI_AGENT_ARCHITECTURE.md`。
- 重点：五个存储各自存什么；Nginx → business → agent 的方向；`business-maintenance` 独立进程为何必须存在。
- 动手：在纸上默画系统拓扑图，标出一条消息从 SDK 到返回的每一跳。
- 检查题：为什么客户断线后 AI 还能继续回答？PostgreSQL 和 MySQL 各承担什么？

## 模块 1｜跑通环境与测试基线（0.5h）
- 命令（在 `python_agent/` 下）：
  ```powershell
  ..\.venv\Scripts\python.exe -m pytest tests/four_agent -q
  ```
- 重点：打开 `tests/four_agent/conftest.py` 与 `app/agent/support/fakes.py`，理解如何用脚本化假模型/假依赖在无 LLM 下测整张图。
- 检查题：测试为什么不需要真实模型？`SupportDeps` 是怎么被替换成假实现的？

## 模块 2｜LangGraph 核心概念速补（1h）
- 概念清单：`StateGraph`、node、edge、conditional edge、state + reducer、checkpointer、`create_agent`、middleware、`recursion_limit`。
- 学法：不要孤立看文档，每个概念回到本项目代码里找对应（见模块 3/4）。
- 检查题：reducer 决定了什么是「追加」、什么是「覆盖」？checkpointer 持久化的是哪一层？

## 模块 3｜状态契约 `state.py`（1h）
- 文件：`app/agent/support/state.py`。
- 重点：
  - `messages` 走 `MessagesState` 的 `add_messages`——同 id 覆盖而非追加，用户/答复 id 固定为 `{turn_id}:u` / `{turn_id}:a`。
  - `merge_delegations/merge_ledger/merge_usage` 的按键 upsert 如何让「同 turn_id 重放」不重复。
  - `Overwrite` 用于清空本轮 scratch（在 graph.open_turn 里）。
- 检查题：为什么客户端重试不会多出一轮？`open_turn` 为什么必须同时清 `delegations`？

## 模块 4｜主图拓扑 `graph.py`（1.5h）
- 文件：`app/agent/support/graph.py`。
- 重点：五个节点各自的输入输出与副作用；`should_continue` 的有界条件；三层上界——middleware（模型/工具次数）→ `attempts`（supervisor pass，默认 3）→ `recursion_limit`（图步数，TURN_STEP_BUDGET=48）。
- 关注常量：`SUPERVISOR_STEP_BUDGET=32`、`TURN_STEP_BUDGET=48`，注释里写了实测二分过程。
- 检查题：`finalize` 如何区分 answered / answered_unchecked / review_blocked / no_result？为什么空草稿也要给客户一句话？

## 模块 5｜动手：离线跑一轮观察 state（1h）
- 命令：
  ```powershell
  # 在仓库根
  .venv\Scripts\python.exe -m app.agent.support.demo   # 如入口参数不同，先 python -m app.agent.support.demo --help
  ```
- 动手：用 `InMemorySaver` 跑一个 turn，用 `graph.aget_state(config)` 打印并对照 `messages / delegations / ledger / usage` 四个通道的变化。
- 检查题：复合问题（同时问政策+订单）时 `delegations` 里有几条？`roles` 是什么？

---

# Day 2：协作 + 规则 + 记忆 + 接入（约 6–7h）

## 模块 6｜四 Agent 构建 `agents.py`（1.5h）
- 文件：`app/agent/support/agents.py`。
- 重点：
  - `build_supervisor`：`consult_faq/order/general` 三个 `@tool` 内部 `_delegate` 跑专职 Agent，并回收 `Delegation`。
  - `build_order_agent`：`lookup_my_order/list_my_orders` 两个只读工具，身份取自服务端，模型无法指定客户。
  - 中间件：`ToolCallLimitMiddleware`、`ModelCallLimitMiddleware`、`safety_gate`（`@before_model` 装饰器产物）。
  - `collect_usage/add_usage`：token 按 pass 累加，重试是真实开销。
- 检查题：为什么专职 Agent 不挂自己的 checkpointer？`_delegate` 里 `evidence_cursor` 起什么作用？

## 模块 7｜Reviewer 规则 `guards.py`（1.5h）
- 文件：`app/agent/support/guards.py`。
- 重点：
  - hard（fail-closed）：无依据的「已退款/已支付/已建工单」、`数字+单位` 无证据、未核验订单号、订单字段无 ok 核验、与已核验状态冲突、内部片段泄漏。
  - soft（触发一次重试）：政策题未委派 FAQ、订单题未委派 Order、未检索/未调只读接口、专职 Agent 失败。
  - 子句级检测（`EXEC_ACTION` + `PERFECT`）、中文数字 `_from_chinese`、全角 NFKC 归一 `_ids`、`safe_fallback`。
- 检查题：为什么 hard/soft 要分级？「签收后 7 天」为什么不该被当成订单字段拦截？

## 模块 8｜水位线记忆 `memory.py`（1.5h）
- 文件：`app/agent/support/memory.py`。
- 重点：
  - `model_context`：摘要横幅 + `messages[watermark:][-window:]`；摘要横幅明确「不作为订单/政策依据」。
  - `compact`：水位线只前进、摘要失败/空/被截断（finish_reason=length）一律不推进、不删消息。
  - `anchors` + `prioritise_anchors` + `repair_line`：单号→点名字段→数值→一般从句，按预算回补。
  - 为什么不用官方 `SummarizationMiddleware`（它 RemoveMessage 重建且不暴露水位线，会丢前端历史）。
- 检查题：`compact` 跑完为什么消息总数不变？摘要单独设 `SUMMARY_MAX_TOKENS=4096` 解决了什么真实问题？

## 模块 9｜接入链路与 JSON/SSE 一致（1h）
- 调用链：`app/api/chat.py`（`four_agent.enabled/run_turn`）→ `app/services/four_agent.py` → `app/agent/support/cutover.py` → `service.SupportService.run_turn` → `graph`；真实依赖由 `legacy_bindings.deps_for_app` 构造。
- 重点：`TurnResult.to_payload()` 与 `sse_events()` 共用同一结果，JSON 与 SSE 内容只可能差帧；`turn_id` 幂等键。
- 检查题：`enabled()` 为什么在有未完成审批时返回 false？同一 turn_id 不同问题会怎样？

## 模块 10｜安全边界与工程化（1h）
- 重点：多租户隔离（`thread_key` 要求 `{tenant}:` 前缀、检查点 `support_v2:` 命名空间）；订单归属由 Business 判定、身份走 ContextVar；无任何写工具（退款/支付关闭）；`thread_guard.py` 的 PG advisory 锁解决跨进程并发丢轮；`services/trace.py` 写 `agent_trace_runs`（白名单过滤，原话与提示词不入库）。
- 检查题：为什么单个 UPSERT 原子还不够，需要线程锁/ advisory 锁？prompt 注入为什么无法让它查到别人的订单？

## 模块 11｜综合实验与答疑（1.5h）
- 动手：改 `fakes.py` 分别制造——① 缺专职 Agent 触发 soft 重试；② 顽固违规触发 hard 兜底；③ 模型故障触发 no_result 且透出 error；跑对应测试验证。
- 收尾：不看资料默画四 Agent 主图 + 状态通道图；回答每个模块的检查题。
- 进阶可跑：`python_agent/evaluation/four_agent/run_all.sh`（test/http/memory/guards 等，需 bash 环境）。

---

## 命令速查（PowerShell，仓库根 E:\...\support-multiAgent）

```powershell
# 跑四 Agent 全部测试（无需模型）
Push-Location python_agent; ..\.venv\Scripts\python.exe -m pytest tests/four_agent -q; Pop-Location

# 只跑某一类
..\.venv\Scripts\python.exe -m pytest python_agent/tests/four_agent/test_guards.py -q

# 离线演示（先 --help 看参数）
.venv\Scripts\python.exe -m app.agent.support.demo --help
```

## 学习建议
- 先读图（graph/state）再读细节（agents/guards/memory），始终带着「这一步在图的哪个节点」的问题。
- 每个模块先自己读 20 分钟，再用检查题自测，答不上来的回到代码行，不要跳过。
- 所有结论以当前代码为准；README 评测表是历史记录，注意其中的口径与限制说明。
