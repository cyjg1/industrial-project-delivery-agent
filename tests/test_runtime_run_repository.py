import tempfile
import unittest
from pathlib import Path

from agent.runtime.contracts import RuntimeEvent
from store.database import SQLiteDatabase
from store.runtime_repository import (
    RuntimeRunRepository,
    SQLiteRuntimeCheckpointSink,
    SQLiteRuntimeEventSink,
)
from store.session_repository import SessionRepository


class RuntimeRunRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.tmpdir.name) / "project.db")
        self.repository = RuntimeRunRepository(self.database)
        self.repository.ensure_schema()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_client_request_id_is_idempotent_and_events_replay_after_sequence(self):
        first = self.repository.create_run(
            run_id="run_first",
            kind="conversation",
            project_id="project_mvp",
            actor_id="u_pm",
            session_id="session_1",
            turn_id="turn_1",
            client_request_id="browser_request_1",
            input_payload={"message": "question"},
        )
        repeated = self.repository.create_run(
            run_id="run_second",
            kind="conversation",
            project_id="project_mvp",
            actor_id="u_pm",
            session_id="session_1",
            turn_id="turn_2",
            client_request_id="browser_request_1",
            input_payload={"message": "question"},
        )
        sink = SQLiteRuntimeEventSink(self.repository)
        sink.append(RuntimeEvent(
            run_id=first["run_id"],
            kind="model_delta",
            payload={"text": "A"},
            created_at="2026-07-22T00:00:00+00:00",
            round_index=1,
        ))
        sink.append(RuntimeEvent(
            run_id=first["run_id"],
            kind="tool_start",
            payload={"name": "lookup"},
            created_at="2026-07-22T00:00:01+00:00",
            round_index=1,
        ))
        all_events = self.repository.list_events(first["run_id"])
        replay = self.repository.list_events(first["run_id"], after_seq=all_events[0]["seq"])

        self.assertEqual(repeated["run_id"], first["run_id"])
        self.assertEqual([event["event_type"] for event in all_events], ["model_delta", "tool_start"])
        self.assertEqual([event["event_type"] for event in replay], ["tool_start"])
        self.assertGreater(all_events[1]["seq"], all_events[0]["seq"])

    def test_terminal_status_and_interrupted_run_recovery_are_explicit(self):
        self.repository.create_run(
            run_id="run_complete",
            kind="conversation",
            project_id="project_mvp",
            actor_id="u_pm",
            input_payload={"message": "done"},
        )
        self.repository.mark_running("run_complete")
        completed = self.repository.complete_run(
            "run_complete",
            stop_reason="model_completed",
            output_payload={"reply": "done"},
        )
        self.repository.create_run(
            run_id="run_recover",
            kind="conversation",
            project_id="project_mvp",
            actor_id="u_pm",
            input_payload={"message": "resume"},
            recoverable=True,
        )
        self.repository.mark_running("run_recover")

        recovered = self.repository.recover_interrupted()

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["output_payload"]["reply"], "done")
        self.assertEqual(recovered, ["run_recover"])
        self.assertEqual(self.repository.get_run("run_recover")["status"], "queued")

    def test_checkpoint_sink_persists_restartable_runtime_state(self):
        self.repository.create_run(
            run_id="run_checkpoint",
            kind="conversation",
            project_id="project_mvp",
            actor_id="u_pm",
            input_payload={"message": "resume"},
        )
        sink = SQLiteRuntimeCheckpointSink(self.repository)

        sink.checkpoint(
            "run_checkpoint",
            {
                "checkpoint_version": 1,
                "stage": "before_model",
                "next_round": 2,
                "messages": [{"role": "tool", "content": "result"}],
            },
        )

        checkpoint = self.repository.get_run("run_checkpoint")["checkpoint"]
        self.assertEqual(checkpoint["stage"], "before_model")
        self.assertEqual(checkpoint["next_round"], 2)
        self.assertEqual(checkpoint["messages"][0]["role"], "tool")


class SessionRepositoryTest(unittest.TestCase):
    def test_turn_state_is_persisted_and_failed_turn_is_not_committed_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            database = SQLiteDatabase(Path(tmpdir) / "project.db")
            repository = SessionRepository(database)
            repository.ensure_schema()
            session = repository.ensure_session(
                session_id="session_1",
                title="conversation",
                project_id="project_mvp",
                actor_id="u_pm",
            )
            repository.append_message(
                session["session_id"],
                "user",
                "failed topic",
                turn_id="turn_failed",
                run_id="run_failed",
                state="running",
            )
            repository.update_turn_state("turn_failed", "failed")
            repository.append_message(
                session["session_id"],
                "user",
                "current topic",
                turn_id="turn_current",
                run_id="run_current",
                state="running",
            )

            history = repository.list_model_history(
                session["session_id"],
                current_turn_id="turn_current",
            )

        self.assertEqual([message["content"] for message in history], ["current topic"])
        self.assertEqual(history[0]["state"], "running")


if __name__ == "__main__":
    unittest.main()
