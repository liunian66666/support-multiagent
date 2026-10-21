# Go 网关下线清单

> 2026-10-21 本轮进展：FastAPI 已增加 `/api/user/messages`、
> `/api/user/rating`、`/api/user/upload` 兼容入口，复用已迁移的客户会话鉴权；
> 增加内部 `/api/agent/model-config`、`/api/agent/tenant-quota`、
> `/api/agent/session-summaries/pending`、`POST /api/agent/session-summaries`。
> Python 业务层 321 项测试通过。**这些新入口尚未切流**：客户聊天、
> 实时 WebSocket、并发连接计数仍由 Go 承担，Agent 的 `GO_BUSINESS_API`
> 仍指向 Go。不得据此停用 Go 或宣称整链迁移完成。
> 运行中 Go/Python 对照：`tenant-quota` 与 `model-config` 的完整响应字段一致
> （校验脚本只输出差异字段名，避免泄露明文模型密钥）；摘要接口目前仅完成单测。

> 状态：**步骤 1、2 已执行（2026-10-21）**——admin/biz/plat 三组共 93 条路由已从
> `router.go` 摘除；`/api/auth/*` 剩余 6 条（public-key / token / user-token /
> refresh / password / tenants）已迁到 FastAPI（阶段 3-10），前端
> `api.js` 的 `PY_AUTH_PREFIXES` 已覆盖，双端令牌/密文/限流/黑名单
> 逐字节互通（compare39 42/42 + 浏览器 23/23 + Python 314 单测）。
> 步骤 3 ~ 5 未开始。
> 前提：admin 组 93 条路由已 100% 迁移到 FastAPI（阶段 3-1 ~ 3-7），
> 前端 `admin-app/src/api.js` 的 `PY_ADMIN_PREFIXES` 已覆盖全部 admin 前缀。
> 本文回答一个问题：**剩下的 Go 什么时候能关、按什么顺序关、关之前要做什么。**

---

## 1. 当前分工全景

nginx（`deploy/nginx.conf`）把流量切成三份：

| 路径 | 去向 | 说明 |
|---|---|---|
| `/py-api/*` | `business:8001`（FastAPI） | 并行期验收入口，`/py-api/api/agent/` 显式 404 |
| `/ws/chat`、`/ws/admin` | `go:8080` | WebSocket，Go 的 `ws.Hub` |
| 其余全部（`/api/*`、`/`、`/chat/*`、`/admin/*`） | `go:8080` | **注意：`/api/*` 默认仍全量打 Go** |

也就是说：admin 组虽然已迁完，但**请求并没有被切到 FastAPI**——是前端 `api.js`
自己按前缀表把 admin 请求改写到 `/py-api/...`。nginx 侧 `/api/admin/*` 仍然打到 Go。
auth 同理（阶段 3-10 起）：`api.js` 的 `PY_AUTH_PREFIXES` 把 6 条 auth 请求
改写到 `/py-api/...`，nginx 侧 `/api/auth/*` 仍打到 Go，Go 侧注册保留作回退通道。

这带来两个后果，下线前必须处理：

1. **回退通道仍在**：任何没登记前缀的 admin 子路径仍会落到 Go
   （摘除后为 404）；auth 已登记前缀走 py，未登记的 auth 子路径
   （如错误拼写）仍会落到 Go 并由其 10 条注册处理。
2. ~~**Go 的 admin 组仍在跑**~~：**已随步骤 1 消除**（2026-10-21）——93 条注册已摘除，
   `/api/admin/*` 现在由 gin 无路由流程返回 404；`AuditMiddleware` /
   `RequirePerm` / `BusinessOnly` / `PlatformAdminOnly` 的实现还在文件里
   （被单测引用），但已无任何路由挂载。

---

## 2. 123 条路由分类

Go 侧 `internal/router/router.go` 共 **123 条**路由。

### 2.1 已迁移、可停用（93 条 = admin 组全量）

| 子域 | 条数 | 迁移阶段 |
|---|---|---|
| 工单 / 在线客服状态 | 15 | 3-1 ~ 3-3 |
| 快捷回复 / 评价 | 7 | 3-1 |
| 会话 / 客户档案 / 客户名册 | 12 | 3-2 |
| 看板 / 审计 / 用量 / 归档 / 模型配置 / 提取规则 | 12 | 3-4 |
| RBAC（菜单树 / 权限 / 成员 / 角色） | 13 | 3-5 |
| 平台域（租户 / 模型提供商 / 全量菜单） | 21 | 3-6 |
| 代理域（Prompt / 知识库 / 平台设置 / 模型测试） | 19 | 3-7 |
| **合计** | **93** | |

**这批可以直接停用**，但建议按第 5 节的顺序分批摘，不要一次性全摘。

### 2.2 仍在服务、有明确依赖（30 条）

> 其中 auth 10 条已全部迁到 FastAPI（阶段 3-1 / 3-10），Go 侧注册
> **保留作回退通道**（见 C 节）；真正还没迁的是 A/B/D/E 共 20 条。

#### A. Agent → Go 内部接口（12 条，`/api/agent/*`，`InternalAuth` 保护）

| 路由 | 处理器 | 调用方 | 能否迁到 FastAPI |
|---|---|---|---|
| `GET /model-config` | `AgentModelConfig` | Agent `builder.py` / `llm_config.py` | 可（FastAPI 已有等价解析） |
| `GET /order` | `BusinessOrder` | Agent `tools.py` 订单查询 | 可 |
| `GET /orders` | `BusinessOrders` | Agent `tools.py` 订单查询 | 可 |
| `GET /tenant-quota` | `AgentTenantQuota` | Agent `infra/db/quota.py` | 可 |
| `GET /tenant-concurrency` | `AgentTenantConcurrency` | Agent `infra/db/quota.py` | 可 |
| `GET /session-summaries/pending` | `AgentSessionSummaryPending` | Agent `workers/summary.py` | 可 |
| `POST /session-summaries` | `AgentSessionSummarySave` | Agent `workers/summary.py` | 可 |
| `POST /ws/broadcast` | `AgentWSBroadcast` | **FastAPI** `go_broadcast()` | 取决于 WS 是否迁走 |
| `POST /extract-rules/reload` | `AgentExtractRulesReload` | **FastAPI** `reload_go_extract_rules()` | 可（改为清 Agent 侧缓存） |
| `POST /menus/reload` | `AgentMenusReload` | **FastAPI** `reload_go_menu_caches()` | **纯脚手架，见 3.1** |
| `GET /ws/conns` | `AgentWSConns` | **FastAPI** `tenant_usage_map()` | **不是脚手架，见 3.3** |
| `POST /tenants/reload` | `AgentTenantsReload` | **FastAPI** `reload_go_tenants()` | **脚手架，但有前置条件，见 3.2** |

> Agent 侧基址由 `GO_BUSINESS_API` / `ORDER_BUSINESS_API` 环境变量决定，
> 改指向只需改容器环境变量，不用改 Agent 代码。

#### B. WebSocket（2 条）

| 路由 | 中间件 | 使用方 |
|---|---|---|
| `GET /ws/chat` | `UserAuth` | 用户聊天页（`/chat/*`） |
| `GET /ws/admin` | `AdminAuth` | Vue 后台 `App.vue:162` 实时推送 |

**这是 Go 最难迁的一块**：Go 的 `ws.Hub` 同时服务两端，并且
`WSHub.SendToUser` / `SendToAdminsByTenant` 被 Go 的 handler 和 Agent 的
`/api/agent/ws/broadcast` 共用。迁 WS = 在 FastAPI 重写一个 Hub +
把 Agent 的 broadcast 指向改过去。

#### C. 认证（10 条，`/api/auth/*`）

| 路由 | 已迁 FastAPI | 说明 |
|---|---|---|
| `POST /customer-token` | 是（阶段 3-1） | |
| `POST /guest-token` | 是（阶段 3-1） | |
| `POST /merchant-preview-token` | 是（阶段 3-1） | |
| `POST /logout` | 是（阶段 3-1） | |
| `GET /public-key` | 是（阶段 3-10） | RSA 公钥下发（共享 `./data/rsa_private.pem`） |
| `POST /token` | 是（阶段 3-10） | 管理员登录（app_key + secret，支持 RSA 密文） |
| `POST /user-token` | 是（阶段 3-10） | 子账号登录 |
| `POST /refresh` | 是（阶段 3-10） | 令牌刷新（旧令牌进黑名单） |
| `PUT /password` | 是（阶段 3-10） | 改密码（`AdminAuth` 先于请求体校验） |
| `GET /tenants` | 是（阶段 3-10） | 公开租户列表（登录页下拉） |

**10 条全部已迁**（阶段 3-1 四条 + 阶段 3-10 六条）。Go 侧 10 条注册
**保留作回退通道**，摘除前置条件见第 5 节步骤 2 备注与第 6 节。

**关键约束**：管理端登录令牌的签发/验签算法必须与 Go 逐字节一致
（`base64url(claims).base64url(HMAC-SHA256(app_secret_hash))`，管理令牌 `kind` 必须缺省）。
FastAPI 的 `signed_token` / `decode_admin_token` 已经实现了同一套算法并被单测锁定，
阶段 3-10 把**签发入口**（读表单、查租户、比对 secret、黑名单、写审计）也搬了过去，
双端令牌/密文/限流/黑名单已验证互通。

#### D. 用户端业务（4 条，`/api/user/*`，`UserAuth`）

| 路由 | 处理器 |
|---|---|
| `POST /chat` | `UserChat`（转发 Agent 对话） |
| `POST /upload` | `UserSessionMediaUpload` |
| `GET /messages` | `UserMessages` |
| `POST /rating` | `UserSubmitRating` |

这 4 条与 WS 强耦合（`UserChat` 里要推 WS），建议与 B 一起迁。

#### E. 其他（2 条）

| 路由 | 说明 |
|---|---|
| `GET /metrics` | Prometheus。FastAPI 目前**没有**等价的指标端点，需要补 |
| `GET /api/business/order` | 已返回 `410 Gone`（公开订单查询早停用）。可直接删 |

---

## 3. `go_internal()` 调用点的准确定性

FastAPI 侧共 **5 个** Go 内部调用点（`python_business/main.py`）：

```
1542  go_broadcast()          → POST /api/agent/ws/broadcast
1554  reload_go_extract_rules() → POST /api/agent/extract-rules/reload
3531  reload_go_menu_caches() → POST /api/agent/menus/reload
3545  reload_go_tenants()     → POST /api/agent/tenants/reload
3602  tenant_usage_map()      → GET  /api/agent/ws/conns
```

之前把后三个统称「并行期脚手架接口」。**这个归类不准确**，逐个纠正：

### 3.1 `POST /api/agent/menus/reload` —— 确实是纯脚手架，可删

调用链：`reload_go_menu_caches()` ← 菜单增删改 / 批量 / 重置（5 处）
→ Go `repository.InvalidateMenuCaches()` → 清 `menuCache` + `rolePermsCache`

这两个缓存只被 Go 的 **admin 组**消费：

- `cachedMenus()` ← `AdminMenuTree` / `AdminMenuList`（已迁）
- `loadRolePermsCache()` ← `GetUserPerms` ← **`RequirePerm` 中间件**（已迁）

`/ws/admin` 只挂 `AdminAuth`，**不经过 `RequirePerm`**，所以 WS 不需要菜单缓存。

> **结论：只要 admin 组停用，这个接口和 `reload_go_menu_caches()` 就是死代码，可删。**

### 3.2 `POST /api/agent/tenants/reload` —— 脚手架，但**不能现在删**

调用链：`reload_go_tenants()` ← 租户增删改 / 配额 / 重置 secret / 启停（6 处）
→ Go `auth.RefreshTenants()` → 重建内存里的 `TenantsList` / `tenantByKey`

`TenantsList` 被 `verifyTokenCommon()` 用来做**签名校验**（遍历找 `p.TenantID` 对应的
`AppSecretHash`）。而 `verifyTokenCommon` 的调用方包括：

- `AdminAuth`（admin 组 + **`/ws/admin`**）
- `UserAuth`（**`/ws/chat`** + `/api/user/*`）
- 管理员登录、刷新令牌

**所以只要 `/ws/admin`、`/ws/chat`、`/api/auth/*`、`/api/user/*` 还在 Go，
这个接口就必须保留**：否则「新建租户后该租户用户连不上 WS」——
令牌验签时 `TenantsList` 里没有新租户 → 直接「租户不存在」。

> **结论：有前置条件。WS 与 auth 迁完后再删。**

### 3.3 `GET /api/agent/ws/conns` —— **不是脚手架，是真实数据源**

`tenant_usage_map()` 用它填充租户列表的 `active_sessions`（并发会话数）：

```python
# 并发会话以 ws Hub 实时连接数为准（与 Go 侧配额校验同口径）
conns = go_internal("GET", "/api/agent/ws/conns")
```

这个数**只有 Go 的 `ws.Hub` 知道**。它不是缓存失效，是数据查询。

> **结论：不可删。要么等 WS 迁到 FastAPI 后从新 Hub 取数，
> 要么在 FastAPI 侧实现一个等价统计（如按 Agent 侧活跃会话数近似）。**
> 注意后者会改变 `active_sessions` 的口径，前端展示会变，需要单独决策。

### 3.4 另外两个调用点（容易被漏掉）

| 调用点 | 作用 | 下线时处理 |
|---|---|---|
| `go_broadcast()` → `/api/agent/ws/broadcast` | FastAPI 通过 Go 的 WS Hub 给管理后台/用户推消息（工单更新、人工接管通知等） | WS 迁走后改为推新 Hub |
| `reload_go_extract_rules()` → `/api/agent/extract-rules/reload` | FastAPI 改提取规则后让 Go 的进程内缓存失效 | 该缓存只服务 Go 的提取逻辑；Go 停用后删 |

---

## 4. 下线后可删除的代码（清单一览）

> 步骤 1 执行后（2026-10-21），「admin 组停用」这一前置条件已满足，
> 下表标注 ⚡ 的行现在就可以删；未标的仍待各自前置条件。

| 位置 | 内容 | 前置条件 |
|---|---|---|
| `go_service/internal/router/router.go` | admin / biz / plat 三个分组全部 93 条 | ✅ 已删（步骤 1） |
| `go_service/internal/router/router.go` | `/api/auth/*` 全部 10 条顶层注册（回退通道，无独立分组） | **先改 Go 健康检查探活路径**（现打 `/api/auth/tenants`，摘除后容器会变 unhealthy），再摘；摘除后前端 auth 请求无回退 |
| `go_service/internal/router/router.go` | `agentInternal` 的 `menus/reload`、`tenants/reload`、`ws/conns` | 见 3.1 / 3.2 / 3.3 |
| `go_service/internal/handler/ws.go` | `AgentMenusReload`、`AgentTenantsReload`、`AgentWSConns` | 同上 |
| `go_service/internal/repository/menu.go` | `InvalidateMenuCaches()` 及整套菜单缓存 | ⚡ admin 组停用（已满足）；注意 `SeedBuiltinMenus`/`CheckMenuPermsConsistency` 启动种入仍要保留 |
| `go_service/internal/pkg/auth/auth.go` | `RefreshTenants()` 及 `TenantsList` | WS + auth 迁完 |
| `go_service/internal/middleware/auth.go` | `RequirePerm` | ⚡ admin 组停用（已满足）；⚠️ `auth_middleware_test.go` 仍用它做边界测试，删实现要连带改测试，建议留到测试重构时一起删 |
| `go_service/internal/middleware/` | `BusinessOnly`、`PlatformAdminOnly`、`AuditMiddleware` | ⚡ 同上（`AuditMiddleware` 的 `auditObjectType` 只服务 admin 路径） |
| `python_business/main.py` | `reload_go_menu_caches()` 及 5 处调用点 | ⚡ admin 组停用（已满足）——FastAPI 菜单变更后通知 Go 清缓存已无意义（Go 的菜单缓存无人消费）。对应 Go 侧 `POST /api/agent/menus/reload` 与 `AgentMenusReload` 可同时删 |
| `python_business/main.py` | `go_internal()`、`reload_go_tenants()`、`reload_go_extract_rules()`、`go_broadcast()` | 各自前置条件满足后（`tenants/reload` 要等 WS+auth 迁完） |
| `admin-app/src/api.js` | `PY_ADMIN_PREFIXES`、`migratedToNewLayer()`、回退逻辑、`PY_BASE` | 全部迁完且回退通道关闭 |
| `deploy/nginx.conf` | `location /py-api/` 整段 | 全部迁完 |
| `go_service/internal/handler/audit.go` | `ArchiveMessages` 的「平台管理员全览」分支 | 见第 6 节遗留项 |
| `go_service/internal/middleware/retired_admin.go` | `RetiredAdminPaths` 观测中间件 | 观测期结束（确认无 `RETIRED_ADMIN_PATH` 外部流量）后 |

---

## 5. 建议的下线顺序

每一步都可独立验证、可独立回滚。**不要跳步。**

### 步骤 1：摘掉已确认无流量的 admin 组路由（低风险）—— ✅ 已执行 2026-10-21

- **动作**：从 `router.go` 删掉 admin / biz / plat 三个分组的全部 93 条注册
- **为什么安全**：前端 `api.js` 已把全部 admin 前缀改写到 `/py-api/`；
  `PY_ADMIN_PREFIXES` 覆盖了 `tenants` / `model-providers` / `menus` /
  `available-model-providers` / `prompts` / `kb` / `platform-settings` /
  `model-config` / `extract-rules` / `sessions` / `customers` / `tickets` /
  `ticket/approve` / `agent-status` / `quick-replies` / `ratings` /
  `ops/dashboard` / `audit-logs` / `token-usage` / `session/*` /
  `messages/archive*` / `menus/tree` / `permissions` / `members` / `roles`
- **实际执行内容**（比原计划多两项，都是摘除直接逼出来的）：
  1. 摘除 93 条注册 + 删除只被 admin 组使用的 `permission` 包 import；
  2. 新增 `middleware/retired_admin.go`：`RetiredAdminPaths` 观测中间件，
     挂在全局链最末端，命中 `/api/admin*` 打 `RETIRED_ADMIN_PATH` Warn 日志
     （method/path/status/client_ip/是否带令牌），**不拦截不改响应**，
     摘除后仍能发现漏网流量，观测期结束可随死代码一并删；
  3. **修掉 `StatusCode` 中间件缺陷**（详见下方专节）：摘除前无路由 404 会被
     改写成 200 空体，`curl /api/admin/tenants` 会假报 200，验收标准直接失效。
- **验证**：compare33~38 全部脚本（Go 侧 admin 探测逐条确认 404 退役签名，
  FastAPI 侧全量功能不回退）+ 浏览器 33~37 全部脚本 + Go 全量单测 +
  Python 273 单测；`curl --noproxy '*' http://localhost:8080/api/admin/tenants`
  返回 404 + gin 默认错误页（注意本机有 HTTP 代理，验收必须 `--noproxy` 或走
  裸 socket 的 `httpc.py`，否则代理缓存会返回旧 Go 的 200）。
- **风险兑现情况**：未登记的 admin 子路径从 200 变 404——已全额兑现且符合预期。
  观测中间件上线后持续监控 `RETIRED_ADMIN_PATH` 日志即可发现真实外部调用方。
- **容器操作纪律**：只 `build` + `up -d --no-deps go`，未 down/rm/prune。

#### 摘除逼出来的 `StatusCode` 中间件缺陷（重要，单独记）

现象：摘除后 `curl /api/admin/tenants` 返回 **200 + Content-Length: 0**，
但 Go 日志里观测中间件明明看到 404。根因在 `internal/middleware/status_code.go`：

1. gin 的无路由流程是 `c.handlers = allNoRoute; serveError(c, 404, ...)`，
   **整个全局中间件链跑在 `serveError` 内部的 `c.Next()` 里**；
2. `serveError` 先直接写 `c.writermem.status = 404`——**绕过** `statusWriter`
   包装的 `WriteHeader`（包装器的 `statusCode` 字段停在初始值 200）；
3. 中间件链退栈时 `StatusCode` 后处理跑在 gin 补写错误页**之前**：缓冲区为空、
   `written=false` → 按 200 `flushAs` 提前落盘；
4. gin 回来看到 `writermem.Written()==true`，跳过默认错误页 → 客户端收到
   200 空体，`RequestLogger` 也把 404 记成 200。

这是**潜在缺陷**（任何未匹配路径一直都中招，如 `/api/nonexistent`），
只是摘除 admin 路由前没有无路由流量所以没暴露。

修复：非 JSON 体按**底层真实状态**透传——空正文 + 非 200 进透传模式
（不 flush，让 gin 用真实状态码补写默认错误页），有正文则按底层状态连正文落盘。
新增 `internal/middleware/status_code_test.go` 7 条锁定：
JSON code 改写保持、handler 真实状态透传、纯文本 200 保持、
**无路由 404 + 默认错误页**、外层中间件观测到 404、/ws/ 跳过改写。

> 教训：`statusWriter` 只包装了 `c.Writer`，gin 内部（serveError / redirect）
> 会直接操作 `c.writermem`。包装响应写的中间件，必须把「底层已有非 200」
> 当作一等公民，不能假定所有状态都经过自己的 WriteHeader。

### 步骤 2：迁 `/api/auth/*` 剩 6 条到 FastAPI —— ✅ 已执行 2026-10-21

- **动作**：把 `public-key` / `token` / `user-token` / `refresh` / `password` / `tenants` 搬到 FastAPI
- **关键约束**：令牌签发验签算法必须与 Go 逐字节一致（已有单测锁定）；
  黑名单走同一 Redis；登录成功要写审计（Go 侧 `IssueTokenHandler` 有手写审计）
- **验证**：双端登录一次，比对 token 字符串是否可互验（Go 签的令牌 FastAPI 能验，反之亦然）
- **前置**：无
- **实际执行内容**：
  1. `python_business/main.py` 新增认证域 section（745–1287 行）：常量 /
     限流三件套 / `write_auth_audit` / RSA 层 / 令牌层 / 6 条路由；
     `PUT /password` 用 `Depends(current_admin)` 保证鉴权先于请求体校验
     （对齐 Go 路由中间件 `AdminAuth` 的顺序）；
  2. `docker-compose.yaml` business 服务挂载 `./data:/app/data`，
     RSA 私钥与 go 容器共用同一文件，公钥双端逐字节一致；
  3. `admin-app/src/api.js` 新增 `PY_AUTH_PREFIXES`（6 条），
     `migratedToNewLayer()` 增加命中判断；nginx dist 重建；
  4. Go 侧**零改动**，10 条 auth 注册保留作回退通道。
- **验证结果**：`compare39.py` 42/42（公钥逐字节一致 / 双端登录 /
  密文互通 / 限流共享 429 / 改密联动 / 黑名单跨端 / refresh 错误链逐字
  一致 / 审计留痕）；`cs_stage39_accept.mjs` 23/23（py-first 登录改密、
  回退通道密文仍解得开、Go 令牌驱动 FastAPI 菜单树）；Python 单测
  41 新增 + 314 全量 OK；Go 单测全过（证明回退通道未受损）；
  compare33~38 + 浏览器 33~37 全量回归通过。
- **摘除 Go 侧 auth 注册的前置条件（下一步，未执行）**：
  **Go 健康检查打的是 `/api/auth/tenants`**——摘除这 10 条前必须先把
  健康检查改到别的路径（如 `/metrics` 或新增 `/healthz`），否则
  cs_go 容器会变 unhealthy 触发重启。改健康检查 + 摘除路由可以合成
  一个独立步骤执行。

### 步骤 3：迁 `/api/user/*` 4 条 + WebSocket

- **动作**：FastAPI 实现 `ws.Hub` 等价物 + 4 条用户端接口；
  把 Agent 的 `/api/agent/ws/broadcast` 指向新 Hub；
  nginx 的 `/ws/chat`、`/ws/admin` 改指 `business:8001`
- **这是最大的一块**，建议单独排期
- **验证**：用户聊天全链路（登录 → WS 建连 → 发消息 → 收推送 → 人工接管 → 关闭）；
  管理后台实时推送（工单更新、客服状态）
- **风险**：WS 重连、心跳、背压、租户隔离语义要逐条对；
  `WSHub.CountAllTenantConns()` 的口径要一致（否则配额校验会错）

### 步骤 4：迁 `/api/agent/*` 剩 9 条（去掉 3 个脚手架后）

- **动作**：FastAPI 实现 `model-config` / `order` / `orders` / `tenant-quota` /
  `tenant-concurrency` / `session-summaries` / `session-summaries/pending` /
  `extract-rules/reload`（改为清 Agent 侧缓存）；
  把 Agent 的 `GO_BUSINESS_API` / `ORDER_BUSINESS_API` 指向 FastAPI
- **验证**：Agent 全链路（对话、工具调用、配额、摘要 worker）
- **前置**：步骤 3（否则 `ws/broadcast` 无处可去）

### 步骤 5：关停 Go

- **动作**：`docker compose stop go`（**不要 `rm`，保留镜像和配置**）；
  nginx 删掉 `go_backend` 相关的三个 location；
  观察一周后删除 `go_service/` 目录
- **保留**：`orders_backup.sql`、`go_service` 的 git 历史
- **补 Prometheus**：`/metrics` 要么在 FastAPI 补一个，要么把 Prometheus 的 target 改掉

---

## 6. 下线前必须处理的遗留项

这些是 Go 侧**自身的设计矛盾或缺陷**，迁完不处理就会变成 FastAPI 的永久遗产。

> 步骤 1 执行后的状态更新（2026-10-21）：#2/#3/#5/#6/#7/#8/#9/#10 全部位于
> admin 组代码路径上。admin 组已无流量（前端全走 `/py-api/`），这些缺陷
> **对用户已不可见**，但代码仍在 Go 里；若步骤 5 要删 `go_service/`，
> 需逐条确认没有「删除时才发现有人依赖」的意外。#1 与 #4 仍然有效。

| # | 项 | 位置 | 建议 |
|---|---|---|---|
| 1 | 归档接口的「平台管理员全租户」分支不可达 | Go `handler/audit.go:58` 写了 `tenantID = ""`，但路由挂 **biz 组**（`BusinessOnly` 会把平台管理员 403） | 二选一：把路由挪到 admin 组（放开平台管理员全局归档），或删掉分支。当前 FastAPI 已按「保持鉴权不变」处理，分支代码保留 |
| 2 | 看板 `satisfaction_rate` 的 NULL 扫描 | Go `repository/ops.go:97` 的聚合 SQL 在无评价时产出 NULL，`Scan` 到 `float64` 直接报错 → **整个看板 500** | FastAPI 侧已免疫（None → 0.0）。若 Go 还要跑，应修 SQL：把 `* 100.0 / NULLIF(...)` 包一层 `COALESCE(..., 0)`。**admin 组已停用，对用户不可见** |
| 3 | 掩码回填缺陷 | Go `isMaskedKey` 只认 `*`，而 `MaskAPIKey` 对长 Key 输出 `...abcd`（不含 `*`）→ 用户只改模型名时 Key 被写成掩码字面量 | FastAPI 已在阶段 3-8 修掉。**admin 组已停用，对用户不可见** |
| 4 | `DELETE /prompts/:key` 留孤儿版本 | Agent 侧删 Prompt 不清 `prompt_versions` | 在 Agent 侧加级联删除（与 Go 无关，但下线后会变成唯一实现） |
| 5 | 审计双写两行 | Go handler 手写一条 + 中间件按路径推断一条 | 下线后删掉中间件那一行（handler 行的 `object_type` 更准）。**admin 组已停用，不再产生新行** |
| 6 | 客户列表 `message_count` 恒 0 | Go GORM `Scan` 列名不匹配 | FastAPI 已在阶段 3-8 修掉。**admin 组已停用，对用户不可见** |
| 7 | 会话列表 `total` 不含 handoff 过滤 | Go `Distinct().Count()` 漏了 `handoffFilter` | FastAPI 已在阶段 3-8 修掉。**admin 组已停用，对用户不可见** |
| 8 | 关闭工单 `old_status` 写死 `Handling` | Go `ticket_service.go:275` | FastAPI 已在阶段 3-8 修掉。**admin 组已停用，对用户不可见** |
| 9 | 提取规则先删后校验 | Go 先 `DELETE` 再逐条校验，失败留中间态 | FastAPI 已在阶段 3-8 修掉。**admin 组已停用，对用户不可见** |
| 10 | 角色 `code` 不可指定 | Go `handler/rbac.go:467` 写死 `genRoleCode()` | FastAPI 已在阶段 3-8 放开。**admin 组已停用，对用户不可见** |

---

## 7. 一句话结论

> **admin 组 93 条已摘（步骤 1）、auth 10 条已全部迁到 FastAPI（步骤 2），
> 均于 2026-10-21 完成——但 Go 本身还不能关**：它还扛着 WebSocket、
> 4 条用户端接口和 Agent 的 9 条内部接口（auth 的 10 条注册虽已迁完，
> 仍保留作回退通道，摘除前须先改 Go 健康检查的探活路径）。
> 之前说的「3 个并行期脚手架接口」里，只有 `menus/reload` 是纯脚手架可随手删
> （随步骤 1 已完成，删除动作见第 4 节 ⚡ 行）；
> `tenants/reload` 被 WS 的令牌验签间接依赖；`ws/conns` 根本不是脚手架，
> 是租户列表并发会话数的唯一数据源，WS 不迁它就没法删。
> 下一步是步骤 3（`/api/user/*` 4 条 + WebSocket），这是最大的一块。
