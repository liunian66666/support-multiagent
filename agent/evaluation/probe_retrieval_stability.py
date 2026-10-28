# 中文题集与文案
"""Read-only retrieval stability probe for the known 金额/时限 题 (phase 5).

Run inside the project's test container, on the compose network, with the repo mounted:

    docker run --rm --network cs-customer-service_cs-network \\
      -v "$ROOT:/work:ro" -e PYTHONPATH=/work:/work/python_agent \\
      -w /work/python_agent cs-test:agent \\
      python /work/agent/evaluation/probe_retrieval_stability.py --repeat 5 --limit 20

Read-only guarantees, and why each one is here:

* refuses to run if the tenant collection does not exist — ``ensure_tenant_vector``
  would otherwise *create* it and start embedding the default corpus;
* never writes ``kb_hits``, never touches the answer cache, never calls a write route;
* uses the same call path the customer-facing FAQ path uses
  (``asimilarity_search_with_relevance_scores`` then ``approved_evidence``), so a
  measured swing is a swing the service can actually have.

Cost note: each query is an embedding call against the tenant's embedding model, so
``--repeat`` multiplies real API usage. That is why the default set is only the cases
whose expected evidence contains a number — the questions the project already knows
swing — not all 100 gold cases.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import statistics
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
from qdrant_client import QdrantClient

GOLD = Path(__file__).resolve().parents[2] / "python_agent" / "evaluation" / "gold_retrieval.jsonl"
NUMERIC = ("元", "天", "小时", "工作日", "%", "折")
# a policy number with its unit, as it appears in a chunk; "7 天" and "7天" both match
NUM_VALUE = re.compile(rf"\d+(?:\.\d+)?\s*(?:{'|'.join(NUMERIC)})")


def wants_number(case: dict) -> bool:
    """A case is a 金额/时限 target when its expected evidence itself contains a number."""
    return any(any(char.isdigit() for char in term) for term in case.get("gold_evidence_terms", []))


def load_cases(limit: int, numeric_only: bool) -> list[dict]:
    cases = [json.loads(line) for line in GOLD.read_text(encoding="utf-8").splitlines() if line.strip()]
    if numeric_only:
        cases = [case for case in cases if wants_number(case)]
    return cases[:limit]


async def probe(cases: list[dict], repeat: int, out: Path) -> list[dict]:
    from psycopg_pool import AsyncConnectionPool

    from app.agent.retrieval import ensure_tenant_vector
    from app.infra.config import POSTGRES_DSN, QDRANT_URL
    from app.infra.db.tenant import collection_name
    from app.infra.db_pool import _pool_conn  # the same pool helper the service uses
    from app.kb.evidence import approved_evidence

    client = QdrantClient(url=QDRANT_URL)
    tenants = {c.get("tenant_id", "t_demo") for c in cases}
    for tenant in tenants:
        if not client.collection_exists(collection_name(tenant)):
            raise SystemExit(f"refusing to run: collection {collection_name(tenant)} missing "
                             "(creating it would write to the vector store)")

    pool = AsyncConnectionPool(POSTGRES_DSN, min_size=1, max_size=2, open=False,
                               kwargs={"autocommit": True})
    await pool.open()
    rows: list[dict] = []
    try:
        async with httpx.AsyncClient() as http_client:
            app = SimpleNamespace(state=SimpleNamespace(
                http_client=http_client, qdrant_client=client, db_pool=pool,
                vec_dim=0, tenant_stores={}, tenant_retrievers={}))
            _ = _pool_conn  # imported to document that reads go through the service's pool
            for case in cases:
                tenant = case.get("tenant_id", "t_demo")
                await ensure_tenant_vector(app, tenant)
                store = app.state.tenant_stores.get(tenant)
                if store is None:
                    raise SystemExit(f"retriever unavailable for {tenant}")
                runs = []
                for _attempt in range(repeat):
                    start = time.perf_counter()
                    matches = await store.asimilarity_search_with_relevance_scores(case["query"], k=3)
                    candidates = [doc for doc, _score in matches]
                    accepted = await approved_evidence(app, tenant, case["query"], candidates)
                    runs.append({
                        "candidate_docs": [str(d.metadata.get("doc_id") or "") for d in candidates],
                        "candidate_chunks": [str(d.metadata.get("chunk_id") or "") for d in candidates],
                        "accepted_chunks": [str(d.metadata.get("chunk_id") or "") for d in accepted],
                        # 6 decimals on purpose: a 4-decimal rounding hid the 0.0001 gap that
                        # decides which sibling chunk wins slot three.
                        "scores": [round(float(s), 6) for _d, s in matches],
                        # what each candidate would actually put in front of the model: the
                        # numeric policy expressions inside it. Two chunks swapping slots is
                        # only harmless if they carry the same numbers.
                        "candidate_values": [sorted(set(NUM_VALUE.findall(str(d.page_content))))
                                             for d in candidates],
                        "clause_covered": all(
                            term in " ".join(str(d.page_content) for d in accepted)
                            for term in case.get("gold_evidence_terms", []))
                        if case.get("gold_evidence_terms") else None,
                        "latency_ms": round((time.perf_counter() - start) * 1000, 2),
                    })
                signatures = {tuple(r["candidate_chunks"]) for r in runs}
                gold = set(case.get("gold_docs", []))
                hit_rate = statistics.fmean([bool(gold & set(r["candidate_docs"])) for r in runs])
                coverage = [r["clause_covered"] for r in runs if r["clause_covered"] is not None]
                rows.append({
                    "id": case["id"], "query": case["query"], "tenant": tenant,
                    "gold_docs": sorted(gold), "repeat": repeat,
                    "distinct_candidate_sets": len(signatures),
                    # the same measurement at two levels: which chunks, and which exact scores.
                    # Sets can stay identical while every score moves, which is what float drift
                    # in the embedding call looks like.
                    "distinct_score_tuples": len({tuple(r["scores"]) for r in runs}),
                    "distinct_value_tuples": len({tuple(tuple(v) for v in r["candidate_values"])
                                                 for r in runs}),
                    "gold_hit_rate": round(hit_rate, 3),
                    "clause_coverage_rate": round(statistics.fmean(coverage), 3) if coverage else None,
                    "score_spread": round(max(max(s for s in r["scores"]) for r in runs)
                                         - min(min(r["scores"]) for r in runs), 4),
                    "runs": runs,
                })
    finally:
        await pool.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    unstable = [r for r in rows if r["distinct_candidate_sets"] > 1 or (r["gold_hit_rate"] or 0) < 1]
    score_drift = [r for r in rows if r["distinct_score_tuples"] > 1]
    print(f"cases={len(rows)} repeat={repeat} → 写入 {out}")
    print(f"候选集不唯一或金标未稳定命中：{len(unstable)} 题；"
          f"分数逐位不同的题：{len(score_drift)} 题；"
          f"金标丢过的题：{len([r for r in rows if (r['gold_hit_rate'] or 0) < 1])} 题；"
          f"条款覆盖掉过的题：{len([r for r in rows if (r['clause_coverage_rate'] or 1) < 1])} 题")
    for row in unstable[:12]:
        print(f"  {row['id']:<24} sets={row['distinct_candidate_sets']} "
              f"score_tuples={row['distinct_score_tuples']} "
              f"value_tuples={row['distinct_value_tuples']} "
              f"hit={row['gold_hit_rate']} cover={row['clause_coverage_rate']}")
        # which chunk sits where, and the numbers it would have handed the model
        seen: dict[tuple, tuple] = {}
        for run in row["runs"]:
            key = tuple(c[:8] for c in run["candidate_chunks"])
            seen.setdefault(key, (tuple(tuple(v) for v in run["candidate_values"]),
                                  tuple(run["scores"])))
        for shape, (values, scores) in seen.items():
            print(f"     {list(shape)} scores={list(scores)} values={[list(v) for v in values]}")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="read-only retrieval stability probe")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--all", action="store_true", help="include non-numeric gold cases too")
    parser.add_argument("--out", default=str(Path(__file__).parent / "runs" / "retrieval_stability.jsonl"))
    args = parser.parse_args()
    stamp = hashlib.sha1(str(args.repeat).encode()).hexdigest()[:6]
    out = Path(args.out if args.all else args.out.replace(".jsonl", f"_numeric{stamp}.jsonl"))
    asyncio.run(probe(load_cases(args.limit, not args.all), args.repeat, out))


if __name__ == "__main__":
    main()
