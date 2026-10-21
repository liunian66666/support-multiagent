"""FastAPI 应用装配：日志、路由注册、tools 全局依赖注入。

本模块是唯一的装配点。Dockerfile 的 `python main.py` 与 compose 的 /docs 健康检查
都经顶层 main.py 复用同一个 app 对象，因此路由/生命周期搬迁后对外行为不变。
"""
import logging

from app.infra.logger import init_logger, setup_root_logging

# 日志必须在其余模块创建 logger 之前完成装配（与原 main.py 的时序一致）
setup_root_logging()
init_logger(level="INFO", output="json")

from fastapi import FastAPI  # noqa: E402

from app.agent import tools  # noqa: E402
from app.api.cache_admin import router as cache_router  # noqa: E402
from app.api.chat import router as chat_router  # noqa: E402
from app.api.kb import router as kb_router  # noqa: E402
from app.api.platform import router as platform_router  # noqa: E402
from app.api.prompt import router as prompt_router  # noqa: E402
from app.api.session import router as session_router  # noqa: E402
from app.api.tenant import router as tenant_router  # noqa: E402
from app.api.trace import router as trace_router  # noqa: E402
from app.api.usage import router as usage_router  # noqa: E402
from app.infra.context import pending_ticket_var, tenant_var, thread_var  # noqa: E402
from app.lifespan import lifespan  # noqa: E402

logger = logging.getLogger("agent")

app = FastAPI(title="CustomerServiceAgent Internal Service", lifespan=lifespan)

# ====================== 路由注册 ======================
# 对话 / 会话 / 租户运维 / 用量 / 平台 五组端点由 main.py 拆分而来，
# kb / prompt / cache 三组沿用原有 routers_* 的路径，注册顺序与拆分前一致。
app.include_router(prompt_router)
app.include_router(kb_router)
app.include_router(cache_router)
app.include_router(chat_router)
app.include_router(trace_router)
app.include_router(session_router)
app.include_router(tenant_router)
app.include_router(usage_router)
app.include_router(platform_router)

# 注入 tools 模块的全局依赖（app/logger/tenant_var/pending_ticket_var/thread_var）
tools.set_app(app, logger, tenant_var, pending_ticket_var, thread_var)
