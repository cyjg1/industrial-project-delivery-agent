from __future__ import annotations

import json
import tempfile
import unittest

from fastapi.testclient import TestClient

from agent.access_policy import AccessContext, User
from agent.schemas import CandidateStatus, InspectionItem
from agent.work_records import WorkRecordValidationError
from backend.main import create_app
from store.sqlite_store import ProjectSQLiteStore


class WorkRecordPolicyTest(unittest.TestCase):
    def test_managed_users_are_a_stable_recursive_closure(self):
        actor = User("u_lead", "org_main", "Lead")
        ctx = AccessContext(
            project_roles={("u_lead", "project_main"): "topic_lead"},
            reporting_managers={
                ("u_exec", "project_main"): "u_lead",
                ("u_junior", "project_main"): "u_exec",
                ("u_other", "project_main"): "u_someone_else",
            },
        )

        first = ctx.managed_user_ids(actor, "project_main")
        second = ctx.managed_user_ids(actor, "project_main")

        self.assertEqual(first, {"u_exec", "u_junior"})
        self.assertEqual(second, first)

    def test_reporting_cycle_does_not_loop_or_include_actor(self):
        actor = User("u_lead", "org_main", "Lead")
        ctx = AccessContext(
            project_roles={("u_lead", "project_main"): "topic_lead"},
            reporting_managers={
                ("u_exec", "project_main"): "u_lead",
                ("u_lead", "project_main"): "u_exec",
            },
        )

        self.assertEqual(ctx.managed_user_ids(actor, "project_main"), {"u_exec"})


class DailyWorkRecordStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = ProjectSQLiteStore(self.tmpdir.name)
        self.store.upsert_org("org_main", "Main")
        for user_id, name in [
            ("u_pm", "PM"),
            ("u_pmo", "PMO"),
            ("u_lead", "Lead"),
            ("u_exec", "Exec"),
            ("u_other", "Other"),
        ]:
            self.store.upsert_user(user_id, "org_main", name)
        self.store.upsert_project("project_main", "org_main", "Project", "u_pm")
        self.store.upsert_project_member("project_main", "u_pm", "pm")
        self.store.upsert_project_member("project_main", "u_pmo", "pmo", manager_id="u_pm")
        self.store.upsert_project_member("project_main", "u_lead", "topic_lead", manager_id="u_pmo")
        self.store.upsert_project_member("project_main", "u_exec", "exec", manager_id="u_lead")
        self.store.upsert_project_member("project_main", "u_other", "exec", manager_id="u_pmo")
        self.actors = {
            user_id: User(user_id, "org_main", name)
            for user_id, name in [
                ("u_pm", "PM"),
                ("u_pmo", "PMO"),
                ("u_lead", "Lead"),
                ("u_exec", "Exec"),
                ("u_other", "Other"),
            ]
        }

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_only_four_record_kinds_are_accepted(self):
        for index, kind in enumerate(("work", "conclusion", "problem", "output")):
            rows = self.store.replace_daily_work_records(
                series_id=f"journal_{index}",
                source_id=f"version_{index}",
                source_origin="conversation_daily_journal",
                org_id="org_main",
                project_id="project_main",
                topic_id=None,
                author_id="u_exec",
                sensitivity="l3",
                subject_user_id="u_exec",
                subject_name="Exec",
                record_date="2026-07-21",
                entries=[{
                    "kind": kind,
                    "text": f"record {kind}",
                    "evidence_refs": [{"message_id": "m1", "quote": "原话"}],
                    "solution_options": [],
                    "source_locator": "message:m1",
                }],
            )
            self.assertEqual(rows[0]["kind"], kind)

        with self.assertRaises(WorkRecordValidationError):
            self.store.replace_daily_work_records(
                series_id="journal_invalid",
                source_id="version_invalid",
                source_origin="conversation_daily_journal",
                org_id="org_main",
                project_id="project_main",
                topic_id=None,
                author_id="u_exec",
                sensitivity="l3",
                subject_user_id="u_exec",
                subject_name="Exec",
                record_date="2026-07-21",
                entries=[{
                    "kind": "progress",
                    "text": "old generic kind",
                    "evidence_refs": [{"message_id": "m1", "quote": "原话"}],
                }],
            )

    def test_pm_sees_all_lead_sees_recursive_team_and_exec_sees_self_only(self):
        self._save_record("version_lead", "u_lead", "Lead", "conclusion", "管理结论")
        self._save_record("version_exec", "u_exec", "Exec", "work", "执行工作")
        self._save_record("version_other", "u_other", "Other", "problem", "其他组问题")

        pm_rows = self._list_as("u_pm")
        pmo_rows = self._list_as("u_pmo")
        lead_rows = self._list_as("u_lead")
        exec_rows = self._list_as("u_exec")
        other_rows = self._list_as("u_other")

        self.assertEqual({row["subject_user_id"] for row in pm_rows}, {"u_lead", "u_exec", "u_other"})
        self.assertEqual({row["subject_user_id"] for row in pmo_rows}, {"u_lead", "u_exec", "u_other"})
        self.assertEqual({row["subject_user_id"] for row in lead_rows}, {"u_lead", "u_exec"})
        self.assertEqual({row["subject_user_id"] for row in exec_rows}, {"u_exec"})
        self.assertEqual({row["subject_user_id"] for row in other_rows}, {"u_other"})

    def test_unmatched_historical_person_is_management_only(self):
        self.store.replace_daily_work_records(
            series_id="historical_source",
            source_id="historical_source",
            source_origin="historical_excel_backfill",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_pm",
            sensitivity="l1",
            subject_user_id="",
            subject_name="未匹配姓名",
            record_date="2026-07-01",
            entries=[{
                "kind": "work",
                "text": "线下历史工作",
                "evidence_refs": [{"source_id": "historical_source", "locator": "Sheet1!A2"}],
                "source_locator": "Sheet1!A2",
            }],
        )

        self.assertEqual(len(self._list_as("u_pm")), 1)
        self.assertEqual(len(self._list_as("u_pmo")), 1)
        self.assertEqual(self._list_as("u_lead"), [])
        self.assertEqual(self._list_as("u_exec"), [])

    def test_authorized_edit_is_audited_and_unrelated_edit_is_rejected(self):
        row = self._save_record("version_exec", "u_exec", "Exec", "work", "原始内容")

        updated = self.store.update_daily_work_record(
            row["record_id"],
            actor=self.actors["u_lead"],
            access_context=self.store.access_context_for_actor("u_lead"),
            patch={"text": "负责人核实后的内容", "kind": "conclusion"},
        )

        self.assertEqual(updated["text"], "负责人核实后的内容")
        self.assertEqual(updated["kind"], "conclusion")
        events = self.store.list_events(entity="daily_work_record", entity_id=row["record_id"])
        self.assertEqual(events[-1]["action"], "daily_work_record_updated")
        self.assertEqual(events[-1]["payload"]["actor_id"], "u_lead")

        with self.assertRaises(PermissionError):
            self.store.update_daily_work_record(
                row["record_id"],
                actor=self.actors["u_other"],
                access_context=self.store.access_context_for_actor("u_other"),
                patch={"text": "越权修改"},
            )

    def test_repeat_generation_preserves_human_edited_fields(self):
        original = self._save_record("version_exec", "u_exec", "Exec", "work", "模型原始内容")
        self.store.update_daily_work_record(
            original["record_id"],
            actor=self.actors["u_lead"],
            access_context=self.store.access_context_for_actor("u_lead"),
            patch={"kind": "conclusion", "text": "人工核实结论"},
        )

        self._save_record("version_exec", "u_exec", "Exec", "work", "模型原始内容")
        rows = self._list_as("u_pm")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "conclusion")
        self.assertEqual(rows[0]["text"], "人工核实结论")
        self.assertTrue(rows[0]["edited_by_human"])
        self.assertEqual(rows[0]["updated_by"], "u_lead")

    def test_later_journal_version_reuses_human_correction_without_duplicate(self):
        original = self.store.replace_daily_work_records(
            series_id="journal_exec_20260721",
            source_id="daily_report_v1",
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_exec",
            sensitivity="l3",
            subject_user_id="u_exec",
            subject_name="Exec",
            record_date="2026-07-21",
            entries=[{
                "kind": "work",
                "text": "模型第一次整理",
                "evidence_refs": [{"message_id": "m1", "quote": "原始消息"}],
                "solution_options": [],
                "source_locator": "messages:m1",
            }],
        )[0]
        self.store.update_daily_work_record(
            original["record_id"],
            actor=self.actors["u_lead"],
            access_context=self.store.access_context_for_actor("u_lead"),
            patch={"kind": "conclusion", "text": "人工确认后的结论"},
        )

        self.store.replace_daily_work_records(
            series_id="journal_exec_20260721",
            source_id="daily_report_v2",
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_exec",
            sensitivity="l3",
            subject_user_id="u_exec",
            subject_name="Exec",
            record_date="2026-07-21",
            entries=[{
                "kind": "work",
                "text": "模型第二次重新措辞",
                "evidence_refs": [{"message_id": "m1", "quote": "原始消息"}],
                "solution_options": [],
                "source_locator": "messages:m1",
            }],
        )

        rows = self._list_as("u_pm")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["record_id"], original["record_id"])
        self.assertEqual(rows[0]["kind"], "conclusion")
        self.assertEqual(rows[0]["text"], "人工确认后的结论")
        self.assertEqual(rows[0]["source_id"], "daily_report_v2")

    def test_one_human_correction_is_not_reused_for_two_entries_with_same_evidence(self):
        first_version = self.store.replace_daily_work_records(
            series_id="journal_multi_entry",
            source_id="daily_multi_v1",
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_exec",
            sensitivity="l3",
            subject_user_id="u_exec",
            subject_name="Exec",
            record_date="2026-07-21",
            entries=[
                {
                    "kind": "work",
                    "text": "完成字段核对",
                    "evidence_refs": [{"message_id": "m_shared", "quote": "完成字段核对并形成结论"}],
                    "solution_options": [],
                    "source_locator": "messages:m_shared",
                },
                {
                    "kind": "conclusion",
                    "text": "字段口径已统一",
                    "evidence_refs": [{"message_id": "m_shared", "quote": "完成字段核对并形成结论"}],
                    "solution_options": [],
                    "source_locator": "messages:m_shared",
                },
            ],
        )
        self.store.update_daily_work_record(
            first_version[0]["record_id"],
            actor=self.actors["u_lead"],
            access_context=self.store.access_context_for_actor("u_lead"),
            patch={"text": "人工修订的工作记录"},
        )

        self.store.replace_daily_work_records(
            series_id="journal_multi_entry",
            source_id="daily_multi_v2",
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_exec",
            sensitivity="l3",
            subject_user_id="u_exec",
            subject_name="Exec",
            record_date="2026-07-21",
            entries=[
                {
                    "kind": "work",
                    "text": "模型重算工作",
                    "evidence_refs": [{"message_id": "m_shared", "quote": "完成字段核对并形成结论"}],
                    "solution_options": [],
                    "source_locator": "messages:m_shared",
                },
                {
                    "kind": "conclusion",
                    "text": "模型重算结论",
                    "evidence_refs": [{"message_id": "m_shared", "quote": "完成字段核对并形成结论"}],
                    "solution_options": [],
                    "source_locator": "messages:m_shared",
                },
            ],
        )

        rows = self._list_as("u_pm")
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["record_id"] for row in rows}), 2)
        self.assertIn("人工修订的工作记录", {row["text"] for row in rows})
        self.assertIn("模型重算结论", {row["text"] for row in rows})

    def test_late_message_expands_evidence_without_duplicating_human_correction(self):
        first = self.store.replace_daily_work_records(
            series_id="journal_late_message",
            source_id="daily_late_v1",
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_exec",
            sensitivity="l3",
            subject_user_id="u_exec",
            subject_name="Exec",
            record_date="2026-07-21",
            entries=[{
                "kind": "problem",
                "text": "接口口径待确认",
                "evidence_refs": [{"message_id": "m1", "quote": "接口口径待确认"}],
                "solution_options": [],
                "source_locator": "messages:m1",
            }],
        )[0]
        self.store.update_daily_work_record(
            first["record_id"],
            actor=self.actors["u_lead"],
            access_context=self.store.access_context_for_actor("u_lead"),
            patch={"kind": "conclusion", "text": "人工确认采用统一字段口径"},
        )

        self.store.replace_daily_work_records(
            series_id="journal_late_message",
            source_id="daily_late_v2",
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_exec",
            sensitivity="l3",
            subject_user_id="u_exec",
            subject_name="Exec",
            record_date="2026-07-21",
            entries=[{
                "kind": "conclusion",
                "text": "模型结合晚到消息重新总结",
                "evidence_refs": [
                    {"message_id": "m1", "quote": "接口口径待确认"},
                    {"message_id": "m2", "quote": "统一字段口径"},
                ],
                "solution_options": [],
                "source_locator": "messages:m1,m2",
            }],
        )

        rows = self._list_as("u_pm")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["record_id"], first["record_id"])
        self.assertEqual(rows[0]["text"], "人工确认采用统一字段口径")
        self.assertEqual(rows[0]["source_locator"], "messages:m1,m2")

    def test_overlap_reconciliation_uses_original_kind_not_new_entry_order(self):
        first = self.store.replace_daily_work_records(
            series_id="journal_overlap_kind",
            source_id="daily_overlap_v1",
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_exec",
            sensitivity="l3",
            subject_user_id="u_exec",
            subject_name="Exec",
            record_date="2026-07-21",
            entries=[{
                "kind": "work",
                "text": "完成接口核对",
                "evidence_refs": [{"message_id": "m1", "quote": "完成接口核对"}],
                "solution_options": [],
                "source_locator": "messages:m1",
            }],
        )[0]
        self.store.update_daily_work_record(
            first["record_id"],
            actor=self.actors["u_lead"],
            access_context=self.store.access_context_for_actor("u_lead"),
            patch={"text": "人工修订后的工作记录"},
        )

        self.store.replace_daily_work_records(
            series_id="journal_overlap_kind",
            source_id="daily_overlap_v2",
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_exec",
            sensitivity="l3",
            subject_user_id="u_exec",
            subject_name="Exec",
            record_date="2026-07-21",
            entries=[
                {
                    "kind": "output",
                    "text": "形成接口核对表",
                    "evidence_refs": [
                        {"message_id": "m1", "quote": "完成接口核对"},
                        {"message_id": "m2", "quote": "形成接口核对表"},
                    ],
                    "solution_options": [],
                    "source_locator": "messages:m1,m2",
                },
                {
                    "kind": "work",
                    "text": "模型重算的工作记录",
                    "evidence_refs": [
                        {"message_id": "m1", "quote": "完成接口核对"},
                        {"message_id": "m3", "quote": "继续复核"},
                    ],
                    "solution_options": [],
                    "source_locator": "messages:m1,m3",
                },
            ],
        )

        rows = self._list_as("u_pm")
        self.assertEqual(len(rows), 2)
        by_kind = {row["kind"]: row for row in rows}
        self.assertEqual(by_kind["work"]["record_id"], first["record_id"])
        self.assertEqual(by_kind["work"]["text"], "人工修订后的工作记录")
        self.assertEqual(by_kind["work"]["source_locator"], "messages:m1,m3")
        self.assertEqual(by_kind["output"]["text"], "形成接口核对表")

    def test_exact_locator_reconciliation_keeps_different_kinds_distinct(self):
        first = self.store.replace_daily_work_records(
            series_id="journal_exact_kind",
            source_id="daily_exact_v1",
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_exec",
            sensitivity="l3",
            subject_user_id="u_exec",
            subject_name="Exec",
            record_date="2026-07-21",
            entries=[{
                "kind": "work",
                "text": "完成接口核对",
                "evidence_refs": [{"message_id": "m1", "quote": "完成接口核对并形成表格"}],
                "solution_options": [],
                "source_locator": "messages:m1",
            }],
        )[0]
        self.store.update_daily_work_record(
            first["record_id"],
            actor=self.actors["u_lead"],
            access_context=self.store.access_context_for_actor("u_lead"),
            patch={"text": "人工修订后的工作记录"},
        )

        self.store.replace_daily_work_records(
            series_id="journal_exact_kind",
            source_id="daily_exact_v2",
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_exec",
            sensitivity="l3",
            subject_user_id="u_exec",
            subject_name="Exec",
            record_date="2026-07-21",
            entries=[
                {
                    "kind": "output",
                    "text": "形成接口核对表",
                    "evidence_refs": [{"message_id": "m1", "quote": "完成接口核对并形成表格"}],
                    "solution_options": [],
                    "source_locator": "messages:m1",
                },
                {
                    "kind": "work",
                    "text": "模型重算的工作记录",
                    "evidence_refs": [{"message_id": "m1", "quote": "完成接口核对并形成表格"}],
                    "solution_options": [],
                    "source_locator": "messages:m1",
                },
            ],
        )

        rows = self._list_as("u_pm")
        self.assertEqual(len(rows), 2)
        by_kind = {row["kind"]: row for row in rows}
        self.assertEqual(by_kind["work"]["record_id"], first["record_id"])
        self.assertEqual(by_kind["work"]["text"], "人工修订后的工作记录")
        self.assertEqual(by_kind["output"]["text"], "形成接口核对表")

    def test_current_kind_match_defers_to_later_original_kind_match(self):
        first = self.store.replace_daily_work_records(
            series_id="journal_kind_priority",
            source_id="daily_priority_v1",
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_exec",
            sensitivity="l3",
            subject_user_id="u_exec",
            subject_name="Exec",
            record_date="2026-07-21",
            entries=[{
                "kind": "work",
                "text": "完成接口核对",
                "evidence_refs": [{"message_id": "m1", "quote": "完成接口核对"}],
                "solution_options": [],
                "source_locator": "messages:m1",
            }],
        )[0]
        self.store.update_daily_work_record(
            first["record_id"],
            actor=self.actors["u_lead"],
            access_context=self.store.access_context_for_actor("u_lead"),
            patch={"kind": "conclusion", "text": "人工确认接口核对完成"},
        )

        self.store.replace_daily_work_records(
            series_id="journal_kind_priority",
            source_id="daily_priority_v2",
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_exec",
            sensitivity="l3",
            subject_user_id="u_exec",
            subject_name="Exec",
            record_date="2026-07-21",
            entries=[
                {
                    "kind": "conclusion",
                    "text": "独立的新结论",
                    "evidence_refs": [
                        {"message_id": "m1", "quote": "完成接口核对"},
                        {"message_id": "m2", "quote": "形成独立结论"},
                    ],
                    "solution_options": [],
                    "source_locator": "messages:m1,m2",
                },
                {
                    "kind": "work",
                    "text": "模型重算的原工作记录",
                    "evidence_refs": [
                        {"message_id": "m1", "quote": "完成接口核对"},
                        {"message_id": "m3", "quote": "继续复核"},
                    ],
                    "solution_options": [],
                    "source_locator": "messages:m1,m3",
                },
            ],
        )

        rows = self._list_as("u_pm")
        self.assertEqual(len(rows), 2)
        by_text = {row["text"]: row for row in rows}
        self.assertEqual(by_text["人工确认接口核对完成"]["record_id"], first["record_id"])
        self.assertEqual(by_text["人工确认接口核对完成"]["source_locator"], "messages:m1,m3")
        self.assertEqual(by_text["独立的新结论"]["source_locator"], "messages:m1,m2")
        self.assertNotIn("模型重算的原工作记录", by_text)

    def test_batch_reconciliation_prefers_global_exact_match_over_earlier_merge(self):
        first_version = self.store.replace_daily_work_records(
            series_id="journal_global_match",
            source_id="daily_global_v1",
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_exec",
            sensitivity="l3",
            subject_user_id="u_exec",
            subject_name="Exec",
            record_date="2026-07-21",
            entries=[
                {
                    "kind": "work",
                    "text": "第一项工作",
                    "evidence_refs": [{"message_id": "m1", "quote": "第一项工作"}],
                    "solution_options": [],
                    "source_locator": "messages:m1",
                },
                {
                    "kind": "work",
                    "text": "第二项工作",
                    "evidence_refs": [{"message_id": "m2", "quote": "第二项工作"}],
                    "solution_options": [],
                    "source_locator": "messages:m2",
                },
            ],
        )
        for row in first_version:
            message_id = row["source_locator"].removeprefix("messages:")
            self.store.update_daily_work_record(
                row["record_id"],
                actor=self.actors["u_lead"],
                access_context=self.store.access_context_for_actor("u_lead"),
                patch={"text": f"人工修订-{message_id}"},
            )

        first_human, second_human = sorted(first_version, key=lambda row: row["record_id"])
        first_message = first_human["source_locator"].removeprefix("messages:")
        second_message = second_human["source_locator"].removeprefix("messages:")

        self.store.replace_daily_work_records(
            series_id="journal_global_match",
            source_id="daily_global_v2",
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_exec",
            sensitivity="l3",
            subject_user_id="u_exec",
            subject_name="Exec",
            record_date="2026-07-21",
            entries=[
                {
                    "kind": "work",
                    "text": "模型合并项",
                    "evidence_refs": [
                        {"message_id": first_message, "quote": "第一项工作"},
                        {"message_id": second_message, "quote": "第二项工作"},
                        {"message_id": "m3", "quote": "第三项补充"},
                    ],
                    "solution_options": [],
                    "source_locator": f"messages:{first_message},{second_message},m3",
                },
                {
                    "kind": "work",
                    "text": "模型第一项",
                    "evidence_refs": [
                        {"message_id": first_message, "quote": "第一项工作"},
                        {"message_id": "m3", "quote": "第三项补充"},
                    ],
                    "solution_options": [],
                    "source_locator": f"messages:{first_message},m3",
                },
            ],
        )

        rows = self._list_as("u_pm")
        self.assertEqual(len(rows), 2)
        by_text = {row["text"]: row for row in rows}
        self.assertEqual(
            by_text[f"人工修订-{first_message}"]["source_locator"],
            f"messages:{first_message},m3",
        )
        self.assertEqual(
            by_text[f"人工修订-{second_message}"]["source_locator"],
            f"messages:{first_message},{second_message},m3",
        )

    def test_legacy_human_row_without_generated_kind_never_matches_another_kind(self):
        first = self.store.replace_daily_work_records(
            series_id="journal_legacy_kind",
            source_id="daily_legacy_v1",
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_exec",
            sensitivity="l3",
            subject_user_id="u_exec",
            subject_name="Exec",
            record_date="2026-07-21",
            entries=[{
                "kind": "work",
                "text": "旧版工作记录",
                "evidence_refs": [{"message_id": "m1", "quote": "旧版工作记录"}],
                "solution_options": [],
                "source_locator": "messages:m1",
            }],
        )[0]
        self.store.update_daily_work_record(
            first["record_id"],
            actor=self.actors["u_lead"],
            access_context=self.store.access_context_for_actor("u_lead"),
            patch={"text": "人工修订旧版工作"},
        )
        with self.store._connect() as conn:
            row = conn.execute(
                "select payload from daily_work_records where id = ?",
                (first["record_id"],),
            ).fetchone()
            legacy_payload = json.loads(row["payload"])
            legacy_payload.pop("generated_kind", None)
            legacy_payload.pop("generated_text", None)
            legacy_payload["logical_key"] = "\0".join([
                "journal_legacy_kind",
                "conversation_daily_journal",
                "messages:m1",
                "0",
            ])
            conn.execute(
                "update daily_work_records set payload = ? where id = ?",
                (json.dumps(legacy_payload, ensure_ascii=False), first["record_id"]),
            )
            conn.commit()

        self.store.replace_daily_work_records(
            series_id="journal_legacy_kind",
            source_id="daily_legacy_v2",
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_exec",
            sensitivity="l3",
            subject_user_id="u_exec",
            subject_name="Exec",
            record_date="2026-07-21",
            entries=[
                {
                    "kind": "output",
                    "text": "新增独立产出",
                    "evidence_refs": [{"message_id": "m1", "quote": "旧版工作记录"}],
                    "solution_options": [],
                    "source_locator": "messages:m1",
                },
                {
                    "kind": "work",
                    "text": "模型重算旧工作",
                    "evidence_refs": [{"message_id": "m1", "quote": "旧版工作记录"}],
                    "solution_options": [],
                    "source_locator": "messages:m1",
                },
            ],
        )

        rows = self._list_as("u_pm")
        self.assertEqual(len(rows), 2)
        by_kind = {row["kind"]: row for row in rows}
        self.assertEqual(by_kind["work"]["record_id"], first["record_id"])
        self.assertEqual(by_kind["work"]["text"], "人工修订旧版工作")
        self.assertEqual(by_kind["output"]["text"], "新增独立产出")

    def test_human_option_edit_preserves_matching_citations_and_rejects_client_fabrication(self):
        self.store.save_item(
            InspectionItem(
                item_id="memory_real",
                category="method",
                title="字段复核方法",
                description="按照已确认字段表逐项复核",
                evidence_refs=[],
                status=CandidateStatus.CONFIRMED,
                org_id="org_main",
                project_id="project_main",
                author_id="u_pm",
                sensitivity="l1",
            ),
            materialize=False,
        )
        row = self.store.replace_daily_work_records(
            series_id="journal_problem",
            source_id="journal_problem",
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id="u_exec",
            sensitivity="l3",
            subject_user_id="u_exec",
            subject_name="Exec",
            record_date="2026-07-21",
            entries=[{
                "kind": "problem",
                "text": "字段待确认",
                "evidence_refs": [{"message_id": "m_problem", "quote": "字段待确认"}],
                "solution_options": [{"text": "按既有字段表复核", "memory_refs": ["memory_real"]}],
                "source_locator": "message:m_problem",
            }],
        )[0]

        updated = self.store.update_daily_work_record(
            row["record_id"],
            actor=self.actors["u_lead"],
            access_context=self.store.access_context_for_actor("u_lead"),
            patch={"solution_options": [
                {"text": "按既有字段表复核", "memory_refs": ["fabricated"]},
            ]},
        )

        self.assertEqual(updated["solution_options"], [
            {"text": "按既有字段表复核", "memory_refs": ["memory_real"]},
        ])

        with self.assertRaisesRegex(WorkRecordValidationError, "actor-visible confirmed memory"):
            self.store.update_daily_work_record(
                row["record_id"],
                actor=self.actors["u_lead"],
                access_context=self.store.access_context_for_actor("u_lead"),
                patch={"solution_options": [
                    {"text": "人工新增联调会", "memory_refs": ["fabricated_too"]},
                ]},
            )

        with self.assertRaisesRegex(WorkRecordValidationError, "actor-visible confirmed memory"):
            self.store.update_daily_work_record(
                row["record_id"],
                actor=self.actors["u_lead"],
                access_context=self.store.access_context_for_actor("u_lead"),
                patch={"solution_options": [
                    {"text": "无出处的人工方案", "memory_refs": []},
                ]},
            )

    def test_api_response_omits_out_of_scope_rows_and_enforces_edit_scope(self):
        lead_row = self._save_record("version_lead", "u_lead", "Lead", "conclusion", "管理结论")
        exec_row = self._save_record("version_exec", "u_exec", "Exec", "work", "执行工作")
        other_row = self._save_record("version_other", "u_other", "Other", "problem", "其他组问题")

        with TestClient(create_app(store_dir=self.tmpdir.name)) as client:
            pm = client.get("/api/daily-work-records", headers={"X-Actor-Id": "u_pm"})
            lead = client.get("/api/daily-work-records", headers={"X-Actor-Id": "u_lead"})
            exec_response = client.get("/api/daily-work-records", headers={"X-Actor-Id": "u_exec"})
            forbidden = client.patch(
                f"/api/daily-work-records/{exec_row['record_id']}",
                headers={"X-Actor-Id": "u_other"},
                json={"text": "越权修改"},
            )
            allowed = client.patch(
                f"/api/daily-work-records/{exec_row['record_id']}",
                headers={"X-Actor-Id": "u_lead"},
                json={"text": "负责人已核实"},
            )

        self.assertEqual(pm.status_code, 200)
        self.assertEqual(
            {row["record_id"] for row in pm.json()["records"]},
            {lead_row["record_id"], exec_row["record_id"], other_row["record_id"]},
        )
        self.assertEqual(
            {row["record_id"] for row in lead.json()["records"]},
            {lead_row["record_id"], exec_row["record_id"]},
        )
        self.assertEqual(
            {row["record_id"] for row in exec_response.json()["records"]},
            {exec_row["record_id"]},
        )
        self.assertNotIn(other_row["record_id"], lead.text)
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["record"]["text"], "负责人已核实")

    def _save_record(self, source_id: str, subject_user_id: str, subject_name: str, kind: str, text: str):
        return self.store.replace_daily_work_records(
            series_id=source_id,
            source_id=source_id,
            source_origin="conversation_daily_journal",
            org_id="org_main",
            project_id="project_main",
            topic_id=None,
            author_id=subject_user_id,
            sensitivity="l3",
            subject_user_id=subject_user_id,
            subject_name=subject_name,
            record_date="2026-07-21",
            entries=[{
                "kind": kind,
                "text": text,
                "evidence_refs": [{"message_id": f"message_{source_id}", "quote": text}],
                "solution_options": [],
                "source_locator": f"message:{source_id}",
            }],
        )[0]

    def _list_as(self, actor_id: str):
        return self.store.list_daily_work_records(
            project_id="project_main",
            actor=self.actors[actor_id],
            access_context=self.store.access_context_for_actor(actor_id),
        )


if __name__ == "__main__":
    unittest.main()
