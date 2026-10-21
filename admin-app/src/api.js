// ============ API 封装 ============
import axios from 'axios'
import { reactive } from 'vue'

// 全局会话状态（登录/登出）
export const session = reactive({
  token: localStorage.getItem('cs_admin_token') || '',
  tenant_id: localStorage.getItem('cs_admin_tenant') || '',
  is_platform_admin: localStorage.getItem('cs_admin_plat') === '1',
  is_admin: localStorage.getItem('cs_admin_is_admin') === '1',
  user_id: Number(localStorage.getItem('cs_admin_uid') || 0) || 0,
  nickname: localStorage.getItem('cs_admin_nickname') || '',
  role_code: localStorage.getItem('cs_admin_role') || '',
  role_name: localStorage.getItem('cs_admin_role_name') || '',
  tenant_name: localStorage.getItem('cs_admin_tenant_name') || '',
  permissions: JSON.parse(localStorage.getItem('cs_admin_perms') || '[]'),
  roles: JSON.parse(localStorage.getItem('cs_admin_roles') || '[]'),
  menus: JSON.parse(localStorage.getItem('cs_admin_menus') || '[]')
})

export function setSession(data) {
  session.token = data.token
  session.tenant_id = data.tenant_id
  session.is_platform_admin = !!data.is_platform_admin
  session.is_admin = !!data.is_admin
  session.user_id = Number(data.user_id || 0) || 0
  session.role_code = data.role_code || ''
  session.role_name = data.role_name || data.role || '' // 角色名称（右上角显示）
  session.tenant_name = data.tenant_name || data.tenant_id // 租户名称（右上角显示）
  session.nickname = data.nickname || ''
  session.permissions = Array.isArray(data.permissions) ? data.permissions : []
  session.roles = Array.isArray(data.roles) ? data.roles : []
  session.menus = Array.isArray(data.menus) ? data.menus : (Array.isArray(data.menu_tree) ? data.menu_tree : [])
  localStorage.setItem('cs_admin_token', data.token)
  localStorage.setItem('cs_admin_tenant', data.tenant_id)
  localStorage.setItem('cs_admin_plat', data.is_platform_admin ? '1' : '0')
  localStorage.setItem('cs_admin_is_admin', data.is_admin ? '1' : '0')
  localStorage.setItem('cs_admin_uid', String(session.user_id))
  localStorage.setItem('cs_admin_role', session.role_code)
  localStorage.setItem('cs_admin_role_name', session.role_name)
  localStorage.setItem('cs_admin_tenant_name', session.tenant_name)
  localStorage.setItem('cs_admin_nickname', session.nickname)
  localStorage.setItem('cs_admin_perms', JSON.stringify(session.permissions))
  localStorage.setItem('cs_admin_roles', JSON.stringify(session.roles))
  localStorage.setItem('cs_admin_menus', JSON.stringify(session.menus))
}

export function clearSession() {
  session.token = ''
  session.tenant_id = ''
  session.is_platform_admin = false
  session.is_admin = false
  session.user_id = 0
  session.role_code = ''
  session.permissions = []
  session.roles = []
  session.menus = []
  localStorage.removeItem('cs_admin_token')
  localStorage.removeItem('cs_admin_tenant')
  localStorage.removeItem('cs_admin_plat')
  localStorage.removeItem('cs_admin_is_admin')
  localStorage.removeItem('cs_admin_nickname')
  localStorage.removeItem('cs_admin_uid')
  localStorage.removeItem('cs_admin_role')
  localStorage.removeItem('cs_admin_perms')
  localStorage.removeItem('cs_admin_roles')
  localStorage.removeItem('cs_admin_menus')
}

// 权限判断：app_key 登录（无 user_id）视为管理员全权限；子账号按角色权限集判断
export function can(perm) {
  if (!session.user_id) return true
  return (session.permissions || []).includes(perm)
}

// 加载当前用户可见菜单树（登录后 / 页面刷新时由路由守卫调用，DB 菜单驱动）
export async function loadMenus() {
  try {
    const r = await api('/api/admin/menus/tree', { silent: true })
    if (r.code === 0 && Array.isArray(r.data)) {
      session.menus = r.data
      localStorage.setItem('cs_admin_menus', JSON.stringify(session.menus))
      return session.menus
    }
  } catch (e) { /* 网络/权限错误时保留旧菜单，路由守卫兜底 */ }
  return session.menus
}

// 轻量 toast（全局事件）
export function toast(msg, type = 'info') {
  window.dispatchEvent(new CustomEvent('cs-toast', { detail: { msg, type } }))
}

// axios 实例：url 已含 /api 前缀（nginx 反代），baseURL 置空避免双重拼接
const http = axios.create({ baseURL: '', timeout: 30000 })

// ============ 阶段 3：管理端接口逐组迁移到 FastAPI 业务层 ============
// 只在这里登记已迁移的路径前缀，视图代码不动；未登记的一律仍走 Go。
// 回退判定按「响应形态」而不是状态码：新层未部署时 nginx 走 SPA 兜底返回 200 HTML，
// 只看状态码会把它当成功，页面拿到一个字符串而不是数据。
const PY_BASE = '/py-api'
const PY_ADMIN_PREFIXES = [
  '/api/admin/ratings',
  '/api/admin/quick-replies',
  // 阶段 3-2：在线客服状态 / 会话记录 / 客户档案 / 客户名册
  '/api/admin/agent-status',
  '/api/admin/sessions',
  '/api/admin/customers',
  // 阶段 3-3：工单域（列表/详情/消息/日志/已读/接管/回复/关闭/优先级/转交/审批）
  '/api/admin/tickets',
  '/api/admin/ticket/approve',
  // 阶段 3-4：聚合运维域（看板/审计日志/用量/会话运维/归档/模型配置/提取规则）
  '/api/admin/ops/dashboard',
  '/api/admin/audit-logs',
  '/api/admin/token-usage',
  '/api/admin/session/stats',
  '/api/admin/session/cleanup',
  '/api/admin/session/cleanup-count',
  '/api/admin/messages/archive',
  '/api/admin/messages/archive-count',
  '/api/admin/model-config',
  '/api/admin/extract-rules',
  // 阶段 3-5：RBAC 域（菜单树 / 权限目录 / 成员 / 角色）
  '/api/admin/menus/tree',
  '/api/admin/permissions',
  '/api/admin/members',
  '/api/admin/roles',
  // 阶段 3-6：平台域（租户 / 模型提供商 / 全量菜单）
  // 注意：/api/admin/menus 与上面的 /menus/tree 同族，两条都命中新层即可
  '/api/admin/tenants',
  '/api/admin/model-providers',
  '/api/admin/menus',
  '/api/admin/available-model-providers',
  // 阶段 3-7：代理域（Prompt / 知识库 / 平台设置）
  // 这一批在 Go 侧只是「鉴权 + 审计 + 原样转发给 Agent」，新层直连 Agent，语义等价。
  // /api/admin/model-config/test 不单列：它已被上面的 /api/admin/model-config 前缀命中
  '/api/admin/prompts',
  '/api/admin/kb',
  '/api/admin/platform-settings'
]

// 已登记前缀里尚未迁移的子路径（前缀会连带命中，这里显式排除）
// 阶段 3-7 起为空：原来的 /api/admin/model-config/test 已迁入新层
const PY_ADMIN_EXCLUDE = []

// ============ 阶段 3-10：认证域（docs/GO_RETIREMENT.md 步骤 2） ============
// 登录/刷新/改密/公钥/公开租户列表迁到 FastAPI。与 admin 前缀分开登记：
// 这批是「入口级」接口，登录页在挂载时就要用，且 RSA 密文要求双端同一把私钥
// （compose 把 ./data 同时挂进 go 与 business），回退通道才成立。
const PY_AUTH_PREFIXES = [
  '/api/auth/public-key',
  '/api/auth/token',
  '/api/auth/user-token',
  '/api/auth/refresh',
  '/api/auth/password',
  '/api/auth/tenants'
]

function migratedToNewLayer(path) {
  if (PY_ADMIN_EXCLUDE.some(p => path === p || path.startsWith(p + '?')
      || path.startsWith(p + '/'))) return false
  if (PY_AUTH_PREFIXES.some(p => path === p || path.startsWith(p + '?')
      || path.startsWith(p + '/'))) return true
  return PY_ADMIN_PREFIXES.some(p => path === p || path.startsWith(p + '?') || path.startsWith(p + '/'))
}

// 响应能否当业务响应来读：必须是 JSON 对象且带 code（新层所有响应含 HTTPException 都带 code）
function isBusinessPayload(d) {
  return !!d && typeof d === 'object' && 'code' in d
}

http.interceptors.request.use(cfg => {
  if (session.token) cfg.headers.Authorization = 'Bearer ' + session.token
  return cfg
})

http.interceptors.response.use(
  resp => {
    const d = resp.data
    // 已迁移路径返回的不是业务 JSON → 新层没应答（多为 nginx 兜底 HTML），交给 api() 回退
    if (resp.config && resp.config.pyLayer && !isBusinessPayload(d)) {
      return Promise.reject({ code: -3, msg: '新层响应非法', retryable: true, handled: true, silent: true })
    }
    if (isBusinessPayload(d)) {
      // 业务错误（HTTP 200 + code!=0）：拦截器统一 toast（silent 场景静默），页面只处理成功路径
      if (d.code !== 0) {
        if (!(resp.config && resp.config.silent)) toast(d.msg || '操作失败', 'error')
        return Promise.reject({ code: d.code, msg: d.msg, message: d.msg, handled: true, retryable: false })
      }
      return d
    }
    return { code: 0, msg: 'ok', data: d }
  },
  err => {
    const status = err.response && err.response.status
    const data = err.response && err.response.data
    const msg = (data && data.msg) || '请求失败 (' + (status || '网络错误') + ')'
    const silent = !!(err.config && err.config.silent)
    // 只有「新层压根没应答」才可回退：无响应 / 5xx / 返回体不是业务 JSON。
    // 带 code 的 4xx（401 登录失效、403 无权限、404 不存在）是业务结论，回退会掩盖真实语义。
    const retryable = !err.response || status >= 500 || !isBusinessPayload(data)
    if (status === 401) {
      // 登录接口的 401 = 认证失败（密钥/密码错误），直接用后端 msg（如"用户名或密码错误"）
      const isLogin = err.config && /\/api\/auth\/(token|user-token)/.test(err.config.url || '')
      if (isLogin) {
        toast(msg || '登录失败，请检查密钥或账号密码', 'error')
      } else {
        // 动态 import 避免 api.js ↔ router.js 循环依赖；重置动态路由状态，防止重登后菜单残留/为空
        import('./router').then(m => m.resetDynamicRoutes()).catch(() => {})
        clearSession()
        toast('登录已失效，请重新登录', 'error')
        // 路由守卫会拦截到未登录状态跳转登录页
        if (location.hash && !location.hash.includes('/login')) {
          setTimeout(() => { location.hash = '#/login' }, 300)
        }
      }
      return Promise.reject({ code: status || -1, msg, message: msg, handled: true, retryable: false })
    }
    // 403 无权限 / 其余 4xx/5xx/网络错误：拦截器统一提示（silent 场景静默），页面 catch 用 handled 防双弹
    if (!silent) toast(status === 403 ? (msg || '无权限执行该操作') : msg, 'error')
    return Promise.reject({ code: status || -1, msg, message: msg, handled: true, retryable })
  }
)

// 便捷方法；silent=true 时拦截器不 toast（降级/轮询/尽力而为场景，页面自行决定提示）
export async function api(path, { method = 'GET', body = null, params = null, form = null, silent = false } = {}) {
  const cfg = { method, silent }
  if (params) cfg.params = params
  if (form) {
    // 不手动设置 Content-Type：由浏览器自动生成带 boundary 的 multipart 头，
    // 手动设置会丢失 boundary 导致后端解析失败
    cfg.data = form
  } else if (body !== null) {
    cfg.data = body
  }
  // 惰性构造：回退时才发旧请求，避免两个入口同时收到同一次操作
  const build = (url, extra) => {
    try {
      return http.request({ url, ...cfg, ...extra })
    } catch (e) {
      // 同步抛错（如请求配置非法）时透出，便于排查
      console.error('[api] sync error', path, e)
      return Promise.reject({ code: -2, msg: '请求配置错误: ' + (e && e.message || e) })
    }
  }
  if (!migratedToNewLayer(path)) return build(path)
  try {
    return await build(PY_BASE + path, { pyLayer: true })
  } catch (e) {
    // 新层无应答才回退旧入口；业务错误（retryable=false）原样抛出
    if (e && e.retryable) return build(path)
    throw e
  }
}

export default http
