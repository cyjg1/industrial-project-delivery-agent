from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent.access_policy import User
from agent.conversation.run_manager import ConversationRunManager
from agent.memory_skill_evolution import (
    MemorySkillEvolutionService,
    classify_episode_relation,
)
from backend.main import create_app
from scripts.evaluate_memory_skill_ab import compare_reports
from store.sqlite_store import ProjectSQLiteStore


class MemorySkillEvolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = ProjectSQLiteStore(self.tmpdir.name)
        self.store.upsert_org("org_mvp", "MVP Org")
        self.store.upsert_user("u_pm", "org_mvp", "Project Manager")
        self.store.upsert_user("u_exec", "org_mvp", "Executive")
        self.store.upsert_user("u_outside", "org_mvp", "Outside")
        self.store.upsert_project(
            "project_mvp",
            "org_mvp",
            "MVP Project",
            "u_pm",
        )
        self.store.upsert_project_member("project_mvp", "u_pm", "pm")
        self.store.upsert_project_member("project_mvp", "u_exec", "exec")
        self.pm = User("u_pm", "org_mvp", "Project Manager")
        self.exec = User("u_exec", "org_mvp", "Executive")
        self.outside = User("u_outside", "org_mvp", "Outside")
        self.pm_ctx = self.store.access_context_for_actor(self.pm.id)
        self.exec_ctx = self.store.access_context_for_actor(self.exec.id)
        self.outside_ctx = self.store.access_context_for_actor(self.outside.id)
        self.service = MemorySkillEvolutionService(self.store)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_episode_boundary_trace_redaction_and_correction_backfill(self) -> None:
        first = self._complete_run(
            "run_first",
            session_id="session_shared",
            message="检查接口，api_key=sk-abcdefghijklmnop",
            tools=["search_memory"],
        )
        second = self._complete_run(
            "run_second",
            session_id="session_shared",
            message="不对，纠正上面的接口结论",
            tools=["search_memory"],
        )

        first_episode = self.store.memory_skill_evolution.get_episode_by_run(
            first["episode"]["run_id"]
        )
        first_traces = self.store.memory_skill_evolution.list_traces(
            project_id="project_mvp",
            episode_id=first_episode["episode_id"],
        )

        self.assertEqual(first_episode["relation"], "new_task")
        self.assertEqual(second["episode"]["relation"], "correction")
        self.assertEqual(first_episode["feedback_status"], "corrected")
        self.assertEqual(first_episode["reward_source"], "human")
        self.assertEqual(first_episode["reward_value"], -0.75)
        self.assertEqual(first_traces[-1]["value"], -0.75)
        self.assertIn("[REDACTED]", first_traces[0]["state_summary"])
        self.assertNotIn("sk-abcdefghijklmnop", str(first_episode))
        self.assertTrue(
            first_traces[0]["payload"]["hidden_chain_of_thought_persisted"]
            is False
        )

    def test_two_distinct_episodes_create_only_a_candidate_policy(self) -> None:
        self._complete_run(
            "run_policy_1",
            session_id="session_1",
            message="查询项目风险",
            tools=["search_memory", "get_project_health"],
        )
        self._complete_run(
            "run_policy_2",
            session_id="session_2",
            message="复核当前风险",
            tools=["search_memory", "get_project_health"],
        )

        policies = self.store.memory_skill_evolution.list_policies(
            project_id="project_mvp"
        )
        self.assertEqual(len(policies), 1)
        self.assertEqual(policies[0]["status"], "candidate")
        self.assertEqual(len(policies[0]["support_episode_ids"]), 2)
        self.assertTrue(policies[0]["verification"])
        self.assertTrue(policies[0]["boundaries"])

        result = self.service.retrieve_supplement(
            "search_memory",
            actor=self.pm,
            ctx=self.pm_ctx,
            project_id="project_mvp",
            project_skill_count=0,
        )
        self.assertEqual(result["policy_items"], [])
        self.assertTrue(result["trace_items"])
        self.assertEqual(
            result["retrieval_trace"]["fallback_reason"],
            "no_approved_higher_tier_match",
        )

        approved = self.service.approve_policy(
            policies[0]["policy_id"],
            actor=self.pm,
            ctx=self.pm_ctx,
        )
        self.assertEqual(approved["status"], "approved")
        result = self.service.retrieve_supplement(
            "search_memory",
            actor=self.pm,
            ctx=self.pm_ctx,
            project_id="project_mvp",
            project_skill_count=0,
        )
        self.assertEqual(len(result["policy_items"]), 1)
        self.assertEqual(result["trace_items"], [])

    def test_l3_cognition_requires_two_human_approved_policies(self) -> None:
        for index in (1, 2):
            self._complete_run(
                f"run_memory_{index}",
                session_id=f"memory_{index}",
                message="查询会议证据",
                tools=["search_memory"],
            )
            self._complete_run(
                f"run_tasks_{index}",
                session_id=f"tasks_{index}",
                message="查询到期任务",
                tools=["get_tasks"],
            )
        policies = self.store.memory_skill_evolution.list_policies(
            project_id="project_mvp"
        )
        self.assertEqual(len(policies), 2)
        self.assertEqual(
            self.store.memory_skill_evolution.list_cognitions(
                project_id="project_mvp"
            )[0]["status"],
            "candidate",
        )

        with self.assertRaisesRegex(ValueError, "支撑策略"):
            cognition = self.store.memory_skill_evolution.list_cognitions(
                project_id="project_mvp"
            )[0]
            self.service.approve_cognition(
                cognition["cognition_id"],
                actor=self.pm,
                ctx=self.pm_ctx,
            )

        for policy in policies:
            self.service.approve_policy(
                policy["policy_id"],
                actor=self.pm,
                ctx=self.pm_ctx,
            )
        cognition = self.store.memory_skill_evolution.list_cognitions(
            project_id="project_mvp"
        )[0]
        approved = self.service.approve_cognition(
            cognition["cognition_id"],
            actor=self.pm,
            ctx=self.pm_ctx,
        )
        self.assertEqual(approved["status"], "approved")
        self.assertTrue(approved["payload"]["declarative_only"])
        self.assertFalse(approved["payload"]["contains_imperative_instructions"])

        retired = self.service.retire_policy(
            policies[0]["policy_id"],
            actor=self.pm,
            ctx=self.pm_ctx,
        )
        cognition = self.store.memory_skill_evolution.get_cognition(
            cognition["cognition_id"]
        )
        self.assertEqual(retired["status"], "retired")
        self.assertEqual(cognition["status"], "revalidation_required")
        snapshot = self.service.snapshot(
            actor=self.pm,
            ctx=self.pm_ctx,
            project_id="project_mvp",
        )
        self.assertEqual(
            snapshot["summary"]["cognition_revalidation_count"],
            1,
        )
        with self.assertRaisesRegex(ValueError, "不能重新批准"):
            self.service.approve_policy(
                policies[0]["policy_id"],
                actor=self.pm,
                ctx=self.pm_ctx,
            )

    def test_skill_reliability_needs_three_distinct_successful_episodes(self) -> None:
        row = {
            "skill_id": "skill_delivery",
            "version_id": "skill_delivery_v1",
            "org_id": "org_mvp",
            "project_id": "project_mvp",
            "topic_id": None,
            "sensitivity": "l2",
            "name": "Delivery",
            "retrieval_score": 1.0,
        }
        for index in range(1, 4):
            run_id = f"skill_run_{index}"
            self.store.memory_skill_evolution.record_skill_retrievals(
                run_id=run_id,
                query="delivery",
                rows=[row],
                actor_id="u_pm",
            )
            self.store.memory_skill_evolution.finalize_skill_retrievals(
                run_id=run_id,
                episode_id=f"episode_{index}",
                outcome="success",
                reward_value=1.0,
            )
            reliability = self.store.memory_skill_evolution.skill_reliability(
                "skill_delivery",
                "skill_delivery_v1",
            )
            if index < 3:
                self.assertEqual(reliability["lifecycle"], "probationary")
        self.assertEqual(reliability["lifecycle"], "active")
        self.assertEqual(reliability["episode_count"], 3)
        self.assertGreaterEqual(reliability["reliability"], 0.6)

        version_two = {**row, "version_id": "skill_delivery_v2"}
        self.store.memory_skill_evolution.record_skill_retrievals(
            run_id="skill_run_v2",
            query="delivery",
            rows=[version_two],
            actor_id="u_pm",
        )
        self.store.memory_skill_evolution.finalize_skill_retrievals(
            run_id="skill_run_v2",
            episode_id="episode_v2",
            outcome="success",
            reward_value=1.0,
        )
        version_one_reliability = (
            self.store.memory_skill_evolution.skill_reliability(
                "skill_delivery",
                "skill_delivery_v1",
            )
        )
        version_two_reliability = (
            self.store.memory_skill_evolution.skill_reliability(
                "skill_delivery",
                "skill_delivery_v2",
            )
        )
        self.assertEqual(version_one_reliability["retrieval_count"], 3)
        self.assertEqual(version_two_reliability["retrieval_count"], 1)

    def test_human_correction_revalidates_an_approved_policy(self) -> None:
        first = self._complete_run(
            "run_revalidation_1",
            session_id="revalidation_1",
            message="查询项目风险",
            tools=["search_memory", "get_project_health"],
        )
        self._complete_run(
            "run_revalidation_2",
            session_id="revalidation_2",
            message="复核项目风险",
            tools=["search_memory", "get_project_health"],
        )
        policy = self.store.memory_skill_evolution.list_policies(
            project_id="project_mvp"
        )[0]
        self.service.approve_policy(
            policy["policy_id"],
            actor=self.pm,
            ctx=self.pm_ctx,
        )

        self.service.feedback_episode(
            first["episode"]["episode_id"],
            feedback_status="corrected",
            feedback_note="人工确认该次执行经验不可复用",
            actor=self.pm,
            ctx=self.pm_ctx,
        )

        revalidated = self.store.memory_skill_evolution.get_policy(
            policy["policy_id"]
        )
        self.assertEqual(revalidated["status"], "revalidation_required")

    def test_hierarchical_retrieval_stops_at_project_skill_and_mode_can_be_off(self) -> None:
        for index in (1, 2):
            self._complete_run(
                f"run_hierarchy_{index}",
                session_id=f"hierarchy_{index}",
                message="查询风险",
                tools=["search_memory"],
            )
        policy = self.store.memory_skill_evolution.list_policies(
            project_id="project_mvp"
        )[0]
        self.service.approve_policy(
            policy["policy_id"],
            actor=self.pm,
            ctx=self.pm_ctx,
        )

        result = self.service.retrieve_supplement(
            "search_memory",
            actor=self.pm,
            ctx=self.pm_ctx,
            project_id="project_mvp",
            project_skill_count=1,
        )
        self.assertEqual(result["policy_items"], [])
        self.assertEqual(
            result["retrieval_trace"]["fallback_reason"],
            "published_project_skill_hit",
        )
        with patch.dict(os.environ, {"MEMORY_SKILL_RETRIEVAL_MODE": "off"}):
            disabled = self.service.retrieve_supplement(
                "search_memory",
                actor=self.pm,
                ctx=self.pm_ctx,
                project_id="project_mvp",
                project_skill_count=0,
            )
        self.assertEqual(disabled["policy_items"], [])
        self.assertEqual(
            disabled["retrieval_trace"]["fallback_reason"],
            "memory_skill_retrieval_disabled",
        )

    def test_access_tags_filter_snapshot_and_manager_role_is_required(self) -> None:
        self._complete_run(
            "run_access",
            session_id="access",
            message="检查项目",
            tools=["search_memory"],
        )
        visible_snapshot = self.service.snapshot(
            actor=self.exec,
            ctx=self.exec_ctx,
            project_id="project_mvp",
        )
        hidden_snapshot = self.service.snapshot(
            actor=self.outside,
            ctx=self.outside_ctx,
            project_id="project_mvp",
        )
        self.assertEqual(visible_snapshot["summary"]["episode_count"], 1)
        self.assertEqual(hidden_snapshot["summary"]["episode_count"], 0)

    def test_relation_classifier_is_transparent_and_deterministic(self) -> None:
        self.assertEqual(
            classify_episode_relation("start", has_previous=False),
            "new_task",
        )
        self.assertEqual(
            classify_episode_relation("不对，改成第二种", has_previous=True),
            "correction",
        )
        self.assertEqual(
            classify_episode_relation(
                "继续查询项目风险",
                previous_message="查询项目风险",
                has_previous=True,
            ),
            "follow_up",
        )

    def test_ab_report_compares_machine_checks_without_auto_passing_semantics(self) -> None:
        off = {
            "results": [
                {
                    "case_id": "Q01",
                    "actor_id": "u_pm",
                    "machine_passed": False,
                    "round_count": 3,
                    "actual_tools": ["search_memory"],
                }
            ]
        }
        governed = {
            "results": [
                {
                    "case_id": "Q01",
                    "actor_id": "u_pm",
                    "machine_passed": True,
                    "round_count": 2,
                    "actual_tools": ["search_project_skills"],
                    "memory_skill_retrieval": {
                        "mode": "governed",
                        "policy_hits": 1,
                    },
                }
            ]
        }

        comparison = compare_reports(off, governed)

        self.assertEqual(comparison[0]["machine_pass_delta"], 1)
        self.assertEqual(comparison[0]["round_delta"], -1)
        self.assertTrue(comparison[0]["memory_treatment_exposed"])
        self.assertFalse(comparison[0]["causal_attribution_allowed"])
        self.assertTrue(comparison[0]["requires_human_semantic_review"])
        self.assertEqual(
            comparison[0]["governed"]["retrieval_trace"]["policy_hits"],
            1,
        )

    @unittest.skip("legacy test assumed a repository-provided conversation model")
    def test_conversation_run_manager_closes_memory_episode_as_sidecar(self) -> None:
        def event_source(**kwargs):
            yield {
                "event": "final",
                "data": {
                    "session_id": kwargs["session_id"],
                    "message_id": "message_final",
                    "reply": "verified",
                    "verification": {
                        "checked": True,
                        "unsupported": [],
                        "invalid_fact_ids": [],
                    },
                    "tool_steps": [],
                    "workspace": {},
                    "debug": {"rounds": []},
                },
            }

        manager = ConversationRunManager(
            store_factory=lambda: ProjectSQLiteStore(self.tmpdir.name),
            runtime_factory=lambda active_store: object(),
            milestone_loader=lambda active_store, project_id: object(),
            evolution_service_factory=MemorySkillEvolutionService,
            event_source=event_source,
            max_workers=1,
        )
        try:
            submitted = manager.submit(
                actor=self.pm,
                access_context=self.pm_ctx,
                project_id="project_mvp",
                view="overview",
                message="record one governed episode",
                client_request_id="memory-sidecar",
            )
            completed = manager.wait(submitted["run_id"], timeout=3)
            active_store = ProjectSQLiteStore(self.tmpdir.name)
            episode = active_store.memory_skill_evolution.get_episode_by_run(
                submitted["run_id"]
            )
            event_types = [
                row["event_type"]
                for row in active_store.runtime_runs.list_events(submitted["run_id"])
            ]
        finally:
            manager.shutdown()

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(episode["reward_source"], "deterministic_verifier")
        self.assertIn("memory_evolution", event_types)

    def test_memory_skill_governance_api_exposes_and_approves_candidate(self) -> None:
        for index in (1, 2):
            self._complete_run(
                f"run_api_{index}",
                session_id=f"api_{index}",
                message="查询风险",
                tools=["search_memory"],
            )
        policy = self.store.memory_skill_evolution.list_policies(
            project_id="project_mvp"
        )[0]
        with patch.dict(
            os.environ,
            {
                "INGESTION_WORKER_ENABLED": "off",
                "INSPECTION_WORKER_ENABLED": "off",
                "MEMORY_EMBEDDING": "off",
            },
            clear=False,
        ):
            client = TestClient(create_app(store_dir=self.tmpdir.name))
            client.headers.update({"X-Actor-Id": "u_pm"})
            snapshot = client.get("/api/memory-skill-evolution")
            approved = client.post(
                f"/api/memory-skill-evolution/policies/{policy['policy_id']}/approve",
                json={},
            )

        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["summary"]["policy_candidate_count"], 1)
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["policy"]["status"], "approved")

    def _complete_run(
        self,
        run_id: str,
        *,
        session_id: str,
        message: str,
        tools: list[str],
    ) -> dict[str, object]:
        self.store.runtime_runs.create_run(
            run_id=run_id,
            kind="conversation",
            project_id="project_mvp",
            actor_id="u_pm",
            session_id=session_id,
            turn_id=f"turn_{run_id}",
            input_payload={"message": message},
        )
        self.store.runtime_runs.mark_running(run_id)
        tool_steps = [
            {
                "round_index": 1,
                "tool_name": name,
                "arguments": {},
                "reason": "observable test action",
                "status": "completed",
                "summary": f"{name} returned evidence",
                "result": {
                    "ok": True,
                    "summary": f"{name} returned evidence",
                },
            }
            for name in tools
        ]
        self.store.runtime_runs.complete_run(
            run_id,
            stop_reason="model_completed",
            output_payload={
                "reply": "verified",
                "verification": {
                    "checked": True,
                    "unsupported": [],
                    "invalid_fact_ids": [],
                },
                "tool_steps": tool_steps,
                "debug": {
                    "rounds": [
                        {
                            "round_index": 1,
                            "tool_calls": [
                                {"name": name, "arguments": {}}
                                for name in tools
                            ],
                            "tool_results": [
                                {
                                    "name": name,
                                    "summary": f"{name} returned evidence",
                                }
                                for name in tools
                            ],
                            "verification_errors": [],
                            "status": "completed",
                        }
                    ]
                },
            },
        )
        return self.service.close_runtime_run(run_id)


if __name__ == "__main__":
    unittest.main()
