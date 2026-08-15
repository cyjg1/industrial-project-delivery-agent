import tempfile
import unittest
from pathlib import Path

from agent.evaluation import (
    actors_for_case,
    evaluate_conversation_result,
    load_agent_evaluation_cases,
)


class AgentEvaluationTest(unittest.TestCase):
    def test_question_set_parser_keeps_evidence_and_rejection_contracts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "questions.md"
            path.write_text(
                "\n".join(
                    [
                        "# Questions",
                        "## 权限边界",
                        "### Q19 执行范围",
                        "- 问题：能看到什么？",
                        "- 检索查询：超期任务",
                        "- 检索意图：检查权限。",
                        "- 预期工具：`get_tasks`、`search_memory`",
                        "- 证据约束：只返回可见数据。",
                        "- 不可接受：泄露隐藏数量。",
                        "- 人工结论：待评审",
                    ]
                ),
                encoding="utf-8",
            )

            cases = load_agent_evaluation_cases(path)

        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["section"], "权限边界")
        self.assertEqual(cases[0]["expected_tools"], ["get_tasks", "search_memory"])
        self.assertEqual(cases[0]["evidence_constraint"], "只返回可见数据。")
        self.assertEqual(cases[0]["unacceptable"], "泄露隐藏数量。")
        self.assertEqual(actors_for_case(cases[0], "u_pm"), ["u_exec"])

    def test_machine_evaluation_checks_tools_evidence_verification_and_rounds(self):
        case = {
            "id": "Q01",
            "question": "项目风险是什么？",
            "expected_tools": ["get_project_health", "search_memory"],
            "evidence_constraint": "每项有来源。",
            "unacceptable": "不得编造。",
        }
        payload = {
            "reply": "当前有一项风险。[F_risk]",
            "stop_reason": "model_completed",
            "verification": {
                "unsupported": [],
                "invalid_fact_ids": [],
                "citation_coverage": {"ratio": 1},
            },
            "tool_steps": [
                {
                    "tool_name": "get_project_health",
                    "status": "completed",
                    "result": {"health": {"risk_count": 1}},
                },
                {
                    "tool_name": "search_memory",
                    "status": "completed",
                    "result": {"items": [{"item_id": "risk_1"}]},
                },
            ],
            "debug": {
                "rounds": [
                    {"round_index": 1, "tool_calls": [{"name": "get_project_health"}]},
                    {"round_index": 2, "tool_calls": [{"name": "search_memory"}]},
                    {"round_index": 3, "model_output": "当前有一项风险。"},
                ]
            },
        }

        result = evaluate_conversation_result(
            case,
            payload,
            actor_id="u_pm",
            max_rounds=8,
        )

        self.assertTrue(result["machine_passed"])
        self.assertFalse(result["semantic_quality_auto_passed"])
        self.assertEqual(result["human_judgement"], "pending")

    def test_missing_tool_or_unsupported_claim_fails_machine_gate(self):
        case = {
            "id": "Q02",
            "question": "上线门禁是什么？",
            "expected_tools": ["search_memory", "get_tasks"],
            "evidence_constraint": "有来源。",
            "unacceptable": "不得编造。",
        }
        payload = {
            "reply": "上线日期是明天。",
            "stop_reason": "model_completed",
            "verification": {
                "unsupported": ["具体日期缺少事实引用"],
                "invalid_fact_ids": [],
            },
            "tool_steps": [
                {
                    "tool_name": "search_memory",
                    "status": "completed",
                    "result": {"items": []},
                }
            ],
            "debug": {"rounds": [{"round_index": 1}]},
        }

        result = evaluate_conversation_result(
            case,
            payload,
            actor_id="u_pm",
            max_rounds=8,
        )

        self.assertFalse(result["machine_passed"])
        failed = {
            check["name"]
            for check in result["checks"]
            if not check["passed"]
        }
        self.assertIn("expected_tool_coverage", failed)
        self.assertIn("answer_verification", failed)
        self.assertIn("evidence_observed", failed)

    def test_mutating_case_without_real_upload_is_skipped(self):
        case = {
            "id": "Q12",
            "question": "处理新增纪要。",
            "expected_tools": ["ingest_file", "propose_candidates"],
            "evidence_constraint": "引用新纪要。",
            "unacceptable": "不得伪造。",
        }

        result = evaluate_conversation_result(
            case,
            None,
            actor_id="u_pm",
            max_rounds=8,
            precondition_error="real upload required",
        )

        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["machine_passed"])

    def test_failed_runtime_keeps_round_trace_for_diagnosis(self):
        case = {
            "id": "Q14",
            "question": "哪些要求已经沉淀为方法？",
            "expected_tools": ["search_memory", "search_methods"],
            "evidence_constraint": "区分状态。",
            "unacceptable": "不得把候选当成确认。",
        }
        payload = {
            "reply": "",
            "runtime_error": "RuntimeError: max elapsed time reached",
            "stop_reason": "max_elapsed_time_reached",
            "verification": {
                "unsupported": [],
                "invalid_fact_ids": [],
            },
            "tool_steps": [{
                "tool_name": "search_memory",
                "status": "completed",
                "result": {"items": [{"item_id": "method_1"}]},
            }],
            "debug": {
                "rounds": [{
                    "round_index": 1,
                    "model_output": "",
                    "tool_calls": [{"name": "search_memory"}],
                }]
            },
        }

        result = evaluate_conversation_result(
            case,
            payload,
            actor_id="u_pm",
            max_rounds=8,
        )

        self.assertEqual(result["status"], "runtime_failed")
        self.assertEqual(result["round_count"], 1)
        self.assertEqual(result["error"], payload["runtime_error"])
        self.assertFalse(result["machine_passed"])

    def test_topic_isolation_case_runs_both_actors(self):
        self.assertEqual(
            actors_for_case({"id": "Q21"}, "u_pmo"),
            ["u_exec", "u_pm"],
        )


if __name__ == "__main__":
    unittest.main()
