"""租户向量集合与检索器构建（从 agent_factory.py 拆出）。"""
import logging

from langchain_qdrant import QdrantVectorStore
from qdrant_client.http.models import Distance, VectorParams

from app.agent.llm_config import get_tenant_embeddings
from app.infra.config import RETRIEVER_TOP_K
from app.infra.db.tenant import collection_name

logger = logging.getLogger("agent.factory")

_vec_fail_ts: dict = {}
_VEC_FAIL_TTL = 60

async def ensure_tenant_vector(app, tenant_id: str) -> QdrantVectorStore | None:
    """确保租户的 Qdrant collection + vector_store 存在（首次自动灌入默认知识库）。

    降级策略：embedding/向量库任一不可用时返回 None（不抛异常），
    对话降级为纯 LLM（无知识库检索，由 search_kb 工具如实告知）；知识库索引由调用方显式报错。
    """
    if tenant_id in app.state.tenant_stores:
        return app.state.tenant_stores[tenant_id]
    # 冷却期内直接降级，不重复尝试
    import time as _t
    last_fail = _vec_fail_ts.get(tenant_id, 0)
    if last_fail and (_t.time() - last_fail) < _VEC_FAIL_TTL:
        return None
    col = collection_name(tenant_id)
    try:
        tenant_emb = await get_tenant_embeddings(app, tenant_id)
        if not app.state.qdrant_client.collection_exists(col):
            # 维度探测：vec_dim 仅在启动时探测一次；若当时平台 embedding 不可用（vec_dim=0）
            # 或后续更换了向量模型，用当前租户 embedding 实时探测维度，无需重启 Agent。
            vec_dim = app.state.vec_dim
            if vec_dim <= 0:
                probe = await tenant_emb.aembed_query("dim-probe")
                vec_dim = len(probe)
                app.state.vec_dim = vec_dim
                logger.info("✅ vec_dim lazily probed via tenant embedding: dim=%d", vec_dim)
            app.state.qdrant_client.create_collection(
                collection_name=col,
                vectors_config=VectorParams(size=vec_dim, distance=Distance.COSINE),
            )
            logger.info("Qdrant collection [%s] created, dim=%d", col, vec_dim)
        vs = QdrantVectorStore(client=app.state.qdrant_client, collection_name=col,
                           embedding=tenant_emb)
        app.state.tenant_stores[tenant_id] = vs
        _vec_fail_ts.pop(tenant_id, None)
        return vs
    except Exception as e:
        # 降级：向量能力不可用不影响服务启动/对话（纯 LLM 模式），知识库功能由上层明确报错
        _vec_fail_ts[tenant_id] = _t.time()
        logger.warning("⚠️ 租户[%s]向量库未就绪，降级为纯LLM对话（知识库不可用）: %s", tenant_id, str(e)[:200])
        return None


def get_tenant_retriever(app, tenant_id: str):
    """获取租户 retriever（缓存；未就绪返回 None）"""
    if tenant_id in app.state.tenant_retrievers:
        return app.state.tenant_retrievers[tenant_id]
    store = app.state.tenant_stores.get(tenant_id)
    if store is None:
        return None
    retriever = store.as_retriever(
        search_type="similarity",
        search_kwargs={"k": RETRIEVER_TOP_K}
    )
    app.state.tenant_retrievers[tenant_id] = retriever
    return retriever
