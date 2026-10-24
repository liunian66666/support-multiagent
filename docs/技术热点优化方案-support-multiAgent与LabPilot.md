# support-multiAgent 与 LabPilot 技术热点优化方案

> 生成日期：2026-10-24。**2026-10-24 修订**：方向一的落地对象由 `support-agent` 更正为 **`support-multiAgent`（完整项目，多租户 SaaS 智能客服，本仓库）**；`support-agent` 仅为其中的编排 demo（supervisor/specialist 参照实现，其模式已整合进本仓库 `python_agent/app/`）。文档对"方向一（RSI+记忆机制）"和"方向二（目标驱动式优化开源项目）"给出可对比的完整方案，并在末尾给出选型决策。所有引用的开源项目均已联网核实仓库、归属与维护状态。

## 0. 现状盘点（决策前提，2026-10-24 按完整项目重新核实）

| 维度 | **support-multiAgent（完整项目）** | LabPilot |
| --- | --- | --- |
| 定位 | 多租户智能客服 SaaS：FastAPI 业务层 + LangGraph Agent + MySQL/PostgreSQL/Qdrant + Redis，多端接入 + 人工接管闭环；fork 自 myrockey/ai_customer_service（AGPL-3.0），自有增量=Go→Python 迁移、business 模块化、多专家路由、决策轨迹、评测基线 | 课题组科研事务助理（纪要抽任务→待办→周报→审批外发） |
| 编排 | LangGraph 1.x，supervisor 分发 order/kb/case 专家，复合问题并行取证后 composer 汇总；demo 期 support-agent 的 5 类路由/confidence<0.45 短路等模式已并入 | LangGraph 1.x，工具闭环 + HITL interrupt + SQLite checkpointer |
| 记忆 | **会话级**：`SafeAsyncPostgresSaver`（Postgres checkpoint，含并发锁/断线重连加固）+ `workers/summary.py` 定时会话摘要（≤80字回写业务库，仅作业务记录，**不可被召回注入**）。无跨会话经验/用户画像召回 | checkpointer thread 级会话记忆，无跨会话经验/用户画像沉淀 |
| RAG | Qdrant 按租户隔离，先过滤再 top-3；已标定 0.74 候补阈值但未上线（样本不足） | 混合检索 + citations，Python 侧算相似度 |
| 评测 | **已有雏形**（原判断"无 gold set"需更正）：`python_agent/evaluation/` 含 gold_rules(11)/gold_retrieval(24)/gold_answers + collect_*/score.py CLI，Recall@3、MRR、拒检率已实测；`EVALUATION_BASELINE.md` 已注册下一轮门槛（扩至 100 条检索题） | 规则评测 13/13、约 30 例固定集、按钮 vs 对话对照评测 |
| 可观测 | agent_trace_runs 结构化轨迹（路由/工具/证据/用量白名单入库）+ Prometheus/Grafana/Loki + trace_id/tenant_id | request_id/审计、预算控制、并发准入 |
| 短板 | **记忆是"一次性"的**：摘要落库但不回流召回，同类问题下次仍重走全流程；路由/护栏靠人工调 prompt；无检索阈值线上标定、拒绝误命中 0/2 | RAG 检索质量无大规模 gold set；外发/恢复链路重；记忆仅会话级 |

两项目共同缺口不变：**记忆只停在会话级、改进靠人工肉眼调 prompt**。但完整项目的起点比原方案预估更高：**评测 CLI 与黄金集雏形已存在**（collect→score 管线可跑），方向一 P1"建 benchmark"从"从零建"降为"按 EVALUATION_BASELINE.md 已注册门槛扩容"。

---

## 方向一：现有项目上的 RSI（递归自我改进）+ 记忆机制优化

### 1.1 目标定义

- **记忆层目标**：把 support-multiAgent 的"checkpoint+摘要落库但不回流"与 LabPilot 的会话记忆，升级为**跨会话、可检索、可自我精炼**的双轨记忆——个人/情景记忆（用户偏好、历史订单语境，按 租户+客户 双键隔离）+ 任务/程序性记忆（成功策略、失败教训），命中时直接复用，减少重复 LLM 调用。落点：会话摘要 worker 旁挂 ReMe 经验抽取替代"只落库"；`app/api/chat.py` 请求前增加 `retrieve_*` 注入。
- **RSI 层目标**：把"人工看失败案例→改 prompt/工具→回归"的循环，升级为**基准驱动的自动假设-测试-评估循环**，在准确率、成本、延迟、可靠性四维上受控迭代，只在目标指标提升且不劣化其它指标时接受改动。

### 1.2 约束条件

1. 兜底开源项目必须是**大厂在 GitHub 开源、当前活跃维护**的（不接受已归档项目当生产依赖）。
2. 复用各项目现有 venv / uv 栈，不重装依赖、不动 Milvus、不改 `.env`（红线：任何环境/文件改动先报备）。
3. RSI 不得以微调权重为前提（无 GPU 训练条件），必须是**harness 层/上下文层的自我改进**（prompt、工具配置、检索策略、经验注入），而非 RL 训练闭环。
4. 后端由用户自行启动，AI 不代为运行。

### 1.3 选型：开源项目 + 集成方式 + 可行性

| 层次 | 选用项目（大厂/归属） | 许可 | 状态 | 集成方式 | 可行性 |
| --- | --- | --- | --- | --- | --- |
| **记忆机制** | **ReMe**（阿里通义实验室 / agentscope-ai，前身 MemoryScope） | Apache-2.0 | 活跃，2026-10 仍在更新 | 以 **HTTP/MCP sidecar** 或 **Python 库**接入：会话结束时异步 `summary_task_memory`（轨迹→经验）、`summary_personal_memory`（用户画像）；新请求前 `retrieve_*` 召回注入上下文。support-multiAgent 在 `workers/summary.py` 与 `app/api/chat.py` 两处挂钩（LangGraph 图结构零改动，checkpointer 保留），LabPilot 在 `app/agent/context` 旁挂经验召回 | **高**。同为 Python，纯外部服务/库调用，不侵入 LangGraph 图结构；有 Appworld/BFCL-V3 验证；记忆以 Markdown 文件为真源，可读可备份 |
| **RSI 主参考** | **Microsoft Agent Lightning v1.0**（微软亚研） | MIT | 活跃，2026-08 发 v1.0 | **不跑 RL 训练**，采用其 v1.0 "**Skill/优化循环**"形态：给定「可编辑的 agent 定义 + 基准」，自动提议改动（system prompt、工具配置、工作流、模型选择、推理设置）→ 在基准上评测 → 四维（准确率/成本/延迟/可靠性）门槛决定是否接受。**统一数据接口**把 LangGraph 轨迹转成 transition 序列，零侵入接入 | **中高**。优化循环无需 GPU/训练集群，纯 harness 层；但**只有给定合理基准才有效**，基准质量是瓶颈，需先建评测集 |
| **RSI 备选** | **Microsoft Agent Framework 1.0**（微软，Semantic Kernel+AutoGen 统一继任，两者已进维护模式） | MIT | GA 2026-04，1.0 稳定 API+LTS | 作为**中长期编排底座**评估；其 context provider 可插拔 Mem0/Redis/Neo4j 记忆、graph 工作流带 checkpoint/HITL、内置 MCP/A2A | **中**（作为整体迁移目标偏重；本项目仅借鉴其记忆 provider 抽象与 middleware 思路，不建议为热点整体迁移） |
| **RSI 概念参考** | facebookresearch/**SWE-RL**（Meta，NeurIPS'25） | 见仓库 | 官方开源 | **仅借鉴范式**：其 reward（与 oracle 相似度）+ SSR 自我博弈出题思路，可迁移成"自动造评测用例→自动打分"的**离线回归生成器**，喂给 Agent Lightning 当基准 | 思路参考，非直接依赖（RL 训练需 GPU，不采用） |

> 兜底结论：**记忆用 ReMe（阿里），RSI 用 Microsoft Agent Lightning（微软）**。二者均为大厂、活跃、宽松许可，且都能"零/低侵入"贴合现有 LangGraph 项目。

### 1.4 优先级排序（方向一）

- **P0 记忆机制落地**：ReMe 接 support-multiAgent（会话摘要"只落库"→情景+任务双轨可召回），跑通"二次同类请求命中经验、省一次 LLM 调用"。这是收益最大、风险最低的一步，且是 RSI 的数据地基。
- **P1 基准扩容 + 接 Agent Lightning 优化循环**：评测 CLI 雏形已在（`python_agent/evaluation/` collect→score），按 `EVALUATION_BASELINE.md` 已注册门槛扩容（≥100 检索题、无答案/反条件各 ≥20），再用 Agent Lightning 对路由/检索策略做受控迭代。
- **P2 记忆驱动路由/护栏自精化 + 跨重启经验复用度量**。

### 1.5 预期产出

- support-multiAgent：`workers/summary.py` 旁挂 ReMe 经验抽取（替代"摘要只回写业务库"）；`app/api/chat.py` 请求前 `retrieve→注入`、会话末异步 `summary`；记忆按 `tenant_id + customer` 双键隔离（复用既有"租户+客户"归属边界）；trace 表增加经验命中与省 token 字段。
- LabPilot：经验召回挂到 chat/graph，周报/任务生成时复用历史成功结构。
- 一份"改动前 vs 改动后"四维对照表（准确率/成本/延迟/可靠性），复用 LabPilot 已有的对照评测口径（16.4s vs 0.28s 那类按钮/对话对照）。

### 1.6 风险与依赖

- **RSI 的 benchmark 陷阱**：Agent Lightning 明确"只给你给的基准那么好"，基准偏则优化出的 agent 生产上翻车——强依赖 P1 gold set 质量。
- **记忆写污**：自动 summary 可能把错误结论固化成经验，需 ReMe 的 Draft-Retrieve-Deduplicate + 置信度 + 人工抽审。
- **合规**：记忆/日志不得落凭证与敏感正文（LabPilot P0 已强调对象级授权，跨会话记忆要按 user 隔离）。
- **依赖**：ReMe 需 embedding 与一个向量后端（可复用其 local 后端，避免新增 Milvus）；Agent Lightning v1.0 Skill 形态需一个可跑的评测 CLI。

---

## 方向二：对现成开源项目做目标驱动式优化

### 2.1 目标定义

> 修订（2026-10-24）：按用户意见，方向二靶子改为**沙箱类项目**——这正贴住 2025-2026 热点"Code-as-Action / CodeAct + 安全执行环境"，且沙箱是 Agent 执行任意代码的前提，能产出**可验证奖励信号**（测试是否通过 / 命令是否成功），恰好可作为方向一自我改进循环的裁判。

候选靶子（均为大厂/头部组织开源，已联网核实）：

1. **smolagents（Hugging Face，Apache-2.0，~27k★，活跃）** — 推荐主靶。核心 <1000 行、可读可改；CodeAgent 生成 Python 在沙箱执行，一等公民支持 **E2B / Modal / Docker / WASM(Pyodide+Deno)** 多后端执行器；GAIA 基准成绩公开（发布期 44.2% 居首，后优化至 55–67%）。短板清晰：官方明示"无跨会话持久记忆、无内置编排/观测"，优化目标可量化。
2. **OpenHands（All Hands AI，MIT 核心，~70k★，1.0 GA 2026-09）** — 备选靶。生产级 Docker 沙箱 + 内置 LLM 安全分析器（动作评级 LOW/MED/HIGH）+ 非 root 执行（SANDBOX_USER_ID）、BYOM。SWE-bench Verified 68%(Qwen3-Coder)/72%(Claude Sonnet 4.5)。工程量大、体量大，适合做"沙箱隔离强度 vs 成本/延迟"对照。

**推荐主靶 = smolagents**（轻量、可整读改造、沙箱后端可插拔、本地 Docker 即可跑，不需云端 E2B 账单）。

### 2.2 约束条件

- 复用本地 **Docker**（用户已用 Docker Desktop/WSL2）做沙箱后端，**不强制上 E2B/Modal 云沙箱**（避免账单与外传代码红线）；如需对比，云沙箱仅做只读参照、单独授权。
- 不改动沙箱内执行的宿主安全边界前，先按红线报备（Docker socket 挂载高危，默认只挂隔离卷、网络受限）。
- 本地小规模、单实例；优化评测优先离线/固定用例，真实模型调用单独授权、标注成本。
- 不承诺无依据指标（沿用 LabPilot"真实测量才写数字、不作百分比结论"口径）。

### 2.3 优化目标 / 评估指标 / 改造范围

| 项目 | 内容 |
| --- | --- |
| **优化目标** | 在 smolagents CodeAgent + Docker 沙箱上，**提升任务解决率(成功率) 同时压低"危险动作误放行"与每任务成本/步数/延迟**：即在安全受控前提下做"更少 LLM 调用、更高一次通过率(code-as-action 优势)"的可量化改进 |
| **评估指标** | ①任务成功率(pass@1，GAIA 子集或自建数据任务集)；②安全：HIGH 风险动作拦截率、越权/逃逸用例 0 通过、资源超限被杀率；③效率：每任务 LLM 调用数、步数、p50/p95 端到端延迟、token/成本；④沙箱：冷启动、隔离正确性(宿主文件不可达) |
| **改造范围** | ①固定任务集 + 自动判分(执行退出码/断言/单测通过=奖励信号)评测 CLI；②执行器侧：Docker 后端加固(非 root、CPU/内存上限、网络白名单、只读 FS)+ 危险 import/命令静态预检再入沙箱(向 OpenHands 的 LOW/MED/HIGH 评级看齐)；③循环侧：max_steps、失败重试与"沙箱报错回喂纠错"策略；④（与方向一打通）沙箱判分作为 ReMe 经验总结/Agent Lightning 优化循环的**客观奖励来源** |
| **可复用开源方案** | smolagents 自带 E2B/Modal/Docker/WASM 执行器与 `additional_authorized_imports` 白名单；OpenHands 的安全分析器评级与 Docker 资源限额策略可借鉴移植到 smolagents 预检；Agent Lightning 统一数据接口消费沙箱轨迹→transition，做 harness 层优化 |

### 2.4 优先级排序（方向二）

- **P0**：固定任务集 + 沙箱自动判分评测 CLI（执行退出码/断言/单测=奖励；无此则"优化"无意义，且它是方向一的裁判）。
- **P1**：Docker 执行器加固（非 root、资源上限、网络白名单、只读 FS）+ 危险 import/命令静态预检评级；跑出"安全 × 成本 × 成功率"对照。
- **P2**：执行后端消融（本地 Docker vs WASM；隔离强度/冷启动/成功率权衡）；把沙箱奖励接入方向一的 ReMe 经验总结与 Agent Lightning 优化循环。

### 2.5 预期产出

- 一份可复现的沙箱 Agent 评测报告（成功率/安全拦截/效率三维 before/after，沿用"真实测量才写数字、不作百分比结论"口径）+ 改造 diff 清单 + 一组"沙箱判分→奖励信号→harness 优化"的打通样例。

### 2.6 风险与依赖

- **执行任意代码的宿主风险**：LLM 生成代码在沙箱跑，逃逸即 RCE。**绝不挂宿主 docker.sock 给不可信容器**，只挂隔离卷、网络受限、非 root、CPU/内存上限先报备再改。
- **判分即奖励的作弊(reward hacking)**：以"测试通过"当奖励，模型可能篡改测试/注释断言过判分（SWE-RL/Agent Lightning 都专门写防作弊）。判分脚本与可写区隔离、关键断言只读。
- **指标可信度**：GAIA/SWE-bench 子集需固定、防污染，且 harness 可致分数波动 15–20 个点，须同 harness 比较。
- **算力/成本**：本地 Docker 沙箱可控；真实模型调用产生费用，离线用例优先、真实调用单独授权。

---

## 3. 选型决策（已按用户意见更新为沙箱靶子）

用户明确方向二走**沙箱类项目**，靶子定为 **smolagents（Hugging Face）**，备选 OpenHands。据此重排优先级：

**主线 = 方向二（smolagents 沙箱优化），并与方向一闭环打通。** 理由：

1. **热点更硬**：Code-as-Action / CodeAct + 安全沙箱是 2025-2026 code-executor agent 的主干，smolagents(~27k★)/OpenHands(~70k★) 均为头部活跃项目，说服力优于已归档的 RAG 靶。
2. **奖励可验证**：沙箱执行天然产出客观奖励（测试/退出码），正好补上方向一 RSI 最缺的"可信裁判"，让 Agent Lightning 优化循环不再悬空。
3. **兜底齐全**：ReMe（阿里，记忆）+ Agent Lightning（微软，harness 优化）+ OpenHands（安全评级/资源限额借鉴）三者都宽松许可、活跃、可本地 Docker 复现。
4. **可控红线**：本地 Docker 沙箱、隔离卷、不挂 docker.sock、不改宿主边界前先报备；不强制云沙箱。

> 落地顺序：**mini-swe-agent 跑通 + 沙箱自动判分 CLI(方向二 P0) → PolicyEnvironment 执行前策略边界 + Docker 加固(方向二 P1) → 沙箱奖励接入 ReMe 经验总结 + Agent Lightning 优化循环(方向一 P1) → 三维对照报告(成功率/安全/效率)。**
> 注：2026-10-24 方向二主靶由 smolagents 升级为 mini-swe-agent（详见《方向2-靶子选型对比-开源agent项目.md》），smolagents 保留为轻量对照靶。
> 方向一在 support-multiAgent/LabPilot 上的 ReMe 记忆落地可并行作为 P0 支线（低风险、独立交付）。

## 4. 统一风险清单

| 风险 | 缓解 |
| --- | --- |
| 执行任意代码的宿主逃逸 | 绝不挂 docker.sock 给不可信容器；隔离卷+网络受限+非 root+CPU/内存上限，改动前报备 |
| 判分即奖励的 reward hacking | 判分脚本/关键断言只读、与可写区隔离；防作弊参照 SWE-RL / Agent Lightning |
| benchmark 偏导致 RSI 跑偏 | 固定任务集、防污染、同 harness 比较(harness 可致 15–20 点波动)；三维门槛共同约束 |
| 经验/记忆污染 | ReMe Draft-Retrieve-Deduplicate + 置信度 + 抽审；错误结论不自动固化 |
| 隐私/越权 | 记忆按 user 隔离，复用 LabPilot 对象级授权；日志不落凭证 |
| 依赖与算力 | ReMe 用 local 后端避免新增 Milvus；真实模型/embedding 单独授权、标注成本 |
| 维护漂移 | 靶/兜底项目均可能变（Kernel Memory 归档为前车之鉴），锁定版本、记录 commit |

## 5. 被排除的候选方向与排除依据（2026-10-24 补）

排除标准只有四条硬尺子：**F1 个人算力/环境是否跑得动** · **F2 是否与现有两个 LangGraph 项目互补（而非挤在同一问题域）** ·
**F3 harness 层能否出可复现交付（不依赖训练/集群）** · **F4 靶子活跃度与改造位点密度**。

| 候选方向 | 代表开源项目 | 淘汰理由（对应尺子） | 什么条件下翻案 |
| --- | --- | --- | --- |
| RL / 强化微调式自我改进（真跑训练） | veRL、OpenRLHF、TRL、Unsloth | **F1 致命**：7B LoRA 的 RFT 实测约需 49GB 显存（权重+优化器+激活+参考模型+rollout buffer），verl 官方底线 24GB，多轮 agent rollout 更吃紧；本机是 Windows+Docker Desktop，无 H100 级资源（远程 dm24 是科研服务器，不宜挪作训练） | 拿到多卡 A100/H100 或有云训练额度 |
| GUI / Computer-Use Agent | UI-TARS-desktop（字节，~30k★）、OSWorld、WebArena、BrowserGym | **F1+F4**：7B 视觉模型推理需 ≥16GB 显存；OSWorld 成功仅 ~22-24%（15/50 步），失败归因极难，个人周期内难出可信结论；岗位偏多模态算法（要求检测/OCR/视觉定位），与应用 Agent 岗不是同一条线 | 目标岗明确是 GUI/多模态算法岗 |
| Deep Research / 多步搜索 Agent | 各家 deep-research 实现 | **F2+F3**：与已有 deep-search-pro / WorkBuddy 侧经历重叠，简历三个项目都在"检索+编排"同域；结果质量评测主观、易撞墙、无客观裁判 | 能拿到独有评测集或真实业务指标 |
| 企业级 RAG 新做一套 | RAGFlow / Dify / LightRAG 等 | **F2**：2026 基础 RAG 已是默认能力、简历同质化最重灾区；招聘端改要"权限+可观测+生产经验"。→ 不新做，只作为已有两项目的**深化层**（分层评测、检索层权限、成本可观测） | 拿到独有的脏文档数据做版面级/多模态检索；或目标是搜索/relevance 岗 |
| 本地推理 / 推理优化 | vLLM、SGLang、TensorRT-LLM | **F1**：无本地可训练/可服务大模型的 GPU；结果高度依赖硬件，个人环境测不出有意义的对比 | 有稳定 GPU 服务器使用权 |
| 多 Agent 编排框架本身/新造框架 | CrewAI、AutoGen、Agent Framework 等 | **F2+F4**：再写一个编排框架是红海且无差异化，面试里"我用了 LangGraph"已不构成信息量 | 不做（无翻案条件） |
| MCP 生态 / 协议侧工具 | 各类 MCP server | 可作副食品不可当主菜：单个 MCP server 工程量与说服力都低 | 仅作为已有项目的接入点，不独立成项 |
| 评测/可观测平台（当方向本身） | Langfuse、RAGAS、DeepEval | 定位是**共用基建**而非独立方向：已在方向一 P1（benchmark）与方向二 M1（判分 CLI）里落地，一份投入三份产出 | 已吸收，无需单列 |

**为什么最后落在"沙箱/执行层 + 记忆与 harness 优化"**：它是唯一同时满足 F1-F4 的组合
——纯 harness 层改动（本地 Docker、不需要 GPU）、与已有两项目互补（补执行层安全与奖励可信度）、
判分客观可复现（测试通过/退出码 + 官方 SWE-bench 抽样）、靶子活跃且上游明确留出了改造位点（issue #953）。

## 6. 对外部候选提案的评估（2026-10-24 二次补充）

收到一份外部提案，建议把方向二排为：①Vanna（Text-to-SQL 正确性与成本）②mini-swe-agent ③IBM ContextForge。逐项以 GitHub API 实证复核：

| 提案选题 | 实证状态（2026-10-24，GitHub API） | 按 F1-F4 判定 |
| --- | --- | --- |
| **Vanna 2.0** | `vanna-ai/vanna` **archived=True**，仓库 2026-03-29 转只读，最后 push 2026-02-02（v2.0.2）；另有 CVE-2026-65702（High，路径穿越）未修 | **F4 一票否决**。与 Kernel Memory 同坑：靶子归档=无 triage、无安全补丁；"独立贡献/上游补丁"路线不成立，只剩 fork 自建（叙事变弱）。Text-to-SQL 题目本身有价值，但应换活跃靶（如 WrenAI、DB-GPT）或作为评测 CLI 的一个任务域吸收 |
| **mini-swe-agent** | `archived=False`，8.8k★，push 2026-10-19，MIT，issue #953 仍 open | **维持主靶不变**。提案的 P1（上下文选择）/P2（错误分类与停止恢复）与我们 M2/M4 正交，可作 M4 优化循环的候选改动面纳入 |
| **IBM ContextForge** | `archived=False`，4.7k★，push 2026-10-23，Apache-2.0；但 **994 open issues**、README 无官方 IBM 支持声明、上游已有 35+ 安全插件与完整网关功能 | **不作主靶**。F2 与已有 MCP 矩阵重叠、F4 表面活跃但 issue 积压说明改动位点不易被接受；提案自己承认"没场景就只剩网关功能展示"。列观察名单，若转投平台/后端岗可升级 |
| 提案称"你的同级目录已有代码审查项目可深化" | 实证：`My_code_review_agent` 与 `code-review-agent` 均为 **iKatiyar/code-review-agent 的克隆**（LICENSE 署名 Ishita Katiyar、origin 指向他人仓库），非用户自有成果；`spring-ai-crossborder-customer-service`（RyanCoreAI）是 Java/Spring 参考仓库 | **不能"深化"**——不是我们的代码，写进简历即造假风险。只可作阅读参考 |

**决定**：方向二主靶维持 mini-swe-agent；Vanna 选题否决（归档）；ContextForge 列观察；提案中真正可吸收的是两点——
①把"上下文选择/失败恢复"作为 M4 优化循环的候选改动面（与已有的 prompt/步数/模型档位并列）；
②"隐藏测试由独立评分环境持有、公开榜分须同条件复测"与已定 M1/M3 判分防伪口径一致，无需改动。
