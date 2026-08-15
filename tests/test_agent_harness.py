import unittest

from agent.runtime.harness import AgentHarness, HarnessToolError
from agent.schemas import AgentPlanStep


class AgentHarnessTest(unittest.TestCase):
    def test_harness_accepts_model_selected_steps_at_runtime(self):
        harness = AgentHarness(run_id="run_dynamic", plan=[])
        harness.start()
        step = AgentPlanStep(
            step_id="step_01",
            tool_name="RiskTool",
            reason="selected by model for current risk objective",
        )

        harness.add_step(step)
        harness.start_step(step, input_summary="current state")
        harness.complete_step(step, {"summary": "risk facts loaded"})
        harness.complete(success=True)

        snapshot = harness.snapshot()
        self.assertEqual(snapshot["max_steps"], 1)
        self.assertEqual(snapshot["completed_steps"], 1)
        self.assertEqual(snapshot["tasks"][0]["tool_name"], "RiskTool")
        self.assertEqual(harness.trace[0]["output_summary"], "risk facts loaded")

    def test_harness_runs_step_records_events_hooks_and_context_window(self):
        plan = [
            AgentPlanStep(
                step_id="step_01",
                tool_name="BigTool",
                reason="exercise context compaction",
            )
        ]
        harness = AgentHarness(run_id="run_test", plan=plan, max_active_chars=80)
        harness.start()

        def big_tool(context):
            context["called"] = True
            return {"summary": "big result", "payload": "x" * 200}

        result = harness.run_step(
            step=plan[0],
            tool=big_tool,
            context={},
            input_summary="test input",
        )
        harness.complete()
        snapshot = harness.snapshot()

        self.assertEqual(result["summary"], "big result")
        self.assertEqual(snapshot["phase"], "completed")
        self.assertEqual(snapshot["completed_steps"], 1)
        self.assertEqual(snapshot["context_window"]["archived_count"], 1)
        self.assertTrue(any(event["event_type"] == "step_completed" for event in snapshot["events"]))
        self.assertTrue(any(hook["hook_type"] == "context_compaction" for hook in snapshot["hooks"]))
        self.assertEqual(harness.trace[0]["context_ref"], "ctx_01_step_01")

    def test_harness_records_structured_error_after_retries(self):
        plan = [
            AgentPlanStep(
                step_id="step_01",
                tool_name="FailingTool",
                reason="exercise structured error handling",
            )
        ]
        harness = AgentHarness(run_id="run_test", plan=plan, max_tool_attempts=2)
        harness.start()

        def failing_tool(_context):
            raise ValueError("boom")

        with self.assertRaises(HarnessToolError):
            harness.run_step(
                step=plan[0],
                tool=failing_tool,
                context={},
                input_summary="test input",
            )

        snapshot = harness.snapshot()
        self.assertEqual(snapshot["tasks"][0]["status"], "failed")
        self.assertEqual(snapshot["tasks"][0]["attempts"], 2)
        self.assertGreaterEqual(snapshot["failure_count"], 2)
        self.assertTrue(any(event["event_type"] == "tool_error" for event in snapshot["events"]))


if __name__ == "__main__":
    unittest.main()
