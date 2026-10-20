"""
P4-3：Agent 工具函数模块
从 main.py 拆分，包含 search_kb、query_order、transfer_to_human 三个工具。
采用 app 注入模式：在 main.py 中调用 set_app(app) 注入全局实例。

本仓库（fork）改动以 `# [ours]` 标注，上游原始实现保持不动。
"""
from dataclasses import dataclass

from langchain_core.tools import tool
from langgraph.types import interrupt

from app.chat.order import (ORDER_FORBIDDEN, ORDER_NOT_FOUND, ORDER_OK,
                            ORDER_UNAUTHENTICATED, ORDER_UNAVAILABLE,
                            ORDER_UNVERIFIED)
from app.infra.cache import _kb_cache_get, _kb_cache_set
from app.infra.config import GO_BUSINESS_API, INTERNAL_API_TOKEN  # [ours] +INTERNAL_API_TOKEN（内部接口鉴权）
from app.infra.db_pool import _pool_conn

# 模块级 app 引用，由 main.py 注入
_app = None
_logger = None
_tenant_var = None
_pending_ticket_var = None
_thread_var = None


def set_app(app, logger, tenant_var, pending_ticket_var=None, thread_var=None):
    """注入全局 app、logger、tenant_var（在 main.py 中调用）"""
    global _app, _logger, _tenant_var, _pending_ticket_var, _thread_var
    _app = app
    _logger = logger
    _tenant_var = tenant_var
    _pending_ticket_var = pending_ticket_var
    _thread_var = thread_var


async def _record_kb_hits(tid: str, thread_id: str, query: str, docs: list) -> None:
    """KB 闭环：检索结果落库（异步旁路，失败不影响对话）。
    docs 为空 → 插入一条未命中记录（doc_id 为空）。
    docs 非空 → 每个命中文档插一条记录（doc_id/doc_title/score）。
    """
    try:
        if docs:
            rows = [
                (tid, thread_id, d.metadata.get("doc_id", ""), d.metadata.get("title", ""), query,
                 float(getattr(d, "metadata", {}).get("score", 0) or 0))
                for d in docs
            ]
        else:
            rows = [(tid, thread_id, "", "", query, 0.0)]
        async with _pool_conn(_app) as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO kb_hits (tenant_id, thread_id, doc_id, doc_title, query, score) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    rows,
                )
    except Exception as e:
        if _logger:
            _logger.warning(f"[KB Hit] 记录失败 tenant={tid}: {str(e)[:120]}")


@tool
async def search_kb(query: str) -> str:
    """搜索知识库，获取关于平台、课程、政策等官方信息。
    若工具返回"知识库暂未就绪"，说明知识库当前不可用：请如实告知用户"知识库暂时无法访问"，
    不要编造或猜测产品价格、政策、库存等具体信息，并建议用户稍后重试或转接人工客服。
    Args:
        query: 搜索问题或关键词
    """
    tid = _tenant_var.get() or ""
    thread_id = (_thread_var.get() if _thread_var is not None else "") or ""

    # P2-2：先查缓存
    cached = _kb_cache_get(tid, query)
    if cached is not None:
        _logger.debug(f"[KB Cache HIT] tenant={tid}, query={query[:30]}")
        return cached

    retriever = _app.state.tenant_retrievers.get(tid)
    if retriever is None:
        # [ours] 修复：索引完成后 worker 清理了检索器缓存，但 Agent 实例仍缓存命中，
        # 不会重建 → 按需重建，避免必须重启服务才能检索到新文档。
        # 索引/重建索引完成后 worker 会清理检索器缓存，而 Agent 实例仍是缓存命中，
        # 不会再次走 get_tenant_agent 的重建分支。此处按需重建，避免刚索引完的文档
        # 长期返回"知识库暂未就绪"，需要重启服务才恢复。
        try:
            from app.agent.retrieval import ensure_tenant_vector, get_tenant_retriever
            await ensure_tenant_vector(_app, tid)
            retriever = get_tenant_retriever(_app, tid)
        except Exception as e:
            if _logger:
                _logger.warning(f"[KB Retriever] rebuild failed tenant={tid}: {str(e)[:120]}")
            retriever = None
    if retriever is None:
        return "知识库暂未就绪，请稍后再试。"
    try:
        docs = await retriever.ainvoke(query)
    except Exception as e:
        # 检索期降级：embedding 调用失败（key 错误/服务不可用）不抛穿对话，如实告知
        _logger.warning(f"[KB Degrade] tenant={tid} 检索失败，降级为纯LLM: {str(e)[:150]}")
        return "知识库暂未就绪，请稍后再试。"
    if not docs:
        result = "未找到相关信息，建议转接人工客服。"
    else:
        result = "\n".join(f"- {doc.page_content}" for doc in docs)

    # KB 闭环：检索结果落库（命中明细 / 未命中查询）
    await _record_kb_hits(tid, thread_id, query, docs)

    # P2-2：写入缓存
    _kb_cache_set(tid, query, result)
    _logger.debug(f"[KB Cache MISS] tenant={tid}, query={query[:30]}, cached")
    return result


# [ours] 新增：订单只读查询——经 Go 内部接口 /api/agent/order，身份取自服务端会话
# （上游原实现直连 /api/business/order，仅凭 order_id 查询，无归属校验）
@dataclass(frozen=True)
class OrderLookup:
    """订单查询结果：code 供路由决策，text 供用户展示，两者互不耦合。"""
    code: str
    text: str


async def lookup_order(order_id: str) -> OrderLookup:
    """Ask the Go business adapter using the trusted tenant/session context.

    Go 的 StatusCode 中间件会把 body.code 映射到 HTTP status（code=403→HTTP 403），
    所以 HTTP 4xx 是正常的业务响应（订单不存在/不属于您），不是请求失败。
    仅 HTTP 5xx 或网络异常视为系统不可用。
    """
    tid = (_tenant_var.get() if _tenant_var is not None else "") or ""
    thread_id = (_thread_var.get() if _thread_var is not None else "") or ""
    if not INTERNAL_API_TOKEN or not tid or not thread_id.startswith(tid + ":"):
        return OrderLookup(ORDER_UNVERIFIED, "订单身份无法核验。")
    try:
        resp = await _app.state.http_client.get(
            f"{GO_BUSINESS_API}/api/agent/order",
            params={"order_id": order_id, "tenant_id": tid, "thread_id": thread_id},
            headers={"X-Internal-Token": INTERNAL_API_TOKEN}, timeout=5,
        )
        if resp.status_code >= 500:
            return OrderLookup(ORDER_UNAVAILABLE, "订单系统暂时不可用，无法核验订单状态。")
        data = resp.json()
        code = data.get("code")
        if code == 0:
            d = data.get("data") or {}
            return OrderLookup(ORDER_OK, (
                f"订单 {d.get('order_id','')} 状态：{d.get('status','未知')}；"
                f"商品：{d.get('item','')}；金额：{d.get('amount','')}；"
                f"下单日期：{d.get('create_date','')}。"
            ))
        if code == 404:
            # [ours] 404 是用户可自助纠正的失败（单号打错/不是这个账号的单），不自动转人工
            return OrderLookup(ORDER_NOT_FOUND, (
                f"没有查到订单 {order_id}。请核对订单号后重新发送；"
                "如果确认单号无误，可以回复「转人工」由客服协助核实。"
            ))
        if code == 401:
            # [ours] 访客/匿名会话：提示登录，而不是报“订单不存在”或直接转人工
            return OrderLookup(ORDER_UNAUTHENTICATED,
                               "查询个人订单请先登录客户账号。其他常见问题可以继续免登录咨询。")
        if code == 403:
            return OrderLookup(ORDER_FORBIDDEN, f"订单 {order_id} 不属于当前登录账号，无法核验状态。")
        return OrderLookup(ORDER_UNVERIFIED, "订单查询未通过核验。")
    except Exception:
        if _logger:
            _logger.warning("order lookup unavailable tenant=%s", tid)
        return OrderLookup(ORDER_UNAVAILABLE, "订单系统暂时不可用，无法核验订单状态。")


async def query_order_readonly(order_id: str) -> str:
    """兼容取文本的调用方（LLM 工具、旧测试）：只取 text，不参与路由判定。"""
    return (await lookup_order(order_id)).text


# [ours] 新增：对 LLM 暴露的工具包装（内部改调 query_order_readonly，fail-closed）
@tool
async def query_order(order_id: str) -> str:
    """只读查询本人订单。校验订单归属（tenant_id + customer_id）通过后返回状态，否则 fail-closed。"""
    return await query_order_readonly(order_id)


@tool
async def transfer_to_human(reason: str) -> str:
    """将用户转接给人工客服，触发LangGraph HITL中断。
    Args:
        reason: 转接原因
    """
    # 待处理工单已存在（排队中未接管）：不再重复触发中断，正常提示等待，人工接入后自动转交
    if _pending_ticket_var is not None and _pending_ticket_var.get():
        return ("您已提交人工客服，正在等待人工接入。人工客服接入后我会将完整对话转交，"
                "排队期间我仍可继续为您解答问题。")
    approval = interrupt({
        "action": "transfer_to_human",
        "reason": reason,
        "message": f"用户请求转接人工客服，原因：{reason}。是否转接？"
    })
    if approval.get("confirmed"):
        return f"转人工工单已创建，工单号：{approval.get('ticket_id', 'N/A')}。请等待客服接管。"  # [ours] 文案调整：去掉未兑现的等待时间承诺
    return "转接已取消，我继续为您服务。"
