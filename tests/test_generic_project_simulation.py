import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.access_policy import AccessContext, User
from agent.milestones import MilestonePlan
from agent.runtime import AgentRuntime
from agent.tools import build_default_registry
from ingestion.source_manifest import build_default_manifest, register_uploaded_meeting_note
from store.sqlite_store import ProjectSQLiteStore


class GenericProjectSimulationTest(unittest.TestCase):
    @unittest.skip("legacy simulation assumed an internal mock model provider")
    def test_uploaded_generic_project_note_does_not_use_a_private_project_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            upload_root = Path(tmpdir) / "uploads"
            store = ProjectSQLiteStore(str(Path(tmpdir) / "store"))
            milestone = MilestonePlan(
                milestone_id="cs_platform_launch_202607",
                project="客户成功平台",
                name="客户成功平台上线准备巡检",
                date_start="2026-07-01",
                date_end="2026-07-10",
                scenario_id="CS1",
                chain_name="客户分层 / 数据看板 / 迁移接口",
                acceptance_criteria=["所有候选都必须有证据", "待办必须可执行"],
                trigger_policy={"type": "manual"},
            )

            with patch.dict(
                os.environ,
                {
                    "LLM_PROVIDER": "mock",
                    "PROJECT_AGENT_UPLOAD_ROOT": str(upload_root),
                    "PROJECT_AGENT_UPLOAD_MANIFEST": str(upload_root / "manifest.json"),
                },
                clear=False,
            ):
                actor = User(id="u_pm", org_id="org_mvp", name="Project Manager")
                tags = {
                    "org_id": "org_mvp",
                    "project_id": "project_mvp",
                    "topic_id": None,
                    "author_id": actor.id,
                    "sensitivity": "l1",
                }
                source = register_uploaded_meeting_note(
                    filename="customer_success_weekly.md",
                    content=(
                        "# 客户成功平台周会\n"
                        "甲方甲负责客户分层规则，甲方乙负责数据看板权限，甲方丙负责迁移接口风险。\n"
                        "当前缺口是迁移接口只确认字段名称，没有确认来源系统、失败重试策略和验收样例。\n"
                        "本次方法是用真实客户数据样例验证分层结果，没有样例的数据项不得进入上线确认。\n"
                        "下一步请甲方丁补充验收口径，产品和数据团队共同确认。"
                    ).encode("utf-8"),
                    title="客户成功平台周会",
                    meeting_date="2026-07-02",
                    topic="客户成功平台上线准备",
                    access_tags=tags,
                )
                store.ingest(source, tags=tags, actor=actor, kind="minutes")
                run = AgentRuntime(
                    tool_registry=build_default_registry(store=store),
                    store=store,
                ).run_inspection(
                    milestone,
                    actor=actor,
                    access_context=AccessContext(project_roles={(actor.id, "project_mvp"): "pm"}),
                    project_id="project_mvp",
                )
                manifest = build_default_manifest(date_start="2026-07-01", date_end="2026-07-10")

        self.assertEqual(run.milestone_plan.project, "客户成功平台")
        self.assertEqual(run.final_report.scenario_id, "CS1")
        self.assertEqual(run.final_report.chain_name, "客户分层 / 数据看板 / 迁移接口")
        self.assertEqual([source.title for source in manifest], ["客户成功平台周会"])
        titles = [
            item.title
            for item in (
                run.final_report.chain_gaps
                + run.final_report.responsibility_gaps
                + run.final_report.followup_drafts
            )
        ]
        self.assertTrue(any("客户成功平台周会" in title or "客户分层规则" in title for title in titles))
        self.assertTrue(any("迁移接口" in title for title in titles))
        self.assertTrue(any("验收口径" in title or "验收样例" in title for title in titles))
        self.assertFalse(any("真实客户" in title for title in titles))
        owners = {
            owner
            for item in run.final_report.followup_drafts
            for owner in (item.owner_candidates or [])
        }
        self.assertTrue({"甲方甲", "甲方乙", "甲方丙", "甲方丁"}.issubset(owners))
        self.assertFalse({"项目周会", "没有"}.intersection(owners))


if __name__ == "__main__":
    unittest.main()
