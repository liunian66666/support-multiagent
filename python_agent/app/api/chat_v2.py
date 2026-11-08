# 对外文案含中文
"""对等 / 调试入口：``/agent/chat/v2`` 与 ``/agent/chat/v2/stream``。

它在结构上属于**传输层**，因此放在 ``app/api/`` 而不是 ``app/agent/support/``：
主图不应 import 任何 HTTP 概念。它也不是第二套链路，具体体现为：

* 请求模型就是 :class:`app.schemas.ChatRequest`（含 ``turn_id``），不另立一份；
* 服务来自 :func:`app.services.support_runtime.runtime_for` 的**唯一**注册表，
  不再维护 ``app.state.support_v2_service`` 这第二个缓存；
* 轮次结果就是 :class:`~app.agent.support.service.TurnResult`，JSON 用
  ``to_payload()``、SSE 用 ``sse_events()``，两者出自同一个对象，因此内容不可能漂移；
* 路由判定与生产入口**共用同一个闸门**（:mod:`app.api.approval_gate`）：普通聊天走主图，
  服务端确认存在未完成审批时返回结构化 409（``approval_pending``），探测失败时返回
  可重试的 503（``approval_state_unknown``）。这里不跑旧图——旧图只从 ``/agent/resume``
  进入。

刻意不在这里做的事：退款/支付/建工单，以及从请求体读取身份。``identity`` 是宿主提供的
解析器；它给出的 tenant/thread 不匹配时 ``thread_key`` 会抛错并 fail-closed。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.agent.support.service import SupportService, sse_events, thread_key
from app.api.approval_gate import require_clear
from app.api.deps import _require_trusted_chat
from app.schemas import ChatRequest
from app.services import trace as agent_trace
from app.services.support_runtime import runtime_for

Identity = Callable[[Request, str], "tuple[str, str] | Awaitable[tuple[str, str]]"]

SERVICE_FOOTER = "\n\n——以上内容由 AI 客服生成，重要信息请以店铺公示与人工核实为准。"

__all__ = ["SERVICE_FOOTER", "build_router", "create_standalone_app"]


def build_router(identity: Identity | None = None, *, service: SupportService | None = None,
                 footer: str = SERVICE_FOOTER) -> APIRouter:
    """JSON + SSE 端点，共用一份轮次结果。

    ``identity``：宿主提供的可信身份解析器。省略时使用生产入口的
    ``_require_trusted_chat``（内部 Token + 租户前缀），也就是与 ``/agent/chat`` 同一套鉴权。
    ``service``：注入已装配的服务（离线验证用）；不传则从共享注册表按租户取。
    """

    async def _resolve(request: Request, thread_id: str) -> tuple[str, str]:
        if identity is None:
            return _require_trusted_chat(request, thread_id), thread_id
        resolved = identity(request, thread_id)
        return await resolved if inspect.isawaitable(resolved) else resolved

    async def _turn(request: Request, payload: ChatRequest):
        tenant_id, thread_id = await _resolve(request, payload.thread_id)
        # 身份先校验：建服务会建图，建图会触达模型与知识库。
        thread_key(tenant_id, thread_id)
        runtime = runtime_for(request.app)
        if service is not None:
            runtime.bind(tenant_id, service)
        # 与 /agent/chat 同一份闸门：pending ⇒ 409、unknown ⇒ 503、clear ⇒ 放行。
        try:
            await require_clear(request, thread_id, payload.turn_id or "")
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            agent_trace.record("route", "approval_blocked",
                               error_code=detail.get("error_code", ""),
                               status=exc.status_code)
            raise
        agent_trace.record("route", "four_agent")
        return await runtime.run_turn(tenant_id, thread_id, payload.user_message,
                                      turn_id=payload.turn_id)

    async def run_guarded(request: Request, payload: ChatRequest):
        try:
            return await _turn(request, payload)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    router = APIRouter()

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

        # 返回生成器本身不会流式输出：FastAPI 会序列化处理函数的返回值。
        return StreamingResponse(generate(), media_type="text/event-stream")

    return router


def create_standalone_app(identity: Identity | None = None,
                          service: SupportService | None = None,
                          *, checkpointer: Any = None) -> FastAPI:
    """独立 ASGI app，用于 JSON/SSE 对等验证（不依赖服务生命周期）。"""
    app = FastAPI(title="support multi-agent v2 (isolated)")
    if checkpointer is not None:
        app.state.checkpointer = checkpointer
    app.include_router(build_router(identity or _default_identity, service=service))
    return app


async def _default_identity(request: Request, thread_id: str) -> tuple[str, str]:
    """测试用解析器：租户来自签名头，绝不来自请求体。

    生产必须传入读取自身会话的解析器；这个实现只为本机验证存在，且下游仍然强制
    tenant/thread 前缀一致。
    """
    tenant = request.headers.get("x-test-tenant", "")
    if not tenant:
        raise HTTPException(status_code=401, detail="x-test-tenant required for the standalone entry")
    return tenant, thread_id
