"""LLM 异常友好化与回复后处理（从 main.py 拆出）。"""
import openai
import re
from langchain_core.messages import ToolMessage

from app.kb.evidence import has_explicit_support
from app.infra.config import PROD_MODE

def _friendly_llm_error(e: Exception) -> str:
    """LLM 调用异常 → 用户可读错误提示（生产不暴露内部细节/密钥）"""
    if PROD_MODE:
        if isinstance(e, openai.RateLimitError):
            return "模型服务当前繁忙（限流），请稍后重试"
        if isinstance(e, openai.APITimeoutError):
            return "模型响应超时，请稍后重试"
        if isinstance(e, openai.APIConnectionError):
            return "模型服务连接失败，请稍后重试"
        return "agent服务内部异常"
    return f"agent error:{str(e)}"


_DEMO_NOTICE = "以下为演示环境中的示例答复；涉及的店铺政策不代表真实商家承诺。"


def demo_faq_prefix(tenant_id: str) -> str:
    """Return a visible notice only for the reserved demo tenant."""
    return f"{_DEMO_NOTICE}\n\n" if tenant_id == "t_demo" else ""


def mark_demo_faq_reply(tenant_id: str, content: str) -> str:
    """Make the demo boundary visible even when the model omits its caveat."""
    if not content.strip() or not demo_faq_prefix(tenant_id):
        return content
    if content.startswith(_DEMO_NOTICE):
        return content
    return f"{demo_faq_prefix(tenant_id)}{content}"



def finalize_faq_reply(question: str, content: str, messages: list) -> str:
    """Keep customer-visible FAQ conclusions within retrieved, usable evidence."""
    evidence = [
        str(m.content) for m in messages
        if isinstance(m, ToolMessage) and m.name == "search_kb"
    ]
    has_evidence = any(
        "未找到相关信息" not in item
        and "知识库暂未就绪" not in item
        and has_explicit_support(question, item)
        for item in evidence
    )
    if re.search(r"拆封", question) and re.search(r"退|退款", question):
        return ("仅凭是否拆封还不能判断能否退货。请提供订单号、商品名称、签收时间、"
                "退货原因和商品现状；客服核验订单归属及适用的店铺政策后，才能给出处理意见。")
    if not has_evidence:
        if re.search(r"货到付款|到付|分期|花呗", question):
            return ("现有店铺资料未明确说明是否支持该支付方式。"
                    "请以结算页显示的选项为准，或联系人工客服核实。")
        if re.search(r"课程|数字商品", question) and re.search(r"退|退款", question):
            return ("现有店铺资料未明确说明该数字商品在当前使用情况下的退款条件，"
                    "暂时无法判断能否办理。请提供订单号和商品信息，由客服核验具体条款。")
        return ("现有店铺资料没有明确说明这个具体事项，暂时无法核实。"
                "请查看店铺公示或联系人工客服。")
    return (content.replace("知识库", "店铺资料")
                   .replace("检索到", "查到")
                   .replace("检索", "查询"))
