"""Lock the labeled deterministic routing improvements into CI."""

import json
import unittest
from pathlib import Path

from evaluation.collect_rules import collect
from evaluation.score import summarize


class GoldRulesTest(unittest.TestCase):
    def test_labeled_routes_and_order_ids(self):
        gold = Path(__file__).resolve().parents[1] / "evaluation" / "gold_rules.jsonl"
        cases = [json.loads(line) for line in gold.read_text(encoding="utf-8").splitlines()
                 if line.strip()]
        report = summarize([{**case, **collect(case)} for case in cases])
        self.assertEqual(report["metrics"]["route_ok"], {"value": 1.0, "n": 11})
        self.assertEqual(report["metrics"]["order_id_ok"], {"value": 1.0, "n": 11})
        self.assertEqual(report["metrics"]["action_ok"], {"value": 1.0, "n": 11})


if __name__ == "__main__":
    unittest.main()
