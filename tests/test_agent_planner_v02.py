import inspect
import unittest
from pathlib import Path

from agent.runtime import AgentRuntime, FunctionCallingPlanner


class AgentPlannerArchitectureTest(unittest.TestCase):
    def test_active_runtime_has_no_static_project_plan_builder(self):
        root = Path(__file__).resolve().parents[1]

        self.assertFalse((root / "agent" / "planner.py").exists())
        self.assertFalse((root / "agent" / "loop_control.py").exists())
        source = inspect.getsource(AgentRuntime.run_inspection)
        self.assertNotIn("build_project_inspection_plan", source)
        self.assertNotIn("ProjectLoopController", source)
        self.assertIn("InspectionAgentService", source)

    def test_real_planner_is_function_calling_not_custom_json_plan(self):
        source = inspect.getsource(FunctionCallingPlanner.stream_turn)

        self.assertIn("stream_chat", source)
        self.assertIn('_value(event, "tool_calls")', source)
        self.assertNotIn("complete_json", source)

    def test_project_prompt_defines_completion_contract_not_fixed_work_order(self):
        root = Path(__file__).resolve().parents[1]
        prompt = (root / "agent" / "project_delivery_manager.md").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("## 工作顺序", prompt)
        self.assertIn("## 完成标准", prompt)
        self.assertIn("自主选择", prompt)


if __name__ == "__main__":
    unittest.main()
