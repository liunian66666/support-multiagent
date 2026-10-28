# ruff: noqa: RUF001
"""Which layer is non-deterministic: the embedding call, or the vector search?

``probe_retrieval_stability.py`` measured that end-to-end FAQ retrieval flips its top-3
composition in ~5.5% of case-batches — always at slot 2/3, always across a score gap of
6e-5..1.4e-3. That is not enough to decide whether *our* evidence-selection layer owns a
fix, because two different causes look identical from the outside:

* the embedding endpoint returns a slightly different vector for the same text — then no
  ranking rule inside this repo can stabilise it;
* the vector is identical and Qdrant's approximate search returns different neighbours —
  then a deterministic tie-break in the selection layer *is* the fix.

So this probe separates them by construction:

1. embed the same query N times → hash the raw float array → ``distinct_vectors``;
2. search Qdrant with **one fixed vector** M times → compare id sets → ``distinct_search_sets_fixed_vector``;
3. run the service path (``asimilarity_search_with_relevance_scores``) M times →
   ``distinct_service_sets``, so any variance the wrapper itself adds becomes visible;
4. report the score gap at the cutoff (``cutoff_gap``) — how close the last included
   candidate is to the first excluded one.

Read-only: refuses to run when the tenant collection is missing (creating it would embed
the default corpus), writes nothing to Postgres or Qdrant, and only calls the embedding
endpoint — the same cost a customer question already pays.

    bash agent/evaluation/run_all.sh embedding-drift
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import httpx
from qdrant_client import QdrantClient

# the four cases that flipped across the multi-batch measurement, plus two that never did
FLIPPED = ["ship-full99", "ship-tracking-24h", "ret-window", "ship-fee-under99"]
STABLE = ["return-window", "refund-bank"]


def digest(vector: list[float]) -> str:
    """Hash of a float array with no rounding: the whole question is whether the
    values differ at all, so precision must not be thrown away here."""
    return hashlib.sha1(struct.pack(f"{len(vector)}f", *vector)).hexdigest()[:12]


def search_ids(client: QdrantClient, collection: str, vector: list[float], limit: int) -> tuple:
    """Point ids for one fixed vector. qdrant-client 1.19 removed ``.search()``."""
    return tuple(sorted(p.id for p in client.query_points(collection_name=collection,
                                                          query=vector, limit=limit).points))


def search_scored(client: QdrantClient, collection: str, vector: list[float], limit: int) -> list:
    return client.query_points(collection_name=collection, query=vector, limit=limit).points


async def one_query(app, client, collection: str, case: dict, embed_times: int,
                    search_times: int, k: int) -> dict:
    from app.agent.retrieval import ensure_tenant_vector

    store = await ensure_tenant_vector(app, case.get("tenant_id", "t_demo"))
    if store is None:
        raise SystemExit("embedding/向量能力不可用，本探针无法分层")
    question = case["query"]

    vectors = [await store.embeddings.aembed_query(question) for _ in range(embed_times)]
    fixed = list(vectors[0])

    ids_per_call = [search_ids(client, collection, fixed, k) for _ in range(search_times)]
    scored = search_scored(client, collection, fixed, k + 2)

    service_sets = []
    for _ in range(search_times):
        matches = await store.asimilarity_search_with_relevance_scores(question, k=k)
        service_sets.append(tuple(sorted(str(d.metadata.get("chunk_id") or "")
                                         for d, _score in matches)))

    gaps = [round(scored[i].score - scored[i + 1].score, 6)
            for i in range(min(k, len(scored) - 1))]
    return {
        "id": case["id"], "query": question, "k": k,
        "dim": len(fixed),
        "embed_times": embed_times, "search_times": search_times,
        "distinct_vectors": len({digest(v) for v in vectors}),
        "distinct_search_sets_fixed_vector": len(set(ids_per_call)),
        "distinct_service_sets": len(set(service_sets)),
        # gap between the last included candidate and the first excluded one
        "cutoff_gap": round(scored[k - 1].score - scored[k].score, 6) if len(scored) > k else None,
        "adjacent_gaps": gaps,
        "top_scores": [round(p.score, 6) for p in scored[:k]],
    }


async def main(args: argparse.Namespace) -> None:
    from app.infra.config import QDRANT_URL
    from app.infra.db.tenant import collection_name

    wanted = FLIPPED + STABLE
    cases = [json.loads(line) for line in
             Path(args.gold).read_text(encoding="utf-8").splitlines() if line.strip()]
    picked = [c for cid in wanted for c in cases if c.get("id") == cid]
    if not picked:
        raise SystemExit(f"gold 里找不到 {wanted}，检查 {args.gold}")

    client = QdrantClient(url=QDRANT_URL)
    col = collection_name("t_demo")
    if not client.collection_exists(col):
        raise SystemExit(f"refusing to run: collection {col} missing "
                         "(creating it would write to the vector store)")

    async with httpx.AsyncClient() as http_client:
        app = SimpleNamespace(state=SimpleNamespace(
            http_client=http_client, qdrant_client=client, db_pool=None,
            vec_dim=0, tenant_stores={}, tenant_retrievers={}))
        rows = []
        for case in picked:
            row = await one_query(app, client, col, case, args.embed_times,
                                  args.search_times, args.k)
            rows.append(row)
            tag = "翻动过" if case["id"] in FLIPPED else "对照组"
            print(f"{row['id']:<18}{tag:<7} vectors={row['distinct_vectors']}/{row['embed_times']} "
                  f"qdrant_fixed={row['distinct_search_sets_fixed_vector']}/{row['search_times']} "
                  f"service={row['distinct_service_sets']}/{row['search_times']} "
                  f"cutoff_gap={row['cutoff_gap']} gaps={row['adjacent_gaps']}", flush=True)

    emb = [r for r in rows if r["distinct_vectors"] > 1]
    qd = [r for r in rows if r["distinct_search_sets_fixed_vector"] > 1]
    sv = [r for r in rows if r["distinct_service_sets"] > 1]
    print(f"\n分层判定（{len(rows)} 题，其中 {len(FLIPPED)} 题是多批实测里翻过名的）：")
    print(f"  embedding 层：{len(emb)}/{len(rows)} 题在同一文本上返回过不同向量"
          + (f" → {sorted(r['id'] for r in emb)}" if emb else ""))
    print(f"  Qdrant 层（同一向量反复检索）：{len(qd)}/{len(rows)} 题返回过不同邻居"
          + (f" → {sorted(r['id'] for r in qd)}" if qd else ""))
    print(f"  服务路径（含 langchain 包装）：{len(sv)}/{len(rows)} 题的候选集合变过"
          + (f" → {sorted(r['id'] for r in sv)}" if sv else ""))
    if emb and not qd:
        print("  ⇒ 波动来自 embedding 调用本身，不在本仓库的排序/选择层：加确定性 tie-break 治不了它。")
    elif qd:
        print("  ⇒ 同一向量也会翻邻居：近似检索排序不稳定 ⇒ 证据选择层的确定性 tie-break 有靶子。")
    elif not sv:
        print("  ⇒ 三层今天都稳定：端到端那 5.5% 的翻动需要更长采样才复现，别急着下结论。")
    else:
        print("  ⇒ 向量与邻居都稳定，但服务路径的候选集合会变：波动出在 langchain 包装/打分层，"
              "这一层是本仓库可控的。")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                       encoding="utf-8")
        print(f"\n原始数据 → {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="read-only embedding vs search determinism probe")
    parser.add_argument("--embed-times", dest="embed_times", type=int, default=8)
    parser.add_argument("--search-times", dest="search_times", type=int, default=8)
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--gold", default=str(Path(__file__).resolve().parents[2]
                                              / "python_agent" / "evaluation" / "gold_retrieval.jsonl"))
    parser.add_argument("--out", default="")
    asyncio.run(main(parser.parse_args()))
