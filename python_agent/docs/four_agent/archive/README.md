> **本目录已归档（2026-10-29），内容不再参与可执行链路。**
>
> 原位置 `python_agent/scripts/four_agent_deploy/`。归档原因：
> - `make_cutover_patches.py` 生成的两个切换补丁锚点已失效或有害——`cutover_chat.patch`
>   把 `chat.py` 改回 `from agent import cutover`（该模块已不存在），`cutover_compose.patch`
>   给 compose 加 `- ../agent:/app/agent:ro` 挂载（该目录已不存在）。
> - `make_mounted_review_fix.py` 的目标 `app/multi_agent/graph.py` 是已下线的旧多 Agent 图，
>   文件本身不存在，脚本必然失败；`run_all.sh mounted-fixed` 分支同步退役。
>
> 四 Agent 主图已并入 `python_agent/app/agent/support/`，随 `python_agent` 镜像一同构建，
> 不再需要切换补丁或额外挂载。以下内容保留为切换期的历史记录。

> 历史实验记录：本文涉及的 `FOUR_AGENT_GRAPH` 与 `X-Agent-Graph` 回退开关已退役。当前普通聊天固定走 `app/agent/support/` 四 Agent 图；未完成审批使用旧检查点兼容路径。

# 切默认入口：开关已挂在 chat.py 上，本目录的两个补丁改为备用

阶段6 要"验收后切换默认入口"。这一页在 10-27 晚被现实更新了一次：**切换不再是"把模块名换成
`agent/cutover.py`"，因为 app 侧已经自己实现并挂载了一张四 Agent 图**。下面是实测过的现状。

## 现状：两条并行实现同时存在，开关缺省 off，且运行中的服务还没有任何一条

`python_agent/app/api/chat.py` 的 JSON 分支（第 96 行）和 SSE 分支（第 211 行）都先问
`four_agent.enabled(pending_ticket=...)`，再调
`four_agent.run_turn(app, tenant_id, thread_id, user_message)`，然后**只读两个字段**：

```python
state["answer"]                    # 拼 SERVICE_FOOTER 后返回客户
state.get("model_messages", [])    # 交给 _record_usage_after_chat 记账
```

开关是 **`FOUR_AGENT_GRAPH`**（不是新链路自己的 `SUPPORT_AGENT_V2`），检查点命名空间同样是
`support_v2:`。10-27 18:15 起 `app/services/four_agent.py` 指向
`app/multi_agent/graph.py`（官方 subagent-tools 模式：supervisor + `consult_faq` /
`consult_order` / `consult_general`），不再是旧的静态 DAG。本目录的 `agent/cutover.py`
因此变成**第二种接法**（把开关指向 agent/ 的实现），两条路共用同一契约，由
`agent/tests/test_cutover.py` 钉住（含"身份不合法时不得建图"这条）。

**运行中的服务里两条都没有**（实测，不是推测）：

```
docker exec cs_agent grep -c four_agent /app/app/api/chat.py   → 0
docker exec cs_agent ls /app/app/multi_agent                   → 不存在
docker exec cs_agent env | grep FOUR_AGENT_GRAPH               → 无（缺省 off）
```

agent 服务的代码烤在镜像里（`build: ./python_agent`，唯一挂载是 `cs_kb_files` 卷），所以
**挂载只发生在工作树里，要生效必须重建 agent 镜像**。

## 开关矩阵（四个开关属于两条不同的路，缺省都是"不改变今天的行为"）

| 开关 | 读它的代码 | 缺省 | 语义 |
| --- | --- | --- | --- |
| `FOUR_AGENT_GRAPH` | `python_agent/app/services/four_agent.py:24` | 未设 = off | `on/true/1` 才走 app 侧那张图；否则走旧路径（`decide_turn` + specialist） |
| `MEMORY_INJECT` | `python_agent/app/services/memory.py:57` | 未设 = on | **三级开关的最后一档**：① Redis `cs:memory:inject=off` 平台级一票否决（不用重启）② 租户级 `tenants.memory_enabled`（默认 1，取失败**按开**，降级行为同旧版）③ 本 env。任一为 off 即不注入摘要段 |
| `AGENT_RECURSION_LIMIT` | `python_agent/app/api/chat.py:38` | 0 = 不收紧 | 沿用 LangGraph 缺省 25；设 `>0` 显式收紧，超限抛 `GraphRecursionError` 走既有友好错误路径 |
| `SUPPORT_V2_THREAD_LOCK` | `agent/entrypoint.py:61` | 未设 = 不接锁 | 只对 `agent/` 那条路生效，且要同时给 `POSTGRES_DSN` 才接（按 thread key 的 advisory 锁，§4b）；单进程部署不用开。**2026-10-28 起挂载入口也吃这一个开关**——`agent/cutover.py` 建 service 时同样调 `_guard_from_env()`，所以开关置 on 之后两条路的跨进程保护是同一份（§5j 的 B10/B11） |

⚠ **切流前有一件事必须有人拍板，而它不在这四个开关里**：两条路对"能不能复述客户自己说过的单号"
已经写了**相反**的答案——`app/services/memory.py:29-31` 的 banner 明文允许
（"用户说过的订单号、金额等可直接复述引用"），而 `agent/guards.py:222-224` 的 Reviewer 把任何
本轮未核验的 id 一律 hard 拦（**只复述、不断言状态也拦**，`agent/tests/test_memory.py` 已把这条钉住）。
证据与代价见 `../实际效果报告.md` §6f 第 4 条。**"断言订单状态"这一边两边都收紧，不需要拍板**；
要拍板的只有"复述"这一格，因为它会改变客户看到的记忆表现，而 36 题留出集与 10 条长对话的 diff 表
都测不出这个方向性差异。

```bash
# 看一眼今天到底开了什么（当前 .env 里这四个键都不存在 ⇒ 全缺省）
grep -nE '^(FOUR_AGENT_GRAPH|MEMORY_INJECT|AGENT_RECURSION_LIMIT|SUPPORT_V2_THREAD_LOCK)=' .env || echo "(none present) → 全部走缺省：旧路径 + 记忆注入 on"
# 生产库命名空间复核（2026-10-28 07:56 重测：support_v2: 前缀 0 行、checkpoints 合计 3686 行、
# 真实模型两臂的自建线程 %t_demo:v2% 0 行 ⇒ 这张图与这批臂都没有在生产库里留下东西）
```

## 打开与回退（app 侧图那条路）

> ⚠ **2026-10-28 更新：这一段写的"两张图"已经合成一张了**。`app/services/four_agent.py` 现在是薄适配器，`run_turn` 里 `from agent.cutover import run_turn` ⇒ 置 on 之后跑的就是 `agent/` 那张图，而不是下面这些数字所描述的 `app/multi_agent/graph.py`。同一套账本在新接法下重测的结果在报告 §5j：Reviewer 0 漏放 / 0 误拦、失败轮与安全规则都由 `agent/` 那份实现兜住。
> 下面这段保留，是因为它记的是**当时那份独立实现**的实测（§5e/§5f/§5h/§5i 的数字没有作废，只是不再描述生产入口）；
> 而"先别开"这个结论**今天仍然成立**，理由换成了三条：镜像里仍然没有 `agent/` 包（第 0 步）、
> `ChatRequest` 仍然没有 `turn_id`（§5j 的 B2：跨请求重试仍算两轮）、以及起第二个 worker/副本时> 必须同时打开 `SUPPORT_V2_THREAD_LOCK`（§5j 的 B11：两个实例写同一线程实测 6 轮落 3）。

> ~~今天给出的结论是"先别开"~~（原文，描述旧接法）。下面第 0 步的两份离线账本里，`mounted-guards` 是实测**有回退**的：
> 同一份 30 例对抗语料（25 例不安全草稿）打在这张图自己的 Reviewer 上，漏放 **9/25**；把该配的委派
> 配齐、只考内容级规则时漏放 **22/25**（原始 24，其中 `val-01/02` 逐例改判——它们复述的正是那份证据的原文），
> 跨客户单号 5 例、内部泄漏 3 例、禁用交易改写 7 例在这条路上全部原样发给客户（`../实际效果报告.md` §5f）。
> 加上 §5e 的三条持久化发现与 §6e 的记忆逐题表（可见性 0/30、可恢复性 12/33），
> 验收标准 2/3/5 在这条路上都还没过。
> 开关缺省 off，且运行中的镜像里根本没有这张图——**这就是当前的安全状态，不要靠置 on 来"先试试"**。
> 对照着看：`agent/` 自己那条路的现场演示（复合咨询 + 长对话续聊 + 重启）已经在真实 PG 上跑通，
> 并与内存臂逐字节同稿（`../实际效果报告.md` §8f）——但那是 `agent/entrypoint.py` / `python -m agent.demo` 的路，
> 不是这个开关的路；开关这条路的账本仍是上面三份。
>
> 现在**三份修法都在同一个临时副本上实测过**（报告 §5h / §5i）：
> `bash agent/evaluation/run_all.sh mounted-fixed` 给出对照组与补丁组同一把尺子的数——
> corpus 漏放 9→**0**、backed 漏放 22→**0**、误拦 **0**（`GATE PASS`，退出码 0），
> 挂载图 12 个 case 两臂逐题对照 **修好 2 / 回退 0 / 缺题 0**（`M2b_replay_with_turn_id` 与
> `M5b_failure_state_and_visibility` FINDING→PASS）；
> diff 在 `mounted_review_fix.patch`（+155/-6）、`mounted_persistence_fix.patch`（+21/-6）与
> `mounted_recovery_fix.patch`（+16/-3），`python_agent/` 工作树一字未动。**收紧规则的那一处代价**
> 也在这条命令的表里：M2 那一步的 `billed_msgs_per_turn` 2→4（多拦一次 = 多跑一遍 supervisor）。
> **什么会改变"先别开"的结论**：把这三份修法（或更好的——抽出两套实现共用的规则模块）落到 app 侧、
> 让 `mounted-guards --gate` 返回 0，同时 §6e 的可恢复性与 §5e 的 M3/M10 也补上，再置 on。
> 这些没有一件是"翻个开关"能解决的，所以决定权在你，不在我。

```bash
# 0) 先跑离线账本，确认这张图的行为你接受（零模型调用、零真实数据写入）
bash agent/evaluation/run_all.sh all           # 一条命令跑完"不需模型、不需额外授权"的全部项（含 mounted-fixed），逐条退出码；账本落 runs/acceptance_ledger.txt
bash agent/evaluation/run_all.sh mounted        # 12 项：6 通过 / 6 发现（§5e；第 12 项 M2b 见 §5i）
bash agent/evaluation/run_all.sh mounted-guards # 30 例对抗语料两遍：漏放 9/25、配齐委派后 22/25（§5f）
bash agent/evaluation/run_all.sh mounted-fixed  # 三份候选修法在同一个临时副本上的对照实测：语料 9/22→0/0 + 挂载图 12 case 逐题差异（修好 2 / 回退 0 / 缺题 0，§5h/§5i）；今天退出码 0
#   同一份账本还能当硬闸门：加 --gate 后两遍都要 0 漏放 0 误拦才返回 0。今天实测退出码 1，
#   输出 GATE FAIL {'corpus': (9, 0), 'backed': (22, 0)} —— 这就是"别开开关"的可执行版本。
bash agent/evaluation/run_all.sh mounted-memory # 逐题记忆五臂表：可见性/可恢复性对比（§6e）
bash agent/evaluation/run_all.sh frontend      # 前端与网络边界 14 项（§5g）：这一项全通过——开关不新增客户可触达的面

# 1) 先做候选镜像自检：单独 build 一个 tag，绝不 compose build agent（那会替换运行中的服务）
bash agent/evaluation/run_all.sh image
#   11 项：入口模块能否 import、路由数、FOUR_AGENT_GRAPH 的 on/true/1/0 语义、
#   未完成 HITL 审批是否仍回旧图、agent/ 是否确实不在镜像里、以及"探测路径不得含挂载点"的自检。
#   全部离线路径，不连库、不写数据；运行中的 cs_agent 保持原镜像不动。

# 2) 自检通过后，重建 agent 镜像（工作树里的 app/multi_agent + chat.py 分支才会进容器）
docker compose build agent

# 3) 置开关并重启该服务；缺省 off，不写 .env 就等于旧路径
#    ⚠ 别直接 >>：重复行会让 docker compose 取哪一行取决于解析顺序，先判存在再写
grep -q '^FOUR_AGENT_GRAPH=' .env || printf 'FOUR_AGENT_GRAPH=on\n' >> .env
docker compose up -d agent

# 回退（两条都即时生效）
sed -i 's/^FOUR_AGENT_GRAPH=on/FOUR_AGENT_GRAPH=off/' .env && docker compose up -d agent
# 或彻底退回旧镜像：先 git stash 掉 app 侧改动，再 docker compose build agent
```

## 验一轮：五个子步骤（V1–V5），只有 V2 会写数据

```bash
# V1) 先验身份契约——三种负例实测都是 403，且 403 发生在建图之前，所以不产生任何写入
docker exec cs_agent python -c "
import httpx
h = {'content-type': 'application/json', 'X-Internal-Token': 'wrong', 'X-Tenant-Id': 't_demo'}
r = httpx.post('http://127.0.0.1:8000/agent/chat',
               json={'thread_id': 't_demo:deploy-check-1', 'user_message': '你好'},
               headers=h, timeout=20)
print(r.status_code, r.text[:60])"          # → 403 Agent chat identity verification failed

# V2) 真跑一轮复合问题。⚠ 这会真实落库（检查点 / session_meta / token_usage / trace），
#     所以用一次性线程名，跑完按 V4 清理；模型不可用时仍可能留下半轮（报告 §5e 的 M5b，候选补丁见 §5i）
docker exec cs_agent python -c "
import httpx, os
h = {'content-type': 'application/json',
     'X-Internal-Token': os.environ['INTERNAL_API_TOKEN'], 'X-Tenant-Id': 't_demo'}
r = httpx.post('http://127.0.0.1:8000/agent/chat',
               json={'thread_id': 't_demo:deploy-check-1',
                     'user_message': 'ORD-2024-003 到哪了？顺便问下怎么退货'},
               headers=h, timeout=120)
print(r.json()['content'])"

# V3) 看这一轮实际走了哪条路（只读 SELECT，实测列名可用）
docker exec cs_postgres psql -U cs_agent_user -d cs_agent_db -c \"
  select route, specialist, status, duration_ms, answer_id, created_at
    from agent_trace_runs order by created_at desc limit 3;\"
#   开关生效 → route='multi_agent'、specialist 形如 'order,faq'
#   旧路径   → route 是 'faq' / 'general' / 'order' / 'handoff' / 'faq_fast'
#   ⚠ specialist 列宽实测 varchar(32)（§5g，14 项全通过）：现网 1741 行最长 10 字符，三条角色
#     全委派＝"faq,order,general" 17 字符 → 今天有 15 字符余量，不会撞墙。但 four_agent 把
#     specialist 截到 120，且 save_trace_run 被 try/except 包着只 logger.exception
#     （app/services/trace.py:62-65）——将来角色变多或改名一旦超过 32，代价是**整行 trace 静默
#     丢失**（不是截断）。所以我此前那句"一旦超 32 就会丢"要收窄成"余量 15，超了才丢"。

# V4) 清理自己造的探针线程（只删自建线程名，别碰真实会话）
docker exec cs_postgres psql -U cs_agent_user -d cs_agent_db -c \"
  delete from checkpoint_writes  where thread_id like '%t_demo:deploy-check-%';
  delete from checkpoint_blobs   where thread_id like '%t_demo:deploy-check-%';
  delete from checkpoints        where thread_id like '%t_demo:deploy-check-%';
  delete from session_meta       where thread_id like 't_demo:deploy-check-%';
  delete from token_usage        where thread_id like 't_demo:deploy-check-%';
  delete from agent_trace_runs   where thread_id like 't_demo:deploy-check-%';"

# V5) 旧路径延迟基线（切流后拿同一把尺子复测；只读聚合，随时可跑）
bash agent/evaluation/run_all.sh trace-baseline
```

`support_v2:` 是图前缀，所以检查点三张表用 `like '%t_demo:deploy-check-%'`；
`session_meta` / `token_usage` / `agent_trace_runs` 存的是裸 `thread_id`，不要带前缀。
命名空间是否干净可以直接查：`select count(*) from checkpoints where thread_id like 'support_v2:%';`
——实测当前为 **0**（3686 个检查点全是旧路径数据），说明这张图至今没有在生产库里写过。

回退之后要注意 §"切流前必读的三条发现" 里的第 2 条：**切回 off 时，旧路径看不见 ON 期间的那几轮**
（它读 `faq_bridge`，而新图只写 `support_v2:*`）。客户可见历史不受影响，受影响的只是 agent 侧上下文。

## ⚠ 只换 import 会把服务打死——这是实测出来的（仅适用于接 `agent/cutover.py` 那条路）

生产镜像里**根本没有 `agent/` 这个包**：

```
docker compose: agent: build: ./python_agent     ← 构建上下文是 python_agent/
docker exec cs_agent ls /app                     → data evaluation main.py pyproject.toml requirements.txt
python -c "import agent"                          → ModuleNotFoundError（find_spec 返回 None）
```

`chat.py` 的 import 在模块顶层。若直接改成 `from agent import cutover`，服务**启动即失败**
（不是首个请求失败），影响面是整个客服入口。容器也没有代码挂载（只有一个 `cs_kb_files`
卷），所以宿主目录不会自动出现在镜像里。

因此本目录的补丁用的是**带兜底的导入**：镜像里没有 `agent/` 就自动退回旧实现，服务照常
起来，切换失败表现为"还是旧路径"而不是"客服全挂"。

## 三步

```bash
# 0) 先看两个补丁会改什么（此时工作树未被修改）
git diff --no-index /dev/null /dev/null && cat agent/deploy/cutover_chat.patch agent/deploy/cutover_compose.patch

# 1) 让 agent/ 在容器里可 import（只读挂载，不重建镜像、不改 build context）
git apply agent/deploy/cutover_compose.patch      # docker-compose.yaml: agent 服务加 ../agent:/app/agent:ro

# 2) 换实现（带 ImportError 兜底）
git apply agent/deploy/cutover_chat.patch

# 3) 打开开关并重启该服务
docker compose up -d agent                        # 需在 .env 里 FOUR_AGENT_GRAPH=on
```

回退 = 反向应用两个补丁，或把 `FOUR_AGENT_GRAPH` 关掉（缺省即 off，旧路径不受影响）。

补丁可应用性由脚本生成时就校验过，随时可重跑确认：

```bash
python agent/deploy/make_cutover_patches.py
#   cutover_chat.patch     target crlf=417 → APPLIES / reverse: still unapplied (good)
#   cutover_compose.patch  target crlf=0   → APPLIES / reverse: still unapplied (good)
```

（顺带一提：`chat.py` 是 CRLF、`docker-compose.yaml` 是 LF。用默认 `write_text` 生成补丁
会被 Windows 悄悄改成 CRLF，于是对 LF 目标的补丁以"内容不匹配"为由失败——看起来像冲突，
其实是换行符。生成脚本里所有读写都钉死 `newline=""`。）

## 切流前必读：谁在消费这个接口

浏览器**不直接**调 `/agent/chat`：`web/sdk/cs-chat.js` 里没有这个调用，真正的消费者是
`python_business/business/chat/realtime.py`。它的解析器就是这个接口的规格：

| 分支 | 它读什么 | 读不到会怎样 |
| --- | --- | --- |
| JSON | `data["success"]` | false/缺失 → 直接对客户报 503「智能客服暂时不可用」 |
| JSON | `data["content"]` + **`data["answer_id"]`** | 二者一起落库；`answer_id` 为空**不报错**，但评分归因锚点静默失效 |
| SSE | `chunk` / `end` / `error` / `interrupt` | 其他 `type` 被跳过；没有 `end` 帧 → 这一轮永不标记完成 |

走 `chat.py` 的切流路径本身是安全的：`chat.py:64` 的外层包装对所有 JSON 分支统一注入
`result.answer_id`，SSE 分支（`:227`）也显式携带。但这个契约已由
`agent/tests/test_wire_contract.py` 钉住——若将来有人把 business 直接指到新入口，
缺 `success` / `answer_id` 会立刻测红，而不是悄悄产生归因脏数据。

## 切换后仍未被验证的部分

第 1–2 步做完、开关打开后，还需要**前端端到端**与**真实模型下的答复质量**两类实测。
前者需要你在授权范围内确认前端调用路径，后者阻塞在模型网关（见
`../实际效果报告.md` §8，10-28 01:40 第三次复探仍未变）。也就是说：切换动作本身是安全的（有兜底、有开关、有回退），
但"切换后效果一定更好"这句话今天还无法用数据支撑。

**还有一类已经不是"未验证"，而是"验出来是负的"**：Reviewer 的内容级覆盖面（§5f，漏放 9/25 与
配齐委派后 22/25）、重放幂等键（§5e M2）、回退后的上下文连续性（M3）、失败轮留半轮且原因不可见（M5b）、
跨进程并发丢轮（M10/M11）。这些都在离线账本里可一键复跑，不需要模型。改它们要么动 `ChatRequest`、
要么动 `graph.py` 的 `review()` 与 `four_agent.run_turn`——**都属于改 app 侧，未获授权，本目录只交证据与复跑入口**。
前两条加上失败轮那条已经有**候选补丁 + 临时副本上的实测**（`mounted_review_fix.patch` / `mounted_persistence_fix.patch` / `mounted_recovery_fix.patch`，
报告 §5h / §5i）：语料两遍 0 漏放 0 误拦、挂载图 12 个 case 两臂零回退零缺题，**修好 2**（M2b 重放、M5b 失败轮）。
M3 是**故意没出补丁**——要让旧路径看见 ON 期间的轮次，唯一直接的做法是新图每轮再写一次 `faq_bridge`，
而那正是阶段3 要取消的"图外检查点补写"（§5i 给了两条不违反它的替代）。M10/M11 是平台层性质，但**不再是没有人事先试过的**：`agent/` 侧已经上了按 thread key 的
Postgres advisory 锁，并实测六批跨进程并发（不加锁 80 轮只落 28 轮、加锁 80 轮零丢轮，报告 §4b，
`bash agent/evaluation/run_all.sh xproc` 可复跑）；把同一把锁接进 `app/services/four_agent.py` 属于改 app，未授权未动。
