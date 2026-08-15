import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent.schemas import CandidateStatus, EvidenceRef, InspectionItem
from backend.main import create_app
from store.sqlite_store import ProjectSQLiteStore


class ProjectSkillDailyJournalApiTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = ProjectSQLiteStore(self.tmpdir.name)
        self.store.upsert_org("org_main", "Main")
        self.store.upsert_user("u_pm", "org_main", "PM")
        self.store.upsert_project("project_main", "org_main", "Project", "u_pm")
        self.store.upsert_project_member("project_main", "u_pm", "pm")
        self.headers = {"X-Actor-Id": "u_pm"}

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_manual_daily_journal_generation_is_visible_in_workspace_and_health(self):
        session = self.store.ensure_session(project_id="project_main", actor_id="u_pm", title="PM")
        self.store.append_session_message(session["session_id"], "user", "今天完成标准层字段核对。")
        report_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()

        provider = type("JournalProvider", (), {
            "name": "scripted-journal",
            "complete_json": lambda self, prompt: '{"entries": []}',
        })()
        with patch("agent.daily_journal.get_provider", return_value=provider):
            with TestClient(create_app(store_dir=self.tmpdir.name)) as client:
                generated = client.post(
                    "/api/daily-journal/generate",
                    headers=self.headers,
                    json={"report_date": report_date},
                )
                workspace = client.get("/api/workspace", headers=self.headers)
                health = client.get("/api/health").json()

        self.assertEqual(generated.status_code, 200)
        self.assertEqual(generated.json()["journal"]["organization_status"], "model_organized")
        self.assertEqual(workspace.json()["daily_journal"]["report_date"], report_date)
        self.assertEqual(health["daily_journal_scheduler"]["hour"], 23)
        self.assertEqual(health["daily_journal_scheduler"]["minute"], 55)
        self.assertEqual(health["daily_journal_scheduler"]["timezone"], "Asia/Shanghai")
        self.assertTrue(health["daily_journal_scheduler"]["next_run_time"])

    def test_skill_compile_and_actor_filtered_list_api(self):
        self.store.save_item(InspectionItem(
            item_id="method_api",
            category="method",
            title="标准层闭环",
            description="形成版本化闭环。",
            evidence_refs=[EvidenceRef("meeting_one", "curated_source", "1", "第一份证据")],
            status=CandidateStatus.CONFIRMED,
            org_id="org_main",
            project_id="project_main",
            author_id="u_pm",
            sensitivity="l1",
        ))

        with TestClient(create_app(store_dir=self.tmpdir.name)) as client:
            compiled = client.post(
                "/api/project-skills/compile",
                headers=self.headers,
                json={"method_id": "method_api"},
            )
            listed = client.get("/api/project-skills", headers=self.headers)

        self.assertEqual(compiled.status_code, 200)
        self.assertFalse(compiled.json()["skill"]["readiness"]["ready"])
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["skills"][0]["source_method_id"], "method_api")


if __name__ == "__main__":
    unittest.main()
