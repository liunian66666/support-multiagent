"""Compose the FastAPI business application from functional routers."""

import time

import pymysql
import redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from business.admin import (auth, catalog, content, ops, platform, rbac,
                            sessions, tickets)
from business.agent import internal
from business.chat import realtime, turns
from business.core.errors import SoftError, code_to_status, deny
from business.customer import routes as customer
from business.infra import storage
from business.infra.runtime import metrics, ws_hub
from business.infra.storage import cache, one

app = FastAPI(title="Customer Support Business API")


@app.middleware("http")
async def collect_http_metrics(request: Request, call_next):
    started = time.monotonic()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        if request.url.path != "/metrics":
            route = request.scope.get("route")
            path = route.path if route else "/unmatched"
            metrics.observe_http(request.method, path, str(status),
                                 time.monotonic() - started)


@app.get("/metrics")
def prometheus_metrics():
    # 队列与连接池指标采集失败时传空：宁缺勿假，0 会让告警哑掉
    queue = pool = None
    try:
        queue = turns.queue_stats()
    except Exception:
        pass
    try:
        pool = storage.pool_stats()
    except Exception:
        pass
    return Response(metrics.render(
        ws_connections=len(ws_hub.users) + len(ws_hub.admins),
        active_sessions=len(ws_hub.users),
        queue=queue, pool=pool,
    ), media_type="text/plain; version=0.0.4")


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    request.state.denied = True
    return JSONResponse(status_code=exc.status_code,
                        content={"code": exc.status_code, "msg": str(exc.detail)})


@app.exception_handler(SoftError)
async def soft_error(_request, exc):
    return JSONResponse(status_code=code_to_status(exc.code),
                        content={"code": exc.code, "msg": exc.message})


@app.get("/health")
def health():
    try:
        one("SELECT 1 AS ok")
        cache().ping()
    except (pymysql.MySQLError, redis.RedisError, OSError, KeyError):
        deny(503, "业务依赖未就绪")
    return {"status": "ok"}


app.include_router(customer.router)
app.include_router(auth.router)


@app.middleware("http")
async def audit_writes(request: Request, call_next):
    """Write audit for successful admin mutations, preserving soft-error behavior."""
    response = await call_next(request)
    path = request.url.path
    if (request.method not in ("POST", "PUT", "DELETE")
            or not path.startswith("/api/admin")
            or getattr(request.state, "denied", False)):
        return response
    admin = getattr(request.state, "admin", None)
    if admin is not None:
        try:
            auth.write_audit(admin, request,
                             {"POST": "create", "PUT": "update",
                              "DELETE": "delete"}[request.method],
                             auth.audit_object_id(request), path)
        except Exception:
            pass
    return response


for router in (catalog.router, sessions.router, tickets.router, ops.router,
               rbac.router, platform.router, content.router, internal.router,
               realtime.router):
    app.include_router(router)
