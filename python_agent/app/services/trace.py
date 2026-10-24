"""Per-turn structured trace with a safe, small event vocabulary."""

import logging
import re
import secrets
import time
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.infra.db.trace import save_trace_run

logger = logging.getLogger("agent.trace")
_current: ContextVar["TraceRun | None"] = ContextVar("agent_trace_run", default=None)
_VALID_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_MAX_EVENTS = 64


@dataclass
class TraceRun:
    trace_id: str
    tenant_id: str
    thread_id: str
    started_at: float = field(default_factory=time.time)
    started_perf: float = field(default_factory=time.perf_counter)
    route: str = ""
    specialist: str = ""
    status: str = "running"
    duration_ms: int = 0
    events: list[dict] = field(default_factory=list)
    # [ours] S1 轮次归因锚点：每一轮 Agent 答复的稳定 ID，随响应返回给业务层，
    # 客户评价按它归因到轮次（trace_id 标识"哪次运行"，answer_id 标识"哪条答复"）。
    answer_id: str = ""

    def event(self, stage: str, name: str, **attributes):
        if len(self.events) >= _MAX_EVENTS and (stage, name) != ("run", "finished"):
            return
        # Only scalar metadata and bounded lists of scalar IDs. Never persist
        # user messages, tool arguments, model prompts or API keys here.
        safe = {}
        for key, value in attributes.items():
            if key.lower() in {"query", "message", "prompt", "content", "api_key", "token", "args"}:
                continue
            if isinstance(value, (str, bool, int, float)):
                safe[key] = value[:120] if isinstance(value, str) else value
            elif isinstance(value, (tuple, list)):
                safe[key] = [str(item)[:80] for item in value[:10]]
        event = {
            "offset_ms": max(0, int((time.perf_counter() - self.started_perf) * 1000)),
            "stage": stage[:32], "name": name[:64], "attributes": safe,
        }
        if len(self.events) >= _MAX_EVENTS:
            self.events[-1] = event
        else:
            self.events.append(event)

    async def finish(self, app, status: str):
        self.status = status
        self.duration_ms = max(0, int((time.perf_counter() - self.started_perf) * 1000))
        self.event("run", "finished", status=status, duration_ms=self.duration_ms)
        if getattr(getattr(app, "state", None), "db_pool", None) is None:
            return
        try:
            await save_trace_run(app, self)
        except Exception:
            logger.exception("agent trace persistence failed trace_id=%s", self.trace_id)


def new_trace(raw_id: str, tenant_id: str, thread_id: str) -> TraceRun:
    trace_id = raw_id if _VALID_ID.fullmatch(raw_id or "") else secrets.token_hex(16)
    run = TraceRun(trace_id, tenant_id, thread_id, answer_id="ans_" + secrets.token_hex(12))
    run.event("run", "started")
    return run


def current_answer_id() -> str:
    """本轮答复的 answer_id；不在 trace 上下文时为空串（调用方按缺省处理）。"""
    run = _current.get()
    return run.answer_id if run is not None else ""


def bind(run: TraceRun):
    return _current.set(run)


def reset(token):
    _current.reset(token)


def record(stage: str, name: str, **attributes):
    run = _current.get()
    if run is not None:
        run.event(stage, name, **attributes)


def current() -> TraceRun | None:
    return _current.get()
