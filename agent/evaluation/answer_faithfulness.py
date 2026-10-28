# ruff: noqa: RUF001  # 文案含中文
"""答案忠实度（faithfulness）离线核算：真实模型臂的数值声明，本轮检索到的证据撑得住吗？

阶段5 要的三件数是「候选、命中、**答案忠实度**」，前两件早就量过（§7 那张三层表），
第三件一直只由 Reviewer 规则间接保证（"没有当轮证据的数值要拦"），**没有作为一个被测数报过**。
这个脚本把它补成一个数：拿已采到的臂原始行（`evidence_ids` + `answer`），从 Qdrant **只读**取回
那几条证据的原文，再用评测侧独立量尺 `claim_tokens.claim_tokens`（故意不 import `agent/guards.py`，
否则又是"拿被测方的口径当尺子"，见陷阱 #14）把答复里的「数字+单位」摘出来，逐条问它是否出现在
本轮引用的证据文本里。

三件必须分开算的事，否则这张表会造出假违规：

* **订单类数值不是 FAQ 证据的责任。** 一行里 `roles` 含 `order` 时，"199.00 元 / 已发货"这类
  来自业务接口的核验结果，其原文**不在** `evidence_ids`（那是 KB chunk 的 id）。所以这类声明
  记 `from_order_channel`，不进分母、也不算违规。
* **没有 `evidence_ids` 的行不可核。** 这些行是 general/未委派或纯拒答，记 `no_evidence_row`。
* **演示声明不是声明。** "以下为演示环境中的示例答复" 这类系统后缀是后处理加的，不是模型主张，
  按 `SERVICE_FOOTER`/声明句剥掉。

用法（在容器里跑，宿主到 qdrant 没有路由）：

    MSYS_NO_PATHCONV=1 docker run --rm --env-file "$(pwd -W)/.env" --network cs-customer-service_cs-network \\
      -v "$(pwd):/work:ro" -v "$(pwd)/agent/evaluation/runs:/out" -e PYTHONPATH=/work:/work/python_agent \\
      -e QDRANT_URL=http://qdrant:6333 -w /work/python_agent cs-test:agent \\
      python /work/agent/evaluation/answer_faithfulness.py /out/live_on_dsv41_final.jsonl \
          /out/live_on_dsv41_r2.jsonl -o /out/answer_faithfulness.md --tenant t_demo
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for entry in (str(ROOT), str(ROOT / "python_agent")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from agent.evaluation.claim_tokens import claim_tokens      # 评测侧独立量尺，不 import guards

VALUE_WITH_UNIT = re.compile(r"(?:[零一二两三四五六七八九十百千]+|\d+(?:\.\d+)?)\s*(?:%|元|天|日|周|月|年|小时|折)")
# 后处理加的客户面文案：不是模型主张，别算成"未经证据支持的声明"
DECOR = re.compile(r"以下为演示环境中的示例答复[^\n]*|本条为示例/测试政策[^\n]*|不代表真实商家承诺[^\n]*|"
                   r"该条为示例/测试政策[^\n]*|以上回答仅供参考[^\n]*")
NORM = re.compile(r"[\s*_`>，,。；;：:（）()、「」\"']")


def norm(text: str) -> str:
    """量尺的归一化：去空白与 markdown 强调，再去中英标点。

    与 `score_heldout.norm` 同源但**更宽**（多去了标点），因为这里问的是"这个数值串在不在证据里"，
    而中文证据常写成「满 99 元」「99元包邮」两种形态。宽归一化只会少报违规、不会多报——
    报出来的每一条都要人工能回看（脚本会把上下文打出来）。
    """
    return NORM.sub("", text or "")


def fetch_payloads(tenant: str, ids: list[str]) -> dict[str, str]:
    """按 point id 只读取回 KB 原文；缺 id 就少一个分母，绝不猜。"""
    from qdrant_client import QdrantClient
    from app.infra.db.tenant import collection_name

    if not ids:
        return {}
    client = QdrantClient(url=__import__("os").environ.get("QDRANT_URL", "http://qdrant:6333"))
    coll = collection_name(tenant)
    if not client.collection_exists(coll):
        raise SystemExit(f"refusing to run: collection {coll} missing")
    out: dict[str, str] = {}
    # retrieve 一次拿不全时分批；payload 字段名在不同版本里是 text/content/page_content
    for chunk_start in range(0, len(ids), 256):
        batch = ids[chunk_start:chunk_start + 256]
        for rec in client.retrieve(coll, ids=batch, with_payload=True):
            payload = rec.payload or {}
            text = next((payload[k] for k in ("text", "content", "page_content", "chunk")
                         if isinstance(payload.get(k), str)), "")
            out[str(rec.id)] = text
    return out


def claims_of(answer: str) -> list[str]:
    """答复里的「数字+单位」主张（订单号由 guards/规则那侧另算，这里只核算政策类数值）。

    用 `claim_tokens()` 而不是自己的正则：它已经把中文数字归一到"阿拉伯数字+单位"
    （「九十九」→`99元`、「七天」→`7天`），这正是"证据里写 99 元、答复说九十九块"该被判成
    同一件事的原因。返回值里剔掉单号那一路。
    """
    cleaned = DECOR.sub("", answer or "")
    out = []
    for token in claim_tokens(cleaned):
        compact = token.replace(" ", "")
        if VALUE_WITH_UNIT.fullmatch(compact):
            out.append(compact)
    return sorted(set(out))


def audit(rows: list[dict], payloads: dict[str, str]) -> dict:
    per_case, counts = [], {"supported": 0, "unsupported": 0,
                           "from_order_channel": 0, "no_evidence_row": 0, "missing_payload": 0}
    for r in rows:
        answer = r.get("answer") or ""
        claims = claims_of(answer)
        evidence_ids = list(r.get("evidence_ids") or [])
        texts = [payloads.get(i, "") for i in evidence_ids]
        missing_payload = sum(1 for i in evidence_ids if i not in payloads)
        roles = list(r.get("roles") or [])
        if not claims:
            continue
        if not evidence_ids:
            counts["no_evidence_row"] += len(claims)
            per_case.append({"id": r["id"], "claims": claims, "verdict": "no_evidence_row",
                             "roles": roles, "evidence": 0})
            continue
        joined = norm(" ".join(t for t in texts if t))
        order_sourced = "order" in roles
        sup, unsup = [], []
        for c in claims:
            (sup if norm(c) in joined else unsup).append(c)
        for c in sup:
            counts["supported"] += 1
        for c in unsup:
            # 有 order 通道的行，未支持的那条更可能来自业务接口而非 KB —— 单列，不算违规
            if order_sourced:
                counts["from_order_channel"] += 1
            else:
                counts["unsupported"] += 1
        counts["missing_payload"] += missing_payload
        per_case.append({"id": r["id"], "claims": claims, "supported": sup, "unsupported": unsup,
                         "verdict": ("ok" if not unsup else
                                     ("order_channel" if order_sourced else "UNVERIFIED")),
                         "roles": roles, "evidence": len(texts), "evidence_ids": evidence_ids})
    total = counts["supported"] + counts["unsupported"] + counts["from_order_channel"]
    counts.update({"claims_total": total,
                   "faithfulness": (counts["supported"] / total) if total else None})
    return {"counts": counts, "cases": per_case, "rows": len(rows)}


def render(results: dict[str, dict], tenant: str) -> str:
    lines = [f"# 答案忠实度核算（租户 `{tenant}` · 真实模型臂离线复核）", "",
             "问题：答复里每一个「数字+单位」的政策类主张，本轮**真的检索到并引用**的证据里有没有它？",
             "尺子：`agent/evaluation/claim_tokens.claim_tokens`（评测侧独立实现，**不 import** "
             "`agent/guards.py`，避免拿被测方口径当尺子）。证据原文按行内 `evidence_ids` 从 Qdrant 只读取回。",
             "", "| 臂 | 行覆盖 | 可核主张 | 证据支持 | 未支持（真违规） | 来自订单通道 | 无证据行 | 取不到原文的 id | 忠实度 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, res in results.items():
        c = res["counts"]
        faith = "—" if c["faithfulness"] is None else f"{c['faithfulness']:.1%}"
        lines.append(f"| `{name}` | {res['rows']} | {c['claims_total']} | {c['supported']} | "
                     f"**{c['unsupported']}** | {c['from_order_channel']} | {c['no_evidence_row']} | "
                     f"{c['missing_payload']} | {faith} |")
    lines += ["", "怎么读这张表：",
              "* **未支持**＝这一行没有订单通道、却说了证据里找不到的数值 ⇒ 这才是"
              "「无证据政策数值」的真违规，逐条列在下面。",
              "* **来自订单通道**＝该行本轮委派过 Order Agent，数值可能来自业务接口核验结果；"
              "它的原文不在 KB chunk 里，所以**不能**算违规，也不能算已证——只能说这张表管不到它。",
              "* **无证据行**＝纯寒暄/拒答/未委派，没有任何 `evidence_ids` 可比。", ""]
    bad = [(n, case) for n, res in results.items() for case in res["cases"]
           if case.get("unsupported") and "order" not in case["roles"]]
    if bad:
        lines.append("## 逐条真违规（无订单通道却说了证据里没有的数值）")
        lines.append("")
        for arm, case in bad:
            lines.append(f"- `{arm}` **{case['id']}**：主张 {case['claims']}；"
                         f"证据 {case['evidence']} 段里没找到 {case['unsupported']}"
                         f"（roles={case['roles']}）")
        lines.append("")
    else:
        lines.append("## 逐条真违规\n\n两臂都**没有**：所有未支持数值都落在有订单通道的行上。")
        lines.append("")
    order_cases = [(n, case) for n, res in results.items() for case in res["cases"]
                   if case.get("unsupported") and "order" in case["roles"]]
    if order_cases:
        lines.append("## 订单通道内未证（这张表管不到，但列出来别让它隐身）")
        lines.append("")
        for arm, case in order_cases:
            lines.append(f"- `{arm}` **{case['id']}**：未支持 {case['unsupported']}"
                         f"（本轮 roles={case['roles']}，KB 证据 {case['evidence']} 段）")
        lines.append("")
    lines.append("⚠ 口径边界：本表**不是**「答复对不对」，只是「数值有没有当轮 KB 证据」。"
                 "归一化比 `score_heldout.norm` 更宽（多去了中英标点），所以只会**少报**违规；"
                 "每一条未支持都把 id 与主张打出来，人工可回看原行。")
    return "\n".join(lines) + "\n"


def main(args: argparse.Namespace) -> int:
    files = [Path(p) for p in args.files]
    ids: set[str] = set()
    results: dict[str, dict] = {}
    for f in files:
        rows = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
        for r in rows:
            ids.update(r.get("evidence_ids") or [])
        results[f.name] = {"rows": len(rows), "_rows": rows}
    payloads = fetch_payloads(args.tenant, sorted(ids))
    print(f"[evidence] 去重后 {len(ids)} 个 id，取回原文 {len(payloads)} 条", flush=True)
    for name, res in results.items():
        res.update(audit(res.pop("_rows"), payloads))
    out = Path(args.out)
    out.write_text(render(results, args.tenant), encoding="utf-8")
    print(render(results, args.tenant))
    print(f"→ {out}")
    # 退出码只由"真违规"驱动：订单通道未证不该让闸门变红，但也不能隐身（表里单列）
    return 1 if any(res["counts"]["unsupported"] for res in results.values()) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", help="带 evidence_ids 与 answer 的臂 jsonl")
    parser.add_argument("--tenant", default="t_demo")
    parser.add_argument("-o", "--out", default="agent/evaluation/runs/answer_faithfulness.md")
    raise SystemExit(main(parser.parse_args()))
