<div align="center">

# 多 Agent 电商智能客服系统

**AI 自动应答 + 人工客服接管 + 知识库问答 + 多租户独立配置**

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](./LICENSE)
[![Online Demo](https://img.shields.io/badge/Demo-在线体验-brightgreen)](https://cs.hwcom.top)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ed?logo=docker)
![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python)
![Vue3](https://img.shields.io/badge/Vue-3-4FC08D?logo=vuedotjs)
![Tests](https://img.shields.io/badge/Tests-pytest-success)

🚀 **在线体验（无需部署，直接看效果）：https://cs.hwcom.top**

</div>

> **2026.09—2026.11｜FastAPI、LangGraph、Qdrant、MySQL、PostgreSQL、Redis、WebSocket**  
> 基于开源客服系统二次开发，面向电商政策问答、本人订单查询、复合咨询与人工接管；重点实现 Agent 协作、长对话记忆、RAG 证据校验和可复算的对照评测。

---

## 🎯 项目问题与实现

- **一次提问包含查单与退货规则等多项任务**：构建 Supervisor、FAQ、Order、General 四 Agent LangGraph 主图，按需委派并汇总结果；规则 Reviewer 校验缺失证据、结果冲突和越权风险。订单归属始终由 FastAPI 业务接口核验。
- **长对话摘要可能丢失订单号与客户约束**：采用“摘要水位线＋最近原文＋关键条件回补”，由 PostgreSQL 检查点保存 Agent 图状态。按项目展示图提供的口径，10 条固定长对话中，模型上下文关键锚点从 **12/30 提升至 28/30**。
- **向量命中并不代表政策适用**：Qdrant 召回后校验文档发布状态、商品适用范围和有效期。按项目展示图提供的口径，24 道固定中文题中，12 道无答案题的片段拒绝从 **2/12 提升至 11/12**。
- **Agent 过程难追溯、连接断开可能丢失答复**：记录委派、工具调用、检索证据和模型用量；MySQL 持久化聊天轮次，独立 Worker 处理已受理消息，结果先落库，再经 WebSocket 推送。断线恢复与故障场景的验收范围见下方验证记录。


## 📸 界面预览

| 平台管理员 · 租户管理 | 租户 · 工单管理（人工接管） | 终端用户 · SDK 在线客服 |
|---|---|---|
| ![平台后台](./docs/cs_images/平台-租户管理.PNG) | ![租户工单](./docs/cs_images/租户-工单管理.PNG) | ![SDK演示](./docs/cs_images/sdk演示聊天.PNG) |

> 📖 **图文操作手册**（20+ 张截图逐步讲解每个模块）：[docs/README.md](./docs/README.md) · **5 分钟快速体验**：[docs/快速体验.md](./docs/快速体验.md)

## 🧭 本仓库做了什么

本仓库基于 [myrockey/ai_customer_service](https://github.com/myrockey/ai_customer_service) 的多租户客服系统二次开发：在原项目之上整合电商客服 Agent，并把原 Go 业务服务完整迁移到 Python FastAPI。**原始项目的架构与绝大部分功能由原作者实现**；迁移过程保留在 Git 提交历史中，本节只列本仓库的改动。

| 改动 | 说明 | 位置 |
|---|---|---|
| Python 业务层迁移 | 客户会话、商家管理、认证、工单、Agent 内部接口及 WebSocket 全部由 FastAPI 提供；新库租户初始化与每日消息归档也由 Python 完成。原 `go_service/` 已移除 | `python_business/`、`deploy/init/mysql/init.sql` |
| 业务层模块化 | 原单体 `main.py` 拆为职责模块，`main.py` 只保留入口；当前 139 条路由的方法、路径、名称和注册顺序由 `route_baseline.json` 锁定 | `python_business/business/`、`python_business/tests/test_route_manifest.py` |
| 客户身份与只读订单 | FastAPI 签发客户会话，订单查询按「租户 + 客户」双条件限定归属；未知单号与「非本人订单」返回完全一致的提示，不暴露订单存在性 | `business/customer/routes.py`、`python_agent/app/agent/tools.py` |
| 电商客服意图与 FAQ | 区分政策咨询、订单查询、主动转人工和退款/支付执行请求；政策回答保留知识来源，不把演示政策冒充真实商家承诺 | `python_agent/app/chat/`、`python_agent/app/api/chat.py` |
| 四 Agent 协作 | Supervisor 动态委派 FAQ、Order、General Agent，Reviewer 规则节点校验证据和权限；复合咨询在 LangGraph 主图中汇总 | `python_agent/app/agent/support/`、`python_agent/app/services/four_agent.py` |
| 知识库证据门控 | 文档审核状态、商品适用范围和有效期参与 FAQ 与复合问答的证据筛选；索引使用稳定分片 ID | `python_agent/app/kb/`、`python_agent/evaluation/` |
| 可恢复客户会话 | 客户消息与处理任务同事务入库，独立维护进程保存回答；登录客户可管理自己的历史会话 | `python_business/business/chat/turns.py`、`worker.py`、`web/chat/conversations.js` |
| 可追溯的决策轨迹 | 每轮对话的路由、工具、检索证据、模型用量写入 PostgreSQL `agent_trace_runs`；字段白名单过滤，用户原话与提示词不入库 | `python_agent/app/services/trace.py` |
| 前后端联通 | Nginx 将 Vue 后台、客户聊天页、SDK 的 API 与 WebSocket 统一接入 Python；商家实时接收本租户转人工工单 | `deploy/nginx.conf`、`web/`、`admin-app/` |

## ✨ 核心能力

| 能力 | 说明 |
|------|------|
| 🤖 AI 智能对话 | 四 Agent LangGraph 主图、JSON/SSE 回复、RAG 证据筛选、只读订单工具与水位线会话记忆 |
| 👨‍💼 人工客服 | 工单管理、会话查看、人工接管对话、实时推送通道 |
| 📚 知识库管理 | 文档上传与审核、适用范围和有效期管理、稳定分片索引、按租户隔离的检索与证据门控 |
| 💬 客户会话 | 断线后后台继续处理、重连恢复答复和失败状态；登录客户可管理近 7 天历史会话 |
| 🧠 经验召回实验 | 支持 `off`、`shadow`、`inject` 策略与版本治理；已有对照尚未证明注入能提升答复质量 |
| 📝 Prompt 管理 | 版本控制、回滚、租户级 + 平台级 Prompt 优先级 |
| 🏢 多租户 | 数据/知识库/模型配置隔离、用量配额、平台/租户双级管理 |
| 👤 账号与安全 | 平台/租户管理员账号密码登录（专属链接 `#/login-account?tid=` 自动填充租户）、修改密码、连续输错密钥触发登录限流防爆破 |
| 🔐 菜单权限 | 四级菜单体系（分组→目录→页面→按钮）、角色四层授权树、批量启停/隐藏、行内改排序、拖拽调整归属、平台/租户按 scope 隔离、内置菜单一键重置 |
| 🔌 第三方集成 | SDK（cs-chat.js 浮窗）、iframe、API、WebSocket 四种接入方式 |
| 💰 用量计费 | Token 用量统计（按模型/来源），支持单价配置与费用核算 |
| 📊 可观测性 | Loki 日志聚合 + Prometheus 指标 + Grafana 看板；容器内结构化日志带 trace_id / tenant_id |
| 🚢 容器化部署 | Docker Compose 一键起全部服务，支持镜像打包 / CI-CD / 私有化部署 |

## 🏗️ 系统架构

```
用户浏览器 / 第三方网站
        │  SDK(cs-chat.js) / iframe / API / WebSocket
        ▼
┌──────────────────────────────────────────────────┐
│  Nginx (8080)  统一入口：静态前端 + API/WS 反代    │
└───────────────┬──────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────┐
│  Python FastAPI 业务层 (8001 内网)：鉴权/租户隔离/ │
│  路由/工单/模型配置/用量报表/审计/WebSocket         │
└───────┬──────────────────────┬──────────────────┘
        ▼                       ▼
┌──────────────────┐   ┌──────────────────────────┐
│ MySQL 8.4 (3306) │   │ Python Agent (8000 内网)  │
│ 业务库/工单/消息  │   │ LangGraph：四 Agent/RAG/   │
│ /会话轮次/审计     │   │ 工具调用/人工接管中断     │
└──────────────────┘   └──────┬─────────┬─────────┘
                              ▼         ▼
                     PostgreSQL (5432)  Qdrant (6333)
                     会话/Prompt/用量   知识库向量
                     决策轨迹 trace
┌──────────────────────────────────────────────────┐
│ Redis 7（内网 6379，强制依赖）：登录防爆破/黑名单/ │
│ 模型配置失效广播/知识库索引队列/定时任务锁          │
└──────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────┐
│ 可观测栈：Grafana(3000)/Prometheus(9090)/Loki(3100)│
│ Jaeger(16686) 为可选容器，需 --profile observability│
└──────────────────────────────────────────────────┘
```

独立的 Python `business-maintenance` 服务负责领取客户聊天轮次、保存处理结果和定时归档消息。客户断线后，已受理轮次仍由该服务处理；至少要保持一个维护进程运行。

## 当前 Agent 入口与兼容范围

普通 JSON/SSE 聊天固定进入 `python_agent/app/agent/support/` 中的四 Agent LangGraph 主图；`FOUR_AGENT_GRAPH` 环境变量与 `X-Agent-Graph` 请求头不再切换旧图。主图使用持久化检查点，旧会话的历史消息首次进入新图时只读导入。已有未完成审批继续沿原检查点恢复，审批完成后新聊天进入主图。退款、支付执行仍未开放。

主图按会话串行写入 PostgreSQL 检查点。Reviewer 对订单归属、无证据政策数值和交易请求设规则边界；摘要只帮助理解上下文，不能作为订单状态或店铺政策的依据。当前 Compose 打开 `SUPPORT_V2_THREAD_LOCK`，并设置了演示用模型覆盖值 `FOUR_AGENT_MODEL_NAME=glm-5.3`；接入真实租户前须核对或移除该覆盖值。

历史评测报告保留旧版对照数据；其中的旧入口开关说明仅反映当时实验配置。

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
| 租户（demo） | `demo_key` / `demo_secret` | 管理后台登录；旧商家预览链接仍兼容，但不应继续分享带密钥的 URL |
| 演示客户 | buyer1 / buyer2；密码默认 `demo_buyer_secret` | 在 `/chat/login.html` 登录，仅演示虚构订单归属 |

> ⚠️ **登录后第一件事**：到「平台模型配置」配置对话模型 + 向量模型，AI 对话才会生效（界面支持一键测试连通性）。

**登录方式的三种入口**：

| 入口 | 是否需要密码 | 能做什么 |
|---|---|---|
| `/chat/pc.html`、`/chat/h5.html`、`/sdk/` | 否 | 咨询 FAQ、转人工；**不能查订单** |
| `/chat/login.html` | 是（演示客户密码） | 额外可查本人演示订单 |
| `#/login-account?tid=<租户ID>` | 是（管理员账号） | 租户成员登录管理后台，链接自动填充租户 ID |

演示客户 secret 经 `.env` 的 `DEMO_CUSTOMER_DEMO_BUYER_KEY_SECRET` 注入，`CS_AUTH_DEMO_CUSTOMER_LOGIN=false` 可整体关闭。登录页使用 POST 提交密码，网址与浏览器存储均不保存密码，只保存两小时客户会话令牌；旧版 URL 中的密码会在首次请求时经过 Web 服务器，应停止分享旧链接。商家旧链接预览由 `CS_AUTH_MERCHANT_PREVIEW_LOGIN` 控制，预览会话**不能读取订单**。生产环境应关闭演示客户登录、演示访客与旧链接预览三个入口。

登录客户在 PC/H5 聊天页可查看近 7 天会话、切换、新建、重命名和删除；访客与商家预览会话不提供跨登录历史。消息发出后即使页面断开，维护进程仍会处理已受理轮次；失败轮次可在页面重试。旧数据库升级需先执行 [会话迁移说明](./docs/durable_chat_turns.md) 中的 MySQL 迁移脚本，新库由初始化 SQL 建表。

## ⚠️ 能力边界（未开放的部分）

订单查询与归属校验逻辑已就绪，使用演示订单数据验证（2 条）。以下功能**未**开放，避免用演示数据冒充真实业务：

- **真实订单数据**：`orders` 表现为演示数据，真实商家接入时只需换成真实订单数据或接业务 API，归属校验逻辑不变；接入条件见 [ORDER_ADAPTER_CONTRACT.md](./docs/ORDER_ADAPTER_CONTRACT.md)
- **退款与支付**：需业务写接口、交易幂等、独立审批与审计，当前不注册任何可调用的写入工具
- **真实客户身份**：演示客户登录只适合严格隔离的虚构数据演示——服务端签发令牌只能证明「签发了这个选择」，不能证明访问者是 buyer1 本人；真实接入时关闭该开关，改为校验商家现有登录凭证后再签发同结构会话（会话/归属/查询链路不变）
- **Jaeger 链路追踪**：容器与端口已编排，但 Python 服务尚未接入 OpenTelemetry SDK，UI 中暂无 span 数据；当前决策轨迹以 `agent_trace_runs` 表为准

## 🧪 验证与测试

以下为**已有记录**，注明评测对象与口径；不能把字面规则通过率当成答案忠实度。当前工作区另有未提交改动，下面的历史结果不等于这些改动已完成完整回归。

| 检查项 | 已记录结果与限制 | 依据 |
|---|---|---|
| 业务与路由 | 2026-10-23 镜像验收为 **330 项业务测试通过**，当时路由为 132 条；当前路由契约清单为 **139 条（含 2 个 WebSocket）**，本次契约测试通过 | [评测基线](./docs/EVALUATION_BASELINE.md)、`python_business/tests/route_baseline.json` |
| 知识库证据 | 演示租户 24 题（12 有答案、12 无答案）中，最终证据的目标源文档 Recall@3 为 **12/12**，无答案拒绝片段 **12/12**；仅 2 个源文档，不能推断真实商家效果 | [知识库最终验收](./docs/EVALUATION_BASELINE.md) |
| 四 Agent 同模型留出题 | DeepSeek 36 题旧/新图按严格字面闸门均为 **34/36**，新图复跑为 **33/36**；首轮新图 p50 **7.8 秒**、旧图 **3.4 秒**，token **149.3k/43.3k**。质量差异处于样本波动内，成本明显更高 | [逐题差异](./python_agent/evaluation/four_agent/runs/heldout_diff_dsv41_off_vs_on.md)、[实际效果报告 §8g](./python_agent/docs/four_agent/实际效果报告.md) |
| 另一模型留出题 | GLM 同模型两次 36 题均为旧图 **34/36**、新图 **35/36**，但逐题均有 **1 例字面回退**，发布闸门未放行；这不能表述为整体质量提升 | [第 2 轮逐题差异](./python_agent/evaluation/four_agent/runs/glm_heldout_diff_r2.md) |
| 长对话记忆 | DeepSeek 10 条、每条 12 轮的后续复测中，旧图 **0/10**、新图 **9/10** 找回全部预埋信息；整条对话的墙钟 p50 为 **34.8/177.1 秒**，不能当成单轮时延 | [逐题对照](./python_agent/evaluation/four_agent/runs/mem_diff_dsv41_r4_off_vs_r5_on.md) |
| 经验召回（RSI） | Qwen 真模型 50 题、off/inject 各两轮，字面分数分别为 **41/43、43/43**；off 自身也波动 2 题，人工核查首轮表面改善为 0，尚无质量提升证据 | [Qwen 对照](./python_agent/evaluation/runs/2026-10-25-qwen-rsi/README.md) |
| 断线处理 | 2026-10-29 基线实测出现“1 条用户消息、0 条 AI 消息”；当前代码改为 MySQL 持久轮次与独立 Worker，待按升级说明完成真实 MySQL 和浏览器断线复验 | [断线基线](./docs/chat_turn_baseline.md)、[持久轮次说明](./docs/durable_chat_turns.md) |

本地本次确认 **351 个业务测试**、**116 个四 Agent 测试**可收集；路由契约与新增会话相关测试 **12 通过、2 跳过**（跳过项需独立 MySQL 测试库），四 Agent 规则和评分尺子测试 **20 通过**。完整套件本次未跑完，因此不把收集数量写成“已通过”。复现入口与原始记录见 [四 Agent 评测说明](./python_agent/docs/four_agent/README.md)；逐阶段历史验收见 [MERGE_ACCEPTANCE.md](./docs/MERGE_ACCEPTANCE.md)。

## 🔌 服务端口

| 服务 | 端口 | 说明 |
|------|------|------|
| Nginx | 8080 | 前端 + API 入口 |
| MySQL | 3306 | 业务库（账号 `root`，密码取 `.env` 的 `MYSQL_ROOT_PASSWORD`） |
| PostgreSQL | 5432 | Agent 会话 checkpoint、Prompt、用量、决策轨迹 |
| Qdrant | 6333 | 向量数据库 |
| Redis | 内网 6379 | 登录限流 / Token 黑名单 / 模型配置广播 / 定时任务锁 |
| Grafana | 3000 | 监控可视化 |
| Prometheus | 9090 | 指标查询 |
| Loki | 3100 | 日志查询 |
| Jaeger | 16686 | 链路追踪 UI（可选，需 `--profile observability`） |

## 📁 目录结构

```
support-multiagent/
├── docker-compose.yaml       # 一键编排（全部服务）
├── .env.example              # 环境变量模板
├── .gitlab-ci.yml            # CI/CD 流水线（lint/test/build/deploy）
├── python_business/          # FastAPI 业务层
│   ├── main.py               #   入口（3 行，uvicorn main:app）
│   └── business/             #   按职责拆分：core/infra/customer/admin/agent/chat
├── python_agent/             # Python LangGraph Agent
│   ├── main.py               #   入口
│   ├── app/agent/            #   能力层：工具、检索、LLM 配置、FAQ/General 专职 Agent
│   ├── app/agent/support/    #   编排层：四 Agent 主图、Reviewer、记忆、状态契约
│   ├── app/api/              #   chat / kb / session / trace 等接口
│   ├── app/chat/             #   意图识别、FAQ、订单、回复
│   ├── app/services/         #   supervisor / specialists / trace / four_agent 适配器
│   ├── app/kb/               #   知识库索引与证据筛选
│   ├── evaluation/           #   金标、采集脚本与原始评测记录
│   └── tests/                #   Agent 与四 Agent 图测试
├── admin-app/                # 管理后台前端源码（Vue3 + Vite）
├── web/                      # 静态前端（chat/ 用户端、sdk/ 接入示例）
│   └── admin/dist/           #   管理后台构建产物（nginx 镜像多阶段构建时生成）
├── deploy/
│   ├── nginx.conf            #   Nginx 配置
│   └── init/                 #   初始化 SQL（mysql/、postgres/）
├── docs/                     # 文档中心（见下方导航）
├── scripts/                  # 验证 / 构建 / 运维脚本
└── thirdDemo/                # 第三方接入最小示例（Python + SDK）
```

> `deploy/` 下的 `production/`、`backup/`、`observability/` 属生产环境部署配置，已在 `.gitignore` 中排除，不在本仓库分发。

## 📚 文档导航

| 文档 | 内容 | 适用 |
|------|------|------|
| [📖 图文操作手册](./docs/README.md) | **按模块与菜单逐一配图（平台侧 + 租户侧 20+ 张）** | 所有人 |
| [⚡ 快速体验](./docs/快速体验.md) | 5 分钟跑通部署→配置→对话→转人工 | 所有人 |
| [产品设计文档](./docs/产品设计文档.md) | 产品定位、系统架构、多租户设计、安全设计 | 产品/架构师 |
| [产品使用说明文档](./docs/产品使用说明文档.md) | 全角色文字版使用指南、第三方接入、运维 | 用户/运维 |
| [模块功能说明文档](./docs/模块功能说明文档.md) | 模块详细功能、数据模型、流程设计 | 开发/测试 |
| [架构与开发指南](./docs/05-架构与开发指南.md) | 目录结构、模块职责、开发规范 | 开发者 |
| [业务层模块结构](./docs/BUSINESS_MODULARIZATION.md) | `business/` 各模块职责与依赖方向 | 开发者 |
| [四 Agent 主图](./python_agent/docs/four_agent/README.md) | 当前入口、图状态、持久化与复现命令；下半部分保留历史实验说明 | 开发/评测 |
| [四 Agent 实际效果报告](./python_agent/docs/four_agent/实际效果报告.md) | 逐题质量、长对话、检索稳定性、时延与成本口径 | 开发/评测 |
| [评测基线](./docs/EVALUATION_BASELINE.md) | 规则、检索、知识库审核与证据门控的阶段结果 | 开发/评测 |
| [断线可恢复聊天](./docs/durable_chat_turns.md) | MySQL 升级、Worker 处理与验证步骤 | 开发/运维 |
| [运维监控指南](./docs/运维监控指南.md) | Grafana/Prometheus/Loki 使用、故障排查 | 运维/SRE |
| [客户接入手册](./docs/接入手册.md) | 产品化接入步骤、Token 换取、多端接入 | 客户/第三方 |
| [订单接入契约](./docs/ORDER_ADAPTER_CONTRACT.md) | 真实订单适配器要求与验收覆盖 | 集成方 |
| [CI/CD 流水线说明](./docs/CICD流水线使用说明.md) | 流水线架构、部署脚本联动 | 运维/DevOps |
| [ACR 镜像上传说明](./docs/阿里云ACR镜像上传说明.md) | 镜像打包上传、版本管理 | DevOps |

## ❓ 常见问题

**Q：接口返回 429？**
防爆破锁定（同一 IP+key 连续失败 5 次锁 10 分钟）。限流状态保存在 Redis，需等待过期或由管理员处理。

**Q：AI 没反应？**
检查「平台模型配置」：对话模型/向量模型是否配置、API Key 是否正确、测试连通性是否通过。

**Q：修改了代码如何生效？**
业务层：`docker compose up -d --build business business-maintenance`；Agent：`docker compose up -d --build agent`；前端（admin-app / chat / sdk）：`docker compose up -d --build nginx`。nginx.conf 改动后执行 `docker exec SM_nginx nginx -s reload`。

**Q：知识库检索不到？**
文档状态需为「已索引」；更换 Embedding 模型后必须到知识库页重新索引。

**Q：脚本在 WSL 执行报错？**
仓库脚本为 LF 行尾；若从 Windows 检出为 CRLF，先 `sed -i 's/\r$//' scripts/*.sh`。

**Q：怎么查看某轮对话的执行轨迹？**
对话返回的 `trace_id` 对应 PostgreSQL `agent_trace_runs` 表，也可通过内部接口 `GET /agent/traces/{trace_id}` 查询（需 `X-Internal-Token` 与 `X-Tenant-Id`）。

## 📄 开源协议与致谢

本仓库以 **[AGPL-3.0](./LICENSE)** 分发，与上游项目保持一致。

| 范围 | 归属 |
|---|---|
| 原始项目架构，以及多租户隔离、RBAC、知识库、工单、Prompt 版本管理等基础功能 | © [myrockey/ai_customer_service](https://github.com/myrockey/ai_customer_service) 原作者 |
| Go → Python 业务层迁移、业务层模块化拆分、电商客服 Agent 整合、多专家路由、决策轨迹 | 本仓库维护者 |

**本仓库不提供商业授权。** 上游项目的商业版（生产部署包、监控告警配置等）由原作者提供，与本仓库无关；如需闭源商用，请遵守 AGPL-3.0 或自行联系上游作者获取授权。

> AGPL-3.0 核心约束：基于本项目的修改若以网络服务形式对外提供，须公开完整源码。

如果这个项目对你有帮助，欢迎点 ⭐ Star 或提 Issue 🙌

---

<div align="center">

**© 2026** · [AGPL-3.0](./LICENSE) · 基础架构归属 [myrockey/ai_customer_service](https://github.com/myrockey/ai_customer_service) 原作者

</div>
