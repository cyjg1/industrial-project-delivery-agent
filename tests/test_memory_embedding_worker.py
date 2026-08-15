import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from agent.schemas import CandidateStatus, EvidenceRef, InspectionItem
from store.sqlite_store import ProjectSQLiteStore


def _item(item_id: str, title: str, description: str = "项目事实需要保留证据。") -> InspectionItem:
    return InspectionItem(
        item_id=item_id,
        category="things",
        title=title,
        description=description,
        evidence_refs=[
            EvidenceRef(
                source_doc_id="source_embedding_test",
                source_kind="curated_source",
                locator="line:1",
                quote=description,
            )
        ],
        status=CandidateStatus.CONFIRMED,
        org_id="org_mvp",
        project_id="project_mvp",
        author_id="u_pm",
        sensitivity="l1",
    )


class MemoryEmbeddingStoreTest(unittest.TestCase):
    def test_schema_migration_removes_legacy_zero_dimension_placeholders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            store.save_item(_item("legacy_item", "历史索引占位"), materialize=False)
            with closing(sqlite3.connect(store.database_path)) as connection:
                connection.execute(
                    """
                    insert or replace into embeddings(
                      item_id, model, dimensions, vector, updated_at
                    ) values(?, '', 0, '[]', '')
                    """,
                    ("legacy_item",),
                )
                connection.commit()

            reopened = ProjectSQLiteStore(tmpdir)
            with closing(sqlite3.connect(reopened.database_path)) as connection:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "pragma table_info(embeddings)"
                    ).fetchall()
                }
                remaining = connection.execute(
                    "select count(*) from embeddings where item_id = ?",
                    ("legacy_item",),
                ).fetchone()[0]

        self.assertEqual(remaining, 0)
        self.assertTrue(
            {
                "content_hash",
                "status",
                "last_error",
                "attempts",
                "next_retry_at",
            }.issubset(columns)
        )

    def test_item_save_queues_changed_content_without_calling_embedding_api(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "MEMORY_EMBEDDING": "on",
                "MEMORY_EMBEDDING_MODEL": "test-embedding-model",
            },
            clear=False,
        ), patch(
            "store.sqlite_store._embed_texts",
            side_effect=AssertionError("item writes must not call the embedding API"),
        ):
            store = ProjectSQLiteStore(tmpdir)
            item = _item("queued_item", "待建立语义索引")
            store.save_item(item, materialize=False)
            first = store.embedding_index_entry(item.item_id)
            with closing(sqlite3.connect(store.database_path)) as connection:
                connection.execute(
                    """
                    update embeddings
                    set status = 'indexed', dimensions = 2, vector = '[1.0, 0.0]',
                        attempts = 1
                    where item_id = ?
                    """,
                    (item.item_id,),
                )
                connection.commit()

            store.save_item(item, materialize=False)
            unchanged = store.embedding_index_entry(item.item_id)
            item.description = "项目事实内容已经发生变更。"
            store.save_item(item, materialize=False)
            changed = store.embedding_index_entry(item.item_id)

        self.assertEqual(first["status"], "pending")
        self.assertTrue(first["content_hash"])
        self.assertEqual(unchanged["status"], "indexed")
        self.assertEqual(unchanged["attempts"], 1)
        self.assertEqual(changed["status"], "pending")
        self.assertNotEqual(changed["content_hash"], first["content_hash"])
        self.assertEqual(changed["dimensions"], 0)
        self.assertEqual(changed["attempts"], 0)

    def test_process_batch_indexes_real_vectors_and_reports_counts(self):
        embedded_batches: list[list[str]] = []

        def deterministic_embedder(texts: list[str]) -> list[list[float]]:
            embedded_batches.append(list(texts))
            return [[float(index + 1), 1.0] for index, _text in enumerate(texts)]

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "MEMORY_EMBEDDING": "on",
                "MEMORY_EMBEDDING_MODEL": "test-embedding-model",
            },
            clear=False,
        ):
            store = ProjectSQLiteStore(tmpdir)
            for index in range(3):
                store.save_item(
                    _item(f"batch_{index}", f"批量索引事项 {index}"),
                    materialize=False,
                )

            result = store.process_embedding_batch(
                worker_id="test-worker",
                batch_size=2,
                embedder=deterministic_embedder,
            )
            status = store.embedding_index_status(project_id="project_mvp")

        self.assertEqual(result["claimed"], 2)
        self.assertEqual(result["indexed"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(len(embedded_batches), 1)
        self.assertEqual(len(embedded_batches[0]), 2)
        self.assertEqual(status["indexed"], 2)
        self.assertEqual(status["pending"], 1)
        self.assertEqual(status["error"], 0)

    def test_process_batch_respects_configured_provider_batch_limit(self):
        observed_batch_sizes: list[int] = []

        def deterministic_embedder(texts: list[str]) -> list[list[float]]:
            observed_batch_sizes.append(len(texts))
            return [[1.0, 0.0] for _text in texts]

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "MEMORY_EMBEDDING": "on",
                "MEMORY_EMBEDDING_MODEL": "test-embedding-model",
                "MEMORY_EMBEDDING_MAX_BATCH_SIZE": "20",
            },
            clear=False,
        ):
            store = ProjectSQLiteStore(tmpdir)
            for index in range(21):
                store.save_item(
                    _item(f"provider_limit_{index}", f"接口批上限 {index}"),
                    materialize=False,
                )
            result = store.process_embedding_batch(
                worker_id="test-worker",
                batch_size=32,
                embedder=deterministic_embedder,
            )

        self.assertEqual(result["claimed"], 20)
        self.assertEqual(observed_batch_sizes, [20])

    def test_stale_embedding_result_cannot_overwrite_requeued_content(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "MEMORY_EMBEDDING": "on",
                "MEMORY_EMBEDDING_MODEL": "test-embedding-model",
            },
            clear=False,
        ):
            store = ProjectSQLiteStore(tmpdir)
            store.save_item(
                _item("edited_while_running", "并发编辑前的内容"),
                materialize=False,
            )

            def edit_during_embedding(_texts: list[str]) -> list[list[float]]:
                changed = store.get_item("edited_while_running")
                changed.description = "API 请求期间写入的新内容。"
                store.save_item(changed, materialize=False)
                return [[1.0, 0.0]]

            result = store.process_embedding_batch(
                worker_id="test-worker",
                batch_size=1,
                embedder=edit_during_embedding,
            )
            entry = store.embedding_index_entry("edited_while_running")

        self.assertEqual(result["claimed"], 1)
        self.assertEqual(result["indexed"], 0)
        self.assertEqual(result["superseded"], 1)
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["dimensions"], 0)

    def test_failed_batches_retry_then_stop_with_explicit_error(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "MEMORY_EMBEDDING": "on",
                "MEMORY_EMBEDDING_MODEL": "test-embedding-model",
            },
            clear=False,
        ):
            store = ProjectSQLiteStore(tmpdir)
            store.save_item(_item("failing_item", "失败状态必须可见"), materialize=False)

            first = store.process_embedding_batch(
                worker_id="test-worker",
                batch_size=1,
                embedder=lambda _texts: (_ for _ in ()).throw(
                    RuntimeError("embedding endpoint unavailable")
                ),
                max_attempts=2,
                retry_delay_seconds=0,
            )
            retrying = store.embedding_index_entry("failing_item")
            second = store.process_embedding_batch(
                worker_id="test-worker",
                batch_size=1,
                embedder=lambda _texts: (_ for _ in ()).throw(
                    RuntimeError("embedding endpoint unavailable")
                ),
                max_attempts=2,
                retry_delay_seconds=0,
            )
            failed = store.embedding_index_entry("failing_item")

        self.assertEqual(first["failed"], 1)
        self.assertEqual(retrying["status"], "retrying")
        self.assertIn("embedding endpoint unavailable", retrying["last_error"])
        self.assertEqual(second["failed"], 1)
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["attempts"], 2)
        self.assertIn("embedding endpoint unavailable", failed["last_error"])

    def test_interrupted_batches_are_recovered_to_pending(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "MEMORY_EMBEDDING": "on",
                "MEMORY_EMBEDDING_MODEL": "test-embedding-model",
            },
            clear=False,
        ):
            store = ProjectSQLiteStore(tmpdir)
            store.save_item(_item("interrupted_item", "中断索引"), materialize=False)
            with closing(sqlite3.connect(store.database_path)) as connection:
                connection.execute(
                    "update embeddings set status = 'running' where item_id = ?",
                    ("interrupted_item",),
                )
                connection.commit()

            recovered = store.recover_interrupted_embeddings()
            entry = store.embedding_index_entry("interrupted_item")

        self.assertEqual(recovered, 1)
        self.assertEqual(entry["status"], "pending")
        self.assertIn("process restart", entry["last_error"])


class MemoryEmbeddingWorkerTest(unittest.TestCase):
    def test_worker_default_batch_matches_provider_limit(self):
        from agent.memory_embedding_worker import MemoryEmbeddingWorker

        with tempfile.TemporaryDirectory() as tmpdir:
            worker = MemoryEmbeddingWorker(
                store_factory=lambda: ProjectSQLiteStore(tmpdir),
                enabled=False,
            )

        self.assertEqual(worker.batch_size, 20)

    def test_backfill_drains_pending_embeddings_and_reports_complete_index(self):
        from scripts.backfill_memory_embeddings import run_backfill

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "MEMORY_EMBEDDING": "on",
                "MEMORY_EMBEDDING_MODEL": "test-embedding-model",
            },
            clear=False,
        ):
            store = ProjectSQLiteStore(tmpdir)
            for index in range(3):
                store.save_item(
                    _item(f"backfill_{index}", f"回填事项 {index}"),
                    materialize=False,
                )
            report = run_backfill(
                store,
                batch_size=2,
                embedder=lambda texts: [[1.0, 0.0] for _text in texts],
            )

        self.assertEqual(report["processed_items"], 3)
        self.assertEqual(report["failed_items"], 0)
        self.assertTrue(report["index"]["complete"])
        self.assertEqual(report["index"]["pending"], 0)
        self.assertEqual(report["index"]["retrying"], 0)
        self.assertEqual(report["index"]["error"], 0)

    def test_disabled_worker_is_explicit_and_does_not_start(self):
        from agent.memory_embedding_worker import MemoryEmbeddingWorker

        with tempfile.TemporaryDirectory() as tmpdir:
            worker = MemoryEmbeddingWorker(
                store_factory=lambda: ProjectSQLiteStore(tmpdir),
                enabled=False,
                disabled_reason="MEMORY_EMBEDDING is off",
            )

            started = worker.start()
            status = worker.status()

        self.assertFalse(started)
        self.assertFalse(status["enabled"])
        self.assertFalse(status["running"])
        self.assertEqual(status["disabled_reason"], "MEMORY_EMBEDDING is off")

    def test_worker_processes_a_pending_batch_after_wake(self):
        from agent.memory_embedding_worker import MemoryEmbeddingWorker

        completed = threading.Event()

        def embedder(texts: list[str]) -> list[list[float]]:
            completed.set()
            return [[1.0, 0.0] for _text in texts]

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "MEMORY_EMBEDDING": "on",
                "MEMORY_EMBEDDING_MODEL": "test-embedding-model",
            },
            clear=False,
        ):
            store = ProjectSQLiteStore(tmpdir)
            store.save_item(_item("worker_item", "后台批量索引"), materialize=False)
            worker = MemoryEmbeddingWorker(
                store_factory=lambda: ProjectSQLiteStore(tmpdir),
                embedder=embedder,
                poll_interval=1.0,
                batch_size=8,
                worker_id="embedding-test-worker",
            )

            self.assertTrue(worker.start())
            self.assertTrue(worker.wake())
            self.assertTrue(completed.wait(timeout=2.0))
            deadline = time.time() + 2.0
            entry = store.embedding_index_entry("worker_item")
            while time.time() < deadline and entry["status"] != "indexed":
                time.sleep(0.01)
                entry = store.embedding_index_entry("worker_item")
            running_status = worker.status()
            worker.shutdown()

        self.assertEqual(entry["status"], "indexed")
        self.assertGreaterEqual(running_status["processed_items"], 1)
        self.assertEqual(running_status["last_error"], "")
        self.assertFalse(worker.status()["running"])


if __name__ == "__main__":
    unittest.main()
