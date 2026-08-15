import unittest
from pathlib import Path

from agent.llm_provider import ChatStreamEvent, ChatToolCall
from agent.runtime import (
    CompletionDecision,
    FunctionCallingPlanner,
    RuntimeFeatures,
    ToolSurface,
    create_agent_runtime,
)


class _ScriptedProvider:
    name = "scripted-real-provider"
    model = "scripted-model"

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def stream_chat(self, messages, tools=None):
        self.requests.append({"messages": list(messages), "tools": list(tools or [])})
        if not self.turns:
            raise AssertionError("scripted provider received an unexpected model turn")
        yield from self.turns.pop(0)


class _CompletionGate:
    def evaluate(self, domain_state):
        missing = [key for key in ("report", "verification") if not domain_state.get(key)]
        return CompletionDecision(
            complete=not missing,
            reason="verified" if not missing else "incomplete",
            missing=missing,
        )


class _FactAnswerGate:
    def evaluate(self, domain_state, runtime_state=None):
        text = str(getattr(runtime_state, "final_text", "") or "")
        if text == "verified [F_method]":
            return CompletionDecision(complete=True, reason="verified")
        return CompletionDecision(
            complete=False,
            reason="unsupported answer",
            errors=["method status is missing [F_method]"],
        )


def _tool_call(name, arguments=None, reason="model selected this tool"):
    return ChatStreamEvent(
        kind="tool_calls",
        tool_calls=[
            ChatToolCall(
                call_id=f"call_{name}",
                name=name,
                arguments=arguments or {},
                reason=reason,
            )
        ],
    )


def _tool_schema(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Use {name} only when it advances the current goal.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


class RuntimeArchitectureTest(unittest.TestCase):
    def test_conversation_and_inspection_profiles_share_the_only_model_tool_loop(self):
        conversation = Path("agent/conversation/service.py").read_text(encoding="utf-8")
        inspection = Path("agent/inspection_agent/service.py").read_text(encoding="utf-8")
        executor = Path("agent/runtime/executor.py").read_text(encoding="utf-8")
        active_agent_sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in Path("agent").rglob("*.py")
            if path != Path("agent/runtime/executor.py")
        )

        self.assertIn("create_agent_runtime(", conversation)
        self.assertIn("create_agent_runtime(", inspection)
        self.assertIn("for round_index in range(", executor)
        self.assertNotIn("for round_index in range(", active_agent_sources)

    def test_actual_model_tool_calls_define_the_plan(self):
        risk_provider = _ScriptedProvider([
            [_tool_call("RiskTool", {"window": "current"}, "current risk is requested")],
            [[ChatStreamEvent(kind="delta", text="complete")][0]],
        ])
        people_provider = _ScriptedProvider([
            [_tool_call("PeopleTool", {}, "ownership is requested")],
            [[ChatStreamEvent(kind="delta", text="complete")][0]],
        ])

        def run(provider, objective):
            state = {}

            def invoke(call):
                state["report"] = True
                state["verification"] = True
                return {"ok": True, "summary": f"{call.name} completed"}

            runtime = create_agent_runtime(
                planner=FunctionCallingPlanner(provider),
                tools=[_tool_schema("RiskTool"), _tool_schema("PeopleTool")],
                invoke_tool=invoke,
                completion_gate=_CompletionGate(),
                features=RuntimeFeatures(max_rounds=4),
            )
            return runtime.run(
                run_id=f"run_{objective}",
                system_prompt="Select tools from the supplied catalog.",
                user_prompt=objective,
                domain_state=state,
            )

        risk_result = run(risk_provider, "inspect current risks")
        people_result = run(people_provider, "inspect responsibility ownership")

        self.assertEqual([step.tool_name for step in risk_result.plan], ["RiskTool"])
        self.assertEqual([step.tool_name for step in people_result.plan], ["PeopleTool"])
        self.assertNotEqual(risk_result.plan, people_result.plan)
        model_event = next(
            event for event in risk_result.events if event["kind"] == "model_turn"
        )
        self.assertEqual(model_event["payload"]["round_index"], 1)
        self.assertGreater(model_event["payload"]["input_message_count"], 0)
        self.assertGreater(model_event["payload"]["input_char_count"], 0)
        self.assertGreater(model_event["payload"]["input_token_count"], 0)
        self.assertIn("tool_calls", model_event["payload"])
        tool_event = next(
            event for event in risk_result.events if event["kind"] == "tool_result"
        )
        self.assertEqual(tool_event["payload"]["round_index"], 1)
        self.assertGreaterEqual(tool_event["payload"]["elapsed_ms"], 0)

    def test_early_model_stop_receives_missing_completion_feedback_and_repeats(self):
        provider = _ScriptedProvider([
            [ChatStreamEvent(kind="delta", text="I am done")],
            [_tool_call("InspectionTool")],
            [_tool_call("EvidenceVerifierTool")],
            [ChatStreamEvent(kind="delta", text="verified and complete")],
        ])
        state = {}

        def invoke(call):
            if call.name == "InspectionTool":
                state["report"] = True
            if call.name == "EvidenceVerifierTool":
                state["verification"] = True
            return {"ok": True, "summary": f"{call.name} completed"}

        runtime = create_agent_runtime(
            planner=FunctionCallingPlanner(provider),
            tools=[_tool_schema("InspectionTool"), _tool_schema("EvidenceVerifierTool")],
            invoke_tool=invoke,
            completion_gate=_CompletionGate(),
            features=RuntimeFeatures(max_rounds=6),
        )

        result = runtime.run(
            run_id="run_early_stop",
            system_prompt="Complete and verify the work.",
            user_prompt="inspect milestone",
            domain_state=state,
        )

        self.assertEqual(result.stop_reason, "model_completed")
        self.assertEqual(
            [step.tool_name for step in result.plan],
            ["InspectionTool", "EvidenceVerifierTool"],
        )
        second_request_messages = provider.requests[1]["messages"]
        self.assertTrue(
            any("report" in str(message.get("content", "")) for message in second_request_messages)
        )

    def test_fact_verification_retry_compacts_context_and_disables_tools(self):
        provider = _ScriptedProvider([
            [_tool_call("EvidenceTool")],
            [ChatStreamEvent(kind="delta", text="target method is confirmed")],
            [ChatStreamEvent(kind="delta", text="verified [F_method]")],
        ])
        large_result = {
            "ok": True,
            "summary": "method evidence",
            "facts": [
                *[
                    {
                        "fact_id": f"F_noise_{index}",
                        "kind": "text",
                        "path": f"$.items[{index}].description",
                        "value": f"unrelated evidence {index}",
                        "source_refs": [{"source_doc_id": "source_noise"}],
                    }
                    for index in range(30)
                ],
                {
                    "fact_id": "F_method",
                    "kind": "text",
                    "path": "$.items[31].title",
                    "value": "target method",
                    "source_refs": [{"source_doc_id": "source_1", "locator": "line:8"}],
                }
            ],
            "padding": "x" * 8000,
        }
        runtime = create_agent_runtime(
            planner=FunctionCallingPlanner(provider),
            tools=[_tool_schema("EvidenceTool")],
            invoke_tool=lambda _call: large_result,
            completion_gate=_FactAnswerGate(),
            features=RuntimeFeatures(
                max_rounds=4,
                verification_retry_context_chars=4096,
            ),
        )

        result = runtime.run(
            run_id="run_verification_repair",
            system_prompt="Answer only from verified facts.",
            user_prompt="Which method is confirmed?",
            domain_state={},
        )

        self.assertTrue(result.completed)
        self.assertEqual(result.final_text, "verified [F_method]")
        repair_request = provider.requests[2]
        self.assertEqual(repair_request["tools"], [])
        self.assertFalse(any(
            message.get("role") == "tool"
            for message in repair_request["messages"]
        ))
        self.assertIn(
            "F_method",
            "\n".join(
                str(message.get("content") or "")
                for message in repair_request["messages"]
            ),
        )
        compacted_event = next(
            event
            for event in result.events
            if event["kind"] == "verification_context_compacted"
        )
        self.assertTrue(compacted_event["payload"]["tools_disabled_for_retry"])
        self.assertLessEqual(compacted_event["payload"]["after_char_count"], 4096)

    def test_tool_call_transition_text_is_not_misreported_as_final_text(self):
        provider = _ScriptedProvider([
            [
                ChatStreamEvent(kind="delta", text="I will inspect first."),
                _tool_call("InspectionTool"),
            ],
            [],
        ])
        state = {}

        def invoke(_call):
            state["report"] = True
            state["verification"] = True
            return {"ok": True, "summary": "inspection completed"}

        runtime = create_agent_runtime(
            planner=FunctionCallingPlanner(provider),
            tools=[_tool_schema("InspectionTool")],
            invoke_tool=invoke,
            completion_gate=_CompletionGate(),
            features=RuntimeFeatures(max_rounds=3),
        )

        result = runtime.run(
            run_id="run_final_text",
            system_prompt="Use tools and finish.",
            user_prompt="inspect milestone",
            domain_state=state,
        )

        self.assertEqual(result.final_text, "")

    def test_repeated_identical_call_stops_as_no_progress(self):
        repeated = _tool_call("InspectionTool", {"scope": "same"})
        provider = _ScriptedProvider([[repeated], [repeated]])
        calls = []
        runtime = create_agent_runtime(
            planner=FunctionCallingPlanner(provider),
            tools=[_tool_schema("InspectionTool")],
            invoke_tool=lambda call: calls.append(call) or {"ok": True, "summary": "unchanged"},
            completion_gate=_CompletionGate(),
            features=RuntimeFeatures(max_rounds=8, duplicate_call_limit=2),
        )

        result = runtime.run(
            run_id="run_no_progress",
            system_prompt="Use tools.",
            user_prompt="inspect milestone",
            domain_state={},
        )

        self.assertEqual(result.stop_reason, "no_progress")
        self.assertEqual(len(calls), 1)
        self.assertFalse(result.completed)

    def test_round_budget_is_deterministic(self):
        provider = _ScriptedProvider([
            [_tool_call("InspectionTool", {"attempt": 1})],
            [_tool_call("InspectionTool", {"attempt": 2})],
            [_tool_call("InspectionTool", {"attempt": 3})],
        ])
        runtime = create_agent_runtime(
            planner=FunctionCallingPlanner(provider),
            tools=[_tool_schema("InspectionTool")],
            invoke_tool=lambda call: {"ok": True, "summary": "still incomplete"},
            completion_gate=_CompletionGate(),
            features=RuntimeFeatures(max_rounds=3, duplicate_call_limit=9),
        )

        result = runtime.run(
            run_id="run_budget",
            system_prompt="Use tools.",
            user_prompt="inspect milestone",
            domain_state={},
        )

        self.assertEqual(result.stop_reason, "max_rounds_reached")
        self.assertEqual(result.model_turn_count, 3)
        self.assertFalse(result.completed)

    def test_tool_surfaces_are_explicit_runtime_contracts(self):
        self.assertEqual(ToolSurface.CONVERSATION.value, "conversation")
        self.assertEqual(ToolSurface.INSPECTION.value, "inspection")
        self.assertEqual(ToolSurface.MCP.value, "mcp")


if __name__ == "__main__":
    unittest.main()
