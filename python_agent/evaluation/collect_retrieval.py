"""Read-only live Qdrant retrieval collector for a fixed gold query set.

Run inside the Agent container with its configured embedding model and Qdrant.
It refuses to create a missing collection and writes no customer conversations.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
from qdrant_client import QdrantClient

from app.agent.retrieval import ensure_tenant_vector
from app.infra.config import QDRANT_URL
from app.infra.db.tenant import collection_name


async def collect(cases):
    client = QdrantClient(url=QDRANT_URL)
    tenants = {case.get("tenant_id", "t_demo") for case in cases}
    for tenant in tenants:
        if not client.collection_exists(collection_name(tenant)):
            raise RuntimeError(f"collection missing: {collection_name(tenant)}")
    async with httpx.AsyncClient() as http_client:
        app = SimpleNamespace(state=SimpleNamespace(
            http_client=http_client, qdrant_client=client, vec_dim=0,
            tenant_stores={}, tenant_retrievers={}))
        rows = []
        for case in cases:
            tenant = case.get("tenant_id", "t_demo")
            await ensure_tenant_vector(app, tenant)
            store = app.state.tenant_stores.get(tenant)
            if store is None:
                raise RuntimeError(f"retriever unavailable: {tenant}")
            start = time.perf_counter()
            matches = await store.asimilarity_search_with_relevance_scores(case["query"], k=3)
            elapsed = round((time.perf_counter() - start) * 1000, 2)
            rows.append({"id": case["id"], "retrieved_docs": [
                str((doc.metadata or {}).get("doc_id", "")) for doc, _ in matches],
                "relevance_scores": [round(float(score), 4) for _, score in matches],
                "latency_ms": elapsed})
        return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gold", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = [json.loads(line) for line in args.gold.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    rows = asyncio.run(collect(cases))
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
