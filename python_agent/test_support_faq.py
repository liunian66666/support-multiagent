import unittest

from support_faq import lookup, is_handoff, order_id


class SupportFaqContractTest(unittest.TestCase):
    def test_process_answer_has_source_and_no_policy_promise(self):
        answer = lookup("怎么申请退货？", "t_demo")
        self.assertIn("generic-process", answer)
        self.assertIn("process-playbook-v1", answer)
        self.assertIn("核验订单归属、商品情况及商家政策", answer)
        self.assertIsNone(lookup("怎么申请退货？", ""))
        self.assertIsNone(lookup("未拆封七天内能退吗", "t_demo"))

    def test_capabilities_do_not_promise_mutations(self):
        answer = lookup("你可以做什么？", "t_demo")
        self.assertIn("退款和支付操作目前尚未接通", answer)
        self.assertIn("当前系统不自动发起退款", lookup("怎么申请退款？", "t_demo"))

    def test_handoff_is_explicit(self):
        self.assertTrue(is_handoff("我要转人工客服"))
        self.assertFalse(is_handoff("如何申请退款"))

    def test_order_id_only_with_order_context(self):
        self.assertEqual(order_id("查询订单 ORD-2024-001"), "ORD-2024-001")
        self.assertIsNone(order_id("ORD-2024-001"))


if __name__ == "__main__":
    unittest.main()

