"""Authoritative publication and applicability checks for retrieved KB chunks."""
import hashlib
import re
from datetime import datetime, timezone

from app.infra.db_pool import _pool_conn

_STALE_FACTS = ("当前主项目尚未接通真实订单业务接口",)
_SPECIFIC_FACTS = (
    (r"比特币|虚拟币|加密货币", r"比特币|虚拟币|加密货币"),
    (r"货到付款|到付", r"货到付款|到付"),
    (r"花呗|分期", r"花呗|分期"),
    (r"增值税|专票", r"增值税|专票"),
    (r"退货地址|收件人电话|寄回地址", r"退货地址|寄回地址|收件人电话"),
    (r"开课|课程表", r"开课|课程表"),
    (r"试听", r"试听"),
    (r"积分", r"积分"),
    (r"保质期", r"保质期"),
    (r"学了一半|学习一半|已学习", r"学了一半|学习一半|已学习|学习进度"),
)
_OUT_OF_DOMAIN = re.compile(r"航班|机票|火车时刻|天气预报")
_ORDER_ID = re.compile(r"(?:ORD-\d[\w-]*|TB\d+|JD\d+|PDD\d+)", re.I)


def stable_chunk_id(tenant_id: str, doc_id: str, index: int, text: str) -> str:
    raw = f"{tenant_id}\0{doc_id}\0{index}\0{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _scope_of_question(query: str) -> str:
    if re.search(r"课程|会员|数字商品|电子书|软件", query):
        return "digital"
    if re.search(r"实物|衣服|吊牌|快递|拆封|签收", query):
        return "physical"
    return "unknown"


def _chunk_scope(text: str) -> str:
    physical = bool(re.search(r"吊牌|二次销售|拆封|签收|退回运费|往返运费", text))
    digital = bool(re.search(r"数字商品|数字课程|课程学习|虚拟商品|学习进度", text))
    if physical and not digital:
        return "physical"
    if digital and not physical:
        return "digital"
    return "mixed" if physical and digital else "unspecified"


def has_explicit_support(query: str, text: str) -> bool:
    if _OUT_OF_DOMAIN.search(query) or _ORDER_ID.search(query):
        return False
    for question_pattern, evidence_pattern in _SPECIFIC_FACTS:
        if re.search(question_pattern, query) and not re.search(evidence_pattern, text):
            return False
    return True


def select_evidence(docs: list, query: str, policy_rows: dict, now=None) -> list:
    """Reject unapproved/expired/out-of-scope documents and unsupported specifics."""
    now = now or datetime.now(timezone.utc)
    requested_scope = _scope_of_question(query)
    selected = []
    for doc in docs:
        meta = getattr(doc, "metadata", {}) or {}
        doc_id = str(meta.get("doc_id") or "")
        row = policy_rows.get(doc_id)
        if not row:
            continue
        status, state, scope, valid_from, valid_until = row
        if status != 1 or state not in ("published", "demo"):
            continue
        if valid_from and now < valid_from:
            continue
        if valid_until and now >= valid_until:
            continue
        if requested_scope != "unknown" and scope not in ("all", requested_scope):
            continue
        content = str(getattr(doc, "page_content", ""))
        if any(stale in content for stale in _STALE_FACTS):
            continue
        chunk_scope = _chunk_scope(content)
        if requested_scope != "unknown" and chunk_scope not in (
            requested_scope, "mixed", "unspecified"
        ):
            continue
        if not has_explicit_support(query, content):
            continue
        meta["policy_state"] = state
        meta["product_scope"] = scope
        meta.setdefault("chunk_id", stable_chunk_id(str(meta.get("tenant_id") or ""), doc_id,
                                                    int(meta.get("chunk_index") or 0), content))
        selected.append(doc)
    return selected


async def approved_evidence(app, tenant_id: str, query: str, docs: list) -> list:
    """Use PostgreSQL as publication source of truth, including for old Qdrant points."""
    doc_ids = list({str((getattr(d, "metadata", {}) or {}).get("doc_id") or "") for d in docs})
    doc_ids = [doc_id for doc_id in doc_ids if doc_id]
    if not doc_ids:
        return []
    async with _pool_conn(app) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT doc_id, status, policy_state, product_scope, valid_from, valid_until "
                "FROM kb_documents WHERE tenant_id = %s AND doc_id = ANY(%s)",
                (tenant_id, doc_ids),
            )
            rows = await cur.fetchall()
    policy_rows = {r[0]: r[1:] for r in rows}
    return select_evidence(docs, query, policy_rows)
