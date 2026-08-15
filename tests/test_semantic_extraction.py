import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.schemas import CandidateStatus, SourceDocument, SourceRef
from agent.access_policy import AccessContext, User
from agent.runtime import AgentRuntime
from agent.semantic_extraction import SemanticExtractionError, extract_document_semantically
from agent.tools import _extract_candidates, build_default_registry
from store.sqlite_store import ProjectSQLiteStore
from tests.milestone_fixtures import s3_material_entry_milestone
from tests.test_inspection_agent_runtime import _ScriptedPlanningProvider


class ScriptedJsonProvider:
    name = "scripted"
    model = "test-extraction-model"

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.prompts = []

    def complete_json(self, prompt):
        self.prompts.append(prompt)
        return json.dumps(self.payloads.pop(0), ensure_ascii=False)


class TailAwareJsonProvider:
    name = "tail-aware"
    model = "test-extraction-model"

    def __init__(self):
        self.prompts = []

    def complete_json(self, prompt):
        self.prompts.append(prompt)
        things = []
        if "TAIL_UNIQUE_DECISION" in prompt:
            things.append(
                {
                    "title": "尾部最终决策",
                    "description": "灰度验证通过后再全量上线。",
                    "status": "candidate",
                    "evidence_refs": [
                        {"locator": "line:240", "quote": "TAIL_UNIQUE_DECISION"}
                    ],
                }
            )
        return json.dumps(
            {
                "people": [],
                "things": things,
                "methods": [],
                "tasks": [],
                "issues": [],
            },
            ensure_ascii=False,
        )


def _source() -> SourceDocument:
    return SourceDocument(
        doc_id="meeting_scope_test",
        title="Delivery review",
        meeting_date="2026-07-11",
        topic="Restricted topic",
        curated_source=SourceRef("curated", "note.md", "matched"),
        raw_source=SourceRef("raw", None, "raw_source_pending"),
        tags=[],
        org_id="org_a",
        project_id="project_a",
        topic_id="topic_a",
        author_id="u_author",
        sensitivity="l2",
        tag_origin="actor_ingest",
    )


class SemanticExtractionTest(unittest.TestCase):
    @unittest.skip("legacy test depended on the private project baseline")
    def test_failed_semantic_retry_persists_failed_run_without_candidates(self):
        invalid = {
            "people": [],
            "things": [{
                "title": "Unsupported claim",
                "description": "Not grounded.",
                "status": "candidate",
                "evidence_refs": [{"locator": "line:1", "quote": "invented quote"}],
            }],
            "methods": [],
            "tasks": [],
            "issues": [],
        }
        provider = ScriptedJsonProvider([invalid, invalid])
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            actor = User(id="u_pm", org_id="org_mvp", name="Project Manager")
            runtime = AgentRuntime(
                build_default_registry(store),
                store,
                provider_factory=lambda role: _ScriptedPlanningProvider([
                    "SourceManifestTool",
                    "PeopleAssetTool",
                    "ProjectMemoryTool",
                    "CuratedNotesTool",
                    "ExtractionTool",
                    "InspectionTool",
                    "EvidenceVerifierTool",
                ]),
            )
            with patch.dict(os.environ, {
                "LLM_PROVIDER": "openai_compatible",
                "OPENAI_COMPAT_API_KEY": "test-key",
                "OPENAI_COMPAT_MODEL": "test-main-model",
                "PROJECT_AGENT_LEGACY_FIXTURE_MODE": "on",
            }, clear=False), patch("agent.tools.get_provider", return_value=provider):
                run = runtime.run_inspection(
                    s3_material_entry_milestone(),
                    actor=actor,
                    access_context=AccessContext(project_roles={(actor.id, "project_mvp"): "pm"}),
                    project_id="project_mvp",
                )
            items = store.list_items()

        self.assertEqual(run.status, "failed")
        self.assertIn("SemanticExtractionError", run.error)
        self.assertEqual(len(provider.prompts), 2)
        self.assertEqual(items, [])

    def test_pipeline_uses_semantic_model_provider(self):
        provider = ScriptedJsonProvider([{
            "people": [],
            "things": [{
                "title": "Grounded semantic result",
                "description": "Comes from the model response.",
                "status": "candidate",
                "evidence_refs": [{"locator": "line:1", "quote": "Only grounded source text"}],
            }],
            "methods": [],
            "tasks": [],
            "issues": [],
        }])
        context = SimpleNamespace(
            manifest=[_source()],
            source_texts={"meeting_scope_test": "Only grounded source text."},
            extractions=[],
            model_io_events=[],
        )

        with patch("agent.tools.get_provider", return_value=provider):
            result = _extract_candidates({"context": context})

        self.assertEqual(result["extraction_count"], 1)
        self.assertEqual(context.extractions[0].things[0].title, "Grounded semantic result")
        self.assertEqual(context.model_io_events[0]["model"], "test-extraction-model")

    def test_valid_items_inherit_source_scope_and_only_propose_sensitivity(self):
        provider = ScriptedJsonProvider([{
            "people": [{
                "title": "Owner A",
                "description": "Owner A leads interface closure.",
                "owner_candidates": ["Owner A"],
                "status": "candidate",
                "evidence_refs": [{"locator": "line:1", "quote": "Owner A leads interface closure"}],
            }],
            "things": [],
            "methods": [],
            "tasks": [{
                "title": "Close interface contract",
                "description": "Deliver the interface contract.",
                "status": "candidate",
                "owner_candidates": ["Owner A"],
                "due_date": "2026-07-15",
                "deliverable": "Interface contract",
                "acceptance_criteria": "Fields and retry behavior are reviewed",
                "observed_fields": [
                    "title",
                    "description",
                    "owner_candidates",
                    "due_date",
                    "deliverable",
                    "acceptance_criteria",
                ],
                "proposed_sensitivity": "l3",
                "sensitivity_reason": "Contains a management assessment.",
                "evidence_refs": [{"locator": "line:2", "quote": "management assessment remains private"}],
            }],
            "issues": [],
        }])

        result = extract_document_semantically(
            _source(),
            "Owner A leads interface closure.\nThe management assessment remains private until review.",
            provider=provider,
        )

        items = result.people + result.things + result.methods + result.chain_links + result.questions
        self.assertEqual(result.status, CandidateStatus.CANDIDATE)
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertEqual(item.org_id, "org_a")
            self.assertEqual(item.project_id, "project_a")
            self.assertEqual(item.topic_id, "topic_a")
            self.assertEqual(item.author_id, "u_author")
            self.assertEqual(item.sensitivity, "l2")
            self.assertEqual(item.tag_origin, "source_inherited")
            self.assertEqual(item.evidence_refs[0].source_doc_id, "meeting_scope_test")
        task = result.questions[0]
        self.assertEqual(task.proposed_sensitivity, "l3")
        self.assertTrue(task.sensitivity_reason)

    def test_bad_evidence_retries_once_then_raises_non_retryable_error(self):
        invalid = {
            "people": [],
            "things": [{
                "title": "Unsupported claim",
                "description": "Not in source.",
                "status": "candidate",
                "evidence_refs": [{"locator": "line:1", "quote": "invented quote"}],
            }],
            "methods": [],
            "tasks": [],
            "issues": [],
        }
        provider = ScriptedJsonProvider([invalid, invalid])

        with self.assertRaises(SemanticExtractionError) as raised:
            extract_document_semantically(_source(), "Only grounded source text.", provider=provider)

        self.assertTrue(raised.exception.non_retryable)
        self.assertEqual(len(provider.prompts), 2)
        self.assertIn("validation", provider.prompts[1].lower())

    def test_incomplete_task_contract_retries_then_fails(self):
        incomplete = {
            "people": [],
            "things": [],
            "methods": [],
            "tasks": [{
                "title": "Close interface contract",
                "description": "Deliver the interface contract.",
                "status": "candidate",
                "due_date": "2026-07-15",
                "observed_fields": ["title", "description", "due_date"],
                "evidence_refs": [{
                    "locator": "line:1",
                    "quote": "Only grounded source text",
                }],
            }],
            "issues": [],
        }
        provider = ScriptedJsonProvider([incomplete, incomplete])

        with self.assertRaisesRegex(SemanticExtractionError, "owner_candidates"):
            extract_document_semantically(
                _source(),
                "Only grounded source text.",
                provider=provider,
            )

        self.assertEqual(len(provider.prompts), 2)

    def test_model_cannot_return_confirmed_items(self):
        payload = {
            "people": [],
            "things": [{
                "title": "Premature confirmation",
                "description": "Must stay candidate.",
                "status": "confirmed",
                "evidence_refs": [{"locator": "line:1", "quote": "Only grounded source text"}],
            }],
            "methods": [],
            "tasks": [],
            "issues": [],
        }
        provider = ScriptedJsonProvider([payload, payload])

        with self.assertRaisesRegex(SemanticExtractionError, "candidate"):
            extract_document_semantically(_source(), "Only grounded source text.", provider=provider)

    def test_long_minutes_extracts_tail_without_truncating_source(self):
        lines = []
        for index in range(1, 241):
            marker = ""
            if index == 1:
                marker = " BEGIN_UNIQUE_CONTEXT"
            elif index == 120:
                marker = " MIDDLE_UNIQUE_CONTEXT"
            elif index == 240:
                marker = " TAIL_UNIQUE_DECISION"
            lines.append(f"第{index:03d}行会议纪要，记录责任、交付物和验收口径。{marker}")
        content = "\n".join(lines)
        provider = TailAwareJsonProvider()

        with patch.dict(os.environ, {"SEMANTIC_EXTRACTION_MAX_CHARS": "1000"}, clear=False):
            result = extract_document_semantically(
                _source(),
                content,
                provider=provider,
                ingestion_job_id="ingest_tail_test",
            )

        self.assertGreater(len(provider.prompts), 1)
        self.assertTrue(any("BEGIN_UNIQUE_CONTEXT" in prompt for prompt in provider.prompts))
        self.assertTrue(any("MIDDLE_UNIQUE_CONTEXT" in prompt for prompt in provider.prompts))
        self.assertTrue(any("TAIL_UNIQUE_DECISION" in prompt for prompt in provider.prompts))
        self.assertEqual(len(result.things), 1)
        self.assertEqual(result.things[0].title, "尾部最终决策")
        self.assertEqual(result.things[0].evidence_refs[0].locator, "line:240")
        self.assertEqual(
            result.things[0].evidence_refs[0].ingestion_job_id,
            "ingest_tail_test",
        )


if __name__ == "__main__":
    unittest.main()
