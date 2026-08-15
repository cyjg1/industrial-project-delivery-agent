import unittest

from agent.milestones import milestone_to_objective
from tests.milestone_fixtures import s3_material_entry_milestone


class MilestonePlanV03Test(unittest.TestCase):
    def test_default_milestone_replaces_freeform_objective(self):
        milestone = s3_material_entry_milestone()

        self.assertEqual(milestone.milestone_id, "rf_uat_s3_material_entry_20260617")
        self.assertEqual(milestone.scenario_id, "S3")
        self.assertEqual(milestone.chain_name, "铁前 / 物料进厂")
        self.assertEqual(milestone.date_start, "2026-05-30")
        self.assertEqual(milestone.date_end, "2026-06-17")
        self.assertEqual(milestone.trigger_policy["type"], "manual")
        self.assertGreaterEqual(len(milestone.acceptance_criteria), 4)

    def test_milestone_generates_runtime_objective(self):
        milestone = s3_material_entry_milestone()

        objective = milestone_to_objective(milestone)

        self.assertIn(milestone.milestone_id, objective)
        self.assertIn(milestone.name, objective)
        self.assertIn("2026-05-30 至 2026-06-17", objective)


if __name__ == "__main__":
    unittest.main()
