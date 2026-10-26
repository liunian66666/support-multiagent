# -*- coding: utf-8 -*-
"""[ours] 长对话记忆契约测试：开关、拼接、水位推进与失败不推进。"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.messages import HumanMessage

from app.services import memory
from app.services.memory import inject_enabled, memory_messages, summary_block
from app.workers import summary as summary_worker


def run(coro):
    return asyncio.run(coro)


class _Resp:
    def __init__(self, payload, code=200):
        self._payload = payload

    def json(self):
        return self._payload


def _app(http_client=None, redis_client=None):
    class S:
        pass
    app = S()
    app.state = S()
    app.state.http_client = http_client
    app.state.redis_client = redis_client
    return app


class InjectSwitchTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict("os.environ", {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_on(self):
        import os
        os.environ.pop("MEMORY_INJECT", None)
        self.assertTrue(run(inject_enabled(_app())))

    def test_env_off(self):
        import os
        os.environ["MEMORY_INJECT"] = "off"
        self.assertFalse(run(inject_enabled(_app())))

    def test_redis_overrides_env(self):
        import os
        os.environ["MEMORY_INJECT"] = "on"
        rc = _RedisStub(b"off")
        self.assertFalse(run(inject_enabled(_app(redis_client=rc))))

    def test_redis_bad_value_falls_back_env(self):
        import os
        os.environ["MEMORY_INJECT"] = "on"
        rc = _RedisStub(error=True)
        self.assertTrue(run(inject_enabled(_app(redis_client=rc))))


class TenantSwitchTest(unittest.TestCase):
    """[ours] 三级判定链：Redis off 一票否决 > 租户 memory_enabled > env。"""

    def setUp(self):
        patcher = patch.dict("os.environ", {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        import os
        os.environ.pop("MEMORY_INJECT", None)

    def _quota(self, memory_enabled):
        return patch("app.infra.db.quota.get_tenant_quota",
                     AsyncMock(return_value={"memory_enabled": memory_enabled}))

    def test_tenant_off_blocks(self):
        with self._quota(0):
            self.assertFalse(run(inject_enabled(_app(), "t_demo")))

    def test_tenant_on_allows(self):
        with self._quota(1):
            self.assertTrue(run(inject_enabled(_app(), "t_demo")))

    def test_quota_missing_key_defaults_on(self):
        # 旧接口/旧缓存 dict 无 memory_enabled 键 → 默认开（行为同旧版）
        with patch("app.infra.db.quota.get_tenant_quota", AsyncMock(return_value={})):
            self.assertTrue(run(inject_enabled(_app(), "t_demo")))

    def test_quota_fetch_failure_defaults_on(self):
        with patch("app.infra.db.quota.get_tenant_quota",
                   AsyncMock(side_effect=RuntimeError("biz down"))):
            self.assertTrue(run(inject_enabled(_app(), "t_demo")))

    def test_redis_off_overrides_tenant_on(self):
        # 平台应急闸门优先级最高：租户开着也一票否决
        with self._quota(1):
            self.assertFalse(run(inject_enabled(_app(redis_client=_RedisStub(b"off")), "t_demo")))

    def test_redis_on_does_not_short_circuit_tenant(self):
        # 新语义：Redis 值 "on" 不再短路为真，继续走租户级判定（租户关 → 关）
        with self._quota(0):
            self.assertFalse(run(inject_enabled(_app(redis_client=_RedisStub(b"on")), "t_demo")))

    def test_no_tenant_id_skips_tenant_level(self):
        # 平台内部调用（无租户上下文）不受租户开关影响
        self.assertTrue(run(inject_enabled(_app(), "")))


class _RedisStub:
    def __init__(self, value=None, error=False):
        self.value, self.error = value, error

    async def get(self, key):
        if self.error:
            raise RuntimeError("redis down")
        return self.value


class MemoryMessagesTest(unittest.TestCase):
    def setUp(self):
        memory._cache.clear()

    def test_off_returns_empty(self):
        with patch("app.services.memory.inject_enabled", AsyncMock(return_value=False)):
            out = run(memory_messages(_app(), "t_demo", "t_demo:u1"))
        self.assertEqual(out, [])

    def test_on_with_summary_returns_prefix_block(self):
        memory._cache[("t_demo", "t_demo:u2")] = (9e18, "客户咨询ORD-2024-001退货运费")
        with patch("app.services.memory.inject_enabled", AsyncMock(return_value=True)):
            out = run(memory_messages(_app(), "t_demo", "t_demo:u2"))
        self.assertEqual(len(out), 1)
        # 网关要求 system 只能在首位，记忆段必须用 HumanMessage（冒烟实锤 400）
        self.assertIsInstance(out[0], HumanMessage)
        self.assertIn("ORD-2024-001", out[0].content)
        self.assertIn("更早对话摘要", out[0].content)
        self.assertIn("不得据此承诺", out[0].content)

    def test_on_without_summary_returns_empty(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=_Resp({"code": 0, "data": None}))
        with patch("app.services.memory.inject_enabled", AsyncMock(return_value=True)):
            out = run(memory_messages(_app(client), "t_demo", "t_demo:u3"))
        self.assertEqual(out, [])

    def test_fetch_failure_degrades_to_empty(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=RuntimeError("down"))
        with patch("app.services.memory.inject_enabled", AsyncMock(return_value=True)):
            out = run(memory_messages(_app(client), "t_demo", "t_demo:u4"))
        self.assertEqual(out, [])


class SummaryBlockTest(unittest.TestCase):
    def setUp(self):
        memory._cache.clear()

    def test_success_and_ttl_cache(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=_Resp(
            {"code": 0, "data": {"summary": "摘要X", "watermark": 9, "version": 2}}))
        app = _app(client)
        self.assertEqual(run(summary_block(app, "t_demo", "t_demo:u5")), "摘要X")
        self.assertEqual(run(summary_block(app, "t_demo", "t_demo:u5")), "摘要X")
        self.assertEqual(client.get.await_count, 1)  # TTL 内只拉一次


class WatermarkTest(unittest.TestCase):
    """水位推进：成功推进到本批最大消息 id；生成/保存失败均不推进（不发包）。"""

    CAND = {"thread_id": "t_demo:u9", "tenant_id": "t_demo", "watermark": 100,
            "old_summary": "旧摘要", "messages": [{"id": 101, "role": "user", "content": "退货运费谁出"},
                                                   {"id": 105, "role": "assistant", "content": "质量问题店铺承担"}]}

    def _app_ok(self):
        client = AsyncMock()
        client.post = AsyncMock(return_value=_Resp({"code": 0}))
        return _app(client), client

    def test_success_advances_watermark(self):
        app, client = self._app_ok()
        with patch.object(summary_worker, "_generate_summary", AsyncMock(return_value="新摘要")):
            ok = run(summary_worker._process_one(app, self.CAND))
        self.assertTrue(ok)
        sent = client.post.await_args.kwargs["json"]
        self.assertEqual(sent["last_message_id"], 105)

    def test_old_summary_uses_merge_prompt_path(self):
        app, _ = self._app_ok()
        with patch.object(summary_worker, "_generate_summary", AsyncMock(return_value="新摘要")) as gen:
            run(summary_worker._process_one(app, self.CAND))
        self.assertEqual(gen.await_args.kwargs.get("old_summary"), "旧摘要")

    def test_generation_failure_does_not_advance(self):
        app, client = self._app_ok()
        with patch.object(summary_worker, "_generate_summary", AsyncMock(side_effect=RuntimeError("llm down"))):
            ok = run(summary_worker._process_one(app, self.CAND))
        self.assertFalse(ok)
        client.post.assert_not_awaited()

    def test_save_failure_does_not_advance(self):
        app = _app(AsyncMock())
        app.state.http_client.post = AsyncMock(return_value=_Resp({"code": 500, "msg": "db"}))
        with patch.object(summary_worker, "_generate_summary", AsyncMock(return_value="新摘要")):
            ok = run(summary_worker._process_one(app, self.CAND))
        self.assertFalse(ok)


class PromptFactsTest(unittest.TestCase):
    def test_prompts_require_verbatim_facts(self):
        for text in (summary_worker.SUMMARY_PROMPT, summary_worker.MERGE_PROMPT):
            self.assertIn("订单号", text)
            self.assertIn("逐字符", text)


if __name__ == "__main__":
    unittest.main()
