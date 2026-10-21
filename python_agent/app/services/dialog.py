"""对话编排的共用服务层（严格来说属于应用服务，允许碰检查点与 DB）。

当前承载：检查点回写、token 用量记账。对话决策统一由 services/supervisor.py
负责，API 的 JSON/SSE 入口只适配各自传输格式。
"""
import logging

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.llm_config import (current_chat_prices, current_model_name,
                                  current_model_source)
from app.infra.db.usage import extract_usage_metadata, record_token_usage
from app.services import trace

logger = logging.getLogger("agent")

async def _record_usage_after_chat(request, conn, tenant_id: str, thread_id: str, messages):
    """对话完成后统一记录 token 用量：模型名/来源/单价一次获取（共享 300s 配置缓存），
    费用按发生时单价快照（价格调整不影响历史账单）"""
    in_cache, in_miss, out_price = await current_chat_prices(request.app, tenant_id)
    await record_token_usage(conn, tenant_id, thread_id, messages,
                             fallback_model=await current_model_name(request.app, tenant_id),
                             source=await current_model_source(request.app, tenant_id),
                             chat_input_cache_price=in_cache, chat_input_price=in_miss,
                             chat_output_price=out_price)
    usage = extract_usage_metadata(messages)
    if usage:
        trace.record("usage", "model", model=usage["model"],
                     input_tokens=usage["input_tokens"],
                     output_tokens=usage["output_tokens"],
                     total_tokens=usage["total_tokens"])


async def _record_to_checkpoint(app, thread_id: str, question: str, answer: str):
    """确定性答复写入会话检查点（best-effort）。

    主 Agent 命中流程 FAQ / 订单核验 / 转人工时直接返回，绕过 LangGraph，导致后续 LLM 对话读不到这次问答。
    通过旁路图把 (HumanMessage, AIMessage) 追加到同一 thread 检查点，使长对话上下文连续。
    写入失败不影响答复，仅记日志。工单未完结（has_pending_ticket）时由调用方跳过，避免触碰中断态线程。
    """
    bridge = getattr(app.state, "faq_bridge", None)
    if bridge is None:
        return
    try:
        await bridge.ainvoke(
            {"messages": [HumanMessage(content=question), AIMessage(content=answer)]},
            config={"configurable": {"thread_id": thread_id}},
        )
    except Exception:
        logger.exception("checkpoint bridge write failed thread=%s", thread_id)
