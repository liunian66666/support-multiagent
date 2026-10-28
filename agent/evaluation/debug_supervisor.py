"""Print the real exception behind a `no_result` turn (diagnostic, not a test).

The graph deliberately degrades instead of propagating, which is right for a customer
facing path and wrong for debugging: the failure text has to end up somewhere. This
script bypasses the graph and invokes the supervisor directly so the traceback is
visible.
"""

from __future__ import annotations

import asyncio
import traceback
from types import SimpleNamespace

from agent.graph import build_support_graph
from agent.legacy_bindings import deps_for_app, session_context

TENANT = "t_demo"
QUESTION = "我买了120块的东西还要不要付邮费？"


async def main() -> None:
    import logging

    from httpx import AsyncClient
    from psycopg_pool import AsyncConnectionPool
    from qdrant_client import QdrantClient

    from app.agent import tools as business_tools
    from app.agent.retrieval import ensure_tenant_vector
    from app.infra.config import POSTGRES_DSN, QDRANT_URL
    from app.infra.context import pending_ticket_var, tenant_var, thread_var
    from app.infra.db.tenant import collection_name
    logging.basicConfig(level=logging.INFO)

    client = QdrantClient(url=QDRANT_URL)
    print("collection exists:", client.collection_exists(collection_name(TENANT)))
    pool = AsyncConnectionPool(POSTGRES_DSN, min_size=1, max_size=4, open=False,
                               kwargs={"autocommit": True})
    await pool.open()
    http = AsyncClient(timeout=30)
    app = SimpleNamespace(state=SimpleNamespace(
        http_client=http, qdrant_client=client, db_pool=pool, vec_dim=0,
        tenant_stores={}, tenant_retrievers={}, tenant_agents={}, faq_agents={},
        intake_agents={}, checkpointer=None))
    business_tools.set_app(app, logging.getLogger("dbg"), tenant_var, pending_ticket_var, thread_var)
    await ensure_tenant_vector(app, TENANT)

    deps = await deps_for_app(app, TENANT, reuse_faq_agent=True)
    model = await deps.model()
    print("model:", type(model).__name__, getattr(model, "model_name", "?"),
          "base_url:", str(getattr(model, "openai_api_base", getattr(model, "base_url", "?")))[:60])
    try:
        answer = await model.ainvoke("只回答两个字：收到")
        print("raw model reply ok:", repr(str(answer.content)[:40]))
    except Exception:
        print("RAW MODEL CALL FAILED")
        traceback.print_exc()

    thread = f"{TENANT}:dbg-1"
    await session_context(TENANT, thread)()
    from langchain_core.messages import HumanMessage


    try:
        faq = await deps.faq_agent()
        print("faq agent:", type(faq).__name__)
        out = await faq.ainvoke({"messages": [HumanMessage(content=QUESTION)]})
        print("faq direct ok:", str(out["messages"][-1].content)[:120])
    except Exception:
        print("FAQ DIRECT CALL FAILED")
        traceback.print_exc()

    try:
        graph = await build_support_graph(deps, timeout_seconds=120.0)
        state = await graph.ainvoke(
            {"messages": [HumanMessage(content=QUESTION, id="dbg:u")], "turn_id": "dbg"},
            config={"configurable": {"thread_id": f"dbg:{thread}"}, "recursion_limit": 24})
        print("graph outcome:", state.get("outcome"), "| answer:", str(state.get("answer"))[:120])
    except Exception:
        print("GRAPH CALL FAILED")
        traceback.print_exc()

    await pool.close()
    await http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
