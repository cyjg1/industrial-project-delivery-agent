import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.access_policy import User
from agent.meeting_ingestion import MeetingIngestionError, MeetingIngestionService, resolve_meeting_input_kind
from ingestion.source_manifest import register_uploaded_meeting_note
from skills.meeting_minutes.runner import MeetingMinutesSkillRunner
from skills.meeting_minutes.store import MeetingMinutesSkillStore
from store.sqlite_store import ProjectSQLiteStore


class PipelineMinutesClient:
    model_name = "pipeline-minutes-test"

    def generate(self, system_prompt: str, user_message: str) -> str:
        if "ASR 术语纠错抽取器" in system_prompt:
            return '{"candidates": []}'
        if "方法论候选抽取器" in system_prompt:
            return '{"methodologies": []}'
        if "第二次转写" in user_message or "第三次转写" in user_message:
            return "# 会议纪要\n\n930上线风险已经明确先灰度验证。"
        return "# 会议纪要\n\n930上线风险仍待确认。"


class PipelineExtractionProvider:
    name = "pipeline-extraction-test"
    model = "pipeline-extraction-test"

    def __init__(self):
        self.prompts = []

    def complete_json(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if "已经明确先灰度验证" in prompt:
            description = "上线前先完成灰度验证。"
            quote = "930上线风险已经明确先灰度验证"
        else:
            description = "上线风险仍待确认。"
            quote = "930上线风险仍待确认"
        return json.dumps(
            {
                "people": [],
                "things": [],
                "methods": [],
                "tasks": [],
                "issues": [
                    {
                        "title": "930上线风险",
                        "description": description,
                        "status": "candidate",
                        "evidence_refs": [{"locator": "line:3", "quote": quote}],
                    }
                ],
            },
            ensure_ascii=False,
        )


class PipelineContinuityProvider:
    name = "pipeline-continuity-test"
    model = "pipeline-continuity-test"

    def __init__(self):
        self.prompts = []

    def complete_json(self, prompt: str) -> str:
        self.prompts.append(prompt)
        marker = "ALLOWED_THREAD_IDS_JSON="
        allowed = json.loads(prompt.split(marker, 1)[1].splitlines()[0])
        if not allowed:
            payload = {
                "thread_id": None,
                "thread_title": "930上线风险",
                "thread_key": "930-release-risk",
                "relation": "new",
                "reason": "首次出现的上线风险事项。",
                "changed_fields": [],
            }
        else:
            payload = {
                "thread_id": allowed[0],
                "thread_title": "930上线风险",
                "thread_key": "930-release-risk",
                "relation": "update",
                "reason": "同一风险从待确认推进为灰度验证。",
                "changed_fields": ["description"],
            }
        return json.dumps(payload, ensure_ascii=False)


def _seed_access(store: ProjectSQLiteStore) -> User:
    actor = User(id="u_pm", org_id="org_mvp", name="Project Manager")
    store.upsert_org("org_mvp", "MVP Org")
    store.upsert_user(actor.id, actor.org_id, actor.name)
    store.upsert_project("project_mvp", "org_mvp", "MVP Project", actor.id)
    store.upsert_project_member("project_mvp", actor.id, "pm")
    store.upsert_topic("topic_a", "project_mvp", "上线专题", actor.id)
    store.upsert_topic_member("topic_a", actor.id)
    return actor


class MeetingIngestionTest(unittest.TestCase):
    def test_auto_input_kind_follows_upload_extension_contract(self):
        self.assertEqual(resolve_meeting_input_kind("auto", Path("notes.md")), "minutes")
        self.assertEqual(resolve_meeting_input_kind("auto", Path("notes.markdown")), "minutes")
        self.assertEqual(resolve_meeting_input_kind("auto", Path("notes.txt")), "minutes")
        self.assertEqual(resolve_meeting_input_kind("auto", Path("transcript.docx")), "transcript")
        self.assertEqual(resolve_meeting_input_kind("minutes", Path("transcript.docx")), "minutes")
        self.assertEqual(resolve_meeting_input_kind("transcript", Path("notes.txt")), "transcript")
        with self.assertRaisesRegex(MeetingIngestionError, "ambiguous"):
            resolve_meeting_input_kind("auto", Path("recording.pdf"))

    def test_three_meetings_form_one_thread_and_exact_repeat_only_adds_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            upload_root = root / "uploads"
            store = ProjectSQLiteStore(root / "store")
            actor = _seed_access(store)
            tags = {
                "org_id": actor.org_id,
                "project_id": "project_mvp",
                "topic_id": "topic_a",
                "author_id": actor.id,
                "sensitivity": "l1",
            }
            minutes_runner = MeetingMinutesSkillRunner(
                store=MeetingMinutesSkillStore(root / "meeting_minutes"),
                model_client=PipelineMinutesClient(),
            )
            extraction = PipelineExtractionProvider()
            continuity = PipelineContinuityProvider()
            service = MeetingIngestionService(
                store=store,
                minutes_runner=minutes_runner,
                extraction_provider=extraction,
                continuity_provider=continuity,
            )

            with patch.dict(
                os.environ,
                {
                    "PROJECT_AGENT_UPLOAD_ROOT": str(upload_root),
                    "PROJECT_AGENT_UPLOAD_MANIFEST": str(upload_root / "manifest.json"),
                },
                clear=False,
            ):
                jobs = []
                for index, text in enumerate(
                    [
                        "第一次转写：930上线风险仍待确认。",
                        "第二次转写：930上线风险已经明确先灰度验证。",
                        "第三次转写：930上线风险已经明确先灰度验证。",
                    ],
                    start=1,
                ):
                    source = register_uploaded_meeting_note(
                        f"meeting_{index}.txt",
                        text.encode("utf-8"),
                        title=f"第{index}次上线会议",
                        meeting_date=f"2026-07-{index + 14:02d}",
                        topic="930上线",
                        access_tags=tags,
                        input_kind="transcript",
                    )
                    store.ingest(
                        source,
                        tags=tags,
                        actor=actor,
                        kind="transcript",
                        materialize=False,
                    )
                    jobs.append(
                        store.enqueue_ingestion_job(
                            source_id=source.doc_id,
                            input_kind="transcript",
                            content_hash=source.content_hash,
                            processor_version=service.processor_version,
                            input_path=str(source.raw_source.path),
                            actor=actor,
                        )
                    )

                    processed = service.process_next(worker_id="test-worker")
                    self.assertEqual(processed["job_id"], jobs[-1]["id"])
                    self.assertEqual(processed["status"], "completed")
                    self.assertTrue(Path(processed["minutes_path"]).exists())

                    if index < 3:
                        pending = [item for item in store.list_items() if item.status.value == "candidate"]
                        self.assertEqual(len(pending), 1)
                        store.confirm_item(pending[0].item_id, editor=actor.id)

            threads = store.list_memory_threads(project_id="project_mvp")
            items = store.list_items()
            timeline = store.list_memory_thread_items(threads[0]["id"])
            sources = store.list_memory_thread_sources(threads[0]["id"])
            current = store.get_item(threads[0]["current_item_id"])
            third_job = store.get_ingestion_job(jobs[2]["id"])

        self.assertEqual(len(threads), 1)
        self.assertEqual(len(items), 2)
        self.assertEqual(len(timeline), 2)
        self.assertEqual(len(sources), 3)
        self.assertEqual(third_job["candidate_count"], 0)
        self.assertEqual(third_job["delta_auto_merged"], 1)
        self.assertEqual(len(continuity.prompts), 2)
        self.assertEqual(
            {ref.source_doc_id for ref in current.evidence_refs},
            {"uploaded_meeting_2", "uploaded_meeting_3"},
        )

    def test_continuity_model_never_receives_hidden_thread(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = ProjectSQLiteStore(root / "store")
            pm = _seed_access(store)
            exec_actor = User(id="u_exec", org_id="org_mvp", name="Executor")
            store.upsert_user(exec_actor.id, exec_actor.org_id, exec_actor.name)
            store.upsert_project_member("project_mvp", exec_actor.id, "exec")
            store.upsert_topic_member("topic_a", exec_actor.id)
            store.upsert_topic("topic_b", "project_mvp", "管理层专题", pm.id)
            store.upsert_topic_member("topic_b", pm.id)
            store.upsert_memory_thread(
                thread_id="hidden_thread",
                category="issue",
                thread_key="hidden-management-risk",
                title="隐藏的管理层风险",
                tags={
                    "org_id": "org_mvp",
                    "project_id": "project_mvp",
                    "topic_id": "topic_b",
                    "author_id": pm.id,
                    "sensitivity": "l3",
                },
                actor=pm,
                first_seen_at="2026-07-01T00:00:00+00:00",
            )
            tags = {
                "org_id": "org_mvp",
                "project_id": "project_mvp",
                "topic_id": "topic_a",
                "author_id": exec_actor.id,
                "sensitivity": "l1",
            }
            minutes_runner = MeetingMinutesSkillRunner(
                store=MeetingMinutesSkillStore(root / "meeting_minutes"),
                model_client=PipelineMinutesClient(),
            )
            continuity = PipelineContinuityProvider()
            service = MeetingIngestionService(
                store=store,
                minutes_runner=minutes_runner,
                extraction_provider=PipelineExtractionProvider(),
                continuity_provider=continuity,
            )
            with patch.dict(
                os.environ,
                {
                    "PROJECT_AGENT_UPLOAD_ROOT": str(root / "uploads"),
                    "PROJECT_AGENT_UPLOAD_MANIFEST": str(root / "uploads" / "manifest.json"),
                },
                clear=False,
            ):
                source = register_uploaded_meeting_note(
                    "exec_meeting.txt",
                    "第一次转写：930上线风险仍待确认。".encode("utf-8"),
                    access_tags=tags,
                    input_kind="transcript",
                )
                store.ingest(source, tags=tags, actor=exec_actor, kind="transcript", materialize=False)
                store.enqueue_ingestion_job(
                    source_id=source.doc_id,
                    input_kind="transcript",
                    content_hash=source.content_hash,
                    processor_version=service.processor_version,
                    input_path=str(source.raw_source.path),
                    actor=exec_actor,
                )
                service.process_next(worker_id="test-worker")

        self.assertEqual(len(continuity.prompts), 1)
        self.assertNotIn("hidden_thread", continuity.prompts[0])
        self.assertNotIn("隐藏的管理层风险", continuity.prompts[0])


if __name__ == "__main__":
    unittest.main()
