import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent.schemas import (
    CandidateItem,
    CandidateStatus,
    EvidenceRef,
    EvidenceVerificationResult,
    InspectionReport,
    ProjectAgentOutput,
    SourceDocument,
    SourceRef,
)
from backend.main import create_app
from store.sqlite_store import ProjectSQLiteStore


class ScriptedChatProvider:
    name = "scripted"

    def __init__(self, turns):
        self.turns = list(turns)

    def complete(self, prompt: str) -> str:
        return "{}"

    def complete_json(self, prompt: str) -> str:
        return "{}"

    def stream_chat(self, messages, tools=None):
        turn = self.turns.pop(0) if self.turns else {"kind": "delta", "text": "done"}
        if turn.get("kind") == "tool_calls":
            yield {"kind": "tool_calls", "tool_calls": turn["tool_calls"]}
            return
        for text in turn.get("text", ""):
            yield {"kind": "delta", "text": text}


def _source(**overrides) -> SourceDocument:
    data = {
        "doc_id": "uploaded_sensitive_note",
        "title": "敏感纪要",
        "meeting_date": "2026-07-09",
        "topic": "能力评价专题",
        "curated_source": SourceRef("markdown", "/tmp/note.md", "matched"),
        "raw_source": SourceRef("transcript", "", "raw_source_pending"),
        "tags": ["测试"],
        "org_id": "org_mvp",
        "project_id": "project_mvp",
        "topic_id": "topic_b",
        "author_id": "u_pm",
        "sensitivity": "l1",
    }
    data.update(overrides)
    return SourceDocument(**data)


def _tags(**overrides) -> dict:
    data = {
        "org_id": "org_mvp",
        "project_id": "project_mvp",
        "topic_id": "topic_a",
        "author_id": "u_pm",
        "sensitivity": "l1",
    }
    data.update(overrides)
    return data


def _candidate(source: SourceDocument, title: str = "能力评价待确认") -> CandidateItem:
    return CandidateItem(
        item_id=f"candidate_{source.doc_id}",
        category="things",
        title=title,
        description="仅用于验证持久化标签与访问范围。",
        evidence_refs=[EvidenceRef(source.doc_id, "curated_source", "line:1", title)],
        org_id=source.org_id,
        project_id=source.project_id,
        topic_id=source.topic_id,
        author_id=source.author_id,
        sensitivity=source.sensitivity,
    )


def _seed_access(store: ProjectSQLiteStore) -> None:
    store.upsert_org("org_mvp", "MVP Org")
    store.upsert_user("u_pm", "org_mvp", "Project Manager")
    store.upsert_user("u_exec", "org_mvp", "Executor")
    store.upsert_project("project_mvp", "org_mvp", "MVP Project", "u_pm")
    store.upsert_project_member("project_mvp", "u_pm", "pm")
    store.upsert_project_member("project_mvp", "u_exec", "exec")
    store.upsert_topic("topic_a", "project_mvp", "Topic A", "u_pm")
    store.upsert_topic_member("topic_a", "u_pm")
    store.upsert_topic_member("topic_a", "u_exec")
    store.upsert_topic("topic_b", "project_mvp", "Topic B", "u_pm")
    store.upsert_topic_member("topic_b", "u_pm")


class IngestTagsTest(unittest.TestCase):
    def test_ingest_rejects_source_without_required_tags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)

            with self.assertRaises(ValueError) as raised:
                store.ingest(_source(), tags={}, actor={"id": "u_pm"})

        self.assertIn("Missing required ingest tags", str(raised.exception))

    def test_ingest_source_persists_tags_to_source_columns_and_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)

            store.ingest(_source(), tags=_tags(topic_id="topic_b", sensitivity="l2"), actor={"id": "u_pm"}, kind="minutes")
            source = store.list_sources()[0]
            with closing(sqlite3.connect(Path(tmpdir) / "project.db")) as conn:
                row = conn.execute(
                    "select org_id, project_id, topic_id, author_id, sensitivity, tag_origin from sources where id = ?",
                    ("uploaded_sensitive_note",),
                ).fetchone()

        self.assertEqual(tuple(row), ("org_mvp", "project_mvp", "topic_b", "u_pm", "l2", "actor_ingest"))
        self.assertEqual(source["org_id"], "org_mvp")
        self.assertEqual(source["payload"]["topic_id"], "topic_b")

    def test_existing_rows_are_migration_backfilled_with_identifiable_origin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "project.db"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "create table sources(id text primary key, kind text not null, title text not null, meeting_date text, path text, created_at text, payload text not null)"
                )
                conn.execute(
                    "create table items(id text primary key, type text not null, status text not null, title text not null, body text, owner text, due_date text, deliverable text, acceptance text, evidence_refs text, source_id text, edited_by_human integer not null default 0, created_at text not null, updated_at text not null, payload text not null)"
                )
                conn.execute(
                    "insert into sources(id, kind, title, meeting_date, path, created_at, payload) values('legacy_source', 'minutes', 'Legacy', '', '', '2026-01-01', ?)",
                    (json.dumps({"doc_id": "legacy_source", "title": "Legacy"}, ensure_ascii=False),),
                )
                conn.execute(
                    "insert into items(id, type, status, title, body, evidence_refs, source_id, edited_by_human, created_at, updated_at, payload) values('legacy_item', 'thing', 'candidate', 'Legacy Item', '', '[]', 'legacy_source', 0, '2026-01-01', '2026-01-01', ?)",
                    (json.dumps({"item_id": "legacy_item", "category": "things", "title": "Legacy Item", "description": "", "evidence_refs": []}, ensure_ascii=False),),
                )
                conn.commit()

            ProjectSQLiteStore(tmpdir)
            with closing(sqlite3.connect(db_path)) as conn:
                source_count = conn.execute("select count(*) from sources where project_id is null or org_id is null").fetchone()[0]
                item_count = conn.execute("select count(*) from items where project_id is null or org_id is null").fetchone()[0]
                origins = conn.execute("select tag_origin from sources union select tag_origin from items").fetchall()

        self.assertEqual(source_count, 0)
        self.assertEqual(item_count, 0)
        self.assertEqual({row[0] for row in origins}, {"migration_backfill"})

    def test_project_output_items_inherit_source_tags_when_saved(self):
        source = _source(sensitivity="l2", topic_id="topic_b")
        item = _candidate(source)
        report = InspectionReport(
            scenario_id="S3",
            chain_name="测试链路",
            chain_gaps=[],
            responsibility_gaps=[],
            followup_drafts=[],
        )
        output = ProjectAgentOutput(
            report=report,
            verification=EvidenceVerificationResult(ok=True, checked_count=0, errors=[]),
            summary="test",
            things=[item],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            store.ingest(source, tags=_tags(topic_id="topic_b", sensitivity="l2"), actor={"id": "u_pm"})
            store.merge_project_output(output)
            rows = store.search_memory(
                "能力评价",
                {"status": CandidateStatus.CANDIDATE.value},
                limit=10,
            )

        self.assertTrue(rows)
        self.assertTrue(all(row["project_id"] == "project_mvp" for row in rows))
        self.assertTrue(all(row["topic_id"] == "topic_b" for row in rows))
        self.assertTrue(all(row["author_id"] == "u_pm" for row in rows))
        self.assertTrue(all(row["sensitivity"] == "l2" for row in rows))

    def test_upload_source_requires_actor_and_tags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = TestClient(create_app(store_dir=tmpdir))

            response = client.post(
                "/api/sources/upload",
                data={"title": "无标签上传", "meeting_date": "2026-07-09"},
                files={"file": ("meeting.md", b"# meeting\ncontent", "text/markdown")},
            )

        self.assertEqual(response.status_code, 401)
        self.assertIn("X-Actor-Id", response.json()["detail"])

    @patch("backend.routers.conversation.require_conversation_model", return_value="public-test-model")
    @patch("agent.conversation.run_manager.require_conversation_model", return_value="public-test-model")
    def test_l3_item_is_not_visible_to_exec_through_conversation_search(self, *_models):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectSQLiteStore(tmpdir)
            _seed_access(store)
            store.ingest(_source(doc_id="s_l3", title="管理层纪要"), tags=_tags(topic_id="topic_a", sensitivity="l3"), actor={"id": "u_pm"})
            item = _candidate(
                _source(doc_id="s_l3", topic_id="topic_a", sensitivity="l3"),
                "管理层决策：单据字段映射由 PM 私下确认。",
            )
            output = ProjectAgentOutput(
                report=InspectionReport("S3", "测试链路", [], [], []),
                verification=EvidenceVerificationResult(ok=True, checked_count=0, errors=[]),
                summary="test",
                things=[item],
            )
            store.merge_project_output(output)
            provider = ScriptedChatProvider([
                {
                    "kind": "tool_calls",
                    "tool_calls": [
                        {
                            "call_id": "call_search",
                            "name": "search_memory",
                            "arguments": {"query": "单据字段映射"},
                            "reason": "查记忆",
                        }
                    ],
                },
                {"kind": "delta", "text": "已按权限查询。"},
            ])
            with patch("agent.runtime.facade.AgentRuntime.provider", return_value=provider):
                client = TestClient(create_app(store_dir=tmpdir))
                response = client.post(
                    "/api/conversation",
                    headers={"X-Actor-Id": "u_exec"},
                    json={"message": "查单据字段映射"},
                )

        self.assertEqual(response.status_code, 200)
        items = response.json()["tool_steps"][0]["result"]["items"]
        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
