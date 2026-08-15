import tempfile
import unittest
from datetime import date
from pathlib import Path

from agent.schemas import AgentRun, EvidenceVerificationResult, MilestonePlan
from store.sqlite_store import ProjectSQLiteStore


def _milestone() -> MilestonePlan:
    return MilestonePlan(
        milestone_id="m1",
        project="Project A",
        name="Release",
        date_start="2026-07-01",
        date_end="2026-07-11",
        scenario_id="GENERAL",
        chain_name="Delivery",
        acceptance_criteria=["All gates pass"],
        trigger_policy={"type": "manual"},
    )


def _task(task_id: str, status: str, due_date: str, *, owners=None, acceptance: str = "accepted") -> dict:
    return {
        "id": task_id,
        "work_item_id": task_id,
        "org_id": "org_mvp",
        "project_id": "project_mvp",
        "topic_id": None,
        "author_id": "u_pm",
        "sensitivity": "l1",
        "title": task_id,
        "status": status,
        "owners": owners or [],
        "due_date": due_date,
        "deliverable": "deliverable" if acceptance else "",
        "acceptance_criteria": acceptance,
        "evidence": [],
    }


class ProjectHealthTest(unittest.TestCase):
    def test_project_health_calculates_counts_dates_and_data_gaps(self):
        from agent.project_health import build_project_health

        health = build_project_health(
            milestone=_milestone(),
            tasks=[
                _task("open_overdue", "open", "2026-07-10", owners=["Alice"]),
                _task("progress_overdue", "in_progress", "2026-07-12", acceptance=""),
                _task("done_old", "done", "2026-07-01", owners=["Bob"]),
                _task("open_no_due", "open", "", acceptance=""),
            ],
            latest_run=None,
            as_of=date(2026, 7, 13),
        )

        self.assertEqual(health["as_of"], "2026-07-13")
        self.assertEqual(health["task_counts"]["total"], 4)
        self.assertEqual(health["task_counts"]["by_status"], {"done": 1, "in_progress": 1, "open": 2})
        self.assertEqual(health["task_counts"]["completed"], 1)
        self.assertEqual(health["overdue"]["count"], 2)
        self.assertEqual(health["overdue"]["by_status"], {"in_progress": 1, "open": 1})
        self.assertEqual(health["overdue"]["percent"], 50.0)
        self.assertEqual(
            {item["work_item_id"]: item["days_overdue"] for item in health["overdue"]["items"]},
            {"open_overdue": 3, "progress_overdue": 1},
        )
        self.assertEqual(health["data_gaps"]["missing_owner"], 2)
        self.assertEqual(health["data_gaps"]["missing_due_date"], 1)
        self.assertEqual(health["data_gaps"]["missing_acceptance"], 2)
        self.assertTrue(health["milestone"]["overdue"])
        self.assertEqual(health["milestone"]["days_to_end"], -2)
        self.assertEqual(health["milestone"]["days_overdue"], 2)
        self.assertEqual(health["verification"]["status"], "not_run")

    def test_latest_verified_run_ignores_newer_conversation_runs_without_verification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(Path(tmpdir) / "store")
            verified = _run("verified", "2026-07-13T01:00:00+00:00", EvidenceVerificationResult(True, 3, []))
            conversation = _run("conversation", "2026-07-13T02:00:00+00:00", None)
            store.save_run(verified)
            store.save_run(conversation)

            latest = store.latest_verified_run("project_mvp")

        self.assertIsNotNone(latest)
        self.assertEqual(latest.run_id, "verified")


def _run(run_id: str, created_at: str, verification: EvidenceVerificationResult | None) -> AgentRun:
    return AgentRun(
        run_id=run_id,
        milestone_id="m1",
        milestone_plan=_milestone(),
        objective="test",
        plan=[],
        steps=[],
        observations=[],
        final_report=None,
        confirmed_item_ids=[],
        created_at=created_at,
        verification=verification,
        input_snapshot={"project_id": "project_mvp"},
    )


if __name__ == "__main__":
    unittest.main()
