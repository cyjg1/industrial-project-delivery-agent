import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent.daily_brief import generate_daily_brief, latest_daily_brief
from agent.project_context import load_project_context
from agent.schemas import AgentRun, EvidenceRef, InspectionItem, MilestonePlan
from backend.main import create_app
from ingestion.source_manifest import build_project_manifest
from store.sqlite_store import ProjectSQLiteStore


def _seed_project(store: ProjectSQLiteStore, project_id: str, name: str, owner_id: str = "u_pm") -> None:
    store.upsert_org("org_mvp", "MVP Org")
    store.upsert_user(owner_id, "org_mvp", "Project Manager")
    store.upsert_project(project_id, "org_mvp", name, owner_id)
    store.upsert_project_member(project_id, owner_id, "pm")


def _milestone(project: str, milestone_id: str, name: str) -> dict:
    return {
        "milestone_id": milestone_id,
        "project": project,
        "name": name,
        "date_start": "2026-07-01",
        "date_end": "2026-07-31",
        "scenario_id": "GENERAL",
        "chain_name": "Delivery",
        "acceptance_criteria": ["evidence required"],
        "trigger_policy": {"type": "manual", "schedule": None},
        "status": "active",
    }


class ProjectContextAuthorityTest(unittest.TestCase):
    def test_conversation_context_uses_current_project_config_instead_of_stale_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed_project(store, "project_mvp", "Current Project")
            store.upsert_project_config(
                "project_mvp",
                org_id="org_mvp",
                source_profile="current",
                milestone_payload=_milestone(
                    "Current Project",
                    "current_milestone",
                    "Current configured milestone",
                ),
                actor_id="u_pm",
            )
            stale_milestone = MilestonePlan(**_milestone(
                "Old Demo Project",
                "stale_run_inspection",
                "Stale run milestone",
            ))
            store.save_run(AgentRun(
                run_id="stale_run",
                milestone_id=stale_milestone.milestone_id,
                milestone_plan=stale_milestone,
                objective="historical run",
                plan=[],
                steps=[],
                observations=[],
                final_report=None,
                confirmed_item_ids=[],
                created_at="2026-07-12T00:00:00+00:00",
                input_snapshot={"project_id": "project_mvp"},
            ))

            context = store.read_context(
                "conversation",
                "当前进度",
                900,
                project_id="project_mvp",
            )

        self.assertIn("Current configured milestone", context["content"])
        self.assertNotIn("Stale run milestone", context["content"])
        self.assertEqual(context["project_id"], "project_mvp")
        self.assertEqual(context["milestone_source"], "project_configs")
        self.assertTrue(context["snapshot_id"].startswith("ctx_"))
        self.assertIn("T", context["as_of"])
        self.assertLessEqual(context["char_count"], context["budget"])

    def test_empty_project_manifest_does_not_fall_back_to_legacy_customer_samples(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed_project(store, "project_empty", "Empty Project")

            manifest = build_project_manifest(store, "project_empty")

        self.assertEqual(manifest, [])

    def test_daily_brief_is_project_scoped_and_stored_with_access_tags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed_project(store, "project_a", "Project A")
            _seed_project(store, "project_b", "Project B", owner_id="u_pm_b")
            context_a = load_project_context(store, "project_a")
            for project_id, owner_id, item_id, title in [
                ("project_a", "u_pm", "item_a", "A-only decision"),
                ("project_b", "u_pm_b", "item_b", "B-only decision"),
            ]:
                store.save_item(InspectionItem(
                    item_id=item_id,
                    category="issue",
                    title=title,
                    description=title,
                    evidence_refs=[EvidenceRef("source", "manual", "line:1", title)],
                    org_id="org_mvp",
                    project_id=project_id,
                    author_id=owner_id,
                    sensitivity="l1",
                ))

            with patch.dict(os.environ, {"LLM_PROVIDER": "mock"}, clear=False):
                brief = generate_daily_brief(
                    store=store,
                    milestone=context_a.default_milestone,
                    project_id="project_a",
                    today=date(2026, 7, 11),
                )
            source = store.get_source(brief["brief_id"])
            latest = latest_daily_brief(store, project_id="project_a")

        self.assertIn("A-only decision", brief["content_markdown"])
        self.assertNotIn("B-only decision", brief["content_markdown"])
        self.assertEqual(source["project_id"], "project_a")
        self.assertEqual(source["org_id"], "org_mvp")
        self.assertEqual(source["author_id"], "u_pm")
        self.assertEqual(source["sensitivity"], "l3")
        self.assertEqual(latest["brief_id"], brief["brief_id"])

    def test_each_project_loads_its_own_sqlite_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed_project(store, "project_a", "Project A")
            _seed_project(store, "project_b", "Project B", owner_id="u_pm_b")
            store.upsert_project_config(
                "project_a",
                org_id="org_mvp",
                source_profile="project_a_profile",
                milestone_payload=_milestone("Project A", "milestone_a", "A release"),
                actor_id="u_pm",
            )
            store.upsert_project_config(
                "project_b",
                org_id="org_mvp",
                source_profile="project_b_profile",
                milestone_payload=_milestone("Project B", "milestone_b", "B release"),
                actor_id="u_pm_b",
            )

            context_a = load_project_context(store, "project_a")
            context_b = load_project_context(store, "project_b")

        self.assertEqual(context_a.project_name, "Project A")
        self.assertEqual(context_a.default_milestone.milestone_id, "milestone_a")
        self.assertEqual(context_b.project_name, "Project B")
        self.assertEqual(context_b.default_milestone.milestone_id, "milestone_b")

    def test_workspace_ignores_conflicting_json_and_uses_sqlite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store_dir = root / "store"
            config_path = root / "project_config.json"
            config_path.write_text(json.dumps({
                "project_id": "wrong_project",
                "project_name": "Wrong file project",
                "default_milestone": _milestone("Wrong file project", "wrong_milestone", "Wrong milestone"),
            }), encoding="utf-8")
            store = ProjectSQLiteStore(store_dir)
            _seed_project(store, "project_mvp", "SQLite Project")
            store.upsert_project_config(
                "project_mvp",
                org_id="org_mvp",
                source_profile="sqlite",
                milestone_payload=_milestone("SQLite Project", "sqlite_milestone", "SQLite milestone"),
                actor_id="u_pm",
            )

            with patch.dict(os.environ, {"PROJECT_AGENT_CONFIG_PATH": str(config_path)}, clear=False):
                client = TestClient(create_app(store_dir=store_dir))
                response = client.get("/api/workspace", headers={"X-Actor-Id": "u_pm"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["milestone_control"]["plan"]["milestone_id"],
            "sqlite_milestone",
        )
        self.assertNotIn("Wrong file project", response.text)

    def test_config_endpoint_updates_sqlite_without_writing_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store_dir = root / "store"
            config_path = root / "must_not_be_written.json"
            store = ProjectSQLiteStore(store_dir)
            _seed_project(store, "project_mvp", "SQLite Project")
            client = TestClient(create_app(store_dir=store_dir))

            with patch.dict(os.environ, {"PROJECT_AGENT_CONFIG_PATH": str(config_path)}, clear=False):
                response = client.post(
                    "/api/milestones/config",
                    headers={"X-Actor-Id": "u_pm"},
                    json={"name": "Database milestone", "date_end": "2026-08-15"},
                )

            context = load_project_context(ProjectSQLiteStore(store_dir), "project_mvp")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(context.default_milestone.name, "Database milestone")
        self.assertEqual(context.default_milestone.date_end, "2026-08-15")
        self.assertFalse(config_path.exists())

    def test_migration_is_dry_run_until_apply_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store_dir = root / "store"
            config_path = root / "project_config.json"
            config_path.write_text(json.dumps({
                "project_id": "project_mvp",
                "project_name": "Migrated Project",
                "source_profile": "legacy_file",
                "default_milestone": _milestone("Migrated Project", "migrated_milestone", "Migrated milestone"),
            }), encoding="utf-8")
            store = ProjectSQLiteStore(store_dir)
            _seed_project(store, "project_mvp", "Before Migration")
            command = [
                sys.executable,
                "scripts/migrate_project_authority.py",
                "--store-dir",
                str(store_dir),
                "--config",
                str(config_path),
            ]

            dry_run = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
            before = store.get_project_config("project_mvp")
            applied = subprocess.run([*command, "--apply"], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
            after = store.get_project_config("project_mvp")

        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertIsNone(before)
        self.assertIn('"mode": "dry_run"', dry_run.stdout)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(after["milestone_payload"]["milestone_id"], "migrated_milestone")
        self.assertEqual(after["migration_origin"], "legacy_project_config")

    def test_migration_requires_explicit_override_for_a_mismatched_legacy_project_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store_dir = root / "store"
            config_path = root / "project_config.json"
            config_path.write_text(json.dumps({
                "project_id": "obsolete_demo_id",
                "project_name": "Obsolete Demo",
                "default_milestone": _milestone("Obsolete Demo", "obsolete_milestone", "Obsolete milestone"),
            }), encoding="utf-8")
            store = ProjectSQLiteStore(store_dir)
            _seed_project(store, "project_mvp", "Before Migration")
            command = [
                sys.executable,
                "scripts/migrate_project_authority.py",
                "--store-dir", str(store_dir),
                "--config", str(config_path),
                "--project-id", "project_mvp",
                "--project-name", "Authoritative Project",
                "--milestone-id", "authoritative_milestone",
                "--milestone-name", "Authoritative milestone",
                "--apply",
            ]

            applied = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
            project = store.get_project("project_mvp")
            context = load_project_context(store, "project_mvp")

        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(project["name"], "Authoritative Project")
        self.assertEqual(context.default_milestone.milestone_id, "authoritative_milestone")
        self.assertEqual(context.default_milestone.name, "Authoritative milestone")


if __name__ == "__main__":
    unittest.main()
