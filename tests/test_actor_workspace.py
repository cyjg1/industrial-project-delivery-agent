import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent.access_policy import AccessContext, User
from agent.presentation import _build_milestone_workspace, _build_people_workspace, build_human_review_workspace
from agent.schemas import CandidateStatus, EvidenceRef, InspectionItem, PeopleStructure, Person, WorkItem, WorkItemStatus
from backend.main import create_app
from store.sqlite_store import ProjectSQLiteStore
from tests.milestone_fixtures import s3_material_entry_milestone


def _seed_access(store: ProjectSQLiteStore) -> None:
    store.upsert_org("org_mvp", "MVP Org")
    store.upsert_user("u_pm", "org_mvp", "Project Manager")
    store.upsert_user("u_exec", "org_mvp", "Executor")
    store.upsert_user("u_pmo", "org_mvp", "PMO")
    store.upsert_project("project_mvp", "org_mvp", "MVP Project", "u_pm")
    store.upsert_project_member("project_mvp", "u_pm", "pm")
    store.upsert_project_member("project_mvp", "u_exec", "exec")
    store.upsert_project_member("project_mvp", "u_pmo", "pmo")
    store.upsert_topic("topic_a", "project_mvp", "Topic A", "u_pm")
    store.upsert_topic_member("topic_a", "u_pm")
    store.upsert_topic_member("topic_a", "u_exec")
    store.upsert_topic("topic_b", "project_mvp", "Topic B", "u_pm")
    store.upsert_topic_member("topic_b", "u_pm")
    store.upsert_topic_member("topic_b", "u_pmo")


def _candidate(
    item_id: str,
    title: str,
    *,
    owner: str = "Executor",
    topic_id: str | None = "topic_a",
    author_id: str = "u_pm",
    sensitivity: str = "l1",
    category: str = "tasks",
    org_id: str = "org_mvp",
    project_id: str = "project_mvp",
) -> InspectionItem:
    return InspectionItem(
        item_id=item_id,
        category=category,
        title=title,
        description=title,
        evidence_refs=[
            EvidenceRef(
                source_doc_id=f"source_{item_id}",
                source_kind="manual",
                locator="test",
                quote=title,
                evidence_level="human_confirmed",
            )
        ],
        status=CandidateStatus.CONFIRMED,
        owner_candidates=[owner],
        due_date="2026-07-01",
        deliverable=title,
        acceptance_criteria="验收",
        org_id=org_id,
        project_id=project_id,
        topic_id=topic_id,
        author_id=author_id,
        sensitivity=sensitivity,
        tag_origin="actor_ingest",
    )


class ActorWorkspaceTest(unittest.TestCase):
    def test_workspace_role_is_resolved_for_the_active_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            actor = User(id="u_multi", org_id="org_mvp", name="Multi-project user")
            context = AccessContext(project_roles={
                (actor.id, "project_mvp"): "exec",
                (actor.id, "project_pm"): "pm",
            })

            payload = build_human_review_workspace(
                run=None,
                store=store,
                manifest=[],
                milestone=s3_material_entry_milestone(),
                project_id="project_mvp",
                actor=actor,
                access_context=context,
            )

        self.assertEqual(payload["access"]["role"], "exec")
        self.assertFalse(payload["access"]["capabilities"]["progress_dashboard"])
        self.assertFalse(payload["access"]["capabilities"]["debug_context"])

    def test_milestone_workspace_caps_formal_and_candidate_rows_together(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            work_items = [
                WorkItem(
                    work_item_id=f"work_{index:03d}",
                    title=f"正式任务 {index:03d}",
                    description="",
                    status=WorkItemStatus.OPEN,
                    evidence_refs=[],
                )
                for index in range(240)
            ]
            candidates = [
                _candidate(f"candidate_{index:03d}", f"候选任务 {index:03d}")
                for index in range(240)
            ]

            payload = _build_milestone_workspace(
                None,
                store,
                s3_material_entry_milestone(),
                work_items=work_items,
                candidate_items=candidates,
            )

        self.assertEqual(len(payload["rows"]), 241)
        self.assertEqual(payload["summary"]["formal_task_count"], 240)
        self.assertEqual(payload["summary"]["candidate_task_count"], 0)

    def test_people_workspace_preserves_source_tree_group(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            people = PeopleStructure(
                root_title="项目团队",
                groups=[],
                scenarios=[],
                people=[Person("原料成员", "1-原料组", "实施", "项目 / 1-原料组 / 原料成员")],
            )

            payload = _build_people_workspace(
                None,
                store,
                people_asset=people,
                work_items=[],
                candidate_items=[],
            )

        self.assertEqual(payload["people"][0]["group"], "1-原料组")
        self.assertEqual(payload["people"][0]["taxonomy_group"], "原料")
        self.assertEqual(payload["summary"]["group_count"], 1)

    def test_people_workspace_places_topic_lead_first_in_business_board(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            people = PeopleStructure(
                root_title="项目团队",
                groups=[],
                scenarios=[],
                people=[
                    Person("实施乙", "数采", "实施人员", "项目 / 数采 / 实施乙"),
                    Person("负责人甲", "数采", "专题负责人", "项目 / 数采 / 负责人甲"),
                    Person("实施甲", "数采", "实施人员", "项目 / 数采 / 实施甲"),
                ],
            )

            payload = _build_people_workspace(
                None,
                store,
                people_asset=people,
                work_items=[],
                candidate_items=[],
            )

        self.assertEqual(payload["summary"]["group_count"], 1)
        self.assertEqual(payload["people"][0]["role"], "专题负责人")
        self.assertEqual(payload["people"][0]["name"], "负责人甲")

    def test_daily_risk_uses_explicit_task_state_not_title_keywords(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed_access(store)
            keyword_only = _candidate(
                "candidate_keyword_only",
                "风险复盘材料已按计划完成",
                owner="Project Manager",
                topic_id="topic_a",
            )
            missing_progress = _candidate(
                "candidate_missing_progress",
                "接口联调",
                owner="Project Manager",
                topic_id="topic_a",
            )
            for item in (keyword_only, missing_progress):
                item.due_date = "2099-12-31"
                store.save_item(item)
            keyword_work = store.publish_item_as_work_item(keyword_only.item_id, editor="u_pm")
            missing_progress_work = store.publish_item_as_work_item(missing_progress.item_id, editor="u_pm")
            store.update_work_item_fields(
                keyword_work.work_item_id,
                progress_percent=80,
                status="in_progress",
                editor="u_pm",
            )
            store.update_work_item_fields(
                missing_progress_work.work_item_id,
                progress_percent=None,
                status="in_progress",
                editor="u_pm",
            )
            client = TestClient(create_app(store_dir=tmpdir))

            response = client.get("/api/workspace", headers={"X-Actor-Id": "u_pm"})

        self.assertEqual(response.status_code, 200)
        risk_titles = {
            row["title"]
            for row in response.json()["daily_report_workspace"]["risk_items"]
        }
        self.assertEqual(risk_titles, {"接口联调"})

    def test_daily_workspace_focus_and_tasks_are_role_scoped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed_access(store)
            store.upsert_user("u_professional", "org_mvp", "Technical Lead")
            store.upsert_user("u_topic_lead", "org_mvp", "Topic Owner")
            store.upsert_project_member("project_mvp", "u_professional", "professional_lead")
            store.upsert_project_member("project_mvp", "u_topic_lead", "topic_lead")
            store.upsert_topic_member("topic_a", "u_professional")
            store.upsert_topic_member("topic_a", "u_topic_lead")
            own_task = _candidate("candidate_exec_own", "Executor 今日开发任务", owner="Executor", topic_id="topic_a")
            topic_task = _candidate("candidate_topic_manage", "板块接口协调", owner="Topic Owner", topic_id="topic_a")
            project_task = _candidate("candidate_project_manage", "跨板块资源协调", owner="PMO", topic_id="topic_b")
            for item in (own_task, topic_task, project_task):
                store.save_item(item)
                store.publish_item_as_work_item(item.item_id, editor="u_pm")
            client = TestClient(create_app(store_dir=tmpdir))

            payloads = {
                actor: client.get("/api/workspace", headers={"X-Actor-Id": actor}).json()
                for actor in ("u_pm", "u_professional", "u_topic_lead", "u_exec")
            }

        self.assertEqual(payloads["u_pm"]["daily_report_workspace"]["title"], "项目管理日报总览")
        self.assertEqual(payloads["u_professional"]["daily_report_workspace"]["title"], "专业统筹日报洞察")
        self.assertEqual(payloads["u_topic_lead"]["daily_report_workspace"]["title"], "板块/专题日报检查")
        self.assertEqual(payloads["u_exec"]["daily_report_workspace"]["title"], "我的日报提醒")
        self.assertEqual(payloads["u_exec"]["daily_report_workspace"]["summary"]["role_missing_progress_count"], 1)
        self.assertTrue(payloads["u_exec"]["daily_report_workspace"]["risk_items"][0]["missing_progress"])
        self.assertTrue(any("实际完成百分比" in action for action in payloads["u_exec"]["daily_report_workspace"]["recommended_actions"]))
        exec_titles = {item["title"] for item in payloads["u_exec"]["task_pool"]["work_items"]}
        topic_titles = {item["title"] for item in payloads["u_topic_lead"]["task_pool"]["work_items"]}
        pm_titles = {item["title"] for item in payloads["u_pm"]["task_pool"]["work_items"]}
        self.assertEqual(exec_titles, {"Executor 今日开发任务"})
        self.assertEqual(topic_titles, {"Executor 今日开发任务", "板块接口协调"})
        self.assertEqual(pm_titles, {"Executor 今日开发任务", "板块接口协调", "跨板块资源协调"})
        self.assertEqual(payloads["u_topic_lead"]["progress_dashboard"]["summary"]["task_count"], 2)
        self.assertEqual(payloads["u_professional"]["progress_dashboard"]["summary"]["task_count"], 2)
        self.assertEqual(_value_paths(payloads["u_topic_lead"], "跨板块资源协调"), [])
        self.assertEqual(_value_paths(payloads["u_professional"], "跨板块资源协调"), [])
        self.assertTrue(payloads["u_topic_lead"]["access"]["capabilities"]["cross_person_load"])
        self.assertFalse(payloads["u_exec"]["access"]["capabilities"]["progress_dashboard"])
        self.assertFalse(payloads["u_exec"]["access"]["capabilities"]["cross_person_load"])
        self.assertEqual(payloads["u_exec"]["progress_dashboard"]["boards"], [])
        self.assertEqual(payloads["u_exec"]["milestone_control"]["adjustment_suggestions"], [])

    def test_progress_dashboard_aggregates_full_visible_set_before_table_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed_access(store)
            for index in range(241):
                item = _candidate(
                    f"candidate_scale_{index:03d}",
                    f"规模任务 {index:03d}",
                    owner="Project Manager",
                    topic_id="topic_a",
                )
                store.save_item(item, materialize=False)
                store.publish_item_as_work_item(item.item_id, editor="u_pm")
            client = TestClient(create_app(store_dir=tmpdir))

            response = client.get("/api/workspace", headers={"X-Actor-Id": "u_pm"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["progress_dashboard"]["summary"]["task_count"], 241)
        self.assertEqual(payload["daily_report_workspace"]["summary"]["project_active_work_count"], 241)
        self.assertEqual(payload["daily_report_workspace"]["summary"]["role_active_work_count"], 241)
        self.assertEqual(payload["task_pool"]["summary"]["total"], 240)
        self.assertEqual(payload["milestone_workspace"]["summary"]["formal_task_count"], 240)
        self.assertEqual(len(payload["milestone_workspace"]["rows"]), 241)

    def test_workspace_requires_explicit_actor_header(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = TestClient(create_app(store_dir=tmpdir))

            response = client.get("/api/workspace")

        self.assertEqual(response.status_code, 401)
        self.assertIn("X-Actor-Id", response.json()["detail"])

    def test_exec_workspace_response_excludes_pm_only_and_invisible_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed_access(store)
            exec_candidate = _candidate("candidate_exec_task", "Executor 可见任务", owner="Executor", topic_id="topic_a")
            hidden_candidate = _candidate(
                "candidate_topic_b_l3",
                "topic_b 管理层结论",
                owner="PMO",
                topic_id="topic_b",
                sensitivity="l3",
            )
            store.save_item(exec_candidate)
            store.save_item(hidden_candidate)
            store.publish_item_as_work_item(exec_candidate.item_id, editor="u_pm")
            store.publish_item_as_work_item(hidden_candidate.item_id, editor="u_pm")
            client = TestClient(create_app(store_dir=tmpdir))

            pm_payload = client.get("/api/workspace", headers={"X-Actor-Id": "u_pm"}).json()
            exec_response = client.get("/api/workspace", headers={"X-Actor-Id": "u_exec"})
            exec_payload = exec_response.json()

        self.assertEqual(exec_response.status_code, 200)
        self.assertEqual(pm_payload["access"]["role"], "pm")
        self.assertEqual(exec_payload["access"]["role"], "exec")
        self.assertTrue(pm_payload["access"]["capabilities"]["cross_person_load"])
        self.assertFalse(exec_payload["access"]["capabilities"]["cross_person_load"])
        self.assertGreater(pm_payload["task_pool"]["summary"]["total"], exec_payload["task_pool"]["summary"]["total"])
        self.assertEqual([item["work_item_id"] for item in exec_payload["task_pool"]["work_items"]], ["work_candidate_exec_task"])
        self.assertEqual(exec_payload["people_workspace"]["people"], [])
        self.assertEqual(exec_payload["team_profiles"], {})
        self.assertEqual(exec_payload["impact_analysis"]["suggestions"], [])
        raw_exec_body = json.dumps(exec_payload, ensure_ascii=False)
        self.assertNotIn("candidate_topic_b_l3", raw_exec_body)
        self.assertNotIn("topic_b 管理层结论", raw_exec_body)

    def test_workspace_and_weekly_review_do_not_mix_another_projects_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed_access(store)
            store.upsert_org("org_other", "Other Org")
            store.upsert_user("u_other", "org_other", "Other Owner")
            store.upsert_project("project_other", "org_other", "Other Project", "u_other")
            store.upsert_project_member("project_other", "u_other", "pm")
            hidden = _candidate(
                "candidate_other_project",
                "OTHER_PROJECT_SECRET_TASK",
                owner="Other Owner",
                topic_id=None,
                author_id="u_other",
                org_id="org_other",
                project_id="project_other",
            )
            store.save_item(hidden)
            store.publish_item_as_work_item(hidden.item_id, editor="u_other")
            client = TestClient(create_app(store_dir=tmpdir))

            with patch.dict(os.environ, {"LLM_PROVIDER": "mock"}, clear=False):
                workspace = client.get("/api/workspace", headers={"X-Actor-Id": "u_pm"})
                weekly = client.post(
                    "/api/weekly-review/generate",
                    headers={"X-Actor-Id": "u_pm"},
                    json={"week_end": "2026-07-10"},
                )

        self.assertEqual(workspace.status_code, 200)
        self.assertEqual(weekly.status_code, 200)
        self.assertEqual(workspace.json()["people_workspace"]["summary"]["people_count"], 0)
        self.assertNotIn("OTHER_PROJECT_SECRET_TASK", json.dumps(workspace.json(), ensure_ascii=False))
        self.assertNotIn("OTHER_PROJECT_SECRET_TASK", weekly.json()["weekly_review"]["content_markdown"])
        weekly_source = weekly.json()["weekly_review"]["source"]
        self.assertEqual(weekly_source["project_id"], "project_mvp")
        self.assertEqual(weekly_source["org_id"], "org_mvp")

def _value_paths(value, target: str, path: str = "$") -> list[str]:
    matches: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            matches.extend(_value_paths(child, target, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            matches.extend(_value_paths(child, target, f"{path}[{index}]"))
    elif target in str(value):
        matches.append(path)
    return matches


if __name__ == "__main__":
    unittest.main()
