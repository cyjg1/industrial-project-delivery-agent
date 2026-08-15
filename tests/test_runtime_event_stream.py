import unittest

from agent.runtime.contracts import (
    CompletionDecision,
    PlannerStreamEvent,
    RuntimeFeatures,
    ToolCallRequest,
)
from agent.runtime.factory import create_agent_runtime
from agent.runtime.planners import FunctionCallingPlanner
from agent.llm_provider import ChatStreamEvent, LLMStreamInterruptedError


class _CompleteGate:
    def evaluate(self, domain_state):
        return CompletionDecision(complete=True, reason="verified")


class _ScriptedStreamingPlanner:
    name = "scripted"

    def __init__(self, turns):
        self.turns = list(turns)
        self.index = 0

    def stream_turn(self, messages, tools):
        events = self.turns[self.index]
        self.index += 1
        yield from events


def _call(call_id="call_1"):
    return ToolCallRequest(
        call_id=call_id,
        name="lookup",
        arguments={"query": "current risk"},
        reason="need project facts",
    )


class RuntimeEventStreamTest(unittest.TestCase):
    def test_partial_model_output_is_reset_before_safe_retry(self):
        class InterruptedProvider:
            name = "interrupted"
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def stream_chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    yield ChatStreamEvent(kind="delta", text="partial")
                    raise LLMStreamInterruptedError(
                        "partial stream",
                        partial=True,
                        attempts=1,
                    )
                yield ChatStreamEvent(kind="delta", text="complete")

        provider = InterruptedProvider()
        runtime = create_agent_runtime(
            planner=FunctionCallingPlanner(provider, partial_retry_attempts=2),
            tools=[],
            invoke_tool=lambda call: {},
            completion_gate=_CompleteGate(),
        )

        events = list(
            runtime.stream(
                run_id="run_retry",
                initial_messages=[{"role": "user", "content": "question"}],
                domain_state={},
            )
        )

        self.assertEqual(provider.calls, 2)
        self.assertIn("model_output_reset", [event.kind for event in events])
        self.assertEqual(events[-1].result.final_text, "complete")

    def test_stream_is_the_single_source_for_run_and_real_time_events(self):
        planner = _ScriptedStreamingPlanner(
            [
                [PlannerStreamEvent(kind="tool_calls", tool_calls=(_call(),))],
                [
                    PlannerStreamEvent(kind="delta", text="current "),
                    PlannerStreamEvent(kind="delta", text="answer"),
                ],
            ]
        )
        runtime = create_agent_runtime(
            planner=planner,
            tools=[{"type": "function", "function": {"name": "lookup"}}],
            invoke_tool=lambda call: {"ok": True, "summary": "found one risk"},
            completion_gate=_CompleteGate(),
        )

        events = list(
            runtime.stream(
                run_id="run_stream",
                initial_messages=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "question"},
                ],
                domain_state={},
            )
        )

        kinds = [event.kind for event in events]
        self.assertLess(kinds.index("tool_start"), kinds.index("tool_result"))
        self.assertEqual(
            [event.payload["text"] for event in events if event.kind == "model_delta"],
            ["current ", "answer"],
        )
        self.assertEqual(events[-1].kind, "runtime_completed")
        self.assertIsNotNone(events[-1].result)
        self.assertEqual(events[-1].result.final_text, "current answer")

    def test_second_identical_call_is_skipped_and_protocol_is_closed(self):
        planner = _ScriptedStreamingPlanner(
            [
                [PlannerStreamEvent(kind="tool_calls", tool_calls=(_call("call_1"),))],
                [PlannerStreamEvent(kind="tool_calls", tool_calls=(_call("call_2"),))],
            ]
        )
        invocations = []
        runtime = create_agent_runtime(
            planner=planner,
            tools=[{"type": "function", "function": {"name": "lookup"}}],
            invoke_tool=lambda call: invocations.append(call.call_id) or {
                "ok": True,
                "summary": "found",
            },
            completion_gate=_CompleteGate(),
        )

        events = list(
            runtime.stream(
                run_id="run_duplicate",
                initial_messages=[{"role": "user", "content": "question"}],
                domain_state={},
            )
        )
        result = events[-1].result

        self.assertEqual(invocations, ["call_1"])
        self.assertEqual(result.stop_reason, "no_progress")
        skipped = [
            event for event in events
            if event.kind == "tool_result" and event.payload.get("skipped")
        ]
        self.assertEqual(len(skipped), 1)
        for index, message in enumerate(result.messages):
            if not message.get("tool_calls"):
                continue
            call_ids = {call["id"] for call in message["tool_calls"]}
            paired = {
                next_message.get("tool_call_id")
                for next_message in result.messages[index + 1:]
                if next_message.get("role") == "tool"
            }
            self.assertTrue(call_ids.issubset(paired))

    def test_conversation_profile_can_finalize_after_no_progress_without_an_empty_reply(self):
        planner = _ScriptedStreamingPlanner(
            [
                [PlannerStreamEvent(kind="tool_calls", tool_calls=(_call("call_1"),))],
                [PlannerStreamEvent(kind="tool_calls", tool_calls=(_call("call_2"),))],
                [PlannerStreamEvent(kind="delta", text="根据已取得的结果，先核实当前风险。")],
            ]
        )
        runtime = create_agent_runtime(
            planner=planner,
            tools=[{"type": "function", "function": {"name": "lookup"}}],
            invoke_tool=lambda call: {"ok": True, "summary": "found"},
            completion_gate=_CompleteGate(),
            features=RuntimeFeatures(final_answer_on_stop=True),
        )

        events = list(runtime.stream(
            run_id="run_duplicate_final",
            initial_messages=[{"role": "user", "content": "question"}],
            domain_state={},
        ))

        self.assertEqual(events[-1].result.stop_reason, "no_progress")
        self.assertTrue(events[-1].result.completed)
        self.assertEqual(
            events[-1].result.final_text,
            "根据已取得的结果，先核实当前风险。",
        )


if __name__ == "__main__":
    unittest.main()
