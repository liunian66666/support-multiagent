<div align="center">

# 智能客服系统（多租户 SaaS 版）

**AI 自动应答 + 人工客服接管 + 知识库问答 + 多租户独立配置**

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](./LICENSE)
[![Online Demo](https://img.shields.io/badge/Demo-在线体验-brightgreen)](https://cs.hwcom.top)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ed?logo=docker)
![Go](https://img.shields.io/badge/Go-1.25-00ADD8?logo=go)
![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python)
![Vue3](https://img.shields.io/badge/Vue-3-4FC08D?logo=vuedotjs)

🚀 **在线体验（无需部署，直接看效果）：https://cs.hwcom.top**

</div>

> 多租户智能客服系统：**Go 网关 + Python LangGraph Agent + MySQL/PostgreSQL/Qdrant + Redis**，支持第三方网站 / App / 小程序多端接入，AI 自动应答 + 人工接管完整闭环，开箱即用。

---

## 本仓库说明：在原项目基础上的整合优化

本仓库基于 [myrockey/ai_customer_service](https://github.com/myrockey/ai_customer_service) 的多租户客服系统，在其之上完成「电商客服 Agent 能力整合」优化。**原始项目的架构与绝大部分功能由原作者实现**，本节只记录本次优化新增与修改的部分，便于区分归属。

### 本次优化内容

| 优化项 | 说明 | 位置 |
|---|---|---|
| 统一入口身份校验 | Go 向 Agent 透传内部令牌与租户头，Agent 校验令牌有效性及 `thread_id` 租户前缀一致，阻断绕过网关直连 | `agent/client.go`、`python_agent/main.py` |
| 订单查询真实查询与归属校验 | `orders` 表加 `tenant_id`+`customer_id` 字段并灌演示数据；`BusinessOrder` 从 fail-closed 503 改为查表+比对 `customer_id==thread_id`，命中返回状态/商品/金额/下单日期，不命中 fail-closed 不暴露订单存在性；Python 按	body.code 分支返回（修复 Go StatusCode 中间件把 body.code 映射到 HTTP status 导致 raise_for_status 误异常） | `handler/user.go`、`model/model.go`、`python_agent/tools.py` |
| 迁移客服流程 FAQ | 从外部客服 Agent 项目迁入通用流程问答与回答边界，意图关键词匹配（退货/退款/订单状态/能力），常见变体问法可命中；答复标注来源，未命中时拒答而非编造商家政策 | `python_agent/support_faq.py` |
| 转人工意图扩展 | 识别「找人工」「人工客服」「转接」「投诉」等说法并区分建单原因；意图优先级：转人工 > 具体订单号 > 流程 FAQ > LLM，避免订单请求被流程答复截胡 | `python_agent/support_faq.py`、`python_agent/main.py` |
| 转人工流程修正 | 区分用户主动转人工与中断恢复，避免建单后重复 resume 打断会话；前端按钮文案与发送文本统一，不承诺等待时长 | `handler/ws.go`、`chat/chat.go`、`web/chat/pc.html` |
| 知识库索引一致性修复 | 重索引后检索器自动重建（原先需重启服务才恢复）；按 `metadata.doc_id` 清理旧向量（原先删除条件未命中，导致残留与重复） | `python_agent/tools.py`、`kb_index_worker.py`、`routers_kb.py` |
| 演示政策知识 | 为演示租户补录示例政策文档，运费、发货时效、退款到账等可检索作答；知识库未覆盖的问题明确拒答而非编造 | `python_agent/kb_seed/demo-store-policy-v1.md` |
| 确定性答复写入检查点 | 流程 FAQ / 订单核验 / 转人工原本绕过 LangGraph，后续 LLM 读不到这次问答；现通过旁路图（与主 Agent 共享同一 checkpointer 与 thread_id）把问答追加到检查点，长对话上下文连续；工单未完结时跳过，写入失败不影响答复 | `python_agent/agent_factory.py`、`python_agent/main.py` |
| 订单号格式扩展 | 由单一 `ORD-xxx` 演示格式扩展至电商前缀（TB/JD/PDD/DY/TMALL/1688）与显式上下文（「订单号是 xxx」「单号: xxx」），识别真实商家订单号；纯短数字与手机号不误匹配 | `python_agent/support_faq.py` |
| 「订单号怎么找」FAQ 化 | 高频问题原先走 LLM，答法不一；现做成确定性 FAQ，分点列出购买记录/收货短信/电子发票/支付凭证四个查找位置 | `python_agent/support_faq.py` |
| 订单核验失败自动转人工 | 核验失败时不再只返回文本「请转人工」，自动触发 interrupt 建单，`reason` 标注「订单核验失败（订单号 xxx），转人工处理」；工单未完结时不重复建单 | `python_agent/main.py` |
| 历史订单上下文记忆 | 用户追问「刚才那个订单现在怎样」「之前那个快递到哪了」时，从检查点历史消息复用最近订单号，避免 LLM 答非所问 | `python_agent/support_faq.py`、`python_agent/main.py` |

### 能力边界（未开放的部分）

订单查询与归属校验逻辑已就绪，使用演示订单数据验证（5 条，覆盖待发货/已发货/已完成/已取消）。以下功能**未**开放，避免用演示数据冒充真实业务：

- **真实订单数据**：`orders` 表现为演示数据，真实商家接入时只需换成真实订单数据或接业务 API，归属校验逻辑不变；接入条件见 [ORDER_ADAPTER_CONTRACT.md](./docs/ORDER_ADAPTER_CONTRACT.md)
- **退款与支付**：需业务写接口、交易幂等、独立审批与审计，当前不注册任何可调用的写入工具
- **客户级会话身份**：现有令牌证明租户，未绑定具体访客；订单归属校验基于 `user_id` 字符串比对，同租户内冒用 `user_id` 可读他人订单——演示不触发，真实业务接入前需可信宿主签发绑定访客身份的会话令牌

### 验收记录

- [MERGE_GOAL.md](./docs/MERGE_GOAL.md)：合并目标、分阶段验收标准与写入操作门禁
- [MERGE_ACCEPTANCE.md](./docs/MERGE_ACCEPTANCE.md)：逐阶段实测结果、可复现命令与未完成边界
- [RESUME_CLAIMS.md](./docs/RESUME_CLAIMS.md)：可对外表述的范围界定

复现方式：

```bash
docker compose exec -T agent python -m unittest test_support_faq.py test_faq_bridge.py -v   # Python 9+3 项通过
docker build --target builder -t cs-go-merge-test -f go_service/Dockerfile .
docker run --rm cs-go-merge-test go test ./...                            # Go 通过
```

---

## 📸 界面预览

| 平台管理员 · 租户管理 | 租户 · 工单管理（人工接管） | 终端用户 · SDK 在线客服 |
|---|---|---|
| ![平台后台](./docs/cs_images/平台-租户管理.PNG) | ![租户工单](./docs/cs_images/租户-工单管理.PNG) | ![SDK演示](./docs/cs_images/sdk演示聊天.PNG) |

> 📖 **图文操作手册**（10 张截图逐步讲解每个模块怎么操作）：[docs/README.md](./docs/README.md) · **5 分钟快速体验**：[docs/快速体验.md](./docs/快速体验.md)

## ✨ 核心能力

| 能力 | 说明 |
|------|------|
| 🤖 AI 智能对话 | LangGraph 编排、流式输出、RAG 知识库检索、工具调用 |
| 👨‍💼 人工客服 | 工单管理、会话查看、人工接管对话、实时推送通道 |
| 📚 知识库管理 | 文档上传、向量索引、语义检索、批量重索引、按租户隔离 |
| 📝 Prompt 管理 | 版本控制、回滚、租户级 + 平台级 Prompt 优先级 |
| 🏢 多租户 | 数据/知识库/模型配置隔离、用量配额、平台/租户双级管理 |
| 👤 账号与安全 | 平台/租户管理员账号密码登录（专属链接 `#/login-account?tid=` 自动填充租户）、修改密码、连续输错密钥触发登录限流防爆破 |
| 🔐 菜单权限 | 四级菜单体系（分组→目录→页面→按钮）、角色四层授权树、批量启停/隐藏、行内改排序、拖拽调整归属、搜索筛选、平台/租户按 scope 隔离、内置菜单一键重置、修改后免重登即时生效 |
| 🔌 第三方集成 | SDK（cs-chat.js 浮窗）、iframe、API、WebSocket 四种接入方式 |
| 💰 用量计费 | Token 用量统计（按模型/来源），支持单价配置与费用核算 |
| 📊 可观测性 | Loki 日志 + Prometheus 指标 + Jaeger 链路 + Grafana 可视化 |
| 🚢 容器化部署 | Docker Compose 一键起全部服务，支持镜像打包 / CI-CD / 私有化部署 |

## 🚀 快速开始

```bash
# 1. 克隆项目
git clone https://github.com/liunian66666/support-multiagent.git
cd support-multiagent

# 2. 配置环境变量
cp .env.example .env

# 3. 一键启动（Docker Compose）
docker compose up -d --build

# 4. 等待健康检查通过（约 1-2 分钟）
docker compose ps

# 5. 访问系统
# 管理后台   http://localhost:8080/admin/
# 用户端     http://localhost:8080/chat/pc.html
# SDK 示例   http://localhost:8080/sdk/
```

**默认账号**（开发环境）：

| 角色 | app_key | app_secret |
|------|---------|-----------|
| 平台管理员 | `admin_key` | `admin_secret` |
| 租户（demo） | `demo_key` | `demo_secret` |

> **账号登录**：租户成员也可通过专属链接 `#/login-account?tid=<租户ID>`（如 `#/login-account?tid=t_demo`）使用账号密码登录，链接自动填充租户 ID，无需手动输入。

> ⚠️ 登录后第一件事：到「平台模型配置」配置对话模型 + 向量模型，AI 对话才会生效（界面支持一键测试连通性）。

## 📜 授权与商业化

|  | 社区版（本仓库 · 开源） | 商业版（私有化交付） |
|--|--|--|
| 协议 | AGPL-3.0：学习 / 自用 / 非商业 / 继续开源 **免费** | 商业授权（[COMMERCIAL.md](./COMMERCIAL.md)） |
| 代码 | go_service / python_agent / web / admin-app / docs 全部开源 | 生产部署包（镜像 + 一键脚本 + 监控告警配置）闭源交付 |
| 适用 | 自部署、学习研究、二次开发 | 企业内部闭源部署、对外商业 SaaS、产品化销售 |
| 支持 | GitHub Issues | 部署支持 / 升级保障 / 定制开发（SSO、多实例等） |
| 获取 | 直接 clone | 邮箱 2771452556@qq.com |

> 支持开源作者：点 ⭐ Star、提 Issue、提交 PR 都是最好的支持 🙌

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
│  Go 网关 (8080 内网)  Gin：鉴权/租户隔离/限流/     │
│  路由/工单/模型配置/用量报表/审计/消息归档          │
└───────┬──────────────────────┬──────────────────┘
        ▼                       ▼
┌──────────────────┐   ┌──────────────────────────┐
│ MySQL 8.4 (3306) │   │ Python Agent (8000 内网)  │
│ 业务库/工单/消息  │   │ LangGraph：智能对话/RAG/  │
│ /租户/审计/归档   │   │ 工具调用/人工接管中断     │
└──────────────────┘   └──────┬─────────┬─────────┘
                              ▼         ▼
                     PostgreSQL (5432)  Qdrant (6333)
                     会话/Prompt/用量   知识库向量
┌──────────────────────────────────────────────────┐
│ Redis 7（内网 6379，强制依赖）：登录防爆破/黑名单/ │
│ 模型配置失效广播/知识库索引队列/定时任务锁          │
└──────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────┐
│ 可观测栈：Grafana(3000)/Prometheus(9090)/          │
│          Loki(3100)/Jaeger(16686)                 │
└──────────────────────────────────────────────────┘
```

## 🔌 服务端口

| 服务 | 端口 | 说明 |
|------|------|------|
| Nginx | 8080 | 前端 + API 入口 |
| MySQL | 3306 | 业务库（开发默认 root/123456，生产请修改 .env） |
| PostgreSQL | 5432 | Agent 会话 checkpoint |
| Qdrant | 6333 | 向量数据库 |
| Redis | 内网 6379 | 登录限流 / Token 黑名单 / 模型配置广播 / 定时任务锁 |
| Grafana | 3000 | 监控可视化 |
| Prometheus | 9090 | 指标查询 |
| Loki | 3100 | 日志查询 |
| Jaeger | 16686 | 链路追踪 UI |

## 📁 目录结构

```
customer_service_prod/
├── docker-compose.yaml       # 一键编排（全部服务）
├── .env.example              # 环境变量模板
├── .gitlab-ci.yml            # CI/CD 流水线（lint/test/build/deploy）
├── go_service/               # Go 网关（internal/：handler/service/model/repository/middleware/config/pkg）
├── python_agent/             # Python LangGraph Agent（main/db_ops/agent_factory/routers）
├── admin-app/                # 管理后台前端（Vue3 + Vite，nginx 镜像构建时容器内自动编译）
├── web/                      # 前端构建产物 + 静态资源
│   ├── admin/                #   管理后台（平台/租户双端）
│   ├── chat/                 #   用户端 pc.html / h5.html
│   └── sdk/                  #   cs-chat.js SDK + 多端接入示例
├── deploy/
│   ├── nginx.conf            #   Nginx 配置
│   ├── init/                 #   初始化 SQL（mysql/、postgres/）
│   └── deploy.sh / push-to-acr.sh  # 基础部署 / 镜像上传脚本
│   # （production/、backup/、observability/ 属商业版内容，不随开源仓库发布，见 COMMERCIAL.md）
├── docs/                     # 文档中心（见下方导航）
│   ├── cs_images/            #   文档用操作截图
│   ├── README.md / 快速体验.md / 产品使用说明文档.md …
├── scripts/                  # 验证/构建/运维脚本
└── thirdDemo/                # 第三方接入最小示例（Go + SDK）
```

## 📚 文档导航

| 文档 | 内容 | 适用 |
|------|------|------|
| [📖 图文操作手册](./docs/README.md) | **按模块与菜单逐一配图（平台侧 + 租户侧 20+ 张）** | 所有人 |
| [⚡ 快速体验](./docs/快速体验.md) | 5 分钟跑通部署→配置→对话→转人工 | 所有人 |
| [产品设计文档](./docs/产品设计文档.md) | 产品定位、系统架构、多租户设计、安全设计 | 产品/架构师 |
| [产品使用说明文档](./docs/产品使用说明文档.md) | 全角色文字版使用指南、第三方接入、运维 | 用户/运维 |
| [模块功能说明文档](./docs/模块功能说明文档.md) | 模块详细功能、数据模型、流程设计 | 开发/测试 |
| [架构与开发指南](./docs/05-架构与开发指南.md) | 目录结构、模块职责、开发规范 | 开发者 |
| [运维监控指南](./docs/运维监控指南.md) | Grafana/Prometheus/Loki/Jaeger 使用、故障排查 | 运维/SRE |
| [客户接入手册](./docs/接入手册.md) | 产品化接入步骤、Token 换取、多端接入 | 客户/第三方 |
| [CI/CD 流水线说明](./docs/CICD流水线使用说明.md) | 流水线架构、部署脚本联动 | 运维/DevOps |
| [ACR 镜像上传说明](./docs/阿里云ACR镜像上传说明.md) | 镜像打包上传、版本管理 | DevOps |

## ❓ 常见问题

**Q：接口返回 429？**
防爆破锁定（同一 IP+key 连续失败 5 次锁 10 分钟）。`docker restart cs_go` 可清锁。

**Q：AI 没反应？**
检查「平台模型配置」：对话模型/向量模型是否配置、API Key 是否正确、测试连通性是否通过。

**Q：修改了代码如何生效？**
后端 Go：`docker compose up -d --build go`；前端（admin-app / chat / sdk）：`docker compose up -d --build nginx`（nginx.Dockerfile 容器内自动编译管理后台，无需宿主机安装 Node）；nginx.conf：改后 `docker exec cs_nginx nginx -s reload`。

**Q：知识库检索不到？**
文档状态需为「已索引」；更换 Embedding 模型后必须到知识库页重新索引。

**Q：脚本在 WSL 执行报错？**
仓库脚本为 LF 行尾；若从 Windows 检出为 CRLF，先 `sed -i 's/\r$//' scripts/*.sh`。

---

<div align="center">

**© 2026 智能客服系统** · [AGPL-3.0](./LICENSE) · [商用授权](./COMMERCIAL.md) · 技术支持：2771452556@qq.com

</div>
