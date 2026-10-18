import unittest

from support_faq import lookup, is_handoff, order_id, handoff_reason


class SupportFaqContractTest(unittest.TestCase):
    def test_process_answer_has_source_and_no_policy_promise(self):
        answer = lookup("怎么申请退货？", "t_demo")
        self.assertIn("generic-process", answer)
        self.assertIn("process-playbook-v1", answer)
        self.assertIn("核验订单归属、商品情况及商家政策", answer)
        self.assertIsNone(lookup("怎么申请退货？", ""))
        self.assertIsNone(lookup("未拆封七天内能退吗", "t_demo"))

    def test_paraphrases_hit_deterministic_process_answers(self):
        for query in (
            "我要退货", "退货怎么申请", "退货流程是什么", "我想退货怎么办",
            "怎么退款", "申请退款", "退款如何操作",
            "帮我查一下我的快递", "订单到哪了", "怎么查询订单状态",
            "你都会什么", "你能帮我什么忙",
        ):
            self.assertTrue(lookup(query, "t_demo"), query)

    def test_policy_questions_fall_through_to_tenant_kb(self):
        for query in (
            "退款多久到账", "满多少包邮", "退货运费谁出",
            "支持货到付款吗", "发货时效是什么",
        ):
            self.assertIsNone(lookup(query, "t_demo"), query)

    def test_capabilities_do_not_promise_mutations(self):
        answer = lookup("你可以做什么？", "t_demo")
        self.assertIn("退款和支付操作目前尚未接通", answer)
        self.assertIn("不能代为执行", answer)
        self.assertIn("当前系统不自动发起退款", lookup("怎么申请退款？", "t_demo"))

    def test_handoff_variants(self):
        for query in ("转人工", "找人工", "人工客服", "我要投诉", "请转接人工客服", "转接", "我要转人工客服"):
            self.assertTrue(is_handoff(query), query)

    def test_handoff_no_false_positive(self):
        for query in ("如何申请退款", "人工服务态度怎么样", "客服在吗", "怎么申请退货"):
            self.assertFalse(is_handoff(query), query)

    def test_handoff_reason_distinguishes_complaint(self):
        self.assertEqual(handoff_reason("我要投诉"), "用户投诉，转人工处理")
        self.assertEqual(handoff_reason("转人工"), "用户主动请求转人工")

    def test_order_id_only_with_order_context(self):
        self.assertEqual(order_id("查询订单 ORD-2024-001"), "ORD-2024-001")
        self.assertEqual(order_id("快递 ORD-2024-001 到哪了"), "ORD-2024-001")
        self.assertIsNone(order_id("ORD-2024-001"))
        self.assertIsNone(order_id("帮我查一下我的快递"))


if __name__ == "__main__":
    unittest.main()
