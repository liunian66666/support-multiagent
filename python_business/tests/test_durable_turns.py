"""MySQL contract checks; run in the business container with CHAT_TURN_DB_TEST=1."""

import os
import uuid
import unittest
from unittest.mock import patch

from business.chat import recovery, turns, worker
from business.customer.routes import Customer
from business.infra.storage import db


@unittest.skipUnless(os.getenv("CHAT_TURN_DB_TEST") == "1", "needs isolated MySQL fixture")
class DurableTurnDatabaseTest(unittest.TestCase):
    def setUp(self):
        suffix = uuid.uuid4().hex[:12]
        self.thread = "t_demo:turn_test:" + suffix
        self.session = "turn_test_" + suffix
        self.customer = Customer("t_demo", "t_demo:turn_test", self.thread,
                                 self.session, False, False)
        with db() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO customer_sessions (tenant_id, customer_id, thread_id, token, "
                "expires_at) VALUES (%s, %s, %s, %s, DATE_ADD(NOW(), INTERVAL 1 HOUR))",
                (self.customer.tenant_id, self.customer.customer_id,
                 self.thread, self.session),
            )

    def tearDown(self):
        with db() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM chat_turns WHERE thread_id = %s", (self.thread,))
            cur.execute("DELETE FROM messages WHERE thread_id = %s", (self.thread,))
            cur.execute("DELETE FROM customer_sessions WHERE thread_id = %s", (self.thread,))

    def test_atomic_accept_duplicate_order_and_recovery(self):
        first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
        first, created = turns.accept_turn(self.customer, "第一题", "text", first_id)
        self.assertTrue(created)
        duplicate, created = turns.accept_turn(self.customer, "第一题", "text", first_id)
        self.assertFalse(created)
        self.assertEqual(first["user_message_id"], duplicate["user_message_id"])
        with self.assertRaises(turns.TurnConflict):
            turns.accept_turn(self.customer, "改过的问题", "text", first_id)
        second, _ = turns.accept_turn(self.customer, "第二题", "text", second_id)
        stranger = Customer("t_demo", "t_demo:other", self.thread, "other", False, False)
        self.assertIsNone(turns.owned_turn(stranger, first_id))
        self.assertEqual(turns.list_turns(stranger), [])

        claimed = turns.claim_next()
        self.assertEqual(claimed["turn_id"], first_id)
        self.assertIsNone(turns.claim_next())  # same-thread next turn cannot overtake
        with db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE chat_turns SET lease_until = DATE_SUB(NOW(), INTERVAL 1 SECOND) "
                        "WHERE id = %s", (claimed["id"],))
        reclaimed = turns.claim_next()
        self.assertEqual(reclaimed["turn_id"], first_id)
        self.assertNotEqual(reclaimed["lease_token"], claimed["lease_token"])
        self.assertFalse(turns.complete_turn(claimed, answer="stale"))
        self.assertTrue(turns.complete_turn(reclaimed, answer="第一答"))
        self.assertFalse(turns.complete_turn(reclaimed, answer="duplicate"))
        next_claim = turns.claim_next()
        self.assertEqual(next_claim["turn_id"], second_id)
        self.assertTrue(turns.complete_turn(next_claim, answer="第二答"))
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT role, content, answer_id FROM messages WHERE thread_id = %s "
                        "ORDER BY id", (self.thread,))
            rows = cur.fetchall()
        self.assertEqual([r["content"] for r in rows], ["第一题", "第二题", "第一答", "第二答"])
        self.assertEqual([r["answer_id"] for r in rows if r["role"] == "ai"],
                         [first_id, second_id])

    def test_failure_and_customer_retry(self):
        turn_id = str(uuid.uuid4())
        turns.accept_turn(self.customer, "会失败的问题", "text", turn_id)
        for attempt in range(1, turns.MAX_ATTEMPTS + 1):
            row = turns.claim_next()
            self.assertEqual(row["attempt_count"], attempt)
            expected = "failed" if attempt == turns.MAX_ATTEMPTS else "queued"
            self.assertEqual(turns.record_failure(row, "agent_unavailable", "失败"), expected)
            if attempt < turns.MAX_ATTEMPTS:
                with db() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE chat_turns SET next_attempt_at = NOW() WHERE id = %s",
                                (row["id"],))
        self.assertEqual(turns.list_turns(self.customer)[0]["status"], "failed")
        self.assertEqual(turns.retry_turn(self.customer, turn_id)["status"], "queued")
        claimed = turns.claim_next()
        self.assertEqual(claimed["attempt_count"], 1)
        self.assertTrue(turns.complete_turn(claimed, answer="恢复了"))

    def test_heartbeat_extends_the_lease_and_only_for_the_holder(self):
        turn_id = str(uuid.uuid4())
        turns.accept_turn(self.customer, "会跑很久的问题", "text", turn_id)
        row = turns.claim_next()

        # 先把租约伪造成"已过期"，再续期：比较全在数据库侧做，避免测试机与库的时区差异
        with db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE chat_turns SET lease_until = DATE_SUB(NOW(3), "
                        "INTERVAL 1 SECOND) WHERE id = %s", (row["id"],))

        self.assertTrue(turns.heartbeat(row), "持租约的 Worker 应该能续期")
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT lease_until > NOW(3) AS future, "
                        "lease_until > DATE_ADD(NOW(3), INTERVAL 60 SECOND) AS extended "
                        "FROM chat_turns WHERE id = %s", (row["id"],))
            state = cur.fetchone()
        self.assertEqual(state["future"], 1, "续期后租约必须在将来")
        self.assertEqual(state["extended"], 1, "续期至少要把租约推后一个心跳周期以上")

        stale = dict(row, lease_token="0" * 32)
        self.assertFalse(turns.heartbeat(stale), "换了 token 就不能再续期")

    def test_queue_stats_reports_waiting_and_running(self):
        first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
        turns.accept_turn(self.customer, "第一个问题", "text", first_id)
        turns.accept_turn(self.customer, "第二个问题", "text", second_id)
        claimed = turns.claim_next()
        self.assertEqual(claimed["turn_id"], first_id)

        stats = turns.queue_stats()
        self.assertGreaterEqual(stats["queued"], 1)
        self.assertGreaterEqual(stats["running"], 1)
        self.assertEqual(stats["stale_running"], 0)
        self.assertGreaterEqual(stats["oldest_queued_seconds"], 0)

        self.assertTrue(turns.complete_turn(claimed, answer="答复一"))
        second = turns.claim_next()
        self.assertTrue(turns.complete_turn(second, answer="答复二"))
        self.assertEqual(turns.queue_stats()["queued"], 0)


class WorkerOrderingTest(unittest.TestCase):
    def test_explicit_handoff_only(self):
        for text in ("转人工", "请转接人工客服", "麻烦帮我转人工", "我要找真人客服"):
            self.assertTrue(worker.explicit_handoff(text), text)
        for text in ("怎么转人工？", "人工客服几点上班？", "请不要转人工", "退货政策是什么？"):
            self.assertFalse(worker.explicit_handoff(text), text)

    def test_explicit_handoff_creates_ticket_without_calling_agent(self):
        row = {"id": 1, "turn_id": str(uuid.uuid4()), "tenant_id": "t_demo",
               "customer_id": "t_demo:buyer1", "thread_id": "t_demo:buyer1:session",
               "user_message_id": 4, "user_message": "请转接人工客服", "msg_type": "text",
               "attempt_count": 1, "lease_token": "lease"}
        ticket = {"id": 7, "ticket_no": "TK-12345678", "status": 0,
                  "created_at": "created"}
        events = []
        with patch.object(worker, "handling_ticket", return_value=None), \
             patch.object(worker, "waiting_ticket", return_value=None), \
             patch.object(worker, "extract_from_content", return_value={}), \
             patch.object(worker, "upsert_extracted_profile"), \
             patch.object(worker, "lease_held", return_value=True), \
             patch.object(recovery, "recover_for_ticket",
                          return_value=recovery.ResumeResult(recovery.ResumeOutcome.CLEAR)), \
             patch.object(worker, "ensure_customer_ticket", return_value=(ticket, False)) as create, \
             patch.object(worker, "reviewed_agent_reply") as agent, \
             patch.object(worker, "complete_turn", return_value=True) as commit, \
             patch.object(worker, "iso", return_value="created"), \
             patch.object(worker, "notify", side_effect=lambda _row, event, **_kw: events.append(event)):
            worker.process_claim(row)
        create.assert_called_once()
        agent.assert_not_called()
        self.assertEqual(commit.call_args.kwargs["result_type"], "ticket")
        self.assertEqual([event["type"] for event in events], ["new_ticket", "ticket"])

    def test_only_committed_answer_is_chunked_and_end_repeats_full_text(self):
        row = {"turn_id": str(uuid.uuid4())}
        answer = "已核验的最终回复。" * 12
        events = []
        with patch.object(worker, "notify", side_effect=lambda _row, event: events.append(event)), \
             patch.object(worker.time, "sleep"):
            worker.send_reviewed_answer(row, answer)
        chunks = [event for event in events if event["type"] == "chat_chunk"]
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(event["chunk"] for event in chunks), answer)
        self.assertEqual([event["seq"] for event in chunks], list(range(len(chunks))))
        self.assertEqual(events[-1]["type"], "chat_end")
        self.assertEqual(events[-1]["msg"], answer)

    def test_commit_precedes_notification(self):
        row = {"id": 1, "turn_id": str(uuid.uuid4()), "tenant_id": "t_demo",
               "customer_id": "t_demo:buyer1", "thread_id": "t_demo:buyer1:session",
               "user_message_id": 4, "user_message": "你好", "msg_type": "text",
               "attempt_count": 1, "lease_token": "lease"}
        sequence = []
        def complete(*args, **kwargs):
            sequence.append("commit")
            return True
        def notify(*args, **kwargs):
            sequence.append("notify")
        with patch.object(worker, "handling_ticket", return_value=None), \
             patch.object(worker, "waiting_ticket", return_value=None), \
             patch.object(worker, "extract_from_content", return_value={}), \
             patch.object(worker, "upsert_extracted_profile"), \
             patch.object(worker, "reviewed_agent_reply", return_value="您好"), \
             patch.object(worker, "complete_turn", side_effect=complete), \
             patch.object(worker, "notify", side_effect=notify):
            worker.process_claim(row)
        self.assertEqual(sequence, ["commit", "notify"])

    def test_progress_exposes_only_phase_and_waits_for_reviewed_end(self):
        row = {"turn_id": "turn-1", "thread_id": "t_demo:buyer1:session",
               "tenant_id": "t_demo", "user_message_id": 1, "attempt_count": 1}
        events = []
        class Response:
            status_code = 200
            def read(self):
                return b""
            def raise_for_status(self):
                pass
            def iter_lines(self):
                yield 'data: {"type":"progress","phase":"searching","draft":"秘密草稿"}'
                yield 'data: {"type":"chunk","content":"未经检查的内容"}'
                yield 'data: {"type":"progress","phase":"reviewing"}'
                yield 'data: {"type":"end","content":"已检查的答复"}'
        class Stream:
            def __enter__(self): return Response()
            def __exit__(self, *_args): pass
        with patch.object(worker.httpx, "stream", return_value=Stream()), \
             patch.object(worker, "notify", side_effect=lambda _row, event: events.append(event)):
            answer = worker.reviewed_agent_reply(row, "退款政策？")
        self.assertEqual(answer, "已检查的答复")
        self.assertEqual([event["phase"] for event in events], ["searching", "reviewing"])
        self.assertNotIn("秘密草稿", str(events))
        self.assertNotIn("未经检查的内容", str(events))


if __name__ == "__main__":
    unittest.main()
