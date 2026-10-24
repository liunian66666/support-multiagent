"""Reindexing must preserve old Qdrant points until replacement is verified."""
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.workers.kb_index import _index_one


class Cursor:
    rowcount = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, sql, params=None):
        pass

    async def fetchone(self):
        return ("演示流程", "text.txt")


class Connection:
    def cursor(self):
        return Cursor()


@asynccontextmanager
async def pool(_app):
    yield Connection()


class VectorClient:
    def __init__(self):
        self.deleted = []

    def scroll(self, **_kwargs):
        return [SimpleNamespace(id="00000000-0000-0000-0000-000000000001")], None

    def retrieve(self, *, ids, **_kwargs):
        return [SimpleNamespace(id=point_id) for point_id in ids]

    def delete(self, **kwargs):
        self.deleted.append(kwargs)


class SafeReindexTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = VectorClient()
        self.app = SimpleNamespace(state=SimpleNamespace(
            qdrant_client=self.client,
            text_splitter=SimpleNamespace(create_documents=lambda _: [
                SimpleNamespace(page_content="新的咨询流程")]),
            tenant_retrievers={},
        ))

    async def test_embedding_failure_does_not_delete_old_points(self):
        store = SimpleNamespace(aadd_texts=AsyncMock(side_effect=RuntimeError("embedding down")))
        with patch("app.workers.kb_index._pool_conn", pool),              patch("app.workers.kb_index.read_kb_file_text", return_value="新的咨询流程"),              patch("app.workers.kb_index.ensure_tenant_vector", new_callable=AsyncMock,
                   return_value=store),              patch("app.workers.kb_index._mark_failed", new_callable=AsyncMock) as failed:
            await _index_one(self.app, "d1", "t_demo")
        self.assertEqual(self.client.deleted, [])
        failed.assert_awaited_once()

    async def test_success_deletes_only_obsolete_points_after_verification(self):
        store = SimpleNamespace(aadd_texts=AsyncMock())
        with patch("app.workers.kb_index._pool_conn", pool),              patch("app.workers.kb_index.read_kb_file_text", return_value="新的咨询流程"),              patch("app.workers.kb_index.ensure_tenant_vector", new_callable=AsyncMock,
                   return_value=store):
            await _index_one(self.app, "d1", "t_demo")
        store.aadd_texts.assert_awaited_once()
        self.assertEqual(len(self.client.deleted), 1)
        self.assertTrue(self.client.deleted[0]["wait"])


if __name__ == "__main__":
    unittest.main()
