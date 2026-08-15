import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from agent.access_policy import User
from agent.schemas import CandidateStatus, EvidenceRef, InspectionItem, SourceDocument, SourceRef
from store.sqlite_store import ProjectSQLiteStore


ACTOR = User(id="u_pm", org_id="org_mvp", name="Project Manager")
TAGS = {
    "org_id": "org_mvp",
    "project_id": "project_mvp",
    "topic_id": "topic_a",
    "author_id": "u_pm",
    "sensitivity": "l2",
}


def _source(source_id: str = "meeting_long") -> SourceDocument:
    return SourceDocument(
        doc_id=source_id,
        title="连续事项会议转写",
        meeting_date="2026-07-15",
        topic="标准层建设",
        curated_source=SourceRef("markdown", "", "curated_pending"),
        raw_source=SourceRef("transcript", f"/tmp/{source_id}.txt", "matched"),
        tags=["meeting", "transcript"],
        **TAGS,
    )


def _item(
    item_id: str,
    *,
    status: CandidateStatus = CandidateStatus.CANDIDATE,
    effective_at: str = "2026-07-15T09:00:00+00:00",
) -> InspectionItem:
    return InspectionItem(
        item_id=item_id,
        category="thing",
        title="标准层发布范围",
        description="先覆盖核心对象，再扩展外围对象。",
        evidence_refs=[
            EvidenceRef(
                source_doc_id="meeting_long",
                source_kind="curated",
                locator="决策",
                quote="先覆盖核心对象",
                ingestion_job_id="job_version_a",
            )
        ],
        status=status,
        thread_id="thread_standard_scope",
        thread_title="标准层发布范围",
        thread_key="standard-release-scope",
        thread_event="update",
        effective_at=effective_at,
        review_required=True,
        changed_fields=["description"],
        claim_hash="claim-a",
        **TAGS,
    )


class MeetingIngestionStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ProjectSQLiteStore(self.tmp.name)
        self.store.ingest(
            _source(),
            tags=TAGS,
            actor=ACTOR,
            kind="transcript",
            materialize=False,
        )

    def test_job_is_idempotent_by_source_hash_and_processor_version(self):
        job = self.store.enqueue_ingestion_job(
            source_id="meeting_long",
            input_kind="transcript",
            content_hash="sha256-a",
            processor_version="meeting-memory-v1",
            input_path="/tmp/immutable/sha256-a.txt",
            actor=ACTOR,
        )
        repeated = self.store.enqueue_ingestion_job(
            source_id="meeting_long",
            input_kind="transcript",
            content_hash="sha256-a",
            processor_version="meeting-memory-v1",
            input_path="/tmp/immutable/sha256-a.txt",
            actor=ACTOR,
        )
        changed = self.store.enqueue_ingestion_job(
            source_id="meeting_long",
            input_kind="transcript",
            content_hash="sha256-b",
            processor_version="meeting-memory-v1",
            input_path="/tmp/immutable/sha256-b.txt",
            actor=ACTOR,
        )

        self.assertEqual(job["id"], repeated["id"])
        self.assertNotEqual(job["id"], changed["id"])
        self.assertEqual(
            len(self.store.list_ingestion_jobs(project_id="project_mvp")),
            2,
        )
        self.assertEqual(job["input_path"], "/tmp/immutable/sha256-a.txt")
        self.assertEqual(job["sensitivity"], "l2")

    def test_chunks_inherit_job_scope_and_progress_is_persisted(self):
        job = self.store.enqueue_ingestion_job(
            source_id="meeting_long",
            input_kind="transcript",
            content_hash="sha256-a",
            processor_version="meeting-memory-v1",
            input_path="/tmp/immutable/sha256-a.txt",
            actor=ACTOR,
        )

        self.store.replace_ingestion_chunks(
            job["id"],
            [
                {"chunk_index": 0, "start_char": 0, "end_char": 4000, "content_hash": "c0"},
                {"chunk_index": 1, "start_char": 3800, "end_char": 7200, "content_hash": "c1"},
            ],
        )
        self.store.update_ingestion_chunk(
            job["id"],
            0,
            status="completed",
            summary="第一段摘要",
        )

        stored_job = self.store.get_ingestion_job(job["id"])
        chunks = self.store.list_ingestion_chunks(job["id"])
        self.assertEqual(stored_job["total_chunks"], 2)
        self.assertEqual(stored_job["processed_chunks"], 1)
        self.assertEqual(chunks[0]["summary"], "第一段摘要")
        self.assertEqual(chunks[1]["start_char"], 3800)

        with closing(sqlite3.connect(Path(self.tmp.name) / "project.db")) as conn:
            rows = conn.execute(
                "select org_id, project_id, topic_id, author_id, sensitivity from ingestion_chunks order by chunk_index"
            ).fetchall()
        self.assertEqual(rows, [("org_mvp", "project_mvp", "topic_a", "u_pm", "l2")] * 2)

    def test_atomic_claim_and_restart_recovery_are_audited(self):
        job = self.store.enqueue_ingestion_job(
            source_id="meeting_long",
            input_kind="transcript",
            content_hash="sha256-a",
            processor_version="meeting-memory-v1",
            input_path="/tmp/immutable/sha256-a.txt",
            actor=ACTOR,
        )

        claimed = self.store.claim_next_ingestion_job(worker_id="worker-test")
        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["attempts"], 1)

        recovered = self.store.requeue_interrupted_ingestion_jobs()
        self.assertEqual(recovered, 1)
        self.assertEqual(self.store.get_ingestion_job(job["id"])["status"], "queued")
        events = self.store.list_events(
            entity="ingestion_job",
            entity_id=job["id"],
            action="ingestion_job_recovered",
        )
        self.assertEqual(len(events), 1)

    def test_thread_scope_prevents_cross_topic_or_sensitivity_merge(self):
        base = self.store.upsert_memory_thread(
            thread_id="thread_standard_scope",
            category="thing",
            thread_key="standard-release-scope",
            title="标准层发布范围",
            tags=TAGS,
            actor=ACTOR,
            first_seen_at="2026-07-01T09:00:00+00:00",
        )
        same = self.store.upsert_memory_thread(
            thread_id="ignored-on-upsert",
            category="thing",
            thread_key="standard-release-scope",
            title="更新后的标题",
            tags=TAGS,
            actor=ACTOR,
            first_seen_at="2026-07-08T09:00:00+00:00",
        )
        other_topic = self.store.upsert_memory_thread(
            thread_id="thread_standard_scope_topic_b",
            category="thing",
            thread_key="standard-release-scope",
            title="标准层发布范围",
            tags={**TAGS, "topic_id": "topic_b"},
            actor=ACTOR,
            first_seen_at="2026-07-08T09:00:00+00:00",
        )
        other_level = self.store.upsert_memory_thread(
            thread_id="thread_standard_scope_l3",
            category="thing",
            thread_key="standard-release-scope",
            title="标准层发布范围",
            tags={**TAGS, "sensitivity": "l3"},
            actor=ACTOR,
            first_seen_at="2026-07-08T09:00:00+00:00",
        )

        self.assertEqual(base["id"], same["id"])
        self.assertNotEqual(base["id"], other_topic["id"])
        self.assertNotEqual(base["id"], other_level["id"])
        self.assertEqual(len(self.store.list_memory_threads(project_id="project_mvp")), 3)

    def test_only_confirmed_thread_event_becomes_current_state(self):
        self.store.upsert_memory_thread(
            thread_id="thread_standard_scope",
            category="thing",
            thread_key="standard-release-scope",
            title="标准层发布范围",
            tags=TAGS,
            actor=ACTOR,
            first_seen_at="2026-07-01T09:00:00+00:00",
        )
        candidate = _item("item_candidate")
        self.store.save_item(candidate, materialize=False)
        self.store.link_memory_thread_item(
            thread_id="thread_standard_scope",
            item_id=candidate.item_id,
            source_id="meeting_long",
            ingestion_job_id="job_version_a",
            relation="update",
            effective_at=candidate.effective_at,
            review_required=True,
        )

        self.store.refresh_memory_thread("thread_standard_scope")
        self.assertIsNone(self.store.get_memory_thread("thread_standard_scope")["current_item_id"])

        self.store.confirm_item(candidate.item_id, editor="u_pm")
        current = self.store.get_memory_thread("thread_standard_scope")
        self.assertEqual(current["current_item_id"], candidate.item_id)
        self.assertEqual(current["last_seen_at"], candidate.effective_at)

        newer = _item("item_newer", effective_at="2026-07-22T09:00:00+00:00")
        self.store.save_item(newer, materialize=False)
        self.store.link_memory_thread_item(
            thread_id="thread_standard_scope",
            item_id=newer.item_id,
            source_id="meeting_long",
            ingestion_job_id="job_version_b",
            relation="conflict",
            effective_at=newer.effective_at,
            review_required=True,
        )
        self.store.reject_item(newer.item_id, editor="u_pm")

        current = self.store.get_memory_thread("thread_standard_scope")
        self.assertEqual(current["current_item_id"], candidate.item_id)
        timeline = self.store.list_memory_thread_items("thread_standard_scope")
        self.assertEqual([event["item_id"] for event in timeline], [candidate.item_id, newer.item_id])

    def test_continuity_fields_round_trip_through_item_storage(self):
        item = _item("item_round_trip")
        self.store.save_item(item, materialize=False)

        stored = self.store.get_item(item.item_id)

        self.assertEqual(stored.thread_id, "thread_standard_scope")
        self.assertEqual(stored.thread_event, "update")
        self.assertEqual(stored.effective_at, "2026-07-15T09:00:00+00:00")
        self.assertTrue(stored.review_required)
        self.assertEqual(stored.changed_fields, ["description"])
        self.assertEqual(stored.evidence_refs[0].ingestion_job_id, "job_version_a")

    def test_project_archive_removes_derived_indexes_and_archives_continuity_state(self):
        job = self.store.enqueue_ingestion_job(
            source_id="meeting_long",
            input_kind="transcript",
            content_hash="sha256-a",
            processor_version="meeting-memory-v1",
            input_path="/tmp/immutable/sha256-a.txt",
            actor=ACTOR,
        )
        self.store.upsert_memory_thread(
            thread_id="thread_standard_scope",
            category="thing",
            thread_key="standard-release-scope",
            title="标准层发布范围",
            tags=TAGS,
            actor=ACTOR,
            first_seen_at="2026-07-01T09:00:00+00:00",
        )
        item = _item("item_archive", status=CandidateStatus.CONFIRMED)
        self.store.save_item(item, materialize=False)
        self.store.link_memory_thread_item(
            thread_id="thread_standard_scope",
            item_id=item.item_id,
            source_id="meeting_long",
            ingestion_job_id=job["id"],
            relation="update",
            effective_at=item.effective_at,
            review_required=False,
        )
        with closing(sqlite3.connect(Path(self.tmp.name) / "project.db")) as conn:
            conn.execute(
                "insert or replace into embeddings(item_id, model, dimensions, vector, updated_at) values(?, 'embedding-3', 2, '[1, 0]', '2026-07-15')",
                (item.item_id,),
            )
            conn.commit()

        self.store.archive_project_workspace("project_mvp", actor_id="u_pm")

        with closing(sqlite3.connect(Path(self.tmp.name) / "project.db")) as conn:
            fts_count = conn.execute("select count(*) from items_fts where id = ?", (item.item_id,)).fetchone()[0]
            embedding_count = conn.execute("select count(*) from embeddings where item_id = ?", (item.item_id,)).fetchone()[0]
            job_status = conn.execute("select status from ingestion_jobs where id = ?", (job["id"],)).fetchone()[0]
            thread_status = conn.execute("select status from memory_threads where id = 'thread_standard_scope'").fetchone()[0]
            evidence_count = conn.execute("select count(*) from memory_thread_items where thread_id = 'thread_standard_scope'").fetchone()[0]

        self.assertEqual(fts_count, 0)
        self.assertEqual(embedding_count, 0)
        self.assertEqual(job_status, "archived")
        self.assertEqual(thread_status, "archived")
        self.assertEqual(evidence_count, 1)

    def test_confirmed_item_backfill_is_explicit_independent_and_idempotent(self):
        legacy = _item("legacy_confirmed", status=CandidateStatus.CONFIRMED)
        legacy.thread_id = None
        legacy.thread_title = None
        legacy.thread_key = None
        legacy.effective_at = None
        legacy.review_required = True
        candidate = _item("legacy_candidate", status=CandidateStatus.CANDIDATE)
        candidate.thread_id = None
        self.store.save_item(legacy, materialize=False)
        self.store.save_item(candidate, materialize=False)

        preview = self.store.backfill_confirmed_items_as_threads(project_id="project_mvp", apply=False)
        self.assertEqual(preview["mode"], "dry_run")
        self.assertEqual(preview["planned"], 1)
        self.assertEqual(self.store.list_memory_threads(project_id="project_mvp"), [])

        applied = self.store.backfill_confirmed_items_as_threads(project_id="project_mvp", apply=True)
        migrated = self.store.get_item(legacy.item_id)
        thread = self.store.get_memory_thread(migrated.thread_id)
        timeline = self.store.list_memory_thread_items(thread["id"])

        self.assertEqual(applied["created"], 1)
        self.assertEqual(thread["current_item_id"], legacy.item_id)
        self.assertEqual(thread["payload"]["migration_origin"], "migration_backfill")
        self.assertEqual(thread["payload"]["semantic_merge"], "not_inferred")
        self.assertEqual(timeline[0]["relation"], "new")
        self.assertFalse(timeline[0]["review_required"])
        self.assertIsNone(self.store.get_item(candidate.item_id).thread_id)

        repeated = self.store.backfill_confirmed_items_as_threads(project_id="project_mvp", apply=True)
        self.assertEqual(repeated["created"], 0)
        self.assertEqual(repeated["planned"], 0)
        self.assertEqual(len(self.store.list_memory_threads(project_id="project_mvp")), 1)


if __name__ == "__main__":
    unittest.main()
