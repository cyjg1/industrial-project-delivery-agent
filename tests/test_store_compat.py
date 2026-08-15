import json
import tempfile
import unittest
from pathlib import Path

from tests.milestone_fixtures import s3_material_entry_milestone
from agent.schemas import to_plain
from store.sqlite_store import ProjectSQLiteStore


class StoreCompatTest(unittest.TestCase):
    def test_reads_legacy_agent_run_without_sdk_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            milestone = s3_material_entry_milestone()
            run_id = "run_legacy"
            payload = {
                run_id: {
                    "run_id": run_id,
                    "milestone_id": milestone.milestone_id,
                    "milestone_plan": to_plain(milestone),
                    "objective": milestone.name,
                    "plan": [],
                    "steps": [],
                    "observations": [],
                    "final_report": None,
                    "confirmed_item_ids": [],
                    "created_at": "2026-06-18T00:00:00+00:00",
                }
            }
            Path(tmpdir, "agent_runs.json").write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )

            run = ProjectSQLiteStore(tmpdir).get_run(run_id)

            self.assertEqual(run.status, "completed")
            self.assertEqual(run.runtime_kind, "legacy_local_harness")
            self.assertEqual(run.agent_trace, [])
            self.assertEqual(run.raw_response_count, 0)
            self.assertEqual(run.harness_state, {})
            self.assertEqual(run.input_snapshot, {})
            self.assertEqual(run.loop_rounds, [])
            self.assertEqual(run.stop_reason, "")
            self.assertEqual(run.structured_output, {})


if __name__ == "__main__":
    unittest.main()
