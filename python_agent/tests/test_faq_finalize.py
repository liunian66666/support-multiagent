import unittest

from langchain_core.messages import HumanMessage, ToolMessage

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

    # [ours] 长对话记忆：窄豁免只放行“复述摘要段中用户本人说过的编号”
    def _mem(self, text):
        return HumanMessage(content="【更早对话摘要】（背景）\n" + text)

    def test_memory_cited_order_id_bypasses_no_evidence_block(self):
        messages = [self._mem("客户催订单ORD-2024-888发货"),
                    kb_result("未找到相关信息。")]
        answer = finalize_faq_reply(
            "我前面提过的那笔单子，编号是什么来着？",
            "您之前提到的订单号是 ORD-2024-888。", messages)
        self.assertIn("ORD-2024-888", answer)
        self.assertNotIn("暂时无法核实", answer)

    def test_hallucinated_token_not_in_memory_still_blocked(self):
        messages = [self._mem("客户催订单ORD-2024-888发货"),
                    kb_result("未找到相关信息。")]
        answer = finalize_faq_reply(
            "那笔单子编号多少？",
            "您的订单号是 ORD-9999-000。", messages)
        self.assertIn("暂时无法核实", answer)
        self.assertNotIn("ORD-9999-000", answer)

    def test_memory_token_does_not_bypass_policy_payment_branch(self):
        # 支付方式属政策兜底分支，优先级高于记忆豁免，必须仍被拦截
        messages = [self._mem("客户提到订单ORD-2024-888"),
                    kb_result("未找到相关信息。")]
        answer = finalize_faq_reply(
            "订单ORD-2024-888支持货到付款吗？",
            "订单ORD-2024-888肯定支持货到付款。", messages)
        self.assertIn("未明确说明", answer)
        self.assertNotIn("肯定支持", answer)


if __name__ == "__main__":
    unittest.main()
