import unittest

from agent.role_views import role_view_key, role_view_profile, role_view_prompt


class RoleViewTest(unittest.TestCase):
    def test_pm_and_pmo_share_the_same_project_management_view(self):
        pm_profile = role_view_profile("pm")
        pmo_profile = role_view_profile("pmo")

        self.assertEqual(role_view_key("pm"), "pmo")
        self.assertEqual(pm_profile, pmo_profile)
        self.assertEqual(pm_profile.label, "项目经理 / PMO / 总体组")
        self.assertIn("人力与资源调配", pm_profile.principle)
        self.assertEqual(role_view_prompt("pm"), role_view_prompt("pmo"))


if __name__ == "__main__":
    unittest.main()
