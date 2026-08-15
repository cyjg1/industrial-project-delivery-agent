import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from agent.conversation_agent import (
    _ConversationAnswerGate,
    _verify_reply_against_tool_results,
)
from agent.runtime.contracts import (
    CompletionDecision,
    PlannerStreamEvent,
    RuntimeFeatures,
    ToolCallRequest,
)
from agent.runtime.factory import create_agent_runtime
from backend.main import create_app
from backend.routers.conversation import _persisted_events
from fastapi.testclient import TestClient
from store.sqlite_store import ProjectSQLiteStore


def _call(call_id: str, name: str, **arguments):
    return ToolCallRequest(
        call_id=call_id,
        name=name,
        arguments=arguments,
        reason=f"need {name}",
    )


class _Planner:
    name = "scripted"

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def stream_turn(self, messages, tools):
        self.requests.append(list(messages))
        if not self.turns:
            raise AssertionError("unexpected model turn")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        yield from turn


class _AlwaysComplete:
    def evaluate(self, domain_state, runtime_state=None):
        return CompletionDecision(complete=True, reason="complete")


class _RecordingSink:
    def __init__(self):
        self.events = []
        self.checkpoints = []

    def append(self, event):
        self.events.append(event)

    def checkpoint(self, run_id, checkpoint):
        self.checkpoints.append((run_id, checkpoint))


class AgentRuntimeUpgradeTest(unittest.TestCase):
    def test_iso_date_before_project_label_is_not_misread_as_an_item_count(self):
        verification = _verify_reply_against_tool_results(
            "# 2026-07-23 [F_brief_date] 项目经理晨报",
            [
                {
                    "result": {
                        "facts": [
                            {
                                "fact_id": "F_brief_date",
                                "kind": "date",
                                "value": "2026-07-23",
                                "source_refs": [],
                            }
                        ]
                    }
                }
            ],
            user_text="生成晨报",
        )

        self.assertEqual(verification["unsupported"], [])

    @unittest.skip("legacy test assumed a repository-provided mock model configuration")
    def test_conversation_run_manager_persists_completed_runtime_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"LLM_PROVIDER": "mock", "MEMORY_EMBEDDING": "off"},
            clear=False,
        ):
            store = ProjectSQLiteStore(tmpdir)
            store.upsert_org("org_mvp", "MVP Org")
            store.upsert_user("u_pm", "org_mvp", "Project Manager")
            store.upsert_project(
                "project_mvp",
                "org_mvp",
                "MVP Project",
                "u_pm",
            )
            store.upsert_project_member("project_mvp", "u_pm", "pm")
            response = TestClient(create_app(store_dir=tmpdir)).post(
                "/api/conversation",
                headers={"X-Actor-Id": "u_pm"},
                json={"message": "你好"},
            )
            runs = ProjectSQLiteStore(tmpdir).runtime_runs.list_runs(
                kind="conversation",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(runs)
        checkpoint = runs[-1]["checkpoint"]
        self.assertEqual(checkpoint["stage"], "completed")
        self.assertEqual(checkpoint["final_text"], response.json()["reply"])
        json.dumps(checkpoint, ensure_ascii=False)

    def test_conversation_answer_gate_rejects_date_without_visible_tool_fact(self):
        runtime = create_agent_runtime(
            planner=_Planner(
                [
                    [PlannerStreamEvent(kind="delta", text="预计 2030-01-01 完成。")],
                    [PlannerStreamEvent(kind="delta", text="目前没有可核实的完成日期。")],
                ]
            ),
            tools=[],
            invoke_tool=lambda call: {},
            completion_gate=_ConversationAnswerGate("什么时候完成？"),
            features=RuntimeFeatures(max_rounds=3),
        )

        events = list(
            runtime.stream(
                run_id="run_conversation_fact_gate",
                initial_messages=[{"role": "user", "content": "什么时候完成？"}],
                domain_state={},
            )
        )

        self.assertIn(
            "completion_rejected",
            [event.kind for event in events],
        )
        self.assertEqual(
            events[-1].result.final_text,
            "目前没有可核实的完成日期。",
        )

    def test_completion_gate_verifies_actual_final_text_and_rewrite_resets_stream(self):
        planner = _Planner(
            [
                [PlannerStreamEvent(kind="delta", text="计划于 2030-01-01 完成。")],
                [PlannerStreamEvent(kind="delta", text="当前日期待核实后再确认。")],
            ]
        )

        class FinalTextGate:
            def evaluate(self, domain_state, runtime_state=None):
                if runtime_state is None or not runtime_state.final_text:
                    return CompletionDecision(complete=False, reason="waiting")
                if "2030-01-01" in runtime_state.final_text:
                    return CompletionDecision(
                        complete=False,
                        reason="unsupported fact",
                        errors=["日期 2030-01-01 没有工具证据"],
                    )
                return CompletionDecision(complete=True, reason="verified")

        runtime = create_agent_runtime(
            planner=planner,
            tools=[],
            invoke_tool=lambda call: {},
            completion_gate=FinalTextGate(),
            features=RuntimeFeatures(max_rounds=3),
        )

        events = list(
            runtime.stream(
                run_id="run_verify_rewrite",
                initial_messages=[{"role": "user", "content": "何时完成？"}],
                domain_state={},
            )
        )

        kinds = [event.kind for event in events]
        self.assertIn("completion_rejected", kinds)
        self.assertIn("model_output_reset", kinds)
        self.assertEqual(events[-1].result.final_text, "当前日期待核实后再确认。")
        self.assertEqual(events[-1].result.stop_reason, "model_completed")
        self.assertTrue(
            any(
                "2030-01-01" in str(message.get("content", ""))
                for message in planner.requests[1]
            )
        )

    def test_unverified_finalization_after_budget_stop_is_not_returned(self):
        runtime = create_agent_runtime(
            planner=_Planner(
                [
                    [
                        PlannerStreamEvent(
                            kind="tool_calls",
                            tool_calls=(_call("call_lookup", "lookup"),),
                        )
                    ],
                    [
                        PlannerStreamEvent(
                            kind="tool_calls",
                            tool_calls=(_call("call_lookup_again", "lookup"),),
                        )
                    ],
                    [PlannerStreamEvent(kind="delta", text="预计 2030-01-01 完成。")],
                ]
            ),
            tools=[],
            invoke_tool=lambda call: {"ok": True, "summary": "没有查到日期"},
            completion_gate=_ConversationAnswerGate("什么时候完成？"),
            features=RuntimeFeatures(max_rounds=4, final_answer_on_stop=True),
        )

        events = list(
            runtime.stream(
                run_id="run_reject_finalization",
                initial_messages=[{"role": "user", "content": "什么时候完成？"}],
                domain_state={},
            )
        )

        kinds = [event.kind for event in events]
        self.assertIn("completion_rejected", kinds)
        self.assertIn("model_output_reset", kinds)
        self.assertFalse(events[-1].result.completed)
        self.assertEqual(events[-1].result.final_text, "")
        self.assertEqual(events[-1].result.stop_reason, "no_progress")

    def test_max_rounds_does_not_issue_an_unbudgeted_final_model_turn(self):
        planner = _Planner(
            [
                [
                    PlannerStreamEvent(
                        kind="tool_calls",
                        tool_calls=(_call("call_lookup", "lookup"),),
                    )
                ],
                [PlannerStreamEvent(kind="delta", text="不应执行")],
            ]
        )
        runtime = create_agent_runtime(
            planner=planner,
            tools=[],
            invoke_tool=lambda call: {"ok": True, "summary": "done"},
            completion_gate=_AlwaysComplete(),
            features=RuntimeFeatures(max_rounds=1, final_answer_on_stop=True),
        )

        result = runtime.run(
            run_id="run_strict_round_budget",
            initial_messages=[{"role": "user", "content": "run"}],
            domain_state={},
            system_prompt="",
            user_prompt="",
        )

        self.assertEqual(len(planner.requests), 1)
        self.assertEqual(result.model_turn_count, 1)
        self.assertEqual(result.final_text, "")
        self.assertEqual(result.stop_reason, "max_rounds_reached")

    def test_elapsed_budget_discards_a_model_turn_that_finishes_after_deadline(self):
        class SlowPlanner(_Planner):
            def stream_turn(self, messages, tools):
                self.requests.append(list(messages))
                time.sleep(0.02)
                yield PlannerStreamEvent(kind="delta", text="超时后的回答")

        planner = SlowPlanner([])
        runtime = create_agent_runtime(
            planner=planner,
            tools=[],
            invoke_tool=lambda call: {},
            completion_gate=_AlwaysComplete(),
            features=RuntimeFeatures(
                max_rounds=3,
                max_elapsed_seconds=0.005,
                final_answer_on_stop=True,
            ),
        )

        events = list(
            runtime.stream(
                run_id="run_elapsed_model_budget",
                initial_messages=[{"role": "user", "content": "run"}],
                domain_state={},
            )
        )

        self.assertEqual(len(planner.requests), 1)
        self.assertEqual(events[-1].result.stop_reason, "max_elapsed_time_reached")
        self.assertEqual(events[-1].result.final_text, "")
        self.assertIn("model_output_reset", [event.kind for event in events])

    def test_terminal_run_without_terminal_event_emits_error_and_closes(self):
        class TerminalManager:
            def list_events(self, run_id, after_seq=0):
                return []

            def get_run(self, run_id):
                return {
                    "status": "failed",
                    "error_id": "err_missing_event",
                }

        frames = list(
            _persisted_events(
                TerminalManager(),
                "run_missing_terminal_event",
                after_seq=0,
                poll_seconds=0.001,
                heartbeat_seconds=10,
                terminal_grace_seconds=0.005,
            )
        )

        self.assertEqual(len(frames), 1)
        self.assertIn("event: error", frames[0])
        self.assertIn("err_missing_event", frames[0])

    def test_total_tool_call_budget_stops_before_executing_excess_call(self):
        planner = _Planner(
            [
                [
                    PlannerStreamEvent(
                        kind="tool_calls",
                        tool_calls=(
                            _call("call_a", "A"),
                            _call("call_b", "B"),
                        ),
                    )
                ]
            ]
        )
        invoked = []
        runtime = create_agent_runtime(
            planner=planner,
            tools=[],
            invoke_tool=lambda call: invoked.append(call.name) or {
                "ok": True,
                "summary": call.name,
            },
            completion_gate=_AlwaysComplete(),
            features=RuntimeFeatures(max_rounds=3, max_tool_calls=1),
        )

        result = runtime.run(
            run_id="run_tool_budget",
            initial_messages=[{"role": "user", "content": "run"}],
            domain_state={},
            system_prompt="",
            user_prompt="",
        )

        self.assertEqual(invoked, ["A"])
        self.assertEqual(result.stop_reason, "max_tool_calls_reached")
        self.assertFalse(result.completed)

    def test_repeated_call_does_not_suppress_new_call_in_same_turn(self):
        planner = _Planner(
            [
                [
                    PlannerStreamEvent(
                        kind="tool_calls",
                        tool_calls=(_call("call_a1", "A", query="same"),),
                    )
                ],
                [
                    PlannerStreamEvent(
                        kind="tool_calls",
                        tool_calls=(
                            _call("call_a2", "A", query="same"),
                            _call("call_b1", "B", query="new"),
                        ),
                    )
                ],
                [PlannerStreamEvent(kind="delta", text="完成")],
            ]
        )
        invoked = []
        runtime = create_agent_runtime(
            planner=planner,
            tools=[],
            invoke_tool=lambda call: invoked.append(call.name) or {
                "ok": True,
                "summary": call.name,
            },
            completion_gate=_AlwaysComplete(),
            features=RuntimeFeatures(max_rounds=4),
        )

        result = runtime.run(
            run_id="run_duplicate_and_progress",
            initial_messages=[{"role": "user", "content": "run"}],
            domain_state={},
            system_prompt="",
            user_prompt="",
        )

        self.assertEqual(invoked, ["A", "B"])
        self.assertEqual(result.stop_reason, "model_completed")
        self.assertEqual(result.final_text, "完成")

    def test_stable_checkpoint_resumes_after_completed_tool_without_reexecution(self):
        first_sink = _RecordingSink()
        first_runtime = create_agent_runtime(
            planner=_Planner(
                [
                    [
                        PlannerStreamEvent(
                            kind="tool_calls",
                            tool_calls=(_call("call_lookup", "lookup", query="risk"),),
                        )
                    ],
                    RuntimeError("process interrupted before next model turn"),
                ]
            ),
            tools=[],
            invoke_tool=lambda call: {"ok": True, "summary": "risk found"},
            completion_gate=_AlwaysComplete(),
            event_sink=first_sink,
        )

        with self.assertRaises(RuntimeError):
            list(
                first_runtime.stream(
                    run_id="run_resume",
                    initial_messages=[{"role": "user", "content": "risk"}],
                    domain_state={},
                )
            )
        stable = [
            checkpoint
            for _run_id, checkpoint in first_sink.checkpoints
            if checkpoint.get("stage") == "before_model"
            and checkpoint.get("next_round") == 2
        ][-1]
        second_runtime = create_agent_runtime(
            planner=_Planner(
                [[PlannerStreamEvent(kind="delta", text="已基于风险结果恢复。")]]
            ),
            tools=[],
            invoke_tool=lambda call: self.fail("completed tool must not be re-executed"),
            completion_gate=_AlwaysComplete(),
        )

        result = second_runtime.run(
            run_id="run_resume",
            initial_messages=[{"role": "user", "content": "ignored"}],
            domain_state={},
            system_prompt="",
            user_prompt="",
            resume_checkpoint=stable,
        )

        self.assertEqual(result.final_text, "已基于风险结果恢复。")
        self.assertEqual(result.model_turn_count, 2)
        self.assertEqual(len(result.tool_results), 1)

    def test_completed_checkpoint_returns_same_terminal_result_without_model_call(self):
        sink = _RecordingSink()
        first = create_agent_runtime(
            planner=_Planner(
                [[PlannerStreamEvent(kind="delta", text="已完成回答。")]]
            ),
            tools=[],
            invoke_tool=lambda call: {},
            completion_gate=_AlwaysComplete(),
            event_sink=sink,
        )
        first_result = first.run(
            run_id="run_completed_resume",
            initial_messages=[{"role": "user", "content": "question"}],
            domain_state={},
            system_prompt="",
            user_prompt="",
        )
        checkpoint = [
            value
            for _run_id, value in sink.checkpoints
            if value.get("stage") == "completed"
        ][-1]
        resumed = create_agent_runtime(
            planner=_Planner([]),
            tools=[],
            invoke_tool=lambda call: self.fail("no tool should run"),
            completion_gate=_AlwaysComplete(),
        ).run(
            run_id="run_completed_resume",
            initial_messages=[{"role": "user", "content": "ignored"}],
            domain_state={},
            system_prompt="",
            user_prompt="",
            resume_checkpoint=checkpoint,
        )

        self.assertTrue(first_result.completed)
        self.assertTrue(resumed.completed)
        self.assertEqual(resumed.stop_reason, "model_completed")
        self.assertEqual(resumed.final_text, first_result.final_text)
        self.assertEqual(resumed.model_turn_count, 1)


if __name__ == "__main__":
    unittest.main()
