# 中文文案必然包含全角标点
"""Offline fixtures: a scripted chat model and fake specialists.

These exist so the graph can be demonstrated and tested end to end — real
``create_agent``, real tool execution, real ``StateGraph``, real checkpointer — with
no model or service reachable. They are *not* a substitute for the live comparison;
anything measured here proves orchestration and guards, never answer quality.

The scripted model routes on the system prompt instead of a global counter, because
one model instance is shared by the supervisor and the order delegate and their
calls interleave.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from .deps import SupportDeps
from .state import Evidence, OrderFact

ORDER_MARKER = "只读订单 Agent"
SUPERVISOR_MARKER = "Supervisor Agent"
ORDER_ID = re.compile(r"ORD-[A-Za-z0-9-]+|(?:TB|JD|PDD|SF)\d{6,}")
USAGE = {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}


def tool_call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def ai_with_tools(text: str, calls: list[dict[str, Any]]) -> AIMessage:
    return AIMessage(content=text, tool_calls=calls, usage_metadata=USAGE)


class ScriptedSupportModel(BaseChatModel):
    """Answers for the supervisor, the order delegate and the compaction call."""

    supervisor_script: list[AIMessage] = Field(default_factory=list)
    summary_mode: str = "drop_anchors"
    fail_summary: bool = False
    fail_supervisor: bool = False
    order_answer: str = "订单 ORD-1001 状态：已发货；金额：199.00 元。"
    recorded: list[str] = Field(default_factory=list)
    summary_finish: str = "stop"
    """``response_metadata.finish_reason`` of the summarisation call — "length" is how a
    truncated summary shows up in the wild (§6g 发现三), and it must not advance the watermark."""
    summary_kwargs: dict = Field(default_factory=dict)
    """What the compaction call was actually invoked with (``max_tokens`` lives here)."""

    @property
    def _llm_type(self) -> str:
        return "scripted-support"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedSupportModel:
        return self

    def _generate(self, messages: list, stop: list[str] | None = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._respond(messages, kwargs))])

    async def _agenerate(self, messages: list, stop: list[str] | None = None,
                         run_manager: Any = None, **kwargs: Any) -> ChatResult:
        return self._generate(messages, stop, run_manager, **kwargs)

    def _respond(self, messages: list, kwargs: dict | None = None) -> AIMessage:
        system = str(getattr(messages[0], "content", "")) if messages else ""
        if ORDER_MARKER in system:
            return self._as_order_agent(messages)
        if SUPERVISOR_MARKER in system:
            self.recorded.append("supervisor")
            if self.fail_supervisor:
                raise RuntimeError("supervisor model unavailable")
            return self.supervisor_script.pop(0) if self.supervisor_script else AIMessage(
                content="暂时没有可核实的结论，建议补充问题或转人工。", usage_metadata=USAGE)
        self.recorded.append("summary")
        self.summary_kwargs = dict(kwargs or {})
        return AIMessage(content=self._summary(messages), usage_metadata=USAGE,
                         response_metadata={"finish_reason": self.summary_finish})

    def _as_order_agent(self, messages: list) -> AIMessage:
        read_back = [m for m in messages if isinstance(m, ToolMessage)]
        if read_back:
            return AIMessage(content=self.order_answer, usage_metadata=USAGE)
        question = str(getattr(messages[-1], "content", ""))
        found = ORDER_ID.search(question)
        call = (tool_call("lookup_my_order", {"order_id": found.group(0)}, "call_order") if found
                else tool_call("list_my_orders", {}, "call_list"))
        self.recorded.append(f"order:{call['name']}")
        return ai_with_tools("", [call])

    def _summary(self, messages: list) -> str:
        if self.fail_summary:
            raise RuntimeError("summary model unavailable")
        prompt = str(getattr(messages[-1], "content", ""))
        dialogue = prompt.split("新增对话：", 1)[-1][:400]
        if self.summary_mode == "keep_anchors":
            return f"对话要点：{dialogue}"
        stripped = re.sub(r"(?:ORD-[A-Za-z0-9-]+|(?:TB|JD|PDD|SF)\d{6,})", "某订单", dialogue)
        stripped = re.sub(r"\d+(?:\.\d+)?\s*(?:元|天|小时|个工作日|%)", "若干", stripped)
        return f"对话要点：{stripped}"


class FakeSpecialist:
    """Stand-in for the FAQ / General agents: returns a fixed message trace."""

    def __init__(self, *, answer: str, tools: tuple[str, ...] = (),
                 evidence: list[Evidence] | None = None, raises: bool = False) -> None:
        self.answer = answer
        self.tools = tools
        self.evidence = evidence or []
        self.raises = raises
        self.questions: list[str] = []
        # Retrieval side effect, invoked mid-run so the evidence cursor sees exactly
        # what this call produced.
        self.on_call: Any = None

    async def ainvoke(self, payload: dict[str, Any], config: Any = None) -> dict[str, Any]:
        question = str(getattr(payload["messages"][-1], "content", ""))
        self.questions.append(question)
        if self.raises:
            raise RuntimeError("specialist unavailable")
        calls = [tool_call(name, {"query": question}, f"call_{index}")
                 for index, name in enumerate(self.tools)]
        messages = []
        if calls:
            messages.append(ai_with_tools("", calls))
            messages += [ToolMessage(content="检索到：7 天内可申请退货。", tool_call_id=call["id"],
                                      name=call["name"]) for call in calls]
        if self.on_call is not None:
            self.on_call()
        messages.append(AIMessage(content=self.answer, usage_metadata=USAGE))
        return {"messages": messages}


def offline_deps(*, model: ScriptedSupportModel | None = None,
                 faq: FakeSpecialist | None = None,
                 general: FakeSpecialist | None = None,
                 orders: dict[str, OrderFact] | None = None,
                 order_list: OrderFact | None = None) -> tuple[SupportDeps, ScriptedSupportModel,
                                                               list[dict[str, Any]], list[Evidence]]:
    """Wire the fakes behind the same dependency contract the real adapter uses."""
    shared_model = model or ScriptedSupportModel()
    faq_agent = faq or FakeSpecialist(answer="店铺资料：签收后 7 天内可申请退货，运费自担。",
                                      tools=("search_kb",),
                                      evidence=[{"id": "chunk-7d", "title": "退货政策",
                                                 "text": "签收后 7 天内可申请退货"}])
    general_agent = general or FakeSpecialist(answer="您好，我可以帮您查订单或解释店铺政策。")
    order_table = orders or {"ORD-1001": {"code": "ok",
                                          "text": "订单 ORD-1001 状态：已发货；金额：199.00 元",
                                          "order_id": "ORD-1001"}}
    captured: list[Evidence] = []
    if faq_agent.on_call is None:
        faq_agent.on_call = lambda: captured.extend(faq_agent.evidence)

    async def model_provider() -> ScriptedSupportModel:
        return shared_model

    async def faq_provider() -> FakeSpecialist:
        return faq_agent

    async def general_provider() -> FakeSpecialist:
        return general_agent

    async def lookup_order(order_id: str) -> OrderFact:
        return order_table.get(order_id, {"code": "not_found",
                                          "text": f"没有查到订单 {order_id}。", "order_id": order_id})

    async def list_orders() -> OrderFact:
        return order_list or {"code": "ok", "text": "以下是您最近的订单：\n- ORD-1001｜耳机｜已发货"}

    events: list[dict[str, Any]] = []

    def trace(stage: str, name: str, **attributes: Any) -> None:
        events.append({"stage": stage, "name": name, "attributes": attributes})

    return SupportDeps(model=model_provider, faq_agent=faq_provider, general_agent=general_provider,
                       lookup_order=lookup_order, list_orders=list_orders,
                       evidence_cursor=lambda: len(captured),
                       evidence_ids_since=lambda cursor: captured[cursor:],
                       trace=trace), shared_model, events, captured
