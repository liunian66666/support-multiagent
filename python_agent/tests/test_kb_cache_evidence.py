"""A cached KB answer must keep the evidence required by the reviewer."""

import asyncio
import logging
from contextvars import ContextVar
from types import SimpleNamespace

from app.agent import tools
from app.infra.cache import _kb_cache_set, clear_kb_cache
from app.services import trace


def test_cache_hit_keeps_evidence_ids():
    tenant = ContextVar("test_tenant", default="t_demo")
    thread = ContextVar("test_thread", default="t_demo:buyer1")
    app = SimpleNamespace(state=SimpleNamespace(tenant_retrievers={}))
    tools.set_app(app, logging.getLogger("test.kb"), tenant, thread_var=thread)
    clear_kb_cache()
    _kb_cache_set("t_demo", "包邮门槛", "满 99 元包邮", ["chunk-99"])
    run = trace.new_trace("cached-evidence-1", "t_demo", "t_demo:buyer1")
    token = trace.bind(run)
    try:
        result = asyncio.run(tools.search_kb.ainvoke({"query": "包邮门槛"}))
        assert result == "满 99 元包邮"
        evidence = [event for event in run.events if event["stage"] == "evidence"]
        assert evidence[-1]["attributes"]["evidence_ids"] == ["chunk-99"]
    finally:
        trace.reset(token)
        clear_kb_cache()
