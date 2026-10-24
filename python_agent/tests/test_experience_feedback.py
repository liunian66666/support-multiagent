"""[ours] S2 经验层：/agent/experience/feedback 内部接口契约测试。"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api.experience import ingest_feedback, review_experience


def make_request(events, tenant="t_shop", token="tok"):
    return SimpleNamespace(
        headers={"X-Internal-Token": token, "X-Tenant-Id": tenant},
        app=SimpleNamespace(),
        json=AsyncMock(return_value={"events": events}),
    )


def event(event_id="outbox:1", tenant="t_shop"):
    return {"event_id": event_id, "tenant_id": tenant, "thread_id": "th",
            "answer_id": "a123", "result_type": "rating_submitted",
            "value": {"score": 5}, "source": "feedback_outbox",
            "occurred_ts": 1764000000.0}


TOKEN = "tok"


class ExperienceFeedbackApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"INTERNAL_API_TOKEN": TOKEN})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_missing_or_wrong_token_rejected(self):
        for token in ("", "wrong"):
            with self.subTest(token=token):
                with self.assertRaises(HTTPException) as ctx:
                    await ingest_feedback(make_request([event()], token=token))
                self.assertEqual(ctx.exception.status_code, 403)

    async def test_cross_tenant_event_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await ingest_feedback(make_request([event(tenant="t_other")]))
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_empty_or_oversized_batch_rejected(self):
        for events in ([], "not-a-list", [event(f"outbox:{i}") for i in range(201)]):
            with self.subTest(n=len(events) if isinstance(events, list) else "str"):
                with self.assertRaises(HTTPException) as ctx:
                    await ingest_feedback(make_request(events))
                self.assertEqual(ctx.exception.status_code, 400)

    async def test_event_requires_id_and_result_type(self):
        with self.assertRaises(HTTPException) as ctx:
            await ingest_feedback(make_request([{"tenant_id": "t_shop"}]))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_valid_batch_reports_received_and_stored(self):
        # 第二条模拟幂等冲突（save_feedback_event 返回 False）
        saves = [True, False]

        async def fake_save(app, ev):
            return saves.pop(0)

        with patch("app.api.experience.save_feedback_event", fake_save):
            result = await ingest_feedback(
                make_request([event("outbox:1"), event("outbox:2")]))
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["data"], {"received": 2, "stored": 1})


class ExperienceReviewApiTest(unittest.IsolatedAsyncioTestCase):
    """[ours] S3 审核接口：publish/suspend 契约与租户隔离行为。"""

    def setUp(self):
        patcher = patch.dict(os.environ, {"INTERNAL_API_TOKEN": TOKEN})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _req(self, body, tenant="t_shop", token="tok"):
        return SimpleNamespace(
            headers={"X-Internal-Token": token, "X-Tenant-Id": tenant},
            app=SimpleNamespace(),
            json=AsyncMock(return_value=body),
        )

    async def test_requires_token(self):
        with self.assertRaises(HTTPException) as ctx:
            await review_experience(self._req({"experience_id": 1, "action": "publish"},
                                              token="wrong"))
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_bad_action_or_id_rejected(self):
        for body in ({"experience_id": 1, "action": "delete"},
                     {"experience_id": "1", "action": "publish"},
                     {"action": "publish"}):
            with self.subTest(body=body):
                with self.assertRaises(HTTPException) as ctx:
                    await review_experience(self._req(body))
                self.assertEqual(ctx.exception.status_code, 400)

    async def test_cross_tenant_id_404(self):
        async def fake_set(app, tenant_id, exp_id, action):
            return False  # SQL 层 tenant_id 条件不命中
        with patch("app.api.experience.set_experience_status", fake_set):
            with self.assertRaises(HTTPException) as ctx:
                await review_experience(self._req(
                    {"experience_id": 99, "action": "publish"}))
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_publish_success(self):
        calls = []

        async def fake_set(app, tenant_id, exp_id, action):
            calls.append((tenant_id, exp_id, action))
            return True
        with patch("app.api.experience.set_experience_status", fake_set):
            result = await review_experience(self._req(
                {"experience_id": 7, "action": "publish"}))
        self.assertEqual(result["data"], {"experience_id": 7,
                                          "review_status": "publish"})
        self.assertEqual(calls, [("t_shop", 7, "publish")])


if __name__ == "__main__":
    unittest.main()
