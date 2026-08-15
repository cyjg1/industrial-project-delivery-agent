import unittest
from datetime import date

from agent.progress_dashboard import BOARDS, PROFESSIONS, build_progress_dashboard, classify_person_group
from agent.schemas import (
    CandidateStatus,
    EvidenceRef,
    InspectionItem,
    PeopleStructure,
    Person,
    WorkItem,
    WorkItemStatus,
)


def _task(
    task_id: str,
    title: str,
    *,
    owner: str = "",
    status: WorkItemStatus = WorkItemStatus.IN_PROGRESS,
    due_date: str = "2026-07-20",
    professional_id: str = "",
    board_id: str = "",
    progress: int | None = None,
) -> WorkItem:
    return WorkItem(
        work_item_id=task_id,
        title=title,
        description=title,
        status=status,
        evidence_refs=[],
        owner_candidates=[owner] if owner else [],
        due_date=due_date,
        professional_id=professional_id,
        board_id=board_id,
        planned_start="2026-07-01",
        progress_percent=progress,
        updated_at="2026-07-15T08:00:00+08:00",
    )


class ProgressDashboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.people = PeopleStructure(
            root_title="项目团队",
            groups=[],
            scenarios=[],
            people=[
                Person("张工", "炼钢", "软件开发", "项目 / 炼钢 / 软件开发 / 张工"),
                Person("李工", "质量", "测试负责人", "项目 / 质量 / 测试负责人 / 李工"),
            ],
        )

    def test_dashboard_has_fixed_profession_and_board_taxonomy(self):
        result = build_progress_dashboard([], [], self.people, today=date(2026, 7, 15))

        self.assertEqual([item["id"] for item in result["professions"]], [item[0] for item in PROFESSIONS])
        self.assertEqual([item["id"] for item in result["boards"]], list(BOARDS))
        self.assertTrue(all(item["health"] == "gray" for item in result["professions"]))

    def test_explicit_fields_override_inference_and_health_uses_plan_gap(self):
        task = _task(
            "task_explicit",
            "炼钢测试任务",
            professional_id="software_development",
            board_id="热轧",
            progress=10,
        )

        result = build_progress_dashboard([task], [], self.people, today=date(2026, 7, 15))
        software = next(item for item in result["professions"] if item["id"] == "software_development")
        hot_rolling = next(item for item in result["boards"] if item["id"] == "热轧")

        self.assertEqual(software["task_count"], 1)
        self.assertEqual(hot_rolling["task_count"], 1)
        self.assertEqual(software["health"], "red")
        self.assertEqual(software["time_nodes"][0]["date"], "2026-07-20")

    def test_task_taxonomy_infers_missing_fields_from_title_and_owner(self):
        tasks = [
            _task("task_dev", "功能模块开发", owner="张工", progress=60),
            _task("task_test", "UAT 用例验证", owner="李工", progress=50),
        ]

        result = build_progress_dashboard(tasks, [], self.people, today=date(2026, 7, 15))
        self.assertEqual(result["summary"]["unclassified_profession_count"], 0)
        self.assertEqual(result["summary"]["unclassified_board_count"], 0)
        boards = {item["id"]: item for item in result["boards"]}
        professions = {item["id"]: item for item in result["professions"]}
        self.assertEqual(boards["炼钢"]["task_count"], 1)
        self.assertEqual(boards["质量"]["task_count"], 1)
        self.assertEqual(professions["software_development"]["task_count"], 1)
        self.assertEqual(professions["testing_uat"]["task_count"], 1)

    def test_candidate_meeting_task_is_classified_before_publish(self):
        candidate = InspectionItem(
            item_id="candidate_interface_check",
            category="task",
            title="质量最终版接口表完整性核验",
            description="逐项核验接口表中的收发关系和字段定义。",
            evidence_refs=[EvidenceRef("uploaded_26_08_01", "meeting_minutes", "会后待办/1", "核验接口表")],
            status=CandidateStatus.CANDIDATE,
            owner_candidates=["李工"],
            due_date="2026-08-06",
            deliverable="质量最终版接口表核验记录",
            updated_at="2026-08-01T08:00:00+08:00",
        )

        result = build_progress_dashboard([], [candidate], self.people, today=date(2026, 8, 5))
        quality = next(item for item in result["boards"] if item["id"] == "质量")
        integration = next(item for item in result["professions"] if item["id"] == "data_integration")

        self.assertEqual(result["summary"]["task_count"], 1)
        self.assertEqual(result["statuses"][0]["id"], "candidate")
        self.assertEqual(quality["task_count"], 1)
        self.assertEqual(quality["three_lists"]["task_count"], 1)
        self.assertEqual(integration["task_count"], 1)
        self.assertEqual(result["source_batches"][0]["id"], "uploaded_26_08_01")

    def test_confirmed_candidate_is_not_duplicated_with_published_task(self):
        candidate = InspectionItem(
            item_id="candidate_published",
            category="task",
            title="炼钢上线导向周计划排定",
            description="排定上线周计划。",
            evidence_refs=[],
            status=CandidateStatus.CONFIRMED,
            owner_candidates=["张工"],
        )
        published = _task("work_candidate_published", candidate.title, owner="张工", progress=0)
        published.source_candidate_id = candidate.item_id

        result = build_progress_dashboard([published], [candidate], self.people, today=date(2026, 8, 5))

        self.assertEqual(result["summary"]["task_count"], 1)

    def test_people_groups_follow_management_and_board_taxonomy(self):
        pmo = Person("PMO成员", "PMO", "项目管理", "项目 / PMO / PMO成员")
        lead = Person("技术总师", "专业统筹", "技术负责人", "项目 / 专业统筹 / 技术总师")
        legacy_lead = Person("总体成员", "总体组", "架构师", "项目 / 总体组 / 总体成员")
        member = Person("板块成员", "设备", "开发", "项目 / 设备 / 板块成员")
        numbered_member = Person("原料成员", "1-原料组", "实施", "项目 / 1-原料组 / 原料成员")
        unknown = Person("待确认人员", "场景联调组", "成员", "项目 / 场景联调组 / 待确认人员")

        self.assertEqual(classify_person_group(pmo), "PMO")
        self.assertEqual(classify_person_group(lead), "专业统筹")
        self.assertEqual(classify_person_group(legacy_lead), "专业统筹")
        self.assertEqual(classify_person_group(member), "设备")
        self.assertEqual(classify_person_group(numbered_member), "原料")
        self.assertEqual(classify_person_group(unknown), "待归类")

    def test_board_three_list_progress_exposes_data_quality_risks(self):
        linked_task = _task("task_quality", "已确认板块的任务", board_id="质量", progress=30)
        issue = InspectionItem(
            item_id="issue_quality",
            category="issue",
            title="质量检验问题",
            description="质量板块问题",
            evidence_refs=[EvidenceRef("source", "meeting", "L1", "质量检验问题")],
            status=CandidateStatus.CANDIDATE,
            owner_candidates=[],
            linked_task_ids=["task_quality"],
        )
        unclassified = InspectionItem(
            item_id="issue_keyword_only",
            category="issue",
            title="质量检验标题不能代替板块标签",
            description="仅有文字，没有显式分类或任务关联",
            evidence_refs=[EvidenceRef("source", "meeting", "L2", "不应靠关键词分类")],
            status=CandidateStatus.CANDIDATE,
            owner_candidates=[],
        )

        result = build_progress_dashboard([linked_task], [issue, unclassified], self.people, today=date(2026, 7, 15))
        quality = next(item for item in result["boards"] if item["id"] == "质量")

        self.assertEqual(quality["three_lists"]["issue_count"], 1)
        self.assertEqual(quality["three_lists"]["issue_progress"], 0)
        self.assertEqual(quality["three_lists"]["unlinked_issue_count"], 0)
        self.assertEqual(quality["three_lists"]["health"], "red")

    def test_board_three_list_uses_formal_task_progress(self):
        task = _task("task_quality", "质量检验开发", board_id="质量", progress=70)

        result = build_progress_dashboard([task], [], self.people, today=date(2026, 7, 15))
        quality = next(item for item in result["boards"] if item["id"] == "质量")

        self.assertEqual(quality["three_lists"]["task_count"], 1)
        self.assertEqual(quality["three_lists"]["task_progress"], 70)
        self.assertEqual(quality["three_lists"]["progress"], 70)

    def test_missing_progress_is_not_inferred_and_canceled_is_excluded(self):
        tasks = [
            _task("missing", "炼钢模块开发", board_id="炼钢", progress=None, due_date=""),
            _task("overdue", "质量用例测试", board_id="质量", progress=None, due_date="2026-07-10"),
            _task("canceled", "仓储接口开发", board_id="仓储", progress=None, status=WorkItemStatus.CANCELED),
        ]

        result = build_progress_dashboard(tasks, [], self.people, today=date(2026, 7, 15))
        boards = {item["id"]: item for item in result["boards"]}

        self.assertEqual(result["summary"]["missing_progress_count"], 2)
        self.assertEqual(boards["炼钢"]["progress"], 0)
        self.assertEqual(boards["炼钢"]["missing_progress_count"], 1)
        self.assertEqual(boards["炼钢"]["health"], "yellow")
        self.assertTrue(boards["炼钢"]["top_risks"][0]["missing_progress"])
        self.assertEqual(boards["质量"]["health"], "red")
        self.assertEqual(result["summary"]["task_count"], 2)
        self.assertEqual(boards["仓储"]["task_count"], 0)
        self.assertEqual(boards["仓储"]["progress"], 0)
        self.assertEqual(boards["仓储"]["missing_progress_count"], 0)
        self.assertEqual(boards["仓储"]["health"], "gray")

    def test_dashboard_maps_tasks_by_owner_status_and_source_batch(self):
        task = _task("meeting_task", "会议派发任务", owner="张工", progress=40)
        task.evidence_refs = [
            EvidenceRef("meeting-20260530", "meeting_minutes", "5.1/UAT-01", "会议任务"),
        ]

        result = build_progress_dashboard([task], [], self.people, today=date(2026, 7, 15))

        self.assertEqual(result["owners"][0]["id"], "张工")
        self.assertEqual(result["owners"][0]["task_count"], 1)
        self.assertEqual(result["statuses"][0]["id"], "in_progress")
        self.assertEqual(result["source_batches"][0]["id"], "meeting-20260530")
        self.assertEqual(result["source_batches"][0]["name"], "2026-05-30 会议任务")
        self.assertEqual(result["source_batches"][0]["progress"], 40)

    def test_meeting_batch_falls_back_to_import_audit_note(self):
        task = _task("legacy_meeting_task", "先通过接口导入的会议任务", owner="张工", progress=20)
        task.evidence_refs = [EvidenceRef("manual_three_list", "manual", "three_lists", "会议任务")]
        task.confirmation_notes = "Imported from meeting 2026-05-30 section 5.1."

        result = build_progress_dashboard([task], [], self.people, today=date(2026, 7, 15))

        self.assertEqual(result["source_batches"][0]["id"], "meeting-20260530")
        self.assertEqual(result["source_batches"][0]["task_count"], 1)


if __name__ == "__main__":
    unittest.main()
