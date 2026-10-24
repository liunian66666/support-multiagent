import unittest

from langchain_core.documents import Document

from app.agent.tools import format_kb_doc_for_model, usable_kb_docs
from app.chat.reply import demo_faq_prefix, mark_demo_faq_reply


class KbFilterTest(unittest.TestCase):
    def test_superseded_order_claim_is_not_available_to_faq(self):
        stale = Document(page_content="订单查询：当前主项目尚未接通真实订单业务接口。")
        current = Document(page_content="退货需核验订单归属和商品情况。")
        self.assertEqual(usable_kb_docs([stale, current]), [current])


    def test_demo_provenance_is_attached_to_each_chunk(self):
        demo = Document(page_content="暂不支持专票。", metadata={"title": "演示店铺服务政策 v1"})
        merchant = Document(page_content="支持专票。", metadata={"title": "已发布店铺政策"})
        self.assertIn("不代表真实店铺承诺", format_kb_doc_for_model(demo))
        self.assertEqual(format_kb_doc_for_model(merchant), "支持专票。")


    def test_demo_reply_notice_is_fixed_and_other_tenants_untouched(self):
        answer = mark_demo_faq_reply("t_demo", "暂不支持专票。")
        self.assertTrue(answer.startswith(demo_faq_prefix("t_demo")))
        self.assertIn("不代表真实商家承诺", answer)
        self.assertEqual(mark_demo_faq_reply("merchant1", "支持专票。"), "支持专票。")
        self.assertEqual(mark_demo_faq_reply("t_demo", ""), "")
        self.assertEqual(mark_demo_faq_reply("t_demo", answer), answer)


if __name__ == "__main__":
    unittest.main()
