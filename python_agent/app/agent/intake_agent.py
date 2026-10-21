"""Tool-free intake agent for greetings and ambiguous customer requests."""

from langchain.agents import create_agent

from app.agent.builder import _check_config_version
from app.agent.llm_config import get_tenant_model
from app.agent.middleware import content_guard

_PROMPT = (
    "你是电商客服的接待与澄清 Agent。只处理问候、能力介绍和不明确的需求，"
    "必要时问一个简短的澄清问题。你没有订单、知识库、工单或交易工具。"
    "不得编造店铺政策、个人订单状态、退款进度或声称已转人工。"
    "用户要查订单时请说明需要登录并提供订单号，或回复「我的订单」；"
    "用户要查询店铺政策时请其明确问题；需要人工时请提示回复「转人工」。"
    "用用户语言简短回答。"
)


async def intake_agent(app, tenant_id: str):
    """Reuse one stateless, tool-free graph per tenant and model config version."""
    state = getattr(app, "state", None)
    if state is not None:
        await _check_config_version(app, tenant_id)
        cache = getattr(state, "intake_agents", None)
        if cache is not None and tenant_id in cache:
            return cache[tenant_id]
    model = await get_tenant_model(app, tenant_id)
    agent = create_agent(model=model, tools=[], middleware=[content_guard],
                         system_prompt=_PROMPT)
    if state is not None and getattr(state, "intake_agents", None) is not None:
        state.intake_agents[tenant_id] = agent
    return agent
