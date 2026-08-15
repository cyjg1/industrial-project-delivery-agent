import json
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from agent.access_policy import User
from agent.daily_journal import (
    DailyJournalScheduler,
    DailyJournalService,
    DailyJournalValidationError,
    _snapshot_hash,
)
from agent.meeting_ingestion import MeetingIngestionService
from agent.schemas import CandidateStatus, InspectionItem
from store.sqlite_store import ProjectSQLiteStore
from skills.meeting_minutes.store import MeetingMinutesSkillStore


class JournalProvider:
    name = "scripted"

    def __init__(self, *, bad_quote=False, kind="work", invalid_memory_ref=False):
        self.bad_quote = bad_quote
        self.kind = kind
        self.invalid_memory_ref = invalid_memory_ref
        self.calls = []

    def complete_json(self, prompt: str) -> str:
        self.calls.append(prompt)
        if "VISIBLE_MEMORY=" in prompt:
            memory = json.loads(prompt.split("VISIBLE_MEMORY=", 1)[1])
            memory_ref = "not_visible_item" if self.invalid_memory_ref else memory[0]["item_id"]
            return json.dumps(
                {"options": [{"text": "先按已确认方法核对接口边界", "memory_refs": [memory_ref]}]},
                ensure_ascii=False,
            )
        payload = json.loads(prompt.split("HUMAN_MESSAGES=", 1)[1])
        message = payload[0]
        quote = "不存在的原话" if self.bad_quote else message["content"]
        return json.dumps(
            {
                "entries": [
                    {
                        "kind": self.kind,
                        "text": "接口联调出现阻塞" if self.kind == "problem" else "推进标准层口径确认",
                        "message_refs": [{"message_id": message["message_id"], "quote": quote}],
                    }
                ]
            },
            ensure_ascii=False,
        )


class EmptyExtractionProvider:
    name = "scripted-extraction"
    model = "test-extraction"

    def complete_json(self, prompt):
        return json.dumps({
            "people": [],
            "things": [],
            "methods": [],
            "tasks": [],
            "issues": [],
        })


class DailyJournalServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = ProjectSQLiteStore(self.tmpdir.name)
        self.store.upsert_org("org_main", "Main")
        self.store.upsert_user("u_pm", "org_main", "PM")
        self.store.upsert_user("u_exec", "org_main", "Exec")
        self.store.upsert_user("u_out", "org_main", "Outsider")
        self.store.upsert_project("project_main", "org_main", "Project", "u_pm")
        self.store.upsert_project_member("project_main", "u_pm", "pm")
        self.store.upsert_project_member("project_main", "u_exec", "exec")
        self.pm = User("u_pm", "org_main", "PM")
        self.exec = User("u_exec", "org_main", "Exec")
        self.outsider = User("u_out", "org_main", "Outsider")
        self.today = datetime.now(ZoneInfo("Asia/Shanghai")).date()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_snapshot_hash_includes_processor_version(self):
        messages = [{
            "message_id": "m1",
            "session_id": "s1",
            "created_at": "2026-07-21T01:00:00+00:00",
            "content": "完成接口联调",
        }]

        self.assertNotEqual(
            _snapshot_hash(messages, processor_version="daily-human-chat-v1"),
            _snapshot_hash(messages, processor_version="daily-human-chat-v2"),
        )

    def test_compiles_only_current_actor_human_messages_and_enqueues_tagged_source(self):
        pm_session = self.store.ensure_session(project_id="project_main", actor_id="u_pm", title="PM")
        exec_session = self.store.ensure_session(project_id="project_main", actor_id="u_exec", title="Exec")
        human = self.store.append_session_message(pm_session["session_id"], "user", "标准层口径今天需要确认。")
        self.store.append_session_message(pm_session["session_id"], "assistant", "我建议明天确认。")
        self.store.append_session_message(exec_session["session_id"], "user", "EXEC_PRIVATE_MESSAGE")

        result = DailyJournalService(self.store, provider=JournalProvider()).compile_day(
            project_id="project_main", actor=self.pm, report_date=self.today
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["message_count"], 1)
        self.assertIn(human["message_id"], result["content_markdown"])
        self.assertIn("标准层口径今天需要确认。", result["content_markdown"])
        self.assertNotIn("我建议明天确认", result["content_markdown"])
        self.assertNotIn("EXEC_PRIVATE_MESSAGE", result["content_markdown"])
        source = self.store.get_source(result["source_id"])
        self.assertEqual(source["kind"], "daily_report")
        self.assertEqual(source["author_id"], "u_pm")
        self.assertEqual(source["sensitivity"], "l3")
        self.assertEqual(self.store.get_ingestion_job(result["ingestion_job_id"])["status"], "queued")
        records = self.store.list_daily_work_records(
            project_id="project_main",
            actor=self.pm,
            access_context=self.store.access_context_for_actor("u_pm"),
        )
        self.assertEqual(
            [(row["kind"], row["text"]) for row in records],
            [("work", "推进标准层口径确认")],
        )
        self.assertEqual(records[0]["source_origin"], "conversation_daily_journal")
        self.assertEqual(records[0]["evidence_refs"][0]["message_id"], human["message_id"])

    def test_same_snapshot_is_idempotent_and_late_message_creates_new_version(self):
        session = self.store.ensure_session(project_id="project_main", actor_id="u_pm", title="PM")
        self.store.append_session_message(session["session_id"], "user", "第一条进展。")
        service = DailyJournalService(self.store, provider=JournalProvider())

        first = service.compile_day(project_id="project_main", actor=self.pm, report_date=self.today)
        repeated = service.compile_day(project_id="project_main", actor=self.pm, report_date=self.today)
        self.store.append_session_message(session["session_id"], "user", "晚些时候补充的第二条进展。")
        second = service.compile_day(project_id="project_main", actor=self.pm, report_date=self.today)

        self.assertFalse(first["unchanged"])
        self.assertTrue(repeated["unchanged"])
        self.assertEqual(first["version_id"], repeated["version_id"])
        self.assertEqual(second["version"], 2)
        self.assertNotEqual(second["source_id"], first["source_id"])

    def test_running_snapshot_is_not_duplicated_and_restart_recovery_allows_retry(self):
        session = self.store.ensure_session(project_id="project_main", actor_id="u_pm", title="PM")
        self.store.append_session_message(session["session_id"], "user", "恢复后继续整理这一条进展。")
        provider = JournalProvider()
        service = DailyJournalService(self.store, provider=provider)
        messages = service._messages(
            project_id="project_main",
            actor_id="u_pm",
            report_date=self.today,
        )
        running = self.store.begin_daily_journal_version(
            project_id="project_main",
            report_date=self.today.isoformat(),
            snapshot_hash=_snapshot_hash(messages),
            messages=messages,
            tags={
                "org_id": "org_main",
                "project_id": "project_main",
                "topic_id": None,
                "author_id": "u_pm",
                "sensitivity": "l3",
            },
            actor=self.pm,
        )

        concurrent = service.compile_day(
            project_id="project_main",
            actor=self.pm,
            report_date=self.today,
        )
        self.assertTrue(concurrent["unchanged"])
        self.assertEqual(concurrent["status"], "running")
        self.assertEqual(provider.calls, [])

        self.assertEqual(self.store.recover_interrupted_daily_journals(), 1)
        completed = service.compile_day(
            project_id="project_main",
            actor=self.pm,
            report_date=self.today,
        )
        self.assertEqual(completed["version_id"], running["version_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(len(provider.calls), 1)

    def test_same_org_nonmember_cannot_compile_project_journal(self):
        with self.assertRaisesRegex(PermissionError, "project member"):
            DailyJournalService(self.store, provider=JournalProvider()).compile_day(
                project_id="project_main",
                actor=self.outsider,
                report_date=self.today,
            )

    def test_fabricated_message_quote_is_rejected_and_failure_is_persisted(self):
        session = self.store.ensure_session(project_id="project_main", actor_id="u_pm", title="PM")
        self.store.append_session_message(session["session_id"], "user", "真实原话。")
        service = DailyJournalService(self.store, provider=JournalProvider(bad_quote=True))

        with self.assertRaises(DailyJournalValidationError):
            service.compile_day(project_id="project_main", actor=self.pm, report_date=self.today)

        latest = self.store.latest_daily_journal(project_id="project_main", actor_id="u_pm", report_date=self.today.isoformat())
        self.assertEqual(latest["status"], "failed")
        self.assertIn("quote", latest["error"])

    def test_old_generic_daily_entry_kind_is_rejected(self):
        session = self.store.ensure_session(project_id="project_main", actor_id="u_pm", title="PM")
        self.store.append_session_message(session["session_id"], "user", "今天推进了一项工作。")

        with self.assertRaises(DailyJournalValidationError):
            DailyJournalService(self.store, provider=JournalProvider(kind="action")).compile_day(
                project_id="project_main", actor=self.pm, report_date=self.today
            )

    def test_problem_options_are_grounded_in_actor_visible_memory(self):
        self._save_visible_method("接口边界核对方法", "接口联调阻塞时先核对字段边界和责任方。")
        session = self.store.ensure_session(project_id="project_main", actor_id="u_exec", title="Exec")
        self.store.append_session_message(session["session_id"], "user", "接口联调出现阻塞。")

        result = DailyJournalService(
            self.store,
            provider=JournalProvider(kind="problem"),
        ).compile_day(project_id="project_main", actor=self.exec, report_date=self.today)

        entries = result["payload"]["entries"]
        self.assertEqual(entries[0]["kind"], "problem")
        self.assertEqual(entries[0]["solution_options"][0]["memory_refs"], ["method_visible"])
        rows = self.store.list_daily_work_records(
            project_id="project_main",
            actor=self.exec,
            access_context=self.store.access_context_for_actor("u_exec"),
        )
        self.assertEqual(rows[0]["solution_options"][0]["memory_refs"], ["method_visible"])

    def test_problem_option_with_unknown_memory_reference_fails_closed(self):
        self._save_visible_method("接口联调阻塞处理", "先核对字段边界。")
        session = self.store.ensure_session(project_id="project_main", actor_id="u_exec", title="Exec")
        self.store.append_session_message(session["session_id"], "user", "接口联调出现阻塞。")

        with self.assertRaisesRegex(DailyJournalValidationError, "memory reference"):
            DailyJournalService(
                self.store,
                provider=JournalProvider(kind="problem", invalid_memory_ref=True),
            ).compile_day(project_id="project_main", actor=self.exec, report_date=self.today)

        self.assertEqual(
            self.store.list_daily_work_records(
                project_id="project_main",
                actor=self.exec,
                access_context=self.store.access_context_for_actor("u_exec"),
            ),
            [],
        )

    def test_scheduler_defaults_to_2355_shanghai_and_exposes_next_run(self):
        scheduler = DailyJournalScheduler(lambda: None)
        scheduler.start()
        try:
            status = scheduler.status()
        finally:
            scheduler.shutdown()

        self.assertEqual((status["hour"], status["minute"]), (23, 55))
        self.assertEqual(status["timezone"], "Asia/Shanghai")
        self.assertTrue(status["running"])
        self.assertTrue(status["next_run_time"])

    def test_daily_report_runs_through_continuous_ingestion_without_losing_source_kind(self):
        session = self.store.ensure_session(project_id="project_main", actor_id="u_pm", title="PM")
        self.store.append_session_message(session["session_id"], "user", "今天确认了一个可追溯的项目结论。")
        journal = DailyJournalService(self.store, provider=JournalProvider()).compile_day(
            project_id="project_main", actor=self.pm, report_date=self.today
        )
        claimed = self.store.claim_next_ingestion_job(worker_id="journal-test-worker")
        self.assertEqual(claimed["id"], journal["ingestion_job_id"])

        processed = MeetingIngestionService(
            store=self.store,
            minutes_runner=SimpleNamespace(
                store=MeetingMinutesSkillStore(f"{self.tmpdir.name}/minutes-artifacts")
            ),
            extraction_provider=EmptyExtractionProvider(),
            continuity_provider=EmptyExtractionProvider(),
        ).process_job(claimed["id"])

        self.assertEqual(processed["status"], "completed")
        self.assertEqual(self.store.get_source(journal["source_id"])["kind"], "daily_report")

    def _save_visible_method(self, title: str, description: str) -> None:
        self.store.save_item(
            InspectionItem(
                item_id="method_visible",
                category="method",
                title=title,
                description=description,
                evidence_refs=[],
                status=CandidateStatus.CONFIRMED,
                org_id="org_main",
                project_id="project_main",
                author_id="u_pm",
                sensitivity="l1",
            ),
            materialize=False,
        )


if __name__ == "__main__":
    unittest.main()
