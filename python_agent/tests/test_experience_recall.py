"""[ours] S3 经验召回与影子模式契约测试（纯函数 + 运行时开关，无 PG）。"""
import unittest
from unittest.mock import patch

from app.services.experience_recall import augment_message, match_experiences, recall_for_turn
from app.workers.distill import _DISTILL_PROMPT, _digest_events, _parse_digest


def item(exp_id=1, route="faq", content="退货咨询先收集订单号与原因",
         keywords=("退货", "订单"), confidence=0.9):
    return {"id": exp_id, "route": route, "content": content,
            "applicability": {"keywords": list(keywords)},
            "confidence": confidence}


class MatchTest(unittest.TestCase):
    def test_keyword_hit_returns_hit(self):
        hits = match_experiences([item()], "faq", "我要退货，怎么办理？")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["id"], 1)
        self.assertIn("退货", hits[0]["matched"])

    def test_no_keyword_no_hit(self):
        self.assertEqual(match_experiences([item()], "faq", "今天天气好吗"), [])

    def test_route_mismatch_filtered(self):
        # 经验 route=order 与当轮 faq 不相容；route='' 视为通用
        self.assertEqual(match_experiences([item(route="order")], "faq", "退货"), [])
        self.assertEqual(len(match_experiences([item(route="")], "faq", "退货")), 1)

    def test_confidence_ordering_and_cap(self):
        pool = [item(1, confidence=0.4), item(2, confidence=0.9),
                item(3, confidence=0.6)]
        hits = match_experiences(pool, "faq", "退货 订单")
        self.assertEqual([h["id"] for h in hits], [2, 3])  # 上限 2 条
        self.assertGreater(hits[0]["score"], hits[1]["score"])


class AugmentTest(unittest.TestCase):
    def test_empty_hits_passthrough(self):
        self.assertEqual(augment_message("原始问题", []), "原始问题")

    def test_inject_carries_guardrail(self):
        out = augment_message("原始问题", match_experiences([item()], "faq", "退货"))
        self.assertIn("原始问题", out)
        self.assertIn("人工审核", out)
        # B 方案护栏：经验只指导处理方式；KB 已有数值必须照常给出，
        # 不得借经验回避数值（S9 配对评测中 2 题因此类措辞回退）
        self.assertIn("仅指导处理方式", out)
        self.assertIn("必须直接给出", out)
        self.assertIn("以核验结果与店铺资料为准", out)


class RecallSwitchTest(unittest.IsolatedAsyncioTestCase):
    async def _recall(self, mode, pool, recorded):
        from app.services import trace

        def fake_record(*a, **k):
            recorded.append((a, k))

        async def fake_pool(*a, **k):
            return pool

        async def fake_mode(app, tenant_id):
            return mode

        with patch("app.services.experience_recall._mode", fake_mode), \
                patch("app.services.experience_recall"
                      ".list_recallable_experiences", fake_pool), \
                patch.object(trace, "record", fake_record):
            return await recall_for_turn(None, "t_a", "faq", "我要退货")

    async def test_off_returns_empty_without_query(self):
        recorded = []
        hits = await self._recall("off", [item()], recorded)
        self.assertEqual(hits, [])
        self.assertEqual(recorded, [])  # off 连召回池都不查

    async def test_shadow_records_hit_but_returns_empty(self):
        recorded = []
        hits = await self._recall("shadow", [item()], recorded)
        self.assertEqual(hits, [])  # 影子：应答侧拿不到 → prompt 零改动
        stages = [(a, k) for (a, k) in recorded]
        self.assertTrue(any(k.get("mode") == "shadow" for _, k in stages))

    async def test_inject_returns_hits_and_records(self):
        recorded = []
        hits = await self._recall("inject", [item()], recorded)
        self.assertEqual(len(hits), 1)

    async def test_db_failure_fail_open(self):
        from app.services import trace
        async def boom(*a, **k):
            raise RuntimeError("pg down")

        async def fake_mode(app, tenant_id):
            return "inject"

        with patch("app.services.experience_recall._mode", fake_mode), \
                patch("app.services.experience_recall"
                      ".list_recallable_experiences", boom), \
                patch.object(trace, "record"):
            self.assertEqual(
                await recall_for_turn(None, "t_a", "faq", "退货"), [])


class StrategyModeTest(unittest.IsolatedAsyncioTestCase):
    """[ours] S5 三级开关：已发布策略 recall_mode 优先级最高；缺省回落 env。"""

    async def _mode(self, env_mode, strategy):
        from app.services import experience_recall as er

        async def fake_strategy(app, tenant_id):
            if isinstance(strategy, Exception):
                raise strategy
            return strategy

        with patch.object(er, "MODE", env_mode), \
                patch.object(er, "get_active_strategy", fake_strategy):
            return await er._mode(None, "t_a")

    async def test_published_strategy_overrides_env(self):
        got = await self._mode("shadow", {"id": 3, "params": {"recall_mode": "inject"},
                                          "recall_mode": "inject"})
        self.assertEqual(got, "inject")

    async def test_no_strategy_falls_back_to_env(self):
        self.assertEqual(await self._mode("shadow", None), "shadow")

    async def test_bad_strategy_mode_falls_back(self):
        got = await self._mode("shadow", {"id": 3, "params": {"recall_mode": "launch"}})
        self.assertEqual(got, "shadow")

    async def test_strategy_query_failure_fail_open(self):
        self.assertEqual(await self._mode("shadow", RuntimeError("pg down")), "shadow")


class DistillParseTest(unittest.TestCase):
    def test_parse_valid(self):
        text = "客户咨询退货时先收集订单号与原因，再核对已发布政策。\nAPPLICABILITY: 退货, 订单号"
        content, ap = _parse_digest(text)
        self.assertIn("退货", content)
        self.assertEqual(ap["keywords"], ["退货", "订单号"])

    def test_parse_missing_keywords_rejects(self):
        self.assertEqual(_parse_digest("只有正文没有关键词行"), ("", {}))

    def test_parse_overlong_rejects(self):
        self.assertEqual(_parse_digest("\n".join(f"第{i}行" for i in range(10))),
                         ("", {}))

    def test_digest_keeps_events_serializable(self):
        d = _digest_events([{"stage": "evidence", "name": "kb_retrieval",
                             "attributes": {"count": 2, "evidence_ids": ["c1"]}}])
        self.assertIn("kb_retrieval", d)

    def test_prompt_placeholders_match_distill_call(self):
        # 回归：模板占位符必须与 _distill_one 的 .format 关键字一致
        # （历史上模板用 {tenant} 而调用传 tenant_id=，提炼每轮 KeyError→经验恒 0）
        import string
        fields = {f for _, f, _, _ in string.Formatter().parse(_DISTILL_PROMPT) if f}
        self.assertTrue(
            fields <= {"tenant", "score", "digest"},
            f"模板占位符 {fields} 与 _distill_one .format 关键字不一致")
        _DISTILL_PROMPT.format(tenant="t_demo", score=5, digest="x")  # 不抛即通过


if __name__ == "__main__":
    unittest.main()
