# 中文文案必然包含全角标点
"""生产环境的**唯一** SupportService 注册表与轮次执行入口。

收敛前存在三处各自为政的东西，本模块把它们合成一处：

1. ``app.agent.support.cutover._services``（模块级 dict）
2. ``app/api/chat_v2.py``（当时的 ``agent/support/entrypoint.py``）里的 ``app.state.support_v2_service``
3. ``app.api.chat`` 里 JSON / SSE 各写一遍的 ``run_turn`` 调用与结果映射

现在：一个 app 一份 ``SupportRuntime``（挂在 ``app.state``），一个租户一个
``SupportService``，一个轮次一个 :class:`~app.agent.support.service.TurnResult`。
JSON 与 SSE 只做**框架层**差异（信封 vs 分帧），内容来自同一个对象。

模型来源也在这一处决定：普通聊天用该租户已验证的模型配置；``FOUR_AGENT_MODEL_NAME``
只在显式开启评测档（``FOUR_AGENT_EVAL=on``）时才生效，常规部署不会静默换成平台模型。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from langchain_core.messages import AIMessage

from app.agent.support.deps import SupportDeps
from app.agent.support.legacy_bindings import deps_for_app, legacy_history
from app.agent.support.service import SupportService, TurnResult
from app.agent.support.thread_guard import guard_from_env

logger = logging.getLogger("agent")

RUNTIME_ATTR = "support_runtime"
"""``app.state`` 上的唯一注册表属性名。调试入口与生产入口都从这里取。"""

EVAL_FLAG = "FOUR_AGENT_EVAL"
"""显式评测档开关。为 on 时才允许 ``FOUR_AGENT_MODEL_NAME`` 覆盖模型。"""

EVAL_MODEL = "FOUR_AGENT_MODEL_NAME"
"""评测档专用模型 id（换模型 id、保留平台 key/base）。常规部署不读它。"""


def eval_model_override() -> str:
    """评测档模型覆盖；常规部署恒为空串。

    为什么要加 ``FOUR_AGENT_EVAL`` 这道门：一个只写模型名的环境变量，在生产上等于
    "把全体租户的模型静默换成平台 key 下的另一张模型"，而用量归因和图纸都会跟着变，
    却没有一行代码说过这件事。换模型是评测行为，必须显式声明。
    """
    if os.getenv(EVAL_FLAG, "").strip().lower() not in {"on", "true", "1"}:
        return ""
    return os.getenv(EVAL_MODEL, "").strip()


def model_plan() -> tuple[str, str]:
    """返回 ``(model_source, model_name)``；默认 ``("tenant", "")``。"""
    name = eval_model_override()
    return ("platform", name) if name else ("tenant", "")


_ALL_RUNTIMES: list["SupportRuntime"] = []


def runtime_for(app: Any) -> "SupportRuntime":
    """取（或惰性创建）这个 app 的唯一运行时。"""
    runtime = getattr(app.state, RUNTIME_ATTR, None)
    if runtime is None:
        runtime = SupportRuntime(app)
        setattr(app.state, RUNTIME_ATTR, runtime)
        _ALL_RUNTIMES.append(runtime)
    return runtime


def clear_all(tenant_id: str | None = None) -> None:
    """模型配置变更广播：丢图缓存。没有 app 引用的调用方（配置回调）用这个。"""
    for runtime in list(_ALL_RUNTIMES):
        runtime.clear(tenant_id)


class SupportRuntime:
    """一个进程一份；租户级图缓存与并发锁都收敛在这里。"""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._services: dict[str, SupportService] = {}
        self._build_lock = asyncio.Lock()

    @property
    def services(self) -> dict[str, SupportService]:
        return self._services

    async def service_for(self, tenant_id: str) -> SupportService:
        service = self._services.get(tenant_id)
        if service is not None:
            return service
        async with self._build_lock:
            if tenant_id in self._services:
                return self._services[tenant_id]
            model_source, model_name = model_plan()

            async def deps_for_tenant(target: str) -> SupportDeps:
                # ``reuse_faq_agent=True``：知识库路径、进程内检索缓存与 kb_hits 埋点
                # 与切换前逐字一致。主图改的是编排，不是检索。
                return await deps_for_app(self._app, target, reuse_faq_agent=True,
                                          model_source=model_source, model_name=model_name)

            self._services[tenant_id] = SupportService(
                deps_for_tenant,
                checkpointer=getattr(self._app.state, "checkpointer", None),
                thread_guard=guard_from_env(),
            )
        return self._services[tenant_id]

    def clear(self, tenant_id: str | None = None) -> None:
        """租户或平台模型配置变更后丢弃按模型装配的图。"""
        if tenant_id is None:
            self._services.clear()
        else:
            self._services.pop(tenant_id, None)

    def bind(self, tenant_id: str, service: SupportService) -> None:
        """注入一个已经装配好的服务（离线验证与探针用；生产由 service_for 装配）。"""
        self._services[tenant_id] = service

    async def run_turn(self, tenant_id: str, thread_id: str, user_message: str, *,
                       turn_id: str | None = None, on_progress=None) -> TurnResult:
        """跑一轮，返回**唯一**的结果对象（JSON 与 SSE 都读它）。"""
        return await run_turn_result(self._app, tenant_id, thread_id, user_message,
                                     turn_id=turn_id, on_progress=on_progress)


async def run_turn_result(app: Any, tenant_id: str, thread_id: str, user_message: str, *,
                          turn_id: str | None = None, on_progress=None,
                          service: SupportService | None = None) -> TurnResult:
    """一轮的**唯一**执行体：取服务 → 导入旧历史 → 跑主图 → 归因。

    ``service`` 只为可注入的测试缝留出（``cutover._service_for``）；生产不传。
    """
    if service is None:
        service = await runtime_for(app).service_for(tenant_id)
    # 身份先于模型：校验失败必须在建图（会触达模型与知识库）之前抛出。
    history = await service.history(tenant_id, thread_id)
    imports = None if history else await legacy_history(app, thread_id)
    result = await service.run_turn(tenant_id, thread_id, user_message,
                                    turn_id=turn_id, import_history=imports,
                                    on_progress=on_progress)
    _attribute_trace(result)
    return result


def billing_messages(result: TurnResult) -> list[Any]:
    """把一轮的汇总用量还原成一条可记账的 AIMessage。

    主图的 AI 消息没有稳定 id 归属到某一轮（委派是子图内部调用），所以用量只能从
    ``TurnResult.usage``（按 turn_id 累加，含重试）回填。模型归因规则：

    * 常规部署（``tenant``）：不写 ``model_override``，交给
      ``_record_usage_after_chat`` 用该租户的模型名与单价 —— 与实际跑的模型一致。
    * 评测档（``platform`` + 指定模型 id）：写 ``model_override``，只记 token 数与
      模型名，不套用租户单价（另一种模型的价格不该记在这个租户账上）。
    """
    usage = result.usage or {}
    if not int(usage.get("total_tokens") or 0):
        return []
    metadata: dict[str, Any] = {}
    model_source, model_name = model_plan()
    if model_name:
        metadata = {"model_name": model_name, "model_source": model_source,
                    "model_override": True}
    return [AIMessage(content="", response_metadata=metadata, usage_metadata={
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    })]


def _attribute_trace(result: TurnResult) -> None:
    """把路由与专职 Agent 写进 trace，控制台的列才有内容可看。"""
    try:
        from app.services import trace
    except Exception:  # noqa: BLE001 - 归因是尽力而为，不能让它变成一次轮次失败
        return
    current = trace.current()
    if not current:
        return
    current.route = "multi_agent"
    current.specialist = ",".join(dict.fromkeys(result.roles))[:120]
