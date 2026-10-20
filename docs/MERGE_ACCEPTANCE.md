# 客服 Agent 合并实测记录（2026-10-18）

## 当前交付范围

主项目是 `ai_customer_service`。用户入口沿用 `http://localhost:8080/chat/pc.html` → Nginx → Go `/ws/chat` / `/api/user/chat` → Python `/agent/chat` / `/agent/chat/stream`；消息与工单沿用 Go/MySQL，模型检查点沿用 PostgreSQL，租户知识库沿用 Qdrant。没有部署第二套 Streamlit/FastAPI/Milvus/SQLite 演示系统。

从 `support-agent` 迁入的是可审核的通用客服流程及回答边界。`python_agent/app/chat/faq.py` 对明确问法直接答复，标明 `process-playbook-v1 / generic-process` 来源，并要求商家政策与订单归属另行核验；`python_agent/assets/kb_seed/support-agent-process-v1.md` 经现有后台上传和手动索引进入 `kb_t_demo`。公开话术模板没有被当作商家政策导入。

## 阶段结果

| 阶段 | 验收 | 实测结果 |
|---|---|---|
| 保护基线 | 两仓状态及原有数据可恢复 | 来源仓未提交文件 126 个、主仓未跟踪 `.idea` 文件 7 个已快照到 `support-agent/runtime/merge-safeguard-20261018/`；MySQL 单事务导出 37,490 字节，SHA-256 `db5325472db1e4471231cebcabfcaf57fd32648fa7e74546d8acff1b77e13134`。原数据卷未重置。 |
| 统一入口/租户 | 客户页面 → Go → Agent；跨租户访问拒绝 | 页面在线；Go 请求给 Agent 带内部令牌和租户头，Agent 校验与 `thread_id` 前缀一致；以 `t_demo` 令牌读取 `t_admin` 历史返回 403；不带内部身份直接调用 Agent 聊天返回 403。 |
| FAQ | 页面问“怎么申请退货？” | 页面得到通用流程答复与来源提示；刷新后历史仍从 MySQL 出现。页面问“你可以做什么？”时明确说明真实订单查询、退款与支付尚未接通。Python 4 个新增单测通过。 |
| 知识库 | 主项目管理接口登记与索引 | `t_demo` 文档 `c111e8bd171b4bfe8abf54fd7ce3ca30` 完成索引，status=1、4 个分片；Qdrant `kb_t_demo` 实际 4 点，嵌入的 `tenant_id` 均为 `t_demo`。 |
| 订单安全框架 | 查询旧演示订单号 | 页面只答“真实订单接口和订单归属验证尚未接通，无法核验”；旧 `/api/business/order` 返回 410 且不含订单内容；内部订单接口无令牌返回 403。**真实订单状态查询未完成**。 |
| 转人工 | 页面按钮 → 工单 → 管理端 | 页面返回 `TK-e1bb1a7d`；管理端查询同租户工单 status=0，消息含用户请求与系统建单记录。按钮发送文本与界面文本已统一，不承诺等待时间。 |
| 质量门 | 构建/测试 | 最终 Go `go test ./...` 已通过；Python FAQ 单测 4/4，通过；Go、Agent、Nginx 镜像构建和容器启动通过；Compose 7 项服务均运行，带健康检查的 6 项均 healthy。 |

## 政策知识补录与检索链路修复（2026-10-18 14:30–14:55）

主项目原有知识库只有一份「通用流程」文档，运费、时效、发票、到账时间一类真实客服高频问题无知识可答。本次为演示租户 `t_demo` 补录示例政策文档，并在过程中发现并修复两个缺陷。

### 补录结果

| 项 | 操作 | 实测结果 |
|---|---|---|
| 文档登记 | 上传 `python_agent/assets/kb_seed/demo-store-policy-v1.md`，分类「店铺政策」 | `doc_id=8b0a0bf7d19d41b8b519b2c2a00fb54e`，status=0 待索引 |
| 索引 | 后台重索引 | status=1，5 个分片；`kb_t_demo` 向量数 4 → 9 |
| 检索命中 | 页面接口问「你们满多少包邮？」 | 回答给出 99 元包邮、未满收 8 元、偏远地区按实际计算 |
| 检索命中 | 「退款一般多久能到账？」 | 回答给出验收后 1 个工作日发起、银行卡 1–3 个工作日 |
| 检索命中 | 「下单后多久发货？」 | 回答给出 48 小时发货、24 小时可查揽收 |
| 未覆盖边界 | 「你们支持货到付款吗？」 | 明确回答知识库无相关说明，未编造政策 |
| 租户隔离 | 检查新增向量 payload | 5 个分片 `metadata.tenant_id` 均为 `t_demo` |

> 该文档为演示用示例政策，数值不代表任何真实商家承诺；真实店铺须提交自己审核发布的政策文档。

### 修复一：索引完成后知识库变为「暂未就绪」

- **现象**：重索引成功后立即提问，回复「知识库暂时无法访问」；Agent 日志中只有模型配置请求，**没有任何 Qdrant 查询**。
- **根因**：`kb_index_worker` 与 `kb_reindex` 在完成后清理 `tenant_retrievers` 缓存，而检索器只在 `get_tenant_agent` 内重建；Agent 实例已缓存并直接返回，导致检索器长期为空，必须重启服务才恢复。
- **修复**：`search_kb` 在检索器为空时按需重建（`ensure_tenant_vector` + `get_tenant_retriever`），失败仍按原逻辑降级提示。
- **验证**：重索引后再提问「退款一般多久能到账？」，日志出现 embedding 与 `points/query` 请求，回答正常命中政策。

### 修复二：向量删除过滤字段错误导致残留与重复

- **现象**：重复重索引后政策文档向量累积到 10 条、集合总数 14；删除文档后其向量仍留在库中可被检索。
- **根因**：Qdrant 中 payload 结构为 `{page_content, metadata:{doc_id,...}}`，但删除过滤使用 `key="doc_id"`，与嵌套字段 `metadata.doc_id` 不匹配，删除条件始终未命中。
- **修复**：`app/workers/kb_index.py`、`app/api/kb.py` 共 4 处（重索引前清理、删除文档、批量清理）改为 `key="metadata.doc_id"`。
- **验证**：重索引后集合总数由 14 回到 9（4 + 5，无重复）；上传临时文档并索引后删除，向量残留 0、总数回到 9。

## FAQ 意图匹配升级（2026-10-18 15:00 前后）

### 问题

原实现是 7 条精确字典匹配：措辞稍变（如「退货怎么申请」「我要退货」）即未命中，全部落到 LLM/RAG；转人工只认 8 个固定短语，「找人工」「人工客服」「我要投诉」均不识别。确定性答复层形同虚设。

### 改动

1. **意图规则匹配**：`support_faq.py` 改为「话题关键词组 + 问法关键词组」双条件意图判定（退货申请、退款申请、订单状态、能力边界四类），常见变体问法直接命中确定性答复。
2. **政策类问题刻意放行**：到账时间、包邮门槛、运费归属等问题不设流程意图，继续由租户知识库作答，不与政策抢答。
3. **转人工识别扩展**：覆盖「找人工」「人工客服」「转接」「投诉」等说法，并区分建单原因（`handoff_reason`：用户主动转人工 / 用户投诉）。
4. **意图优先级固定**：转人工 > 具体订单号 > 流程 FAQ > LLM/RAG。避免「订单 ORD-xxx 到哪了」被订单流程 FAQ 截胡而不走订单核验路径。
5. **单测从 4 项扩到 8 项**：变体命中、政策问题放行、转人工正/反例、投诉原因区分、订单号边界。
6. **订单号上下文扩展**：`order_id` 的上下文判定由「订单/order」扩到「订单/快递/物流/包裹」，避免「快递 ORD-xxx 到哪了」被流程 FAQ 反问「请提供订单号」（用户已提供单号）。无单号时仍走流程 FAQ。

### 实测（前端接口 /api/user/chat）

| 提问 | 路由 | 实测结果 |
|---|---|---|
| 我要退货 | 流程 FAQ（原 MISS） | 确定性答复 + 来源标识，未经 LLM |
| 帮我查一下我的快递 | 流程 FAQ（原 MISS） | 提示提供订单号、未接通真实订单系统 |
| 订单 ORD-2024-001 到哪了 | 订单核验路径 | 「真实订单接口和订单归属验证尚未接通，无法核验」，未被 FAQ 截胡 |
| 快递 ORD-2024-001 到哪了 | 订单核验路径 | 同上（订单号上下文扩展后命中，原先会被流程 FAQ 反问「请提供订单号」） |
| 我要投诉（干净会话） | 转人工直达 | 工单 `TK-c5131579`（status=0），用户消息与建单系统消息均落 MySQL |
| 退款多久到账 | 租户知识库 | 仍按政策作答（1 个工作日发起、银行卡 1–3 个工作日） |
| 怎么申请退货 | 流程 FAQ | 回归通过 |

回归：Python 单测 8/8 通过；Go `go test ./...` 通过。

已知边界（更新）：确定性答复（流程 FAQ / 订单核验 / 转人工）此前绕过 LangGraph、不进 PostgreSQL 检查点，导致后续 LLM 对话读不到这次问答。现已通过旁路图（`build_faq_bridge`，与主 Agent 共享同一 checkpointer 与 thread_id）把 (HumanMessage, AIMessage) 追加到检查点；写入为 best-effort，失败仅记日志不影响答复，工单未完结（`has_pending_ticket=True`）时跳过避免触碰中断态线程。`has_pending_ticket` 状态下转人工请求复用未完结工单、不再新建——此项保持不变。

流式输出内容泄漏修复（实测发现）：
- **根因一**：`search_kb` 工具内部又调了一次 `lookup_process_faq`，用 LLM 改写后的查询命中 FAQ，把带 `（来源：support-agent:process-playbook-v1...）` 标签的确定性答复当成检索结果返回给 LLM，LLM 原样吐出再补自己的话——前端看到的就是「FAQ 答复 + LLM 答复」混杂。修复：`search_kb` 只做向量检索，不再内嵌 FAQ 匹配（主入口 main.py 已在 LLM 之前拦截 FAQ）。
- **根因二**：流式过滤器只排除了 `HumanMessage`，没排除 `ToolMessage`——`search_kb` 的工具返回（原始 KB 文档文本）被当成 AI 回复 chunk 推给前端。修复：只推送 `AIMessage` 的内容。
- 实测对比：问「订单号怎么找」，修复前 chunks 含原始文档 + source 标签，修复后只输出 LLM 自然语言「您可以登录平台进入「我的订单」页面...」。

实测（走前端 `/api/user/chat`）：

| 路径 | 提问 | 检查点 | 后续 LLM 能否回忆 |
|---|---|---|---|
| 流程 FAQ | 我要退货（thread `t_demo:faqtest1`） | 写入 3 个 checkpoint | 跟进「你刚说核验订单归属和商品情况，再说一遍」→ LLM 引用上一轮 FAQ 答复的措辞作答 |
| 订单核验 | 订单 ORD-2024-001 到哪了（thread `t_demo:faqtest2`） | 写入 3 个 checkpoint | 跟进「你说无法核验，现在怎么处理」→ LLM 据此升级建单 `TK-302c670f` |
| 转人工 | 我要投诉（thread `t_demo:faqtest3`） | 写入 3 个 checkpoint | 工单 `TK-8c9a7af4` 正常建单 |

## 订单服务四项优化（2026-10-18 16:15 前后）

### 优化一：订单号格式扩展

原先只识别 `ORD-xxx` 演示格式，演示时必须给 ORD 开头的假单号才触发订单核验路径，不真实。扩展为：
- **演示格式** `ORD-2024-001`（要求订单/快递/物流/包裹上下文，避免误匹配）
- **电商前缀** `TB2024xxx` / `JD2024xxx` / `PDDxxx` / `DYxxx` / `TMALLxxx` / `1688xxx`（识别性强，不要求上下文）
- **显式上下文**「订单号是 xxx」「单号: xxx」「订单编号 xxx」（空格/冒号/等为分隔，可识别无前缀纯数字/字母数字组合）

纯短数字（如"30 分钟"）与手机号（11 位）不会误匹配。

### 优化二：「订单号怎么找」FAQ 化

高频问题「订单号怎么找/在哪/咋查」原先每次走 LLM，答法不一、浪费 token。现做成确定性 FAQ（分点列出 4 个查找位置：购买记录 / 收货短信 / 电子发票 / 支付凭证），答复末尾引导用户提供订单号后核验。`find_order_id` 意图组 A 约束话题（订单号/单号/订单编号），组 B 约束问法（怎么找/在哪/咋查等），避免与订单状态查询混淆。

### 优化三：订单核验失败自动转人工（2026-10-19 已收窄，见文末「订单查询失败分级」）

原先核验失败返回「请转人工客服」文本，用户得自己再发「转人工」才建单，闭环断层。当时改为检测 `query_order_readonly` 返回值含失败标识（无法核验/未通过核验/暂不可用等）时自动触发 interrupt 建单，`reason` 标注「订单核验失败（订单号 xxx），转人工处理」，`direct_handoff=True`。工单未完结（`has_pending_ticket=True`）时不重复建单。

> 后续发现该判定按**用户文案子串**匹配，把「单号打错（404）」也升级成人工，用户输入 `订单10001` 即静默建单。现已改为结构化结果码分级，详见文末 2026-10-19 章节。

### 优化四：历史订单上下文记忆

用户先给订单号后追问「刚才那个订单现在怎样」「之前那个快递到哪了」时，原先走 LLM，可能答非所问。现 `support_faq.has_order_reference` 检测「指代词（刚才/那个/之前/上次）+ 订单/快递上下文」，命中时 `_last_order_id_from_history` 从检查点历史消息逆序找最近一条用户消息里的订单号复用。读取为 best-effort，失败回退到 LLM 路径。

### 实测（前端接口 /api/user/chat）

| 提问 | 路径 | 结果 |
|---|---|---|
| 订单号怎么找 | find_order_id FAQ | 分点列出 4 个查找位置 + 引导提供订单号 |
| 帮我查 TB202410180001 | 订单核验（电商前缀识别） | 自动转人工，工单 reason 含订单号 |
| 订单 ORD-2024-001 到哪了 | 订单核验（失败自动转人工） | `is_human_wait=true` + 工单 reason「订单核验失败（订单号 ORD-2024-001），转人工处理」 |
| （已建单后）刚才那个订单现在怎样 | 指代回溯 + 订单核验 | 从历史取 ORD-2024-001，调 query_order 返回「真实订单接口...尚未接通」（has_pending_ticket=True 不重复建单） |

单测：`test_order_id_only_with_order_context` 覆盖演示格式 + 电商前缀 + 显式上下文 + 误匹配负例（短数字/手机号）；`test_find_order_id_answer_is_structured_and_honest` 覆盖 find_order_id 答复结构。12/12 通过。

## 订单查询真实查询与归属校验（2026-10-18 17:00 前后）

### 改动

此前 `BusinessOrder` 对所有订单 ID fail-closed 返回 503，因为 `orders` 表没有租户/客户归属字段，无法做归属校验。现改为真实查询 + 归属校验：

| 步骤 | 内容 |
|---|---|
| 1 | `orders` 表加 `tenant_id` + `customer_id` 字段（ALTER TABLE ADD COLUMN，不删数据）+ 索引 |
| 2 | 灌 5 条带归属的演示订单：ORD-2024-001/002（更新现有行的乱码内容为可读中文）+ ORD-2024-003 + TB202410180001 + JD20241018001，覆盖待发货/已发货/已完成/已取消四种状态 |
| 3 | `model.Order` 加 `TenantID` + `CustomerID` 字段 |
| 4 | `BusinessOrder` 从"校验通过即 503"改为"校验通过 → 查表 → 比对 `order.customer_id == thread_id` 且 `order.tenant_id == tenant_id` → 命中返回状态，不命中 fail-closed" |
| 5 | Python `query_order_readonly` 去掉 `raise_for_status()`（Go 的 StatusCode 中间件会把 body.code 映射到 HTTP status，HTTP 4xx 是业务响应不是请求失败），改为按 body.code 分支返回结构化文本 |

### 关键发现：Go StatusCode 中间件

Go 有个 `middleware.StatusCode()` 中间件，会把 response body 里的 `code` 字段映射到 HTTP status（body `{"code":403}` → HTTP 403）。所以 Go 代码写 `c.JSON(http.StatusOK, gin.H{"code":403,...})` 实际返回 HTTP 403。Python 原先用 `raise_for_status()` 把这当异常，进了 except 分支返回"订单系统暂时不可用"。修复后改为按 body.code 判断，仅 HTTP 5xx 视为系统不可用。

### 实测（前端 /api/user/chat，全新会话）

| 提问 | 结果 |
|---|---|
| 订单 ORD-2024-001 到哪了（buyer1 本人） | 「订单 ORD-2024-001 状态：已发货；商品：VIP会员套餐；金额：799；下单日期：2024-01-15」 |
| 订单 ORD-2024-002 状态（buyer1 查他人订单） | 自动转人工，工单 reason「订单核验失败（订单号 ORD-2024-002），转人工处理」 |
| 订单 ORD-9999 怎样（不存在） | 自动转人工，工单 reason「订单核验失败（订单号 ORD-9999），转人工处理」 |
| 帮我查 TB202410180001（buyer1 电商前缀本人） | 「订单 TB202410180001 状态：已发货；商品：电商前缀格式测试订单；金额：159」 |
| 订单 JD20241018001 状态（buyer2 京东本人） | 「订单 JD20241018001 状态：已取消；商品：京东格式测试订单；金额：89」 |

### 诚实边界

- 演示订单数据已明确标注为演示用，不构成真实商家业务答复
- 订单查询能力本身是真的（查真实 MySQL 表 + 归属校验），但数据是演示数据
- 不接客户级身份，仍用现有 `user_id` 字符串做归属比对——演示时用固定 `user_id`，同租户内冒用风险存在但演示不触发
- 真实商家接入时，只需把演示订单换成真实订单数据（或接业务 API），归属校验逻辑不变

## 尚未完成的产品边界

1. **真实订单数据**：`orders` 表现为演示数据（5 条，覆盖四种状态）。订单查询与归属校验逻辑已就绪，真实商家接入时只需把演示数据换成真实订单数据或接业务 API。`ORDER_ADAPTER_CONTRACT.md` 已备好验收用例。
2. **客户级会话身份**：现有用户令牌证明租户，不绑定具体访客 `user_id`；现有 SDK/页面从浏览器传 `user_id`。跨租户校验已通过，订单归属校验基于 `user_id` 字符串比对（`thread_id == customer_id`），同租户内冒用 `user_id` 可读他人订单——演示不触发，真实业务接入前需可信宿主签发绑定访客身份的会话令牌。该项卡在外部业务身份映射，非代码优化可独立完成。
3. **退款与支付**：主项目当前 Agent 只有知识库搜索、订单只读查询、转人工三个工具，没有接入来源项目的 SQLite 演示写入工具。订单归属、真实业务接口和独立审批接通前保持关闭。
4. **政策知识**：`t_demo` 现已导入示例政策文档，资格、时效、运费、到账时间可被检索并带引用作答；但该文档是演示数据，不构成真实商家承诺。真实店铺仍需按租户提交并审核自己的政策文档，逐条验证检索证据后才能对外答复。

## 可复现命令

```text
docker compose ps
docker compose exec -T agent python -m unittest test_support_faq.py -v
docker build --target builder -t cs-go-merge-test -f go_service/Dockerfile .
docker run --rm cs-go-merge-test go test ./...
```

用户页面：`http://localhost:8080/chat/pc.html`。演示账号仅供本地测试，生产不可把 `app_secret` 放进页面 URL。





真实订单后续接入条件见 [ORDER_ADAPTER_CONTRACT.md](./ORDER_ADAPTER_CONTRACT.md)。

## 订单查询失败分级（2026-10-19）

### 问题

用户输入「订单10001」（演示库里没有该单号）时被自动建单（`TK-…`）。原因不是意图识别出错，而是：

1. `order_id()` 的内联规则把「订单+10001」识别为订单号，路由到 `order`；
2. 订单接口返回 404，`query_order_readonly` 输出「未找到订单 10001…请转人工客服。」；
3. `api/chat.py` 用 `_ORDER_LOOKUP_FAILURE_MARKERS` 对**用户文案**做子串匹配，命中「未找到订单」即返回 `is_interrupt=True`；
4. Go 收到中断后 `EnsureTicket()` 建单，前端提示「已为您创建工单」。

即：单号打错属于用户可自助纠正的失败，却被当成必须人工的失败；判定还绑在文案上，改措辞就会改流程。

### 改动

| 层 | 文件 | 内容 |
|---|---|---|
| 领域规则 | `app/chat/order.py` | 新增结果码 `ORDER_OK/NOT_FOUND/UNAUTHENTICATED/FORBIDDEN/UNAVAILABLE/UNVERIFIED`、`AUTO_HANDOFF_ORDER_CODES`、`should_auto_handoff()`、`order_handoff_reason()`；删除 `_ORDER_LOOKUP_FAILURE_MARKERS` |
| 适配器 | `app/agent/tools.py` | `lookup_order()` 返回 `OrderLookup(code, text)`；`query_order_readonly()` 仅取 text 供 LLM 工具兼容；404/401 文案去掉「请转人工客服」 |
| 路由 | `app/api/chat.py`（HTTP + SSE 双入口） | 仅 `should_auto_handoff(code)` 为真时建单；404 返回核对提示；`pending_ticket` 时补充「客服接入后会继续核实」 |
| 网关 | `go_service/internal/pkg/chat/chat.go`、`handler/ws.go`、`handler/user.go` | `ChatResult.Reason` 透传；WS `ticket` 事件与 HTTP `ticket` 响应新增 `reason` 字段 |
| 前端 | `web/chat/pc.html`、`web/chat/h5.html` | 工单提示追加「（原因：…）」，`cs_handoff` 回传 `reason` |

### 失败分级

| 场景 | 结果码 | 是否转人工 | 用户看到 |
|---|---|---|---|
| 本人订单 | `OK` | 否 | 订单状态、商品、金额、下单日期 |
| 单号不存在 / 不属于该账号（Go 合并返回 404） | `NOT_FOUND` | 否 | 「没有查到订单 xxx。请核对订单号后重新发送；如果确认单号无误，可以回复「转人工」…」 |
| 访客未登录（401） | `UNAUTHENTICATED` | 否 | 提示登录客户账号，其他问题可继续免登录咨询 |
| 归属校验不通过（403） | `FORBIDDEN` | 是 | 工单提示 + 原因「订单 xxx 归属校验未通过，需要人工核实」 |
| 订单系统 5xx / 超时 | `UNAVAILABLE` | 是 | 工单提示 + 原因「订单系统暂时不可用，无法核验订单 xxx」 |
| 内部 Token / 租户上下文缺失 | `UNVERIFIED` | 是 | 工单提示 + 原因「订单身份上下文缺失，无法核验订单 xxx」 |

单测：`tests/test_order_lookup_code.py`（11 项：结果码映射、自助/转人工分级、原因文案、文本包装一致性），全量 48 项通过。
