import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent.schemas import CandidateStatus, EvidenceRef, InspectionItem
from backend.main import create_app
from store.sqlite_store import ProjectSQLiteStore


def _seed_access(store: ProjectSQLiteStore) -> None:
    store.upsert_org("org_mvp", "MVP Org")
    store.upsert_user("u_pm", "org_mvp", "Project Manager")
    store.upsert_user("u_exec", "org_mvp", "Executor")
    store.upsert_user("u_pmo", "org_mvp", "PMO")
    store.upsert_project("project_mvp", "org_mvp", "MVP Project", "u_pm")
    store.upsert_project_member("project_mvp", "u_pm", "pm")
    store.upsert_project_member("project_mvp", "u_exec", "exec")
    store.upsert_project_member("project_mvp", "u_pmo", "pmo")
    store.upsert_topic("topic_private", "project_mvp", "Management", "u_pm")
    store.upsert_topic_member("topic_private", "u_pm")
    store.upsert_topic_member("topic_private", "u_pmo")


def _private_item() -> InspectionItem:
    return InspectionItem(
        item_id="private_management_item",
        category="issue",
        title="Management-only issue",
        description="Restricted project issue",
        evidence_refs=[EvidenceRef("source_private", "manual", "line:1", "restricted")],
        status=CandidateStatus.CANDIDATE,
        org_id="org_mvp",
        project_id="project_mvp",
        topic_id="topic_private",
        author_id="u_pm",
        sensitivity="l3",
    )


class ApiAccessScopeTest(unittest.TestCase):
    def test_missing_actor_is_rejected_before_conversation_uses_default_pm(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed_access(store)
            client = TestClient(create_app(store_dir=tmpdir))

            response = client.post("/api/conversation", json={"message": "show all tasks"})

        self.assertEqual(response.status_code, 401)
        self.assertIn("X-Actor-Id", response.json()["detail"])

    def test_unknown_actor_cannot_confirm_item_and_state_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed_access(store)
            store.save_item(_private_item())
            client = TestClient(create_app(store_dir=tmpdir))

            response = client.post(
                "/api/items/private_management_item/confirm",
                headers={"X-Actor-Id": "u_unknown"},
                json={"editor": "u_unknown", "notes": "should not write"},
            )
            item = store.get_item("private_management_item")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(item.status, CandidateStatus.CANDIDATE)

    def test_exec_cannot_confirm_hidden_management_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed_access(store)
            store.save_item(_private_item())
            client = TestClient(create_app(store_dir=tmpdir))

            response = client.post(
                "/api/items/private_management_item/confirm",
                headers={"X-Actor-Id": "u_exec"},
                json={"editor": "u_exec", "notes": "should not write"},
            )
            item = store.get_item("private_management_item")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(item.status, CandidateStatus.CANDIDATE)

    def test_exec_session_list_does_not_return_pm_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed_access(store)
            pm_session = store.ensure_session(title="PM private session", project_id="project_mvp")
            client = TestClient(create_app(store_dir=tmpdir))

            response = client.get(
                "/api/conversation/sessions",
                headers={"X-Actor-Id": "u_exec"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(pm_session["session_id"], {row["session_id"] for row in response.json()["sessions"]})

    def test_exec_cannot_read_or_generate_management_brief(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed_access(store)
            client = TestClient(create_app(store_dir=tmpdir))

            latest = client.get("/api/brief/latest", headers={"X-Actor-Id": "u_exec"})
            generated = client.post(
                "/api/brief/generate",
                headers={"X-Actor-Id": "u_exec"},
                json={"push_feishu": False},
            )

        self.assertEqual(latest.status_code, 403)
        self.assertEqual(generated.status_code, 403)

    def test_exec_workspace_read_does_not_generate_management_brief_as_side_effect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed_access(store)
            client = TestClient(create_app(store_dir=tmpdir))

            with patch.dict(os.environ, {"LLM_PROVIDER": "mock"}, clear=False):
                response = client.get("/api/workspace", headers={"X-Actor-Id": "u_exec"})

            briefs = [source for source in store.list_sources() if source["kind"] == "brief"]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(briefs, [])

    def test_exec_project_config_request_is_rejected_before_file_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store_dir = root / "store"
            config_path = root / "project_config.json"
            store = ProjectSQLiteStore(store_dir)
            _seed_access(store)
            client = TestClient(create_app(store_dir=store_dir))

            with patch.dict(os.environ, {"PROJECT_AGENT_CONFIG_PATH": str(config_path)}, clear=False):
                response = client.post(
                    "/api/milestones/config",
                    headers={"X-Actor-Id": "u_exec"},
                    json={"name": "unauthorized mutation"},
                )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(config_path.exists())

    def test_followup_comparison_does_not_match_same_title_meeting_from_another_org(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed_access(store)
            store.upsert_org("org_other", "Other Org")
            store.upsert_user("u_other", "org_other", "Other PM")
            store.upsert_project("project_other", "org_other", "Other Project", "u_other")
            store.upsert_project_member("project_other", "u_other", "pm")
            _save_source(
                store,
                "meeting_current",
                "Weekly Delivery Meeting",
                "2026-07-10",
                org_id="org_mvp",
                project_id="project_mvp",
                author_id="u_pm",
            )
            _save_source(
                store,
                "meeting_other_secret",
                "Weekly Delivery Meeting",
                "2026-07-03",
                org_id="org_other",
                project_id="project_other",
                author_id="u_other",
            )
            client = TestClient(create_app(store_dir=tmpdir))

            response = client.get(
                "/api/meetings/meeting_current/followup-comparison",
                headers={"X-Actor-Id": "u_pm"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["comparison"]["previous_source"], {})
        self.assertNotIn("meeting_other_secret", response.text)


def _save_source(
    store: ProjectSQLiteStore,
    source_id: str,
    title: str,
    meeting_date: str,
    *,
    org_id: str,
    project_id: str,
    author_id: str,
) -> None:
    store.save_source(
        {
            "doc_id": source_id,
            "title": title,
            "meeting_date": meeting_date,
            "topic": title,
            "tags": ["test"],
            "curated_source": {"source_type": "test", "path": "", "status": "matched", "notes": ""},
            "raw_source": {"source_type": "test", "path": "", "status": "matched", "notes": ""},
            "org_id": org_id,
            "project_id": project_id,
            "topic_id": None,
            "author_id": author_id,
            "sensitivity": "l1",
            "tag_origin": "test_seed",
        },
        kind="minutes",
    )


if __name__ == "__main__":
    unittest.main()
