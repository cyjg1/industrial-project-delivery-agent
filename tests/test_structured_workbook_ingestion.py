from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from agent.access_policy import User
from agent.schemas import CandidateStatus, InspectionItem
from agent.source_ingestion import SourceIngestionService
from agent.structured_ingestion import StructuredIngestionService
from agent.meeting_ingestion import enqueue_source_ingestion
from ingestion.source_manifest import read_curated_text, register_uploaded_project_file
from ingestion.structured_workbook import (
    StructuredWorkbookError,
    detect_structured_workbook_kind,
)
from store.sqlite_store import ProjectSQLiteStore


TASK_HEADERS = [
    "任务描述",
    "任务项目详细说明",
    "任务类型",
    "责任人",
    "交付方相关人员",
    "交付方责任板块",
    "客户方信息化任务",
    "客户方责任部门",
    "关联：关联问题",
    "截止时间",
    "可验收标准",
    "优先级",
    "进度",
    "任务状态",
    "实际开始时间",
    "实际完成时间",
    "备注",
    "父任务",
    "创建人",
    "最后修改人",
    "父任务2",
    "创建时间",
    "最后修改时间",
]

ISSUE_HEADERS = [
    "问题描述",
    "问题类型",
    "问题层级",
    "问题优先级",
    "状态",
    "问题来源",
    "关联-原始问题",
    "登记时间",
    "责任人",
    "涉及部门",
    "计划解决时间",
    "确认人",
    "确认部门",
    "确认时间",
    "备注",
    "关联：任务清单",
    "任务数量",
    "当前的困难是什么",
]

WORK_LOG_HEADERS = [
    "工作内容",
    "姓名",
    "板块",
    "日期",
    "工作时长",
    "是否在现场",
    "工作类型",
    "执行任务",
    "卡点问题",
    "问题类型",
    "交付物",
    "交付物附件_1",
]


def workbook_bytes(headers: list[str], rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "表格视图"
    sheet.append(headers)
    for row in rows:
        sheet.append([*row, *([None] * max(0, len(headers) - len(row)))])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def task_row(
    title: str,
    *,
    owner: str = "",
    due_date: str = "",
    acceptance: str = "",
    priority: str = "",
    progress: object = None,
    status: str = "未开始",
    parent: str = "",
    created_at: str = "2026-07-01 09:00:00",
) -> list[object]:
    row: list[object] = [None] * len(TASK_HEADERS)
    values = {
        "任务描述": title,
        "任务项目详细说明": f"{title}的详细说明",
        "责任人": owner,
        "截止时间": due_date,
        "可验收标准": acceptance,
        "优先级": priority,
        "进度": progress,
        "任务状态": status,
        "父任务": parent,
        "创建时间": created_at,
    }
    for key, value in values.items():
        row[TASK_HEADERS.index(key)] = value
    return row


class StructuredWorkbookIngestionTest(unittest.TestCase):
    def seed_store(self, root: Path) -> tuple[ProjectSQLiteStore, User]:
        store = ProjectSQLiteStore(root)
        store.upsert_org("org_mvp", "MVP Org")
        store.upsert_user("u_pm", "org_mvp", "Project Manager")
        store.upsert_user("u_exec_a", "org_mvp", "执行人甲")
        store.upsert_project("project_mvp", "org_mvp", "MVP Project", "u_pm")
        store.upsert_project_member("project_mvp", "u_pm", "pm")
        store.upsert_project_member("project_mvp", "u_exec_a", "exec", manager_id="u_pm")
        return store, User(id="u_pm", org_id="org_mvp", name="Project Manager")

    def test_detects_supported_spreadsheets_by_headers_and_rejects_unknown_schema(self):
        task = workbook_bytes(TASK_HEADERS, [task_row("任务")])
        issue = workbook_bytes(ISSUE_HEADERS, [["问题"]])
        work_log = workbook_bytes(WORK_LOG_HEADERS, [["完成接口", "执行人", "铁区", "2026-07-20"]])
        unknown = workbook_bytes(["列一", "列二"], [["a", "b"]])

        self.assertEqual(detect_structured_workbook_kind("任务.xlsx", task), "three_list_tasks")
        self.assertEqual(detect_structured_workbook_kind("问题.xlsx", issue), "three_list_issues")
        self.assertEqual(detect_structured_workbook_kind("日志.xlsx", work_log), "work_logs")
        with self.assertRaisesRegex(StructuredWorkbookError, "无法识别 Excel 表头"):
            detect_structured_workbook_kind("未知.xlsx", unknown)

    def test_chinese_upload_names_have_stable_distinct_source_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "PROJECT_AGENT_UPLOAD_ROOT": str(Path(tmpdir) / "uploads"),
                "PROJECT_AGENT_UPLOAD_MANIFEST": str(Path(tmpdir) / "uploads" / "manifest.json"),
            },
            clear=False,
        ):
            task = register_uploaded_project_file(
                "(任务清单)表格视图.xlsx",
                workbook_bytes(TASK_HEADERS, [task_row("任务 A")]),
                input_kind="three_list_tasks",
            )
            issue = register_uploaded_project_file(
                "(问题清单)表格视图.xlsx",
                workbook_bytes(ISSUE_HEADERS, [["问题 A"]]),
                input_kind="three_list_issues",
            )
            task_update = register_uploaded_project_file(
                "(任务清单)表格视图.xlsx",
                workbook_bytes(TASK_HEADERS, [task_row("任务 B")]),
                input_kind="three_list_tasks",
            )

            self.assertEqual(read_curated_text(task), "")

        self.assertNotEqual(task.doc_id, issue.doc_id)
        self.assertEqual(task.doc_id, task_update.doc_id)

    def test_task_snapshot_updates_existing_status_and_only_adds_qualified_nodes(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "PROJECT_AGENT_UPLOAD_ROOT": str(Path(tmpdir) / "uploads"),
                "PROJECT_AGENT_UPLOAD_MANIFEST": str(Path(tmpdir) / "uploads" / "manifest.json"),
            },
            clear=False,
        ):
            root = Path(tmpdir) / "store"
            store, actor = self.seed_store(root)
            existing = InspectionItem(
                item_id="existing_task",
                category="task",
                title="既有任务",
                description="旧描述",
                evidence_refs=[],
                status=CandidateStatus.CONFIRMED,
                org_id="org_mvp",
                project_id="project_mvp",
                author_id="u_pm",
                sensitivity="l1",
            )
            store.save_item(existing, materialize=False)
            store.publish_item_as_work_item(existing.item_id, editor="u_pm", notes="seed")

            content = workbook_bytes(
                TASK_HEADERS,
                [
                    task_row("项目交付", owner="项目经理", priority="重要"),
                    task_row(
                        "既有任务",
                        owner="执行人甲",
                        due_date="2026-07-20",
                        acceptance="验收通过",
                        priority="重要",
                        progress=1,
                        status="已完成",
                        parent="项目交付",
                        created_at="2026-06-01 09:00:00",
                    ),
                    task_row(
                        "新增重点任务",
                        owner="执行人乙",
                        due_date="2026-07-30",
                        acceptance="提交评审记录",
                        priority="非常重要",
                        progress=0.6,
                        status="进行中",
                        parent="项目交付",
                        created_at="2026-07-01 10:00:00",
                    ),
                    task_row("普通节点", owner="执行人丙", priority="一般", parent="项目交付"),
                ],
            )
            source = register_uploaded_project_file(
                "(任务清单)表格视图.xlsx",
                content,
                access_tags={
                    "org_id": "org_mvp",
                    "project_id": "project_mvp",
                    "topic_id": None,
                    "author_id": "u_pm",
                    "sensitivity": "l1",
                },
                input_kind="three_list_tasks",
            )
            store.ingest(
                source,
                tags={
                    "org_id": "org_mvp",
                    "project_id": "project_mvp",
                    "topic_id": None,
                    "author_id": "u_pm",
                    "sensitivity": "l1",
                },
                actor=actor,
                kind=source.input_kind,
                materialize=False,
            )
            enqueue_source_ingestion(store=store, source=source, actor=actor)

            result = SourceIngestionService(store=store).process_next(worker_id="test-worker")
            work_items = {item.title: item for item in store.list_work_items()}

        self.assertEqual(result["status"], "completed")
        self.assertEqual(set(work_items), {"既有任务", "新增重点任务"})
        self.assertEqual(work_items["既有任务"].status.value, "done")
        self.assertEqual(work_items["既有任务"].owner_candidates, ["执行人甲"])
        self.assertEqual(work_items["新增重点任务"].status.value, "in_progress")
        self.assertEqual(work_items["新增重点任务"].progress_percent, 60)
        self.assertEqual(work_items["新增重点任务"].due_date, "2026-07-30")
        self.assertEqual(work_items["新增重点任务"].org_id, "org_mvp")
        self.assertEqual(work_items["新增重点任务"].evidence_refs[0].source_doc_id, source.doc_id)

    def test_work_logs_are_grouped_as_memory_and_never_published_as_tasks(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "PROJECT_AGENT_UPLOAD_ROOT": str(Path(tmpdir) / "uploads"),
                "PROJECT_AGENT_UPLOAD_MANIFEST": str(Path(tmpdir) / "uploads" / "manifest.json"),
            },
            clear=False,
        ):
            root = Path(tmpdir) / "store"
            store, actor = self.seed_store(root)
            content = workbook_bytes(
                WORK_LOG_HEADERS,
                [
                    [
                        "完成接口 A", "执行人甲", "铁区", "2026-07-20", 7.5,
                        "否", "开发", "", "等待字段确认", "技术", "接口清单", "image-a.png",
                    ],
                    ["完成接口 B", "执行人乙", "铁区", "2026-07-20", 6, "否", "测试"],
                    ["完成方案评审", "执行人丙", "质量", "2026-07-20", 4, "是", "评审"],
                    ["", "执行人甲", "铁区", "2026-07-20", 1, "否", "协调", "", "仅有问题也要回填"],
                    ["", "执行人甲", "质量", "2026-07-20", 1, "否", "交付", "", "", "", "仅有产出也要回填"],
                ],
            )
            source = register_uploaded_project_file(
                "(工作日志记录)表格视图.xlsx",
                content,
                access_tags={
                    "org_id": "org_mvp",
                    "project_id": "project_mvp",
                    "topic_id": None,
                    "author_id": "u_pm",
                    "sensitivity": "l1",
                },
                input_kind="work_logs",
            )
            store.ingest(
                source,
                tags={
                    "org_id": "org_mvp",
                    "project_id": "project_mvp",
                    "topic_id": None,
                    "author_id": "u_pm",
                    "sensitivity": "l1",
                },
                actor=actor,
                kind=source.input_kind,
                materialize=False,
            )
            enqueue_source_ingestion(store=store, source=source, actor=actor)
            result = SourceIngestionService(store=store).process_next(worker_id="test-worker")
            activities = [item for item in store.list_items() if item.category == "activity"]
            work_items = store.list_work_items()
            records_before_backfill = store.list_daily_work_records(
                project_id="project_mvp",
                actor=actor,
                access_context=store.access_context_for_actor(actor.id),
            )
            backfill = StructuredIngestionService(store=store).backfill_work_log_source(source.doc_id)
            records = store.list_daily_work_records(
                project_id="project_mvp",
                actor=actor,
                access_context=store.access_context_for_actor(actor.id),
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(records_before_backfill, [])
        self.assertEqual(backfill["work_record_count"], 7)
        self.assertEqual(len(activities), 2)
        self.assertEqual(work_items, [])
        self.assertTrue(any("完成接口 A" in item.description for item in activities))
        self.assertEqual([row["kind"] for row in records].count("work"), 3)
        self.assertEqual([row["kind"] for row in records].count("problem"), 2)
        self.assertEqual([row["kind"] for row in records].count("output"), 2)
        self.assertNotIn("conclusion", {row["kind"] for row in records})
        self.assertTrue(all(row["source_origin"] == "historical_excel_backfill" for row in records))
        matched = next(row for row in records if row["subject_name"] == "执行人甲" and row["kind"] == "work")
        unmatched = next(row for row in records if row["subject_name"] == "执行人乙")
        self.assertEqual(matched["subject_user_id"], "u_exec_a")
        self.assertEqual(unmatched["subject_user_id"], "")
        self.assertNotIn("image-a.png", json.dumps(records, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
