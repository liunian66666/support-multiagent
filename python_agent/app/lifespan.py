"""应用生命周期：资源装配（PG 池 / checkpointer / Qdrant）与后台任务启动。"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

import httpx
import psycopg
from fastapi import FastAPI
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool
from qdrant_client import QdrantClient

from app.agent import tools
from app.agent.faq_bridge import build_faq_bridge
from app.agent.llm_config import get_platform_embeddings, get_platform_model
from app.infra.checkpoint import SafeAsyncPostgresSaver
from app.infra.config import (POSTGRES_DSN, QDRANT_URL, SESSION_CLEANUP_INTERVAL,
                              SESSION_RETENTION_DAYS, TEXT_SPLITTER_CHUNK_OVERLAP,
                              TEXT_SPLITTER_CHUNK_SIZE)
from app.infra.db.prompt import load_active_prompt, seed_default_prompt
from app.infra.db.session import verify_agent_tables
from app.infra.db.trace import ensure_trace_table
from app.infra import pubsub as redis_pubsub
from app.services.invalidation import clear_tenant_caches
from app.workers.cleanup import session_cleanup_loop

logger = logging.getLogger("agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化state占位
    app.state.db_pool: Optional[AsyncConnectionPool] = None
    app.state.checkpointer_conn: Optional[psycopg.AsyncConnection] = None
    app.state.checkpointer: Optional[AsyncPostgresSaver] = None
    app.state.qdrant_client: Optional[QdrantClient] = None
    app.state.embeddings = None
    app.state.vec_dim = 0
    app.state.text_splitter = None
    app.state.tenant_stores = {}
    app.state.tenant_retrievers = {}
    app.state.tenant_agents = {}
    app.state.faq_agents = {}
    app.state.intake_agents = {}
    app.state.agent = None
    app.state.model = None
    app.state.tools = None
    app.state.cleanup_task: Optional[asyncio.Task] = None
    app.state.http_client: Optional[httpx.AsyncClient] = None

    logger.info("==== Agent Service starting ====")
    try:
        # 1. 初始化异步Postgres连接池（业务查询并发安全）+ AsyncPostgresSaver（会话checkpoint独立连接）
        #    check=check_connection：借出前 ping 验证，PG 重启残留的死连接即时丢弃重建（避免借出后执行才报错）
        app.state.db_pool = AsyncConnectionPool(
            POSTGRES_DSN, min_size=2, max_size=10, open=True,
            kwargs={"autocommit": True},
            check=AsyncConnectionPool.check_connection,
        )
        app.state.checkpointer_conn = await psycopg.AsyncConnection.connect(POSTGRES_DSN, autocommit=True)
        # 并发安全 + 断线自动重连包装：
        # 1) AsyncPostgresSaver 单连接不支持并发 aput，多租户并发聊天会触发
        #    "another command is already in progress"（warning/偶发接口报错），加锁串行化；
        # 2) PostgreSQL 重启/断连后常驻连接会 closed，必须自动重建，否则所有聊天恢复历史时
        #    抛 "the connection is closed" 直到 agent 重启（SafeAsyncPostgresSaver 内部按需重连）。
        app.state.checkpointer = SafeAsyncPostgresSaver(app.state.checkpointer_conn, dsn=POSTGRES_DSN)
        await app.state.checkpointer.setup()
        logger.info("✅ AsyncPostgresSaver checkpointer setup done (concurrency-safe wrapper)")
        # [ours] 新增：FAQ 快答检查点旁路图（与主 Agent 共享同一 checkpointer）
        # FAQ 快答旁路图：与主 Agent 共享同一 checkpointer，命中流程 FAQ 时把问答写进检查点，
        # 使后续 LLM 对话能回忆快答上下文，长对话不再出现「忘了刚才答过什么」断层。
        app.state.faq_bridge = build_faq_bridge(app.state.checkpointer)
        logger.info("✅ FAQ 快答检查点旁路图已构建")

        # 扩展表：会话元数据 / Prompt / 知识库文档（含租户维度）
        async with app.state.db_pool.connection() as conn:
            await verify_agent_tables(conn)
            await ensure_trace_table(conn)
            await seed_default_prompt(conn)
            global_prompt = await load_active_prompt(conn, "")
        logger.info("✅ global prompt loaded (len=%d)", len(global_prompt))

        # 2. 异步 http 客户端（必须先于 embedding/model 初始化：get_tenant_model_config 依赖它）
        app.state.http_client = httpx.AsyncClient(timeout=10.0)

        # 3. Qdrant 基础：客户端 + embeddings（集合按租户懒创建）
        app.state.qdrant_client = QdrantClient(url=QDRANT_URL)

        # 平台默认 Embedding 模型（DB 单一数据源，后台「模型提供商/租户模型配置」维护；
        # 模型不可用时降级启动，知识库上传/检索时再重试）
        try:
            app.state.embeddings = await get_platform_embeddings(app)
        except Exception as e:
            app.state.embeddings = None
            logger.warning("⚠️ 平台默认 Embedding 模型不可用（服务降级启动，请检查后台模型配置）: %s", e)
        # 探测 embedding 维度；模型不可用（如额度耗尽）时降级启动，不阻塞服务
        # 后续向量模型恢复后，Qdrant 集合创建/上传会自动重试
        try:
            test_embedding = await app.state.embeddings.aembed_query("test")
            app.state.vec_dim = len(test_embedding)
            logger.info("✅ embedding dim=%d", app.state.vec_dim)
        except Exception as e:
            app.state.vec_dim = 0
            logger.warning("⚠️ embedding 探测失败，知识库/向量检索暂不可用（服务降级启动）: %s", e)

        app.state.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=TEXT_SPLITTER_CHUNK_SIZE,
            chunk_overlap=TEXT_SPLITTER_CHUNK_OVERLAP
        )

        # 4. 平台默认对话模型（DB 单一数据源；不可用时降级启动，请求时懒构建重试）
        try:
            app.state.model = await get_platform_model(app)
        except Exception as e:
            app.state.model = None
            logger.warning("⚠️ 平台默认对话模型不可用（服务降级启动，请检查后台模型配置）: %s", e)
        app.state.tools = [tools.search_kb, tools.query_order, tools.transfer_to_human]

        # 6. 启动后台会话回收任务
        app.state.cleanup_task = asyncio.create_task(session_cleanup_loop(app))
        logger.info("✅ session cleanup task started (retention=%dd, interval=%ds)",
                    SESSION_RETENTION_DAYS, SESSION_CLEANUP_INTERVAL)

        # 6.1 会话自动摘要后台任务（🚀②）
        from app.workers import summary as session_summary
        app.state.summary_task = asyncio.create_task(session_summary.session_summary_loop(app))
        logger.info("✅ session summary task started")

        # 7. Redis：缓存失效广播订阅（多实例下模型切换全量即时生效）——强制依赖，失败即启动失败
        app.state.redis_client = redis_pubsub.make_redis_client()
        # 连接检查：Ping 失败 → 抛错，启动失败（fail-fast）
        await app.state.redis_client.ping()
        app.state.redis_task = asyncio.create_task(
            redis_pubsub.invalidate_listener(app, app.state.redis_client, clear_tenant_caches))
        logger.info("✅ Redis 缓存失效广播订阅任务已启动")

        # 8. 知识库异步索引 worker（Redis 队列消费）：上传/重索引不阻塞请求
        from app.workers import kb_index as kb_index_worker
        # 启动兜底：扫描 status=0 遗留记录重新入队（进程崩溃/重启恢复）
        await kb_index_worker.requeue_pending(app)
        n_workers = max(1, int(os.getenv("KB_INDEX_WORKERS", "2")))
        app.state.kb_workers = []
        for i in range(n_workers):
            t = asyncio.create_task(kb_index_worker.index_worker_loop(app, i))
            app.state.kb_workers.append(t)
        logger.info("✅ KB index workers started (%d)", n_workers)

    except Exception as e:
        logger.exception(f"❌ 服务启动初始化失败:{str(e)}")
        raise

    yield

    # ============ 服务关闭：释放全部资源 ============
    logger.info("==== Agent Service shutdown, release resources ====")
    if app.state.cleanup_task is not None:
        app.state.cleanup_task.cancel()
        try:
            await app.state.cleanup_task
        except asyncio.CancelledError:
            pass
    if getattr(app.state, "redis_task", None) is not None:
        app.state.redis_task.cancel()
        try:
            await app.state.redis_task
        except asyncio.CancelledError:
            pass
    # 停止知识库索引 worker
    for t in getattr(app.state, "kb_workers", []) or []:
        t.cancel()
    for t in getattr(app.state, "kb_workers", []) or []:
        try:
            await t
        except asyncio.CancelledError:
            pass
    # 停止会话摘要后台任务
    if getattr(app.state, "summary_task", None) is not None:
        app.state.summary_task.cancel()
        try:
            await app.state.summary_task
        except asyncio.CancelledError:
            pass
    if getattr(app.state, "redis_client", None) is not None:
        await app.state.redis_client.aclose()
    if app.state.http_client is not None:
        await app.state.http_client.aclose()
    if app.state.qdrant_client is not None:
        app.state.qdrant_client.close()
    if app.state.checkpointer_conn is not None:
        await app.state.checkpointer_conn.close()
    if app.state.db_pool is not None:
        await app.state.db_pool.close()
