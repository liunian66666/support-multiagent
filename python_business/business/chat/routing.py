# 中文文案必然包含全角标点
"""业务状态先行的消息分流（纯函数，不依赖 Agent、不碰数据库）。

顺序是固定的，而且**全部先于任何 Agent 调用**：

1. ``human`` —— 已有 ``status=1`` 的工单（人工已接管）：消息转给人工；
2. ``media_notice`` —— 图片/视频：当前能力不支持查看内容；
3. ``handoff`` —— 用户明确要求转人工：创建或复用一张工单；
4. ``pending_notice`` —— 已有 ``status=0`` 的待处理工单：返回"已提交，等待人工接入"的
   **确定提示**，不再把这条消息送去问 Agent；
5. ``agent`` —— 其余情况才进四 Agent。

第 4 条是这个模块存在的理由。收敛前它被写成"有待处理工单 ⇒ 请求体带
``has_pending_ticket=true`` ⇒ Agent 内部走旧图"，于是"业务层在排队"变成了"选哪张图"的
开关：客户排着队，却还在被旧 Agent 答疑。现在排队就是排队，Agent 只处理第 5 条。

顺序也是判据：先把"人工"和"用户自己要求的转人工"排除掉，剩下的才谈排队与 AI。
"""

from __future__ import annotations

import re
from typing import Final

ROUTE_HUMAN: Final = "human"
ROUTE_MEDIA: Final = "media_notice"
ROUTE_HANDOFF: Final = "handoff"
ROUTE_PENDING: Final = "pending_notice"
ROUTE_ANSWER_THEN_HANDOFF: Final = "answer_then_handoff"
ROUTE_AGENT: Final = "agent"

#: Agent 侧结构化拒绝的两种 ``error_code``（见 python_agent/app/api/approval_gate.py）。
#: 前者要建单，后者只是"状态没确认"，绝不能混成同一个重试分支。
ERROR_APPROVAL_PENDING: Final = "approval_pending"
ERROR_APPROVAL_UNKNOWN: Final = "approval_state_unknown"

MEDIA_TYPES: Final = ("image", "video")

HUMAN_ANSWER: Final = "您当前由人工客服接待，消息已转给人工客服，请稍候"
MEDIA_ANSWER: Final = "图片/视频已收到。当前 AI 暂不支持查看内容，如需处理请转人工客服。"
WAITING_ANSWER: Final = "您的问题已提交人工客服（工单号：{ticket_no}），正在等待人工接入，请稍候"
TICKET_ANSWER: Final = "已提交人工客服，工单号：{ticket_no}"
HANDOFF_REASON: Final = "用户主动请求转人工"
APPROVAL_REASON: Final = "会话存在未完成的转人工审批"

_DIRECT_HANDOFF = re.compile(
    r"^(?:麻烦)?(?:请)?(?:帮我|给我|我要|我想|我需要|直接|现在|能不能|能否|可以)?"
    r"(?:转接|转|接入|找|联系)(?:一下)?(?:人工客服|真人客服|人工|真人)"
    r"(?:吧|吗|谢谢|谢谢你)?$"
)
_CONDITIONAL_HANDOFF = re.compile(
    r"(?:(?:退货|退款).{0,50}(?:不可以|不能|无法|不行)|"
    r"(?:不可以|不能|无法|不行).{0,10}(?:退货|退款)).{0,25}"
    r"(?:转接|转|找|联系).{0,10}(?:人工|客服)"
)


def explicit_handoff(text: str) -> bool:
    """只匹配**整句**就是转人工请求，不匹配"提到人工客服"的问句。

    问句归 Agent（它要答题），命令句归业务（它要建单）——这条界线一松，客服就会被
    "人工客服几点下班"这类问题直接建单。
    """
    compact = re.sub(r"[\s，。！？!?,、；;：:]+", "", text or "")
    return bool(_DIRECT_HANDOFF.fullmatch(compact))


def answer_then_handoff(text: str) -> bool:
    """客户要求先核实退货/退款，若不能办理则转人工。"""
    compact = re.sub(r"[\s，。！？!?,、；;：:]+", "", text or "")
    return bool(_CONDITIONAL_HANDOFF.search(compact))


def route_message(*, msg_type: str, handling: bool, pending: bool,
                  handoff: bool, answer_then_transfer: bool = False) -> str:
    """按业务状态决定这条消息归谁处理（见模块文档的固定顺序）。"""
    if handling:
        return ROUTE_HUMAN
    if msg_type in MEDIA_TYPES:
        return ROUTE_MEDIA
    if handoff:
        return ROUTE_HANDOFF
    if pending:
        return ROUTE_PENDING
    if answer_then_transfer:
        return ROUTE_ANSWER_THEN_HANDOFF
    return ROUTE_AGENT


#: 只有"讲流程"的句子才算多余。单纯提到人工（"由人工客服为您办理"、"人工客服会在
#: 1 个工作日内处理"）不在命中范围内——那些是业务事实，删了反而丢信息。
#:
#: 「转人工」「工单」这两类之所以整句删：这一轮的系统提示（🎫 已创建工单 TK-xxx）
#: 已经把它们说全了，答复再说一遍就是同一件事讲两遍，客户反而会怀疑到底转成没有。
_HANDOFF_GUIDE = re.compile(
    r"转人工|转接人工|转接客服"
    r"|(?:创建|生成|提交|建|分配).{0,4}工单|工单号|工单.{0,4}(?:已|稍后|随后)"
    r"|客服热线|服务热线|热线电话|电话客服|客服电话"
    r"|(?:App|APP|官网|官方网站|网站|小程序)[^。；;！!\n]{0,14}?(?:在线客服|客服入口|联系客服|客服中心)"
)
#: 整节小标题命中转人工时，连标题带正文一起删（这段本来就是给人工准备的说明）
_HANDOFF_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*|\*\*)?\s*(?:关于)?\s*(?:转接人工|转人工|联系人工|人工客服|转接)\s*"
    r"(?:客服)?\s*(?:\*\*)?\s*[:：]?\s*$"
)
_HEADING_SHAPE = re.compile(r"^\s*(?:#{1,6}\s+\S.*|\*\*[^*]+\*\*)\s*[:：]?\s*$")
_SENTENCE_END = re.compile(r"(?<=[。！？!?；;])")


def _heading_of(line: str) -> str | None:
    """整行是不是一个小标题（``**标题**`` 或 ``### 标题``）。"""
    return line if _HEADING_SHAPE.match(line) else None


def tidy_handoff_reply(text: str) -> str:
    """把"先答后转"的答复整理成只讲业务的结论。

    为什么必须在这里做：业务层知道**这一轮结束后会自动建单**，模型不知道。不整理的话，
    客户会先读到"您可以在对话中输入「转人工」，或通过客服热线…"，紧接着下面又出现
    "🎫 已为您创建工单（TK-xxx）"——一句话让客户怀疑自己是不是还要再转一次。

    处理三件事，其余原文保留：

    1. 删掉以"转人工 / 人工客服"为标题的整节；
    2. 删掉教客户去找人工的句子（命中 :data:`_HANDOFF_GUIDE` 的那句，不是整段）；
    3. 压掉因此产生的连续空行。

    清理后只剩开场白时返回空串，调用方据此退回"纯转人工"的单条系统提示，不留半截话。
    """
    lines = (text or "").splitlines()
    kept: list[str] = []
    skipping = False
    for raw in lines:
        line = raw.rstrip()
        if _heading_of(line) is not None:
            skipping = bool(_HANDOFF_HEADING.match(line))
            if skipping:
                continue
        elif skipping:
            continue
        pieces = [part for part in _SENTENCE_END.split(line) if part]
        survivors = [part for part in pieces if not _HANDOFF_GUIDE.search(part)]
        line = "".join(survivors).rstrip()
        if pieces and not line.strip():
            continue  # 整行都是引导语，删完只剩空白
        kept.append(line)
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    if len(re.sub(r"[\s，。！？!?,、；;：:]+", "", result)) < 12:
        return ""  # 只剩"您好，为您说明如下"这类开场白，算不上回答
    return result
