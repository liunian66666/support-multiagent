# -*- coding: utf-8 -*-
"""[ours] S3 经验召回层：三级开关 + 关键词匹配 + 影子记录。

设计口径（设计方案 §4.4）：
- 开关三级：全局 env EXPERIENCE_RECALL_MODE（off|shadow|inject，默认 off）
  → 租户配置（后续 S4 接租户表）→ 策略版本（S4 发布后生效）。
  影子期 = shadow：召回并记录 experience_hit 事件，但不改动任何 prompt，
  线上应答与现行为逐字节一致（注入路径零流量）。
- 匹配面：route 相容 + applicability.keywords 命中用户消息（简单可解释，
  先于向量召回；召回池只含 published 未过期条目，租户隔离在 SQL 层）。
- fail-open：召回任何异常都降级为无经验路径，绝不阻塞客服应答。
- 注入护栏（inject 模式，S4 才开启）：经验是"参考做法"不是事实来源，
  政策数值仍以知识库证据为准；经验与订单事实冲突时以订单事实为准。
"""
import logging
import os

from app.infra.db.experience import list_recallable_experiences
from app.services import trace

logger = logging.getLogger("agent.experience_recall")

MODE = os.getenv("EXPERIENCE_RECALL_MODE", "off").strip().lower()
_MAX_INJECT = 2  # 单轮最多注入条数，控制 prompt 膨胀与错误经验放大面
_RECALL_POOL = 20


def _mode(tenant_id: str) -> str:
    """三级开关的当前实现：全局 env；租户级/策略级覆盖在 S4 接入。"""
    m = MODE
    tenant_override = os.getenv(f"EXPERIENCE_RECALL_MODE_{tenant_id}", "").strip().lower()
    if tenant_override in ("off", "shadow", "inject"):
        m = tenant_override
    return m if m in ("off", "shadow", "inject") else "off"


def _keywords_of(item: dict) -> list:
    ap = item.get("applicability") or {}
    if isinstance(ap, dict):
        return [str(k).lower() for k in (ap.get("keywords") or []) if str(k).strip()]
    return []


def match_experiences(items: list, route: str, message: str) -> list:
    """route 相容 + 关键词命中。纯函数，供测试直接调用。

    route 相容：经验 route 为空=通用；否则须与当轮 specialist 相同。
    命中分：命中关键词个数 × confidence，取前 _MAX_INJECT 条。
    """
    text = (message or "").lower()
    scored = []
    for item in items:
        route_ok = (not item.get("route")) or item.get("route") == route
        kws = _keywords_of(item)
        hit = [k for k in kws if k and k in text]
        if route_ok and hit:
            scored.append((len(hit) * float(item.get("confidence") or 0.5), item, hit))
    scored.sort(key=lambda x: -x[0])
    return [{"id": it["id"], "score": round(s, 4), "matched": hs,
             "route": it.get("route", ""), "content": it["content"]}
            for s, it, hs in scored[:_MAX_INJECT]]


async def recall_for_turn(app, tenant_id: str, route: str, message: str) -> list:
    """当轮召回：off 直接空；shadow 召回+记事件不改应答；inject 召回供注入。

    返回命中列表（可能为空）。任何异常吞掉返回 []（fail-open）。
    """
    mode = _mode(tenant_id)
    if mode == "off":
        return []
    try:
        pool = await list_recallable_experiences(app, tenant_id, _RECALL_POOL)
        hits = match_experiences(pool, route, message)
    except Exception:
        logger.exception("experience recall failed tenant=%s (fail-open)", tenant_id)
        return []
    if hits:
        trace.record("experience", "experience_hit",
                     mode=mode,
                     experience_ids=[h["id"] for h in hits],
                     scores=[h["score"] for h in hits])
        logger.info("experience %s tenant=%s hits=%s", mode, tenant_id,
                    [h["id"] for h in hits])
    return hits if mode == "inject" else []


# 注入语句的固定护栏前缀：经验只作参考做法，事实仍锚定 KB 证据
_INJECT_HEADER = "以下是经人工审核的历史服务经验（仅参考做法，政策数值以店铺资料为准）："


def augment_message(base_content: str, hits: list) -> str:
    """把命中经验拼进用户消息尾部（inject 模式；影子模式不调用此函数）。"""
    if not hits:
        return base_content
    lines = "\n".join(f"- {h['content']}" for h in hits)
    return (f"{base_content}\n\n{_INJECT_HEADER}\n{lines}\n"
            "（经验与订单核验结果或店铺资料冲突时，一律以核验结果与店铺资料为准。）")
