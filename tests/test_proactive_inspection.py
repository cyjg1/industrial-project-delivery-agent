import tempfile
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from agent.inspection_jobs import InspectionDispatcher, InspectionWorker
from backend.main import create_app
from store.sqlite_store import ProjectSQLiteStore


def _seed(store: ProjectSQLiteStore) -> None:
    store.upsert_org("org_mvp", "MVP Org")
    store.upsert_user("u_pm", "org_mvp", "Project Manager")
    store.upsert_user("u_exec", "org_mvp", "Executor")
    store.upsert_project("project_mvp", "org_mvp", "MVP Project", "u_pm")
    store.upsert_project_member("project_mvp", "u_pm", "pm")
    store.upsert_project_member("project_mvp", "u_exec", "exec")


class ProactiveInspectionTest(unittest.TestCase):
    def test_manual_api_queues_job_and_legacy_milestone_run_route_is_absent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed(store)
            with patch.dict(
                "os.environ",
                {"INSPECTION_WORKER_ENABLED": "off"},
                clear=False,
            ):
                client = TestClient(create_app(store_dir=tmpdir))
                client.headers.update({"X-Actor-Id": "u_pm"})
                response = client.post(
                    "/api/inspections",
                    json={"reason": "人工检查近期变化", "request_id": "manual_1"},
                )
                listed = client.get("/api/inspections")
                legacy = client.post("/api/milestones/run", json={})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["job"]["trigger_type"], "manual")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["jobs"]), 1)
        self.assertEqual(legacy.status_code, 404)

    def test_ingestion_and_completed_work_events_enqueue_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed(store)
            store.append_event(
                "ingestion_job",
                "ingest_1",
                "ingestion_job_completed",
                {
                    "project_id": "project_mvp",
                    "source_id": "source_1",
                    "author_id": "u_exec",
                    "sensitivity": "l2",
                },
            )
            store.append_event(
                "task",
                "task_open",
                "work_item_updated",
                {
                    "editor": "u_exec",
                    "work_item": {
                        "work_item_id": "task_open",
                        "project_id": "project_mvp",
                        "author_id": "u_exec",
                        "sensitivity": "l1",
                        "status": "in_progress",
                        "evidence_refs": [],
                    },
                },
            )
            store.append_event(
                "task",
                "task_done",
                "work_item_updated",
                {
                    "editor": "u_exec",
                    "work_item": {
                        "work_item_id": "task_done",
                        "project_id": "project_mvp",
                        "author_id": "u_exec",
                        "sensitivity": "l1",
                        "status": "done",
                        "evidence_refs": [{"source_doc_id": "source_2"}],
                    },
                },
            )
            dispatcher = InspectionDispatcher(store)

            first = dispatcher.dispatch_new_events()
            repeated = dispatcher.dispatch_new_events()
            jobs = store.inspection_jobs.list(project_id="project_mvp")

        self.assertEqual(len(first), 2)
        self.assertEqual(repeated, [])
        self.assertEqual(
            {job["trigger_type"] for job in jobs},
            {"ingestion_completed", "work_item_completed"},
        )
        sources = {job["trigger_type"]: job["source_ids"] for job in jobs}
        self.assertEqual(sources["ingestion_completed"], ["source_1"])
        self.assertEqual(sources["work_item_completed"], ["source_2"])
        self.assertTrue(all(job["actor_id"] == "u_pm" for job in jobs))

    def test_daily_schedule_is_idempotent_per_project_and_date(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed(store)
            dispatcher = InspectionDispatcher(store)

            first = dispatcher.enqueue_scheduled(
                project_id="project_mvp",
                inspection_date=date(2026, 7, 22),
            )
            repeated = dispatcher.enqueue_scheduled(
                project_id="project_mvp",
                inspection_date=date(2026, 7, 22),
            )

        self.assertTrue(first["created"])
        self.assertFalse(repeated["created"])
        self.assertEqual(repeated["job_id"], first["job_id"])

    def test_worker_runs_shared_inspection_profile_with_trigger_scope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed(store)
            dispatcher = InspectionDispatcher(store)
            dispatcher.enqueue_scheduled(
                project_id="project_mvp",
                inspection_date=date(2026, 7, 22),
            )
            calls = []

            class FakeRuntime:
                def run_inspection(self, milestone, **kwargs):
                    calls.append(kwargs)
                    return SimpleNamespace(
                        run_id="inspection_run_1",
                        status="completed",
                        error="",
                        stop_reason="model_completed",
                        confirmed_item_ids=[],
                        final_report=None,
                        verification=SimpleNamespace(ok=True),
                        no_change_reason="触发范围内没有值得新增的项目变化。",
                    )

            worker = InspectionWorker(
                store_factory=lambda: ProjectSQLiteStore(tmpdir),
                runtime_factory=lambda active_store: FakeRuntime(),
                milestone_loader=lambda active_store, project_id: object(),
                enabled=False,
            )
            result = worker.process_once()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["status"], "no_change")
        self.assertEqual(
            result["result"]["no_change_reason"],
            "触发范围内没有值得新增的项目变化。",
        )
        self.assertEqual(result["result_run_id"], "inspection_run_1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["actor"].id, "u_pm")
        self.assertEqual(calls[0]["trigger"].trigger_type, "scheduled_daily")

    def test_dispatch_failure_is_explicit_and_does_not_escape_worker_poll(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed(store)
            store.append_event(
                "ingestion_job",
                "bad_ingestion",
                "ingestion_job_completed",
                {
                    "project_id": "missing_project",
                    "source_id": "source_missing",
                },
            )
            worker = InspectionWorker(
                store_factory=lambda: ProjectSQLiteStore(tmpdir),
                runtime_factory=lambda active_store: None,
                milestone_loader=lambda active_store, project_id: object(),
                enabled=False,
            )

            result = worker.process_once()
            status = worker.status()

        self.assertEqual(result["status"], "dispatch_failed")
        self.assertIn("Unknown project", result["error"])
        self.assertEqual(status["last_error"], result["error"])


if __name__ == "__main__":
    unittest.main()
