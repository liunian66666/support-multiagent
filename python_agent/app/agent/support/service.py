# 中文文案必然包含全角标点
"""Turn entry point: identity, serialisation, idempotency, and one shared answer.

The service is deliberately thin. It refuses an unverified tenant/thread pair,
serialises turns per thread so a checkpoint is never written concurrently (in-process
``asyncio.Lock``; cross-process needs the optional ``thread_guard`` from
:mod:`agent.thread_guard` -- one turn is a read-modify-write over the same checkpoint,
and a single UPSERT being atomic does not make the pair atomic), derives
message ids from the caller's idempotency key so a retried request cannot create a
second copy of a turn, and then hands everything to the graph. It writes no chat
row itself: history is read back from the checkpoint, which is what removes the
out-of-graph checkpoint bridge from this path.

``to_payload`` and ``sse_events`` format the *same* ``TurnResult`` so the JSON and
SSE transports cannot drift in content by construction, only in framing.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from . import memory
from .deps import SupportDeps
from .graph import PROGRESS_NODES, TURN_STEP_BUDGET, build_support_graph

NAMESPACE = "support_v2"
"""主图检查点键前缀。旧审批仍留在原始 ``thread_id`` 上，两者互不覆盖。"""

_MAX_THREAD_ID_CHARS = 200
# 跨进程锁取不到时的安全话术。为什么不裸跑也不 500：裸跑会把这一轮的读-改-写压在另一个
# 进程正在写的同一份检查点上（=§5e M10/M11 的丢轮），500 会让客户看到空白。
# 措辞刻意不承诺任何事实，`outcome`/`error` 两列把真实原因留给账本与 trace。
LOCK_UNAVAILABLE_ANSWER = "当前会话正在处理另一条消息，这条还没写进您的记录。请稍后重试，或回复「转人工」由客服协助。"


class TurnConflictError(ValueError):
    """同一个 ``turn_id`` 被用于**另一个问题**。调用方必须报冲突，不能悄悄作答。

    为什么不是"就当新的一轮跑"：幂等键一旦能被两个问题共用，业务层的重试逻辑就再也
    分不清"这是重试"还是"这是新问题"，落库与评价会张冠李戴。宁可 409。
    """


def thread_key(tenant_id: str, thread_id: str) -> str:
    """Checkpoint key namespaced away from the legacy thread state on purpose: an old
    checkpoint must never be read with this graph's schema."""
    if not tenant_id or not thread_id.startswith(f"{tenant_id}:"):
        raise PermissionError("tenant/thread identity must come from a verified server session")
    key = f"{NAMESPACE}:{thread_id}"
    if len(key) > _MAX_THREAD_ID_CHARS:
        raise ValueError("thread_id too long for a checkpoint key")
    return key


@dataclass(frozen=True)
class TurnResult:
    turn_id: str
    answer: str
    outcome: str
    roles: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    review_issues: list[str] = field(default_factory=list)
    attempts: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    context: dict[str, int] = field(default_factory=dict)
    model_messages: list[Any] = field(default_factory=list)
    error: str = ""
    replayed: bool = False

    def to_payload(self) -> dict[str, Any]:
        """Business-facing shape, not just our own.

        ``python_business/business/chat/realtime.py`` gates on ``data["success"]``
        (line ~494) and persists ``data["content"]`` together with
        ``data["answer_id"]`` as the turn-attribution anchor (line ~523). A payload
        missing ``answer_id`` degrades quietly: the reply renders, but the rating lands
        with an empty anchor and can no longer be attributed to a turn. ``success`` is
        therefore "we produced an answer", not "the answer passed review" -- a
        ``review_blocked`` or ``no_result`` turn still returns safe text.
        """
        return {"content": self.answer, "success": bool(self.answer.strip()),
                "is_interrupt": False, "interrupt_data": None,
                "answer_id": self.turn_id,
                "outcome": self.outcome, "turn_id": self.turn_id, "agents": self.roles,
                "attempts": self.attempts,
                "evidence_ids": self.evidence_ids, "review_issues": self.review_issues,
                "context": self.context, "usage": self.usage, "error": self.error}


class SupportService:
    """One instance per Agent process; graphs are cached per tenant."""

    def __init__(self, deps_for_tenant, *, checkpointer: Any = None,
                 graph_kwargs: dict[str, Any] | None = None,
                 thread_guard: Any = None) -> None:
        """`thread_guard`: 可选的**跨进程**串行化入口（`agent.thread_guard.pg_advisory_guard`）。

        默认 None ⇒ 只有进程内的 `asyncio.Lock`，也就是今天的行为；一旦起第二个 worker
        或第二个副本，那把锁就管不到对面进程了（报告 §5e 的 M10/M11：两个实现都会丢轮）。
        接了 guard 之后，顺序是 **进程内锁 → advisory 锁 → ainvoke**：
        内层先挡住同进程的并发，避免每个等待中的轮次都去占一条数据库连接。
        """
        self._deps_for_tenant = deps_for_tenant
        self._checkpointer = checkpointer
        self._graph_kwargs = graph_kwargs or {}
        self._thread_guard = thread_guard
        self._graphs: dict[str, Any] = {}
        self._build_lock = asyncio.Lock()
        self._thread_locks: dict[str, asyncio.Lock] = {}

    async def graph_for(self, tenant_id: str) -> Any:
        graph = self._graphs.get(tenant_id)
        if graph is not None:
            return graph
        async with self._build_lock:
            if tenant_id not in self._graphs:
                deps: SupportDeps = await self._deps_for_tenant(tenant_id)
                self._graphs[tenant_id] = await build_support_graph(
                    deps, checkpointer=self._checkpointer, **self._graph_kwargs)
        return self._graphs[tenant_id]

    async def run_turn(self, tenant_id: str, thread_id: str, message: str, *,
                       turn_id: str | None = None,
                       import_history: Sequence[BaseMessage] | None = None,
                       on_progress: Callable[[str], Awaitable[None]] | None = None) -> TurnResult:
        """Run one customer turn and return the reviewed, checkpointed result."""
        key = thread_key(tenant_id, thread_id)
        graph = await self.graph_for(tenant_id)
        token = (turn_id or uuid.uuid4().hex)[:64]
        incoming = [HumanMessage(content=message, id=f"{token}:u")]
        if import_history:
            incoming = [*self._replayable(import_history), *incoming]
        lock = self._thread_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if self._thread_guard is None:
                state, replayed = await self._invoke(graph, key, incoming, token, on_progress)
            else:
                # 取锁与跑图分开写，是为了**只把"取不到锁"变成安全答复**：
                # 用 `async with` 一整块包住的话，图里抛出的异常也会被同一个 except 捞走，
                # 一次模型故障就会被记成 lock_unavailable，账本从此看不出区别。
                guard = self._thread_guard(key)
                try:
                    await guard.__aenter__()
                except Exception as exc:  # noqa: BLE001  跨进程锁拿不到 ⇒ 这一轮不跑，绝不"裸跑"
                    return TurnResult(turn_id=token, answer=LOCK_UNAVAILABLE_ANSWER,
                                      outcome="lock_unavailable",
                                      error=f"{type(exc).__name__}: {exc}")
                try:
                    state, replayed = await self._invoke(graph, key, incoming, token, on_progress)
                finally:
                    await guard.__aexit__(None, None, None)
        return self._result(token, state, replayed=replayed)

    async def _invoke(self, graph: Any, key: str, incoming: list[BaseMessage],
                      token: str, on_progress: Callable[[str], Awaitable[None]] | None = None
                      ) -> tuple[dict[str, Any], bool]:
        config = {"configurable": {"thread_id": key}, "recursion_limit": TURN_STEP_BUDGET}
        snapshot = await graph.aget_state(config)
        previous = snapshot.values or {}
        record = next((item for item in reversed(previous.get("ledger", []))
                       if item["turn_id"] == token), None)
        if record is not None:
            question = next((memory.message_text(item) for item in incoming
                             if isinstance(item, HumanMessage) and item.id == f"{token}:u"), "")
            if record["question"] != question[:500]:
                raise TurnConflictError("turn_id was already used for a different question")
            return previous, True
        if on_progress is None:
            state = await graph.ainvoke({"messages": incoming, "turn_id": token}, config=config)
        else:
            # Updates are emitted only after each node completes. Never expose node output.
            # 白名单来自图本身：node 引擎多了 precheck/route/各专家/compose，写死一份
            # 就会让前端停在「supervisor」上不动，而图其实已经走到第三个专家了。
            async for update in graph.astream({"messages": incoming, "turn_id": token},
                                              config=config, stream_mode="updates"):
                for node in update:
                    if node in PROGRESS_NODES:
                        await on_progress(node)
            state = (await graph.aget_state(config)).values or {}
        return state, False

    async def history(self, tenant_id: str, thread_id: str) -> list[dict[str, Any]]:
        """Customer-visible transcript, read straight from the checkpoint."""
        graph = await self.graph_for(tenant_id)
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_key(tenant_id, thread_id)}})
        values = snapshot.values or {}
        return [{"role": "user" if isinstance(m, HumanMessage) else "assistant",
                 "content": memory.message_text(m), "message_id": m.id}
                for m in memory.visible_messages(values)]

    async def memory_state(self, tenant_id: str, thread_id: str) -> dict[str, Any]:
        graph = await self.graph_for(tenant_id)
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_key(tenant_id, thread_id)}})
        values = snapshot.values or {}
        return {"summary": values.get("summary", ""), "watermark": values.get("summary_watermark", 0),
                **memory.context_state(values)}

    @staticmethod
    def _replayable(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
        """Give imported turns stable ids so re-importing an old thread is a no-op."""
        replayed: list[BaseMessage] = []
        for index, message in enumerate(messages):
            if isinstance(message, (HumanMessage, AIMessage)) and not getattr(message, "tool_calls", None):
                digest = uuid.uuid5(uuid.NAMESPACE_URL, f"legacy:{index}:{memory.message_text(message)}")
                content = memory.message_text(message)[:2000]
                if not content:
                    continue
                replayed.append(type(message)(content=content, id=f"legacy:{digest.hex}"))
        return replayed

    @staticmethod
    def _result(token: str, state: dict[str, Any], *, replayed: bool = False) -> TurnResult:
        record = next((item for item in reversed(state.get("ledger", [])) if item["turn_id"] == token), None)
        if record is not None:
            roles, evidence_ids = record["roles"], record["evidence_ids"]
            issues, attempts = record["review_issues"], record["attempts"]
        else:
            # Only for a turn that never reached finalize (crash mid-graph): read this
            # turn's findings, never the accumulated ones from earlier turns.
            own = [item for item in state.get("delegations", []) if item["turn_id"] == token]
            roles = list(dict.fromkeys(item["specialist"] for item in own))
            evidence_ids = [e["id"] for item in own for e in item["evidence"]]
            issues, attempts = [], state.get("attempts", 0)
        return TurnResult(turn_id=token,
                          answer=record["answer"] if record is not None else state.get("answer", ""),
                          outcome=record["outcome"] if record is not None else state.get("outcome", ""),
                          roles=roles, evidence_ids=evidence_ids, review_issues=issues,
                          attempts=attempts,
                          usage=(state.get("usage", {}) or {}).get(token, {}),
                          context=memory.context_state(state),
                          error=state.get("last_error", ""), replayed=replayed,
                          model_messages=[m for m in state.get("messages", [])
                                          if getattr(m, "id", "") and f":{token}" in str(m.id)])


def sse_events(result: TurnResult, *, answer_id: str = "", footer: str = "") -> list[str]:
    """Frame the same answer for SSE; chunk and end carry identical text."""
    content = result.answer + footer
    body = {"type": "chunk", "content": content}
    end = {"type": "end", "content": content, "answer_id": answer_id,
           "outcome": result.outcome, "agents": result.roles, "turn_id": result.turn_id}
    return [f"data: {json.dumps(body, ensure_ascii=False)}\n\n",
            f"data: {json.dumps(end, ensure_ascii=False)}\n\n"]
