import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.milestones import default_project_milestone
from agent.project_config import initialize_project_workspace, load_project_config
from agent.project_context import load_project_context
from store.sqlite_store import ProjectSQLiteStore


class ProjectConfigTest(unittest.TestCase):
    def test_default_project_milestone_is_generic_when_no_config_file_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_config = Path(tmpdir) / "missing_project_config.json"
            with patch.dict(os.environ, {"PROJECT_AGENT_CONFIG_PATH": str(missing_config)}, clear=False):
                milestone = default_project_milestone()

        self.assertEqual(milestone.project, "通用项目交付巡检")
        self.assertEqual(milestone.scenario_id, "GENERAL")
        self.assertEqual(milestone.chain_name, "项目交付链路")
        self.assertNotIn("真实客户", milestone.project)
        self.assertNotIn("S3 铁前", milestone.name)

    def test_project_config_file_overrides_default_milestone(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "project_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project_id": "customer_success_platform",
                        "project_name": "客户成功平台",
                        "default_milestone": {
                            "milestone_id": "cs_platform_launch_202607",
                            "project": "客户成功平台",
                            "name": "客户成功平台上线准备巡检",
                            "date_start": "2026-07-01",
                            "date_end": "2026-07-10",
                            "scenario_id": "CS1",
                            "chain_name": "客户分层 / 数据看板 / 迁移接口",
                            "acceptance_criteria": ["所有候选都必须有证据", "待办必须可执行"],
                            "trigger_policy": {"type": "manual", "schedule": None},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"PROJECT_AGENT_CONFIG_PATH": str(config_path)}, clear=False):
                config = load_project_config()
                milestone = default_project_milestone()

        self.assertEqual(config.project_id, "customer_success_platform")
        self.assertEqual(config.project_name, "客户成功平台")
        self.assertEqual(milestone.milestone_id, "cs_platform_launch_202607")
        self.assertEqual(milestone.scenario_id, "CS1")
        self.assertEqual(milestone.chain_name, "客户分层 / 数据看板 / 迁移接口")

    def test_initialize_project_workspace_writes_sqlite_authority_and_required_runtime_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report = initialize_project_workspace(
                root_dir=Path(tmpdir),
                project_id="customer_success_platform",
                project_name="客户成功平台",
                milestone_id="cs_platform_launch_202607",
                milestone_name="客户成功平台上线准备巡检",
                scenario_id="CS1",
                chain_name="客户分层 / 数据看板 / 迁移接口",
                date_start="2026-07-01",
                date_end="2026-07-10",
            )

            context = load_project_context(
                ProjectSQLiteStore(Path(tmpdir) / "data" / "store"),
                "customer_success_platform",
            )

        self.assertEqual(context.project_name, "客户成功平台")
        self.assertEqual(context.default_milestone.scenario_id, "CS1")
        self.assertIn("#project_configs/customer_success_platform", report["created_paths"]["project_authority"])
        self.assertEqual(Path(report["created_paths"]["store_dir"]).parts[-2:], ("data", "store"))
        self.assertEqual(Path(report["created_paths"]["database_path"]).parts[-3:], ("data", "store", "project.db"))
        self.assertEqual(Path(report["created_paths"]["vault_dir"]).parts[-2:], ("data", "vault"))
        self.assertEqual(Path(report["created_paths"]["meeting_minutes_skill_dir"]).parts[-3:], ("data", "skills", "meeting_minutes"))
        self.assertIn("data/assets/people_structure.json", report["required_inputs"])

if __name__ == "__main__":
    unittest.main()
