"""Score labeled JSONL runs; unobserved metrics stay unscored.

Each row may contain id, category, gold_docs, retrieved_docs, answer,
required, forbidden, expected_route/actual_route, expected_order_id/
actual_order_id, expected_action/actual_action, and latency_ms.
Document labels are stable source document IDs, not Qdrant point IDs.
"""

import argparse
import json
import math
from pathlib import Path


def _mean(values):
    return round(sum(values) / len(values), 4) if values else None


def _percentile(values, fraction):
    return round(sorted(values)[math.ceil(fraction * len(values)) - 1], 2) if values else None


def score_case(case, k=3):
    row = {"id": case["id"], "category": case.get("category", "uncategorized")}
    if "gold_docs" in case and "retrieved_docs" in case:
        gold = set(case["gold_docs"])
        ranked = list(dict.fromkeys(case["retrieved_docs"]))[:k]
        if gold:
            row["recall_at_k"] = len(gold.intersection(ranked)) / len(gold)
            row["reciprocal_rank"] = next(
                (1 / (i + 1) for i, doc in enumerate(ranked) if doc in gold), 0.0)
        else:
            row["negative_retrieval_ok"] = not ranked
    if "gold_evidence_terms" in case and isinstance(case.get("clause_covered"), bool):
        row["clause_covered"] = case["clause_covered"]
    if "answer" in case:
        answer = case["answer"]
        row["missing_required"] = [x for x in case.get("required", []) if x not in answer]
        row["present_forbidden"] = [x for x in case.get("forbidden", []) if x in answer]
        row["answer_rules_ok"] = not (row["missing_required"] or row["present_forbidden"])
    for field in ("route", "order_id", "action"):
        if f"expected_{field}" in case and f"actual_{field}" in case:
            row[f"{field}_ok"] = case[f"expected_{field}"] == case[f"actual_{field}"]
    if isinstance(case.get("latency_ms"), (int, float)) and case["latency_ms"] >= 0:
        row["latency_ms"] = case["latency_ms"]
    return row


def summarize(cases, k=3):
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case id")
    rows = [score_case(case, k) for case in cases]
    metrics = {}
    for field in ("recall_at_k", "reciprocal_rank", "negative_retrieval_ok",
                  "clause_covered", "answer_rules_ok", "route_ok", "order_id_ok", "action_ok"):
        values = [float(row[field]) for row in rows if field in row]
        metrics[field] = {"value": _mean(values), "n": len(values)}
    latency = [row["latency_ms"] for row in rows if "latency_ms" in row]
    metrics["latency_ms"] = {"p50": _percentile(latency, 0.5),
                             "p95": _percentile(latency, 0.95), "n": len(latency)}
    return {"k": k, "cases": len(rows), "metrics": metrics, "details": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    parser.add_argument("--predictions", type=Path,
                        help="JSONL predictions keyed by id; missing predictions remain unscored")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--k", type=int, default=3)
    args = parser.parse_args()
    if args.k < 1:
        parser.error("--k must be positive")
    cases = [json.loads(line) for line in args.cases.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    if args.predictions:
        predictions = [json.loads(line) for line in args.predictions.read_text(
            encoding="utf-8").splitlines() if line.strip()]
        ids = [row["id"] for row in predictions]
        if len(ids) != len(set(ids)):
            parser.error("duplicate prediction id")
        by_id = {row["id"]: row for row in predictions}
        unknown = set(by_id) - {row["id"] for row in cases}
        if unknown:
            parser.error(f"predictions without gold case: {sorted(unknown)}")
        cases = [{**case, **{key: value for key, value in by_id.get(case["id"], {}).items()
                           if key != "id"}} for case in cases]
    payload = json.dumps(summarize(cases, args.k), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
