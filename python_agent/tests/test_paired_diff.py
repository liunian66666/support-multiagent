"""The release gate must not treat a quoted demo number as an answered policy."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluation import paired_diff


class PairedDiffTest(unittest.TestCase):
    def _run(self, baseline: str, candidate: str):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gold = root / "gold.jsonl"
            base = root / "base.jsonl"
            cand = root / "cand.jsonl"
            report = root / "report.json"
            gold.write_text(json.dumps({"id": "fee", "required": ["99 元包邮"]}) + "\n", encoding="utf-8")
            base.write_text(json.dumps({"id": "fee", "answer": baseline}, ensure_ascii=False) + "\n", encoding="utf-8")
            cand.write_text(json.dumps({"id": "fee", "answer": candidate}, ensure_ascii=False) + "\n", encoding="utf-8")
            args = ["paired_diff", str(gold), "--base", str(base), "--cand", str(cand), "--output", str(report)]
            with patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    paired_diff.main()
            return raised.exception.code, json.loads(report.read_text(encoding="utf-8"))

    def test_quoted_number_with_refusal_requires_review(self):
        code, report = self._run(
            "以下为演示环境中的示例答复；涉及的店铺政策不代表真实商家承诺。"
            "抱歉，我无法确认准确门槛，比如满 99 元包邮只是示例，不能作为承诺。",
            "目前无法确认包邮门槛。",
        )
        self.assertEqual(code, 3)
        self.assertEqual(report["blockers"], [])
        self.assertEqual([r["id"] for r in report["manual_review"]], ["fee"])

    def test_actual_numeric_answer_lost_is_blocked(self):
        code, report = self._run(
            "演示店铺示例政策：单笔实付满 99 元包邮，仅供测试。",
            "无法为您确认包邮门槛。",
        )
        self.assertEqual(code, 2)
        self.assertEqual([r["id"] for r in report["blockers"]], ["fee"])
        self.assertEqual(report["manual_review"], [])


if __name__ == "__main__":
    unittest.main()
