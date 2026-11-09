"""Shared identifiers and session limits."""

import os
import re


TENANT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ORDER_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
TOKEN_TTL = 7200

# 聊天会话的保留天数。与 agent 侧 SESSION_RETENTION_DAYS 同源，默认 7 天。
# 判定口径：chat_conversations.last_active_at（最后一次成功受理客户消息），
# 只在 accept_turn 的事务里更新。
CONVERSATION_RETENTION_DAYS = int(os.getenv("SESSION_RETENTION_DAYS", "7"))

# Agent 服务地址。原先在 tickets.py 与 outbox.py 各定义一份，集中到这里避免漂移。
# tickets.py 仍从本模块导入并 re-export，既有 `from business.admin.tickets import AGENT_URL` 不受影响。
AGENT_URL = os.getenv("CS_AGENT_URL", "http://agent:8000")
