"""Collect the exact evidence candidates that the customer-facing KB paths may use."""
import argparse
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
from psycopg_pool import AsyncConnectionPool
from qdrant_client import QdrantClient

from app.agent.retrieval import ensure_tenant_vector
from app.infra.config import POSTGRES_DSN, QDRANT_URL
from app.infra.db.tenant import collection_name
from app.kb.evidence import approved_evidence


async def collect(cases):
    client = QdrantClient(url=QDRANT_URL)
    for tenant in {case.get("tenant_id", "t_demo") for case in cases}:
        if not client.collection_exists(collection_name(tenant)):
            raise RuntimeError(f"collection missing: {collection_name(tenant)}")
    pool = AsyncConnectionPool(POSTGRES_DSN, min_size=1, max_size=2, open=False,
                               kwargs={"autocommit": True})
    await pool.open()
    try:
        async with httpx.AsyncClient() as http_client:
            app = SimpleNamespace(state=SimpleNamespace(
                http_client=http_client, qdrant_client=client, db_pool=pool,
                vec_dim=0, tenant_stores={}, tenant_retrievers={}))
            rows = []
            for case in cases:
                tenant = case.get("tenant_id", "t_demo")
                await ensure_tenant_vector(app, tenant)
                store = app.state.tenant_stores.get(tenant)
                if store is None:
                    raise RuntimeError(f"retriever unavailable: {tenant}")
                start = time.perf_counter()
                matches = await store.asimilarity_search_with_relevance_scores(
                    case["query"], k=3)
                candidates = [doc for doc, _ in matches]
                accepted = await approved_evidence(app, tenant, case["query"], candidates)
                rows.append({
                    "id": case["id"],
                    "retrieved_docs": [str(doc.metadata.get("doc_id") or "") for doc in accepted],
                    "accepted_chunk_ids": [str(doc.metadata.get("chunk_id") or "") for doc in accepted],
                    "candidate_docs": [str(doc.metadata.get("doc_id") or "") for doc in candidates],
                    "candidate_scores": [round(float(score), 4) for _, score in matches],
                    "clause_covered": any(
                        all(term in str(doc.page_content)
                            for term in case.get("gold_evidence_terms", []))
                        for doc in accepted
                    ) if case.get("gold_evidence_terms") else None,
                    "latency_ms": round((time.perf_counter() - start) * 1000, 2),
                })
            return rows
    finally:
        await pool.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gold", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = [json.loads(line) for line in args.gold.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n"
                      for row in asyncio.run(collect(cases)))
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
