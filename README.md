<div align="center">

# 多 Agent 电商智能客服系统

**AI 自动应答 + 人工客服接管 + 知识库问答 + 多租户独立配置**

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](./LICENSE)
[![界面预览](https://img.shields.io/badge/Preview-界面截图-blue)](./docs/README.md)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ed?logo=docker)
![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python)
![Vue3](https://img.shields.io/badge/Vue-3-4FC08D?logo=vuedotjs)
![Tests](https://img.shields.io/badge/Tests-pytest-success)

**本仓库没有公开在线演示站**：按 [快速开始](#quick-start) 本地一键启动，或先看 [图文操作手册](./docs/README.md) 的界面截图。

</div>

<!-- 自建演示站上线后：把上面「本仓库没有公开在线演示站」一行换成
     🚀 在线体验：https://你的域名 ，并把徽章行的「界面预览」改回 Online Demo 指向该域名。
     请勿回链上游项目的演示站：它部署的不是本仓库代码版本。 -->

> **2026.09—2026.11｜FastAPI、LangGraph、Qdrant、MySQL、PostgreSQL、Redis、WebSocket**  
> 基于开源客服系统二次开发，面向电商政策问答、本人订单查询、复合咨询与人工接管；重点实现 Agent 协作、长对话记忆、RAG 证据校验和可复算的对照评测。

---

## 🎯 项目问题与实现

> 上游项目提供多租户客服基座；下面四条是本仓库围绕电商客服 Agent **实际解决并留下验证记录**的问题，也是本项目的主线。

| # | 要解决的问题 | 实现方式 | 可验证的结果 |
|---|---|---|---|
| 1 | **一次提问叠加多项任务**（本人订单 + 退货规则等） | Supervisor 按需委派 FAQ / Order / General 三个专职 Agent，LangGraph 主图汇总；Reviewer 规则节点核查缺失证据、结果冲突与越权风险；订单归属始终由 FastAPI 业务接口核验 | 复合咨询不再靠单轮问答拼装；越权陈述、无依据数值直接转安全答复 |
| 2 | **长对话摘要丢失订单号与客户约束** | 「摘要水位线 + 最近原文 + 关键条件回补」，Agent 图状态写入 PostgreSQL 检查点 | 10 条固定长对话关键锚点 **12/30 → 28/30**（展示图口径）；另有 10 条 ×12 轮复测，旧图 **0/10**、新图 **9/10** 找回全部预埋信息 |
| 3 | **向量命中 ≠ 政策适用** | Qdrant 召回后再校验文档发布状态、商品适用范围与有效期；稳定分片 ID、按租户隔离 | 24 道固定中文题，无答案片段拒绝 **2/12 → 11/12**（展示图口径）；最终验收 Recall@3 **12/12**、拒绝 **12/12** |
| 4 | **Agent 过程难追溯、断线可能丢答复** | 每轮委派、工具、检索证据与模型用量写入 `agent_trace_runs`；MySQL 持久化聊天轮次，独立 Worker 先落库再经 WebSocket 推送 | 决策轨迹可按 `trace_id` 查询；断线恢复的真实 MySQL + 浏览器复验**仍待完成**，不宣称已验收 |

**代码落点**：[四 Agent 主图](./python_agent/app/agent/support/) · [知识库证据门控](./python_agent/app/kb/) · [持久化聊天轮次](./python_business/business/chat/turns.py) · [决策轨迹](./python_agent/app/services/trace.py) · [金标与评测脚本](./python_agent/evaluation/)

> ⚠️ 「展示图口径」与「实测记录」来自不同评测集和闸门，不可互相换算；完整口径、样本量与限制见 [验证与测试](#evaluation)。评测对象是演示数据（2 条演示订单、2 个源文档），不代表真实商家效果。

---

## 📸 界面预览

| 平台管理员 · 租户管理 | 租户 · 工单管理（人工接管） | 终端用户 · SDK 在线客服 |
|---|---|---|
| ![平台后台](./docs/cs_images/平台-租户管理.PNG) | ![租户工单](./docs/cs_images/租户-工单管理.PNG) | ![SDK演示](./docs/cs_images/sdk演示聊天.PNG) |

> 📖 **图文操作手册**（20+ 张截图逐步讲解每个模块）：[docs/README.md](./docs/README.md) · **5 分钟快速体验**：[docs/快速体验.md](./docs/快速体验.md)

<a id="quick-start"></a>

## 🚀 快速开始

```bash
# 1. 克隆项目
git clone https://github.com/liunian66666/support-multiagent.git
cd support-multiagent

# 2. 配置环境变量（首次启动前必须修改数据库密码等占位符）
cp .env.example .env

# 3. 一键启动（Docker Compose）
docker compose up -d --build

# 4. 等待健康检查通过
docker compose ps

# 5. 访问系统
# 管理后台     http://localhost:8080/admin/
# 访客免登录咨询 http://localhost:8080/chat/pc.html
# 演示买家登录   http://localhost:8080/chat/login.html
# SDK 示例     http://localhost:8080/sdk/
```

**默认账号**（开发环境；管理端凭证与演示客户凭证完全分离）：

| 角色 | 凭证 | 说明 |
|------|------|------|
| 平台管理员 | `admin_key` / `admin_secret` | 管理后台登录，可换管理员令牌 |
| 租户（demo） | `demo_key` / `demo_secret` | 管理后台登录；旧商家预览链接仍兼容，但不应分享带密钥的 URL |
| 演示客户 | buyer1 / buyer2；密码默认 `demo_buyer_secret` | 在 `/chat/login.html` 登录，仅演示虚构订单归属 |

> ⚠️ **登录后第一件事**：到「平台模型配置」配置对话模型 + 向量模型，AI 对话才会生效（界面支持一键测试连通性）。

**登录入口**：免登录的 `/chat/pc.html`、`/chat/h5.html`、`/sdk/` 只能咨询 FAQ 与转人工；`/chat/login.html` 用演示客户账号登录后可查本人演示订单；管理后台走 `#/login-account?tid=<租户ID>`。演示客户登录由 `.env` 的 `DEMO_CUSTOMER_DEMO_BUYER_KEY_SECRET` 注入，`CS_AUTH_DEMO_CUSTOMER_LOGIN=false` 可关闭。消息发出后即使页面断开，维护进程仍会处理已受理轮次，失败轮次可在页面重试；旧库升级需先执行 [会话迁移说明](./docs/durable_chat_turns.md)。**生产环境应关闭演示客户登录、演示访客与旧链接预览三个入口**，详见 [产品使用说明文档](./docs/产品使用说明文档.md)。

## 🧩 本仓库相对上游做了什么

上游：[myrockey/ai_customer_service](https://github.com/myrockey/ai_customer_service)。原项目架构及多租户隔离、RBAC、知识库、工单、Prompt 版本管理等基础功能由原作者实现；本仓库的改动：

- **Go → Python FastAPI 业务层迁移并模块化**：139 条路由由 `route_baseline.json` 锁定，原 `go_service/` 已移除（`python_business/`）。
- **电商客服 Agent 整合**：四 Agent 主图、Reviewer 证据/权限门控、意图识别、只读订单工具（`python_agent/app/agent/support/`）。
- **客户身份与可恢复会话**：订单按「租户 + 客户」双条件限定归属，未知单号与「非本人订单」返回一致提示；消息与轮次同事务入库，Worker 先落库再推送（`business/customer/`、`business/chat/`）。
- **可复算的评测与决策轨迹**：金标、采集/评分脚本与 `agent_trace_runs`（`python_agent/evaluation/`、`python_agent/app/services/trace.py`）。

## ✨ 核心能力

| 领域 | 能力 |
|---|---|
| 对话与协作 | 四 Agent LangGraph 主图、JSON/SSE 回复、RAG 证据筛选、只读订单工具、水位线会话记忆 |
| 人工与会话 | 工单管理、人工接管、实时推送、断线后继续处理、登录客户近 7 天历史会话 |
| 租户与治理 | 数据/知识库/模型配置隔离、用量配额与计费、Prompt 版本与回滚、四级菜单权限、登录限流 |
| 接入与运维 | SDK（cs-chat.js）/ iframe / API / WebSocket、Docker Compose、Loki + Prometheus + Grafana、决策轨迹 |

## 🏗️ 系统架构

```
浏览器 / 第三方站点 ── SDK · iframe · API · WebSocket ──▶ Nginx :8080
    ├─▶ FastAPI 业务层 :8001  鉴权 / 租户隔离 / 工单 / WebSocket ──▶ MySQL 8.4
    └─▶ Python Agent :8000    四 Agent 主图 / RAG / 人工接管中断
             ├─▶ PostgreSQL  检查点 / Prompt / 用量 / agent_trace_runs
             ├─▶ Qdrant      知识库向量
             └─▶ Redis       限流 / 黑名单 / 配置广播 / 索引队列 / 定时锁
独立 Worker：领取客户聊天轮次 → 保存回答 → 定时归档（至少保留一个实例）
可观测栈：Grafana :3000 / Prometheus :9090 / Loki :3100；Jaeger 为可选容器，尚未接入 OTel
```

普通 JSON/SSE 聊天固定进入四 Agent 主图，`FOUR_AGENT_GRAPH` 与 `X-Agent-Graph` 不再切换旧图；旧会话历史首次进新图时只读导入，未完成审批继续沿原检查点恢复。主图按会话串行写检查点，摘要不能作为订单状态或店铺政策依据。Compose 中演示用模型覆盖 `FOUR_AGENT_MODEL_NAME=glm-5.3`，接入真实租户前须核对或移除。详见 [四 Agent 主图](./python_agent/docs/four_agent/README.md)。

## ⚠️ 能力边界（未开放的部分）

- **真实订单数据**：`orders` 为演示数据（2 条）；换真实订单或接业务 API 时归属校验逻辑不变，见 [订单接入契约](./docs/ORDER_ADAPTER_CONTRACT.md)。
- **退款与支付**：需业务写接口、交易幂等、独立审批与审计，当前不注册任何写入工具。
- **真实客户身份**：演示登录只证明「签发了这个选择」，不证明访问者本人；真实接入应校验商家现有凭证后再签发同结构会话。
- **Jaeger 链路追踪**：容器与端口已编排，Python 服务尚未接入 OpenTelemetry SDK，UI 暂无 span；当前轨迹以 `agent_trace_runs` 为准。

<a id="evaluation"></a>

## 🧪 验证与测试

以下为**已有记录**，注明对象与口径；不能把字面规则通过率当成答案忠实度。当前工作区另有未提交改动，历史结果不等于这些改动已完成完整回归。

| 检查项 | 已记录结果与限制 | 依据 |
|---|---|---|
| 业务与路由 | 2026-10-23 镜像验收 **330 项通过**（当时 132 条路由）；当前路由契约 **139 条（含 2 个 WS）**，契约测试通过 | [评测基线](./docs/EVALUATION_BASELINE.md) |
| 知识库证据 | 24 题（12 有答案 / 12 无答案）Recall@3 **12/12**、无答案拒绝 **12/12**；仅 2 个源文档，不能推断真实商家效果 | [评测基线](./docs/EVALUATION_BASELINE.md) |
| 四 Agent 留出题 | DeepSeek 36 题旧/新图字面闸门 **34/36**，新图复跑 **33/36**；p50 **7.8s / 3.4s**、token **149.3k / 43.3k**。质量差异在样本波动内，成本更高 | [逐题差异](./python_agent/evaluation/four_agent/runs/heldout_diff_dsv41_off_vs_on.md) |
| 长对话记忆 | 10 条 ×12 轮复测，旧图 **0/10**、新图 **9/10** 找回全部预埋信息；整条对话墙钟 p50 **34.8s / 177.1s**，不是单轮时延 | [逐题对照](./python_agent/evaluation/four_agent/runs/mem_diff_dsv41_r4_off_vs_r5_on.md) |
| 经验召回（RSI） | Qwen 50 题 off/inject 各两轮字面分数 **41/43、43/43**；off 自身波动 2 题，人工核查表面改善为 0，无质量提升证据 | [Qwen 对照](./python_agent/evaluation/runs/2026-10-25-qwen-rsi/README.md) |
| 断线处理 | 2026-10-29 基线出现「1 条用户消息、0 条 AI 消息」；已改为 MySQL 持久轮次 + 独立 Worker，**待真实 MySQL 与浏览器断线复验** | [断线基线](./docs/chat_turn_baseline.md) |

本地本次确认 **351 个业务测试**、**116 个四 Agent 测试**可收集，完整套件本次未跑完，因此不写成「已通过」。复现入口见 [四 Agent 评测说明](./python_agent/docs/four_agent/README.md)，逐阶段验收见 [MERGE_ACCEPTANCE.md](./docs/MERGE_ACCEPTANCE.md)。

## 📚 文档导航

| 文档 | 内容 |
|---|---|
| [图文操作手册](./docs/README.md) · [5 分钟快速体验](./docs/快速体验.md) | 配图操作流程、最快跑通路线 |
| [产品设计文档](./docs/产品设计文档.md) · [模块功能说明文档](./docs/模块功能说明文档.md) | 产品定位、多租户与安全设计、数据模型 |
| [架构与开发指南](./docs/05-架构与开发指南.md) · [业务层模块结构](./docs/BUSINESS_MODULARIZATION.md) | 目录结构、端口清单、模块职责与开发规范 |
| [四 Agent 主图](./python_agent/docs/four_agent/README.md) · [实际效果报告](./python_agent/docs/four_agent/实际效果报告.md) | 图状态、持久化、逐题质量与时延成本口径 |
| [评测基线](./docs/EVALUATION_BASELINE.md) · [断线可恢复聊天](./docs/durable_chat_turns.md) | 阶段结果、MySQL 升级与 Worker 验证步骤 |
| [产品使用说明文档](./docs/产品使用说明文档.md) · [客户接入手册](./docs/接入手册.md) · [运维监控指南](./docs/运维监控指南.md) | 全角色使用、第三方接入、监控排障 |

## ❓ 常见问题

**Q：有在线演示网址吗？**
没有。旧版 README 指向的是**上游项目**的演示站，部署的不是本仓库代码，已移除；请勿把它当作本项目效果。请用 `docker compose up -d --build` 本地启动（[快速开始](#quick-start)），或看 [界面截图手册](./docs/README.md)。自建演示站上线后按文件顶部注释替换为自己的域名。

**Q：接口返回 429 / AI 没反应？**
429 是防爆破锁定（同一 IP+key 连败 5 次锁 10 分钟，状态在 Redis）；AI 无响应先检查「平台模型配置」的对话/向量模型与 API Key。

**Q：修改了代码如何生效？**
业务层 `docker compose up -d --build business business-maintenance`；Agent `... --build agent`；前端 `... --build nginx`；nginx.conf 改动后 `docker exec SM_nginx nginx -s reload`。

**Q：怎么查看某轮对话的执行轨迹？**
用返回的 `trace_id` 查 PostgreSQL `agent_trace_runs`，或调 `GET /agent/traces/{trace_id}`（需 `X-Internal-Token` 与 `X-Tenant-Id`）。

## 📄 开源协议与致谢

本仓库以 **[AGPL-3.0](./LICENSE)** 分发，与上游保持一致，**不提供商业授权**。

| 范围 | 归属 |
|---|---|
| 原始项目架构，以及多租户隔离、RBAC、知识库、工单、Prompt 版本管理等基础功能 | © [myrockey/ai_customer_service](https://github.com/myrockey/ai_customer_service) 原作者 |
| Go → Python 业务层迁移、模块化拆分、电商客服 Agent 整合、多专家路由、决策轨迹 | 本仓库维护者 |

> AGPL-3.0 核心约束：基于本项目的修改若以网络服务形式对外提供，须公开完整源码。上游商业版（生产部署包、监控告警配置等）由原作者提供，与本仓库无关。

---

<div align="center">

**© 2026** · [AGPL-3.0](./LICENSE) · 基础架构归属 [myrockey/ai_customer_service](https://github.com/myrockey/ai_customer_service) 原作者

</div>
