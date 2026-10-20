"""LangGraph 中间件：内容守卫与自动签名（从 agent_factory.py 拆出）。"""
from langchain.agents.middleware import after_model, before_model
from langchain_core.messages import HumanMessage


# [ours] 修复：content_guard 必须以 @before_model 装饰后再传给 create_agent。
# 当前 langchain 版本在 factory 中按实例访问 `m.__class__.wrap_tool_call`（AgentMiddleware 才有），
# 直接传裸函数会抛 AttributeError: type object 'function' has no attribute 'wrap_tool_call'，
# 导致 FAQ / general 两条需要构建 Agent 的路径全部报“调用agent失败”。
# can_jump_to=["end"] 是 {"jump_to": "end"} 生效的前提：未声明时该跳转不会注册条件边。
@before_model(can_jump_to=["end"])
def content_guard(state, runtime):
    last_msg = state["messages"][-1] if state.get("messages") else None
    if not last_msg:
        return None
    content = str(getattr(last_msg, 'content', ''))
    blocked = ["黄X", "X博", "违法"]
    for word in blocked:
        if word in content:
            return {
                "jump_to": "end",
                "messages": [HumanMessage(content="抱歉，我不能处理这个请求。")]
            }
    return None


@after_model
def auto_signature(state, runtime):
    """已废弃：签名改为 Prompt 管理维护（不再代码追加，避免同消息重复累积）
    保留空实现仅为兼容历史导入，middleware 不再挂载。
    """
    return None
