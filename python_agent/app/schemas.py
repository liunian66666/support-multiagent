"""
P4-3：Pydantic 数据模型模块
从 main.py 拆分，集中管理所有 API 请求/响应模型。
"""
import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# 轮次 ID 要同时充当 LangGraph 消息 id 前缀与 trace id，字符集必须两边都安全。
# 业务层的 turn_id 是 UUID/雪花串，`:` `-` `_` `.` 都出现过，故一并放行。
TURN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class ChatRequest(BaseModel):
    """对话请求。

    ``turn_id`` 是**幂等键**，不是追踪号：同一 ID + 同一问题重复提交只对应一轮结果
    （重放不再计费、不再调用模型），同一 ID + 不同问题明确报冲突。省略时按
    ``X-Trace-Id`` 兼旧调用方，语义与升级前一致。
    """
    thread_id: str = Field(min_length=1, description="会话ID")
    user_message: str = Field(min_length=1, description="用户消息")
    turn_id: Optional[str] = Field(
        default=None, max_length=64,
        description="幂等键；业务 Worker 每轮生成一次，重试必须复用同一个值")
    has_pending_ticket: bool = Field(default=False, description="线程存在待处理工单（业务层排队事实）。仅用于抑制重复转人工，不决定走哪张图")

    @field_validator("turn_id")
    @classmethod
    def _valid_turn_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        candidate = value.strip()
        if not candidate:
            return None
        if not TURN_ID_PATTERN.match(candidate):
            raise ValueError("turn_id 只允许字母、数字与 _.:- ，长度 1~64")
        return candidate


class ResumeRequest(BaseModel):
    """恢复人工接管请求"""
    thread_id: str = Field(min_length=1)
    confirmed: bool
    wait_time: int = Field(ge=0)
    ticket_id: str


class AgentResp(BaseModel):
    """Agent 响应。

    ``answer_id`` 与 ``turn_id`` 是同一个值（轮次归因锚点）：业务层把
    ``answer_id`` 落库、把评价挂在它上面，所以两种传输必须是同一个字符串。
    """
    success: bool
    is_interrupt: bool
    interrupt_data: Optional[dict] = None
    content: Optional[str] = None
    trace_id: Optional[str] = None
    # [ours] S1 轮次归因锚点（附加字段，旧客户端忽略即可）
    answer_id: Optional[str] = None
    # 阶段1：显式轮次 ID 与机器可读的错误分类（旧客户端忽略即可）
    turn_id: Optional[str] = None
    outcome: Optional[str] = None
    error_code: Optional[str] = None
    agents: Optional[list[str]] = None
    evidence_ids: Optional[list[str]] = None
    """本轮答复实际引用的已发布证据 ID；JSON 与 SSE 必须给同一组。"""


class SessionCleanupReq(BaseModel):
    """会话清理请求"""
    days: int = Field(default=7, ge=1, le=365, description="回收多少天前未活跃的会话")



class SessionCleanupItem(BaseModel):
    """协调清理的单个线程"""
    thread_id: str = Field(min_length=1, max_length=200)
    has_pending_ticket: bool = Field(
        default=False, description="该线程有未完成审批，跳过检查点删除并保留索引")


class SessionCleanupBatchReq(BaseModel):
    """按线程批量清理（业务层驱动，逐项返回成功/失败）"""
    threads: list[SessionCleanupItem] = Field(min_length=1, max_length=500)
    days: int = Field(default=7, ge=1, le=365)


class TenantWarmReq(BaseModel):
    """租户预热请求"""
    tenant_id: str = Field(min_length=1, description="要预热的租户 ID")


class PromptReq(BaseModel):
    """Prompt 管理请求"""
    key: str = Field(min_length=1, description="Prompt标识")
    title: str = ""
    content: str = Field(min_length=1, description="Prompt内容")
    is_active: bool = False
