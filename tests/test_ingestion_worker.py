import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent.access_policy import User
from agent.ingestion_worker import IngestionWorker
from store.sqlite_store import ProjectSQLiteStore


class _CompletingService:
    def __init__(self, store: ProjectSQLiteStore, completed: threading.Event):
        self.store = store
        self.completed = completed

    def process_next(self, *, worker_id: str):
        job = self.store.claim_next_ingestion_job(worker_id=worker_id)
        if job is None:
            return None
        result = self.store.complete_ingestion_job(job["id"])
        self.completed.set()
        return result


class IngestionWorkerTest(unittest.TestCase):
    def test_start_recovers_interrupted_job_and_starts_only_one_worker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_path = root / "meeting.md"
            input_path.write_text("# meeting", encoding="utf-8")
            store = ProjectSQLiteStore(root)
            actor = User(id="u_pm", org_id="org_mvp", name="PM")
            store.save_source({
                "doc_id": "meeting_restart",
                "title": "Restart meeting",
                "meeting_date": "2026-07-15",
                "curated_source": {"path": str(input_path), "status": "matched"},
                "raw_source": {"path": "", "status": "raw_source_pending"},
                "org_id": "org_mvp",
                "project_id": "project_mvp",
                "topic_id": None,
                "author_id": actor.id,
                "sensitivity": "l1",
            }, materialize=False)
            job = store.enqueue_ingestion_job(
                source_id="meeting_restart",
                input_kind="minutes",
                content_hash="sha256-restart",
                processor_version="test-v1",
                input_path=str(input_path),
                actor=actor,
            )
            store.claim_next_ingestion_job(worker_id="dead-worker")
            completed = threading.Event()
            worker = IngestionWorker(
                store_factory=lambda: ProjectSQLiteStore(root),
                service_factory=lambda worker_store: _CompletingService(worker_store, completed),
                poll_interval=0.02,
                worker_id="test-worker",
            )

            first_start = worker.start()
            second_start = worker.start()
            self.assertTrue(completed.wait(timeout=2.0))
            running_status = worker.status()
            worker.shutdown()

            self.assertTrue(first_start)
            self.assertFalse(second_start)
            self.assertEqual(running_status["recovered_jobs"], 1)
            self.assertTrue(running_status["running"])
            self.assertEqual(store.get_ingestion_job(job["id"])["status"], "completed")
            self.assertFalse(worker.status()["running"])
            recovery_events = store.list_events(
                entity="ingestion_job",
                entity_id=job["id"],
                action="ingestion_job_recovered",
            )
            self.assertEqual(len(recovery_events), 1)

    def test_service_initialization_failure_is_exposed_without_killing_worker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            worker = IngestionWorker(
                store_factory=lambda: ProjectSQLiteStore(tmpdir),
                service_factory=lambda store: (_ for _ in ()).throw(
                    RuntimeError("provider configuration is invalid")
                ),
                poll_interval=0.02,
                worker_id="failing-worker",
            )

            worker.start()
            deadline = time.time() + 1.0
            status = worker.status()
            while time.time() < deadline and not status["last_error"]:
                time.sleep(0.02)
                status = worker.status()
            worker.shutdown()

        self.assertTrue(status["running"])
        self.assertIn("provider configuration is invalid", status["last_error"])


if __name__ == "__main__":
    unittest.main()
