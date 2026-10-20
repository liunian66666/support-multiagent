"""LLM 异常友好化与回复后处理（从 main.py 拆出）。"""
import openai

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
