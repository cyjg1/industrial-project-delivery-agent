import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent.milestones import default_project_milestone
from agent.schemas import AgentRun, CandidateStatus, EvidenceRef, InspectionItem, InspectionReport
from backend.main import create_app
from store.sqlite_store import ProjectSQLiteStore


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "business_acceptance"


class BusinessAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store_dir = Path(self.tempdir.name) / "store"
        self.store = ProjectSQLiteStore(self.store_dir)
        self._seed_access()
        self.material = json.loads((FIXTURE_DIR / "project_cases.json").read_text(encoding="utf-8"))
        self._seed_material()
        self.environment = patch.dict(
            os.environ,
            {
                "PROJECT_AGENT_LEGACY_FIXTURE_MODE": "on",
                "PROJECT_AGENT_PEOPLE_ASSET_PATH": str(FIXTURE_DIR / "people_structure.json"),
                "LLM_PROVIDER": "mock",
            },
            clear=False,
        )
        self.environment.start()
        self.client = TestClient(create_app(store_dir=self.store_dir))

    def tearDown(self) -> None:
        self.environment.stop()
        self.tempdir.cleanup()

    def test_dashboard_matches_confirmed_taxonomy_health_and_three_lists(self):
        workspace = self._workspace("u_pmo")
        dashboard = workspace["progress_dashboard"]

        self.assertEqual([row["name"] for row in dashboard["professions"]], [
            "产品/需求设计", "软件开发", "数据与接口", "测试/UAT", "实施上线",
        ])
        self.assertEqual([row["name"] for row in dashboard["boards"]], [
            "原料", "铁区", "炼钢", "热轧", "冷轧", "仓储", "物流", "质量", "能源", "设备",
            "安全", "环保", "生产实绩", "计划", "成本",
        ])
        self.assertEqual(dashboard["summary"]["task_count"], 9)
        self.assertEqual(dashboard["summary"]["unclassified_profession_count"], 1)
        self.assertEqual(dashboard["summary"]["unclassified_board_count"], 1)

        boards = {row["name"]: row for row in dashboard["boards"]}
        self.assertEqual(boards["原料"]["health"], "green")
        self.assertEqual(boards["炼钢"]["health"], "red")
        self.assertEqual(boards["质量"]["health"], "red")
        self.assertEqual(boards["仓储"]["health"], "yellow")
        self.assertEqual(boards["物流"]["health"], "green")
        self.assertEqual(boards["铁区"]["health"], "gray")
        self.assertEqual(boards["炼钢"]["top_risks"][0]["title"], "炼钢生产实绩模块开发")
        self.assertEqual(boards["仓储"]["missing_plan_count"], 1)

        quality_lists = boards["质量"]["three_lists"]
        self.assertEqual(quality_lists["issue_progress"], 0)
        self.assertEqual(quality_lists["task_progress"], 40)
        self.assertEqual(quality_lists["method_progress"], 100)
        self.assertEqual(quality_lists["progress"], 47)
        self.assertEqual(quality_lists["health"], "red")
        self.assertEqual(quality_lists["unlinked_issue_count"], 1)

    def test_four_roles_receive_different_daily_work_and_scoped_overviews(self):
        payloads = {
            actor: self._workspace(actor)
            for actor in ("u_pmo", "u_professional", "u_topic_lead", "u_exec")
        }

        self.assertEqual(payloads["u_pmo"]["access"]["role_profile"]["label"], "项目经理 / PMO / 总体组")
        self.assertEqual(payloads["u_professional"]["access"]["role_profile"]["label"], "专业统筹负责人")
        self.assertEqual(payloads["u_topic_lead"]["access"]["role_profile"]["label"], "专题负责人")
        self.assertEqual(payloads["u_exec"]["access"]["role_profile"]["label"], "实施人员")

        self.assertEqual(payloads["u_pmo"]["daily_report_workspace"]["title"], "项目管理日报总览")
        self.assertEqual(payloads["u_professional"]["daily_report_workspace"]["title"], "专业统筹日报洞察")
        self.assertEqual(payloads["u_topic_lead"]["daily_report_workspace"]["title"], "板块/专题日报检查")
        self.assertEqual(payloads["u_exec"]["daily_report_workspace"]["title"], "我的日报提醒")
        self.assertEqual(len({payloads[actor]["daily_report_workspace"]["focus"] for actor in payloads}), 4)

        self.assertEqual(payloads["u_pmo"]["progress_dashboard"]["summary"]["task_count"], 9)
        self.assertEqual(payloads["u_professional"]["progress_dashboard"]["summary"]["task_count"], 5)
        self.assertEqual(payloads["u_topic_lead"]["progress_dashboard"]["summary"]["task_count"], 5)
        self.assertFalse(payloads["u_exec"]["access"]["capabilities"]["progress_dashboard"])
        self.assertEqual(payloads["u_exec"]["progress_dashboard"]["boards"], [])
        self.assertEqual(payloads["u_exec"]["milestone_control"]["adjustment_suggestions"], [])

        pmo_tasks = {row["title"] for row in payloads["u_pmo"]["task_pool"]["work_items"]}
        topic_tasks = {row["title"] for row in payloads["u_topic_lead"]["task_pool"]["work_items"]}
        exec_tasks = {row["title"] for row in payloads["u_exec"]["task_pool"]["work_items"]}
        self.assertEqual(len(pmo_tasks), 9)
        self.assertIn("原料专题外部输入协调", topic_tasks)
        self.assertNotIn("跨板块资源调配与决策", topic_tasks)
        self.assertNotIn("跨板块资源调配与决策", json.dumps(payloads["u_topic_lead"], ensure_ascii=False))
        self.assertEqual(exec_tasks, {"执行人员今日完成冷轧页面开发"})

    def test_people_board_counts_imported_business_groups(self):
        workspace = self._workspace("u_pmo")
        people = workspace["people_workspace"]
        grouped = {row["name"]: row["taxonomy_group"] for row in people["people"]}
        source_groups = {row["name"]: row["group"] for row in people["people"]}

        self.assertEqual(people["summary"]["people_count"], 8)
        self.assertEqual(people["summary"]["group_count"], 8)
        self.assertEqual(grouped["项目统筹"], "PMO")
        self.assertEqual(grouped["技术总师"], "专业统筹")
        self.assertEqual(grouped["原料负责人"], "原料")
        self.assertEqual(grouped["炼钢开发"], "炼钢")
        self.assertEqual(grouped["归属待确认"], "待归类")
        self.assertEqual(source_groups["归属待确认"], "场景联调组")
        topic_people = self._workspace("u_topic_lead")["people_workspace"]
        topic_names = {row["name"] for row in topic_people["people"]}
        self.assertEqual(topic_names, set(grouped))
        unmapped_topic_people = self._workspace("u_topic_unmapped")["people_workspace"]
        self.assertEqual(unmapped_topic_people["summary"]["people_count"], 8)
        self.assertEqual(self._workspace("u_exec")["people_workspace"]["people"], [])

    def test_people_board_never_falls_back_to_invisible_run_items(self):
        hidden = InspectionItem(
            item_id="hidden_topic_b_run_item",
            category="task",
            title="HIDDEN_TOPIC_B",
            description="仅管理专题可见",
            evidence_refs=[_evidence("hidden_topic_b_run_item", "HIDDEN_TOPIC_B")],
            status=CandidateStatus.CANDIDATE,
            owner_candidates=["归属待确认"],
            topic_id="topic_b",
            author_id="u_pmo",
            sensitivity="l3",
        )
        milestone = default_project_milestone()
        self.store.save_run(AgentRun(
            run_id="run_with_hidden_topic_item",
            milestone_id=milestone.milestone_id,
            milestone_plan=milestone,
            objective="验证运行产物权限",
            plan=[],
            steps=[],
            observations=[],
            final_report=InspectionReport(
                scenario_id=milestone.scenario_id,
                chain_name=milestone.chain_name,
                chain_gaps=[hidden],
                responsibility_gaps=[],
                followup_drafts=[],
            ),
            confirmed_item_ids=[],
            created_at="2099-01-01T00:00:00+08:00",
            input_snapshot={"project_id": "project_mvp"},
        ))

        payload = self._workspace("u_topic_lead")

        self.assertNotIn("HIDDEN_TOPIC_B", json.dumps(payload, ensure_ascii=False))

    def test_frontend_contract_exposes_views_and_keeps_actor_mapping_consistent(self):
        progress_panel = (ROOT / "frontend" / "src" / "panels" / "ProgressDashboard.tsx").read_text(encoding="utf-8")
        people_panel = (ROOT / "frontend" / "src" / "panels" / "People.tsx").read_text(encoding="utf-8")
        main_pane = (ROOT / "frontend" / "src" / "layout" / "MainPane.tsx").read_text(encoding="utf-8")
        icon_rail = (ROOT / "frontend" / "src" / "layout" / "IconRail.tsx").read_text(encoding="utf-8")

        for label in ("按专业", "按板块", "计划明细", "问题关闭", "任务完成", "方法确认"):
            self.assertIn(label, progress_panel)
        for label in ("PMO", "专业统筹", "待归类", "groupPeopleByBusinessBoard"):
            self.assertIn(label, people_panel)
        for actor_switch_contract in ("switchableUsers", "user.user_id", "onActorChange"):
            self.assertIn(actor_switch_contract, main_pane)
        self.assertIn("canViewDashboard", icon_rail)
        self.assertIn("canViewPeople", icon_rail)
        # rail 是唯一主导航：断言它按 ActiveView 提供 progress / people 两个入口，
        # 而不是锁死按钮上的中文标签（标签要塞进 48px 图标按钮，属于设计决策）。
        self.assertIn("ActiveView", icon_rail)
        for view_id in ('"progress"', '"people"'):
            self.assertIn(view_id, icon_rail)

    def _seed_access(self) -> None:
        self.store.upsert_org("org_mvp", "验收组织")
        for user_id, name in (
            ("u_pmo", "项目统筹"),
            ("u_professional", "技术总师"),
            ("u_topic_lead", "专题负责人"),
            ("u_topic_unmapped", "未绑定专题负责人"),
            ("u_exec", "执行人员"),
        ):
            self.store.upsert_user(user_id, "org_mvp", name)
        self.store.upsert_project("project_mvp", "org_mvp", "业务验收项目", "u_pmo")
        self.store.upsert_project_member("project_mvp", "u_pmo", "pmo")
        self.store.upsert_project_member("project_mvp", "u_professional", "professional_lead")
        self.store.upsert_project_member("project_mvp", "u_topic_lead", "topic_lead")
        self.store.upsert_project_member("project_mvp", "u_topic_unmapped", "topic_lead")
        self.store.upsert_project_member("project_mvp", "u_exec", "exec")
        self.store.upsert_topic("topic_a", "project_mvp", "原料专题", "u_topic_lead")
        self.store.upsert_topic_member("topic_a", "u_professional")
        self.store.upsert_topic_member("topic_a", "u_topic_lead")
        self.store.upsert_topic_member("topic_a", "u_exec")
        self.store.upsert_topic("topic_b", "project_mvp", "跨板块管理", "u_pmo")
        self.store.upsert_topic_member("topic_b", "u_pmo")

    def _seed_material(self) -> None:
        today = date.today()
        for row in self.material["tasks"]:
            due_date = _offset_date(today, row["due_offset"])
            candidate = InspectionItem(
                item_id=row["id"],
                category="task",
                title=row["title"],
                description=row["title"],
                evidence_refs=[_evidence(row["id"], row["title"])],
                status=CandidateStatus.CONFIRMED,
                owner_candidates=[row["owner"]] if row["owner"] else [],
                due_date=due_date,
                deliverable=f"{row['title']}交付物",
                acceptance_criteria="结果可验证并由负责人确认",
                topic_id=row["topic"],
                author_id="u_pmo",
                sensitivity="l1",
            )
            self.store.save_item(candidate, materialize=False)
            published = self.store.publish_item_as_work_item(row["id"], editor="u_pmo")
            self.store.update_work_item_fields(
                published.work_item_id,
                status=row["status"],
                professional_id=row["profession"],
                board_id=row["board"],
                planned_start=_offset_date(today, row["start_offset"]),
                progress_percent=row["progress"],
                editor="u_pmo",
            )

        for row in self.material["inspection_items"]:
            item = InspectionItem(
                item_id=row["id"],
                category=row["category"],
                title=row["title"],
                description=row["title"],
                evidence_refs=[_evidence(row["id"], row["title"])],
                status=CandidateStatus(row["status"]),
                owner_candidates=[row["owner"]] if row["owner"] else [],
                due_date=_offset_date(today, row["due_offset"]),
                board_id=row.get("board", ""),
                linked_task_ids=row["linked_tasks"],
                topic_id="topic_a",
                author_id="u_pmo",
                sensitivity="l1",
            )
            self.store.save_item(item, materialize=False)

    def _workspace(self, actor_id: str) -> dict:
        response = self.client.get("/api/workspace", headers={"X-Actor-Id": actor_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


def _offset_date(today: date, days: int | None) -> str | None:
    return (today + timedelta(days=days)).isoformat() if days is not None else None


def _evidence(item_id: str, title: str) -> EvidenceRef:
    return EvidenceRef(
        source_doc_id=f"acceptance_{item_id}",
        source_kind="synthetic_acceptance_fixture",
        locator="case:1",
        quote=title,
        evidence_level="human_confirmed",
    )


if __name__ == "__main__":
    unittest.main()
