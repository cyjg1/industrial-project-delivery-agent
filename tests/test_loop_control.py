import unittest
from types import SimpleNamespace

from agent.inspection_agent import InspectionCompletionGate
from agent.schemas import EvidenceVerificationResult


class InspectionCompletionGateTest(unittest.TestCase):
    def test_requires_report_and_verification(self):
        decision = InspectionCompletionGate().evaluate(
            SimpleNamespace(report=None, verification=None, tool_failures={})
        )

        self.assertFalse(decision.complete)
        self.assertEqual(decision.missing, ["report", "verification"])

    def test_failed_verification_is_not_done(self):
        decision = InspectionCompletionGate().evaluate(
            SimpleNamespace(
                report=object(),
                verification=EvidenceVerificationResult(
                    ok=False,
                    checked_count=1,
                    errors=["missing evidence"],
                ),
                tool_failures={},
            )
        )

        self.assertFalse(decision.complete)
        self.assertIn("verification_passed", decision.missing)
        self.assertEqual(decision.errors, ["missing evidence"])

    def test_unrecovered_tool_failure_is_not_done(self):
        decision = InspectionCompletionGate().evaluate(
            SimpleNamespace(
                report=object(),
                verification=EvidenceVerificationResult(
                    ok=True,
                    checked_count=1,
                    errors=[],
                ),
                tool_failures={"ExtractionTool": "provider failed"},
            )
        )

        self.assertFalse(decision.complete)
        self.assertIn("failed_tools_recovered", decision.missing)
        self.assertIn("ExtractionTool: provider failed", decision.errors)

    def test_verified_state_with_no_tool_failures_is_done(self):
        decision = InspectionCompletionGate().evaluate(
            SimpleNamespace(
                report=object(),
                verification=EvidenceVerificationResult(
                    ok=True,
                    checked_count=1,
                    errors=[],
                ),
                tool_failures={},
            )
        )

        self.assertTrue(decision.complete)
        self.assertEqual(decision.reason, "verified_project_output")


if __name__ == "__main__":
    unittest.main()
