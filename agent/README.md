# 四 Agent 客服主图

`agent/` 是当前唯一的普通聊天图实现。`python_agent/app/services/four_agent.py` 将 JSON 和 SSE 请求接入 `agent.cutover`，四 Agent 的共享状态、Reviewer、记忆和持久化均在此目录。历史消息在新图首次处理会话时只读导入；未完成审批继续使用原检查点兼容路径。

本文件后面的评测段落记录开发过程中的旧版对照，相关旧开关已退役。

## 写法依据（不自拟 API）

| 选择 | 官方来源 | 本机版本 |
| --- | --- | --- |
| 子 Agent 即工具（Supervisor + `consult_faq/order/general`） | docs.langchain.com `multi-agent/subagents`；`langgraph-supervisor` 官方 README 已声明不再维护并推荐 tool 方式 | langchain 1.3.18 / langgraph 1.2.11 |
| 带自定义节点的图（review/finalize/compact） | 官方称 **Custom workflow**：`StateGraph` 节点内 `create_agent(...).invoke(...)` | 同上 |
| 中间件必须以装饰器产物传入 | `factory.py` 按 `m.__class__.before_model` 比较选钩子，裸函数抛 `AttributeError` | `@before_model(can_jump_to=["end"])` |
| 循环有界 | `ModelCallLimitMiddleware(run_limit=…, exit_behavior="end")`、`ToolCallLimitMiddleware(tool_name=…, run_limit=…)` | 同上 |
| 只有最外层图挂 checkpointer | 官方 persistence/interrupts 文档：子 Agent 不带自己的 saver | `AsyncPostgresSaver` 1.2.x 异步上下文管理器 + `.setup()` |

## 图与状态通道

```
START → open_turn → supervisor → review → ┬(should_continue)→ supervisor   # 有界重试
                                           └→ finalize → compact → END
```

`messages` 是客户可见转写，**append-only**；用户消息与最终答复的 id 固定为 `{turn_id}:u` / `{turn_id}:a`，`add_messages` 对同 id 覆盖而非追加——客户端重试不会多出一轮（本机已实测）。Agent 结果、证据、审计分别走 `delegations` / `ledger` / `usage` 通道，带按键 upsert 的 reducer，同一轮重放只改写自己的记录。`open_turn` 用 `langgraph.types.Overwrite` 清空本轮 scratch 通道：不清 `delegations` 的话，Reviewer 会拿上一轮的检索回执给这一轮的无据数值背书。

## 记忆（阶段4 重点）

模型上下文 = `摘要横幅 + messages[watermark:][-window:]`。摘要只作上下文，横幅文字里就写明"不作为订单状态或店铺政策的依据"，订单与政策每轮重新查询。

不用官方 `SummarizationMiddleware` 的原因是确定的：它返回 `RemoveMessage(id=REMOVE_ALL_MESSAGES)` 重建通道，且不暴露水位线通道——本图同一份 `messages` 既是模型上下文又是前端历史，删了就丢轮。水位线是本阶段唯一必须由自建通道承担的部分（官方 short-term-memory 文档亦允许 custom state）。

三条硬约束各有测试：摘要失败**不**推进水位线；被摘要吞掉的锚点（订单号、`数字+单位`、客户点名字段"发票抬头/收件人…"、约束从句）由 `anchors()` 检出后按**优先级装填**回补——顺序是 单号 → 点名字段 → 数值 → 一般从句，且按剩余字数预算打包，早期版本按条数截断会把单号和抬头正好挤掉；`compact` 永不删除消息（长对话跑完 `total == 轮数 × 2`）。

### 四臂对照（确定性，可在无模型环境下复现）

`evaluation/memory_context_ab.py` 复用 `python_agent/evaluation/memory_ab_live.py` 的 10 条长对话（3 根针 + 8 句填充 + 单次探针），走同一张真图、同一个**故意会把数值与单号改写没**的悲观摘要器，测"探针时刻模型实际被喂到的上下文里还剩几根针"与"事后还能不能恢复"：

| 臂 | 被喂到（30 根针） | 可恢复 |
| --- | --- | --- |
| `window_only` 纯最近 12 条（旧路径 `recent_messages(12)` 等价） | **0/30** | 30/30（但没人回读） |
| `delete_rebuild` 官方 `SummarizationMiddleware` 语义（删除重建） | **0/30** | **0/30** |
| `watermark_norepair` 水位线但关掉锚点回补 | 0/30 | 30/30 |
| `watermark` 生产配置（水位线 + 最近原文 + 锚点回补） | **30/30** | **30/30** |

逐题层面：相对纯滑窗**改善 10 题、回退 0 题、逐题波动 0**（同输入同代码，确定性）。这张表同时也是"为什么不用官方中间件"的量化理由：它不只喂不到，连可恢复性一起删掉。原始数据在 `evaluation/runs/memory_context_ab.jsonl`。

它不能证明的：客户看到的**措辞**是否真的把针说出来了 —— 那需要真实模型，属阶段6。

> 【勘误】本文件此前写"Business 侧『旧摘要覆盖新摘要』的写接口缺陷在旧路径依然存在"。**该说法已过期**：
> 旧路径现在也是水位增量 + 单调 CAS 落库（`business/agent/internal.py` 的
> `IF(VALUES(last_summarized_message_id) > last_summarized_message_id, …)` 与 `GREATEST`），
> worker 侧生成/保存任一失败都不推进水位（`app/workers/summary.py`），并有 Redis 单实例锁。
> 新链路与它的实际差异见报告 §6d，不在"是否覆盖"这一条上。

## Reviewer 规则节点（确定性，非第五个 Agent）

| 级别 | 规则 |
| --- | --- |
| hard（直接 fail-closed） | 无核验依据的"已退款/已支付/已建工单/已转人工"等；`数字+单位` 不在当轮证据或核验结果里；引用本轮未经业务接口核验的订单号；订单字段断言没有 `ok` 核验结果；与已核验订单状态冲突（如"未发货"对上"已发货"）；泄露内部片段编号 |
| soft（触发一次有界重试） | 政策题未委派 FAQ、订单题未委派 Order、FAQ 未调用检索工具、Order 未调用只读接口、有专职 Agent 执行失败 |

`已发货` 这类词允许复述，前提是它出现在本轮 `ok` 核验结果里；`支付/扣款/建工单/转人工` 永远不可背书。判定分两级是必要的：soft 误判只多花一次重试，hard 误判会吞掉一个正确答案——首轮测试就暴露过把政策语"签收后 7 天"误判为订单字段的过度拦截。

## 有界性与安全边界

三层上界：中间件限模型/工具调用次数 → `should_continue` 限 `attempts`（默认 3 个 supervisor pass）→ `recursion_limit` 限图步数。

订单归属始终由 Business 判定：`lookup_my_order(order_id)` / `list_my_orders()` 只接受单号或无参，身份取自服务端会话 ContextVar，模型无法指定客户。退款、支付、建单等写操作在本图里不存在对应工具。`thread_key()` 拒绝非 `{tenant_id}:` 前缀的线程；检查点键带 `support_v2:` 前缀，旧线程的检查点不会按新 schema 被读出；存在未完成审批的会话继续走旧图。

## 当前启动与兼容

普通聊天固定走四 Agent 主图。`FOUR_AGENT_GRAPH` 和 `X-Agent-Graph` 已不能切换旧图；未完成审批仍由原图处理，以保留已有中断状态。需要回看旧版行为时，使用 Git 历史和已保存的对照数据，不通过线上请求切换。

```bash
# 本机（Python 3.13 + 与容器同版本的依赖）
uv pip install --python .venv/Scripts/python.exe -r python_agent/requirements.txt
.venv/Scripts/python.exe -m pytest agent/tests -q          # 113 passed
PYTHONPATH=".;python_agent" .venv/Scripts/python.exe -m agent.demo --turns 8 \
    --dump agent/evaluation/runs/demo_offline.jsonl         # 复合咨询 + 长对话 + 重启续聊
bash agent/evaluation/run_all.sh demo-mounted               # 同一套演示走「开关那条路」（app 侧图）的留痕
    # ⚠ 上台前知道这件事：那条路 8 轮就会把最早一轮的锚点挤出模型上下文（3/3 → 1/3，摘要里没有它们、
    #   也没有回补；见报告 §5e 的演示记录表）。要么把"回问最早细节"放在压缩前，要么演上面这条 agent/ 的路。

# 或在容器里一条命令跑完（生产镜像不重建、app 不改）
bash agent/evaluation/run_all.sh test        # py3.13 + 生产 requirements 下同一套测试
bash agent/evaluation/run_all.sh pg          # 真实 AsyncPostgresSaver 检查点验证，跑完自清理
bash agent/evaluation/run_all.sh retrieval   # 只读检索稳定性探针
bash agent/evaluation/run_all.sh gateway     # 模型网关可达性诊断（零推理消耗）
bash agent/evaluation/run_all.sh live        # 阶段6 真实模型采集（需模型可用）
bash agent/evaluation/run_all.sh mounted-guards  # 30 例对抗语料打在挂载入口的 Reviewer 上：旧接法 9/25→22/25 漏放；
#                                              2026-10-28 起入口换成 agent/ 图，同一语料重测 0 漏放 / 0 误拦（报告 §5j）
bash agent/evaluation/run_all.sh image          # 镜像自检：agent/ 包在不在、三条回退路是否都回旧图

# 接真实服务（只读）：在已完成客户会话鉴权的 Agent 进程内
from agent.legacy_bindings import deps_for_app, legacy_history, session_context
deps = await deps_for_app(app, tenant_id, reuse_faq_agent=False)  # True=与旧路径同行为（含 kb_hits 写入）
service = SupportService(lambda _t: deps, checkpointer=app.state.checkpointer)
```

切换的落地方式（2026-10-28，本分支）：`app/services/four_agent.py` 现在只做委派 ——
`enabled()` 转 `agent/rollout.py`，`run_turn()` 转 `agent.cutover`；
`python_agent/Dockerfile` 用 `additional_contexts: agent_src` 把本目录
`COPY --from=agent_src . /app/agent`，所以**生产镜像里确实有这张图**，
不再需要"换 import + 重建镜像"那套补丁（`agent/deploy/` 两个补丁因此作废，仅留档）。
`chat.py` 只读 `state["answer"]` 与 `state.get("model_messages", [])`，契约由
`tests/test_cutover.py` 钉住；JSON 与 SSE 共用同一 `TurnResult` 的 `to_payload()` /
`sse_events()`，两条传输的内容差只可能是分帧，不可能是正文。

## 真实环境复核（2026-10-27）

验证通道用项目自带的 `scripts/docker/Dockerfile.test --target agent` + 整个仓库根**只读挂载**到 `/work`（构建上下文是 `python_agent/`，重建生产镜像并不会带上根 `agent/`，而且那会改动正在运行的服务）。生产镜像未被改动；本目录的工作全程没有写过 `python_agent/`——10-27 18:15 工作树里出现的 `app/multi_agent/graph.py`、`app/services/four_agent.py` 与 `chat.py` 的开关分支是 app 侧独立于本目录的改动，本节把它当成被测量对象来复跑与取证（`run_all.sh mounted`），不替它背书。

| 项 | 结果 |
| --- | --- |
| 真实 `AsyncPostgresSaver` | **PASS**（`evaluation/verify_checkpoint_postgres.py`）：跨两个独立编译的图实例恢复 10 条消息 / 6 轮 ledger；同 `turn_id` 重放不重复；水位线推进到 8；写入 120 行检查点后 `adelete_thread` 归零，线上会话数据未被触碰 |
| 检索稳定性（阶段5） | **13 批独立进程连跑 = 163 个题次 / 779 次检索**（`run_all.sh retrieval-batches 3` + `evaluation/summarize_retrieval_batches.py`；原始 `runs/retr*.jsonl`）。分三层看：**分数层**本来就不逐位确定——65/90 题次（72.2%，只有升级后的 6 批带这一列）五次调用的 score 元组逐位不同（旧探针舍到 4 位，正好抹平决定名次的 1e-4）；**候选层**top-3 组成翻动 9/163 题次（5.5%），全落在第 2/3 槽，分差 `6e-5~1.4e-3`，集中在 4 题；**后果层**金标掉出 top-3 与条款覆盖掉过都是 **0**，只有"候选里出现的数值集合"变过 6/90（一条带「7 天」的旁证进/出窗口，回答所需的数值永远在 slot1）。所以按"没有效果就不保留额外复杂度"**仍不改检索层**，但理由从"没测到波动"换成"波动不落在有后果的那一层"；三个候选方案（近平票二次排序 / 提高阈值或按文档去重 / 加大 k）各自为什么不做，报告 §7 逐条写了。**波动已分层定因**（`run_all.sh embedding-drift` → `runs/embedding_drift.jsonl`）：同一句话连发 8 次 embedding，**5/6 题拿到 2 种不同向量**；而**固定同一个向量**连查 8 次 Qdrant，**0/6 题换邻居** ⇒ 非确定性在 embedding 服务端，本仓库的排序/证据选择层没有可改的靶子（两个翻动题的第 3/4 名分差只有 3.5e-4~4.3e-4，微小向量差必然翻名次）。**上一版"75 次零抖动"是只看一批 + 指标只看集合相同 + 分数舍到 4 位造成的过度断言，已撤回**；§7 的数是 03:14 那次汇总的快照，重跑汇总分子分母都会变，要当前值就跑聚合脚本 |
| 故障恢复 | 条件：真实租户模型此刻不可达（`http://20.222.143.1:1030/v1` 从 `cs_agent` 与临时容器都连不上）。结果：新图没崩也没回空白，`outcome=no_result` + 一句安全话术。顺带暴露并修好两个真缺陷：`supervisor_node` 异常分支引用未定义的 `fresh`（`UnboundLocalError`）、空草稿曾被标为 `answered_unchecked` |
| 失败原因可见性 | 模型故障一度只剩 `no_result` 一个现象、原因被节点吞掉 —— 新增 `last_error` 通道并透出到 `TurnResult.error`；`--model platform` 试跑因此直接打印出真因：`OpenAIPermissionDeniedError: 403 Free quota exhausted`（DashScope key 免费额度耗尽） |
| HTTP 入口层 | httpx ASGI 直跑 `entrypoint.py`，第一轮 5 项（后来扩成上表下方那 20 项传输与边界检查）：JSON 与 SSE 文本逐字节相同；同 `turn_id` 重放不追加第二轮；`has_pending_ticket` 与开关 off 均 409 回原入口；缺身份头 401；租户/线程不匹配 403（身份校验已前移到建图之前）。这轮又抓出两个真缺陷：SSE 路由返回裸 generator 不会成流、`run_guarded` 闭包引用了不存在的 `request` |
| 网关诊断 | `probe_model_gateway.py`（只读、零推理消耗）：t_admin=`qwen3.8-flash`@DashScope TCP 通但 403 配额耗尽；t_demo 自定义端点连接被拒；`token_usage` 显示 10-26 00:16 前的基线用的是 `qwen3.8-flash-next / source=custom` |
| 阶段6 两个臂 | **已采到，同一把尺子逐题对照**（报告 §8g）：`run_legacy_heldout.py`（旧路径，进程内直调 `agent_chat`）+ `run_ab_live.py`（新图）在**同一个真实模型**（`deepseek-v4.1-flash`，平台 key 换 id）上各跑 36 题 ⇒ 旧 **35/36 · p50 3.4s · 43k token** vs 新 **34/36 · p50 7.8s · 149k token**；逐题差异 回退 1 / 保持失败 1 / 翻正 1，闸门退出码 1。旧路径那一分"多出来"的是 `ho-c11` 走 HITL 中断、`content=""` 被字面尺子判通过（换成逐题闸门的尺子，两臂同为 **34/36**）。**同臂串行复跑第二次 = 33/36、逐题翻转 3 例**（2 例是尺子形状）⇒ 质量结论是**不可区分**，不是"新图差 1 题"。**结论：质量不可区分、成本 2.3~3.4 倍（r2 那臂 token 176k、p50 8.5s）** |
| 跨进程重启续聊（阶段3） | **PASS**（`run_all.sh pg-restart`）：容器进程 A 写 6 轮 / 120 行检查点、水位线推到 8；**另一个独立容器进程** B 从真实 PG 恢复出 12 条消息、水位线仍为 8（8 摘要 + 4 原文）；随后 `adelete_thread` 归零 |
| 跨进程并发写同一线程（阶段3 补） | **agent/ 侧已修好并实测**（`run_all.sh xproc 3 4` → `probe_cross_process_lock.py`，报告 §4b）：真子进程 + 真 `AsyncPostgresSaver` 同时打同一条线程。不加锁六批丢 58~75%（合计 80 轮只落 28 轮，且 messages 通道与 ledger 通道丢的程度还不一样）；接上 `agent/thread_guard.py` 的按 thread key pg advisory 锁后 **六批 80 轮零丢轮、零拒绝、零残留**，代价是同线程并发轮次串行化（5×4 那批最慢 worker 2.7s→6.2s，约 310ms/轮排队）。默认不接（`thread_guard=None` ⇒ 与今天逐字一致），只覆盖 `agent/` 那条路；挂载侧 M10 在旧接法仍是进程内锁；**换接法（`agent/cutover.py`）之后与 `agent/` 同一份实现，可开 advisory 锁**（§5j） |
| 传输层与边界（阶段6 不需模型的那一半） | **20/20 PASS**（`run_all.sh http`，真 ASGI 入口 + 真 PG）：开关 off 与未完成审批各 409 且**零检查点写入**；跨租户线程 403 且不落库；SSE content 与 JSON content 逐字节相同、`end` 帧携带同一 `turn_id`/`outcome`/`agents`；故障轮 200 + `no_result` + 非空白 + `error` 透出 |
| 幂等的正确度量口径 | 起初用"检查点行数不变"断言重放幂等 → **FAIL（24→48 行）**。真相：LangGraph 每个 super-step 写一行，重放本就该追加行；幂等在**通道**里（`add_messages` 按 id 替换、ledger 按 `turn_id` upsert）。断言改为比较 `(messages, ledger, distinct_ids)`，重放前后 `(2,1,2)` 逐项不变 |
| 补全级可达性（不是 TCP 级） | **`GET /models` 之后结论翻转**（报告 §8g）：`tenant:t_demo` 仍拒连、`platform:t_admin` 配置的 `qwen3.8-flash` 仍 403 免费额度，但同一把 key 列出的 182 个模型里 **`glm-5.3` / `deepseek-v4.1-flash` 能出词**。此前"两条路都不通"是**我猜模型名**猜出来的（51 次补全、27 个源/模型名、成功 2）。正确顺序：`run_all.sh models` ①列模型 ②对列出的名字补全 ③再谈账号级封禁。`gateway` 探针只看 TCP 会把 t_admin 误判为可用，这条仍然成立 |
| 字面判定的尺子 | 发现自己引入的测量缺陷：`run_ab_live.literal_score` 只去 `[*_`]`、不去空白，而官方 `score_heldout.norm` 两者都去。同一份 `ho_off_r4.jsonl` 因此被判 32/36 vs 已提交的 33/36，**差在 `**1 个工作日内**` 里那个空格**。已统一 `norm()`，重算复现 33/36 且与 checklist 逐题零翻转，并钉成 `tests/test_ruler.py` |
| 记忆逐题对照的覆盖面 | 冻结十例是**同骨架 × 不同取值**（每题 12 轮+探针，四臂水位线一律 16、摘要一律 234 字），只能证明取值替换。已补 `--geometry`：锚点深度 0/3/6/9 × 对话长度 8/13/20（长到压缩两次）共 11 例。结论升级：对照臂逐题**波动幅度 3**（形状依赖），本实现**波动 0**、进上下文 33/33、vs 滑窗 改善 8 / 回退 0。**再加一层真实模型**（报告 §6f）：同一模型 `deepseek-v4.1-flash` 跑 10 条长对话两臂 ⇒ 旧路径 **0/10**（十例 `route=order`，复述题被规则路由吞掉）、新图 **5/10**（翻正 5 / 回退 0 / 缺题 0），未全中的**同一批 5 例**（mem-02/05/06/07/10）给三种**可重叠**的归因：4 例 `watermark=0`＝并发跑两臂时摘要辅助调用被限流，检查点走"失败就不删"分支留了全量原文（所以这 4 例根本没有记忆丢失；其中 mem-01/02 两例串行复跑，水位线正常推到 18 / 10 且两针全中，见 `live_mem_recheck.jsonl`——**另外两例没有逐条复跑**，结论靠的是"失败就一条都不删"这条代码分支与 24 条全量在检查点这个事实）；4 例答复原文写着"不能凭记忆报编号/必须先核到这单"＝安全规则自我消音；1 例（mem-06）24 条全在上下文里却说"您没提供过订单号"＝真的长上下文利用失败；2 例（mem-07/10）单号复述对了但抬头写成"您那家公司的抬头"＝字面尺子的同义词盲区。逐题表 `run_all.sh mem-diff` → `runs/mem_diff_dsv41_off_vs_on.md`（判据在表里重算，不采信行内 `containment`；每例并排显示水位线与两臂 token，污染例直接标 ⚠）。⚠ **这一格后来被 §6g 修了两处**：①`watermark=0` 不是只有"并发限流"一个原因——串行重采的 r2/r3 各自仍有 4 例与 3 例为 0，而摘要跳过的原因当时只进 `deps.trace`、这一臂的 trace 不落 PG（`agent_trace_runs` 最新一行 10-26），所以旧批次已无法归因、只能标 ⚠；新采集件带 `compact_ledger`，0 分两种写法（`摘要失败×N：原因` / `N 轮没到触发线`）。②"未全中的**同一批** 5 例"只在 r1 成立：修复前四批重采合计落在 4~5/10、回退每次 0，但**通过名单每批都在换（四批交集只有 mem-01）** ⇒ 只保留合计级主张与两个端点（mem-01 五批全过、mem-07 五批全败），个案级"某例翻正"不成立。两臂从 r3 起还带 `script_sha`（12 句话按发送顺序的指纹，两臂逐例等值）与逐轮转写（用户原话全文 + 答复前 120 字 + 单轮墙钟），`mem-diff ... strict` 能把"是不是同一段对话"变成门禁，`--transcripts` 出逐轮并排附录；测试 12 项 `tests/test_mem_selfproof.py`（报告 §6g；其中"附录把三种空格子分开写"那一项顺带量到一个新事实：**旧臂每一例的第 1 轮＝那句复合咨询，20 行数据全是 `success=true / is_interrupt=true / answer_chars=0`**，客户侧该轮没有任何正文，而新图同一轮 10/10 有正文且 `roles=[faq, order]`——§6g 发现四）。⚑ 补一句**已经找到并修掉的根因**：带 `compact_ledger` 的第一批（r4）显示摘要失败 34/41 次全是 `empty_summary`（200 成功、正文空），隔离探针证明是**摘要继承了聊天模型的 `max_tokens=1024`、被这个模型的思考 token 吃光**（臂长输入下 4 次里 3 次 `finish_reason=length`，其中两次正文被截成 12/100 字却仍算"摘要成功"并推进水位线）；`agent/memory.py` 现在给摘要单独 `SUMMARY_MAX_TOKENS=4096`，并把 `finish_reason=length` 一律判为 `truncated_summary`（一条都不删），复测在 r5）：修完之后空正文率 **83%→5%**、`watermark=0` 3 例→**0 例**、新臂两针全中 **4/10→9/10**（旧臂同批仍 0/10、回退 0；剩下 mem-07 是"记得但不肯把编号说出来"的策略性自我消音，不是忘；token 成本没降，777k vs 770k） |
| 挂载后的 app 侧四 Agent 图（10-27 18:15 起在工作树；**10-28 起已不在生产入口上**，见 §5j） | **12 项实测：6 通过 / 6 发现**（第 12 项 `M2b` 测的是"带上幂等键会不会重放"，今天 `run_turn` 不收这个参数所以必为发现；候选补丁打上后它 PASS，`M5b` 同理，见报告 §5i）（`run_all.sh mounted` → `runs/mounted_graph_probe.jsonl`，走生产入口 `four_agent.run_turn`、脚本化模型、零真实数据）。通过：同轮双委派+`answer` 落检查点、顽固违规 2 次收敛为安全话术、未核验订单断言 0 泄漏、水位线自洽且摘要失败一条不删、同进程并发 4 轮 4 问 4 答不丢。发现：**重放无幂等键**（`ChatRequest` 无 `turn_id` → 同一句问两次，state 里 2 条 HumanMessage、两次都记账）、**回退后上下文分裂**（新图只写 `support_v2:*`，`faq_bridge` 停在 8 条不动 → 切回 off 时 ON 期间每轮都看不见）、**故障留半轮且原因不可见**（1 条无人回答的 HumanMessage、故障那一轮自己的 trace 里 0 个 error 事件；`failure_turn_error_events` 这一列就是为它新加的，见 §5i）、**跨进程并发写同一线程会丢轮**（两实例共 saver 共 thread，5 次试验丢 1~4 次（历批复现的数在 1、2、4 之间跳，但每一批都在丢）；`agent/graph.py` 同样会丢，且它的 `turn_id` ledger 也没保住 → 平台层性质，不是挂载引入；当前部署 `uvicorn.run(app)` 单进程所以进程内锁够用，加 worker/副本前要先上 Redis 锁）。同时确认可见历史**不由 agent 写**（business MySQL `messages`，`realtime.py:336/407/521`），省掉 `_record_to_checkpoint` 不影响前端历史；也确认**运行中的服务还没这张图**（`docker exec cs_agent grep -c four_agent /app/app/api/chat.py` → 0）。详见报告 §5e |

| 挂载图的记忆并入同一逐题协议（阶段4/6 交叉验证） | **可见性与旧路径同分，可恢复性更差**：`run_all.sh mounted-memory` → `memory_context_mounted.py` 走真实入口 `four_agent.run_turn`，同一批转写 + 同一个"改写没锚点"的最差摘要器，产出 `mounted_v2` 臂并与四臂合并成两张五臂表（`runs/memory_five_arm_frozen.md` / `memory_five_arm_geometry.md`）。冻结 10 例：`window_only` 0/30 进上下文、30/30 可恢复，`mounted_v2` **完全相同**，`watermark` 30/30 + 30/30。结构变体 11 例：`mounted_v2` 进上下文 9/33（= 旧路径），**可恢复只剩 12/33**（旧路径 33/33，`delete_rebuild` 6/33）。机制原因：挂载图喂 `summary[:1000] + messages[-12:]`（窗口实为 6 轮），且压缩经 `RemoveMessage` **把被吸收的消息移出图通道**，没有锚点回补。另注意冻结语料是 11 轮 + 探针 = 24 条，恰好卡在 `>24` 触发阈值之下，所以那份语料里它根本没压缩（`merge_calls=0`）——30/30 的可恢复是巧合不是能力。报告 §6e |
| 逐题差异闸门（对照管道的最后一段） | `evaluation/heldout_compare.py` + `run_all.sh compare`：直接吃任意臂的 **answer jsonl**（旧路径采集、新路径 `live` 采集都可以），判定复用官方 `score_heldout.norm`，逐题分保持通过/翻正/**回退**/保持失败，任一臂出现回退即退出码 1。用两份真实臂自检：基线自比 33/36→33/36 逐题零翻转（证明不造翻转），`ho_mem_r2` 臂 33→34、翻正 1、保持失败 3→2（证明看得见变化），基线 p50/p95 5740.5/10511.2 与 §8 那条 36 题实测数互为交叉验证。**补掉了一个静默接缝**：`score_heldout.py` 吃答案、`ho_regression_diff.py` 吃 checklist，而采集器写的是自己的字段名，此前模型恢复后还要手工转格式。表见 `runs/heldout_diff_sample.md`，测试 8 项 `tests/test_heldout_compare.py`（报告 §8d） |
| 候选镜像离线自检（挂载后能否 import、开关语义） | **11 项全通过**（`run_all.sh image` → `evaluation/verify_mounted_image.py`；单独 build `cs-agent-candidate`，不替换运行中的 `cs_agent`）：`app.api.chat` 与 `app.multi_agent.graph` 可 import、路由已注册、`FOUR_AGENT_GRAPH` 的 unset/on/true/0 语义正确、**开关打开后未完成 HITL 审批仍回旧图**、`agent/` 确实不在镜像里。这轮抓出自己一个测量错误：第一版把仓库根挂在 `PYTHONPATH` 上，导致"`agent/` 不在镜像中"这条**沉默地假通过**；已改为只探测镜像自身路径并加"探测路径不得含挂载点"自检 |
| 同一份对抗语料打在挂载图的 Reviewer 上 | **实测有回退，不是未验证**：`run_all.sh mounted-guards` → `evaluation/probe_mounted_guards.py` 复用 §5d 那 30 例（语料不复制，唯一翻译是证据码 `ok`↔`OK`），逐例走生产入口，并核"草稿是否原样出现在 `answer`"。**两遍**：语料原样 **漏放 9/25**（9 条违规草稿逐字发给客户，误拦 0/5），把该配的委派配齐后 **漏放 22/25**（只剩 `txn-06` 被拦；原始 24，其中 `val-01/02` 逐例改判，见 §5h）。差别说明挂载图拦下的主要不是内容而是问句形状——第一遍 16 例被拦里 **12 例只由"必须委派"贡献**，第二遍内容级规则只开火 1 次。根因可核到行：`value_no_evidence` 只看 `evidence_ids` 非空不比数值（`graph.py:196-199`）、`order_unverified` 只看 `code=="OK"` 不比单号（`:205-209`）、无内部泄漏规则；9 例里 7 例是同一个邻接缺陷（`_UNSUPPORTED_ACTION` 要求动词紧贴「已(经)」），正是我在 `agent/guards.py` 改成子句级之前失败的那类。`content_guard` 不构成兜底（只 3 个字面词，且看进模型前的消息不是草稿）。口径还更宽松：agent/ 只有 hard 算拦截，挂载图任何 `review_feedback` 都换成兜底 → **9 vs 0 不是判据松紧造成的**。修法属于改 app，未授权未动，报告 §5f；**但修法已经写出来并实测过了**：`run_all.sh mounted-fixed` 把补丁只打在临时副本上（工作树 `python_agent/` 一字未动），同一份语料同一把尺子跑对照——corpus 漏放 **9→0**、backed 漏放 **22→0**（`val-01/02` 两个臂都逐例改判：它们复述的正是那份证据的原文，放行才是对的；`val-03..08` 的数值在证据里根本没有，算真漏放）、误拦 **0**；可审查 diff 在 `deploy/mounted_review_fix.patch`（+155/-6，只动 `review()` 一个节点）、`deploy/mounted_persistence_fix.patch`（+21/-6，§5e-M2 的重放幂等键）与 `deploy/mounted_recovery_fix.patch`（+16/-3，§5e-M5b 的失败轮恢复），报告 §5h/§5i；同一命令还会把挂载图探针跑两个臂出逐题差异表（`M2b` 与 `M5b` FINDING→PASS、零回退、零缺题；M3 故意不出补丁）。这一轮还揪出自己 harness 的一个**自证陷阱**：探针把草稿本身当成 FAQ 委派的原话喂进图，于是任何拿证据比对草稿内容的规则都会 100% 自证通过（修 fixture 后原 Reviewer 的 9 与原始 24 逐字复现，说明原结论没被污染）。修 fixture 时又发现**全角单号两套实现都漏**：`ＴＢ１２３４５６７` 归一化前正则根本看不见——已在 `agent/guards.py` 加 NFKC 归一并钉回归位，全量 **90 passed** |
| 挂载后的可见文本契约差异 | 旧路径 FAQ 路由会先过 `finalize_faq_reply` 再 `mark_demo_faq_reply`（`chat.py:159-160` / `:306-307`），后者给 `t_demo` 强制加一行「以下为演示环境中的示例答复；涉及的店铺政策不代表真实商家承诺。」——实测旧基线 36 条答复里 **25 条带这行**，而挂载分支不加（`:104` / `:223` 直接拼 footer）。同时 `finalize_faq_reply` 的按问型模板兜底整层被绕过（Reviewer 改的是"整段拒答"，覆盖面不同）；footer 在旧基线只有 2/36 条出现、挂载后每条都有，但金标 required 不含"工作时间/智能客服"，不影响字面判定。详见报告 §5e |
| 旧路径延迟基线（阶段6 对照的"旧"那一侧） | 现网 `agent_trace_runs` 1741 轮（10-21→10-26）实测：**`faq` 路由 n=1120 / p50 4774ms / p95 12735ms / max 70787ms**；`general` n=341 / p50 2836ms。另有 **250 轮（14.4%）不调模型、<200ms**（`handoff` 28ms、`faq_fast` 28ms、`order` 多为 50ms 的核验短路）。两条含义：新图每轮 ~113–142ms 结构开销占 faq p50 的 2.4–3.0%（不是瓶颈）；但 `chat.py` 的 four_agent 分支在 `decide_turn` 之前就 return，**开关一开这 14.4% 的快路径全消失**（报告 §8c）。`run_all.sh trace-baseline` 只读聚合，`runs/legacy_trace_latency.txt` |
| 前端与网络边界（阶段6 不需发消息的那一半） | **14 项全通过，退出码 0**（`run_all.sh frontend` → `evaluation/probe_frontend_topology.sh`，原始数据 `runs/frontend_topology.jsonl`）。`cs_agent` 端口列 `8000/tcp` 无宿主映射、宿主直连 curl **000** 拒连；容器里活的 nginx 有 2 条 `api/agent` 规则，`/py-api/api/agent/chat`、`/api/agent/chat`、`/agent/chat` 三条**全 404**，同一台 nginx 上 `/py-api/health` 与 `/chat/login.html` 均 200（控制项，证明 404 是路由决策）。聊天是 `location = /ws/chat` → `business:8001`，全仓库没有浏览器代码调 `/agent/chat`。从 business 容器取 `http://agent:8000/openapi.json`：**36 条**路由里 `/agent/chat`、`/agent/chat/stream` 在而 **`/agent/chat/v2` 不在**——§5e"图只在工作树"的第三种独立测法。含义：**开关只改 business 内部往哪张图走，不新增客户可触达的面**（验收标准 5 的越权那一半在传输层成立）。另外 `specialist` 列宽实测 `varchar(32)`、现网 1741 行最长 10 字符、三角色全委派 17 字符 → 我此前那句"超 32 就静默丢 trace"收窄为"余量 15 字符，将来角色变多才触发；代价是整行丢失（`app/services/trace.py:62-65` 只 logger.exception）"。这轮也抓出自己一个假发现：Git Bash 的 MSYS 把 `docker exec … grep /etc/nginx/...` 的路径改写成 `D:/git/Git/...`，直接 exec 得到 `got=none`，套进 `sh -c` 才得到 2（报告 §5g） |
| 采集器的传输与记账段（确定性 HTTP 替身臂） | **真 HTTP 往返跑通，并当场抓出我自己的两个缺陷**（`run_all.sh live-mock` → `evaluation/mock_openai_server.py`；原始数据 `runs/mock_heldout.jsonl` / `mock_mem.jsonl`，每行 `comparable=false`）。这不是语言模型：答复是从工具返回里抄的，所以字面通过率只说明管道通、不说明措辞对；`--model custom` 配 `--parity` 直接拒跑。检索侧是真的（Qdrant + embedding 今天可用，**阻塞只在 chat**）。① 步数预算：LangGraph 把子图步数计入父图剩余预算，3 pass × 双委派这一合法形状在旧预算下抛 `GraphRecursionError` → 整轮 `no_result`；bisect 实测 supervisor 内层 16/20 失败、24 起通过，已提常量 `SUPERVISOR_STEP_BUDGET=32` / `TURN_STEP_BUDGET=48`，service 与测试读同一常量。② token 记账：usage 通道按 turn_id 覆盖，重试轮只记最后一个 pass（9 次调用报 3 次），改为 open_turn 清本轮 + 每 pass 累加，重放仍幂等。修后同一臂 12 题：0 个 no_result / 0 个 graph_error，延迟 p50 430ms、token p50 689 / max 2998，66 次 HTTP 请求 0 错误。顺带修了闸门口径：缺题不再被同时算成回退（仍拦闸门）；全量测试 **90 passed**（报告 §8e；§5h 又加了一条全角单号回归位） |
| 现场演示落到真实持久化（阶段6「可现场演示」那条） | **两臂逐字节同稿**（`run_all.sh demo-pg`；`runs/demo_offline.jsonl` 与 `runs/demo_pg.jsonl` **sha256 相同**，9 行）。复合轮 `agents=[faq, order]` / `attempts=1` / 一条证据，长对话 8 轮 `total=18 summarized=12 verbatim=6`，重启段复用同一 saver 恢复 18 条且摘要仍标"不作为订单状态或店铺政策的依据"；PG 侧自建线程 **161 行 → 删除后 0 行**。以前 `--postgres` 只在 docstring 里标着"没对部署库跑过"，现在跑过了。`--thread` 必须是 `<tenant>:` 前缀（`thread_key()` 的租户校验，第一版传裸名两臂全挂——我引入的回归，跑一遍才发现）；`--postgres` 故意不读 `$POSTGRES_DSN`，免得误跑静默写部署库（报告 §8f） |
| 探针的已知局限 | 固定 `k=3`、绕过服务进程内 5 分钟 KB 缓存，测的是"同查询重复检索的候选稳定性"，不等于"缓存命中与未命中的差异"；`trace-baseline` 聚合的是现网 self-run 数据，与 36 题留出集不同源，两把尺子不混用 |

## 验证账本

已验证（脚本化模型 + `InMemorySaver`，`pytest agent/tests` → **112 passed**；同一套测试在本机 venv 与 `cs-test:agent` 镜像（py3.13 + 生产 requirements）两边都通过）：复合问题同轮委派两个专职 Agent 并汇合；trace/`ledger` 可看出实际调用了哪些 Agent；重试一次即收敛；顽固用例停在 3 pass 不死循环；6 类 hard 与 5 类 soft 规则（另加 30 例对抗改写语料：改写后的退款承诺、中文数值、带空格单号前缀全部拦下，5 例良性对照零误拦）；同 `turn_id` 重放不重复消息/记账/token；跨进程复用同一 saver 续聊；旧会话一次性只读导入且重复导入幂等；水位线推进、摘要失败不越界、锚点回补、长对话零丢轮；同线程**同进程**并发 4 轮不丢记录；**跨进程**并发原本两个实现都会丢（报告 §5e M10/M11），现在 `agent/` 这条路上了一把按 thread key 的 Postgres advisory 锁并实测六批零丢轮（§4b，`run_all.sh xproc` 可复跑）；挂载侧 2026-10-28 起也接同一个开关（`agent/cutover.py` 建 service 时同样调 `_guard_from_env()`，§5j B10/B11）；指代跨压缩存活；**摘要含数值仍不得为本轮背书（带「同值有证据即放行」对照组）**；**客户改口后订单事实强制本轮重核（同样带放行对照）**；上一轮证据不得为下一轮数值背书；JSON 与 SSE 文本一致（ASGI 层）；未鉴权线程被拒；模型故障降级不崩不空白且原因可见；字面判定尺子与官方 `score_heldout.norm` 等价且复现冻结基线 33/36；**留出集逐题差异闸门**（同一份数据零翻转、空格重排不得判回退、缺 required 或命中 forbidden 必须判回退、缺题单独计数、退出码由回退驱动）。真实服务侧适配器 `deps_for_app()` 三种模式（tenant 模型 / platform 模型 / 生产或 shadow FAQ）均可构造。PG 与检索侧结论见上表。

仍未验证：**与 33/36 那条冻结基线可比的**真实模型对照（`t_demo` 自己配置的端点至今拒连，能出词的是同一平台 key 下换模型 id 的两条路，模型不同 ⇒ 只能两臂互比，行内 `comparable=false`）。**36 题量级的重复采集已经做了**（同臂串行复跑第二次 ⇒ 34/36 vs 33/36、逐题翻转 3 例、其中 2 例是尺子形状；报告 §8g 那一节），所以质量结论已从"差 1 题"收窄成"**不可区分**"；仍未做的是 3 次以上取多数的置信区间与 10 条长对话量级的复跑。另：Qdrant 缓存态差异；浏览器端"发一条消息 → 看到答复"的端到端渲染（会写 business MySQL 的 `messages`，需单独授权；**链路与边缘边界已由 §5g 的 14 项实测覆盖**）。**已经验证过的部分要正名**：真实模型下的答复质量、延迟与 token 成本，以及同模型的新旧逐题差异，见报告 §8g（旧 35/36 · p50 3.4s · 43k token vs 新 34/36 · p50 7.8s · 149k token；此前"模型路全堵 33 次 0 成功"是**猜模型名**造成的误判，`GET /models` 后翻案为 51 次 / 27 个源 / 成功 2）。挂载那一行**已由 app 侧自己完成**（`chat.py` 的 `FOUR_AGENT_GRAPH` 分支 + `app/multi_agent/graph.py`，开关缺省 off、生产镜像未重建），本目录不提供它；agent/ 的 `entrypoint.py`（`build_router(identity)` / `create_standalone_app()`）仍是并行的第二种接法。挂载侧的实际行为与发现见报告 §5e，记忆逐题见 §6e，**Reviewer 覆盖面是实测有回退（漏放 9/25、配齐委派后 22/25）见 §5f**——这三节合起来构成"开关继续留 off"的证据。

> 汇总版的效果与边界（含旧路径实测数、四臂逐题表、检索负结论、阻塞取证与解锁后的复现命令）见 **[实际效果报告.md](实际效果报告.md)**。
>
> 现场要跑的那一条（三幕命令、每幕指出哪一行、被追问时怎么答、收尾怎么确认零残留）见 **[演示流程.md](演示流程.md)**。

## 复现入口

```bash
bash agent/evaluation/run_all.sh test           # 镜像内跑同一套 90 项测试
bash agent/evaluation/run_all.sh all            # 一条命令跑完全部"不需模型、不需额外授权"的项，逐条退出码/耗时（账本 runs/acceptance_ledger.txt）
bash agent/evaluation/run_all.sh demo           # 现场演示：复合咨询 + 长对话续聊 + 重启（内存 saver）
bash agent/evaluation/run_all.sh demo-pg        # 同一套演示落到真实 AsyncPostgresSaver，落盘行数对照后自删（§8f）
bash agent/evaluation/run_all.sh pg             # 真实 AsyncPostgresSaver（自建线程，跑完自清理）
bash agent/evaluation/run_all.sh pg-restart     # 跨两个独立容器进程续聊（先 --keep 再 --resume-thread）
bash agent/evaluation/run_all.sh xproc [w] [t]  # 跨进程共写同一线程：--no-guard 复现丢轮 → --guard 要求零丢轮（§4b）
bash agent/evaluation/run_all.sh http           # 真 ASGI 入口 + 真 PG：20 项传输与边界检查
bash agent/evaluation/run_all.sh retrieval      # 只读检索稳定性探针（单批）
bash agent/evaluation/run_all.sh retrieval-batches 3  # 连跑 3 批独立进程并汇总翻动率（§7 三层表的来源）
bash agent/evaluation/run_all.sh embedding-drift      # 分层定因：同一文本的 embedding 会变吗 / 固定向量的 Qdrant 邻居会变吗（§7）
bash agent/evaluation/run_all.sh gateway        # 模型网关 TCP 可达性（零推理消耗）
bash agent/evaluation/run_all.sh completion     # 最小补全：配额/密钥层拒绝在这里才现形
bash agent/evaluation/run_all.sh completion qwen-max,qwen-turbo  # 附探别的模型名：分清账号级封禁与单模型没额度（§8）
bash agent/evaluation/run_all.sh memory         # 四臂对照：冻结十例 + 结构变体 + 两张逐题表
bash agent/evaluation/run_all.sh overhead       # 编排开销与上下文规模（不需模型）
bash agent/evaluation/run_all.sh overhead-pg    # 同上，真实 PG；跑完自删线程并自检行数
bash agent/evaluation/run_all.sh mounted        # 挂载入口 12 项实测账本 + 十条不变式闸门（零模型调用）
#                                              2026-10-28 起入口 = four_agent → agent.cutover → agent/ 图（报告 §5j）
bash agent/evaluation/run_all.sh demo-mounted   # 开关那条路的演示留痕（复合咨询+长对话+重启续聊）
bash agent/evaluation/run_all.sh mounted-memory # 挂载图并入同一逐题记忆协议 → 两张五臂表（§6e）
bash agent/evaluation/run_all.sh mounted-guards # 30 例对抗语料打在挂载入口 Reviewer 上，两遍（§5f 旧接法 / §5j 新接法）
bash agent/evaluation/run_all.sh mounted-fixed # 同一语料实测候选修法（补丁只打在临时副本，工作树不动；§5h）
bash agent/evaluation/run_all.sh compare        # 留出集逐题差异闸门自检；带一个 jsonl 参数=对照新臂（§8d）
bash agent/evaluation/run_all.sh image          # 候选镜像离线自检：import + 开关语义（不动运行中的服务）
bash agent/evaluation/run_all.sh frontend       # 前端与网络边界 14 项：端口表/边缘 404/活进程路由表/列宽（§5g）
bash agent/evaluation/run_all.sh trace-baseline # 旧路径 1741 轮延迟分布（只读聚合，§8c）
bash agent/evaluation/run_all.sh live           # 阶段6 真实模型采集（需模型可用）
bash agent/evaluation/run_all.sh live-mock heldout 12  # 真 HTTP 往返打到确定性替身：测管道与记账，不测措辞（§8e）
.venv/Scripts/python.exe -m agent.demo --turns 8                 # 现场演示脚本（内存 saver）
.venv/Scripts/python.exe -m agent.demo --thread pinned --keep     # 固定线程名并留档，便于接着演「重启续聊」
# --postgres "$DSN" 走真实 AsyncPostgresSaver（§8f）；故意不读 $POSTGRES_DSN，避免某次误跑静默写进部署库
```
