"""[ours] S2：feedback_outbox → agent 经验层同步的契约测试（免网络/免库）。"""
import json
import unittest
from unittest.mock import patch

from business.infra import outbox


def row(i=1, tenant="t_demo", payload=None):
    return {"id": i, "event_type": "rating_submitted", "tenant_id": tenant,
            "thread_id": "t_demo:buyer1:session", "answer_id": f"ans{i}",
            "payload": json.dumps(payload or {"score": 5}) if payload is not None
            else '{"score": 5}',
            "occurred_ts": 1764000000.0}


class OutboxBuildTest(unittest.TestCase):
    def test_event_shape_and_idempotency_key(self):
        ev = outbox.build_events([row(7)])[0]
        self.assertEqual(ev["event_id"], "outbox:7")
        self.assertEqual(ev["tenant_id"], "t_demo")
        self.assertEqual(ev["answer_id"], "ans7")
        self.assertEqual(ev["result_type"], "rating_submitted")
        self.assertEqual(ev["value"], {"score": 5})
        self.assertEqual(ev["occurred_ts"], 1764000000.0)

    def test_bad_payload_degrades_to_empty_value(self):
        bad = row(1)
        bad["payload"] = "{not json"
        self.assertEqual(outbox.build_events([bad])[0]["value"], {})

    def test_null_occurred_ts_passes_none(self):
        r = row(1)
        r["occurred_ts"] = None
        self.assertIsNone(outbox.build_events([r])[0]["occurred_ts"])


class OutboxSyncOnceTest(unittest.TestCase):
    def test_sync_marks_published_only_after_push(self):
        calls = {}

        class Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"code": 0, "data": {"received": 2, "stored": 2}}

        def fake_post(url, json=None, headers=None, timeout=None):
            calls["url"] = url
            calls["events"] = json["events"]
            calls["tenant"] = headers.get("X-Tenant-Id")
            return Resp()

        with patch.object(outbox, "many", side_effect=lambda *a, **k: [row(1), row(2)]), \
                patch.object(outbox, "write") as fake_write, \
                patch.object(outbox.httpx, "post", fake_post), \
                patch.dict("os.environ", {"INTERNAL_API_TOKEN": "tok"}):
            fetched, pushed = outbox.sync_once()

        self.assertEqual((fetched, pushed), (2, 2))
        self.assertTrue(calls["url"].endswith("/agent/experience/feedback"))
        self.assertEqual(calls["tenant"], "t_demo")
        fake_write.assert_called_once()
        sql, args = fake_write.call_args[0]
        self.assertIn("SET status = 1", sql)
        self.assertEqual(args, (1, 2))

    def test_push_failure_leaves_rows_unpublished(self):
        def boom(*a, **k):
            raise outbox.httpx.ConnectError("agent down")

        writes = []
        with patch.object(outbox, "many", side_effect=lambda *a, **k: [row(1)]), \
                patch.object(outbox, "write", side_effect=writes.append), \
                patch.object(outbox.httpx, "post", boom), \
                patch.dict("os.environ", {"INTERNAL_API_TOKEN": "tok"}):
            with self.assertRaises(outbox.httpx.ConnectError):
                outbox.sync_once()
        self.assertEqual(writes, [])  # 失败不标已发布 → 下轮重试

    def test_empty_pending_short_circuits(self):
        with patch.object(outbox, "many", side_effect=lambda *a, **k: []):
            self.assertEqual(outbox.sync_once(), (0, 0))

    def test_batch_groups_by_tenant(self):
        tenants = []

        class Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"code": 0, "data": {}}

        def fake_post(url, json=None, headers=None, timeout=None):
            tenants.append(headers.get("X-Tenant-Id"))
            return Resp()

        rows = [row(1, "t_a"), row(2, "t_b"), row(3, "t_a")]
        with patch.object(outbox, "many", side_effect=lambda *a, **k: rows), \
                patch.object(outbox, "write"), \
                patch.object(outbox.httpx, "post", fake_post), \
                patch.dict("os.environ", {"INTERNAL_API_TOKEN": "tok"}):
            outbox.sync_once()
        # 按租户分组：t_a 一批（2 条）、t_b 一批（1 条），共两次推送
        self.assertEqual(sorted(tenants), ["t_a", "t_b"])


if __name__ == "__main__":
    unittest.main()
