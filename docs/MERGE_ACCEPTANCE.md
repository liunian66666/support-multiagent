# 客服 Agent 合并实测记录（2026-10-18）

## 当前交付范围

主项目是 `ai_customer_service`。用户入口沿用 `http://localhost:8080/chat/pc.html` → Nginx → Go `/ws/chat` / `/api/user/chat` → Python `/agent/chat` / `/agent/chat/stream`；消息与工单沿用 Go/MySQL，模型检查点沿用 PostgreSQL，租户知识库沿用 Qdrant。没有部署第二套 Streamlit/FastAPI/Milvus/SQLite 演示系统。

从 `support-agent` 迁入的是可审核的通用客服流程及回答边界。`python_agent/app/chat/faq.py` 对明确问法直接答复，标明 `process-playbook-v1 / generic-process` 来源，并要求商家政策与订单归属另行核验；`python_agent/assets/kb_seed/support-agent-process-v1.md` 经现有后台上传和手动索引进入 `kb_t_demo`。公开话术模板没有被当作商家政策导入。

## 阶段结果

| 阶段      | 验收                        | 实测结果                                                                                                                                                                                                |
| ------- | ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 保护基线    | 两仓状态及原有数据可恢复              | 来源仓未提交文件 126 个、主仓未跟踪 `.idea` 文件 7 个已快照到 `support-agent/runtime/merge-safeguard-20261018/`；MySQL 单事务导出 37,490 字节，SHA-256 `db5325472db1e4471231cebcabfcaf57fd32648fa7e74546d8acff1b77e13134`。原数据卷未重置。 |
| 统一入口/租户 | 客户页面 → Go → Agent；跨租户访问拒绝 | 页面在线；Go 请求给 Agent 带内部令牌和租户头，Agent 校验与 `thread_id` 前缀一致；以 `t_demo` 令牌读取 `t_admin` 历史返回 403；不带内部身份直接调用 Agent 聊天返回 403。                                                                                |
| FAQ     | 页面问“怎么申请退货？”              | 页面得到通用流程答复与来源提示；刷新后历史仍从 MySQL 出现。<u>页面问“你可以做什么？”时明确说明真实订单查询、退款与支付尚未接通</u>。Python 4 个新增单测通过。                                                                                                         |
| 知识库     | 主项目管理接口登记与索引              | `t_demo` 文档 `c111e8bd171b4bfe8abf54fd7ce3ca30` 完成索引，status=1、4 个分片；Qdrant `kb_t_demo` 实际 4 点，嵌入的 `tenant_id` 均为 `t_demo`。                                                                           |
| 订单安全框架  | 查询旧演示订单号                  | 页面只答“真实订单接口和订单归属验证尚未接通，无法核验”；旧 `/api/business/order` 返回 410 且不含订单内容；内部订单接口无令牌返回 403。**真实订单状态查询未完成**。                                                                                                |
| 转人工     | 页面按钮 → 工单 → 管理端           | 页面返回 `TK-e1bb1a7d`；管理端查询同租户工单 status=0，消息含用户请求与系统建单记录。按钮发送文本与界面文本已统一，不承诺等待时间。                                                                                                                       |
| 质量门     | 构建/测试                     | 最终 Go `go test ./...` 已通过；Python FAQ 单测 4/4，通过；Go、Agent、Nginx 镜像构建和容器启动通过；Compose 7 项服务均运行，带健康检查的 6 项均 healthy。                                                                                     |

## 政策知识补录与检索链路修复（2026-10-18 14:30–14:55）

主项目原有知识库只有一份「通用流程」文档，运费、时效、发票、到账时间一类真实客服高频问题无知识可答。本次为演示租户 `t_demo` 补录示例政策文档，并在过程中发现并修复两个缺陷。

### 补录结果

| 项     | 操作                                                                | 实测结果                                                   |
| ----- | ----------------------------------------------------------------- | ------------------------------------------------------ |
| 文档登记  | 上传 `python_agent/assets/kb_seed/demo-store-policy-v1.md`，分类「店铺政策」 | `doc_id=8b0a0bf7d19d41b8b519b2c2a00fb54e`，status=0 待索引 |
| 索引    | 后台重索引                                                             | status=1，5 个分片；`kb_t_demo` 向量数 4 → 9                   |
| 检索命中  | 页面接口问「你们满多少包邮？」                                                   | 回答给出 99 元包邮、未满收 8 元、偏远地区按实际计算                          |
| 检索命中  | 「退款一般多久能到账？」                                                      | 回答给出验收后 1 个工作日发起、银行卡 1–3 个工作日                          |
| 检索命中  | 「下单后多久发货？」                                                        | 回答给出 48 小时发货、24 小时可查揽收                                 |
| 未覆盖边界 | 「你们支持货到付款吗？」                                                      | 明确回答知识库无相关说明，未编造政策                                     |
| 租户隔离  | 检查新增向量 payload                                                    | 5 个分片 `metadata.tenant_id` 均为 `t_demo`                 |

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

| 提问                  | 路由             | 实测结果                                           |
| ------------------- | -------------- | ---------------------------------------------- |
| 我要退货                | 流程 FAQ（原 MISS） | 确定性答复 + 来源标识，未经 LLM                            |
| 帮我查一下我的快递           | 流程 FAQ（原 MISS） | 提示提供订单号、未接通真实订单系统                              |
| 订单 ORD-2024-001 到哪了 | 订单核验路径         | 「真实订单接口和订单归属验证尚未接通，无法核验」，未被 FAQ 截胡             |
| 快递 ORD-2024-001 到哪了 | 订单核验路径         | 同上（订单号上下文扩展后命中，原先会被流程 FAQ 反问「请提供订单号」）          |
| 我要投诉（干净会话）          | 转人工直达          | 工单 `TK-c5131579`（status=0），用户消息与建单系统消息均落 MySQL |
| 退款多久到账              | 租户知识库          | 仍按政策作答（1 个工作日发起、银行卡 1–3 个工作日）                  |
| 怎么申请退货              | 流程 FAQ         | 回归通过                                           |

回归：Python 单测 8/8 通过；Go `go test ./...` 通过。

已知边界（更新）：确定性答复（流程 FAQ / 订单核验 / 转人工）此前绕过 LangGraph、不进 PostgreSQL 检查点，导致后续 LLM 对话读不到这次问答。现已通过旁路图（`build_faq_bridge`，与主 Agent 共享同一 checkpointer 与 thread_id）把 (HumanMessage, AIMessage) 追加到检查点；写入为 best-effort，失败仅记日志不影响答复，工单未完结（`has_pending_ticket=True`）时跳过避免触碰中断态线程。`has_pending_ticket` 状态下转人工请求复用未完结工单、不再新建——此项保持不变。

流式输出内容泄漏修复（实测发现）：

- **根因一**：`search_kb` 工具内部又调了一次 `lookup_process_faq`，用 LLM 改写后的查询命中 FAQ，把带 `（来源：support-agent:process-playbook-v1...）` 标签的确定性答复当成检索结果返回给 LLM，LLM 原样吐出再补自己的话——前端看到的就是「FAQ 答复 + LLM 答复」混杂。修复：`search_kb` 只做向量检索，不再内嵌 FAQ 匹配（主入口 main.py 已在 LLM 之前拦截 FAQ）。
- **根因二**：流式过滤器只排除了 `HumanMessage`，没排除 `ToolMessage`——`search_kb` 的工具返回（原始 KB 文档文本）被当成 AI 回复 chunk 推给前端。修复：只推送 `AIMessage` 的内容。
- 实测对比：问「订单号怎么找」，修复前 chunks 含原始文档 + source 标签，修复后只输出 LLM 自然语言「您可以登录平台进入「我的订单」页面...」。

实测（走前端 `/api/user/chat`）：

| 路径     | 提问                                            | 检查点               | 后续 LLM 能否回忆                                    |
| ------ | --------------------------------------------- | ----------------- | ---------------------------------------------- |
| 流程 FAQ | 我要退货（thread `t_demo:faqtest1`）                | 写入 3 个 checkpoint | 跟进「你刚说核验订单归属和商品情况，再说一遍」→ LLM 引用上一轮 FAQ 答复的措辞作答 |
| 订单核验   | 订单 ORD-2024-001 到哪了（thread `t_demo:faqtest2`） | 写入 3 个 checkpoint | 跟进「你说无法核验，现在怎么处理」→ LLM 据此升级建单 `TK-302c670f`    |
| 转人工    | 我要投诉（thread `t_demo:faqtest3`）                | 写入 3 个 checkpoint | 工单 `TK-8c9a7af4` 正常建单                          |

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

| 提问                  | 路径                | 结果                                                                               |
| ------------------- | ----------------- | -------------------------------------------------------------------------------- |
| 订单号怎么找              | find_order_id FAQ | 分点列出 4 个查找位置 + 引导提供订单号                                                           |
| 帮我查 TB202410180001  | 订单核验（电商前缀识别）      | 自动转人工，工单 reason 含订单号                                                             |
| 订单 ORD-2024-001 到哪了 | 订单核验（失败自动转人工）     | `is_human_wait=true` + 工单 reason「订单核验失败（订单号 ORD-2024-001），转人工处理」                 |
| （已建单后）刚才那个订单现在怎样    | 指代回溯 + 订单核验       | 从历史取 ORD-2024-001，调 query_order 返回「真实订单接口...尚未接通」（has_pending_ticket=True 不重复建单） |

单测：`test_order_id_only_with_order_context` 覆盖演示格式 + 电商前缀 + 显式上下文 + 误匹配负例（短数字/手机号）；`test_find_order_id_answer_is_structured_and_honest` 覆盖 find_order_id 答复结构。12/12 通过。

## 订单查询真实查询与归属校验（2026-10-18 17:00 前后）

### 改动

此前 `BusinessOrder` 对所有订单 ID fail-closed 返回 503，因为 `orders` 表没有租户/客户归属字段，无法做归属校验。现改为真实查询 + 归属校验：

| 步骤 | 内容                                                                                                                                                  |
| -- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1  | `orders` 表加 `tenant_id` + `customer_id` 字段（ALTER TABLE ADD COLUMN，不删数据）+ 索引                                                                         |
| 2  | 灌 5 条带归属的演示订单：ORD-2024-001/002（更新现有行的乱码内容为可读中文）+ ORD-2024-003 + TB202410180001 + JD20241018001，覆盖待发货/已发货/已完成/已取消四种状态                                |
| 3  | `model.Order` 加 `TenantID` + `CustomerID` 字段                                                                                                        |
| 4  | `BusinessOrder` 从"校验通过即 503"改为"校验通过 → 查表 → 比对 `order.customer_id == thread_id` 且 `order.tenant_id == tenant_id` → 命中返回状态，不命中 fail-closed"           |
| 5  | Python `query_order_readonly` 去掉 `raise_for_status()`（Go 的 StatusCode 中间件会把 body.code 映射到 HTTP status，HTTP 4xx 是业务响应不是请求失败），改为按 body.code 分支返回结构化文本 |

### 关键发现：Go StatusCode 中间件

Go 有个 `middleware.StatusCode()` 中间件，会把 response body 里的 `code` 字段映射到 HTTP status（body `{"code":403}` → HTTP 403）。所以 Go 代码写 `c.JSON(http.StatusOK, gin.H{"code":403,...})` 实际返回 HTTP 403。Python 原先用 `raise_for_status()` 把这当异常，进了 except 分支返回"订单系统暂时不可用"。修复后改为按 body.code 判断，仅 HTTP 5xx 视为系统不可用。

### 实测（前端 /api/user/chat，全新会话）

| 提问                                | 结果                                                         |
| --------------------------------- | ---------------------------------------------------------- |
| 订单 ORD-2024-001 到哪了（buyer1 本人）    | 「订单 ORD-2024-001 状态：已发货；商品：VIP会员套餐；金额：799；下单日期：2024-01-15」 |
| 订单 ORD-2024-002 状态（buyer1 查他人订单）  | 自动转人工，工单 reason「订单核验失败（订单号 ORD-2024-002），转人工处理」            |
| 订单 ORD-9999 怎样（不存在）               | 自动转人工，工单 reason「订单核验失败（订单号 ORD-9999），转人工处理」                |
| 帮我查 TB202410180001（buyer1 电商前缀本人） | 「订单 TB202410180001 状态：已发货；商品：电商前缀格式测试订单；金额：159」            |
| 订单 JD20241018001 状态（buyer2 京东本人）  | 「订单 JD20241018001 状态：已取消；商品：京东格式测试订单；金额：89」                |

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

真实订单后续接入条件见 [ORDER\_ADAPTER\_CONTRACT.md](./ORDER_ADAPTER_CONTRACT.md)。

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

---

## 阶段 1 验收：客户身份／会话与订单入口由 FastAPI 接管（2026-10-20）

分支 `codex/customer-order-fastapi`。本轮范围＝**只做客户侧身份与会话入口**，Vue 商家后台、客户聊天页外观、Python Agent 编排结构均不改；退款/支付保持关闭。

### 设计约束

| 约束 | 落地方式 |
|---|---|
| 不改前端形态 | `CONFIG.authBase = '/py-api'`，只在认证/会话类请求上换后端 |
| 不可用可回退 | `authFetch()` 依次尝试 `/py-api` → 旧入口；仅 404 / 5xx / 网络异常回退，401/403 **不回退**（避免已失效令牌被旧入口再认一次） |
| 回退不产生两套身份 | 两端令牌格式一致（`base64url(claims).base64url(HMAC(secret_hash))`，`kind=customer`），会话同表 `customer_sessions` |
| 会话失效要能自愈 | 恢复会话前先 `GET /py-api/api/customer/session` 校验；401 清本地并重建，5xx/网络异常保留本地会话 |
| 内部适配器不对外 | nginx 把 `/py-api/api/agent/*` 挡成 404（Agent 侧仍可经内网直连） |

### 改动清单

| 层 | 文件 | 内容 |
|---|---|---|
| 新业务层 | `python_business/main.py`（362 行 / 10 路由） | `customer-token`／`guest-token`／`merchant-preview-token`／`logout`／`customer/session`／`customer/orders[/{id}]`／`agent/orders`／`agent/order`；登录限流用 Redis Lua 原子 INCR+EXPIRE |
| 网关 | `deploy/nginx.conf` | 新增 `/py-api/` 反向代理；屏蔽 `/py-api/api/agent/*` |
| 编排 | `docker-compose.yaml` | 新增 `business` 服务并加入 `depends_on` |
| 前端 | `web/chat/pc.html`、`web/chat/h5.html`、`web/chat/login.html` | `authBase` / `authFetch()` / `validateCustomerSession()`；认证与登出改走新层 |
| 文档 | `web/sdk/cs-chat.js`、`web/sdk/demo-codes.js` | 示例地址改为 `/py-api/api/auth/customer-token`（仅注释，SDK 仍由宿主签发令牌） |
| 测试 | `python_business/tests/test_contract.py`（7 项） | 客户/访客/预览令牌边界、归属隔离、越权不可区分、登出吊销、内部适配器双重校验 |

### 证据一：测试通过

```
python_business  docker compose exec business python -m unittest discover -s tests -v
                 Ran 7 tests ... OK
python_agent     docker compose exec agent python -m unittest discover -s tests -t .
                 Ran 66 tests ... OK
```

### 证据二：接口实测

| 场景 | 实测 |
|---|---|
| FastAPI 签发的访客令牌调 Go `/api/user/chat` | 200（两端互认） |
| Go 签发的令牌调 FastAPI `/api/customer/session` | 200 |
| 过期令牌（`exp = now-120`，正确签名） | FastAPI 401 / Go 401 |
| 登出后原令牌 | FastAPI 401 / Go 401 |
| `/py-api/api/agent/orders`、`/py-api/api/agent/order` 对外 | 404 |
| `/py-api/health` | 200 |

### 证据三：浏览器实测（系统 Chrome + CDP，无 Playwright）

`13 项`主链路（访客／商家预览／买家三身份 + 重启恢复 + 登出失效）：

```
PASS  访客身份建立            customer=t_demo:guest_…
PASS  访客免登录 FAQ          命中退货流程快答
PASS  访客查个人订单被拒      未返回任何订单状态
PASS  预览链接不再暴露 app_secret
PASS  预览会话已建立
PASS  商家预览不暴露订单
PASS  买家登录建立会话        t_demo:buyer1
PASS  登录后 URL 不含密码
PASS  买家查询本人订单        列表含 ORD-2024-001/003
PASS  重启后会话令牌保留
PASS  重启后历史会话恢复
PASS  登出后 FastAPI 拒绝该令牌   session=401
PASS  登出后 Go 旧入口拒绝该令牌  go=401
total=13 failed=0
```

`12 项`补充链路（h5 页 / 会话过期自愈 / SDK iframe 嵌入）：

```
PASS  h5 访客身份建立
PASS  h5 访客令牌双端互认            fastapi=200 go=200
PASS  h5 免登录 FAQ
PASS  过期令牌双端 401               fastapi=401 go=401
PASS  过期令牌被前端自动清除并重建身份 rotated=true
PASS  过期自愈后问答可用
PASS  SDK 宿主取得客户令牌            status=200
PASS  SDK 初始化并携带令牌            version=2.1.0
PASS  iframe 地址不含令牌             src 无 token 参数
PASS  SDK iframe 连接建立
PASS  SDK iframe 以客户身份查询本人订单  列表含本人订单号
PASS  SDK iframe 身份来自宿主令牌        未被当作访客拦截
total=12 failed=0
```

### 本轮发现并修复的缺陷：快答链路无 chunk 导致助手气泡空白

- **现象**：h5 页发出消息后，助手气泡一直空白；服务端日志显示 `/agent/chat/stream` 已 200 且 `specialist=faq_fast`。
- **抓包**：WS 只收到一帧 `{"type":"chat_end","msg":"<整段答复>"}`，**没有 `chat_chunk`**。
- **根因（两层）**：
  1. `h5.html` 的 `handleMsg` 只处理 `chat_reply`，从不处理 `chat_chunk`／`chat_end`——流式改造后 h5 未被同步；
  2. 更要紧的一层：即使补上分支，`endStreaming()` 只在 `streamingEl` 存在时才渲染。快答/缓存命中时一个 chunk 都没有，`streamingEl` 为空，整段 `chat_end` 被静默丢弃。**pc.html 有同样隐患**，此前能通过只是因为那一轮恰好走了有 chunk 的路径。
- **修复**：`web/chat/pc.html`、`web/chat/h5.html` 的 `endStreaming()` 增加无 chunk 分支——直接用整段文本落一条完整消息；h5 另补 `chat_chunk`／`chat_end` 分支与 `cancelStreaming()`，并在 `ticket`／`human_notice`／`human_reply`／`notice`／`error` 分支清理流式元素。静态资源在 nginx 镜像内，需 `docker compose build nginx && docker compose up -d nginx` 生效。

### 待办（如实列出，均未完成）

| 项 | 说明 |
|---|---|
| `python_business/` 未入 git | 目前仍是未跟踪目录，需确认后再 `git add` |
| pymysql 无连接池 | 每请求新建连接，压测下会打满 MySQL `max_connections` |
| `/py-api/docs` 公网可访问 | 实测 200，应关闭或加 IP 白名单 |
| 金额格式 | `799` 渲染为 `799.0` |
| Go / Python 双份订单实现并存 | 阶段 2 迁移完成后才能删 |
| `docs/COMPOUND_TASKS.md` | 内容已过期，待重写 |
| 商家预览未验证 `is_preview` 会话过期路径 | 只验证了访客与买家 |

---

## 阶段 2 验收：聊天记录 / 评价 / 工单状态（2026-10-20）

阶段 1 已完成「身份／会话 + 订单」，本轮接续迁移客户侧剩余 HTTP 能力。

### 迁移盘点（客户侧公开端点）

| Go 端点 | 处理 | 说明 |
|---|---|---|
| `GET /api/user/messages` | 迁移 → `GET /api/customer/messages` | 返回字段与旧接口对齐，可直接替换 |
| `POST /api/user/rating` | 迁移 → `POST /api/customer/rating` | 同会话 upsert，覆盖语义保持一致 |
| `POST /api/user/chat` | 不迁 | 走 Agent 编排，不在本阶段范围 |
| `POST /api/user/upload` | 不迁 | 依赖 `cs_msg_files` 共享卷与消息落库，与聊天写链路耦合 |
| `GET /ws/chat` | 不迁 | 实时通道由 Go 持有 |
| —— | 新增 `GET /api/customer/ticket` | 客户侧原本无法查询自己的工单状态，属新增只读能力 |

### 改动清单

| 层 | 文件 | 内容 |
|---|---|---|
| 业务层 | `python_business/main.py` | 新增 `customer_messages()` / `upsert_rating()` / `customer_ticket()` 与三个路由；`MESSAGE_PAGE_MAX = 500`；`TICKET_STATUS` 状态映射 |
| 前端 | `web/chat/pc.html`、`web/chat/h5.html` | 评价改走 `authFetch('/api/customer/rating')`；新增 `refreshTicketNotice()` 在 WS `history` 后回显进行中工单 |
| 测试 | `python_business/tests/test_stage2_contract.py`（10 项） | 会话作用域、客户端伪造 thread 无效、分页上限与顺序、访客/预览可读自己会话、评价校验与 upsert、截断、工单映射 |

### 关键实现约束

- **thread_id 只取自服务端会话**：三个端点都不接受 `thread_id` 参数；测试专门断言「请求里塞 `thread_id`/`tenant_id` 也不影响 SQL 作用域」。
- **聊天记录分页**：Go 一次性返回全部消息；新接口默认最近 200 条、上限 500，`before_id` 往前翻，返回按 id 正序。
- **工单只读**：只查 `deleted_at = 0` 且属于当前会话的最近一张，不提供任何写操作，建单仍由 Agent 中断 + Go 完成。

### 证据一：测试通过

```
python_business  docker compose exec business python -m unittest discover -s tests
                 Ran 17 tests ... OK      # 阶段1 7 项 + 阶段2 10 项
```

### 证据二：接口实测

| 场景 | 实测 |
|---|---|
| 同一会话，FastAPI 与 Go 返回的消息 id 序列 | 完全一致（`[402, 403]`） |
| 返回字段集 | 与 Go 对齐：`id/thread_id/role/content/msg_type/tenant_id/ticket_id/is_read/created_at` |
| `POST /py-api/api/customer/rating` | `{"code":0,"msg":"评价成功"}` |
| `POST /api/user/rating`（同令牌同参数） | `{"code":0,"msg":"评价成功"}` |
| 评分越界（`score=9`） | `400 评分需为 1-5 星` |
| `GET /py-api/api/customer/ticket`（无工单） | `{"code":0,"data":null}` |

### 证据三：浏览器实测

新增 9 项（`cs_browser_accept3.mjs`，同时用 CDP `Network` 事件抓真实请求）：

```
PASS  买家登录并连上 WS              t_demo:buyer1
PASS  转人工建单                    TK-b75281ed
PASS  刷新后回显工单状态             提示含工单号 TK-b75281ed
PASS  工单状态请求走 FastAPI         GET /py-api/api/customer/ticket
PASS  工单状态未请求 Go 旧接口
PASS  评价提交成功                  出现「感谢您的评价」
PASS  评价请求走 FastAPI            POST /py-api/api/customer/rating
PASS  评价未请求 Go 旧接口           无 /api/user/rating 调用
PASS  聊天记录与 Go 旧接口一致        fastapi=2 条，go=2 条，序列一致=true
```

回归（阶段 1 两套脚本）：主链路 13/13、补充链路 12/12，全部通过。累计浏览器断言 34 项。

### 未完成 / 已知问题

| 项 | 说明 |
|---|---|
| ~~新层不可用→回退旧入口的浏览器实测~~ | **已补齐**（见下节「回退判定修正」）：16 项浏览器断言覆盖 6 种新层应答形态。 |
| ~~回退触发条件的一个坑~~ | **已修正**（见下节）：回退判定改为按响应形态判，不再依赖 404 分支。 |
| `POST /api/user/upload` | 未迁移，仍是唯一写链路停留在 Go 的客户端点 |
| 聊天记录无前端调用方 | 历史消息目前由 WS `history` 事件推送，HTTP 接口用于后续/第三方接入，因此其证据是「接口一致」而非「页面渲染」 |

---

## 修正：回退判定改为按响应形态（2026-10-20）

### 原设计的问题

`authFetch()` 原来只按状态码判断新层是否可用（`404 || >= 500`），但真实环境里这两种情况都不成立：

| 真实情形 | 实际返回 | 原判定的结果 |
|---|---|---|
| 新层未部署 | nginx 走 SPA 兜底，**200 + `text/html`** | 当作成功返回，调用方 `resp.json()` 抛 `Unexpected token '<'` |
| 新层部署但缺某路由 | FastAPI **404 + `{"detail":"Not Found"}`** | 与「业务 404（订单不存在）」无法区分，一律回退 |

也就是说回退确实会发生，但触发点是「JSON 解析意外失败」，而不是设计里的 404 判断——真出故障时行为不可预期，`authFetch` 里那句 `status === 404` 基本是死代码。

### 修正方式

新增 `looksLikeBusinessResponse(response)`，判断「这个响应能不能当业务响应来读」：

| 应答 | 判定 | 理由 |
|---|---|---|
| `5xx` | 不可用 → 回退 | 新层内部故障，旧入口还能服务 |
| 非 `application/json`（含兜底 HTML） | 不可用 → 回退 | 新层不存在，请求被别的层接走了 |
| `404/405` 且 body 无 `code` 字段 | 不可用 → 回退 | 框架级 404，说明新层缺这个路由 |
| 带 `code` 的 4xx（401/403/404 等） | 可用 → **不回退** | 这是新层的业务结论，回退会掩盖真实语义 |
| `2xx` JSON | 可用 | 正常 |

依据：`python_business` 的所有响应（含 `HTTPException` 处理器）都带 `code` 字段；FastAPI 框架级 404 只有 `detail`。二者可区分。

同步改动三处，逻辑一致：`web/chat/pc.html`、`web/chat/h5.html`（`authFetch` + `validateCustomerSession`）、`web/chat/login.html`（登录直连回退）。`validateCustomerSession` 另加一层：新层不可用时保留本地会话（临时故障不该把用户踢下线），只有新层明确给 401 才清除。

### 证据：浏览器实测 16/16

做法：`Page.addScriptToEvaluateOnNewDocument` 在页面最早期注入 `fetch` 包装，可切换 `/py-api/*` 的应答形态；模式存 `sessionStorage`，因此跨刷新、跨导航保持。脚本 `cs_fallback_accept.mjs`（未入库）。

```
PASS  基线：访客会话建立
PASS  基线：authFetch 可用
PASS  新层返回 200 HTML（SPA 兜底）        → 应回退  status=200 log=[/py-api/…, /api/customer/ticket]
PASS  新层返回 502 网关错误               → 应回退  status=200 log=[/py-api/…, /api/customer/ticket]
PASS  新层返回框架 404（无 code 字段）     → 应回退  status=200 log=[/py-api/…, /api/customer/ticket]
PASS  新层返回 500 JSON（内部故障）        → 应回退  status=200 log=[/py-api/…, /api/customer/ticket]
PASS  新层返回业务 401（身份被拒）         → 不回退  status=401 log=[/py-api/…]
PASS  新层返回业务 404（资源不存在）       → 不回退  status=404 log=[/py-api/…]
PASS  新层兜底 HTML 时访客仍建立会话        wsConnected=true new=1 legacy=1
PASS  访客令牌实际由旧入口签发              legacy calls=1
PASS  新层兜底 HTML 时登录页仍能登录        url=/chat/pc.html?customer_id=buyer1
PASS  登录请求已回退旧入口                 new=0 legacy=1
PASS  会话校验被 401 拒后不回退旧入口       new=1 legacy=0
PASS  被拒令牌已被前端清除                 stillStored=false
PASS  商家预览会话建立（带 app_secret）      key=cs_preview_session_…
PASS  预览会话被 401 拒后不回退且已清除      stillStored=false legacy=0
TOTAL 16  FAILED 0
```

顺带结掉阶段 1 遗留待办「商家预览未验证 `is_preview` 会话过期路径」：最后两项即是该路径，预览会话被拒后同样清本地、不换来源。

### 回归

| 脚本 | 范围 | 结果 |
|---|---|---|
| `cs_browser_accept.mjs` | 阶段 1 主链路（三身份 / 重启恢复 / 登出） | 13/13 |
| `cs_browser_accept2.mjs` | 阶段 1 补充（h5 / 过期自愈 / SDK iframe） | 12/12 |
| `cs_browser_accept3.mjs` | 阶段 2（聊天记录 / 评价 / 工单） | 9/9 |

累计浏览器断言 50 项，全部通过。

### 验收脚本踩过的坑（记录备查）

- `Page.reload` 会重跑 `addScriptToEvaluateOnNewDocument`，页面内设的模拟模式被重置成默认值 → 模式必须落 `sessionStorage`。
- 带 `app_key` 但**不带** `app_secret` 时，`pc.html` 会丢弃 `app_key` 退回访客模式（`CONFIG.app_key = null`），会话写 `localStorage`；带两者才是预览模式、写 `sessionStorage`。脚本取会话必须复用页面自身的 `sessionStore()` / `sessionStorageKey()`，自己猜前缀必然取空。
- 登录成功会 `location.replace`，页面内日志随文档销毁 → 跨文档的请求断言要用 CDP `Network.requestWillBeSent`。

---

## 阶段 3（第 1 批）：admin 接口迁 FastAPI + Vue 切换（2026-10-20）

### 迁移范围与边界

Go 侧 admin 组共 3 类（公共/`biz`/`plat`，约 70 条路由）。本轮只迁**单表、权限清晰**的一组，先把鉴权基座跑通，其余分组留待后续批次。

| Go 端点 | 处理 |
|---|---|
| `GET /api/admin/ratings` | 迁 → `GET /api/admin/ratings` |
| `GET /api/admin/ratings/stats` | 迁 → `GET /api/admin/ratings/stats` |
| `GET /api/admin/quick-replies` | 迁 → 同名 |
| `POST /api/admin/quick-replies` | 迁 → 同名 |
| `PUT /api/admin/quick-replies/:id` | 迁 → 同名 |
| `DELETE /api/admin/quick-replies/:id` | 迁 → 同名 |
| 其余 ~64 条（tickets/sessions/members/roles/tenants/prompts/kb…） | **未迁**，仍由 Go 处理 |

### 鉴权基座（本轮的核心，端点只是它的第一个消费者）

| 环节 | 实现 | 对齐依据 |
|---|---|---|
| 令牌验签 | 同一 HMAC-SHA256(secret_hash) 格式 | Go `verifyTokenCommon` |
| 类型边界 | `kind == "customer"` → 401「客户会话令牌不可用于管理端」；其他非空 kind → 401「未知令牌类型」；只有 kind 缺省才算管理令牌 | Go `VerifyTokenFull` 的 choke point 语义 |
| 登出黑名单 | Redis `cs:tok:bl:<token>`，命中即 401 | Go `tokenBlacklistTokenPrefix` |
| RBAC | `uid=0` 隐式全权限；`uid>0` 查 `users`（`deleted_at=0`、`tenant_id` 必须一致、`status=1`）+ `user_roles → sys_role_menu → menus.perm` | Go `RequirePerm` / `GetUserPerms` / `PermsOfMenuIDs` |
| 平台管理员隔离 | `tenants.is_platform_admin=1` → 业务组端点 403 | Go `BusinessOnly` |
| 审计 | 写操作成功后写 `audit_logs`：`operator`（子账号记用户名，app_key 管理员记租户 ID）、`user_id`、`object_type`、`object_id`、`detail`、`ip`（`X-Real-IP`） | Go `AuditMiddleware` + `WriteAuditLog` |

### 前端切换

只改 `admin-app/src/api.js` 一个文件，**视图代码零改动**：

- `PY_ADMIN_PREFIXES` 登记已迁移前缀（`/api/admin/ratings`、`/api/admin/quick-replies`），匹配 `前缀 + 结束/`?`/`/`；
- 命中前缀 → 请求 `/py-api` + path，其余仍走 Go；
- 回退判定按**响应形态**（与阶段 2 的 `looksLikeBusinessResponse` 同源）：无响应 / 5xx / 返回体不是带 `code` 的 JSON → `retryable`；带 `code` 的 4xx（401/403/404）→ 业务结论，**不回退**；
- 旧路径请求改为**惰性构造**，只有确认需要回退时才发出 —— 否则两个入口会同时收到同一次写操作。

### 证据一：测试通过

```
python_business  docker compose exec business python -m unittest discover -s tests
                 Ran 35 tests ... OK     # 阶段1 7 + 阶段2 10 + 阶段3 18
```

阶段 3 的 18 项覆盖：kind 边界（客户令牌/未知 kind）、篡改/过期/错误密钥、黑名单、`uid=0` 跳过权限查询、子账号权限命中与未命中、账号禁用、跨租户子账号、平台管理员 403、创建字段归一化与审计、空字段 400、软删除作用域、404、列表过滤与排序、非法过滤参数忽略、分页与上限、统计结构对齐。

### 证据二：接口实测

| 场景 | 结果 |
|---|---|
| Go 签发的管理令牌调 `/py-api/api/admin/*` | 200（令牌互认） |
| 客户令牌调管理端点 | `401 客户会话令牌不可用于管理端` |
| 篡改签名 | `401 令牌签名无效` |
| 无令牌 | `401 缺少访问令牌 token`（Go 侧同为 401） |
| 平台管理员调业务组端点 | py 403 / go 403，**文案逐字一致** |
| 评价统计逐字段比对 | `total/avg_score/dist/satisfied/satisfy_rate/last7days` 全部一致 |
| 话术库 CRUD | Python 建 → Go 读到同一行（改后 `is_active=0`）→ Python 软删 → **两端列表均不可见** |
| 审计落库 | 3 条 `quick_reply` 记录，`detail` 中文完整（`hex` 校验），`operator=t_demo` |

### 证据三：浏览器实测

`cs_admin_accept.mjs`（11 项，CDP `Network` 抓真实请求）：

```
PASS  后台登录页加载 / 管理员登录成功
PASS  评价接口走 FastAPI          py=2 go=0
PASS  评价接口未请求 Go 旧入口
PASS  话术库接口走 FastAPI        py=1 go=0
PASS  话术库接口未请求 Go 旧入口
PASS  新层不可用时页面仍渲染
PASS  先尝试新层                  new=2
PASS  自动回退 Go 旧入口          go=2
```

`cs_admin_write.mjs`（8 项，UI 层写操作）：

```
PASS  新增弹窗打开 / UI 新建后列表出现该话术
PASS  新建请求走 FastAPI          new=1 old=0
PASS  删除请求走 FastAPI          new=1 old=0
PASS  UI 删除后列表不再出现
```

「新层不可用」用 CDP `Fetch.fulfillRequest` 把 `/py-api/api/admin/*` 改写成 **200 + text/html**（即 nginx SPA 兜底的真实形态），页面仍拿到数据 —— 说明回退不依赖状态码。

### 回归

| 脚本 | 范围 | 结果 |
|---|---|---|
| `cs_browser_accept.mjs` | 阶段 1 主链路 | 13/13 |
| `cs_browser_accept2.mjs` | 阶段 1 补充 | 12/12 |
| `cs_browser_accept3.mjs` | 阶段 2 | 9/9 |
| `cs_fallback_accept.mjs` | 回退判定矩阵 | 16/16 |

累计浏览器断言 50 + 19 = **69 项**，全部通过。

### 本轮修掉的一个环境问题：business 容器时区

`docker-compose.yaml` 的 `business` 原为默认 UTC，而 `mysql`/`go` 是 CST。统计类接口按「今天」切日期边界，在 CST 00:00–08:00 区间会让 `last7days` 的最后一天错位。已加 `TZ: ${TZ:-Asia/Shanghai}`，实测容器内 `date` 与 mysql/go 一致。

### 遗留（如实列出）

| 项 | 说明 |
|---|---|
| admin 路由仍有 ~64 条在 Go | 含 tickets/sessions/members/roles/tenants/prompts/kb/model-providers 等；prompts 与 kb 本身是 Go→Agent 的代理，迁移时需先决定是否跳过 Go |
| 子账号 RBAC 只用单测覆盖 | 库里 `users` 表为空，无法用真实子账号验证；403 路径靠 mock SQL 断言 |
| `created_at` 无时区标记 | Python 返回 `2026-10-20T20:54:38`，Go 返回 `...T20:54:38+08:00`。前端 `fmtTime` 只做字符串截断，显示一致；但若将来用 `new Date()` 解析，无时区串会按浏览器本地时间处理 |
| 查询参数中文 | 未编码的中文 query 会让 uvicorn 直接拒（`Invalid HTTP request received`），Go 侧更宽松。浏览器会自动编码，故不影响真实流量 |
| `python_business/` 仍未入 git | 本轮又加了 ~330 行，版本控制缺口在扩大 |

---

## 阶段 3-2：客服状态 / 会话记录 / 客户档案（2026-10-20）

### 迁移范围

| Go 端点 | 处理 |
|---|---|
| `GET /api/admin/agent-status` | 迁 → 同名（`ticket:view`） |
| `PUT /api/admin/agent-status` | 迁 → 同名（`ticket:handle`） |
| `GET /api/admin/sessions` | 迁 → 同名（`session:view`） |
| `GET /api/admin/sessions/:thread_id/messages` | 迁 → 同名（`session:view`） |
| `GET /api/admin/sessions/:thread_id/tags` | 迁 → 同名（`session:view`） |
| `PUT /api/admin/sessions/:thread_id/tags` | 迁 → 同名（`session:profile:edit`） |
| `PUT /api/admin/sessions/:thread_id/profile` | 迁 → 同名（`session:profile:edit`） |
| `POST /api/admin/sessions/:thread_id/reextract` | 迁 → 同名（`session:profile:edit`） |
| `POST /api/admin/sessions/upload` | 迁 → 同名（`ticket:handle`，multipart） |
| `GET /api/admin/customers` | 迁 → 同名（`customer:view`） |

批次边界：这一组只碰 `messages / tickets / customer_profiles / session_tags / session_summaries / agent_status / platform_config / users`，不写工单状态机，不跨服务调用，因此可以独立验收。累计 admin 已迁 6 + 10 = **16 条**，剩余 77 条。

### 本轮补上的三个基础设施（后续所有批次复用）

**① 通用审计中间件，替代逐 handler 写审计**

Go 的 `AuditMiddleware` 挂在 `/api/admin` 分组上，按「method + 路径」自动推断，而不是每个 handler 手写。上一批我在 quick-replies 里手写的 `audit()` 因此有三处偏差（detail 多写了文案、object_type 写成 `quick_reply`、没有覆盖将来批次）。本轮改为真正的 ASGI 中间件：

```
object_type = 路径首段去掉一个尾缀 s → 命中白名单否则 other
object_id   = :id 路由参数，没有则取路径最后一段
action      = POST→create / PUT→update / DELETE→delete
detail      = ""（Go 恒为空）
```

去尾 `s` 是「去掉一个 s」而不是英语单数化，所以 `quick-replies → quick-replie`、`agent-status → agent-statu`，两者都落 `other`；`sessions → session`、`tickets → ticket`、`tenants → tenant` 才命中。**这是 Go 现有行为，不是笔误**，已在单测里锁死。

**② `StatusCode` 中间件的发现与对齐（上一批文档的结论需要更正）**

Go 有一个全局中间件 `middleware.StatusCode()`：把 `c.JSON(http.StatusOK, {"code": 4xx/5xx})` 按 body.code 改写成**真实 HTTP 状态码**（≥500→500；400/401/403/404/405/409/429 原样；其余 4xx→400）。也就是说 Go 的对外契约里 **body.code 与 HTTP 状态码是一致的**。

上一批我按「HTTP 200 + body code」实现是错的（当时只验了 quick-replies，那组 handler 恰好写的是真实状态码，问题被掩盖）。本轮改为 `code_to_status()` 映射，`soft()` 直接返回真实状态。

连带一个必须刻意复刻的次序问题：审计中间件注册在 `StatusCode` **之内**，它读到的 `c.Writer.Status()` 还是 200，所以「软失败」（body code 报错）的写操作**仍会写审计日志**。FastAPI 侧用一个 `request.state.denied` 标记区分：`deny()`（真实 4xx 中止）不记，`soft()` 记。已用真实数据比对确认。

**③ 时间序列化对齐 Go 的 RFC3339**

`iso()` 原来输出 `2026-10-20T20:58:32.477`（无时区），Go 是 `2026-10-20T20:58:32.477+08:00`。容器已设 `TZ=Asia/Shanghai`，改为补本地偏移并在微秒为 0 时省略小数位，与 `encoding/json` 的 `time.Time` 输出逐字一致。这是上一批遗留项，本轮修掉。

### 上一批文档的三处更正

| 上一批的写法 | 实测的 Go 行为 | 处理 |
|---|---|---|
| 客户令牌 → `401 客户会话令牌不可用于管理端`；未知 kind → `401 未知令牌类型` | 任何非空 kind 都回 `401 令牌无效\u6216已过期`（`VerifyTokenFull` 不细分） | 已改为统一文案，`test_stage3_contract` 两处断言同步更新 |
| 审计 `object_type` = `quick_reply` | 实际是 `other`（`quick-replies` 去尾 s 后不命中白名单） | 中间件接管，断言改为 `other` |
| `created_at` 无时区标记（列为遗留） | —— | 本轮修复，不再是遗留项 |

### 证据一：测试通过

```
python_business  python -m unittest discover -s tests
                 Ran 59 tests ... OK      # 阶段1 7 + 阶段2 10 + 阶段3 18 + 阶段3-2 24
```

阶段 3-2 的 24 项覆盖：`iso()` 的 RFC3339 形态（含微秒裁剪）、客服状态列表作用域/排序/LEFT JOIN 空值、`uid=0` 拒绝改状态、非法 body 四种形态（`{}`/越界/字符串/布尔）、只看本人、分页收敛（`page=0&size=500`→1/20）、过滤条件与参数顺序（关键词 5 个、日期 2 个、标签子查询 2 个）、会话项 16 字段集合、`has_open_ticket` 0/1→bool、NULL summary→空串、空列表 `[]`、归档合并与 `ticket_id` 省略、跨租户会话 403、平台管理员 8 个端点全 403、档案 upsert 的 `extract_status=2`、非法 JSON→400、重新提取写入/长数字串边界/规则回退默认值、标签空值与 upsert、客户列表 `null` 与 `[]` 的差异、审计中间件跳过 GET 与真实 4xx、软失败仍记审计、object_type 路径推断矩阵。

### 证据二：接口实测（Go 与 FastAPI 逐字段 diff）

`compare32.py` 对同一路径分别打 Go（`/api/admin/...`）与并行层（`/py-api/api/admin/...`），递归比较 JSON 的每个 key：

```
26/26 一致
```

覆盖：客服状态、会话列表（分页/关键词/转人工/日期/非法参数/越界页）、会话聊天记录、会话标签读、客户列表（含空结果与非法 size）、`PUT agent-status`（app_key 拒绝、非法 body）、标签写入、跨租户 403、档案写入、重新提取、平台管理员 4 个端点 403、客户令牌 401。

`upload32.py`：同一张 PNG 分别上传，`msg_type` 与 URL 前缀一致，两个文件都能被 nginx 以 200 提供（证明 `cs_msg_files` 卷在两层之间共享）；非法扩展在两层都返回 `400 仅支持图片(jpg/png/gif/webp)或视频(mp4/webm)`。

`audit_compare.py`：同一路径分别打两层，`audit_logs` 两行 **除 created_at 外逐字段相同**（`t_demo/t_demo/0/update/other/3/''/172.27.0.1`）；软失败场景两行同样相同 —— 确认审计的「软失败也记」这个次序被正确复刻。

### 证据三：浏览器实测

`cs_stage32_accept.mjs`（20 项，系统 Chrome + CDP `Network` 抓真实请求）：

```
PASS  管理员登录成功
PASS  会话页渲染 / 会话行按 Go 字段渲染（20 行，(N 条客户消息) 正常）
PASS  会话列表走 FastAPI          py=1 go=0
PASS  会话列表未请求 Go 旧入口     go=0
PASS  会话详情打开 / 聊天记录走 FastAPI / 未请求 Go 旧入口 / 标签读取走 FastAPI
PASS  保存标签走 FastAPI          py=1 go=0
PASS  保存标签未打 Go 旧入口 / 标签保存后回显
PASS  客户列表页渲染 / 走 FastAPI / 未请求 Go 旧入口
PASS  客服状态走 FastAPI          py=1 go=0
PASS  客服状态未请求 Go 旧入口
PASS  新层不可用时客户页仍渲染 / 先尝试新层 / 自动回退 Go 旧入口
```

### 前端切换

`admin-app/src/api.js` 的 `PY_ADMIN_PREFIXES` 增加 `/api/admin/agent-status`、`/api/admin/sessions`、`/api/admin/customers` 三条前缀，视图代码零改动。前缀按 `结束 / ? / /` 边界匹配，`/api/admin/session/stats`（单数）不会被误命中，仍走 Go。

### 已知缺陷（照抄 Go，不在本轮修）

| 项 | 说明 |
|---|---|
| 客户列表 `message_count` 恒为 0 | Go 的 query 取 `m.msg_count`，而 `model.CustomerItem.MessageCount` 映射的列名是 `message_count`，GORM `Scan` 匹配不上 → 恒 0。并行期必须与 Go 输出一致，已在代码注释和单测里标明，**Go 下线后再改为真实值** |
| `GET /api/admin/sessions` 的 `total` 不含转人工过滤 | Go 的 COUNT 查询没有 `HAVING`，`handoff=1` 时 `total` 仍是全部会话数。同样照抄 |
| 会话列表逐行补最后一条消息 | Go 是分页后每行一次查询（N+1），本层沿用同一查询形态以保证结果一致；行数上限 100，代价可控。后续可一次性批量取 |

## 阶段 3-3：工单域（2026-10-20）

### 迁移范围

| Go 端点 | 处理 | 权限 |
|---|---|---|
| `GET /api/admin/tickets` | 迁 → 同名 | `ticket:view` |
| `GET /api/admin/tickets/transfer-targets` | 迁 → 同名 | `ticket:handle` |
| `GET /api/admin/tickets/:id` | 迁 → 同名 | `ticket:view` |
| `GET /api/admin/tickets/:id/messages` | 迁 → 同名 | `ticket:view` |
| `GET /api/admin/tickets/:id/logs` | 迁 → 同名 | `ticket:view` |
| `POST /api/admin/tickets/:id/read` | 迁 → 同名 | `ticket:view` |
| `POST /api/admin/tickets/:id/takeover` | 迁 → 同名 | `ticket:handle` |
| `POST /api/admin/tickets/:id/reply` | 迁 → 同名 | `ticket:handle` |
| `POST /api/admin/tickets/:id/close` | 迁 → 同名 | `ticket:handle` |
| `POST /api/admin/tickets/:id/priority` | 迁 → 同名 | `ticket:handle` |
| `POST /api/admin/tickets/:id/transfer` | 迁 → 同名 | `ticket:handle` |
| `POST /api/admin/ticket/approve` | 迁 → 同名（无 `:id`） | `ticket:handle` |

累计 admin 已迁 16 + 12 = **28 条**，剩余 65 条。这是第一个带**写状态机**的批次，也是第一个需要**回写 Go 的 WS Hub** 的批次。

### 本轮补上的两块新基础设施

**① 跨进程 WS 推送桥（Go 侧新增内部接口）**

Go 里 `ws.WSHub` 是进程内单例，客服端浏览器连的是 **Go 的** WebSocket。FastAPI 接管工单写操作后，必须能让「用户端」和「同租户管理端」立刻收到 `human_notice` / `ticket_update`，否则前端要等下一次轮询。

做法是在 Go 侧加一个 InternalAuth 保护的内部接口，而不是把 WS Hub 搬到 Python：

```go
// go_service/internal/handler/ws.go
// POST /api/agent/ws/broadcast   {target, key, tenant_id, payload}
// target: user → SendToUser(key)
//         admins → SendToAdmins()
//         admins_tenant → SendToAdminsByTenant(tenant_id)
```

`python_business` 侧 `go_broadcast(target, payload, key, tenant_id)` 调它（失败重试 1 次）。这样 WS 连接归属不变、无需前端改动，也避免了两套 Hub 状态不同步。

**② 直连 Agent 的 resume 调用**

接管待处理工单前要让 LangGraph 从 HITL 中断点恢复，原实现走 Go 的 `agent.CallAgentResume`。FastAPI 侧用 `httpx.post(f"{CS_AGENT_URL}/agent/resume", ...)` 直连，语义按 Go 复刻：**best effort** —— 失败不阻断接管，只把错误拼进返回文案（`接管成功，但恢复Agent会话失败:<err>`）。

### 复刻到的几处「非显然」Go 行为

| 行为 | 说明 |
|---|---|
| 消息窗口上界 | 待处理 = `created_at + 2s`；接管中 = `now()`；已关闭/取消 = `closed_at + 2s`。目的是让「工单创建之后」的用户消息不被算进本工单视图 |
| 归档合并丢 `msg_type` | 合并 `messages_archive` 时 Go 构造的是 `model.Message` 而没带 `MsgType`，所以归档消息的 `msg_type` 恒为空串；只有 `messages` 表里的才有值 |
| `priority` 不能传 0 | `binding:"required"` 对 int 生效，`0` 视为「未提供」→ `400 参数错误：priority 必填`。`"2"`、`true` 同样绑定失败 |
| 转交写两条审计 | 中间件自动一条（`detail` 为空）+ handler 手写一条（`detail = "转交工单：<no> → <name>"`），两条都要有 |
| 关闭/接管写死 old_status | `close` 的日志 `old_status` 硬编码为 1（接管中），`takeover` 硬编码为 0（待处理），不取工单真实状态 |
| `uid=0` 不统计处理量 | `AdjustHandlingCount` 对 `uid==0` 直接 return；`handler_name` 也退化为租户 ID。app_key 管理员接管不占客服并发名额 |
| 幂等分支照样写审计 | 「该工单已在人工接管中」是 HTTP 200 + code 0，审计中间件读到的仍是 200 → 照写。已作为断言固定下来 |
| `unread_count` 恒在 | Go 的 `Ticket.UnreadCount` 带 `gorm:"-"`，不加 `omitempty`，所以列表与详情都输出该字段（详情恒 0） |
| `closed_at` 省略 | `*time.Time` + `omitempty` → 未关闭时字段不出现 |

### 证据一：测试通过

```
python_business  python -m unittest tests.test_contract tests.test_stage2_contract \
                   tests.test_stage3_contract tests.test_stage32_contract \
                   tests.test_stage33_contract
                 Ran 89 tests ... OK
                 # 阶段1 7 + 阶段2 10 + 阶段3 18 + 阶段3-2 24 + 阶段3-3 30
```

阶段 3-3 的 30 项覆盖：列表过滤条件与参数顺序（status/priority/关键词三参数×3/分页）、非法枚举被忽略、`scope=mine` 转 `handler_uid`、`ORDER BY created_at DESC, id DESC`、`unread_count` 计算与字段集合（12 个键、`closed_at` 缺席）、详情三态错误（400/404/403 跨租户）、消息窗口上界按状态取值、归档合并 与 `msg_type` 空串、已读只标非 user、接管完整副作用链（handler 更新 → 处理量 +1 且状态联动 → 状态改 1 → 日志 → 系统消息 → 两条广播）、`uid=0` 跳过处理量、resume 失败仍完成接管、已关闭/已取消拒绝接管、他人工单 403、接管幂等零业务写、回复前后置校验与广播载荷、关闭幂等/释放处理量/硬编码 old_status、优先级 0 与类型错误四形态、优先级更新与日志、转交校验链（缺参/不存在/跨租户/禁用/状态不符）、转交升级待处理→接管中且双审计、审批缺参、审批取消路径、审批确认委派接管、平台管理员 12 个端点全 403。

### 证据二：接口实测（Go 与 FastAPI 逐字段 diff）

`compare33.py` 对 12 条路由逐字段比对。写接口用「快照 → 打 Go → 还原 → 打 FastAPI → 还原」的方式，保证两端起始状态完全相同：

```
41/41 一致
```

覆盖：列表（默认/分页收敛/status+priority/关键字/非法枚举/scope=mine/越界页）、详情（待处理/已关闭/不存在/非法 id）、消息（待处理窗口/已关闭）、日志、转交候选人、平台管理员 5 个端点 403、客户令牌 401×2、已读、优先级（正常/缺参/0/越界）、转交（无此人/缺参/已关闭）、接管（已关闭/待处理/幂等）、回复（空内容/非接管中/接管中）、关闭（已关闭幂等/接管中）、审批（缺参/工单不存在/取消转接）。

关于最后两项 `msg` 的说明：`Agent resume` 依赖的上游模型当前返回 `403 Free quota exhausted`，Go 与 FastAPI 都把这段错误原样回显，只有里面的 `chatcmpl-<uuid>` / `request_id` 不同 —— 这是模型侧的随机量，不是契约差异。比对脚本对这两处做了归一化，其余字段逐字一致。

数据还原核对：探针结束后 `ticket_logs` / `messages` / `audit_logs` 的 max(id) 与 tickets 各行的 `status/priority/handler_uid/handler_name/closed_at` 全部回到基线，`agent_status` 仍为空表。

### 证据三：浏览器实测

`cs_stage33_accept.mjs`（22 项，系统 Chrome + CDP `Network` 抓真实请求）：

```
PASS  管理员登录成功
PASS  工单列表页渲染                    rows=25
PASS  工单列表走 FastAPI                py=2 go=0
PASS  工单列表未请求 Go 旧入口           go=0
PASS  工单行按 Go 字段渲染（工单号+状态徽标）
PASS  总数来自新层列表 total            共 25 条
PASS  工单详情打开
PASS  工单消息走 FastAPI                py=1 go=0
PASS  工单消息未请求 Go 旧入口           go=0
PASS  消息已读 POST 走 FastAPI          py=1 go=0
PASS  详情头渲染工单号+状态
PASS  操作日志按钮存在
PASS  操作日志走 FastAPI                py=1 go=0
PASS  操作日志未请求 Go 旧入口           go=0
PASS  状态筛选刷新走 FastAPI            py=1 go=0
PASS  接管中工单详情走 FastAPI          py=3
PASS  优先级控件出现（接管中 + canHandle）
PASS  设置优先级走 FastAPI              py=1 go=0
PASS  设置优先级未打 Go 旧入口           go=0
PASS  新层不可用时工单页仍渲染           rows=25
PASS  先尝试新层                        new=2
PASS  自动回退 Go 旧入口                 go=2
```

最后三项是回退验证：用 CDP `Fetch.fulfillRequest` 把 `*/py-api/api/admin/tickets*` 全部替换成 200 HTML（模拟新层没部署时 nginx 的 SPA 兜底），页面仍能从 Go 拿到 25 条工单正常渲染。

浏览器验收同样做了前后快照还原（`snap33.py`），跑完 `messages.is_read`、`tickets.priority`、三张副作用表均回到基线。

### 前端切换

`admin-app/src/api.js` 的 `PY_ADMIN_PREFIXES` 增加：

```js
'/api/admin/tickets',        // 列表/详情/消息/日志/已读/接管/回复/关闭/优先级/转交
'/api/admin/ticket/approve'  // 注意是单数 ticket，独立前缀
```

`/api/admin/ticket/approve` 与 `/api/admin/tickets` 不互相误命中：边界判定是 `结束 / ? / /`，而 `tickets` 在 `ticket` 之后是 `s` 不是 `/`。视图代码零改动。

### 遗留 / 已知缺陷

| 项 | 说明 | 何时处理 |
|---|---|---|
| 客户列表 `message_count` 恒 0 | 阶段 3-2 遗留，Go 的列名对不上 | Go 下线后 |
| `sessions` 的 `total` 不含 handoff | 阶段 3-2 遗留 | Go 下线后 |
| 接管/关闭的日志 `old_status` 硬编码 | `close` 恒记 1、`takeover` 恒记 0，与工单真实前态可能不符（例如对待处理工单直接关闭） | 照抄；Go 下线后可修 |
| 幂等分支写审计 | 「已在接管中」「已关闭」也落一条 `audit_logs`，属 Go 现有噪声 | 照抄 |
| `Agent resume` 依赖上游模型可用 | 当前模型免费额度耗尽，接管会返回 `接管成功，但恢复Agent会话失败:...`。链路本身正确（双端一致），但**用户可见文案受模型侧可用性影响** | 待用户处理额度 |
| 工单写操作的事务性 | Go 的这些写也没有包事务（`SaveTicketMessage` / `AddTicketLog` 各自独立执行），本轮照抄以保证结果一致 | Go 下线后可改 |

## 阶段 3-4：聚合运维域（2026-10-20）

### 迁移范围

| Go 端点 | 分组 | 处理 | 权限 |
|---|---|---|---|
| `GET /api/admin/ops/dashboard` | biz | 迁 → 同名 | `dashboard:view` |
| `GET /api/admin/audit-logs` | admin | 迁 → 同名 | `audit:view` |
| `GET /api/admin/token-usage` | admin | 迁 → 同名（代理 agent） | `usage:view` |
| `GET /api/admin/session/stats` | biz | 迁 → 同名（代理 agent） | `session:view` |
| `GET /api/admin/session/cleanup-count` | biz | 迁 → 同名（代理 agent） | `session:cleanup` |
| `POST /api/admin/session/cleanup` | biz | 迁 → 同名（代理 agent） | `session:cleanup` |
| `GET /api/admin/messages/archive-count` | biz | 迁 → 同名 | `session:cleanup` |
| `POST /api/admin/messages/archive` | biz | 迁 → 同名 | `session:cleanup` |
| `GET /api/admin/model-config` | biz | 迁 → 同名 | `model:view` |
| `PUT /api/admin/model-config` | biz | 迁 → 同名 | `model:edit` |
| `GET /api/admin/extract-rules` | plat | 迁 → 同名 | 仅平台管理员 |
| `PUT /api/admin/extract-rules` | plat | 迁 → 同名 | 仅平台管理员 |

累计 admin 已迁 28 + 12 = **40 条**，剩余 53 条。

注意分组差异（决定平台管理员能不能访问）：`audit-logs` / `token-usage` 挂在 **admin 组**，平台管理员可访问（全租户视角）；其余 10 条挂 **biz 组**，平台管理员一律 403；`extract-rules` 挂 **plat 组**，非平台管理员 403。

### 本轮补上的三块基础设施

**① API Key 加密的跨语言兼容（AES-256-GCM）**

`tenants.model_api_key` / `embedding_api_key` 是 Go `crypto` 包加密的：`base64(nonce[12] || AES-GCM(plaintext))`，密钥 = `MODEL_KEY_ENCRYPT_KEY` 截断/补零到 32 字节。FastAPI 侧用 `cryptography` 的 `AESGCM` 逐字复刻，并实测双向互通（见证据二）。

同时发现一个**部署级缺口**：`MODEL_KEY_ENCRYPT_KEY` 原先只注入 `go` 容器，`business` 没有。如果不补，FastAPI 读到的密文全部解成空串（`api_key_configured` 恒 false、`api_key_masked` 恒空），写入的 Key Go 也解不开。已在 `docker-compose.yaml` 的 business 段补上同一个环境变量。

**② 提取规则的内存缓存失效桥**

Go 的 `repository.loadExtractRules` 有一层 `atomic.Value` 进程内缓存，只在进程重启或缓存为空时读库。规则一旦改由 FastAPI 写入，Go 侧就会一直用旧规则。为此新增：

```go
// repository/customer.go
func InvalidateExtractRulesCache()   // 存一个空切片，下次读取落回查库
// handler/ws.go + router.go
POST /api/agent/extract-rules/reload // InternalAuth
```

FastAPI 在 PUT 成功后 best-effort 调用它（`reload_go_extract_rules()`，失败不影响写入结果）。

**③ 代理层 `ProxyToAgent` 的等价实现**

`/session/stats`、`/session/cleanup`、`/session/cleanup-count`、`/token-usage` 原本是 Go 向 Agent 透传。FastAPI 侧 `agent_call()` 复刻：透传 method/query/body/`Content-Type`/`X-Trace-Id`/`X-Tenant-Id` + `X-Internal-Token`，**不检查 Agent 的 HTTP 状态**，拿到什么原样以 200 回写（Go 就是这么做的）。

`token-usage` 多一层作用域收敛：非平台管理员强制把 `tenant_id` 覆盖成本租户，防止越权查看他租户用量。

### 复刻到的几处「非显然」Go 行为

| 行为 | 说明 |
|---|---|
| 归档/审计的 `days` 从 **query** 读 | `POST /messages/archive?days=90`，body 里的 days 被忽略。`days<=0 → 90`，`days>365 → 365` |
| 归档条件 | `created_at < cutoff AND (ticket_id IS NULL OR 工单状态 IN (2,3))`——活跃工单上下文的消息不归档，保证客服还能查历史 |
| 看板 `agents` 是 `null` 不是 `[]` | `var agents []AgentTicketStat` 直接 Scan，无数据时是 nil slice；而 `tickets` 是 `make([]TicketDist,0,n)`，输出 `[]` |
| 看板趋势固定 7 天 | 先按天分组查库，再按「今天往前 6 天」逐日填充，缺的补 0 |
| 审计 `page`/`page_size` 回显原始值 | 钳制发生在 repository 内部，handler 回显的是 `Atoi` 的结果：`page=abc → 0`、`page_size=999 → 999`，但 SQL 用的是 1/20 |
| 审计空结果是 `[]` | `q.Find(&logs)` 会把目标切片初始化为空切片（这点和工单列表的 nil 行为相反） |
| `provider_key` 是推断值 | 存量字段为空时，按 `model_providers.default_model` → `default_api_base` → `custom` 依次推断 |
| `model-config` 只写真正变化的字段 | 逐字段与库中现值比对，`updates` 精确反映差异；空 `updates` → `400 没有需要更新的字段` |
| 保存后广播缓存失效 | `cache().publish("cs:model:cache:invalidate", {tenant_id, scope})`，`scope` 按字段前缀判 chat/embedding/all |
| 提取规则先删后校验 | `DELETE ... LIKE 'cs_extract_rule_%'` 之后才逐条校验正则，**非法正则会留下已删状态**（Go 原缺陷，照抄） |
| 正则用 RE2 语义 | Go 用 `regexp.Compile`：不支持 lookaround/反向引用；错误文案形如 `missing closing ]: ` + 出错位置起的剩余片段 |

### 证据一：测试通过

```
python_business  python -m unittest tests.test_contract tests.test_stage2_contract \
                   tests.test_stage3_contract tests.test_stage32_contract \
                   tests.test_stage33_contract tests.test_stage34_contract
                 Ran 125 tests ... OK
                 # 阶段1 7 + 阶段2 10 + 阶段3 18 + 阶段3-2 24 + 阶段3-3 30 + 阶段3-4 36
```

阶段 3-4 的 36 项覆盖：看板结构/7 天补零/状态名映射/租户参数 9 次绑定、`agents` 无数据为 null、平台管理员 403、统计失败为软 500；审计日志租户作用域与平台全览、`ORDER BY id DESC`、分页「回显原值 + 查询钳制」、空结果为 `[]`、四类模糊/区间筛选的参数顺序；四个代理接口的方法/路径/query/body 透传、Agent 失败时的 `code 500` 文案、token-usage 的租户强制与平台保留；归档 days 收敛矩阵、归档条件与分批（INSERT + DELETE + 事务提交）；模型配置 GET 的掩码与 provider 推断、租户不存在 404、无变化 400、逐字段 diff、custom 缺 Key、`sk-***` 掩码忽略、`...abcd` 不被识别为掩码（缺陷照抄）、缓存失效广播 scope、scope 判定矩阵；AES-GCM 往返 + 手工拼装的 Go 密文可解 + 掩码边界；提取规则默认值回退、DB 读取、先删后写与空字段跳过、非法 JSON/非法正则/RE2 不支持语法、非平台管理员 403、写后必须触发 Go 缓存失效；平台管理员 7 个 biz 端点全 403。

### 证据二：接口实测（Go 与 FastAPI 逐字段 diff）

`compare34.py`：

```
35/35 一致
```

覆盖：运营看板（+平台管理员 403）、审计日志 11 种查询（分页/越界/非法 page/按 action/按对象/模糊 operator/模糊 ip/时间窗/空结果/平台全览/客户令牌 401）、token-usage（普通 + 平台）、会话统计、清理计数、清理（365 天空操作）、归档计数（含非法 days 与上限）、归档执行、模型配置 GET/PUT 5 种（无变化/custom 缺 Key/清除 Key）、提取规则 6 种（读/非平台 403/写/空字段/非法 body/非法正则）。

**AES-GCM 双向互操作实测**（真实写入 DB 再交叉读取）：

```
1) 现状 masked(Go)         : ...vZOA
2) Go 写入 updated          : ['llm_provider_id', 'model_api_key']
3) Go 密文 base64 长度      : 68（12 字节 nonce + 密文 + 16 字节 tag）
4) Python 解 Go 密文 mask  : ...9f3a      ← 期望 ...9f3a  ✅
5) Python 写入 updated     : ['model_api_key']
6) 两次密文不同(随机 nonce) : True
7) Go 解 Python 密文 mask  : ...7b2c      ← 期望 ...7b2c  ✅
8) 已还原 model_api_key    : ...vZOA
```

数据还原核对：`tenants` 的 16 个模型配置字段、`platform_config` 的规则行、`audit_logs` 行数全部回到基线（探针过程中写入的 2 条审计行已按 id 清除）。

### 证据三：浏览器实测

`cs_stage34_accept.mjs`（17 项）：

```
PASS  管理员登录成功
PASS  运营看板渲染 / 看板走 FastAPI(py=1 go=0) / 未请求 Go 旧入口 / 总览指标渲染
PASS  审计日志页渲染（共 231 条）/ 走 FastAPI(py=1 go=0) / 未请求 Go 旧入口
PASS  审计日志有数据行 rows=10
PASS  审计日志翻页走 FastAPI py(page=2)=1
PASS  用量页渲染 / Token 用量走 FastAPI(py=2 go=0) / 未请求 Go 旧入口 / 表格有数据行 rows=3
PASS  新层不可用时审计页仍渲染 / 先尝试新层 / 自动回退 Go 旧入口
```

回归：`compare32.py` 26/26、`compare33.py` 41/41、`cs_stage33_accept.mjs` 22/22 全部仍通过。

### 前端切换

```js
'/api/admin/ops/dashboard', '/api/admin/audit-logs', '/api/admin/token-usage',
'/api/admin/session/stats', '/api/admin/session/cleanup',
'/api/admin/session/cleanup-count',
'/api/admin/messages/archive', '/api/admin/messages/archive-count',
'/api/admin/model-config', '/api/admin/extract-rules'
```

新增 `PY_ADMIN_EXCLUDE`：`/api/admin/model-config/test` 是平台管理员的模型连通性测试（阶段 3-7），虽然被 `/api/admin/model-config` 前缀连带命中，但显式排除后仍走 Go。这是本轮新引入的机制——后续阶段凡「父路径已迁、子路径未迁」都需要在这里登记。

### 遗留 / 已知缺陷

| 项 | 说明 | 何时处理 |
|---|---|---|
| 模型配置掩码回填会覆盖 Key | `MaskAPIKey` 输出 `...abcd`（不含 `*`），但 `isMaskedKey` 只认 `*` → 把回显值原样提交会被当成新 Key 覆盖。单测已固化这个行为 | 照抄；Go 下线后可统一改成含 `*` 的掩码 |
| 提取规则「先删后校验」 | 非法正则会让旧规则已被删除，留下空配置（随后回退内置默认规则） | 照抄；Go 下线后改成先校验再写入 |
| 归档的「全租户」分支不可达 | `tenantID=""` 需 `AuthEnabled=false` 才成立，实际永远传本租户 | 照抄 |
| 看板 `satisfaction_rate` 为 NULL 时 | Go 侧是 `NULL` 扫进 `float64`，理论上会 Scan 失败；当前数据有 7 条评价未触发。FastAPI 按 0 处理 | 待真实空评价环境再核对 |
| 代理接口的传输失败文案 | Go 是 `调用agent失败:<go 的 err 文本>`，FastAPI 是 `<httpx 的 err 文本>`，两边措辞不同（只在 Agent 不可达时出现） | 可接受 |
| 提取规则缓存 | 已用 reload 桥主动失效；但若 FastAPI 调用失败（Go 不可达），Go 侧仍会用旧规则直到重启 | 并行期已知限制 |

## 阶段 3-5：RBAC 域（2026-10-20）

### 迁移范围

| Go 端点 | 分组 | 处理 | 权限 |
|---|---|---|---|
| `GET /api/admin/menus/tree` | admin | 迁 → 同名 | 无（登录即可） |
| `GET /api/admin/permissions` | biz | 迁 → 同名 | `role:view` |
| `GET /api/admin/members` | biz | 迁 → 同名 | `member:view` |
| `POST /api/admin/members` | biz | 迁 → 同名 | `member:add` |
| `PUT /api/admin/members/:id` | biz | 迁 → 同名 | `member:edit` |
| `PUT /api/admin/members/:id/status` | biz | 迁 → 同名 | `member:edit` |
| `PUT /api/admin/members/:id/password` | biz | 迁 → 同名 | `member:edit` |
| `PUT /api/admin/members/:id/roles` | biz | 迁 → 同名 | `member:edit` |
| `DELETE /api/admin/members/:id` | biz | 迁 → 同名 | `member:remove` |
| `GET /api/admin/roles` | biz | 迁 → 同名 | `role:view` |
| `POST /api/admin/roles` | biz | 迁 → 同名 | `role:add` |
| `PUT /api/admin/roles/:id` | biz | 迁 → 同名 | `role:edit` |
| `DELETE /api/admin/roles/:id` | biz | 迁 → 同名 | `role:remove` |

累计 admin 已迁 40 + 13 = **53 条**，剩余 40 条。

分组要点：只有 `menus/tree` 挂 **admin 组**（登录后拉侧边栏，不做权限校验）；其余 12 条全部挂 **biz 组**，平台管理员一律 403。`menus/tree` 是本阶段唯一「不分租户业务数据」的接口，因为它本身就是身份视图。

### 本轮补上的三块基础设施

**① 显式事务 `transaction()`（首个需要多表原子写的域）**

`db()` 是 `autocommit=True` 的单连接，成员创建要同时写 `users` + `user_roles`，角色创建要写 `roles` + `sys_role_menu`。新增：

```python
@contextmanager
def transaction():
    # begin → 交出 DictCursor → commit / rollback → close
```

单测里把它 patch 成一个**假游标**（`FakeCursor`），这样 45 条用例既能断言 SQL 语句与参数，又不需要真库。

**② `write_handler_audit()`：handler 级审计与中间件审计的双写**

Go 的 RBAC handler 在成功之后会**额外**调一次 `repository.WriteAuditLog`（带 detail），中间件随后又写一条（object_type/object_id 由路径推导）。两条都会落库，因此同一操作在 `audit_logs` 里是两行：

```python
write_handler_audit(admin, request, "create", "member", "kefu01", "添加成员")
# ↑ object_type 由调用方显式给出；operator 语义取 Go currentOperator：uid=0 → 空串
```

注意两处细节：

- `object_type` **不能**按路径推导。Go 这里传的是 `"member"` / `"role"`，但 `auditObjectType("/api/admin/members")` 得到的是 `member` 之外的值（`members` 去掉一个尾缀 `s` 得 `member`，但 `member` 不在白名单 → `other`）。所以 handler 行进白名单值、中间件行进 `other`，两行不同。
- handler 行的 `operator`：app_key 管理员（uid=0）是**空串**（`currentOperator` 的既有约定），中间件行则是租户 ID。两者刻意不一致。

**③ 菜单树的三种身份视图（`MenuTreeForUser` 的完整复刻）**

```
平台管理员          → 只返回「系统管理」组，不做权限过滤
租户 app_key 管理员  → 「业务中心」全部启用节点（隐式全权）
租户子账号(uid>0)   → 「业务中心」中命中权限码的节点 + 自动补齐父链
无租户上下文        → 全量（演示模式，本部署不可达）
```

子账号视图的匹配条件必须照抄：`status==1 && type != 1 && perm 非空 && perm 命中`——目录节点（`type=1`）本身不参与匹配，只由叶子节点反向 `mark()` 父链。少了这条，纯目录权限会凭空多出一整棵树。

### 复刻到的几处「非显然」Go 行为

| 行为 | 说明 |
|---|---|
| `menus.created_at` 是 NULL | 表里建库时没写 created_at，Go 用非指针 `time.Time` 接 → 零值，序列化成 `0001-01-01T00:00:00Z`（不是 `null`） |
| 空菜单树是 `null` | `MenuTreeForUser` 无节点时 `return nil` → `data: null`；而组的 `children` 为空时也是 `null`（`[]*MenuNode` 为 nil） |
| 权限目录顺序 | 来自 `cachedMenus` 的 `GROUP BY group_name ASC, sort ASC, id ASC`，不是权限目录常量顺序；`perm` 为 NULL/空串的菜单直接跳过 |
| `is_admin` 只看权限集合 | `isFullPerms(角色菜单推导出的权限码) == 覆盖 32 个权限码全集`。与角色名、`code` 无关——`admin` 角色改名/改权限后判定会跟着变 |
| 角色列表时间格式 | `RoleItem` 用 `Format("2006-01-02 15:04:05")`（空格、无时区），与其它接口的 RFC3339 不同 |
| `EnsureTenantRoles` 懒初始化 | 列表/创建接口都会先跑一次：无角色且 `rbac_seeded=0` → 种入 `admin`（业务中心全量）+ `staff`（9 个客服权限码），并把 `rbac_seeded` 置 1。**GET 也会写库**；已有角色则纯读 |
| 成员状态接口不校验存在性 | `PUT /members/999/status` 回 200「已更新」并写审计，SQL 影响 0 行 |
| 密码哈希 | `sha256(tenant_id|username|password)` 的 hex（Go `crypto.HashUserPassword`）；重置密码用的是**库里的 username**，不是请求体里的 |
| 软删除存 unix **秒** | `users.deleted_at = int(time.time())`，与该表其它软删记录一致 |
| `role_ids` 去重差异 | `CreateRole` **不去重**（重复 menu_id 撞 `sys_role_menu` 主键 → 整单 500「创建角色失败」）；`UpdateRole` 会去重并跳过 0 |
| 角色 code 随机 | `custom_` + 12 位 hex，创建后不可改；双端比对时按 `custom_<rand>` 归一 |
| 自操作保护三处文案不同 | 改状态「不能操作自己的账号」/ 改资料「不能修改自己的账号信息」/ 改角色「不能修改自己的角色」/ 删除「不能删除自己的账号」；**重置密码没有自保护** |
| 成员编辑只写变化字段 | 与库中现值逐字段比对，值未变则不更新；`phone`/`email` 只在「有值且与原值不同」时才校验格式 |
| `ParseUint` 语义 | 路径 id 只认纯十进制数字：`/members/abc`、`/members/ 5`、`/members/-1` 一律落到 `id==0` 分支 → `400 参数错误` |

### 证据一：测试通过

```
python_business  python -m unittest tests.test_contract tests.test_stage2_contract \
  tests.test_stage3_contract tests.test_stage32_contract tests.test_stage33_contract \
  tests.test_stage34_contract tests.test_stage35_contract
Ran 170 tests in 2.6s
OK
```

本阶段新增 `tests/test_stage35_contract.py` **45 条**（累计 125 → 170）：菜单树三种身份 + 空集形态 + 节点序列化、权限目录过滤与权限校验、成员列表筛选与租户作用域、创建参数校验矩阵（7 组）、密码哈希、角色覆盖的越权过滤、自操作保护、状态接口的「不校验存在」怪癖、软删秒级时间戳、角色 CRUD 与去重差异、四条端点的权限拒绝。

顺带修掉一条**环境依赖型旧用例**：`test_crypto_matches_go_format` 硬编码了 DEV 默认密钥，而部署环境已注入真实 `MODEL_KEY_ENCRYPT_KEY`，容器里必然失败。改为 `main.model_key_bytes()` 生成密钥后重新通过（只改测试，不改实现）。

### 证据二：接口实测（Go 与 FastAPI 逐字段 diff）

`compare35.py`：**8 个场景 / 58 个请求步骤 / 8 个场景全部一致**。写场景按「快照 → 跑 Go → 取副作用 → 还原 → 跑 FastAPI → 取副作用 → 还原」执行，`users`/`user_roles`/`roles`/`sys_role_menu` 四张表全量还原（含 `AUTO_INCREMENT` 回拨，保证自增 id 两端一致），审计行按 `id > baseline` 比对增量。

（脚本可在两处跑，结果一致：宿主机 `~/.workbuddy/binaries/python/envs/cs-business`，或容器内 `docker cp` 到 `cs_business:/tmp` 并注入 `CA_GW_HOST=nginx CA_GW_PORT=80 CA_DB_HOST=mysql`。请求客户端换成裸 socket 的 `httpc.py`——本机 urllib 会被代理与命令沙箱干扰，出现过 `getaddrinfo` 假失败；另外 Git Bash 会把 `/api/...` 参数当路径改写，命令行调用需 `MSYS_NO_PATHCONV=1`。）

```
[ok] 读接口：菜单树 / 权限目录 / 成员列表 / 角色列表（10 步）
[ok] 权限边界：平台管理员 403 / 子账号缺权限 403（5 步）
[ok] 成员：创建 → 状态 → 密码 → 资料 → 角色 → 删除 → 回读（10 步）
[ok] 成员：自操作保护（改状态 / 删除 / 改资料 / 改角色）（5 步）
[ok] 成员：创建参数校验矩阵（11 步）
[ok] 角色：创建 → 更新 → 删除 → 回读（5 步）
[ok] 角色：异常分支（空名 / 不存在 / 重复菜单 / 坏 id）（7 步）
[ok] 子账号：staff 角色访问成员/角色管理被拒（5 步）
双端逐字段完全一致
```

比对内容不止响应体：成员创建的 `password_hash`（确定性，跨语言必须逐字节相同）、新增的 `user_roles` 行、角色 `code` 归一后的 `roles` 行、`sys_role_menu` 全量、审计增量（action/object_type/object_id/detail/operator 五元组，含**双写两行**的差异）、`tenants.rbac_seeded`。

数据还原核对：`users` 0 行、`user_roles` 0 行、`roles` 2 行、`sys_role_menu` 47 行、`audit_logs` max(id) 回到 427。

> 环境说明（如实记录）：本阶段第一次探测 `GET /api/admin/roles` 时，`t_demo` 的 `roles` 表是空的且 `rbac_seeded=0`，于是触发了 `EnsureTenantRoles` 的懒初始化——为租户种入 `admin`/`staff` 两个预置角色（39 + 9 条菜单授权）并把 `rbac_seeded` 置 1。这是该接口的**既有设计行为**（Go 同样会写），不是迁移引入的副作用；此后所有比对都以「已初始化」为基线。

### 证据三：浏览器实测

`cs_stage35_accept.mjs`（25 项，全 PASS）：

```
PASS  管理员登录成功
PASS  菜单树走 FastAPI(py=1) / 未请求 Go 旧入口(go=0)
PASS  侧边栏渲染业务中心 6 个目录（会话/工单/知识库/模型/统计/用户管理）
PASS  成员页渲染 / 成员列表走 FastAPI(py=1 go=0) / 角色下拉已加载
PASS  成员搜索带 q 走 FastAPI(py q=kefu=1)
PASS  添加成员弹窗打开 / 提交新成员成功 / 创建走 FastAPI(POST) / 未请求 Go
PASS  新成员出现在列表 → 删除 → 删除走 FastAPI(DELETE) → 列表不再出现
PASS  角色页渲染预置角色（管理员/客服）/ 角色列表走 FastAPI(py=1 go=0)
PASS  角色权限树走 FastAPI(py tree=1) / 未请求 Go 平台级全量菜单接口(go menus=0)
PASS  新层不可用时成员页仍渲染 / 先尝试新层 / 自动回退 Go 旧入口
```

这一轮特意把**写链路**（添加成员 → 删除成员）放进浏览器验收：Go 侧的角色下拉、权限树、创建后的列表刷新都依赖 `roles` + `menus/tree` 两个已迁接口，串起来才能真正证明「前端不改代码也能跑」。

回归：`compare33.py` 41/41、`compare34.py` 35/35、`cs_stage33_accept.mjs` 22/22、`cs_stage34_accept.mjs` 17/17 全部仍通过。浏览器验收产生的临时账号（`kefu_ui_probe`）与审计行已按 id 清除；3-3 回归脚本对演示工单的优先级改动也已回滚（`ticket_logs` id=75 删除、`tickets#20.priority` 复位为 1）。

### 前端切换

```js
// 阶段 3-5：RBAC 域（菜单树 / 权限目录 / 成员 / 角色）
'/api/admin/menus/tree', '/api/admin/permissions',
'/api/admin/members', '/api/admin/roles'
```

只登记 `/api/admin/menus/tree` 而不是 `/api/admin/menus`：后者是平台级「全量菜单管理」（增删改/批量/重置内置），属阶段 3-6，仍走 Go。`migratedToNewLayer` 按「整段相等 / 前缀 + `/`」匹配，因此 `/menus` 不会被 `/menus/tree` 连带命中——但**反向**必须靠登记精确路径，这点在 3-6 迁 `/api/admin/menus` 时要重新核对（届时两条路径会同时命中新层）。

### 遗留 / 已知缺陷

| 项 | 说明 | 何时处理 |
|---|---|---|
| 角色 code 不可指定 | `genRoleCode()` 随机生成，前端与运维都无法自定义编码；两端一致 | 照抄 |
| 重复 `menu_ids` 导致整单 500 | 前端勾选不会重复，但 API 直连可触发（单测已固化） | 照抄；Go 下线后可改成去重 |
| 建成员的 `username` 不去空格 | 校验用原始值（`b.Username == ""`），入库也用原始值；只有 nickname/phone/email/remark 会 `TrimSpace` | 照抄 |
| `menus/tree` 无缓存 | Go 有进程内 `menuCache`，改菜单后同进程立即生效；FastAPI 每次查库（结果一致，只是少一层缓存） | 可接受（查询很轻） |
| 角色权限的内存缓存 | Go 侧 `rolePermsCache` 是进程内缓存，FastAPI 改了某人的角色后，Go 侧到进程重启前仍用旧权限（仅影响仍由 Go 处理的接口，如 `RequirePerm` 校验的 3-6/3-7 路由） | 并行期已知限制，与 3-4 的提取规则缓存同类 |
| 审计双写 | 每个成员/角色写操作在 `audit_logs` 里是两行（handler + 中间件），`object_type` 分别是 `member`/`role` 与 `other` | 照抄；Go 下线后建议只保留 handler 行 |

---

## 阶段 3-6：平台域（租户 / 模型提供商 / 全量菜单）（2026-10-20）

### 迁移范围

| Go 端点 | 分组 | 处理 | 权限 |
|---|---|---|---|
| `GET /api/admin/tenants` | plat | 迁 → 同名 | 平台管理员 |
| `GET /api/admin/tenants/:tid` | plat | 迁 → 同名 | 平台管理员 |
| `POST /api/admin/tenants` | plat | 迁 → 同名 | 平台管理员 |
| `PUT /api/admin/tenants/:tid` | plat | 迁 → 同名 | 平台管理员 |
| `PUT /api/admin/tenants/:tid/quota` | plat | 迁 → 同名 | 平台管理员 |
| `POST /api/admin/tenants/:tid/reset-secret` | plat | 迁 → 同名 | 平台管理员 |
| `POST /api/admin/tenants/:tid/toggle` | plat | 迁 → 同名 | 平台管理员 |
| `DELETE /api/admin/tenants/:tid` | plat | 迁 → 同名 | 平台管理员 |
| `GET /api/admin/tenants/:tid/model-config` | plat | 迁 → 同名 | 平台管理员 |
| `PUT /api/admin/tenants/:tid/model-config` | plat | 迁 → 同名 | 平台管理员 |
| `GET /api/admin/model-providers` | plat | 迁 → 同名 | 平台管理员 |
| `POST /api/admin/model-providers` | plat | 迁 → 同名 | 平台管理员 |
| `PUT /api/admin/model-providers/:id` | plat | 迁 → 同名 | 平台管理员 |
| `DELETE /api/admin/model-providers/:id` | plat | 迁 → 同名 | 平台管理员 |
| `GET /api/admin/available-model-providers` | **admin** | 迁 → 同名 | 登录即可 |
| `GET /api/admin/menus` | plat | 迁 → 同名 | 平台管理员 |
| `POST /api/admin/menus` | plat | 迁 → 同名 | 平台管理员 |
| `PUT /api/admin/menus/:id` | plat | 迁 → 同名 | 平台管理员 |
| `DELETE /api/admin/menus/:id` | plat | 迁 → 同名 | 平台管理员 |
| `POST /api/admin/menus/batch-update` | plat | 迁 → 同名 | 平台管理员 |
| `POST /api/admin/menus/reset-builtin` | plat | 迁 → 同名 | 平台管理员 |

累计 admin 已迁 53 + 21 = **74 条**，剩余 19 条（3-7 代理域）。

**这一批唯一的权限陷阱**：`available-model-providers` 名字看起来像平台配置，实际在 Go 里挂 **admin 组**（源码注释：「公共配置数据，不属于租户业务数据」）。按 plat 组写会让租户管理员 403，而 Go 返回 200。单测已固化为 `test_available_model_providers_is_admin_group_not_platform_only`。

### 本轮补上的三块基础设施

**① `go_internal()`：反向回调 Go 的进程内缓存（并行期专用）**

前三批都是「读库写库」的纯数据迁移，这一批第一次撞上**进程内状态**。Go 侧有三份内存缓存，FastAPI 写库后它们不会自动失效，会产生可复现的真实故障：

| Go 缓存 | 失效方式 | 不失效的后果 |
|---|---|---|
| `menuCache` + `rolePermsCache` | `POST /api/agent/menus/reload` | 改完菜单/权限码，侧边栏与 Go 侧的 `RequirePerm` 仍用旧值 |
| `auth` 租户表（`tenantByKey` / `TenantsList`） | `POST /api/agent/tenants/reload` | ① 新建租户用 app_key+secret 登录失败（缓存里查不到）；② **已禁用租户的旧令牌在 Go 侧仍然有效** |
| `ws.Hub` 连接计数 | `GET /api/agent/ws/conns`（只读） | 租户列表的「在线」列恒 0 |

为此在 `go_service` 侧新增 3 个内部接口（`X-Internal-Token` 校验，不进公开路由表）：

```go
// router.go — agentInternal 组
agentInternal.POST("/menus/reload",   handler.AgentMenusReload)    // repository.InvalidateMenuCaches()
agentInternal.GET ("/ws/conns",       handler.AgentWSConns)        // ws.WSHub.CountAllTenantConns()
agentInternal.POST("/tenants/reload", handler.AgentTenantsReload)  // auth.RefreshTenants()
```

Python 侧统一包装成**带重试的 best-effort**（失败返回 `None`，不阻断主流程，因为并行期 Go 可能正在重启）：

```python
def go_internal(method, path, body=None, timeout=5):     # 失败 → None
def reload_go_menu_caches():  for _ in range(2): ...     # 5 个菜单写接口后各调一次
def reload_go_tenants():      for _ in range(2): ...     # 6 个租户写接口后各调一次
```

> 这 3 个接口是**并行期脚手架**，Go 下线时应当一并删除；`go_internal` 的调用点已在代码里就地注明。

**② GORM `Create` 零值语义：一张 30 列的 INSERT 必须写全**

这是本批最花时间的一处坑。GORM 的 `Create` 会把**所有没有 `default` tag 的字段按 Go 零值写库**（`0` / `''` / 零值时间），而 MySQL 表里这些列的默认值是 **NULL**。也就是说：Go 插入租户后，`deleted_at` 是 `0`、`created_at` 是 `NOW(3)`、`is_platform_admin` 是 `0`——看起来「本该是 NULL」的列其实是零值。

我最初只写了一部分列，结果连撞三个 404/错行：

| 漏写列 | 现象 |
|---|---|
| `deleted_at` | 落 NULL → 详情/配额/删除全部 404（查询条件是 `deleted_at = 0`） |
| `created_at` | 落 NULL → 详情返回 `null`、列表时间乱序 |
| `is_platform_admin` | 落 NULL → 列表的 `is_platform_admin = 0` 过滤把新租户整条剔掉（列表长度 2≠1） |

最终改为**逐列写全 `model.TenantRow` 的所有零值字段**，包括 `model_provider='platform'`、`llm_provider_id=0`、各种 `*_price=0`、`max_concurrent_sessions=100` / `daily_message_limit=1000` / `max_kb_docs=100` / `max_kb_size_mb=100`。这条经验对 3-7 同样适用：**凡 Go 侧用 GORM `Create` 的表，Python 侧 INSERT 都要按零值写全，不能依赖 MySQL 默认值。**

**③ `BUILTIN_MENUS`：50 条内置菜单逐字对齐**

`POST /api/admin/menus/reset-builtin` 会先清空 `menus` + `sys_role_menu`，再按 Go 的 `builtinMenus` 重新种入 50 条并给平台管理员重新授权。这 50 条的名称/权限码/图标/排序/父链必须与 Go **逐字一致**，否则重置一次就把两侧菜单树写歪了。做法是把 Go 的 `builtinMenus` 切片原样搬到 Python 常量，并用 `reset-builtin → 回读全量菜单` 的双端 diff 兜底。

### 复刻到的几处「非显然」Go 行为

| 行为 | 细节 |
|---|---|
| `time.Truncate(24*time.Hour)` ≠ 本地午夜 | 它按 **Unix 纪元**取整，得到的是**本地 08:00**（Asia/Shanghai +8）。日用量统计的窗口起点必须用它，不能用 `date_trunc('day')` |
| `messages` 表**没有** `deleted_at` | 删租户时的消息清理是 `DELETE FROM messages`（硬删），`model.Message` 也确实没有 `DeletedAt`。写成 `UPDATE ... SET deleted_at` 会直接 1054 Unknown column |
| 软删除单位不一致 | `tenants` / `tickets` 存 unix **秒**（`0` 表示未删）；`messages` 硬删 |
| 多字段 required 校验文案 | gin/validator 的 `err.Error()` 会把**所有**失败项用 `\n` 串起来，不是只报第一条 → `required_field_error(*fields)` 收集全部再拼接 |
| 菜单删除是级联 | 删父节点会连带删除子孙菜单及所有角色的 `sys_role_menu` 关联，不是单纯软删 |
| 重复权限码 | Go 靠 DB 唯一键报错，Python 侧用 `is_duplicate_error(exc)` 识别 1062 后回同样的 `code` |
| 租户删除后的副作用 | 依次：软删租户 → 硬删该租户 `messages` → 清 Agent 侧会话（异步）→ 清 ws 连接（异步）；异步部分用 `threading.Thread(daemon=True)` 包 `prewarm_tenant_async` / `cleanup_tenant_async` |

### 证据一：测试通过

```
python -m unittest discover -s tests -t tests   →  Ran 220 tests, OK
```

其中 `tests/test_stage36_contract.py` 新增 **50 条**（累计 220）。断言覆盖：21 条路由的鉴权分组、零值列写全、多字段 required 文案、级联删除 SQL 顺序、审计双写的两条记录、`available-model-providers` 属 admin 组。

### 证据二：接口实测（Go 与 FastAPI 逐字段 diff）

`compare36.py`：**11 场景 / 89 步 / 失败 0 / 双端逐字段完全一致**

```
基线：tenants=4 providers=6 menus=50 role_menu=47 audit_max=793
[ok] 读接口：租户列表 / 详情 / 提供商 / 可用提供商 / 全量菜单（12 步）
[ok] 权限边界：租户/子账号访问平台域 403（7 步）
[ok] 租户：创建 → 详情 → 改名 → 配额 → 启停 → 重置密钥 → 列表 → 删除（11 步）
[ok] 租户：参数校验与不存在的分支（12 步）
[ok] 租户：平台代配模型配置（读 / 改 / 幂等）（4 步）
[ok] 模型提供商：创建 → 列表 → 更新 → 删除 → 回读（8 步）
[ok] 模型提供商：必填 / 无效 ID / 空更新 / 重复键（8 步）
[ok] 菜单：创建 → 列表 → 更新 → 删除 → 回读（6 步）
[ok] 菜单：参数校验 / 父节点 / 重复权限码 / 批量更新（15 步）
[ok] 菜单：删除父节点级联子节点与角色关联（4 步）
[ok] 菜单：重置内置菜单并回读（2 步）
场景 11，步骤 89，失败 0
```

还原纪律：`tenants` / `model_providers` / `menus` / `sys_role_menu` 四张表每场景前后**全量快照还原**（含 `AUTO_INCREMENT` 回拨，保证两端自增 id 一致），`tickets` 软删还原，`audit_logs` 按「五元组 + 条数增量」比对后删除探针行。

> 踩过的坑（如实记录）：`compare36.py` 最初默认用 `tid="t_demo"` 的令牌，平台域接口全部 403 —— 89 步等于什么都没验证，脚本却打印「双端一致」。是 `probe_scene3.py` 把每一步响应原样打出来才发现的。已把默认令牌改为 `t_admin`，权限边界场景里再显式标注 `"tid": "t_demo"`。**比对脚本必须能打印每一步的原始响应，否则假阴性无法察觉。**

### 证据三：浏览器实测

`cs_stage36_accept.mjs`（**28 项，全 PASS**）：

```
PASS  租户管理员登录成功 hash=#/ops
PASS  菜单树仍走 FastAPI(py=1 go=0)
PASS  平台管理员身份生效（侧边栏出现「系统管理」）
PASS  平台菜单树走 FastAPI(py=1)
PASS  租户页渲染数据行 / 租户列表走 FastAPI(py=1) / 未请求 Go 旧入口(go=0)
PASS  租户页展示配额用量列（会 N/M 消 N/M）
PASS  租户筛选为前端本地过滤（不额外请求 req=0）
PASS  筛选后仅显示启用租户（badges=启用）
PASS  新建租户弹窗打开 / 提交新建租户成功
PASS  创建租户走 FastAPI(POST) / 未请求 Go 旧入口(go POST=0)
PASS  新租户出现在列表
PASS  删除租户走 FastAPI(DELETE) / 删除后列表不再出现该租户
PASS  模型提供商页渲染 6 行 / 列表走 FastAPI / 未请求 Go / 渲染内置条目
PASS  菜单权限页渲染内置节点 / 全量菜单走 FastAPI / 未请求 Go
PASS  menus 与 menus/tree 同层（无跨层混跑 go menus*=0）
PASS  新层不可用时租户页仍渲染 / 先尝试新层(new=1) / 自动回退 Go 旧入口(go=1)
```

最后一项「…同层（无跨层混跑）」是 3-5 遗留的伏笔：3-5 只登记了 `/api/admin/menus/tree`，本批登记 `/api/admin/menus` 后，两条路径必须同时命中新层，不能出现 `/menus/tree` 走 FastAPI 而 `/menus` 走 Go 的混跑。

> 一处验收脚本自身的 bug（已修）：`筛选后仅显示启用租户` 最初写成 `!row.innerText.includes('禁用')`，一直 FAIL。根因不在产品代码——`Tenants.vue` 第 54 行，**启用行**的操作按钮文案就是「禁用」（切换动作），整行文本必然含这两个字。改为读状态徽章 `.badge` 并加轮询等待重渲染后通过。

回归：`compare33.py` 41/41、`compare34.py` 35/35、`compare35.py` 8 场景 58 步、`cs_stage33_accept.mjs` 22/22、`cs_stage34_accept.mjs` 17/17、`cs_stage35_accept.mjs` 25/25 全部仍通过。

### 前端切换

```js
// 阶段 3-6：平台域（租户 / 模型提供商 / 全量菜单）
'/api/admin/tenants',
'/api/admin/model-providers',
'/api/admin/menus',
'/api/admin/available-model-providers'
```

`migratedToNewLayer` 的匹配规则是「整段相等 / 前缀 + `/`」，所以：

- `/api/admin/tenants` 会连带命中 `/tenants/{tid}`、`/tenants/{tid}/quota`、`/tenants/{tid}/model-config` 等全部子路径；
- `/api/admin/menus` 与上批已登记的 `/api/admin/menus/tree` 同时命中，符合预期；
- `/api/admin/model-providers` 与 `/api/admin/available-model-providers` 是两条独立前缀，**必须分别登记**——前者不以 `/` 结尾，不会连带命中后者。

### 遗留 / 已知缺陷

| 项 | 说明 | 何时处理 |
|---|---|---|
| 3 个 Go 内部接口 | `menus/reload`、`tenants/reload`、`ws/conns` 纯为并行期缓存一致性而加 | Go 下线时删除 |
| 缓存失效是 best-effort | `go_internal` 最多重试 2 次，全失败只丢一行日志不阻断。Go 正在重启时可能出现「库里已改、Go 内存未变」的短暂窗口 | 并行期可接受；单进程后不存在 |
| 租户容量校验依赖 Agent | `max_kb_docs` / `max_kb_size_mb` 的实际占用仍由 Agent 侧 `ProxyToAgent` 提供，Go/FastAPI 都只是转发 | 3-7 一并处理 |
| 一次性令牌缺省 `kind` | 管理令牌的 claims 里 **`kind` 必须缺省**（不能写 `"kind": ""`），否则 Go 侧按「租户令牌」解析导致行为偏差 | 照抄，已固化在验收脚本 |
| 删除租户的异步清理不保证完成 | Agent 会话清理与 ws 踢线是 daemon 线程，进程退出即丢 | 并行期可接受；后续可改为任务队列 |
| UI 验收残留软删行 | 浏览器验收「建租户 → 删租户」走的是真软删，每轮在 `tenants` 表留一行 `deleted_at != 0` 且 `name='UI探针租户'` 的记录（当前 2 行：`t_tk_a6ca6e93d83a4e58`、`t_tk_9fef381cc8b3d174`）。不影响接口（列表过滤 `deleted_at = 0`），但会让 `tenants` 全表计数偏离 | 需要时手工 `DELETE` 清理，已在此登记 |

---

## 阶段 3-7：代理域（Prompt / 知识库 / 平台设置 / 模型连通性测试）（2026-10-20）

### 迁移范围

| Go 端点 | 分组 | 处理 | 权限 |
|---|---|---|---|
| `GET /api/admin/prompts` | biz | 迁 → 同名 | `prompt:view` |
| `GET /api/admin/prompts/active` | biz | 迁 → 同名 | `prompt:view` |
| `POST /api/admin/prompts` | biz | 迁 → 同名 | `prompt:add` |
| `POST /api/admin/prompts/:key/activate` | biz | 迁 → 同名 | `prompt:edit` |
| `DELETE /api/admin/prompts/:key` | biz | 迁 → 同名 | `prompt:remove` |
| `POST /api/admin/prompts/:key/versions` | biz | 迁 → 同名 | `prompt:edit` |
| `GET /api/admin/prompts/:key/versions` | biz | 迁 → 同名 | `prompt:view` |
| `POST /api/admin/prompts/:key/versions/:vid/rollback` | biz | 迁 → 同名 | `prompt:edit` |
| `POST /api/admin/kb/upload` | biz | 迁 → 同名 | `knowledge:add` |
| `GET /api/admin/kb/documents` | biz | 迁 → 同名 | `knowledge:view` |
| `DELETE /api/admin/kb/documents/:doc_id` | biz | 迁 → 同名 | `knowledge:remove` |
| `POST /api/admin/kb/documents/:doc_id/category` | biz | 迁 → 同名 | `knowledge:edit` |
| `POST /api/admin/kb/documents/:doc_id/reindex` | biz | 迁 → 同名 | `knowledge:edit` |
| `POST /api/admin/kb/index` | biz | 迁 → 同名 | `knowledge:edit` |
| `GET /api/admin/kb/categories` | biz | 迁 → 同名 | `knowledge:view` |
| `POST /api/admin/kb/batch` | biz | 迁 → 同名 | `knowledge:edit` |
| `GET /api/admin/platform-settings` | **admin** | 迁 → 同名 | 登录即可 |
| `PUT /api/admin/platform-settings` | **plat** | 迁 → 同名 | 平台管理员 |
| `POST /api/admin/model-config/test` | **admin** | 迁 → 同名（**本地重写**） | 登录即可 |

累计 admin 已迁 74 + 19 = **93 条**，admin 组路由迁移**全部完成**。

> 这三批分组不一致的地方又多了一处：`platform-settings` 的 **GET 挂 admin 组、PUT 挂 plat 组**（读设置给所有人，写设置只给平台管理员）；`model-config/test` 也在 admin 组。按「名字看着像平台配置就挂 plat」写会连带出 403。

### 这一批和前六批的性质差别

前六批是「把 Go 的业务逻辑用 Python 重写一遍，逐字段对齐」。这一批 19 条里有 18 条在 Go 侧**本来就没有业务逻辑**——它只做「鉴权 → 审计 → 把请求原样转发给 Python Agent → 把 Agent 的响应原样吐回去」。

所以迁移的目标不是重写业务，而是**复刻转发语义**。翻成一句话：

| Go 侧行为 | 迁移后 |
|---|---|
| `io.ReadAll(c.Request.Body)` 后原样重发 | 读 raw body，**不解析不重编码** |
| `c.Request.URL.RawQuery` 原样拼到 Agent URL | 原样透传（**不重排序、不重编码**） |
| 三个头：`X-Trace-Id` / `X-Tenant-Id` / `X-Internal-Token` | 逐字对齐，且 `tenant` 为空时**不设该头** |
| `c.Data(200, "application/json", data)` | 见下面「最大的坑」 |
| 连不上 Agent → `{"code":500,"msg":"调用agent失败:<err>"}` | 同文案，但 HTTP 状态由 body code 决定 |

### 本阶段最大的坑：代理响应必须复刻 StatusCode 中间件

这一条踩了很久，值得单独写。

Go 的 `ProxyToAgent` 写的是 `c.Data(http.StatusOK, "application/json", data)` —— 看起来永远是 200。但项目里还有一个**全局 StatusCode 中间件**，它在响应写出后按 `body.code` 把 HTTP 状态改写成本地码：

```
code >= 500        → 500
400/401/403/404/405/409/429 → 原样
其余 4xx           → 400
< 400              → 不改（200）
```

于是端到端看到的真实行为是：

| Agent 返回 | 对外 HTTP | 对外 body |
|---|---|---|
| `{"code":0,...}` | 200 | 原样 |
| `{"code":400,"msg":"doc_ids 不能为空"}` | **400** | 原样 |
| `{"code":403,"msg":"..."}` | **403** | 原样 |
| `{"code":404,"msg":"prompt 不存在"}` | **404** | 原样 |
| `{"code":500,"msg":"调用agent失败:..."}` | **500** | 原样 |
| 纯文本 `Internal Server Error` | **200** | 原样（非 JSON 不改写） |

我第一版把 18 条代理全写成固定 200，双端比对立刻在 `kb/batch`（400）和 `?scope=platform`（403）两处暴露。修法是加一个统一出口：

```python
def agent_payload_response(data):
    """按 body.code 改写状态；非 JSON 响应体不改写。"""
    ... code_to_status(code) ...
```

顺带修掉了一个更早的隐患：`agent_call` 的 timeout 原本写死 60s，而 Go 管理台透传用的是 `adminHTTPClient = 120s`。现在 `proxy_to_agent` 默认 120s，与 Go 一致。

### 本轮补上的三块基础设施

**① `BusinessOnly` 的 prompts 白名单（一个必须照抄的「不一致」）**

`require_perm(..., biz_scope=True)` 表达「biz 组拒绝平台管理员」。但 Go 的 `BusinessOnly()` 里有一个硬编码例外：

```go
if ok && info.IsPlatformAdmin {
    // 平台管理员仅可访问平台级 Prompt 管理接口
    if strings.HasPrefix(c.Request.URL.Path, "/api/admin/prompts") {
        c.Next(); return
    }
    // 其余一律 403
}
```

不照抄这个白名单，`ProxyPrompt` 里 `?scope=platform` 的整条分支就**永远不可达**——因为能走到它的只有平台管理员，而平台管理员在进 handler 之前就被 403 了。

所以 `require_perm` 的守卫改成了带 `request` 的签名：

```python
def guard(request: Request, admin: Admin = Depends(current_admin)) -> Admin:
    if biz_scope and admin.is_platform_admin:
        if not request.url.path.startswith("/api/admin/prompts"):
            deny(403, "平台管理员不可访问租户业务数据，请使用对应租户账号")
```

**验证方式**：平台管理员访问 `/api/admin/prompts*` 全部 200，访问 `/api/admin/kb/*` 全部 403 且文案逐字对齐。

**② `__platform__` 作用域与 multipart 重组**

平台级 Prompt 在 Agent 库里是 `tenant_id=''` 的全局行，Go 用特殊标识 `__platform__` 表达它：

```python
async def proxy_prompt(request, method, agent_path, admin):
    if request.query_params.get("scope") == "platform":
        if not admin.is_platform_admin:
            return JSONResponse(code_to_status(403), {"code":403,"msg":"仅平台管理员可管理平台级（全局）Prompt"})
        return await proxy_to_agent(..., PLATFORM_SCOPE)      # X-Tenant-Id: __platform__
    return await proxy_to_agent(..., admin.tenant_id)
```

`POST /kb/upload` 则是**重新组装** multipart，不是原样透传：Go 用 `multipart.NewWriter` 只写 `file` / `title` / `category` 三个 part，且 title/category 为空也照写空串。Python 用 httpx 的 `files=` + `data=` 复刻同样的形状（boundary 必然不同，Agent 按 part 名解析，不影响结果）。

**③ `resolve_chat_config` / `resolve_emb_config`：模型配置的四级解析**

`POST /model-config/test` 是本批唯一有本地逻辑的一条——它不碰 Agent，直接用表单里的模型名/Base/Key 打一次第三方 OpenAI 兼容接口。表单留空时要回退读已存配置，而「已存配置」不是表里的裸字段，是**解析后**的结果：

```
1. llm_provider_id > 0  → 取 model_providers 的 default_model/default_api_base，
                           被租户字段覆盖；Key 取本行 model_api_key（空=未配置，继续往下）
2. model_provider='custom' → 兼容旧版手填（Key 与模型名/Base 齐全才算有效）
3. 跟随平台 → 递归解析 t_admin（最多 3 层，超出报「递归过深」）
4. t_admin 也未配置 → 取第一个启用的同类提供商兜底
```

这段逻辑是 `resolveChatConfig` / `resolveEmbConfig` 的等价物，本阶段首次落到 Python（3-6 的 `get_business_model_config` 只做脱敏回显，不做解析）。

### 复刻到的几处「非显然」Go 行为

| 行为 | 细节 |
|---|---|
| `X-Tenant-Id` 空则不设 | `if tenantID != ""` 才 `Header.Set`，不是设成空串。空串与缺省在 Agent 侧语义不同 |
| Agent 的 HTTP 状态被丢弃 | Go 只读 body，不检查 `resp.StatusCode`。Agent 回 500 + body `{"code":0}` → 对外 200 |
| `model-config/test` 的回退来源默认是 `t_admin` | `tid := c.Query("tenant_id"); if tid == "" { tid = "t_admin" }` —— 不是调用者自己的租户。app_key 管理员恰好属于 `t_admin` 才「碰巧」对 |
| `use_custom` 用对话侧口径卡 embedding | `if cfg.UseCustom { if type=="chat" {...} else {...Embedding...} }` —— 一个开关管两种类型。「对话跟随平台、Embedding 自有」的租户测 embedding 时不回填 |
| 校验顺序是 key → name/base | 全空时报的是「请填写 API Key 后再测试」，不是「模型名称与 API Base 不能为空」 |
| `type` 只认 `chat` / `embedding` | 其他值报「type 必须是 chat 或 embedding」，在 key 校验**之后** |
| `DELETE /prompts/:key` 不清 `prompt_versions` | Agent 侧的既有行为：Prompt 删了，历史版本行还在。双端一致（都不是我们引入的），但跨端串跑会互相污染，比对脚本必须手动清 |
| `kb/batch` 的校验在 Agent 侧 | `action` 非法 / `doc_ids` 空 → `{"code":400}`，Go 只负责把它改写成 HTTP 400 |
| 失败文案含底层错误串 | 「调用agent失败:」前缀后的部分是 `dial tcp: lookup x on 127.0.0.11:53: no such host`（Go）vs httpx 的错误串，**两端不可能逐字相同** |

### 证据一：测试通过

```
python -m unittest discover -s tests -t tests   →  Ran 250 tests, OK
```

其中 `tests/test_stage37_contract.py` 新增 **30 条**（累计 250）。断言覆盖：

- 18 条代理路由的鉴权分组、`prompts` 白名单、子账号权限码
- 请求侧 query/body/Content-Type/三个响应头的逐一透传
- 响应侧「Agent 状态码被丢弃 + body.code 改写 HTTP 状态 + 非 JSON 不改写」
- `scope=platform` 的软 403 与 `__platform__` 租户注入
- multipart 重组（含空 title/category 照写）与无文件时的 JSON 透传
- `model-config/test` 的校验顺序、回退、五类失败分支

### 证据二：接口实测（Go 与 FastAPI 转发语义 diff）

`compare37.py`：**9 场景 / 44 个请求步骤 / 2 项终态校验 / 失败 0**

```
基线（Agent）：docs=2 prompts=0 settings=1 prompt_versions=0 prompts_rows=1
[ok] 读接口：prompts / prompts-active / kb-documents / kb-categories / platform-settings
[ok] 权限边界：platform 作用域 403 / biz 组 403 / plat 组 403
[ok] Prompt：创建 → 版本 → 激活 → 删除 → 复位
[ok] 知识库：JSON 上传 / multipart 上传 / 删除 / 复位
[ok] 知识库：batch 校验 / index 幂等
[ok] 平台设置：校验分支 / 写入探针键 / 回读
[ok] 模型连通性测试：required / 非法 type / 缺 key / 缺 name-base
[ok] 写操作审计双端一致
[ok] Agent 侧数据复位到基线
```

这一批的比对策略与前六批不同，值得说明：**两端转发到的是同一个 Agent**，所以比的是「同一份请求经 Go 与经 FastAPI 送进 Agent，拿回来的字节是否一样」，而不是两边各自算出的数据。

写接口的处置：

- Prompt 写链路：跑完当场 `DELETE /prompts/:key` 复原，再换另一端跑同一脚本
- 知识库上传：JSON 与 multipart 各传一次，取 `doc_id` 后立刻删除
- 平台设置：写一行非特殊键 `probe_stage37`，跑完从 PG 删除；最后核对 `platform_settings` 行数回归
- 归一化：`prompt_versions.id` 自增序列、时间戳、32 位十六进制 `doc_id`/`file_url` 一律打码后再 diff

> 踩过的两个脚本自身的坑（如实记录）：
> ① `prompt_versions.id` 在第二端会 +1（因为第一端跑过），是纯序列产物不是语义差异 → 每端开跑前 `setval` 回拨。
> ② `DELETE /prompts/:key` 不清 `prompt_versions`（Agent 既有行为），不清残留会导致第二端的「创建版本」变成 version 2 → 每端开跑前按 `prompt_key` 清一次。

### 证据三：浏览器实测

`cs_stage37_accept.mjs`（**34 项，全 PASS**）：

```
PASS  租户管理员登录成功 hash=#/ops
PASS  菜单树仍走 FastAPI(py=1 go=0)
PASS  知识库页渲染文档行 / 列表走 FastAPI / 未请求 Go / 分类走 FastAPI / 未请求 Go 的 kb 旧入口
PASS  知识库页展示分片/命中信息
PASS  Prompt 页渲染 / 列表走 FastAPI / 未请求 Go 旧入口
PASS  新建 Prompt 弹窗打开 / 提交成功 / 创建走 FastAPI(POST) / 未请求 Go
PASS  新 Prompt 出现在列表
PASS  删除 Prompt 走 FastAPI(DELETE) / 删除后列表不再出现
PASS  新层不可用时知识库页仍渲染 / 先尝试新层(new=1) / 自动回退 Go 旧入口(go=1)
PASS  平台管理员身份生效（侧边栏出现系统管理）
PASS  平台级 Prompt 走 FastAPI(scope=platform) / 未请求 Go / 渲染全局默认 Prompt
PASS  平台设置页渲染 / 走 FastAPI(GET) / 未请求 Go
PASS  保存平台设置走 FastAPI(PUT) / 未请求 Go
PASS  模型配置页请求走 FastAPI / 连通性测试走 FastAPI(POST) / 未请求 Go / 结果已渲染
```

这一轮特意把**知识库上传**放进了浏览器验收吗？没有——上传需要真实文件选择器，headless 下走 `DOM.setFileInputFiles` 太重，改用 `compare37.py` 的 multipart 直连覆盖，浏览器侧只验「列表/分类/新建/删除」这几条高频路径。

> 三个验收脚本自身的坑（如实记录，都不是产品缺陷）：
> ① 切到平台管理员后 hash 还是 `#/prompts`，再赋同一个值**不会触发路由变化**，数据在 `sleep` 期间就已加载完，标记点取晚了 → 改成「先 `#/ops` 再 `#/prompts`」。
> ② 回退场景原本放在平台管理员身份下跑，但平台管理员侧边栏**没有知识库菜单**，路由不存在 → 页面空转、一个请求都不发 → 把回退场景移回租户身份段。
> ③ 「渲染内置 default」断言写错了：Prompt 表格列只有 标题/内容预览/更新时间，**key 不展示** → 改成按标题「默认客服Prompt」+「生效中」徽章断言。

### 前端切换

```js
// 阶段 3-7：代理域（Prompt / 知识库 / 平台设置）
'/api/admin/prompts',
'/api/admin/kb',
'/api/admin/platform-settings'
```

同时把 `PY_ADMIN_EXCLUDE` 清空——3-4 时登记过的 `/api/admin/model-config/test` 现在已迁入新层，而它本来就被 `/api/admin/model-config` 前缀命中，不需要单列。

### 遗留 / 已知缺陷

| 项 | 说明 | 何时处理 |
|---|---|---|
| 代理失败文案不逐字 | 「调用agent失败:」后面是底层 HTTP 客户端的错误串（Go 的 `dial tcp: lookup ...` vs httpx 的 `All connection attempts failed`），只能保证前缀一致 | 无法对齐，也不必对齐 |
| `DELETE /prompts/:key` 留孤儿版本 | Agent 侧删 Prompt 不清 `prompt_versions`，历史版本行永久残留。两端一致 | 照抄；建议后续在 Agent 侧加级联删除 |
| `model-config/test` 会真实调用第三方 | 点一次「测试模型可用性」就是一次真实的 LLM/Embedding 请求（chat 用 `max_tokens:5`，embedding 用 `input:"test"`），会计费 | 设计如此 |
| `model-config/test` 回退默认读 `t_admin` | 前端传 `params: { tenant_id: tid }` 才读对租户；直接调接口不传 → 读平台配置 | 照抄 |
| 上传为 multipart 重组而非字节透传 | boundary 与 Go 不同（同一份文件两端的 multipart 字节不等），只保证 part 结构一致 | 无影响 |
| 审计仍双写 | 代理类写操作在 `audit_logs` 里由中间件按路径推断一行（`object_type` 多为 `other`）；本批没有 handler 级第二行（Go 也没写） | 照抄 |
| UI 验收残留软删行 | `tenants` 现有 10 行（2 活 + 8 条 `UI探针租户` 软删）、`users` 现有 2 行（均 `kefu_ui_probe` 软删）。软删行对所有查询不可见（都带 `deleted_at = 0`），只是全表计数会偏离 | 需要精确计数时按 `deleted_at = 0` 过滤 |
| `platform_settings` 多一行 | 浏览器验收点「保存」写入 `kb_max_file_mb=10`，与代码默认 `DEFAULT_MAX_FILE_MB = 10` **完全一致**，无行为变化 | 保留 |

---

## 阶段 3-8：补齐「照抄 Go 的缺陷」（2026-10-21）

### 这一阶段的性质

前七个阶段的目标是「**逐字节复刻 Go**」——包括复刻它的缺陷，避免切层瞬间改变前端展示。
阶段 3-8 反过来：**把已经确认是缺陷的行为修掉**。

因此这一阶段的所有改动都是**有意偏离 Go**，双端 diff 在这些点上必然不一致。
验证方式也随之改变：不再跑 `compare3x.py` 的双端比对，而是跑
`compare38.py` 做**单端验证**——直接查库证明落库结果正确。

### 修复清单（7 项）

| # | 缺陷 | Go 侧表现 | 修复方式 |
|---|---|---|---|
| 1 | **模型配置掩码回填**（高危） | `MaskAPIKey` 对长 Key 输出 `"..." + 后4位`（**不含星号**），而 `isMaskedKey` 只查 `*` → 用户打开配置页只改模型名、点保存，Key 被静默改写为掩码字面量，之后所有 LLM 调用失败 | `is_masked_key(value, stored_plain)` 增加 `"..." + 4位` 形态识别，并用已存 Key 明文做二次兜底 |
| 2 | 客户列表 `message_count` 恒 0 | Go 的 query 取 `msg_count`，`model.CustomerItem` 字段映射 `message_count`，GORM `Scan` 匹配不上 | 加子查询按 thread 统计 `messages` + `messages_archive` |
| 3 | 会话列表 `handoff=1` 时 `total` 虚高 | Go 用 `Distinct().Count()`，只带 `where`、漏了 `handoffFilter` → 筛选后 total 远大于实际行数，分页控件错乱 | total 包进子查询，与列表共用同一个 `HAVING` |
| 4 | 提取规则先删后校验 | Go 先 `DELETE FROM platform_config` 再逐条校验，任一条非法就留下「旧规则已删、新规则只写了一部分」的中间态 | 先把整批校验完（含正则编译），全部通过才落库 |
| 5 | 关闭工单 `old_status` 写死 `Handling` | 关闭一个「待处理」工单，日志显示 `handling → closed`，与实际状态流转不符 | 取关闭前的真实状态 |
| 6 | 角色 `code` 不可指定 | Go handler 写死 `Code: genRoleCode()`，请求体的 `code` 被直接丢弃 | 放开：传了就用（限长 32，与列宽一致），没传才自动生成 |
| 7 | 看板 `satisfaction_rate` NULL 扫描 | Go 的聚合 SQL 在租户无评价时产出 NULL，`Scan` 到 `float64` 直接报错 → **整个看板 500** | Python 侧本就免疫（None → 0.0），**无需改代码**，仅登记 |

### 过程中发现并修掉的一个真 bug（值得单独记）

第 3 项的第一版写法把 `HAVING` 包进了子查询，但**漏了 `tickets` 的 JOIN**：

```
(1054, "Unknown column 't.status' in 'having clause'")
```

`handoff=1` 直接 500。**单测全是绿的**——因为单测 mock 掉了 `one()`，
根本没碰真实 SQL。是 `compare38.py` 打真实库才暴露的。

补救：在单测里加了 `test_session_total_subquery_has_tickets_join`，
断言 total 子查询必须带齐 `LEFT JOIN tickets t` / `LEFT JOIN customer_profiles cp`，
且主查询的 JOIN 集合是子查询的超集。

> 教训：**mock 掉 DB 的单测能锁逻辑，锁不住 SQL 结构**。
> 凡是新写/改写的聚合 SQL，必须有一条打真实库的用例。

### 顺带修掉的两个「测试自身的问题」

| 问题 | 说明 |
|---|---|
| `test_stage34` 的看板测试硬编码 `trend[-1]` | 跨天后 `trend[-1]` 变成「今天」，而数据是昨天的 → 10-20 写的测试 10-21 就跑红。改为按日期字典定位 |
| `compare38.py` 的 SQL 里裸 `%` | pymysql 把 `LIKE 'cs_extract_rule_%'` 的 `%` 当占位符 → `not enough arguments`。改为 `%%` |

### 证据一：测试通过

```
python -m unittest discover -s tests -t tests   →  Ran 273 tests, OK
```

新增 `tests/test_stage38_fixes.py` **23 条**（累计 273）。同时更新了 3 个旧测试文件里
**锁住缺陷行为**的断言（`test_stage32` × 3、`test_stage34` × 2）。

### 证据二：接口实测（单端，`compare38.py`）

```
合计 14 项，通过 14，失败 0

[①] 客户列表 message_count
    PASS  客户列表返回真实 message_count  rows=1 非零=1
    PASS  message_count 与 DB 计数一致  t_demo:buyer1:... api=2 db=2
[②] 会话列表 handoff total
    PASS  handoff=1 时 total == DB 同条件计数  api=23 db=23
    PASS  无筛选时 total == DB 全量计数  api=128 db=128
    PASS  size 超限被 clamp 且 total 仍为全量  size=20 total=128
[③] 提取规则
    PASS  非法正则返回 HTTP 500 且旧规则未被删除  before=1 after=1
    PASS  合法规则正常写入
    PASS  提取规则已还原到基线
[④] 关闭工单 old_status
    PASS  关闭待处理工单日志 old_status = 0（待处理）
[⑤] 角色 code
    PASS  请求体 code 生效  row={'id': 8, 'code': 'fix38_code', ...}
    PASS  code 超长被拒绝（HTTP 400）
    PASS  不传 code 时自动生成  code=custom_b5ab8107cd8
    PASS  测试角色已清理
[⑥] 模型配置掩码
    PASS  回传掩码不覆盖已存 Key  masked='...vZOA' unchanged=True
```

**回归**（证明修复没有波及其他阶段）：

```
compare33.py   41/41 一致
compare34.py   35/35 一致
compare35.py   8 场景 / 58 步，双端逐字段完全一致
compare36.py   11 场景 / 89 步，失败 0
compare37.py   9 场景，失败 0
cs_stage33_accept.mjs  22 项，FAILED 0
cs_stage34_accept.mjs  17 项，FAILED 0
cs_stage35_accept.mjs  25 项，FAILED 0
cs_stage36_accept.mjs  28 项，失败 0
cs_stage37_accept.mjs  34 项，失败 0
```

### 数据还原

本轮跑完清理了 4 行自产数据（**均为 `compare38.py` 自身产生，非业务数据**）：

| 表 | 行 | 来源 |
|---|---|---|
| `cs_biz_db.ticket_logs` | id 110 / 111 / 112 | compare38 三次运行各关闭一次工单 |
| `cs_biz_db.platform_config` | id 39（`cs_extract_rule_baseline`） | compare38 种入的基线行 |

工单 2 的 `status` 已还原为 `0`（待处理）。还原后基线：

```
tenants_all=11（2 活 + 9 条软删 UI 探针）  tenants_live=2
roles=2  sys_role_menu=47  menus=50
ticket_logs=34  extract_rules=0
```

### 相关文档

- `docs/GO_RETIREMENT.md` —— Go 下线清单（本轮同时产出，见下一节）

---

## 阶段 3-9：Go 下线清单步骤 1 —— 摘除 admin 组 93 条路由（2026-10-21）

### 这一阶段的性质

前八个阶段都在**加**东西（把 Go 的能力迁到 FastAPI）。这一阶段第一次**减**：
执行 `docs/GO_RETIREMENT.md` 步骤 1，把已确认无流量的 admin/biz/plat 三组
共 93 条路由从 `go_service/internal/router/router.go` 摘除。

摘除的直接后果：`/api/admin/*` 从此由 gin 无路由流程处理（404）。
本轮最重要的发现是——**这个 404 一开始拿不到**，被 Go 自己的
`StatusCode` 中间件吃掉了（见下节）。

### 改动清单

| # | 文件 | 改动 |
|---|---|---|
| 1 | `go_service/internal/router/router.go` | 摘除 admin/biz/plat 三组全部 93 条注册（-173 行）；删除只被 admin 组使用的 `internal/pkg/permission` import；全局链注册 `RetiredAdminPaths` 观测中间件 |
| 2 | `go_service/internal/middleware/retired_admin.go` | **新增**。只记录不拦截：命中 `/api/admin*` 打 `RETIRED_ADMIN_PATH` Warn 日志（method/path/status/client_ip/query/是否带令牌/trace_id），响应零影响。摘除后持续发现漏网流量，观测期结束可删 |
| 3 | `go_service/internal/middleware/status_code.go` | **修复潜在缺陷**：无路由 404 不再被改写为 200 空体（详见下节） |
| 4 | `go_service/internal/middleware/status_code_test.go` | **新增 7 条**锁定修复后行为 |

`RequirePerm` / `BusinessOnly` / `PlatformAdminOnly` / `AuditMiddleware`
四个中间件实现**保留未删**：它们已被单测 `auth_middleware_test.go` 引用
（自建路由做边界测试），删实现要连带改测试，按清单留到测试重构时一起删。
同理 `reload_go_menu_caches()`（FastAPI 侧 5 处调用）现在已是死代码，
删除动作登记在 GO_RETIREMENT.md 第 4 节 ⚡ 行，本轮不执行。

### 过程中发现并修掉的一个真 bug（值得单独记）

摘除后 `curl http://localhost:8080/api/admin/tenants` 返回 **200 +
Content-Length: 0**——而验收标准要求 404。Go 日志里观测中间件明明看到 404，
`RequestLogger` 却记 200。根因在 `status_code.go` 的包装器假设：

1. gin 无路由流程是 `c.handlers = allNoRoute; serveError(c, 404, ...)`，
   **整个全局中间件链跑在 `serveError` 内部的 `c.Next()` 里**；
2. `serveError` 直接写 `c.writermem.status = 404`——**绕过** `statusWriter`
   的 `WriteHeader`（包装器 `statusCode` 字段停在初始值 200，`written=false`）；
3. 中间件退栈时 `StatusCode` 后处理跑在 gin 补写错误页**之前**：缓冲区为空、
   解析不到 JSON → `flushAs(200)` 提前落盘 200 空体；
4. gin 回头看到 `writermem.Written()==true`，跳过默认错误页。

这是**潜在缺陷**（`/api/nonexistent` 一直都中招），只是摘除前没有无路由
流量所以没暴露——**下线动作本身把它逼现了**。

修复：非 JSON 体按底层真实状态透传：

```go
if err := json.Unmarshal(w.buf.Bytes(), &payload); err != nil {
    if st := w.ResponseWriter.Status(); st != http.StatusOK {
        if w.buf.Len() == 0 {
            w.passthrough = true   // gin 稍后补写错误页，进透传不提前 flush
            return
        }
        w.flushAs(st)             // 已有正文：按底层状态连正文落盘
        return
    }
    w.flushAs(http.StatusOK)
    return
}
```

新增 `status_code_test.go` 7 条：JSON code 改写保持 / handler 真实状态透传 /
纯文本 200 保持 / **无路由 404 + `404 page not found` 正文** /
外层中间件（模拟 RequestLogger）观测到 404 / `/ws/` 跳过改写。

> 教训：包装 `c.Writer` 的中间件，锁不住 gin 内部直接操作 `c.writermem` 的路径
> （serveError、redirect）。「底层已有非 200」必须是一等公民。
> 另一个附带教训：**本机有 HTTP 代理（`http_proxy=127.0.0.1:56736`）**，
> curl 不走 `--noproxy` 会吃到代理缓存的旧 Go 200——这正是验收脚本
> `httpc.py` 用裸 socket 的原因，手动验证也必须 `--noproxy '*'`。

### 验收脚本适配（10 个）

compare33~37 与浏览器 33~37 原本按「双端都在服务」编写。退役后 Go 侧
admin 探测的预期结果变为 404，逐个打了适配补丁（脚本在
`%TEMP%/cs_verify/`，非仓库文件）：

| 脚本 | 适配方式 |
|---|---|
| compare33 / compare34 | `case()`/`record()` 识别 Go 侧 404 退役签名 → 记 PASS 并标注「Go 已退役，仅 FastAPI 在服务」 |
| compare35 / compare36 | `run_side()` 见到退役签名后短路后续步骤（合成同签名响应，避开 capture 取空值崩溃）；`scenario()` 跳过退役步骤 diff，并断言 Go 侧 DB 状态与基线完全一致（无写入） |
| compare37 | `run_pair()` 跳过退役步骤；`baseline_counts()` 改读 FastAPI；审计探针改断言「Go 不留痕、FastAPI 正常留痕」 |
| 浏览器 33~37 | 回退段断言从「页面经 Go 回退仍渲染」改为 HTTP 级：「Go 旧入口已摘除返回 404」+「Go 旧入口无业务数据（gin 默认 404 页）」；「先尝试新层 / 自动回退 Go 旧入口」保持（证明前端回退逻辑仍在尝试、只是已无数据可回落） |

> 渲染级断言（页面不显示旧数据）在 stage34 被弃用：审计页此前已加载过，
> SPA 组件缓存保留「共 N 条」，那是页面自身缓存而非 Go 回退数据，
> 渲染级断言既脆又与退役契约无关。退役的正确证据在 HTTP 层。

### 证据一：测试通过

```
go test ./...（一次性 golang:1.25-alpine 容器，挂载 go_service + cs_gomod 缓存卷）
  ok  customer_service/internal/handler
  ok  customer_service/internal/middleware   （含新增 status_code_test.go 7 条）
  ok  customer_service/internal/pkg/auth

python -m unittest discover -s tests   →  Ran 273 tests, OK   （python_business 未改动，留证）
```

### 证据二：接口实测

```
curl --noproxy '*'（10 个 admin 路径全部 404 + "404 page not found" 正文）：
  /api/admin/tenants|menus/tree|tickets|prompts|ops/dashboard|model-providers
  |extract-rules|members|roles|sessions                     → 全部 404
非 admin 路径保持原状：
  /api/auth/tenants 200  /api/auth/public-key 200
  /api/business/order 410  /metrics 200
  /ws/chat 401（无升级头，路由仍在）  /ws/admin 401

compare33.py   41/41 一致（Go 侧逐条 404 退役签名，FastAPI 侧全量功能）
compare34.py   35/35 一致
compare35.py   8 场景 / 58 步，双端逐字段完全一致
compare36.py   11 场景 / 89 步，失败 0
compare37.py   9 场景，失败 0（含「Go 已退役不留痕，FastAPI 正常留痕」
             与「Agent 侧数据复位到基线」）
compare38.py   14/14（未打补丁，纯 FastAPI 端，行为与上轮一致）
```

Go 日志三层印证（观测中间件 / RequestLogger / gin 自身）均为 404：

```
RETIRED_ADMIN_PATH ... path":"/api/admin/tenants","status":404
HTTP请求客户端错误 ... path":"/api/admin/tenants","status":404
[GIN] | 404 | GET "/api/admin/tenants"
```

### 证据三：浏览器实测

```
cs_stage33_accept.mjs  23 项，FAILED 0
cs_stage34_accept.mjs  18 项，FAILED 0
cs_stage35_accept.mjs  26 项，FAILED 0
cs_stage36_accept.mjs  29 项，失败 0
cs_stage37_accept.mjs  35 项，失败 0
```

每个脚本的回退段均确认：先尝试新层（py）=1~2、自动回退 Go 旧入口=1~2、
Go 旧入口 404、Go 旧入口无业务数据。

### 数据还原

本轮 compare 跑完后的基线核对（软删存 unix 秒，0=存活）：

```
tenants_all=12（2 活 + 10 条软删 UI 探针，本轮新增 1 条软删探针）
tenants_live=2（t_admin / t_demo 完好）
roles=2  sys_role_menu=47  menus=50
工单 2（MUTATE_TICKET）status 已还原为 0（待处理）
ticket_logs=38（本轮新增 3 条：1 条 close 探针 + 2 条浏览器优先级操作，
              均为验收操作痕迹，非业务数据）
```

与历轮残留模式一致（每轮 compare36 会留下 1 条软删探针租户，不可见）。

### 容器操作纪律

只对 go 服务执行 `docker compose build go` + `docker compose up -d --no-deps go`
（两次：摘除后一次、StatusCode 修复后一次）。未 down / rm / prune 任何容器，
未动任何卷。Go 单测借助一次性 `golang:1.25-alpine` 容器 + 新建命名卷
`cs_gomod`（模块缓存，纯增量）。

### 相关文档

- `docs/GO_RETIREMENT.md` —— 下线清单（步骤 1 已标记完成，第 4 节 ⚡ 行现在可删）
- `docs/MERGE_ACCEPTANCE.md` 阶段 3-1 ~ 3-8 —— 93 条路由的迁移过程

---

## 阶段 3-10：Go 下线清单步骤 2 —— `/api/auth/*` 剩余 6 条迁 FastAPI（2026-10-21）

### 这一阶段的性质

步骤 1 摘掉的是「已迁完、只是没摘注册」的 93 条。这一阶段是**真迁移**：
Go 侧 `/api/auth/*` 还剩 6 条没有 FastAPI 等价物（`public-key` / `token` /
`user-token` / `refresh` / `password` / `tenants`），其中 4 条是登录链路的
签发入口。迁完后管理端登录全链路（含前端）走 FastAPI，Go 侧 10 条 auth
注册保留作回退通道。

与 admin 组迁移最大的不同：**这一批的验收标准是「双端逐字节一致」**，
不是「FastAPI 功能正确」——因为登录令牌是跨端流通的，Go 签的令牌
FastAPI 必须能验，反之亦然，否则回退通道是个假通道。

### 改动清单

| # | 文件 | 改动 |
|---|---|---|
| 1 | `python_business/main.py` | 新增认证域 section（745–1287 行，约 540 行）：常量（`ADMIN_TOKEN_TTL` / `RSA_KEY_PATH` / `LOGIN_*`）、工具层（`go_minutes` / 限流三件套 / `write_auth_audit`）、RSA 层（`rsa_private_key` 懒加载生成 + `rsa_public_pem` SPKI + `rsa_decrypt_oaep`）、令牌层（`extract_bearer` / `AdminTokenError` / `verify_admin_token_strict` / `blacklist_admin_token` / `admin_token_payload`）、6 条路由 |
| 2 | `python_business/tests/test_stage39_auth.py` | **新增 41 条**：算法/工具层、路由全分支、双端不变量（黑名单/限流同键空间、载荷字段顺序） |
| 3 | `docker-compose.yaml` | business 服务挂载 `./data:/app/data`——RSA 私钥 `./data/rsa_private.pem` 与 go 容器**共用同一文件**，公钥双端天然逐字节一致 |
| 4 | `admin-app/src/api.js` | 新增 `PY_AUTH_PREFIXES`（6 条 auth 前缀），`migratedToNewLayer()` 增加命中判断——登录/改密/租户列表/刷新全部走 `/py-api/` |
| 5 | nginx 镜像 | `docker compose build nginx`——admin dist 是构建期内嵌的，改 `api.js` 必须重建才生效（本轮踩过，见下） |

### 关键实现点（逐条对齐 Go 的行为）

1. **令牌算法**：`base64url(claims).base64url(HMAC-SHA256(app_secret_hash))`，
   管理令牌 `kind` 缺省；载荷字段顺序 `tid/exp/nonce[/uid]`（`uid=0` 时
   omitempty）——逐字节复刻，双端互验的前提。
2. **验签错误链**：`verify_admin_token_strict()` 复刻 Go `VerifyTokenFull`
   的完整文案（过期 / 租户不存在 / 签名无效 / 客户令牌 / 未知类型 /
   格式 / 解码 / 载荷类型），refresh 统一加 `token 刷新失败: ` 前缀。
3. **黑名单/限流同键空间**：`cs:tok:bl:`（黑名单，TTL 取剩余有效期
   下限 60s）、`cs:login:fail:{ip}:{app_key}`（5 次失败锁 10 分钟，
   Redis 故障 fail-closed 返回 `(False, 60)`）——与 Go 共用同一 Redis，
   回退通道切换时间流不中断。
4. **RSA 共享私钥**：go 与 business 两容器挂同一 `rsa_private.pem`；
   PKCS#1（`TraditionalOpenSSL`，"RSA PRIVATE KEY"）持久化、chmod 600；
   公钥以 SPKI（"PUBLIC KEY"）下发。cryptography 46 没有
   `PrivateFormat.PKCS1` 枚举值，用 `TraditionalOpenSSL` 才是同格式。
5. **中间件顺序**：Go 的 `AdminAuth` 是路由中间件，**先于 handler**——
   `PUT /api/auth/password` 必须是 `Depends(current_admin)` 先鉴权，
   「无令牌 + 非法体」返回 401 而非 400。初版写成「先 body 后 auth」，
   被单测 + compare39 双端比对抓出后修正。
6. **审计**：`write_auth_audit()` 手写 `login_ok` / `login_fail` /
   `change_password` 三行（对齐 Go `IssueTokenHandler` 的手写审计），
   tenant_id 空记 "system"。
7. **登录限流文案**：`wait.Round(time.Minute).String()` 的 Python 等价
   （`go_minutes()`），`"10m0s"` / `"0s"` 逐字一致。

### 过程中发现并修掉的两个问题

1. **容器内测试文件滞后于源码**（本轮回归时暴露）：business 镜像构建于
   最后一次测试修订之前，容器里 `test_stage39_auth.py` 缺
   `self.tenant_by_id = self.demo_tenant()` 一行，跑出 1 个假失败
   （401 != 400）。宿主机 `main.py` 与镜像 md5 一致、运行中的应用
   本来就是对的。按全量 md5 比对确认唯一滞后文件后重建镜像修正。
   **教训：镜像构建时机要晚于最后一次源码修订，回归前先做
   容器/宿主机文件级比对。**
2. **nginx dist 构建期固化**：改完 `api.js` 后浏览器验收前 3 项失败，
   根因是 dist 还是旧构建——`docker compose build nginx` 后全过。

### 验收脚本（2 个，均在 `%TEMP%/cs_verify/`，非仓库文件）

- `compare39.py`（42 项）：双端真实登录 + 跨端互验 + **密文互通**
  （一端公钥加密送另一端解）+ 限流双端共享 429 + 改密联动 +
  审计留痕 + 探针清理核对基线。Redis 操作必须
  `docker exec cs_redis redis-cli`——compose 的 redis 未发布端口到
  主机，主机 6379 是另一个空实例（直接连会假清理）。
- `cs_stage39_accept.mjs`（23 项）：Node 侧建探针，CDP 驱动 Chrome：
  app_key 登录走 py、改密走 py、新密码可登、**回退通道**（只拦 py
  token，密文回退 Go 仍解得开）、Go 签发令牌可访问 FastAPI 菜单树、
  子账号登录走 py。带 `cleanupLeftovers()` 防重入。

### 证据一：测试通过

```
tests/test_stage39_auth.py            41/41
python -m unittest discover -s tests  314 tests, OK（business 容器内）
go test ./...（cs-go-merge-test --target builder 镜像）
  ok  customer_service/internal/handler
  ok  customer_service/internal/middleware
  ok  customer_service/internal/pkg/auth
```

> 注：Go 侧本轮**零改动**（6 条路由原样保留作回退通道），单测全过
> 证明回退通道本身没被碰坏。

### 证据二：接口实测（compare39.py 42/42）

```
公钥：Go PEM == FastAPI PEM（逐字节一致，共享私钥的直接证据）
登录：双端 200，tenant_id/is_platform_admin 一致，token 因 nonce 不同
      但可跨端 refresh 互验
密文：FastAPI 公钥加密的登录密文送 Go → 200；Go 公钥加密送 FastAPI → 200
限流：5 次失败后双端均 429「尝试过于频繁，请 10m0s 后再试」（同键空间）
改密：旧密钥双端失效、新密钥双端可登；旧令牌同步进黑名单双端 401
错误链：refresh 各错误场景双端文案逐字一致
审计：login_ok/login_fail/change_password 均留痕，operator 正确
```

### 证据三：浏览器实测（cs_stage39_accept.mjs 23/23）

```
py-first 登录 / 改密 / 新密码登录 / 子账号登录：全部走 /py-api/
回退通道：拦截 py 的 token 响应后，密文回退 Go 仍解得开（真通道）
Go 令牌驱动 FastAPI 菜单树：跨端令牌在管理后台可用
```

### 回归验证（api.js 全局改动 + 双容器重建后）

```
compare33.py   41/41 一致
compare34.py   35/35 一致
compare35.py   8 场景 / 58 步，双端逐字段完全一致
compare36.py   11 场景 / 89 步，失败 0
compare37.py   9 场景，失败 0
compare38.py   14/14
浏览器 33/34/35/36/37：23 / 18 / 26 / 29 / 35 项，失败 0
```

### 数据还原

本轮回归后基线核对（软删存 unix 秒，0=存活）：

```
tenants_all=20（2 活：t_admin / t_demo；18 条软删 UI 探针）
users_all=11（0 活，全部为历轮探针成员，软删不可见）
roles=2
audit_logs=793（较阶段 3-9 的 711 +82，均为本轮验收痕迹）
```

与历轮残留模式一致（每轮 compare36 留 1 条软删探针租户，不可见）。

### 容器操作纪律

只对 business 执行 `build` + `up -d --no-deps business`（两次：
`./data` 挂载后一次、测试文件滞后修正后一次），对 nginx 一次
（dist 重建）。未 down / rm / prune 任何容器，未动任何卷。

### 遗留项（接入步骤 3 的前置）

1. **Go 侧 10 条 auth 注册仍作回退通道保留**（步骤 1 前已迁的 4 条
   + 本轮迁的 6 条）。摘除它们的前置条件：**Go 健康检查打的是
   `/api/auth/tenants`**，摘除前必须先把健康检查改到别的路径，
   否则容器会变 unhealthy。
2. `reload_go_tenants()` 仍被 6 处 FastAPI 租户变更调用——Go 侧
   `TenantsList` 还要服务 `/ws/admin`、`/ws/chat` 的令牌验签，
   WS 迁完（步骤 3）后才能删。
3. `python_business/` 仍未入 git（项目既有状态，本轮未改变）。

### 相关文档

- `docs/GO_RETIREMENT.md` —— 下线清单（步骤 2 已标记完成）
- `docs/MERGE_ACCEPTANCE.md` 阶段 3-1 ~ 3-9 —— admin 93 条的迁移与摘除
