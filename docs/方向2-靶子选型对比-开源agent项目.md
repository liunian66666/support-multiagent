# 方向二靶子选型对比：是否有比 smolagents 更优的开源项目

> 2026-10-24。所有条目均于今日联网核实（仓库归属、许可证、star、最近提交、维护状态），无臆造。
> 结论先行：**主靶子建议由 smolagents 换成 `SWE-agent/mini-swe-agent`**，执行层用 `SWE-agent/SWE-ReX`；
> smolagents 降级为轻量对照/备份靶；`bytedance/SandboxFusion` 作为 M4 判分侧可选增强。

## 1. 评价维度（针对"Agent 开发实习简历"加权）

简历含金量不等于 star 数。真正决定面试价值的是四项：

| 维度 | 为什么重要 |
| --- | --- |
| A 可读性 | 能否整读并在面试里讲出每行设计取舍（读不完 70k★ 的代码库） |
| B 改造位点密度 | 是否存在"执行边界/策略/判分"这类可插入自己代码的清晰 seam |
| C 判分可信度 | 是否自带客观判分（测试通过/官方 harness），决定指标能否被第三方复现 |
| D 简历识别度 | 面试官是否知道这个项目，能不能一句话建立共识 |

## 2. 候选靶子横向结论

| 项目 | 归属/许可 | 状态 | A 可读 | B 改造位点 | C 判分 | D 识别度 | 定位 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **mini-swe-agent** | Princeton+Stanford（SWE-bench 原班）· MIT | 活跃（~8.7k★，push 2026-10-19，v2.4.6） | ★★★（agent 类约 190 行，仅 bash 一个"工具"） | ★★★（Environment/Model 可子类化；**上游明确拒绝把执行策略做进核心**） | ★★★（官方 SWE-bench harness + mini 子集 + 轨迹 inspector） | ★★★（SWE-bench 就是他们提的，业界用户含 Meta/NVIDIA/IBM） | **推荐主靶** |
| SWE-ReX | 同组织 · MIT | 活跃（621★，push 2026-10-19） | ★★ | ★★★（runtime 抽象：local/docker/apptainer/modal/fargate，任意命令 + 退出码/输出抽取 + 大规模并行） | ★ | ★★ | 作为**执行后端**被主靶调用 |
| smolagents（原方案） | Hugging Face · Apache-2.0 | 活跃（~27k★） | ★★★ | ★★（DockerE2BExecutor 可换，但执行"代码片段"非真 shell） | ★★（GAIA 需联网下载、判分较重） | ★★（知名度高但被认知为"教程/demo 框架"） | 降级为对照/备份靶 |
| SandboxFusion | 字节 Seed · Apache-2.0 | 放缓（~1.1k★，最后提交 2026-07） | ★★ | ★★（HTTP 服务 + Python SDK，支持 `isolation: none/lite`（overlayfs+cgroups+namespaces）） | ★★★（内置 HumanEval/MBPP/CodeContests/CRUXEval 等 OJ+RL 数据集） | ★★（中文厂面试识别度好） | M4 判分侧可选增强 |
| dify-sandbox | langgenius · Apache-2.0 | 活跃（1.3k★） | ★（**Go** + seccomp/libseccomp，**仅 Linux**） | ★★（seccomp/资源限制实现确实漂亮） | ★ | ★★ | **栈不匹配，排除**（Windows+Python 环境无法高效动手） |
| Qwen-Agent | Alibaba · Apache-2.0 | 放缓（17k★，约 7 个月无大更新） | ★★ | ★（README 明确 **python executor 未沙箱、仅本地测试**） | ★ | ★★ | 排除：沙箱本身就是它的已知缺陷，无现成基线 |
| OpenHands | All Hands AI · MIT 核心 | 活跃（~70k★，1.0 GA） | ✗（代码库体量过大） | — | ★★★ | ★★★ | 只借鉴其 LOW/MED/HIGH 安全评级与资源限额思想，不做靶子 |
| microsandbox | superradcompany（YC）· Apache-2.0 | 活跃（~8.8k★，v0.7.8，**beta**） | ★（Rust workspace；有 Python SDK/MCP） | ★★（host 侧 secret 绑定单一日、no-net/allowlist 三档、fork/snapshot） | ★ | ★ | 认知参照物：面试讲"容器共享内核 vs microVM 硬件隔离"的分界线；**不作靶子**（需 WHP/嵌套虚拟化，Rust 栈） |
| anthropic sandbox-runtime (srt) | Anthropic 实验组 · Apache-2.0 | 活跃（~5.6k★，**研究预览**） | ★★（TS；CLI `srt` 包任意命令） | ★★（OS 级原语：macOS sandbox-exec / Linux bubblewrap；文件系统 deny-then-allow + 代理式域名过滤） | ★ | ★★★（Anthropic 出品，"无容器的 OS 级沙箱"话题性极强） | 思想来源，不作靶子（TS 栈、API 会变） |

## 3. 为什么 mini-swe-agent 比 smolagents 更适合做简历项目

1. **执行模型更真实**：mini 只给模型一个 bash，用 `subprocess.run` 逐步执行、无状态 shell 会话。
   这意味着"隔离"这件事完全落在你写的执行环境上 —— 换掉执行后端就等于换掉整个沙箱，
   改造位点天然就在你想做安全的那一层。smolagents 执行的是"一段 Python 代码"，安全面更窄、也更像 demo。
2. **判分是官方的、可被第三方复现的**：SWE-bench 官方 harness + `swe-bench-verified-mini`（50 例、约 5GB，
   保留原分布）+ 轨迹浏览器。你的 before/after 能落在被广泛承认的尺子上，而不是自造 30 例任务集。
   同时它自带 `cost_limit`（默认 3 美元）与逐实例成本记录 —— 成本维度指标是现成的。
3. **识别度无可争议**：SWE-bench / SWE-agent 就是这个团队提出的，bash-only 榜单是当前"裸模型能力"的事实标准口径。
   实习面试里"我拿 mini-swe-agent 在 SWE-bench-mini 上做了执行层加固与判分防伪"一句话即可建立共识。
4. **上游存在已被确认真实的空缺**（关键加分证据，见第 4 节）。

保留风险（必须诚实写进简历边界）：官方 SWE-bench 镜像体积大，本地 Windows/WSL2 拉取吃力；
主线用自造任务集 + 官方 mini 子集抽样跑通即可，不承诺跑完整 500 实例。

## 4. 关键情报：上游 RFC #953（这是本选题的"题目来源"）

`SWE-agent/mini-swe-agent` issue **#953（open，2026-08-29 开）**："RFC: deterministic execution boundaries for tool calls (task text is untrusted input)"。核实要点：

- **提出的问题**：mini 把任意 issue 文本直接渲染进初始 prompt，而 local/容器环境把模型生成的命令原样交给后端执行，
  **中间没有任何确定性的执行前策略边界**。在真实仓库场景下，被污染的 issue 正文就是对一个持有 shell、
  GitHub token 与模型 API key 的 Agent 的直接提示注入（外传 `~/.ssh`、把密钥写进 patch、push 到攻击者远端）。
- **提议方案**：默认关闭的 pre-tool-call 策略钩子 —— 密钥拒绝、egress 允许清单（fail-closed）、git remote 允许清单、
  单次运行费用上限、每次允许/拦截的**可审计收据（receipts）**；先跑 shadow 模式。
- **社区结论**：一位评论者给出了具体接缝设计（对 `Environment.execute` 做 opt-in `PolicyEnvironment` 包装，
  影子模式默认、收据先作为结构化诊断而非签名声明）；另一位回复"太复杂了，需要的人自己加"；
  发起者最终收敛为"**作为独立 extra / 独立包，零核心改动**"。**维护者本人未表态**。

对求职的直接含义（这是稀缺信号，务必用起来）：

1. 靶子的空缺是**上游公开承认、且明确不愿纳入核心**的能力 —— 你的项目因此"有题目、有边界、不越权"。
2. 结论是应当**做独立包**而不是魔改 mini 主干：`mini-guard`（PolicyEnvironment 包装器）+ 红队用例 + 拦截收据，
   对上游尊重"极简契约"，对简历是"给生态补了缺失的一层"，甚至可以顺手把 PR/RFC 讨论链接进简历。
3. shadow → 硬拦截的两态设计、以及"拦截收据即审计轨迹"，正好与方向一的 ReMe 经验召回和 Agent Lightning 式
   优化循环共用同一份轨迹数据，方向一/二闭环不变。

## 5. 修订后的方向二方案（对已定里程碑的最小改动）

| 原（smolagents） | 修订（mini-swe-agent） | 说明 |
| --- | --- | --- |
| M0 跑通 CodeAgent + `executor_type="docker"` | 独立 venv + `pip install mini-swe-agent`，YAML 配置跑通一条 bash 任务（先 local，再 docker/podman 后端） | 依赖更轻，无 litellm 之外的额外栈；模型仍走百炼 compatible-mode（注意自定义网关会导致内置成本计算报错，需 model-registry 映射） |
| M1 自造 30 例任务集 + 判分 CLI | **保留自造集（含对抗/无解样本）** + 追加 `swe-bench-verified-mini` 抽样（10–20 例）用官方 harness 打分 | 自造集做快速迭代，官方子集做外部可比性；两者都留逐例计数，不作百分比结论 |
| M2 Docker 加固 + 危险动作静态预检 | 同一位置改为**实现 `PolicyEnvironment` 包装 `Environment.execute`**（密钥拒绝 / egress 与 git remote 允许清单 / 费用与步数上限 / 拦截收据），Docker 参数加固照旧（非 root、cap-drop、`--network none`、内存/CPU/pid 上限） | 与 RFC #953 的接缝设计一致，评级 LOW/MED/HIGH 沿用 OpenHands 语义 |
| M3 红队 + reward hacking 防护 | 直接吸收 RFC 的攻击场景（读密钥、patch 内嵌密钥、恶意远端 push）+ 保留判分篡改/假报成功用例 | 攻击面来自上游自己的分析，不是自造稻草人 |
| M4 奖励接方向一 | 不变；轨迹与拦截收据同时喂 ReMe 与 Agent Lightning 形式循环 | 见《技术热点优化方案》§3 |

红线不变：新建 `AGENT_exp/sandbox-agent-lab/` 独立 venv 与自己的 `.env`；绝不挂载宿主 `docker.sock`；
每个 Docker 加固参数首次运行前列出报备；真实模型调用与批量评测前先确认额度。

## 6. 一句话结论（含备选口径）

- 想做**执行层安全 + 可复现评测**，投 Agent 开发/infra 岗：**mini-swe-agent（主靶） + SWE-ReX（执行后端）**，最优。
- 想两周内快速出东西、给非 SWE-bench 圈子看：**smolagents**，够用但仍偏 demo。
- 想做**判分服务/OJ 与 RL 环境**而不做 Agent 主干：**SandboxFusion**（注意其默认 `isolation: none`，安全叙事要靠自己的 Docker + lite 隔离）。
- 只在面试里**讲隔离层级**（进程 → seccomp → 容器 → microVM）时引用：**dify-sandbox / microsandbox / srt**，不必动手改。
