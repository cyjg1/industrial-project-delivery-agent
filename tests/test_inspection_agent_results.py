import unittest
from types import SimpleNamespace

from agent.inspection_agent.results import build_project_output
from agent.schemas import CandidateItem, EvidenceRef, ExtractionResult


def _candidate(item_id, category):
    return CandidateItem(
        item_id=item_id,
        category=category,
        title=f"{category} candidate",
        description="Only evidence-backed source content is present.",
        evidence_refs=[
            EvidenceRef(
                source_doc_id="source_1",
                source_kind="curated_source",
                locator="line:1",
                quote="Only evidence-backed source content is present.",
            )
        ],
    )


class InspectionAgentResultsTest(unittest.TestCase):
    def test_real_model_path_does_not_fill_missing_method_or_task_fields_with_templates(self):
        method = _candidate("method_1", "methods")
        task = _candidate("task_1", "task")
        context = SimpleNamespace(
            extractions=[
                ExtractionResult(
                    source_doc_id="source_1",
                    meeting_date="2026-07-22",
                    title="source",
                    people=[],
                    things=[],
                    methods=[method],
                    chain_links=[],
                    questions=[task],
                )
            ],
            report=object(),
            verification=object(),
            stop_reason="model_completed",
        )

        output = build_project_output(
            context,
            summary="done",
        )

        self.assertIsNone(output.methods[0].business_goal)
        self.assertIsNone(output.methods[0].reasoning_chain)
        self.assertIsNone(output.tasks[0].due_date)
        self.assertIsNone(output.tasks[0].deliverable)
        self.assertEqual(output.tasks[0].item_id, "task_1")

    def test_non_task_questions_are_not_promoted_to_tasks_on_real_path(self):
        question = _candidate("question_1", "questions")
        context = SimpleNamespace(
            extractions=[
                ExtractionResult(
                    source_doc_id="source_1",
                    meeting_date="2026-07-22",
                    title="source",
                    people=[],
                    things=[],
                    methods=[],
                    chain_links=[],
                    questions=[question],
                )
            ],
            report=object(),
            verification=object(),
            stop_reason="model_completed",
        )

        output = build_project_output(
            context,
            summary="done",
        )

        self.assertEqual(output.tasks, [])
        self.assertEqual([item.item_id for item in output.open_questions], ["question_1"])


if __name__ == "__main__":
    unittest.main()
