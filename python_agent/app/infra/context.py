"""请求级协程上下文（ContextVar）。

从 main.py 拆出：trace_id / tenant / thread / 待处理工单 四个上下文变量同时被
lifespan、agent/tools 与各 api 端点使用，独立成模块可避免 api 与 server 循环导入。
"""
from contextvars import ContextVar

# 协程上下文，每个请求独立trace_id
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")
# 协程上下文：当前请求所属租户（多租户隔离）
tenant_var: ContextVar[str] = ContextVar("tenant_id", default="")
thread_var: ContextVar[str] = ContextVar("thread_id", default="")
# 协程上下文：线程存在待处理工单（未接管）——agent 不再重复触发转人工中断，正常回答
pending_ticket_var: ContextVar[bool] = ContextVar("pending_ticket", default=False)
