import unittest

from agent.policy import load_agent_policy, render_agent_instructions


class AgentPolicyTest(unittest.TestCase):
    def test_policy_defines_methodology_matter_taxonomy_and_completion(self):
        policy = load_agent_policy()
        prompt = render_agent_instructions()

        self.assertEqual(policy["matter_taxonomy"]["primary"], ["design", "function", "architecture"])
        self.assertIn("reasoning_chain", policy["methodology"]["required_fields"])
        self.assertEqual(
            policy["task_contract"]["required_fields"],
            ["owner", "due_date", "deliverable", "acceptance_criteria"],
        )
        self.assertIn("人工反馈拥有最高优先级", prompt)
        self.assertIn("原始转写是最终核查依据", prompt)
        self.assertIn("不算完成", prompt)


if __name__ == "__main__":
    unittest.main()
