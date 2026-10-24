import unittest

from langchain_core.messages import ToolMessage

from app.chat.reply import finalize_faq_reply


def kb_result(text):
    return ToolMessage(content=text, name="search_kb", tool_call_id="1")


class FaqFinalizerTest(unittest.TestCase):
    def test_no_evidence_blocks_generated_policy_claim(self):
        messages = [kb_result("未找到相关信息，建议转接人工客服。")]
        answer = finalize_faq_reply("支持货到付款吗？", "肯定支持货到付款。", messages)
        self.assertIn("未明确说明", answer)
        self.assertNotIn("肯定支持", answer)

    def test_missing_tool_call_cannot_support_policy_claim(self):
        answer = finalize_faq_reply("能开专票吗？", "肯定能开。", [])
        self.assertIn("暂时无法核实", answer)

    def test_model_rephrasing_cannot_drop_customer_learning_condition(self):
        messages = [kb_result("- 课程退款需核验订单。")]
        answer = finalize_faq_reply(
            "数字课程已学习一半还能退吗？",
            "根据通用退货政策，通常可以退款。",
            messages,
        )
        self.assertIn("未明确说明", answer)
        self.assertNotIn("通常可以退款", answer)

    def test_disassembly_eligibility_requires_order_facts(self):
        messages = [kb_result("- 退货需保持吊牌完整。")]
        answer = finalize_faq_reply("未拆封能退吗？", "通常都可以退。", messages)
        self.assertIn("仅凭是否拆封还不能判断", answer)
        self.assertNotIn("通常都可以退", answer)

    def test_grounded_answer_is_preserved_in_customer_language(self):
        messages = [kb_result("- 满 99 元包邮。")]
        answer = finalize_faq_reply("满多少包邮？", "检索到知识库说满 99 元包邮。", messages)
        self.assertIn("99 元", answer)
        self.assertNotIn("知识库", answer)
        self.assertNotIn("检索", answer)


if __name__ == "__main__":
    unittest.main()
