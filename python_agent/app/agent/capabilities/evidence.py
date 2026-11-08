# 中文文案必然包含全角标点
"""检索能力：租户知识库检索 + 「已发布证据」过滤。

两者不能拆开用。只检索不过滤，草稿/未发布文档就会被当作政策引用；只过滤不检索，
"没有证据"这条评审规则就无从判定。因此对外只暴露两个组合好的入口：

``retrieve(app, tenant_id, query)``
    返回**已发布**证据列表（租户隔离由 retriever 与 ``approved_evidence`` 双重保证）。

``evidence_ids(docs)``
    取证据 ID（优先 ``chunk_id``），评审器据此判断"有没有证据"。
"""

from __future__ import annotations

from typing import Any


async def retrieve(app: Any, tenant_id: str, query: str) -> list[Any]:
    """检索该租户**已发布**的资料。检索器缺失时就地重建（索引完成后无需重启服务）。"""
    from app.agent.retrieval import ensure_tenant_vector, get_tenant_retriever
    from app.kb.evidence import approved_evidence

    retriever = get_tenant_retriever(app, tenant_id)
    if retriever is None:
        await ensure_tenant_vector(app, tenant_id)
        retriever = get_tenant_retriever(app, tenant_id)
    if retriever is None:
        raise RuntimeError("tenant retriever unavailable")
    docs = await retriever.ainvoke(query[:300])
    return await approved_evidence(app, tenant_id, query, docs)


def evidence_ids(docs: list[Any], limit: int = 3) -> list[str]:
    """证据 ID 列表（``chunk_id`` → ``doc_id`` → 空串丢弃）。"""
    ids: list[str] = []
    for doc in docs[:limit]:
        metadata = getattr(doc, "metadata", {}) or {}
        value = str(metadata.get("chunk_id") or metadata.get("doc_id") or "")
        if value:
            ids.append(value[:100])
    return ids


def is_demo(doc: Any) -> bool:
    """演示资料必须在展示时带上"示例政策"标记，否则会被当成真实承诺。"""
    metadata = getattr(doc, "metadata", {}) or {}
    title = str(metadata.get("title") or "")
    return metadata.get("policy_state") == "demo" or "演示" in title or "示例" in title


def format_for_model(doc: Any) -> str:
    """检索片段送进模型时的统一口径（保留演示来源）。"""
    text = str(getattr(doc, "page_content", ""))
    return f"【演示资料，仅供测试，不代表真实店铺承诺】{text}" if is_demo(doc) else text


__all__ = ["evidence_ids", "format_for_model", "is_demo", "retrieve"]
