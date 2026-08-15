import json
import os
import re
import tempfile
import unittest
from unittest.mock import patch

from agent.access_policy import AccessContext, User
from agent.llm_provider import ChatStreamEvent, ChatToolCall
from agent.inspection_agent import InspectionCompletionGate
from agent.inspection_agent.results import validate_project_output
from agent.runtime import AgentRuntime, RuntimeFeatures
from agent.schemas import CandidateStatus, EvidenceRef
from agent.tool_registry import INSPECTION_SURFACE, ToolSpec
from agent.tools import build_default_registry
from store.sqlite_store import ProjectSQLiteStore
from tests.milestone_fixtures import s3_material_entry_milestone


class _ScriptedPlanningProvider:
    name = "scripted_planning_provider"
    model = "planning-test-model"

    def __init__(self, tool_names):
        calls = [
            (entry[0], dict(entry[1]))
            if isinstance(entry, tuple)
            else (entry, {})
            for entry in tool_names
        ]
        self.turns = [
            [
                ChatStreamEvent(
                    kind="tool_calls",
                    tool_calls=[
                        ChatToolCall(
                            call_id=f"call_{index:02d}",
                            name=name,
                            arguments=arguments,
                            reason=f"model chose {name} for the current state",
                        )
                    ],
                )
            ]
            for index, (name, arguments) in enumerate(calls, start=1)
        ]
        self.turns.append([ChatStreamEvent(kind="delta", text="verified work is complete")])
        self.requests = []

    def stream_chat(self, messages, tools=None):
        self.requests.append({"messages": list(messages), "tools": list(tools or [])})
        if not self.turns:
            raise AssertionError("unexpected planning turn")
        yield from self.turns.pop(0)


class _ScriptedExtractionProvider:
    name = "scripted_extraction_provider"
    model = "extraction-test-model"

    def complete_json(self, prompt):
        source_section = prompt.split("NUMBERED SOURCE\n", 1)[-1]
        source_lines = re.findall(r"(?m)^(\d+):[ \t]+(.+)$", source_section)
        if not source_lines:
            return json.dumps(
                {"people": [], "things": [], "methods": [], "tasks": [], "issues": []}
            )
        line_number, quote = source_lines[0]
        return json.dumps(
            {
                "people": [],
                "things": [],
                "methods": [],
                "tasks": [
                    {
                        "title": "核查会议行动项",
                        "description": "核查原文中的行动项并形成结果。",
                        "status": "candidate",
                        "owner_candidates": ["Project Manager"],
                        "due_date": "2026-06-17",
                        "deliverable": "行动项核查结果",
                        "acceptance_criteria": "核查结果逐项有结论",
                        "observed_fields": ["title", "description"],
                        "proposed_fields": [
                            "owner_candidates",
                            "due_date",
                            "deliverable",
                            "acceptance_criteria",
                        ],
                        "inference_basis": ["测试用可控模型响应，用于验证待确认字段边界。"],
                        "inference_confidence": "low",
                        "evidence_refs": [
                            {"locator": f"line:{line_number}", "quote": quote}
                        ],
                    }
                ],
                "issues": [],
            },
            ensure_ascii=False,
        )


def _complete_planning_provider():
    return _ScriptedPlanningProvider(
        [
            "SourceManifestTool",
            "PeopleAssetTool",
            "CuratedNotesTool",
            "ExtractionTool",
            "InspectionTool",
            "EvidenceVerifierTool",
        ]
    )


class _FailingPlanningProvider:
    name = "failing-real-provider"
    model = "failing-model"

    def stream_chat(self, messages, tools=None):
        raise RuntimeError("planning provider unavailable")
        yield


class InspectionAgentRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.fixture_mode = patch.dict(
            os.environ,
            {"PROJECT_AGENT_LEGACY_FIXTURE_MODE": "on"},
            clear=False,
        )
        self.fixture_mode.start()
        self.addCleanup(self.fixture_mode.stop)
        self.actor = User(id="u_pm", org_id="org_mvp", name="Project Manager")
        self.access_context = AccessContext(
            project_roles={(self.actor.id, "project_mvp"): "pm"}
        )

    def _run(self, runtime):
        return runtime.run_inspection(
            s3_material_entry_milestone(),
            actor=self.actor,
            access_context=self.access_context,
            project_id="project_mvp",
        )

    def test_project_plan_is_the_actual_model_selected_tool_sequence(self):
        selected = [
            "SourceManifestTool",
            "PeopleAssetTool",
            "CuratedNotesTool",
            "ExtractionTool",
            "InspectionTool",
            "EvidenceVerifierTool",
        ]
        planning_provider = _ScriptedPlanningProvider(selected)
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            runtime = AgentRuntime(
                tool_registry=build_default_registry(store),
                store=store,
                provider_factory=lambda role: planning_provider,
                features=RuntimeFeatures(max_rounds=8),
            )
            with patch("agent.tools.get_provider", return_value=_ScriptedExtractionProvider()):
                run = self._run(runtime)

            persisted = store.get_run(run.run_id)

        self.assertEqual([step.tool_name for step in run.plan], selected)
        self.assertNotIn("ProjectMemoryTool", [step.tool_name for step in run.plan])
        self.assertEqual(run.runtime_kind, "model_function_calling_inspection")
        self.assertGreaterEqual(run.raw_response_count, len(selected) + 1)
        self.assertGreaterEqual(len(run.model_io_events), len(selected) + 1)
        self.assertEqual(run.stop_reason, "model_completed")
        self.assertEqual(run.status, "completed")
        self.assertEqual([step.tool_name for step in persisted.plan], selected)
        self.assertTrue(
            all(
                "ProjectMemoryTool"
                in {item["function"]["name"] for item in request["tools"]}
                for request in planning_provider.requests
            )
        )

    def test_planning_provider_failure_is_persisted_without_candidate_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            runtime = AgentRuntime(
                tool_registry=build_default_registry(store),
                store=store,
                provider_factory=lambda role: _FailingPlanningProvider(),
            )

            run = self._run(runtime)

            self.assertEqual(run.status, "failed")
            self.assertEqual(run.stop_reason, "runtime_failed")
            self.assertIn("planning provider unavailable", run.error)
            self.assertEqual(store.list_items(), [])
            self.assertEqual(store.latest_run().run_id, run.run_id)

    def test_failed_tool_can_be_retried_and_recovered_by_a_later_model_turn(self):
        selected = [
            "SourceManifestTool",
            "PeopleAssetTool",
            "CuratedNotesTool",
            "ExtractionTool",
            ("ExtractionTool", {"retry": True}),
            "InspectionTool",
            "EvidenceVerifierTool",
        ]
        provider = _ScriptedPlanningProvider(selected)
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            registry = build_default_registry(store)
            original = next(
                spec for spec in registry.specs() if spec.name == "ExtractionTool"
            )
            attempts = []

            def flaky_extraction(actor, ctx, args):
                attempts.append(len(attempts) + 1)
                if len(attempts) == 1:
                    raise RuntimeError("temporary extraction failure")
                return original.handler(actor, ctx, args)

            registry.register(
                ToolSpec(
                    name="ExtractionTool",
                    description=original.description,
                    parameters=original.parameters,
                    handler=flaky_extraction,
                    required_role=original.required_role,
                    surfaces=frozenset({INSPECTION_SURFACE}),
                    access_filter_keys=original.access_filter_keys,
                )
            )
            runtime = AgentRuntime(
                tool_registry=registry,
                store=store,
                provider_factory=lambda role: provider,
                features=RuntimeFeatures(max_rounds=8),
            )
            with patch("agent.tools.get_provider", return_value=_ScriptedExtractionProvider()):
                run = self._run(runtime)

        self.assertEqual(attempts, [1, 2])
        self.assertEqual(run.status, "completed")
        extraction_steps = [
            task
            for task in run.harness_state["tasks"]
            if task["tool_name"] == "ExtractionTool"
        ]
        self.assertEqual(
            [task["status"] for task in extraction_steps],
            ["failed", "completed"],
        )

    def test_unknown_model_tool_call_is_structurally_rejected(self):
        provider = _ScriptedPlanningProvider(["HiddenAdminTool"])
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            runtime = AgentRuntime(
                tool_registry=build_default_registry(store),
                store=store,
                provider_factory=lambda role: provider,
                features=RuntimeFeatures(max_rounds=2),
            )
            run = self._run(runtime)

        self.assertEqual(run.status, "failed")
        self.assertIn("HiddenAdminTool", run.error)
        self.assertIn("工具不可用或权限不足", str(run.harness_state))

    def test_completion_gate_rejects_report_without_verification(self):
        state = type(
            "State",
            (),
            {"report": object(), "verification": None, "tool_failures": {}},
        )()

        decision = InspectionCompletionGate().evaluate(state)

        self.assertFalse(decision.complete)
        self.assertEqual(decision.missing, ["verification"])

    def test_model_io_records_real_calls_without_fabricated_ids_or_usage(self):
        selected = [
            "SourceManifestTool",
            "PeopleAssetTool",
            "CuratedNotesTool",
            "ExtractionTool",
            "InspectionTool",
            "EvidenceVerifierTool",
        ]
        provider = _ScriptedPlanningProvider(selected)
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            runtime = AgentRuntime(
                tool_registry=build_default_registry(store),
                store=store,
                provider_factory=lambda role: provider,
            )
            with patch("agent.tools.get_provider", return_value=_ScriptedExtractionProvider()):
                run = self._run(runtime)

        first_response = run.model_io_events[0]["responses"][0]
        self.assertTrue(run.model_io_events[0]["api_called"])
        self.assertEqual(first_response["raw_response_id"], "")
        self.assertEqual(first_response["token_usage"], {})
        self.assertEqual(first_response["tool_calls"][0]["name"], selected[0])

    @unittest.skip("legacy fixture was removed from the public release")
    def test_output_validation_rejects_non_candidate_report_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            runtime = AgentRuntime(
                tool_registry=build_default_registry(store),
                store=store,
                provider_factory=lambda role: _complete_planning_provider(),
            )
            with patch("agent.tools.get_provider", return_value=_ScriptedExtractionProvider()):
                run = self._run(runtime)
            candidate = next(item for item in store.list_items() if item.category == "task")
            candidate.status = CandidateStatus.CONFIRMED

            errors = validate_project_output(
                type("Output", (), {
                    "report": run.final_report,
                    "verification": run.verification,
                    "people": [],
                    "things": [],
                    "methods": [],
                    "tasks": [candidate],
                    "open_questions": [],
                })(),
                store.list_sources(),
                allowed_roots=(store.root_dir,),
            )

        self.assertTrue(any("status must remain candidate" in error for error in errors))

    @unittest.skip("legacy fixture was removed from the public release")
    def test_output_validation_rejects_unknown_evidence_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            runtime = AgentRuntime(
                tool_registry=build_default_registry(store),
                store=store,
                provider_factory=lambda role: _complete_planning_provider(),
            )
            with patch("agent.tools.get_provider", return_value=_ScriptedExtractionProvider()):
                run = self._run(runtime)
            candidate = next(item for item in store.list_items() if item.category == "task")
            candidate.evidence_refs = [
                EvidenceRef(
                    source_doc_id="unknown_source",
                    source_kind="curated_source",
                    locator="line:1",
                    quote="unsupported",
                )
            ]

            errors = validate_project_output(
                type("Output", (), {
                    "report": run.final_report,
                    "verification": run.verification,
                    "people": [],
                    "things": [],
                    "methods": [],
                    "tasks": [candidate],
                    "open_questions": [],
                })(),
                store.list_sources(),
                allowed_roots=(store.root_dir,),
            )

        self.assertTrue(any("references unknown source" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
