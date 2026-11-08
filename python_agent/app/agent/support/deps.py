# 中文文案必然包含全角标点
"""Injected capabilities at the trusted boundaries of the graph.

The graph itself never talks to Business, Qdrant or a model SDK directly: it calls
these hooks. That keeps the parent graph testable with fakes and keeps the security
property the project depends on — order ownership is decided by the Business
service inside ``lookup_order`` / ``list_orders``, so no prompt can widen access.

``EvidenceLedger`` lets the FAQ delegate report which knowledge-base chunks
actually backed its answer, which is what makes the Reviewer's "no evidence, no
policy number" rule checkable instead of rhetorical. Attribution is per dispatch
(not per event stream) because the specialists run in parallel.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from .state import Evidence, OrderFact

ModelProvider = Callable[[], Awaitable[Any]]
AgentProvider = Callable[[], Awaitable[Any]]
OrderReader = Callable[[str], Awaitable[OrderFact]]
OrderLister = Callable[[], Awaitable[OrderFact]]
TraceSink = Callable[..., None]
ExperienceRecall = Callable[[str], Awaitable[list[dict[str, Any]]]]
EvidenceWindow = Callable[[], int]
EvidenceSlice = Callable[[int], "list[str | Evidence]"]

EVIDENCE_ROLES = frozenset({"faq"})
"""能产出知识库证据的角色。只有 FAQ 挂了 ``search_kb``，其余角色即使运行在同一条事件流
里也不该拿到别人的检索结果——Reviewer 的「有证据才允许说政策数值」依赖这条边界。"""

_EVIDENCE_SCOPE: ContextVar[str] = ContextVar("evidence_scope", default="")
"""当前证据归属窗口。contextvar 而非普通变量：并行扇出的每个节点是独立 asyncio 任务，
各自持有上下文的副本，兄弟分支互不可见。"""


class EvidenceLedger:
    """按「派发任务」归属知识库证据。

    旧实现是「执行前记游标、执行后取新增」：三个专家同 super-step 运行、共享同一条
    trace 事件流，后结束的分支会把别的分支检索到的 chunk 算成自己的证据，Reviewer 的
    「有证据」于是被别人的检索结果买通。改成按 key 记账后，每一路只认自己的窗口。

    ``window`` / ``since`` 是给生产 FAQ 路径留的兜底：真实 FAQ 专家发出的 evidence 事件
    由 ``app/agent/tools.py`` 直接写进 ``app.services.trace``，不在我们可控的 record 点上。
    它仍按「窗口起点 + 新增」读取，但只在 FAQ 派发里开启，而同一轮里 FAQ 只会派发一次
    （``plan.parse_decision`` 与扇出都按角色去重），所以窗口不会互相重叠。

    **这个对象是租户图共享的**（一个租户一份 ``SupportDeps``，挂在进程里），所以它的
    生命周期必须按「这一轮、这次派发」收紧，不能按进程：
    ``collected`` 读完即消费、``discard`` 收掉失败派发的窗口、``clear`` 只清本轮 key。
    否则 A 会话的 FAQ 正在检索时，B 会话开局整本清账，A 那次派发的窗口就没了，它的
    finding 会「查到了但拿不出证据」——Reviewer 那条「有证据才允许说政策数值」于是
    只能拿专家答复文本充数。
    """

    def __init__(self, *, window: EvidenceWindow | None = None,
                 since: EvidenceSlice | None = None) -> None:
        self._window = window
        self._since = since
        self._by_key: dict[str, list[str | Evidence]] = {}
        self._cursors: dict[str, int] = {}

    @contextmanager
    def scope(self, key: str) -> Iterator[None]:
        """打开一次派发的归属窗口；窗口内 ``record`` 的证据记在这个 key 名下。"""
        if self._window is not None:
            self._cursors[key] = self._window()
        token = _EVIDENCE_SCOPE.set(key)
        try:
            yield
        finally:
            _EVIDENCE_SCOPE.reset(token)

    def record(self, items: Iterable[str | Evidence]) -> None:
        """窗口外记录一律丢弃：没有归属的证据不进任何一路的账。"""
        key = _EVIDENCE_SCOPE.get()
        if not key:
            return
        bucket = self._by_key.setdefault(key, [])
        bucket.extend(item for item in items if item)

    def collected(self, key: str) -> list[str | Evidence]:
        """读出**并消费**这次派发的证据。

        读完即 ``pop`` 是有意的：一次派发的证据只在自己的 finding 组装时用一次，留着只会
        让账本随运行时长无限增长（这个对象活在进程里）。二次读取返回空列表，那是正确语义：
        这次派发已经被收走了。
        """
        items: list[str | Evidence] = list(self._by_key.pop(key, []))
        cursor = self._cursors.pop(key, None)
        if self._since is not None and cursor is not None:
            for item in self._since(cursor):
                if item and item not in items:
                    items.append(item)
        return items

    def discard(self, key: str) -> None:
        """派发失败时收掉窗口：没有 finding 会来消费它，留着就是泄漏。"""
        self._by_key.pop(key, None)
        self._cursors.pop(key, None)

    def clear(self, turn_id: str = "") -> None:
        """按轮清账。

        ``turn_id`` 为空时整本清空，**只允许在没有轮次在途时使用**（离线测试、进程收尾）。
        图里必须传本轮 ``turn_id``：账本挂在租户图上是跨会话共享的，整本清空会把另一个
        会话正在检索的那次派发连窗口一起抹掉。
        """
        if not turn_id:
            self._by_key.clear()
            self._cursors.clear()
            return
        prefix = f"{turn_id}|"
        for store in (self._by_key, self._cursors):
            for key in [item for item in store if item.startswith(prefix)]:
                del store[key]


async def _no_experience(_question: str) -> list[dict[str, Any]]:
    return []


def _noop_trace(*_args: Any, **_kwargs: Any) -> None:
    return None


@dataclass(frozen=True)
class SupportDeps:
    """Capabilities the graph needs. Implementations must already hold a verified
    tenant/customer session; a browser-supplied customer id is never accepted.

    A thread's pre-existing conversation is not a dependency: the service passes it
    into the graph once as input, so the checkpoint stays the only writer.
    """

    model: ModelProvider
    """Chat model for the supervisor, the order delegate and summarisation."""

    faq_agent: AgentProvider
    """Existing FAQ agent (tenant knowledge-base tool only, no write tools)."""

    general_agent: AgentProvider
    """Existing tool-free intake agent (greetings, capability, clarification)."""

    lookup_order: OrderReader
    list_orders: OrderLister

    evidence: EvidenceLedger = field(default_factory=EvidenceLedger)
    """知识库证据账本；默认空账本，装配时由适配层接上真实来源。"""

    recall_experience: ExperienceRecall = field(default=_no_experience)
    trace: TraceSink = field(default=_noop_trace)
