import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluation.score import score_case, summarize


class EvaluationTest(unittest.TestCase):
    def test_retrieval_deduplicates_source_documents(self):
        row = score_case({"id": "x", "gold_docs": ["policy", "process"],
                          "retrieved_docs": ["policy", "policy", "other", "process"]})
        self.assertEqual(row["recall_at_k"], 1.0)
        self.assertEqual(row["reciprocal_rank"], 1.0)

    def test_missing_labels_are_not_counted_as_success(self):
        metrics = summarize([{"id": "negative", "gold_docs": [], "retrieved_docs": []},
                             {"id": "unlabeled"}])["metrics"]
        self.assertEqual(metrics["negative_retrieval_ok"], {"value": 1.0, "n": 1})
        self.assertEqual(metrics["recall_at_k"], {"value": None, "n": 0})

    def test_clause_metric_is_scored_only_for_labeled_predictions(self):
        metrics = summarize([
            {"id": "a", "gold_evidence_terms": ["99"], "clause_covered": True},
            {"id": "b", "gold_evidence_terms": ["地址"]},
            {"id": "c", "clause_covered": False},
        ])["metrics"]
        self.assertEqual(metrics["clause_covered"], {"value": 1.0, "n": 1})

    def test_answer_and_identity_rules(self):
        row = score_case({"id": "x", "answer": "订单待发货，退货需核验。",
                          "required": ["待发货", "需核验"], "forbidden": ["知识来源"],
                          "expected_order_id": "ORD-2024-003", "actual_order_id": "ORD-2024-"})
        self.assertTrue(row["answer_rules_ok"])
        self.assertFalse(row["order_id_ok"])

    def test_live_cases_use_independent_guest_sessions(self):
        from evaluation.collect_answers_live import main

        with tempfile.TemporaryDirectory() as directory:
            gold = Path(directory) / "gold.jsonl"
            output = Path(directory) / "out.jsonl"
            gold.write_text(
                json.dumps({"id": "a", "query": "问题一", "customer": "guest"}) + "\n"
                + json.dumps({"id": "b", "query": "问题二", "customer": "guest"}) + "\n",
                encoding="utf-8")
            tokens = []

            def fake_post(path, payload, token=""):
                if path == "/api/auth/guest-token":
                    return 200, {"code": 0, "data": {"token": f"guest-{len(tokens) + 1}"}}
                tokens.append(token)
                return 200, {"code": 0, "msg": "回答"}

            with patch("evaluation.collect_answers_live.post", side_effect=fake_post), \
                    patch("sys.argv", ["collector", str(gold), "--output", str(output)]):
                main()
            self.assertEqual(tokens, ["guest-1", "guest-2"])
            self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 2)

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError):
            summarize([{"id": "x"}, {"id": "x"}])

    def test_latency_percentiles(self):
        metrics = summarize([{"id": "a", "latency_ms": 10},
                             {"id": "b", "latency_ms": 20},
                             {"id": "c", "latency_ms": 30}])["metrics"]["latency_ms"]
        self.assertEqual(metrics, {"p50": 20, "p95": 30, "n": 3})


if __name__ == "__main__":
    unittest.main()
