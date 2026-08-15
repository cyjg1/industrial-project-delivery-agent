import tempfile
import threading
import unittest
from unittest.mock import patch

from agent.access_policy import User
from agent.conversation.run_manager import ConversationRunManager
from store.sqlite_store import ProjectSQLiteStore


def _seed_actor(store: ProjectSQLiteStore) -> tuple[User, object]:
    store.upsert_org("org_mvp", "MVP Org")
    store.upsert_user("u_pm", "org_mvp", "Project Manager")
    store.upsert_project("project_mvp", "org_mvp", "MVP Project", "u_pm")
    store.upsert_project_member("project_mvp", "u_pm", "pm")
    actor = User(id="u_pm", org_id="org_mvp", name="Project Manager")
    return actor, store.access_context_for_actor(actor.id)


class ConversationRunManagerTest(unittest.TestCase):
    def setUp(self):
        self._model_patch = patch(
            "agent.conversation.run_manager.require_conversation_model",
            side_effect=lambda requested: requested or "public-test-model",
        )
        self._model_patch.start()

    def tearDown(self):
        self._model_patch.stop()

    def test_wait_does_not_return_until_terminal_local_worker_exits(self):
        worker_release = threading.Event()
        waiter_done = threading.Event()
        manager = ConversationRunManager(
            store_factory=lambda: None,
            runtime_factory=lambda active_store: object(),
            milestone_loader=lambda active_store, project_id: object(),
            max_workers=1,
        )
        future = manager.executor.submit(worker_release.wait, 2)
        with manager._lock:
            manager._futures["run_terminal"] = future
        manager.get_run = lambda run_id: {
            "run_id": run_id,
            "status": "completed",
        }
        result: list[dict[str, object]] = []

        def wait_for_run() -> None:
            result.append(manager.wait("run_terminal", timeout=1))
            waiter_done.set()

        waiter = threading.Thread(target=wait_for_run)
        try:
            waiter.start()
            self.assertFalse(waiter_done.wait(0.1))
            worker_release.set()
            self.assertTrue(waiter_done.wait(1))
            waiter.join(1)
        finally:
            worker_release.set()
            waiter.join(1)
            manager.shutdown()

        self.assertEqual(result[0]["status"], "completed")

    def test_selected_model_is_persisted_and_reused_by_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            actor, context = _seed_actor(store)
            observed_models: list[str] = []

            def event_source(**kwargs):
                observed_models.append(kwargs["model_id"])
                yield {
                    "event": "final",
                    "data": {
                        "session_id": kwargs["session_id"],
                        "message_id": "message_final",
                        "reply": "完成",
                        "tool_steps": [],
                        "workspace": {},
                        "debug": {"model": kwargs["model_id"]},
                    },
                }

            manager = ConversationRunManager(
                store_factory=lambda: ProjectSQLiteStore(tmpdir),
                runtime_factory=lambda active_store: object(),
                milestone_loader=lambda active_store, project_id: object(),
                event_source=event_source,
                max_workers=1,
            )
            try:
                submitted = manager.submit(
                    actor=actor,
                    access_context=context,
                    project_id="project_mvp",
                    view="overview",
                    message="使用指定模型",
                    model_id="qwen3.5-plus",
                    client_request_id="model_request",
                )
                manager.wait(submitted["run_id"], timeout=2)
                active_store = ProjectSQLiteStore(tmpdir)
                run = active_store.runtime_runs.get_run(submitted["run_id"])
                messages = active_store.list_session_messages(submitted["session_id"])
            finally:
                manager.shutdown()

        self.assertEqual(run["input_payload"]["model"], "qwen3.5-plus")
        self.assertEqual(observed_models, ["qwen3.5-plus"])
        self.assertEqual(messages[0]["metadata"]["model"], "qwen3.5-plus")

    def test_execution_survives_subscriber_absence_and_events_replay_by_sequence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            actor, context = _seed_actor(store)
            started = threading.Event()
            release = threading.Event()

            def event_source(**kwargs):
                started.set()
                yield {"event": "delta", "data": {"text": "前半段"}}
                release.wait(2)
                yield {"event": "delta", "data": {"text": "后半段"}}
                yield {
                    "event": "final",
                    "data": {
                        "session_id": kwargs["session_id"],
                        "message_id": "message_final",
                        "reply": "前半段后半段",
                        "tool_steps": [],
                        "workspace": {},
                        "debug": {},
                    },
                }

            manager = ConversationRunManager(
                store_factory=lambda: ProjectSQLiteStore(tmpdir),
                runtime_factory=lambda active_store: object(),
                milestone_loader=lambda active_store, project_id: object(),
                event_source=event_source,
                max_workers=1,
            )
            try:
                submitted = manager.submit(
                    actor=actor,
                    access_context=context,
                    project_id="project_mvp",
                    view="overview",
                    message="检查持久化流",
                    client_request_id="browser_request_1",
                )
                self.assertTrue(started.wait(1))
                first_batch = manager.list_events(submitted["run_id"])
                self.assertEqual(first_batch[0]["event_type"], "run_started")
                self.assertEqual(first_batch[-1]["event_type"], "delta")

                last_seen = first_batch[-1]["seq"]
                release.set()
                completed = manager.wait(submitted["run_id"], timeout=2)
                replay = manager.list_events(submitted["run_id"], after_seq=last_seen)

                self.assertEqual(completed["status"], "completed")
                self.assertEqual(
                    [event["event_type"] for event in replay],
                    ["delta", "final"],
                )
                self.assertEqual(completed["output_payload"]["reply"], "前半段后半段")
            finally:
                release.set()
                manager.shutdown()

    def test_idempotent_browser_request_does_not_duplicate_user_turn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            actor, context = _seed_actor(store)

            def event_source(**kwargs):
                yield {
                    "event": "final",
                    "data": {
                        "session_id": kwargs["session_id"],
                        "message_id": "message_final",
                        "reply": "完成",
                        "tool_steps": [],
                        "workspace": {},
                        "debug": {},
                    },
                }

            manager = ConversationRunManager(
                store_factory=lambda: ProjectSQLiteStore(tmpdir),
                runtime_factory=lambda active_store: object(),
                milestone_loader=lambda active_store, project_id: object(),
                event_source=event_source,
                max_workers=1,
            )
            try:
                first = manager.submit(
                    actor=actor,
                    access_context=context,
                    project_id="project_mvp",
                    view="overview",
                    message="只执行一次",
                    client_request_id="stable_request",
                )
                repeated = manager.submit(
                    actor=actor,
                    access_context=context,
                    project_id="project_mvp",
                    view="overview",
                    message="只执行一次",
                    client_request_id="stable_request",
                )
                manager.wait(first["run_id"], timeout=2)
                messages = ProjectSQLiteStore(tmpdir).list_session_messages(first["session_id"])

                self.assertEqual(repeated["run_id"], first["run_id"])
                self.assertEqual([message["role"] for message in messages], ["user"])
                self.assertEqual(messages[0]["state"], "committed")
            finally:
                manager.shutdown()

    def test_failed_turn_is_explicit_and_excluded_from_next_model_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            actor, context = _seed_actor(store)

            def event_source(**kwargs):
                yield {"event": "delta", "data": {"text": "未完成草稿"}}
                raise ConnectionError("provider disconnected")

            manager = ConversationRunManager(
                store_factory=lambda: ProjectSQLiteStore(tmpdir),
                runtime_factory=lambda active_store: object(),
                milestone_loader=lambda active_store, project_id: object(),
                event_source=event_source,
                max_workers=1,
            )
            try:
                submitted = manager.submit(
                    actor=actor,
                    access_context=context,
                    project_id="project_mvp",
                    view="overview",
                    message="旧话题",
                    client_request_id="failed_request",
                )
                failed = manager.wait(submitted["run_id"], timeout=2)
                active_store = ProjectSQLiteStore(tmpdir)
                messages = active_store.list_session_messages(submitted["session_id"])
                history = active_store.list_model_session_messages(submitted["session_id"])
                error = manager.list_events(submitted["run_id"])[-1]

                self.assertEqual(failed["status"], "failed")
                self.assertEqual(messages[0]["state"], "failed")
                self.assertEqual(history, [])
                self.assertEqual(error["event_type"], "error")
                self.assertNotIn("provider disconnected", str(error["payload"]))
                self.assertTrue(error["payload"]["error_id"].startswith("err_"))
            finally:
                manager.shutdown()


if __name__ == "__main__":
    unittest.main()
