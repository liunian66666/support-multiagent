import unittest
from datetime import datetime, timedelta, timezone

from langchain_core.documents import Document

from app.kb.evidence import select_evidence, stable_chunk_id

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def item(text="支持货到付款。", scope="all"):
    return Document(page_content=text, metadata={"doc_id": "d1", "tenant_id": "t_demo",
                                                 "product_scope": scope})


def rows(state="published", scope="all", status=1, start=None, end=None):
    return {"d1": (status, state, scope, start, end)}


class EvidenceGateTest(unittest.TestCase):
    def test_only_published_indexed_valid_docs_can_answer(self):
        doc = item()
        self.assertEqual(select_evidence([doc], "支持货到付款吗", rows(), NOW), [doc])
        for state in ("unreviewed", "retired"):
            self.assertEqual(select_evidence([doc], "支持货到付款吗", rows(state), NOW), [])
        self.assertEqual(select_evidence([doc], "支持货到付款吗", rows(status=2), NOW), [])
        self.assertEqual(select_evidence([doc], "支持货到付款吗",
                                         rows(start=NOW + timedelta(days=1)), NOW), [])
        self.assertEqual(select_evidence([doc], "支持货到付款吗",
                                         rows(end=NOW), NOW), [])

    def test_demo_and_product_scope(self):
        doc = item("课程购买后可申请退款。")
        self.assertEqual(select_evidence([doc], "课程如何退款", rows("demo", "digital"), NOW), [doc])
        self.assertEqual(select_evidence([doc], "课程如何退款", rows("demo", "physical"), NOW), [])
        self.assertEqual(select_evidence([doc], "课程如何退款", rows("demo", "unknown"), NOW), [])

    def test_mixed_document_does_not_apply_physical_clause_to_course(self):
        doc = item("退货需保持吊牌完整且不影响二次销售。")
        self.assertEqual(select_evidence([doc], "数字课程怎么退货", rows("demo", "all"), NOW), [])
        self.assertEqual(select_evidence([doc], "衣服怎么退货", rows("demo", "all"), NOW), [doc])

    def test_similar_but_unsupported_specifics_are_rejected(self):
        doc = item("支持余额与银行卡支付，退款原路返回。")
        for question in ("支持货到付款吗", "支持花呗分期吗", "退款能退比特币钱包吗",
                         "退货地址和收件人电话是什么", "课程能试听吗",
                         "订单ORD-2024-003发货了吗", "明天航班几点"):
            with self.subTest(question=question):
                self.assertEqual(select_evidence([doc], question, rows("demo"), NOW), [])

    def test_stale_claim_and_chunk_id(self):
        stale = item("当前主项目尚未接通真实订单业务接口。")
        self.assertEqual(select_evidence([stale], "订单如何查询", rows("demo"), NOW), [])
        first = stable_chunk_id("t_demo", "d1", 0, "hello")
        self.assertEqual(first, stable_chunk_id("t_demo", "d1", 0, "hello"))
        self.assertNotEqual(first, stable_chunk_id("t_demo", "d1", 1, "hello"))


if __name__ == "__main__":
    unittest.main()
