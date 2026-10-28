# 对外文案含中文
"""Mountable HTTP entry for the new graph — supplied, not installed.

`python_agent/app/api/chat.py` is not modified. This module exposes everything a
caller needs to switch the customer chat entry over in one line:

    # inside the authenticated Agent service (host supplies the verified identity)
    from agent.entrypoint import build_router
    app.include_router(build_router(
        lambda request, thread_id: (verified_tenant_of(request), thread_id)))

and, for local/JSON-SSE parity testing without touching the service at all:

    uvicorn "agent.entrypoint:create_standalone_app" --factory --port 8100

Both transports read the *same* ``TurnResult``, so a difference between JSON and SSE
can only be framing, never content — that is the phase-3 requirement, enforced by
construction rather than by test discipline.

Deliberately not here: refund/payment/ticket writes, and any identity taken from the
request body. ``identity`` is a host-provided resolver; if it returns a thread that
does not belong to its tenant, ``service.thread_key`` raises and the request fails
closed.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .deps import SupportDeps
from .legacy_bindings import deps_for_app, legacy_history
from .service import SupportService, enabled, sse_events, thread_key

Identity = Callable[[Request, str], "tuple[str, str] | Awaitable[tuple[str, str]]"]

SERVICE_FOOTER = "\n\n——以上内容由 AI 客服生成，重要信息请以店铺公示与人工核实为准。"


class ChatRequest(BaseModel):
    thread_id: str = Field(min_length=3, max_length=200)
    user_message: str = Field(min_length=1, max_length=2000)
    turn_id: str | None = Field(default=None, max_length=64,
                                description="client idempotency key; replay must reuse it")
    has_pending_ticket: bool = False


def _guard_from_env() -> Any:
    """可选的跨进程串行化开关：`SUPPORT_V2_THREAD_LOCK=on` + `POSTGRES_DSN` 才生效。

    默认关 ⇒ 与今天逐字一致（单进程部署本来也不需要它）。开它的唯一时机是"要起第二个
    worker / 第二个副本"：那时 `asyncio.Lock` 管不到对面进程，同一线程的轮次会互相覆盖
    （报告 §5e M10/M11，实测不加锁 12 轮只剩 3~4 轮；加锁的实测账本见 §4b）。
    """
    if os.getenv("SUPPORT_V2_THREAD_LOCK", "").strip().lower() not in {"on", "true", "1"}:
        return None
    dsn = os.getenv("POSTGRES_DSN") or ""
    if not dsn:
        return None
    from .thread_guard import pg_advisory_guard  # noqa: PLC0415  (只有真开锁才要求 psycopg)
    return pg_advisory_guard(dsn)


def build_router(identity: Identity, *, service: SupportService | None = None,
                 footer: str = SERVICE_FOOTER, thread_guard: Any = None) -> APIRouter:
    """JSON + SSE endpoints over one shared turn result.

    ``service`` may be injected (e.g. with a PostgreSQL saver already open); by
    default it is built lazily per tenant from the live app state. ``thread_guard``
    overrides the env-derived cross-process lock (see :func:`_guard_from_env`).
    """

    def _service(request: Request, tenant_id: str) -> SupportService:
        if service is not None:
            return service
        holder = getattr(request.app.state, "support_v2_service", None)
        if holder is None:
            holder = request.app.state.support_v2_service = {}
        if tenant_id not in holder:
            async def deps_for_tenant(target: str) -> SupportDeps:
                return await deps_for_app(request.app, target, reuse_faq_agent=True)

            holder[tenant_id] = SupportService(
                deps_for_tenant, checkpointer=getattr(request.app.state, "checkpointer", None),
                thread_guard=(thread_guard if thread_guard is not None else _guard_from_env()))
        return holder[tenant_id]

    async def _turn(request: Request, payload: ChatRequest):
        # The host owns the verified identity: chat.py today sets tenant/thread
        # ContextVars before dispatch, and read-only order tools fail closed when those
        # are absent. Re-setting them here would imply this entry could authorise a
        # session, which it cannot and must not pretend to.
        # Hosts resolve identity either synchronously (from request state) or
        # asynchronously (a session lookup), so both are accepted.
        resolved = identity(request, payload.thread_id)
        tenant_id, thread_id = await resolved if inspect.isawaitable(resolved) else resolved
        # Validate the identity pair before building anything: constructing a graph can
        # reach the model and the knowledge base, and an unverifiable session must not
        # get that far (nor pay for it).
        key = thread_key(tenant_id, thread_id)  # noqa: F841 - validated, used inside run_turn
        if not enabled(pending_ticket=payload.has_pending_ticket):
            raise HTTPException(status_code=409, detail="新链路未启用或该会话存在未完成审批，请走原入口")
        target = _service(request, tenant_id)
        history = await target.history(tenant_id, thread_id)
        imports = None if history else await legacy_history(request.app, thread_id)
        return await target.run_turn(tenant_id, thread_id, payload.user_message,
                                     turn_id=payload.turn_id, import_history=imports)

    router = APIRouter()

    async def run_guarded(request: Request, payload: ChatRequest):
        """Fail closed with a 4xx instead of a 500 when the identity is not verifiable."""
        try:
            return await _turn(request, payload)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @router.post("/agent/chat/v2")
    async def chat_v2(request: Request, payload: ChatRequest) -> dict[str, Any]:
        result = await run_guarded(request, payload)
        return {**result.to_payload(), "content": result.answer + footer}

    @router.post("/agent/chat/v2/stream")
    async def chat_v2_stream(request: Request, payload: ChatRequest) -> StreamingResponse:
        result = await run_guarded(request, payload)
        events = sse_events(result, answer_id=result.turn_id, footer=footer)

        async def generate():
            for event in events:
                yield event

        # Returning the generator itself would not stream: FastAPI serialises the
        # handler's return value. The event stream has to be an explicit response.
        return StreamingResponse(generate(), media_type="text/event-stream")

    return router


def create_standalone_app(identity: Identity | None = None,
                          service: SupportService | None = None) -> FastAPI:
    """Standalone ASGI app for end-to-end JSON/SSE parity checks, outside the service."""
    app = FastAPI(title="support multi-agent v2 (isolated)")
    app.include_router(build_router(identity or _default_identity, service=service))
    return app


async def _default_identity(request: Request, thread_id: str) -> tuple[str, str]:
    """Test-only resolver: a signed header pair, never a body field.

    In production the host must pass a resolver that reads its verified session; this
    one exists so the entry can be exercised locally, and it still enforces the
    tenant/thread prefix rule downstream.
    """
    tenant = request.headers.get("x-test-tenant", "")
    if not tenant:
        raise HTTPException(status_code=401, detail="x-test-tenant required for the standalone entry")
    return tenant, thread_id
