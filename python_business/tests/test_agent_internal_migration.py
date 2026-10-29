"""Internal Agent APIs must retain their token and tenant boundaries."""

import hashlib
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main_compat as main
from patch_business import patch_business


class AgentInternalMigrationTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.headers = {"X-Internal-Token": "test-internal-token"}
        self.env = patch.dict(os.environ, {"INTERNAL_API_TOKEN": "test-internal-token"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_quota_requires_internal_token_and_scopes_query(self):
        with patch_business(main, "one", return_value={
            "max_concurrent_sessions": 3, "daily_message_limit": 25,
            "max_kb_docs": 4, "max_kb_size_mb": 20,
        }) as query:
            self.assertEqual(self.client.get(
                "/api/agent/tenant-quota", params={"tenant_id": "t_demo"}
            ).status_code, 403)
            self.assertEqual(self.client.get(
                "/api/agent/tenant-quota", params={"tenant_id": "../other"},
                headers=self.headers,
            ).status_code, 400)
            response = self.client.get(
                "/api/agent/tenant-quota", params={"tenant_id": "t_demo"},
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["data"]["daily_message_limit"], 25)
            self.assertEqual(query.call_count, 1)
            self.assertEqual(query.call_args.args[1], ("t_demo",))

    def test_model_config_matches_go_signature_and_hides_key_without_token(self):
        row = {"model_api_key": "enc-chat", "llm_provider_id": 1,
               "model_provider": "custom", "model_name": "chat-model",
               "model_api_base": "https://chat.example",
               "embedding_api_key": "enc-emb", "embedding_provider_id": 2,
               "embedding_provider": "custom", "embedding_model_name": "emb-model",
               "embedding_api_base": "https://emb.example",
               "chat_input_cache_price": 1, "chat_input_price": 2,
               "chat_output_price": 3, "embedding_price": 4}
        with patch_business(main, "tenant_config_row", return_value=row), \
                patch_business(main, "decrypt_api_key", side_effect=lambda key: key), \
                patch_business(main, "resolve_chat_config", return_value=(
                    "dashscope", "chat-model", "https://chat.example", "chat-key")), \
                patch_business(main, "resolve_emb_config", return_value=(
                    "dashscope", "emb-model", "https://emb.example", "emb-key")):
            self.assertEqual(self.client.get(
                "/api/agent/model-config", params={"tenant_id": "t_demo"}
            ).status_code, 403)
            response = self.client.get(
                "/api/agent/model-config", params={"tenant_id": "t_demo"},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        expected = hashlib.sha256(
            b"dashscope|chat-model|chat-key|https://chat.example|"
            b"dashscope|emb-model|emb-key|https://emb.example"
        ).hexdigest()[:16]
        self.assertEqual(data["config_version"], expected)
        self.assertEqual(data["embedding"]["api_key"], "emb-key")

    def test_summary_save_rejects_cross_tenant_thread(self):
        with patch_business(main, "write") as save:
            response = self.client.post(
                "/api/agent/session-summaries", headers=self.headers,
                json={"tenant_id": "t_demo", "thread_id": "other:buyer:session",
                      "summary": "private"},
            )
            self.assertEqual(response.status_code, 403)
            save.assert_not_called()
            response = self.client.post(
                "/api/agent/session-summaries", headers=self.headers,
                json={"tenant_id": "t_demo", "thread_id": "t_demo:buyer:session",
                      "summary": "safe"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(save.call_args.args[1],
                             ("t_demo", "t_demo:buyer:session", "safe", 0))

    def test_summary_candidates_keep_tenant_in_message_lookup(self):
        with patch_business(main, "many", side_effect=[
            [{"thread_id": "t_demo:buyer:session", "tenant_id": "t_demo",
              "watermark": 0, "old_summary": ""}],
            [{"id": 1, "role": "user", "content": "question"},
             {"id": 2, "role": "ai", "content": "reply"}],
        ]) as query:
            response = self.client.get(
                "/api/agent/session-summaries/pending", headers=self.headers,
                params={"limit": 1},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["data"][0]["messages"][0]["role"],
                             "user")
            self.assertEqual(query.call_args.args[1],
                             ("t_demo:buyer:session", "t_demo", 0))


if __name__ == "__main__":
    unittest.main()
