# [ours] 本文件为 fork 新增：support_faq 规则契约测试
import unittest

from app.chat.faq import lookup, is_handoff, order_id, handoff_reason


class SupportFaqContractTest(unittest.TestCase):
    def test_process_answer_clean_no_internal_source_tag(self):
        answer = lookup("怎么申请退货？", "t_demo")
        self.assertIn("核验订单归属、商品情况及商家政策", answer)
        # 不应向终端用户暴露内部工程标识与架构术语
        self.assertNotIn("support-agent", answer)
        self.assertNotIn("process-playbook", answer)
        self.assertNotIn("generic-process", answer)
        self.assertNotIn("租户知识库", answer)
        self.assertNotIn("来源", answer)
        self.assertIsNone(lookup("怎么申请退货？", ""))
        self.assertIsNone(lookup("未拆封七天内能退吗", "t_demo"))

    def test_paraphrases_hit_deterministic_process_answers(self):
        for query in (
            "我要退货", "退货怎么申请", "退货流程是什么", "我想退货怎么办",
            "怎么退款", "申请退款", "退款如何操作",
            "帮我查一下我的快递", "订单到哪了", "怎么查询订单状态",
            "你都会什么", "你能帮我什么忙",
            "订单号怎么找", "单号在哪看", "订单编号咋查",
        ):
            self.assertTrue(lookup(query, "t_demo"), query)

    def test_policy_questions_fall_through_to_tenant_kb(self):
        for query in (
            "退款多久到账", "满多少包邮", "退货运费谁出",
            "支持货到付款吗", "发货时效是什么",
            "退款政策怎么规定", "如何查询退款规则", "帮我查询退款政策",
            # [ours] 留出集ho-t08实锤：问时限被return_apply流程话术抢答，
            # 「7天内发起」的KB数值答不出
            "签收之后最晚几天内能申请退货？", "退货审核几天能批？",
        ):
            self.assertIsNone(lookup(query, "t_demo"), query)

    def test_return_eligibility_with_different_conditions_uses_policy_specialist(self):
        from app.chat.intent import route_message

        for query in ("未拆封能退吗？", "已经拆封能退吗？"):
            self.assertEqual(route_message(query, "t_demo").specialist, "faq")

    def test_payment_method_questions_use_faq_but_payment_actions_do_not(self):
        from app.chat.intent import route_message

        for query in ("支持货到付款吗？", "买课程支持花呗分期吗？", "有哪些支付方式？"):
            self.assertEqual(route_message(query, "t_demo").specialist, "faq", query)
        for query in ("帮我付款", "给我支付这单"):
            self.assertEqual(route_message(query, "t_demo").specialist, "handoff", query)

    def test_capabilities_do_not_promise_mutations(self):
        answer = lookup("你可以做什么？", "t_demo")
        self.assertIn("订单状态查询已接通", answer)
        self.assertIn("退款和支付操作尚未接通", answer)
        self.assertIn("不能代为执行", answer)
        self.assertIn("当前系统不自动发起退款", lookup("怎么申请退款？", "t_demo"))

    def test_order_status_answer_asks_for_id_without_false_claim(self):
        answer = lookup("怎么查询订单状态", "t_demo")
        self.assertIn("订单号", answer)
        # 订单查询已接通，不能再声称「未接通真实订单系统」
        self.assertNotIn("未接通", answer)

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
        # 演示格式
        self.assertEqual(order_id("查询订单 ORD-2024-001"), "ORD-2024-001")
        self.assertEqual(order_id("ORD-2024-003订单的状态，以及怎么退货？"), "ORD-2024-003")
        self.assertEqual(order_id("查TB202410180001物流"), "TB202410180001")
        self.assertEqual(order_id("快递 ORD-2024-001 到哪了"), "ORD-2024-001")
        # 电商前缀
        self.assertEqual(order_id("帮我查 TB202410180001"), "TB202410180001")
        self.assertEqual(order_id("订单 JD20241018001 状态"), "JD20241018001")
        self.assertEqual(order_id("查 PDD-12345678 物流"), "PDD-12345678")
        # 显式上下文（无前缀纯数字/字母数字组合）
        self.assertEqual(order_id("订单号是 ABC-123-XYZ"), "ABC-123-XYZ")
        self.assertEqual(order_id("我的单号: 202410181234"), "202410181234")
        self.assertEqual(order_id("订单编号 1234567890"), "1234567890")
        # [ours] 「订单」直接接单号（用户习惯说法，不带「号」字）
        self.assertEqual(order_id("查询订单10001"), "10001")
        self.assertEqual(order_id("订单 10001 到哪了"), "10001")
        self.assertEqual(order_id("查订单：TB202410180001"), "TB202410180001")
        # 年份不是订单号
        self.assertIsNone(order_id("订单2024年还发货吗"))
        # 无上下文或无格式
        # [ours] 语义修订（留出集off两轮实锤）：裸演示单号 ORD-数字-数字 特征足够强，
        # 「ORD-2024-001现在到哪一步了？」这类完整问句此前漏提取→落general被瞎答。
        # 弱形态（单段 ORD-2024、尾横线）与手机号仍不匹配，见下行负例。
        self.assertEqual(order_id("ORD-2024-001"), "ORD-2024-001")
        self.assertIsNone(order_id("ORD-2024"))
        self.assertIsNone(order_id("查询订单 ORD-2024-"))
        self.assertIsNone(order_id("帮我查一下我的快递"))
        self.assertIsNone(order_id("我等了 30 分钟还没发货"))  # 纯短数字不误匹配
        self.assertIsNone(order_id("13800138000"))  # 手机号不误匹配

    def test_find_order_id_answer_is_structured_and_honest(self):
        answer = lookup("订单号怎么找", "t_demo")
        self.assertIn("购买记录", answer)
        self.assertIn("收货短信", answer)
        self.assertIn("电子发票", answer)
        self.assertIn("支付凭证", answer)
        self.assertIn("核验订单归属", answer)  # 不承诺一定查到，只承诺核验动作


if __name__ == "__main__":
    unittest.main()
