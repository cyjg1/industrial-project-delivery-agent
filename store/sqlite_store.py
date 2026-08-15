from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from math import sqrt
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from agent.env_loader import load_project_env
from agent.memory import get_token_counter
from agent.repository_paths import (
    normalize_repository_paths,
    normalize_source_payload_paths,
    resolve_repository_path,
)
from agent.access_policy import AccessContext, ROLE_RANK, User, visible, visible_filter
from agent.milestones import legacy_milestone_from_objective, milestone_from_dict
from agent.schemas import (
    AgentLoopRound,
    AgentObservation,
    AgentPlanStep,
    AgentRun,
    AgentStep,
    CandidateItem,
    CandidateStatus,
    EvidenceRef,
    EvidenceVerificationResult,
    InspectionItem,
    InspectionReport,
    MilestonePlan,
    ProjectAgentOutput,
    SourceDocument,
    SourceRef,
    WorkItem,
    WorkItemStatus,
    to_plain,
)
from agent.work_records import (
    WORK_RECORD_SOURCE_ORIGINS,
    WorkRecordValidationError,
    can_edit_work_record,
    can_view_work_record,
    validate_work_record_entry,
)
from store.database import SQLiteDatabase
from store.inspection_repository import InspectionJobRepository
from store.memory_skill_repository import MemorySkillEvolutionRepository
from store.runtime_repository import RuntimeRunRepository
from store.session_repository import SessionRepository


_STORE_LOCK = threading.RLock()
_UNSET = object()

ACCESS_TAG_FIELDS = ("org_id", "project_id", "topic_id", "author_id", "sensitivity")
REQUIRED_INGEST_TAG_FIELDS = ("org_id", "project_id", "author_id", "sensitivity")
SENSITIVITY_LEVELS = {"l1", "l2", "l3", "l4"}


class ProjectSQLiteStore:
    """SQLite-backed authority store for project memory, runs, tasks and sessions."""

    LEGACY_JSON_FILES = (
        "agent_runs.json",
        "candidate_items.json",
        "work_items.json",
        "work_item_events.jsonl",
        "sessions.jsonl",
    )

    LEGACY_MEMORY_FILES = (
        "events.jsonl",
        "summary.json",
        "index.json",
        "schema.json",
        "session_resume.json",
        "context_layers.json",
        "host_memory.json",
    )

    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root_dir / "project.db"
        self.database = SQLiteDatabase(self.database_path, lock=_STORE_LOCK)
        self.archive_dir = self.root_dir / "_archived"
        self.vault_dir = self.root_dir.parent / "vault"

        # Compatibility attributes for older callers. They point to the authority DB.
        self.runs_path = self.database_path
        self.items_path = self.database_path
        self.work_items_path = self.database_path
        self.work_item_events_path = self.database_path
        self.sessions_path = self.database_path

        with _STORE_LOCK:
            self._ensure_schema()
            self.runtime_runs = RuntimeRunRepository(self.database)
            self.runtime_runs.ensure_schema()
            self.memory_skill_evolution = MemorySkillEvolutionRepository(self.database)
            self.memory_skill_evolution.ensure_schema()
            self.inspection_jobs = InspectionJobRepository(self.database)
            self.inspection_jobs.ensure_schema()
            self.session_repository = SessionRepository(
                self.database,
                event_writer=self._append_event,
            )
            self.session_repository.ensure_schema()
            self._migrate_legacy_files()
            self._backfill_source_chunk_index()

    def save_run(self, run: AgentRun) -> None:
        payload = to_plain(run)
        project_id = _run_project_id(run)
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                """
                insert into runs(id, milestone_id, project_id, status, archived, created_at, payload)
                values(?, ?, ?, ?, 0, ?, ?)
                on conflict(id) do update set
                  milestone_id=excluded.milestone_id,
                  project_id=excluded.project_id,
                  status=excluded.status,
                  archived=0,
                  created_at=excluded.created_at,
                  payload=excluded.payload
                """,
                (
                    run.run_id,
                    run.milestone_id,
                    project_id,
                    run.status,
                    run.created_at,
                    _json(payload),
                ),
            )
            conn.commit()
        self._append_event("run", run.run_id, "run_saved", payload)

    def get_run(self, run_id: str) -> AgentRun:
        with self._connect() as conn:
            row = conn.execute("select payload from runs where id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _agent_run_from_dict(_loads(row["payload"], {}))

    def list_runs(self) -> list[AgentRun]:
        with self._connect() as conn:
            rows = conn.execute("select payload from runs where archived = 0 order by created_at, id").fetchall()
        return [_agent_run_from_dict(_loads(row["payload"], {})) for row in rows]

    def latest_run(self, project_id: str | None = None) -> AgentRun | None:
        with self._connect() as conn:
            if project_id is None:
                row = conn.execute(
                    "select payload from runs where archived = 0 order by created_at desc, id desc limit 1"
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    select payload from runs
                    where archived = 0 and project_id = ?
                    order by created_at desc, id desc limit 1
                    """,
                    (project_id,),
                ).fetchone()
        return _agent_run_from_dict(_loads(row["payload"], {})) if row else None

    def latest_verified_run(self, project_id: str) -> AgentRun | None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select payload from runs
                where archived = 0 and project_id = ?
                order by created_at desc, id desc
                """,
                (project_id,),
            ).fetchall()
        for row in rows:
            run = _agent_run_from_dict(_loads(row["payload"], {}))
            if run.verification is not None:
                return run
        return None

    def merge_report_items(self, report: InspectionReport) -> list[str]:
        return self._merge_items(_report_items(report))

    def merge_project_output(self, output: ProjectAgentOutput) -> list[str]:
        structured = [
            _candidate_to_inspection(item)
            for item in output.people + output.things + output.methods + output.tasks + output.open_questions
        ]
        return self._merge_items([*_report_items(output.report), *structured])

    def ingest(
        self,
        payload: SourceDocument | InspectionItem | CandidateItem | dict[str, Any],
        *,
        tags: dict[str, Any],
        actor: Any,
        kind: str = "minutes",
        materialize: bool = True,
        tag_origin: str = "actor_ingest",
    ) -> dict[str, Any]:
        access_tags = _validate_ingest_tags(tags, actor)
        if isinstance(payload, SourceDocument) or (
            isinstance(payload, dict) and ("doc_id" in payload or "curated_source" in payload)
        ):
            source_payload = _apply_access_tags(_source_payload(payload), access_tags, tag_origin=tag_origin)
            saved = self.save_source(source_payload, kind=kind, materialize=materialize)
            self._append_event("source", saved["doc_id"], "source_ingested", {
                "actor_id": _actor_id(actor),
                "tags": access_tags,
                "tag_origin": tag_origin,
            })
            return saved

        if isinstance(payload, CandidateItem):
            item = _candidate_to_inspection(payload)
        elif isinstance(payload, InspectionItem):
            item = payload
        else:
            item = _inspection_item_from_dict(dict(payload))
        item_payload = _apply_access_tags(to_plain(item), access_tags, tag_origin=tag_origin)
        item = _inspection_item_from_dict(item_payload)
        self.save_item(item, materialize=materialize)
        self._append_event("item", item.item_id, "item_ingested", {
            "actor_id": _actor_id(actor),
            "tags": access_tags,
            "tag_origin": tag_origin,
        })
        return to_plain(item)

    def save_source(
        self,
        source: SourceDocument | dict[str, Any],
        kind: str = "minutes",
        *,
        materialize: bool = True,
    ) -> dict[str, Any]:
        payload = normalize_source_payload_paths(_source_payload(source))
        source_id = payload["doc_id"]
        curated_path = _source_path(payload.get("curated_source"))
        raw_path = _source_path(payload.get("raw_source"))
        path = curated_path or raw_path
        existing = self.get_source(source_id)
        now = _now()
        current_ingestion_job_id = str(
            payload.get("current_ingestion_job_id")
            or (existing or {}).get("current_ingestion_job_id")
            or ""
        )
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                """
                insert into sources(
                  id, kind, title, meeting_date, path, org_id, project_id, topic_id,
                  author_id, sensitivity, tag_origin, input_kind,
                  current_ingestion_job_id, created_at, updated_at, payload
                )
                values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                  kind=excluded.kind,
                  title=excluded.title,
                  meeting_date=excluded.meeting_date,
                  path=excluded.path,
                  org_id=excluded.org_id,
                  project_id=excluded.project_id,
                  topic_id=excluded.topic_id,
                  author_id=excluded.author_id,
                  sensitivity=excluded.sensitivity,
                  tag_origin=excluded.tag_origin,
                  input_kind=excluded.input_kind,
                  current_ingestion_job_id=excluded.current_ingestion_job_id,
                  updated_at=excluded.updated_at,
                  payload=excluded.payload
                """,
                (
                    source_id,
                    kind,
                    payload.get("title", source_id),
                    payload.get("meeting_date", ""),
                    path,
                    payload.get("org_id", "org_mvp"),
                    payload.get("project_id", "project_mvp"),
                    payload.get("topic_id") or None,
                    payload.get("author_id", "system"),
                    payload.get("sensitivity", "l1"),
                    payload.get("tag_origin", "runtime_default"),
                    payload.get("input_kind", kind if kind in {"minutes", "transcript"} else "auto"),
                    current_ingestion_job_id,
                    (existing or {}).get("created_at") or now,
                    now,
                    _json(payload),
                ),
            )
            conn.commit()
        chunk_index = self._index_source_chunks(payload)
        self._record_source_chunk_index_status(chunk_index)
        if chunk_index["status"] != "indexed":
            self._append_event(
                "source",
                source_id,
                "source_chunk_index_skipped",
                chunk_index,
            )
        if not existing or existing.get("payload") != payload:
            self._append_event("source", source_id, "source_saved", payload)
            if materialize:
                self.generate_vault_mirror()
        return payload

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("select * from sources where id = ?", (source_id,)).fetchone()
        return _source_row(row) if row else None

    def list_sources(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("select * from sources order by meeting_date, title, id").fetchall()
        return [_source_row(row) for row in rows]

    def upsert_calendar_event(self, event: dict[str, Any]) -> dict[str, Any]:
        event_id = str(event.get("event_id") or "").strip()
        project_id = str(event.get("project_id") or "").strip()
        title = str(event.get("title") or "").strip()
        event_date = str(event.get("date") or "").strip()
        if not event_id or not project_id or not title:
            raise ValueError("calendar event requires event_id, project_id, and title")
        try:
            datetime.strptime(event_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("calendar event date must use YYYY-MM-DD") from exc
        now = _now()
        payload = {
            "event_id": event_id,
            "event_type": "meeting",
            "project_id": project_id,
            "org_id": str(event.get("org_id") or "org_mvp"),
            "topic_id": str(event.get("topic_id") or ""),
            "author_id": str(event.get("author_id") or "system"),
            "sensitivity": str(event.get("sensitivity") or "l1"),
            "date": event_date,
            "title": title,
            "time_label": str(event.get("time_label") or "会议"),
            "owner_names": list(dict.fromkeys(
                str(name).strip() for name in event.get("owner_names") or [] if str(name).strip()
            )),
            "status": str(event.get("status") or "scheduled"),
            "deliverable": str(event.get("deliverable") or ""),
            "description": str(event.get("description") or ""),
            "source_id": str(event.get("source_id") or ""),
            "created_at": str(event.get("created_at") or now),
            "updated_at": now,
        }
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                """
                insert into calendar_events(
                  id, project_id, event_date, title, org_id, topic_id, author_id,
                  sensitivity, payload, created_at, updated_at
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                  project_id=excluded.project_id,
                  event_date=excluded.event_date,
                  title=excluded.title,
                  org_id=excluded.org_id,
                  topic_id=excluded.topic_id,
                  author_id=excluded.author_id,
                  sensitivity=excluded.sensitivity,
                  payload=excluded.payload,
                  updated_at=excluded.updated_at
                """,
                (
                    event_id, project_id, event_date, title, payload["org_id"],
                    payload["topic_id"], payload["author_id"], payload["sensitivity"],
                    _json(payload), payload["created_at"], now,
                ),
            )
            conn.commit()
        self._append_event("calendar_event", event_id, "calendar_event_saved", payload)
        return payload

    def list_calendar_events(self, project_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if project_id:
                rows = conn.execute(
                    "select payload from calendar_events where project_id = ? order by event_date, title, id",
                    (project_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "select payload from calendar_events order by event_date, title, id"
                ).fetchall()
        return [_loads(row["payload"], {}) for row in rows]

    def _index_source_chunks(self, source: dict[str, Any]) -> dict[str, Any]:
        source_id = str(source.get("doc_id") or "").strip()

        def skipped(reason: str, **details: Any) -> dict[str, Any]:
            self._clear_source_chunks(source_id)
            return {
                "status": "skipped",
                "reason": reason,
                "source_id": source_id,
                **details,
            }

        source_path = (
            _source_path(source.get("curated_source"))
            or _source_path(source.get("raw_source"))
        )
        if not source_path:
            return skipped("source_path_missing")
        try:
            resolved = resolve_repository_path(
                source_path,
                allowed_roots=(self.root_dir, self.root_dir.parent),
            )
        except ValueError as exc:
            return skipped(
                "source_path_outside_configured_roots",
                detail=str(exc),
            )
        if not resolved.is_file():
            return skipped("source_file_missing", path=source_path)
        if resolved.suffix.lower() not in {".md", ".markdown", ".txt"}:
            return skipped("source_format_not_text", path=source_path)
        try:
            content = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            self._clear_source_chunks(source_id)
            raise RuntimeError(
                f"Unable to index source text for {source_id}: {type(exc).__name__}: {exc}"
            ) from exc
        chunks = _source_text_chunks(content)
        now = _now()
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                "delete from source_chunks_fts where source_id = ?",
                (source_id,),
            )
            conn.execute("delete from source_chunks where source_id = ?", (source_id,))
            for chunk_index, chunk in enumerate(chunks):
                chunk_id = "chunk_" + hashlib.sha256(
                    f"{source_id}:{chunk_index}:{chunk['content_hash']}".encode("utf-8")
                ).hexdigest()[:24]
                conn.execute(
                    """
                    insert into source_chunks(
                      id, source_id, chunk_index, title, locator, content, content_hash,
                      org_id, project_id, topic_id, author_id, sensitivity,
                      created_at, updated_at
                    )
                    values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        source_id,
                        chunk_index,
                        str(source.get("title") or source_id),
                        chunk["locator"],
                        chunk["content"],
                        chunk["content_hash"],
                        str(source.get("org_id") or ""),
                        str(source.get("project_id") or ""),
                        source.get("topic_id") or None,
                        str(source.get("author_id") or ""),
                        str(source.get("sensitivity") or ""),
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    insert into source_chunks_fts(id, source_id, title, content)
                    values(?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        source_id,
                        _fts_index_text(str(source.get("title") or source_id)),
                        _fts_index_text(chunk["content"]),
                    ),
                )
            conn.commit()
        return {
            "status": "indexed",
            "source_id": source_id,
            "chunk_count": len(chunks),
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "path": source_path,
        }

    def _clear_source_chunks(self, source_id: str) -> None:
        if not source_id:
            return
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                "delete from source_chunks_fts where source_id = ?",
                (source_id,),
            )
            conn.execute(
                "delete from source_chunks where source_id = ?",
                (source_id,),
            )
            conn.commit()

    def _record_source_chunk_index_status(
        self,
        status: dict[str, Any],
    ) -> None:
        source_id = str(status.get("source_id") or "")
        if not source_id:
            return
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                """
                insert into source_chunk_index_status(
                  source_id, path, status, reason, content_hash,
                  chunk_count, updated_at
                )
                values(?, ?, ?, ?, ?, ?, ?)
                on conflict(source_id) do update set
                  path=excluded.path,
                  status=excluded.status,
                  reason=excluded.reason,
                  content_hash=excluded.content_hash,
                  chunk_count=excluded.chunk_count,
                  updated_at=excluded.updated_at
                """,
                (
                    source_id,
                    str(status.get("path") or ""),
                    str(status.get("status") or "skipped"),
                    str(status.get("reason") or ""),
                    str(status.get("content_hash") or ""),
                    int(status.get("chunk_count") or 0),
                    _now(),
                ),
            )
            conn.commit()

    def _backfill_source_chunk_index(self) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select sources.*
                from sources
                left join source_chunk_index_status index_status
                  on index_status.source_id = sources.id
                where index_status.source_id is null
                   or sources.updated_at > index_status.updated_at
                order by sources.updated_at, sources.id
                """
            ).fetchall()
        for row in rows:
            source = _source_row(row)
            status = self._index_source_chunks(source["payload"])
            self._record_source_chunk_index_status(status)

    def enqueue_ingestion_job(
        self,
        *,
        source_id: str,
        input_kind: str,
        content_hash: str,
        processor_version: str,
        input_path: str,
        actor: Any,
        max_attempts: int = 3,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        source = self.get_source(source_id)
        if source is None:
            raise KeyError(f"Unknown source: {source_id}")
        if input_kind not in {
            "auto",
            "minutes",
            "transcript",
            "project_document",
            "three_list_tasks",
            "three_list_issues",
            "work_logs",
        }:
            raise ValueError(f"Unsupported input_kind: {input_kind}")
        if not content_hash.strip() or not processor_version.strip():
            raise ValueError("content_hash and processor_version are required")
        if not input_path.strip():
            raise ValueError("An immutable input_path is required")
        actor_id = _actor_id(actor)
        actor_org_id = _actor_org_id(actor)
        if not actor_id:
            raise ValueError("actor id is required")
        if actor_org_id and actor_org_id != source["org_id"]:
            raise ValueError("Actor and source must belong to the same org")

        digest = hashlib.sha256(
            f"{source_id}\0{content_hash}\0{processor_version}".encode("utf-8")
        ).hexdigest()[:20]
        job_id = f"ingest_{digest}"
        now = _now()
        job_payload = {
            "source_id": source_id,
            "input_kind": input_kind,
            "content_hash": content_hash,
            "processor_version": processor_version,
            "input_path": input_path,
            **(payload or {}),
        }
        created = False
        with _STORE_LOCK, self._connect() as conn:
            cursor = conn.execute(
                """
                insert or ignore into ingestion_jobs(
                  id, source_id, org_id, project_id, topic_id, author_id,
                  sensitivity, actor_id, input_kind, content_hash,
                  processor_version, input_path, status, stage, max_attempts,
                  created_at, updated_at, payload
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    source_id,
                    source["org_id"],
                    source["project_id"],
                    source.get("topic_id"),
                    source["author_id"],
                    source["sensitivity"],
                    actor_id,
                    input_kind,
                    content_hash,
                    processor_version,
                    input_path,
                    max(1, int(max_attempts)),
                    now,
                    now,
                    _json(job_payload),
                ),
            )
            created = cursor.rowcount == 1
            if created:
                conn.execute(
                    """
                    update sources
                    set input_kind = ?, current_ingestion_job_id = ?, updated_at = ?
                    where id = ?
                    """,
                    (input_kind, job_id, now, source_id),
                )
            row = conn.execute(
                """
                select * from ingestion_jobs
                where source_id = ? and content_hash = ? and processor_version = ?
                """,
                (source_id, content_hash, processor_version),
            ).fetchone()
            conn.commit()
        if created:
            self._append_event(
                "ingestion_job",
                job_id,
                "ingestion_job_queued",
                {"source_id": source_id, "content_hash": content_hash, "processor_version": processor_version},
            )
        assert row is not None
        return _ingestion_job_row(row)

    def get_ingestion_job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("select * from ingestion_jobs where id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _ingestion_job_row(row)

    def list_ingestion_jobs(
        self,
        *,
        project_id: str | None = None,
        statuses: set[str] | None = None,
        actor: User | None = None,
        ctx: AccessContext | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"status in ({placeholders})")
            params.extend(sorted(statuses))
        sql = "select * from ingestion_jobs"
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by created_at desc, id desc"
        bounded_limit = max(1, min(int(limit), 500))
        if actor is None:
            sql += " limit ?"
            params.append(bounded_limit)
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        jobs = [_ingestion_job_row(row) for row in rows]
        if actor is not None:
            if ctx is None:
                raise ValueError("AccessContext is required when filtering jobs for an actor")
            jobs = visible_filter(actor, jobs, ctx)
        return jobs[:bounded_limit]

    def claim_next_ingestion_job(
        self,
        *,
        worker_id: str,
        project_id: str | None = None,
        lease_seconds: int = 300,
    ) -> dict[str, Any] | None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        now = _now()
        lease_expires_at = _iso_after_seconds(lease_seconds)
        with _STORE_LOCK, self._connect() as conn:
            conn.execute("begin immediate")
            clauses = ["status = 'queued'", "attempts < max_attempts", "(retry_at = '' or retry_at <= ?)"]
            params: list[Any] = [now]
            if project_id:
                clauses.append("project_id = ?")
                params.append(project_id)
            row = conn.execute(
                "select * from ingestion_jobs where "
                + " and ".join(clauses)
                + " order by created_at, id limit 1",
                tuple(params),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                """
                update ingestion_jobs
                set status = 'running', stage = 'starting', attempts = attempts + 1,
                    lease_owner = ?, lease_expires_at = ?, error = '', updated_at = ?
                where id = ? and status = 'queued'
                """,
                (worker_id, lease_expires_at, now, row["id"]),
            )
            claimed = conn.execute("select * from ingestion_jobs where id = ?", (row["id"],)).fetchone()
            conn.commit()
        assert claimed is not None
        result = _ingestion_job_row(claimed)
        self._append_event(
            "ingestion_job",
            result["id"],
            "ingestion_job_claimed",
            {"worker_id": worker_id, "attempt": result["attempts"]},
        )
        return result

    def update_ingestion_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {
            "status", "stage", "total_chunks", "processed_chunks", "candidate_count",
            "delta_new", "delta_updated", "delta_conflict", "delta_resolved",
            "delta_auto_merged", "retry_at", "lease_owner", "lease_expires_at",
            "minutes_path", "minutes_hash", "error", "payload",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unsupported ingestion job fields: {', '.join(sorted(unknown))}")
        if not changes:
            return self.get_ingestion_job(job_id)
        current = self.get_ingestion_job(job_id)
        if "payload" in changes:
            changes["payload"] = _json({**current["payload"], **dict(changes["payload"] or {})})
        assignments = [f"{field} = ?" for field in changes]
        values = list(changes.values())
        assignments.append("updated_at = ?")
        values.append(_now())
        values.append(job_id)
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                f"update ingestion_jobs set {', '.join(assignments)} where id = ?",
                tuple(values),
            )
            if conn.total_changes == 0:
                raise KeyError(job_id)
            conn.commit()
        return self.get_ingestion_job(job_id)

    def complete_ingestion_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        result = self.update_ingestion_job(
            job_id,
            status="completed",
            stage="completed",
            error="",
            lease_owner="",
            lease_expires_at="",
            **changes,
        )
        self._append_event("ingestion_job", job_id, "ingestion_job_completed", result)
        return result

    def fail_ingestion_job(self, job_id: str, error: str) -> dict[str, Any]:
        if not error.strip():
            raise ValueError("A concrete ingestion error is required")
        result = self.update_ingestion_job(
            job_id,
            status="failed",
            stage="failed",
            error=error,
            lease_owner="",
            lease_expires_at="",
        )
        self._append_event("ingestion_job", job_id, "ingestion_job_failed", {"error": error})
        return result

    def retry_ingestion_job(self, job_id: str) -> dict[str, Any]:
        current = self.get_ingestion_job(job_id)
        if current["status"] not in {"failed", "interrupted"}:
            raise ValueError(f"Only failed or interrupted jobs can be retried: {current['status']}")
        if current["attempts"] >= current["max_attempts"]:
            raise ValueError("Ingestion job exhausted its retry limit")
        result = self.update_ingestion_job(
            job_id,
            status="queued",
            stage="queued",
            retry_at="",
            error="",
            lease_owner="",
            lease_expires_at="",
        )
        self._append_event("ingestion_job", job_id, "ingestion_job_retried", {})
        return result

    def requeue_interrupted_ingestion_jobs(self) -> int:
        with _STORE_LOCK, self._connect() as conn:
            rows = conn.execute(
                "select id, lease_owner from ingestion_jobs where status = 'running'"
            ).fetchall()
            now = _now()
            for row in rows:
                conn.execute(
                    """
                    update ingestion_jobs
                    set status = 'queued', stage = 'queued', lease_owner = '',
                        lease_expires_at = '', error = 'Recovered after process restart', updated_at = ?
                    where id = ?
                    """,
                    (now, row["id"]),
                )
            conn.commit()
        for row in rows:
            self._append_event(
                "ingestion_job",
                row["id"],
                "ingestion_job_recovered",
                {"previous_worker": row["lease_owner"]},
            )
        return len(rows)

    def replace_ingestion_chunks(self, job_id: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        job = self.get_ingestion_job(job_id)
        indexes = [int(chunk["chunk_index"]) for chunk in chunks]
        if len(indexes) != len(set(indexes)):
            raise ValueError("chunk_index must be unique within a job")
        with _STORE_LOCK, self._connect() as conn:
            conn.execute("delete from ingestion_chunks where job_id = ?", (job_id,))
            now = _now()
            for chunk in chunks:
                start_char = int(chunk["start_char"])
                end_char = int(chunk["end_char"])
                if start_char < 0 or end_char <= start_char:
                    raise ValueError("Chunk offsets must be a non-empty half-open range")
                chunk_index = int(chunk["chunk_index"])
                chunk_id = f"{job_id}_chunk_{chunk_index:05d}"
                payload = dict(chunk)
                conn.execute(
                    """
                    insert into ingestion_chunks(
                      id, job_id, source_id, org_id, project_id, topic_id, author_id,
                      sensitivity, chunk_index, start_char, end_char, content_hash,
                      status, attempts, summary, error, payload, updated_at
                    ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, '', '', ?, ?)
                    """,
                    (
                        chunk_id,
                        job_id,
                        job["source_id"],
                        job["org_id"],
                        job["project_id"],
                        job.get("topic_id"),
                        job["author_id"],
                        job["sensitivity"],
                        chunk_index,
                        start_char,
                        end_char,
                        str(chunk["content_hash"]),
                        _json(payload),
                        now,
                    ),
                )
            conn.execute(
                """
                update ingestion_jobs
                set total_chunks = ?, processed_chunks = 0, stage = 'chunked', updated_at = ?
                where id = ?
                """,
                (len(chunks), now, job_id),
            )
            conn.commit()
        return self.list_ingestion_chunks(job_id)

    def list_ingestion_chunks(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "select * from ingestion_chunks where job_id = ? order by chunk_index",
                (job_id,),
            ).fetchall()
        return [_ingestion_chunk_row(row) for row in rows]

    def update_ingestion_chunk(
        self,
        job_id: str,
        chunk_index: int,
        *,
        status: str,
        summary: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"queued", "running", "completed", "failed"}:
            raise ValueError(f"Unsupported chunk status: {status}")
        now = _now()
        with _STORE_LOCK, self._connect() as conn:
            row = conn.execute(
                "select * from ingestion_chunks where job_id = ? and chunk_index = ?",
                (job_id, int(chunk_index)),
            ).fetchone()
            if row is None:
                raise KeyError(f"{job_id}:{chunk_index}")
            attempts = int(row["attempts"]) + (1 if status == "running" else 0)
            conn.execute(
                """
                update ingestion_chunks
                set status = ?, attempts = ?, summary = ?, error = ?, updated_at = ?
                where job_id = ? and chunk_index = ?
                """,
                (
                    status,
                    attempts,
                    row["summary"] if summary is None else summary,
                    row["error"] if error is None else error,
                    now,
                    job_id,
                    int(chunk_index),
                ),
            )
            processed = conn.execute(
                "select count(*) from ingestion_chunks where job_id = ? and status = 'completed'",
                (job_id,),
            ).fetchone()[0]
            conn.execute(
                "update ingestion_jobs set processed_chunks = ?, updated_at = ? where id = ?",
                (processed, now, job_id),
            )
            updated = conn.execute(
                "select * from ingestion_chunks where job_id = ? and chunk_index = ?",
                (job_id, int(chunk_index)),
            ).fetchone()
            conn.commit()
        assert updated is not None
        return _ingestion_chunk_row(updated)

    def upsert_memory_thread(
        self,
        *,
        thread_id: str,
        category: str,
        thread_key: str,
        title: str,
        tags: dict[str, Any],
        actor: Any,
        first_seen_at: str,
        status: str = "active",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scope = _validate_ingest_tags(tags, actor)
        if not thread_id.strip() or not category.strip() or not thread_key.strip() or not title.strip():
            raise ValueError("thread_id, category, thread_key and title are required")
        if not first_seen_at.strip():
            raise ValueError("first_seen_at is required")
        now_payload = {
            "thread_key": thread_key,
            "title": title,
            "migration_origin": (payload or {}).get("migration_origin", "runtime"),
            **(payload or {}),
        }
        created = False
        with _STORE_LOCK, self._connect() as conn:
            existing = conn.execute(
                """
                select * from memory_threads
                where org_id = ? and project_id = ? and ifnull(topic_id, '') = ifnull(?, '')
                  and sensitivity = ? and category = ? and thread_key = ?
                """,
                (
                    scope["org_id"],
                    scope["project_id"],
                    scope.get("topic_id"),
                    scope["sensitivity"],
                    category,
                    thread_key,
                ),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    insert into memory_threads(
                      id, org_id, project_id, topic_id, author_id, sensitivity,
                      category, thread_key, title, status, current_item_id,
                      first_seen_at, last_seen_at, payload
                    ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, null, ?, ?, ?)
                    """,
                    (
                        thread_id,
                        scope["org_id"],
                        scope["project_id"],
                        scope.get("topic_id"),
                        scope["author_id"],
                        scope["sensitivity"],
                        category,
                        thread_key,
                        title,
                        status,
                        first_seen_at,
                        first_seen_at,
                        _json(now_payload),
                    ),
                )
                created = True
                selected_id = thread_id
            else:
                selected_id = existing["id"]
                conn.execute(
                    """
                    update memory_threads
                    set title = ?, payload = ?
                    where id = ?
                    """,
                    (title, _json({**_loads(existing["payload"], {}), **now_payload}), selected_id),
                )
            row = conn.execute("select * from memory_threads where id = ?", (selected_id,)).fetchone()
            conn.commit()
        if created:
            self._append_event("memory_thread", selected_id, "memory_thread_created", now_payload)
        assert row is not None
        return _memory_thread_row(row)

    def get_memory_thread(self, thread_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("select * from memory_threads where id = ?", (thread_id,)).fetchone()
        if row is None:
            raise KeyError(thread_id)
        return _memory_thread_row(row)

    def list_memory_threads(
        self,
        *,
        project_id: str | None = None,
        actor: User | None = None,
        ctx: AccessContext | None = None,
    ) -> list[dict[str, Any]]:
        sql = "select * from memory_threads"
        params: tuple[Any, ...] = ()
        if project_id:
            sql += " where project_id = ?"
            params = (project_id,)
        sql += " order by last_seen_at desc, id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        threads = [_memory_thread_row(row) for row in rows]
        if actor is not None:
            if ctx is None:
                raise ValueError("AccessContext is required when filtering threads for an actor")
            threads = visible_filter(actor, threads, ctx)
        return threads

    def backfill_confirmed_items_as_threads(
        self,
        *,
        project_id: str | None = None,
        apply: bool = False,
    ) -> dict[str, Any]:
        sql = "select * from items where status = 'confirmed'"
        params: tuple[Any, ...] = ()
        if project_id:
            sql += " and project_id = ?"
            params = (project_id,)
        sql += " order by project_id, id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        planned: list[dict[str, Any]] = []
        already_threaded = 0
        for row in rows:
            payload = _item_payload_from_row(row)
            if payload.get("thread_id"):
                already_threaded += 1
                continue
            item = _inspection_item_from_dict(payload)
            digest = hashlib.sha256(f"{item.project_id}:{item.item_id}".encode("utf-8")).hexdigest()[:20]
            effective_at = item.effective_at or row["updated_at"] or row["created_at"] or _now()
            source_id = str(row["source_id"] or "")
            if not source_id and item.evidence_refs:
                source_id = item.evidence_refs[0].source_doc_id
            planned.append(
                {
                    "item": item,
                    "thread_id": f"thread_migration_{digest}",
                    "thread_key": f"migration-{digest}",
                    "effective_at": effective_at,
                    "source_id": source_id or f"migration_item:{item.item_id}",
                    "created_at": row["created_at"] or effective_at,
                    "updated_at": row["updated_at"] or effective_at,
                }
            )

        result = {
            "mode": "apply" if apply else "dry_run",
            "project_id": project_id or "*",
            "matched_confirmed": len(rows),
            "already_threaded": already_threaded,
            "planned": len(planned),
            "created": 0,
            "item_ids": [entry["item"].item_id for entry in planned],
        }
        if not apply:
            return result

        for entry in planned:
            item = entry["item"]
            tags = {field: getattr(item, field) for field in ACCESS_TAG_FIELDS}
            thread = self.upsert_memory_thread(
                thread_id=entry["thread_id"],
                category=item.category,
                thread_key=entry["thread_key"],
                title=item.title,
                tags=tags,
                actor={"id": item.author_id, "org_id": item.org_id},
                first_seen_at=entry["effective_at"],
                payload={
                    "migration_origin": "migration_backfill",
                    "semantic_merge": "not_inferred",
                    "source_item_id": item.item_id,
                },
            )
            item.thread_id = thread["id"]
            item.thread_title = item.title
            item.thread_key = entry["thread_key"]
            item.thread_event = "new"
            item.effective_at = entry["effective_at"]
            item.review_required = False
            item.changed_fields = []
            self.link_memory_thread_item(
                thread_id=thread["id"],
                item_id=item.item_id,
                source_id=entry["source_id"],
                relation="new",
                effective_at=entry["effective_at"],
                review_required=False,
            )
            item_payload = {
                **to_plain(item),
                "created_at": entry["created_at"],
                "updated_at": entry["updated_at"],
            }
            with _STORE_LOCK, self._connect() as conn:
                conn.execute(
                    "update items set payload = ? where id = ? and status = 'confirmed'",
                    (_json(item_payload), item.item_id),
                )
                conn.commit()
            result["created"] += 1

        if planned:
            self._append_event(
                "project",
                project_id or "all_projects",
                "memory_thread_backfill_completed",
                {
                    "migration_origin": "migration_backfill",
                    "semantic_merge": "not_inferred",
                    "created": result["created"],
                    "item_ids": result["item_ids"],
                },
            )
        return result

    def link_memory_thread_item(
        self,
        *,
        thread_id: str,
        item_id: str,
        source_id: str,
        ingestion_job_id: str = "",
        relation: str,
        effective_at: str,
        review_required: bool,
    ) -> dict[str, Any]:
        if relation not in {"new", "update", "reinforce", "conflict", "resolved", "supersede"}:
            raise ValueError(f"Unsupported thread relation: {relation}")
        thread = self.get_memory_thread(thread_id)
        item = self.get_item(item_id)
        for field in ACCESS_TAG_FIELDS:
            item_value = getattr(item, field)
            thread_value = thread.get(field)
            if (item_value or None) != (thread_value or None):
                raise ValueError(f"Item and thread access scope differ: {field}")
        if item.thread_id and item.thread_id != thread_id:
            raise ValueError("Item is already assigned to another thread")
        evidence_refs = [to_plain(ref) for ref in item.evidence_refs]
        now = _now()
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                """
                insert into memory_thread_items(
                  thread_id, item_id, ingestion_job_id, source_id, org_id, project_id,
                  topic_id, author_id, sensitivity, relation, supersedes_item_id,
                  effective_at, review_required, review_status, changed_fields,
                  evidence_refs, created_at, updated_at
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(thread_id, item_id) do update set
                  ingestion_job_id=excluded.ingestion_job_id,
                  source_id=excluded.source_id,
                  relation=excluded.relation,
                  supersedes_item_id=excluded.supersedes_item_id,
                  effective_at=excluded.effective_at,
                  review_required=excluded.review_required,
                  review_status=excluded.review_status,
                  changed_fields=excluded.changed_fields,
                  evidence_refs=excluded.evidence_refs,
                  updated_at=excluded.updated_at
                """,
                (
                    thread_id,
                    item_id,
                    ingestion_job_id,
                    source_id,
                    item.org_id,
                    item.project_id,
                    item.topic_id,
                    item.author_id,
                    item.sensitivity,
                    relation,
                    item.supersedes_item_id or "",
                    effective_at,
                    1 if review_required else 0,
                    item.status.value,
                    _json(item.changed_fields),
                    _json(evidence_refs),
                    now,
                    now,
                ),
            )
            conn.commit()
        self.refresh_memory_thread(thread_id)
        linked = [row for row in self.list_memory_thread_items(thread_id) if row["item_id"] == item_id]
        assert linked
        return linked[0]

    def list_memory_thread_items(self, thread_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select mti.*, i.status as item_status
                from memory_thread_items mti
                left join items i on i.id = mti.item_id
                where mti.thread_id = ?
                order by mti.effective_at, mti.created_at, mti.item_id
                """,
                (thread_id,),
            ).fetchall()
        return [_memory_thread_item_row(row) for row in rows]

    def link_memory_thread_source(
        self,
        *,
        thread_id: str,
        ingestion_job_id: str,
        source_id: str,
        relation: str,
        effective_at: str,
        evidence_refs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if relation not in {"new", "update", "reinforce", "conflict", "resolved", "supersede"}:
            raise ValueError(f"Unsupported thread relation: {relation}")
        thread = self.get_memory_thread(thread_id)
        source = self.get_source(source_id)
        if source is None:
            raise KeyError(source_id)
        for field in ACCESS_TAG_FIELDS:
            if (source.get(field) or None) != (thread.get(field) or None):
                raise ValueError(f"Source and thread access scope differ: {field}")
        now = _now()
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                """
                insert into memory_thread_sources(
                  thread_id, ingestion_job_id, source_id, org_id, project_id,
                  topic_id, author_id, sensitivity, relation, effective_at,
                  evidence_refs, created_at, updated_at
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(thread_id, ingestion_job_id, source_id) do update set
                  relation=excluded.relation,
                  effective_at=excluded.effective_at,
                  evidence_refs=excluded.evidence_refs,
                  updated_at=excluded.updated_at
                """,
                (
                    thread_id,
                    ingestion_job_id,
                    source_id,
                    source["org_id"],
                    source["project_id"],
                    source.get("topic_id"),
                    source["author_id"],
                    source["sensitivity"],
                    relation,
                    effective_at,
                    _json(evidence_refs),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                update memory_threads
                set last_seen_at = case when last_seen_at < ? then ? else last_seen_at end
                where id = ?
                """,
                (effective_at, effective_at, thread_id),
            )
            row = conn.execute(
                """
                select * from memory_thread_sources
                where thread_id = ? and ingestion_job_id = ? and source_id = ?
                """,
                (thread_id, ingestion_job_id, source_id),
            ).fetchone()
            conn.commit()
        assert row is not None
        return _memory_thread_source_row(row)

    def list_memory_thread_sources(self, thread_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select * from memory_thread_sources
                where thread_id = ?
                order by effective_at, created_at, ingestion_job_id, source_id
                """,
                (thread_id,),
            ).fetchall()
        return [_memory_thread_source_row(row) for row in rows]

    def refresh_memory_thread(self, thread_id: str) -> dict[str, Any]:
        thread = self.get_memory_thread(thread_id)
        with _STORE_LOCK, self._connect() as conn:
            all_row = conn.execute(
                """
                select effective_at from memory_thread_items
                where thread_id = ? order by effective_at desc, item_id desc limit 1
                """,
                (thread_id,),
            ).fetchone()
            current = conn.execute(
                """
                select mti.item_id, mti.effective_at, mti.relation
                from memory_thread_items mti
                join items i on i.id = mti.item_id
                where mti.thread_id = ? and i.status = 'confirmed'
                order by mti.effective_at desc, mti.created_at desc, mti.item_id desc
                limit 1
                """,
                (thread_id,),
            ).fetchone()
            current_item_id = current["item_id"] if current else None
            lifecycle = "resolved" if current and current["relation"] == "resolved" else "active"
            last_seen_at = all_row["effective_at"] if all_row else thread["first_seen_at"]
            conn.execute(
                """
                update memory_threads
                set current_item_id = ?, status = ?, last_seen_at = ?
                where id = ?
                """,
                (current_item_id, lifecycle, last_seen_at, thread_id),
            )
            conn.execute(
                """
                update memory_thread_items
                set review_status = coalesce((select status from items where items.id = memory_thread_items.item_id), review_status),
                    updated_at = ?
                where thread_id = ?
                """,
                (_now(), thread_id),
            )
            conn.commit()
        return self.get_memory_thread(thread_id)

    def append_event(self, entity: str, entity_id: str, action: str, payload: dict[str, Any]) -> None:
        self._append_event(entity, entity_id, action, payload)

    def list_events(
        self,
        *,
        entity: str | None = None,
        entity_id: str | None = None,
        action: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "select * from events"
        clauses: list[str] = []
        params: list[Any] = []
        if entity is not None:
            clauses.append("entity = ?")
            params.append(entity)
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by created_at, id"
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [_event_row(row) for row in rows]

    def upsert_org(self, org_id: str, name: str) -> dict[str, Any]:
        now = _now()
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                """
                insert into orgs(id, name, created_at)
                values(?, ?, ?)
                on conflict(id) do update set name=excluded.name
                """,
                (org_id, name, now),
            )
            conn.commit()
        return {"id": org_id, "name": name, "created_at": now}

    def upsert_user(self, user_id: str, org_id: str, name: str, feishu_id: str = "") -> dict[str, Any]:
        now = _now()
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                """
                insert into users(id, org_id, name, feishu_id, created_at)
                values(?, ?, ?, ?, ?)
                on conflict(id) do update set
                  org_id=excluded.org_id,
                  name=excluded.name,
                  feishu_id=excluded.feishu_id
                """,
                (user_id, org_id, name, feishu_id, now),
            )
            conn.commit()
        return {"id": user_id, "org_id": org_id, "name": name, "feishu_id": feishu_id, "created_at": now}

    def get_access_user(self, user_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("select id, org_id, name, coalesce(feishu_id, '') as feishu_id from users where id = ?", (user_id,)).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    select person_id as id, org_id, name, '' as feishu_id
                    from person_entities where person_id = ?
                    order by project_id limit 1
                    """,
                    (user_id,),
                ).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "org_id": row["org_id"], "name": row["name"], "feishu_id": row["feishu_id"]}

    def list_project_access_users(self, project_id: str) -> list[dict[str, Any]]:
        """Return concrete login identities available in one project."""
        with self._connect() as conn:
            people = conn.execute(
                """
                select person_id, org_id, name, identity_status
                from person_entities where project_id = ?
                order by name, person_id
                """,
                (project_id,),
            ).fetchall()
            if people:
                assignment_rows = conn.execute(
                    """
                    select person_id, group_name, role, path
                    from person_assignments where project_id = ?
                    order by person_id, group_name, role, path
                    """,
                    (project_id,),
                ).fetchall()
                assignments_by_person: dict[str, list[sqlite3.Row]] = {}
                for assignment in assignment_rows:
                    assignments_by_person.setdefault(str(assignment["person_id"]), []).append(assignment)
                result: list[dict[str, Any]] = []
                for person in people:
                    person_id = str(person["person_id"])
                    assignments = assignments_by_person.get(person_id, [])
                    groups = _unique_people_groups(assignments)
                    result.append({
                        "user_id": person_id,
                        "org_id": str(person["org_id"]),
                        "name": str(person["name"]),
                        "role": _infer_people_access_role(assignments),
                        "identity_status": str(person["identity_status"]),
                        "topics": [
                            {"topic_id": f"people_group:{group}", "name": group}
                            for group in groups
                        ],
                    })
                return sorted(
                    result,
                    key=lambda value: (
                        {"pmo": 0, "professional_lead": 1, "topic_lead": 2, "exec": 3}.get(value["role"], 4),
                        value["name"],
                        value["user_id"],
                    ),
                )

            rows = conn.execute(
                """
                select
                  u.id as user_id,
                  u.org_id,
                  u.name,
                  pm.role,
                  tm.topic_id,
                  t.name as topic_name
                from project_members pm
                join users u on u.id = pm.user_id and u.org_id = pm.org_id
                left join topic_members tm
                  on tm.project_id = pm.project_id and tm.user_id = pm.user_id
                left join topics t on t.id = tm.topic_id and t.project_id = pm.project_id
                where pm.project_id = ?
                order by
                  case pm.role
                    when 'pm' then 0
                    when 'pmo' then 1
                    when 'professional_lead' then 2
                    when 'topic_lead' then 3
                    when 'exec' then 4
                    else 5
                  end,
                  u.name,
                  u.id,
                  t.name,
                  tm.topic_id
                """,
                (project_id,),
            ).fetchall()

        users: dict[str, dict[str, Any]] = {}
        for row in rows:
            user = users.setdefault(
                str(row["user_id"]),
                {
                    "user_id": str(row["user_id"]),
                    "org_id": str(row["org_id"]),
                    "name": str(row["name"]),
                    "role": str(row["role"]),
                    "topics": [],
                },
            )
            if row["topic_id"]:
                user["topics"].append({
                    "topic_id": str(row["topic_id"]),
                    "name": str(row["topic_name"] or row["topic_id"]),
                })
        return list(users.values())

    def exact_project_user_id_by_name(self, project_id: str, name: str) -> str:
        normalized = str(name or "").strip()
        if not normalized:
            return ""
        with self._connect() as conn:
            rows = conn.execute(
                """
                select u.id
                from users u
                join project_members pm on pm.user_id = u.id
                where pm.project_id = ? and trim(u.name) = ?
                order by u.id
                """,
                (project_id, normalized),
            ).fetchall()
        return str(rows[0]["id"]) if len(rows) == 1 else ""

    def access_context_for_actor(self, actor_id: str) -> AccessContext:
        with self._connect() as conn:
            project_rows = conn.execute(
                "select project_id, role from project_members where user_id = ?",
                (actor_id,),
            ).fetchall()
            virtual_project_roles: dict[tuple[str, str], str] = {}
            if not project_rows:
                people_projects = conn.execute(
                    "select project_id from person_entities where person_id = ? order by project_id",
                    (actor_id,),
                ).fetchall()
                for project in people_projects:
                    assignment_rows = conn.execute(
                        """
                        select group_name, role, path from person_assignments
                        where project_id = ? and person_id = ?
                        order by group_name, role, path
                        """,
                        (project["project_id"], actor_id),
                    ).fetchall()
                    virtual_project_roles[(actor_id, str(project["project_id"]))] = _infer_people_access_role(assignment_rows)
            project_ids = sorted({
                str(row["project_id"]) for row in project_rows
            } | {
                project_id for _, project_id in virtual_project_roles
            })
            reporting_rows: list[sqlite3.Row] = []
            if project_ids:
                placeholders = ",".join("?" for _ in project_ids)
                reporting_rows = conn.execute(
                    f"select project_id, user_id, manager_id from project_members where project_id in ({placeholders})",
                    tuple(project_ids),
                ).fetchall()
            topic_rows = conn.execute("select topic_id, user_id from topic_members").fetchall()
        return AccessContext(
            project_roles={
                **{(actor_id, row["project_id"]): row["role"] for row in project_rows},
                **virtual_project_roles,
            },
            topic_members=_topic_members_from_rows(topic_rows),
            item_blocklists={},
            reporting_managers={
                (str(row["user_id"]), str(row["project_id"])): str(row["manager_id"] or "")
                for row in reporting_rows
            },
        )

    def default_project_id_for_actor(self, actor_id: str, org_id: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute(
                """
                select pm.project_id
                from project_members pm
                join projects p on p.id = pm.project_id
                where pm.user_id = ? and (? = '' or p.org_id = ?)
                order by case pm.role when 'pm' then 0 when 'pmo' then 1 else 2 end, pm.project_id
                limit 1
                """,
                (actor_id, org_id, org_id),
            ).fetchone()
            if row:
                return str(row["project_id"])
            row = conn.execute(
                """
                select project_id from person_entities
                where person_id = ? and (? = '' or org_id = ?)
                order by project_id limit 1
                """,
                (actor_id, org_id, org_id),
            ).fetchone()
            if row:
                return str(row["project_id"])
            row = conn.execute(
                "select id from projects where (? = '' or org_id = ?) order by created_at, id limit 1",
                (org_id, org_id),
            ).fetchone()
        return str(row["id"]) if row else "project_mvp"

    def upsert_project(self, project_id: str, org_id: str, name: str, owner_id: str) -> dict[str, Any]:
        now = _now()
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                """
                insert into projects(id, org_id, name, owner_id, created_at)
                values(?, ?, ?, ?, ?)
                on conflict(id) do update set
                  org_id=excluded.org_id,
                  name=excluded.name,
                  owner_id=excluded.owner_id
                """,
                (project_id, org_id, name, owner_id, now),
            )
            conn.commit()
        return {"id": project_id, "org_id": org_id, "name": name, "owner_id": owner_id, "created_at": now}

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("select * from projects where id = ?", (project_id,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "org_id": row["org_id"],
            "name": row["name"],
            "owner_id": row["owner_id"],
            "created_at": row["created_at"],
        }

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("select * from projects order by created_at, id").fetchall()
        return [
            {
                "id": row["id"],
                "org_id": row["org_id"],
                "name": row["name"],
                "owner_id": row["owner_id"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_project_config(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "select * from project_configs where project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "project_id": row["project_id"],
            "org_id": row["org_id"],
            "source_profile": row["source_profile"],
            "milestone_payload": _loads(row["milestone_payload"], {}),
            "migration_origin": row["migration_origin"],
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
        }

    def upsert_project_config(
        self,
        project_id: str,
        *,
        org_id: str,
        source_profile: str,
        milestone_payload: dict[str, Any],
        actor_id: str,
        migration_origin: str = "runtime",
    ) -> dict[str, Any]:
        if not self.get_project(project_id):
            raise KeyError(f"Unknown project: {project_id}")
        now = _now()
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                """
                insert into project_configs(
                  project_id, org_id, source_profile, milestone_payload,
                  migration_origin, updated_by, updated_at
                )
                values(?, ?, ?, ?, ?, ?, ?)
                on conflict(project_id) do update set
                  org_id=excluded.org_id,
                  source_profile=excluded.source_profile,
                  milestone_payload=excluded.milestone_payload,
                  migration_origin=excluded.migration_origin,
                  updated_by=excluded.updated_by,
                  updated_at=excluded.updated_at
                """,
                (
                    project_id,
                    org_id,
                    source_profile or "generic",
                    _json(milestone_payload),
                    migration_origin,
                    actor_id,
                    now,
                ),
            )
            conn.commit()
        result = self.get_project_config(project_id)
        assert result is not None
        self._append_event(
            "project_config",
            project_id,
            "project_config_updated",
            {
                "org_id": org_id,
                "actor_id": actor_id,
                "migration_origin": migration_origin,
                "milestone_id": milestone_payload.get("milestone_id", ""),
            },
        )
        return result

    def upsert_project_member(
        self,
        project_id: str,
        user_id: str,
        role: str,
        org_id: str | None = None,
        manager_id: str | None = None,
    ) -> dict[str, Any]:
        scoped_org_id = org_id or self._project_org_id(project_id)
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                """
                insert into project_members(id, org_id, project_id, user_id, role, manager_id)
                values(?, ?, ?, ?, ?, ?)
                on conflict(project_id, user_id) do update set
                  id=excluded.id,
                  org_id=excluded.org_id,
                  role=excluded.role,
                  manager_id=excluded.manager_id
                """,
                (user_id, scoped_org_id, project_id, user_id, role, manager_id),
            )
            conn.commit()
        return {
            "id": user_id,
            "org_id": scoped_org_id,
            "project_id": project_id,
            "user_id": user_id,
            "role": role,
            "manager_id": manager_id,
        }

    def upsert_topic(self, topic_id: str, project_id: str, name: str, created_by: str, org_id: str | None = None) -> dict[str, Any]:
        scoped_org_id = org_id or self._project_org_id(project_id)
        now = _now()
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                """
                insert into topics(id, org_id, project_id, name, created_by, created_at)
                values(?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                  org_id=excluded.org_id,
                  project_id=excluded.project_id,
                  name=excluded.name,
                  created_by=excluded.created_by
                """,
                (topic_id, scoped_org_id, project_id, name, created_by, now),
            )
            conn.commit()
        return {
            "id": topic_id,
            "org_id": scoped_org_id,
            "project_id": project_id,
            "name": name,
            "created_by": created_by,
            "created_at": now,
        }

    def upsert_topic_member(self, topic_id: str, user_id: str, org_id: str | None = None, project_id: str | None = None) -> dict[str, Any]:
        scoped_org_id, scoped_project_id = self._topic_scope(topic_id, org_id=org_id, project_id=project_id)
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                """
                insert into topic_members(org_id, project_id, topic_id, user_id)
                values(?, ?, ?, ?)
                on conflict(topic_id, user_id) do update set
                  org_id=excluded.org_id,
                  project_id=excluded.project_id
                """,
                (scoped_org_id, scoped_project_id, topic_id, user_id),
            )
            conn.commit()
        return {"org_id": scoped_org_id, "project_id": scoped_project_id, "topic_id": topic_id, "user_id": user_id}

    def save_people_profile(self, name: str, profile: dict[str, Any]) -> None:
        with _STORE_LOCK, self._connect() as conn:
            existing = conn.execute("select payload from people where name = ?", (name,)).fetchone()
            payload = _loads(existing["payload"], {}) if existing else {}
            payload["team_profile"] = profile
            conn.execute(
                """
                insert into people(
                  name, org, role, responsibilities, aliases, payload,
                  profile_payload, current_load, avg_completion_days, profile_updated_at
                )
                values(?, '', '', '', '[]', ?, ?, ?, ?, ?)
                on conflict(name) do update set
                  payload=excluded.payload,
                  profile_payload=excluded.profile_payload,
                  current_load=excluded.current_load,
                  avg_completion_days=excluded.avg_completion_days,
                  profile_updated_at=excluded.profile_updated_at
                """,
                (
                    name,
                    _json(payload),
                    _json(profile),
                    int(profile.get("current_load", 0) or 0),
                    profile.get("avg_completion_days"),
                    _now(),
                ),
            )
            conn.commit()

    def list_people_profiles(self) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("select name, profile_payload from people where profile_payload is not null").fetchall()
        return {row["name"]: _loads(row["profile_payload"], {}) for row in rows}

    def replace_people_directory(
        self,
        *,
        org_id: str,
        project_id: str,
        entities: list[dict[str, Any]],
        assignments: list[dict[str, Any]],
    ) -> None:
        with _STORE_LOCK, self._connect() as conn:
            current_entities = [
                _loads(row["payload"], {})
                for row in conn.execute(
                    "select payload from person_entities where org_id = ? and project_id = ? order by person_id",
                    (org_id, project_id),
                ).fetchall()
            ]
            current_assignments = [
                _loads(row["payload"], {})
                for row in conn.execute(
                    "select payload from person_assignments where org_id = ? and project_id = ? order by assignment_id",
                    (org_id, project_id),
                ).fetchall()
            ]
            if (
                sorted(current_entities, key=lambda row: row["person_id"])
                == sorted(entities, key=lambda row: row["person_id"])
                and sorted(current_assignments, key=lambda row: row["assignment_id"])
                == sorted(assignments, key=lambda row: row["assignment_id"])
            ):
                return
            conn.execute("delete from person_assignments where org_id = ? and project_id = ?", (org_id, project_id))
            conn.execute("delete from person_entities where org_id = ? and project_id = ?", (org_id, project_id))
            for entity in entities:
                conn.execute(
                    """
                    insert into person_entities(
                      org_id, project_id, person_id, name, identity_status,
                      source_id, author_id, sensitivity, payload, updated_at
                    ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        org_id,
                        project_id,
                        entity["person_id"],
                        entity["name"],
                        entity["identity_status"],
                        entity["source_id"],
                        entity["author_id"],
                        entity["sensitivity"],
                        _json(entity),
                        _now(),
                    ),
                )
            for assignment in assignments:
                conn.execute(
                    """
                    insert into person_assignments(
                      org_id, project_id, assignment_id, person_id, group_name, role,
                      path, responsibility_note, source_id, author_id, sensitivity, payload, updated_at
                    ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        org_id,
                        project_id,
                        assignment["assignment_id"],
                        assignment["person_id"],
                        assignment["group"],
                        assignment["role"],
                        assignment["path"],
                        assignment["responsibility_note"],
                        assignment["source_id"],
                        assignment["author_id"],
                        assignment["sensitivity"],
                        _json(assignment),
                        _now(),
                    ),
                )
            conn.commit()

    def save_item(self, item: InspectionItem, *, materialize: bool = True) -> None:
        with _STORE_LOCK:
            existing = self._get_item_payload(item.item_id)
            payload = to_plain(item)
            self._upsert_item_payload(payload)
            if existing != payload:
                self._append_event("item", item.item_id, "item_saved", payload)
            if materialize:
                self.generate_vault_mirror()

    def get_item(self, item_id: str) -> InspectionItem:
        payload = self._get_item_payload(item_id)
        if payload is None:
            raise KeyError(item_id)
        return _inspection_item_from_dict(payload)

    def list_items(self, status: CandidateStatus | None = None) -> list[InspectionItem]:
        sql = "select * from items"
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += " where status = ?"
            params = (status.value,)
        sql += " order by id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_inspection_item_from_dict(_item_payload_from_row(row)) for row in rows]

    def confirm_item(self, item_id: str, editor: str, notes: str = "") -> InspectionItem:
        return self._set_item_status(item_id, CandidateStatus.CONFIRMED, editor, notes)

    def reject_item(self, item_id: str, editor: str, notes: str = "") -> InspectionItem:
        return self._set_item_status(item_id, CandidateStatus.REJECTED, editor, notes)

    def update_item_fields(
        self,
        item_id: str,
        status: str | None = None,
        title: str | None = None,
        description: str | None = None,
        due_date: str | None = None,
        deliverable: str | None = None,
        acceptance_criteria: str | None = None,
        professional_id: str | None = None,
        board_id: str | None = None,
        owner_candidates: list[str] | None = None,
        linked_issue_ids: list[str] | None = None,
        linked_task_ids: list[str] | None = None,
        editor: str = "local_user",
        notes: str = "",
    ) -> InspectionItem:
        with _STORE_LOCK:
            item = self.get_item(item_id)
            if status is not None:
                item.status = CandidateStatus(status)
            if title is not None:
                item.title = title
            if description is not None:
                item.description = description
            if due_date is not None:
                item.due_date = due_date
            if deliverable is not None:
                item.deliverable = deliverable
            if acceptance_criteria is not None:
                item.acceptance_criteria = acceptance_criteria
            if professional_id is not None:
                item.professional_id = professional_id
            if board_id is not None:
                item.board_id = board_id
            if owner_candidates is not None:
                item.owner_candidates = owner_candidates
            if linked_issue_ids is not None:
                item.linked_issue_ids = linked_issue_ids
            if linked_task_ids is not None:
                item.linked_task_ids = linked_task_ids
            item.confirmation_editor = editor
            item.confirmation_notes = notes or item.confirmation_notes
            item.updated_at = _now()
            payload = to_plain(item)
            self._upsert_item_payload(payload)
            self._append_event("item", item.item_id, "item_edited", payload)
            self.generate_vault_mirror()
            return item

    def publish_item_as_work_item(
        self,
        item_id: str,
        editor: str = "local_user",
        notes: str = "",
        milestone_id: str = "",
        source_run_id: str = "",
    ) -> WorkItem:
        with _STORE_LOCK:
            candidate = self.get_item(item_id)
            work_item_id = _work_item_id_from_candidate(item_id)
            existing = self._get_task_payload(work_item_id)
            version = int(existing.get("version", 0)) + 1 if existing else 1
            now = _now()
            work_item = WorkItem(
                work_item_id=work_item_id,
                title=candidate.title,
                description=candidate.description,
                status=WorkItemStatus.OPEN,
                evidence_refs=candidate.evidence_refs,
                owner_candidates=candidate.owner_candidates or [],
                due_date=candidate.due_date,
                deliverable=candidate.deliverable,
                acceptance_criteria=candidate.acceptance_criteria,
                professional_id=existing.get("professional_id", "") if existing else candidate.professional_id,
                board_id=existing.get("board_id", "") if existing else candidate.board_id,
                planned_start=existing.get("planned_start", "") if existing else "",
                progress_percent=existing.get("progress_percent") if existing else None,
                status_updated_at=existing.get("status_updated_at", now) if existing else now,
                milestone_id=milestone_id,
                source_candidate_id=candidate.item_id,
                source_run_id=source_run_id,
                version=version,
                created_at=existing.get("created_at", now) if existing else now,
                updated_at=now,
                confirmation_notes=notes,
                confirmation_editor=editor,
                linked_issue_ids=candidate.linked_issue_ids or [],
                linked_task_ids=candidate.linked_task_ids or [],
                org_id=candidate.org_id,
                project_id=candidate.project_id,
                topic_id=candidate.topic_id,
                author_id=candidate.author_id,
                sensitivity=candidate.sensitivity,
                proposed_sensitivity=candidate.proposed_sensitivity,
                sensitivity_reason=candidate.sensitivity_reason,
                tag_origin=candidate.tag_origin,
            )
            payload = to_plain(work_item)
            self._upsert_task_payload(payload)
            self._append_event("task", work_item.work_item_id, "work_item_published", {
                "editor": editor,
                "notes": notes,
                "work_item": payload,
            })
            candidate.status = CandidateStatus.CONFIRMED
            candidate.confirmation_editor = editor
            candidate.confirmation_notes = notes or "发布为正式任务"
            candidate.updated_at = now
            candidate_payload = to_plain(candidate)
            self._upsert_item_payload(candidate_payload)
            self._append_event(
                "item",
                candidate.item_id,
                "item_confirmed_by_task_publish",
                candidate_payload,
            )
            if candidate.thread_id:
                self.refresh_memory_thread(candidate.thread_id)
            self.generate_vault_mirror()
            return work_item

    def list_work_items(self, status: WorkItemStatus | None = None) -> list[WorkItem]:
        sql = "select payload from tasks"
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += " where status = ?"
            params = (status.value,)
        sql += " order by id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_work_item_from_dict(_loads(row["payload"], {})) for row in rows]

    def update_work_item_fields(
        self,
        work_item_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        due_date: str | None = None,
        deliverable: str | None = None,
        acceptance_criteria: str | None = None,
        professional_id: str | None = None,
        board_id: str | None = None,
        planned_start: str | None = None,
        progress_percent: int | None | object = _UNSET,
        owner_candidates: list[str] | None = None,
        linked_issue_ids: list[str] | None = None,
        linked_task_ids: list[str] | None = None,
        editor: str = "local_user",
        notes: str = "",
    ) -> WorkItem:
        with _STORE_LOCK:
            payload = self._get_task_payload(work_item_id)
            if payload is None:
                raise KeyError(work_item_id)
            item = _work_item_from_dict(payload)
            if title is not None:
                item.title = title
            if description is not None:
                item.description = description
            if status is not None:
                item.status = WorkItemStatus(status)
                if item.status == WorkItemStatus.DONE:
                    item.progress_percent = 100
            if due_date is not None:
                item.due_date = due_date
            if deliverable is not None:
                item.deliverable = deliverable
            if acceptance_criteria is not None:
                item.acceptance_criteria = acceptance_criteria
            if professional_id is not None:
                item.professional_id = professional_id
            if board_id is not None:
                item.board_id = board_id
            if planned_start is not None:
                item.planned_start = planned_start
            if progress_percent is not _UNSET:
                item.progress_percent = (
                    None
                    if progress_percent is None
                    else max(0, min(100, int(progress_percent)))
                )
            if owner_candidates is not None:
                item.owner_candidates = owner_candidates
            if linked_issue_ids is not None:
                item.linked_issue_ids = linked_issue_ids
            if linked_task_ids is not None:
                item.linked_task_ids = linked_task_ids
            item.version += 1
            item.updated_at = _now()
            if status is not None or progress_percent is not _UNSET:
                item.status_updated_at = item.updated_at
            item.confirmation_editor = editor
            item.confirmation_notes = notes or item.confirmation_notes
            next_payload = to_plain(item)
            self._upsert_task_payload(next_payload)
            self._append_event("task", item.work_item_id, "work_item_updated", {
                "editor": editor,
                "notes": notes,
                "work_item": next_payload,
            })
            return item

    def archive_project_workspace(self, project_id: str, *, actor_id: str) -> dict[str, int]:
        """Soft-archive mutable workspace state while preserving source evidence."""
        now = _now()
        item_audit: list[dict[str, str]] = []
        task_audit: list[dict[str, str]] = []
        with _STORE_LOCK, self._connect() as conn:
            item_rows = conn.execute(
                "select id, status, payload from items where project_id = ? and status <> 'archived'",
                (project_id,),
            ).fetchall()
            for row in item_rows:
                payload = _loads(row["payload"], {})
                payload.update({
                    "status": CandidateStatus.ARCHIVED.value,
                    "confirmation_editor": actor_id,
                    "confirmation_notes": "项目工作台批量归档",
                    "updated_at": now,
                })
                conn.execute(
                    """
                    update items
                    set status = 'archived', edited_by_human = 1, updated_at = ?, payload = ?
                    where id = ?
                    """,
                    (now, _json(payload), row["id"]),
                )
                item_audit.append({"id": row["id"], "previous_status": row["status"]})

            task_rows = conn.execute(
                "select id, status, payload from tasks where status <> 'archived'"
            ).fetchall()
            for row in task_rows:
                payload = _loads(row["payload"], {})
                if str(payload.get("project_id") or "project_mvp") != project_id:
                    continue
                payload.update({
                    "status": WorkItemStatus.ARCHIVED.value,
                    "confirmation_editor": actor_id,
                    "confirmation_notes": "项目工作台批量归档",
                    "updated_at": now,
                })
                conn.execute(
                    "update tasks set status = 'archived', updated_at = ?, payload = ? where id = ?",
                    (now, _json(payload), row["id"]),
                )
                task_audit.append({"id": row["id"], "previous_status": row["status"]})

            run_rows = conn.execute(
                "select id from runs where project_id = ? and archived = 0",
                (project_id,),
            ).fetchall()
            conn.execute(
                "update runs set archived = 1 where project_id = ? and archived = 0",
                (project_id,),
            )
            session_rows = conn.execute(
                "select id from sessions where project_id = ? and archived = 0",
                (project_id,),
            ).fetchall()
            conn.execute(
                "update sessions set archived = 1, updated_at = ? where project_id = ? and archived = 0",
                (now, project_id),
            )
            job_rows = conn.execute(
                "select id from ingestion_jobs where project_id = ? and status <> 'archived'",
                (project_id,),
            ).fetchall()
            thread_rows = conn.execute(
                "select id from memory_threads where project_id = ? and status <> 'archived'",
                (project_id,),
            ).fetchall()
            conn.execute(
                """
                update ingestion_jobs
                set status = 'archived', stage = 'archived', lease_owner = '',
                    lease_expires_at = '', updated_at = ?
                where project_id = ? and status <> 'archived'
                """,
                (now, project_id),
            )
            conn.execute(
                "update memory_threads set status = 'archived' where project_id = ? and status <> 'archived'",
                (project_id,),
            )
            if item_rows:
                item_ids = [row["id"] for row in item_rows]
                placeholders = ", ".join("?" for _ in item_ids)
                conn.execute(f"delete from items_fts where id in ({placeholders})", tuple(item_ids))
                conn.execute(f"delete from embeddings where item_id in ({placeholders})", tuple(item_ids))
            conn.commit()

        result = {
            "items": len(item_audit),
            "tasks": len(task_audit),
            "runs": len(run_rows),
            "sessions": len(session_rows),
        }
        if any(result.values()) or job_rows or thread_rows:
            self._append_event("project", project_id, "project_workspace_archived", {
                "actor_id": actor_id,
                "counts": result,
                "items": item_audit,
                "tasks": task_audit,
                "run_ids": [row["id"] for row in run_rows],
                "session_ids": [row["id"] for row in session_rows],
                "ingestion_job_ids": [row["id"] for row in job_rows],
                "memory_thread_ids": [row["id"] for row in thread_rows],
                "sources_preserved": True,
            })
            self.generate_vault_mirror()
        return result

    def ensure_session(
        self,
        session_id: str | None = None,
        title: str = "",
        project_id: str = "project_mvp",
        actor_id: str = "u_pm",
    ) -> dict[str, Any]:
        return self.session_repository.ensure_session(
            session_id=session_id,
            title=title,
            project_id=project_id,
            actor_id=actor_id,
        )

    def append_session_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
        turn_id: str = "",
        run_id: str = "",
        state: str = "committed",
    ) -> dict[str, Any]:
        return self.session_repository.append_message(
            session_id,
            role,
            content,
            metadata=metadata,
            turn_id=turn_id,
            run_id=run_id,
            state=state,
        )

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self.session_repository.get_session(session_id)

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        return self.session_repository.rename_session(session_id, title)

    def archive_session(self, session_id: str, archived: bool = True) -> dict[str, Any]:
        return self.session_repository.archive_session(session_id, archived)

    def delete_session(self, session_id: str) -> None:
        self.session_repository.delete_session(session_id)

    def list_sessions(
        self,
        *,
        project_id: str | None = None,
        actor_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.session_repository.list_sessions(
            project_id=project_id,
            actor_id=actor_id,
        )

    def list_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        return self.session_repository.list_messages(session_id)

    def list_model_session_messages(
        self,
        session_id: str,
        *,
        current_turn_id: str = "",
    ) -> list[dict[str, Any]]:
        return self.session_repository.list_model_history(
            session_id,
            current_turn_id=current_turn_id,
        )

    def update_session_turn_state(self, turn_id: str, state: str) -> int:
        return self.session_repository.update_turn_state(turn_id, state)

    def list_human_messages(
        self,
        *,
        project_id: str,
        actor_id: str,
        start_at: str,
        end_at: str,
    ) -> list[dict[str, Any]]:
        if not project_id or not actor_id or not start_at or not end_at:
            raise ValueError("project_id, actor_id, start_at and end_at are required")
        with self._connect() as conn:
            rows = conn.execute(
                """
                select m.*, s.project_id, s.actor_id
                from messages m
                join sessions s on s.id = m.session_id
                where s.project_id = ? and s.actor_id = ? and m.role = 'user'
                  and m.created_at >= ? and m.created_at < ?
                order by m.created_at, m.id
                """,
                (project_id, actor_id, start_at, end_at),
            ).fetchall()
        return [
            {
                "message_id": row["id"],
                "session_id": row["session_id"],
                "project_id": row["project_id"],
                "actor_id": row["actor_id"],
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def human_message_actor_ids(
        self,
        *,
        project_id: str,
        start_at: str,
        end_at: str,
    ) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select distinct s.actor_id
                from messages m
                join sessions s on s.id = m.session_id
                where s.project_id = ? and m.role = 'user'
                  and m.created_at >= ? and m.created_at < ?
                order by s.actor_id
                """,
                (project_id, start_at, end_at),
            ).fetchall()
        return [str(row["actor_id"]) for row in rows]

    def begin_daily_journal_version(
        self,
        *,
        project_id: str,
        report_date: str,
        snapshot_hash: str,
        messages: list[dict[str, Any]],
        tags: dict[str, Any],
        actor: Any,
    ) -> dict[str, Any]:
        scope = _validate_ingest_tags(tags, actor)
        if scope["project_id"] != project_id:
            raise ValueError("Daily journal project_id must match access tags")
        if not report_date or not snapshot_hash or not messages:
            raise ValueError("report_date, snapshot_hash and at least one human message are required")
        journal_key = f"{project_id}\0{scope['author_id']}\0{report_date}"
        journal_id = "daily_journal_" + hashlib.sha256(journal_key.encode("utf-8")).hexdigest()[:20]
        now = _now()
        with _STORE_LOCK, self._connect() as conn:
            existing_version = conn.execute(
                """
                select v.* from daily_journal_versions v
                where v.journal_id = ? and v.snapshot_hash = ?
                """,
                (journal_id, snapshot_hash),
            ).fetchone()
            if existing_version is not None:
                if existing_version["status"] == "failed":
                    conn.execute(
                        """
                        update daily_journal_versions
                        set status = 'running', organization_status = 'pending', error = '', updated_at = ?
                        where id = ?
                        """,
                        (now, existing_version["id"]),
                    )
                    conn.execute(
                        "update daily_journals set status = 'running', last_error = '', updated_at = ? where id = ?",
                        (now, journal_id),
                    )
                    existing_version = conn.execute(
                        "select * from daily_journal_versions where id = ?",
                        (existing_version["id"],),
                    ).fetchone()
                    conn.commit()
                    result = _daily_journal_version_row(existing_version)
                    result["unchanged"] = False
                    return result
                result = _daily_journal_version_row(existing_version)
                result["unchanged"] = existing_version["status"] in {"running", "completed"}
                return result

            current = conn.execute(
                "select coalesce(max(version), 0) as max_version from daily_journal_versions where journal_id = ?",
                (journal_id,),
            ).fetchone()
            version = int(current["max_version"] or 0) + 1
            version_id = f"{journal_id}_v{version}"
            journal_payload = {
                "report_date": report_date,
                "tag_origin": "daily_chat_compiler",
            }
            conn.execute(
                """
                insert into daily_journals(
                  id, org_id, project_id, topic_id, author_id, sensitivity,
                  report_date, active_version_id, status, last_error,
                  created_at, updated_at, payload
                ) values(?, ?, ?, ?, ?, ?, ?, '', 'running', '', ?, ?, ?)
                on conflict(project_id, author_id, report_date) do update set
                  status = 'running', last_error = '', updated_at = excluded.updated_at,
                  sensitivity = excluded.sensitivity, payload = excluded.payload
                """,
                (
                    journal_id, scope["org_id"], project_id, scope.get("topic_id"),
                    scope["author_id"], scope["sensitivity"], report_date,
                    now, now, _json(journal_payload),
                ),
            )
            conn.execute(
                """
                insert into daily_journal_versions(
                  id, journal_id, version, org_id, project_id, topic_id, author_id,
                  sensitivity, report_date, snapshot_hash, message_count, status,
                  organization_status, source_id, ingestion_job_id, content_markdown,
                  error, created_at, updated_at, payload
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', 'pending', '', '', '', '', ?, ?, ?)
                """,
                (
                    version_id, journal_id, version, scope["org_id"], project_id,
                    scope.get("topic_id"), scope["author_id"], scope["sensitivity"],
                    report_date, snapshot_hash, len(messages), now, now,
                    _json({"messages": messages, "tag_origin": "daily_chat_compiler"}),
                ),
            )
            row = conn.execute("select * from daily_journal_versions where id = ?", (version_id,)).fetchone()
            conn.commit()
        assert row is not None
        result = _daily_journal_version_row(row)
        result["unchanged"] = False
        self._append_event("daily_journal", journal_id, "daily_journal_started", {
            "version_id": version_id,
            "message_count": len(messages),
            "actor_id": scope["author_id"],
        })
        return result

    def recover_interrupted_daily_journals(self) -> int:
        error = "Recovered interrupted daily journal after process restart"
        now = _now()
        with _STORE_LOCK, self._connect() as conn:
            rows = conn.execute(
                "select id, journal_id from daily_journal_versions where status = 'running' order by id"
            ).fetchall()
            if not rows:
                return 0
            version_ids = [str(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in version_ids)
            conn.execute(
                f"""
                update daily_journal_versions
                set status = 'failed', organization_status = 'failed', error = ?, updated_at = ?
                where id in ({placeholders})
                """,
                (error, now, *version_ids),
            )
            journal_ids = sorted({str(row["journal_id"]) for row in rows})
            journal_placeholders = ",".join("?" for _ in journal_ids)
            conn.execute(
                f"""
                update daily_journals
                set status = 'failed', last_error = ?, updated_at = ?
                where id in ({journal_placeholders})
                """,
                (error, now, *journal_ids),
            )
            conn.commit()
        for row in rows:
            self._append_event("daily_journal", str(row["journal_id"]), "daily_journal_recovered", {
                "version_id": str(row["id"]),
                "error": error,
            })
        return len(rows)

    def complete_daily_journal_version(
        self,
        version_id: str,
        *,
        organization_status: str,
        source_id: str,
        ingestion_job_id: str,
        content_markdown: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.get_daily_journal_version(version_id)
        now = _now()
        merged_payload = {**current["payload"], **(payload or {})}
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                """
                update daily_journal_versions
                set status = 'completed', organization_status = ?, source_id = ?,
                    ingestion_job_id = ?, content_markdown = ?, error = '',
                    updated_at = ?, payload = ?
                where id = ?
                """,
                (
                    organization_status, source_id, ingestion_job_id,
                    content_markdown, now, _json(merged_payload), version_id,
                ),
            )
            conn.execute(
                """
                update daily_journals
                set active_version_id = ?, status = 'completed', last_error = '', updated_at = ?
                where id = ?
                """,
                (version_id, now, current["journal_id"]),
            )
            conn.commit()
        result = self.get_daily_journal_version(version_id)
        self._append_event("daily_journal", current["journal_id"], "daily_journal_completed", {
            "version_id": version_id,
            "source_id": source_id,
            "ingestion_job_id": ingestion_job_id,
        })
        return result

    def fail_daily_journal_version(self, version_id: str, error: str) -> dict[str, Any]:
        if not error.strip():
            raise ValueError("A concrete daily journal error is required")
        current = self.get_daily_journal_version(version_id)
        now = _now()
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                """
                update daily_journal_versions
                set status = 'failed', organization_status = 'failed', error = ?, updated_at = ?
                where id = ?
                """,
                (error, now, version_id),
            )
            conn.execute(
                "update daily_journals set status = 'failed', last_error = ?, updated_at = ? where id = ?",
                (error, now, current["journal_id"]),
            )
            conn.commit()
        self._append_event("daily_journal", current["journal_id"], "daily_journal_failed", {
            "version_id": version_id,
            "error": error,
        })
        return self.get_daily_journal_version(version_id)

    def get_daily_journal_version(self, version_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("select * from daily_journal_versions where id = ?", (version_id,)).fetchone()
        if row is None:
            raise KeyError(version_id)
        return _daily_journal_version_row(row)

    def latest_daily_journal(
        self,
        *,
        project_id: str,
        actor_id: str,
        report_date: str | None = None,
    ) -> dict[str, Any] | None:
        clauses = ["project_id = ?", "author_id = ?"]
        params: list[Any] = [project_id, actor_id]
        if report_date:
            clauses.append("report_date = ?")
            params.append(report_date)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                select * from daily_journal_versions
                where {' and '.join(clauses)}
                order by report_date desc, version desc limit 1
                """,
                tuple(params),
            ).fetchone()
        return _daily_journal_version_row(row) if row else None

    def replace_daily_work_records(
        self,
        *,
        series_id: str,
        source_id: str,
        source_origin: str,
        org_id: str,
        project_id: str,
        topic_id: str | None,
        author_id: str,
        sensitivity: str,
        subject_user_id: str,
        subject_name: str,
        record_date: str,
        entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not series_id.strip() or not source_id.strip():
            raise WorkRecordValidationError("series_id and source_id are required")
        if source_origin not in WORK_RECORD_SOURCE_ORIGINS:
            raise WorkRecordValidationError(f"Unsupported work record source origin: {source_origin}")
        if not org_id.strip() or not project_id.strip() or not author_id.strip():
            raise WorkRecordValidationError("Daily work record access tags are required")
        if sensitivity not in SENSITIVITY_LEVELS:
            raise WorkRecordValidationError(f"Unsupported sensitivity: {sensitivity}")
        if not subject_name.strip():
            raise WorkRecordValidationError("subject_name is required")
        try:
            datetime.fromisoformat(record_date)
        except ValueError as exc:
            raise WorkRecordValidationError("record_date must use ISO date format") from exc
        normalized_entries = [validate_work_record_entry(entry) for entry in entries]
        entry_descriptors = _work_record_entry_descriptors(
            normalized_entries,
            series_id=series_id,
            source_origin=source_origin,
        )
        now = _now()
        record_ids: list[str] = []
        with _STORE_LOCK, self._connect() as conn:
            human_rows = conn.execute(
                """
                select * from daily_work_records
                where series_id = ? and source_origin = ? and status = 'active'
                  and edited_by_human = 1
                order by created_at, id
                """,
                (series_id, source_origin),
            ).fetchall()
            parsed_human_rows: list[dict[str, Any]] = []
            for human_row in human_rows:
                parsed = _daily_work_record_row(human_row)
                parsed_human_rows.append(parsed)
            matched_humans = _reconcile_work_record_entries(
                normalized_entries,
                entry_descriptors,
                parsed_human_rows,
                source_origin=source_origin,
            )
            conn.execute(
                """
                update daily_work_records
                set status = 'superseded', updated_at = ?
                where series_id = ? and source_origin = ? and status = 'active'
                  and edited_by_human = 0
                """,
                (now, series_id, source_origin),
            )
            for index, entry in enumerate(normalized_entries):
                scoped_subject_user_id = str(entry.get("subject_user_id", subject_user_id) or "")
                scoped_subject_name = str(entry.get("subject_name", subject_name) or "").strip()
                scoped_record_date = str(entry.get("record_date", record_date) or "").strip()
                scoped_author_id = str(entry.get("author_id", author_id) or "").strip()
                scoped_locator = str(entry.get("source_locator") or "")
                if not scoped_subject_name or not scoped_author_id:
                    raise WorkRecordValidationError("Each work record requires subject_name and author_id")
                logical_key = entry_descriptors[index]["logical_key"]
                matched_human = matched_humans.get(index)
                record_id = str((matched_human or {}).get("record_id") or "")
                if not record_id:
                    record_id = "work_record_" + hashlib.sha256(
                        logical_key.encode("utf-8")
                    ).hexdigest()[:24]
                record_ids.append(record_id)
                mutable = matched_human or {}
                payload = {
                    "record_id": record_id,
                    "series_id": series_id,
                    "source_id": source_id,
                    "source_origin": source_origin,
                    "org_id": org_id,
                    "project_id": project_id,
                    "topic_id": topic_id,
                    "author_id": scoped_author_id,
                    "sensitivity": sensitivity,
                    "subject_user_id": scoped_subject_user_id,
                    "subject_name": scoped_subject_name,
                    "record_date": mutable.get("record_date", scoped_record_date),
                    "kind": mutable.get("kind", entry["kind"]),
                    "text": mutable.get("text", entry["text"]),
                    "solution_options": mutable.get("solution_options", entry["solution_options"]),
                    "evidence_refs": entry["evidence_refs"],
                    "source_locator": scoped_locator,
                    "status": "active",
                    "edited_by_human": bool(matched_human),
                    "updated_by": mutable.get("updated_by", scoped_author_id),
                    "logical_key": logical_key,
                    "generated_kind": entry["kind"],
                    "generated_text": entry["text"],
                }
                conn.execute(
                    """
                    insert into daily_work_records(
                      id, series_id, source_id, source_origin, org_id, project_id,
                      topic_id, author_id, sensitivity, subject_user_id, subject_name,
                      record_date, kind, text, solution_options, evidence_refs,
                      source_locator, status, edited_by_human, updated_by,
                      created_at, updated_at, payload
                    ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?, ?)
                    on conflict(id) do update set
                      series_id=excluded.series_id,
                      source_id=excluded.source_id,
                      source_origin=excluded.source_origin,
                      org_id=excluded.org_id,
                      project_id=excluded.project_id,
                      topic_id=excluded.topic_id,
                      author_id=excluded.author_id,
                      sensitivity=excluded.sensitivity,
                      subject_user_id=excluded.subject_user_id,
                      subject_name=excluded.subject_name,
                      record_date=case when daily_work_records.edited_by_human = 1 then daily_work_records.record_date else excluded.record_date end,
                      kind=case when daily_work_records.edited_by_human = 1 then daily_work_records.kind else excluded.kind end,
                      text=case when daily_work_records.edited_by_human = 1 then daily_work_records.text else excluded.text end,
                      solution_options=case when daily_work_records.edited_by_human = 1 then daily_work_records.solution_options else excluded.solution_options end,
                      evidence_refs=excluded.evidence_refs,
                      source_locator=excluded.source_locator,
                      status='active',
                      updated_by=case when daily_work_records.edited_by_human = 1 then daily_work_records.updated_by else excluded.updated_by end,
                      updated_at=case when daily_work_records.edited_by_human = 1 then daily_work_records.updated_at else excluded.updated_at end,
                      payload=excluded.payload
                    """,
                    (
                        record_id, series_id, source_id, source_origin, org_id, project_id,
                        topic_id, scoped_author_id, sensitivity, scoped_subject_user_id,
                        scoped_subject_name, scoped_record_date, entry["kind"], entry["text"],
                        _json(entry["solution_options"]), _json(entry["evidence_refs"]),
                        scoped_locator, scoped_author_id, now, now, _json(payload),
                    ),
                )
            conn.commit()
        return [self.get_daily_work_record(record_id) for record_id in record_ids]

    def get_daily_work_record(self, record_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "select * from daily_work_records where id = ?",
                (record_id,),
            ).fetchone()
        if row is None:
            raise KeyError(record_id)
        return _daily_work_record_row(row)

    def list_daily_work_records(
        self,
        *,
        project_id: str,
        actor: User,
        access_context: AccessContext,
        date_from: str = "",
        date_to: str = "",
        kind: str = "",
        subject_user_id: str = "",
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = ?", "status = 'active'"]
        params: list[Any] = [project_id]
        if date_from:
            clauses.append("record_date >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("record_date <= ?")
            params.append(date_to)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if subject_user_id:
            clauses.append("subject_user_id = ?")
            params.append(subject_user_id)
        with self._connect() as conn:
            rows = conn.execute(
                "select * from daily_work_records where " + " and ".join(clauses)
                + " order by record_date desc, subject_name, kind, id",
                tuple(params),
            ).fetchall()
        parsed_rows = [_daily_work_record_row(row) for row in rows]
        visible_rows = [
            row for row in parsed_rows if can_view_work_record(actor, row, access_context)
        ]
        return [
            {**row, "can_edit": can_edit_work_record(actor, row, access_context)}
            for row in visible_rows
        ]

    def update_daily_work_record(
        self,
        record_id: str,
        *,
        actor: User,
        access_context: AccessContext,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        current = self.get_daily_work_record(record_id)
        if not can_edit_work_record(actor, current, access_context):
            raise PermissionError("Daily work record is outside the actor's editable scope")
        unsupported = set(patch) - {"record_date", "kind", "text", "solution_options"}
        if unsupported:
            raise WorkRecordValidationError(
                "Unsupported daily work record fields: " + ", ".join(sorted(unsupported))
            )
        raw_options = patch.get("solution_options", current["solution_options"])
        if not isinstance(raw_options, list):
            raise WorkRecordValidationError("solution_options must be an array")
        existing_refs = {
            str(option.get("text") or "").strip(): list(option.get("memory_refs") or [])
            for option in current["solution_options"]
            if isinstance(option, dict)
        }
        sanitized_options = []
        for option in raw_options:
            if not isinstance(option, dict):
                continue
            option_text = str(option.get("text") or "").strip()
            memory_refs = existing_refs.get(option_text)
            if memory_refs is None:
                supplied_refs = option.get("memory_refs")
                memory_refs = list(supplied_refs) if isinstance(supplied_refs, list) else []
            sanitized_options.append({"text": option_text, "memory_refs": memory_refs})
        if len(sanitized_options) != len(raw_options):
            raise WorkRecordValidationError("Each solution option must be an object")
        next_kind = str(patch.get("kind", current["kind"]) or "")
        if next_kind != "problem":
            sanitized_options = []
        elif "solution_options" in patch:
            for option in sanitized_options:
                self._require_actor_visible_confirmed_memory_refs(
                    option["memory_refs"],
                    actor=actor,
                    access_context=access_context,
                )
        next_entry = validate_work_record_entry({
            "kind": next_kind,
            "text": patch.get("text", current["text"]),
            "solution_options": sanitized_options,
            "evidence_refs": current["evidence_refs"],
            "source_locator": current["source_locator"],
        })
        record_date = str(patch.get("record_date", current["record_date"]) or "").strip()
        try:
            datetime.fromisoformat(record_date)
        except ValueError as exc:
            raise WorkRecordValidationError("record_date must use ISO date format") from exc
        now = _now()
        payload = {
            **current["payload"],
            "record_date": record_date,
            "kind": next_entry["kind"],
            "text": next_entry["text"],
            "solution_options": next_entry["solution_options"],
            "edited_by_human": True,
            "updated_by": actor.id,
        }
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                """
                update daily_work_records
                set record_date = ?, kind = ?, text = ?, solution_options = ?,
                    edited_by_human = 1, updated_by = ?, updated_at = ?, payload = ?
                where id = ?
                """,
                (
                    record_date, next_entry["kind"], next_entry["text"],
                    _json(next_entry["solution_options"]), actor.id, now,
                    _json(payload), record_id,
                ),
            )
            conn.commit()
        self._append_event("daily_work_record", record_id, "daily_work_record_updated", {
            "actor_id": actor.id,
            "changed_fields": sorted(patch),
        })
        result = self.get_daily_work_record(record_id)
        result["can_edit"] = True
        return result

    def save_deliverable_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        required = [
            "deliverable_id", "org_id", "project_id", "author_id", "sensitivity",
            "milestone_id", "title", "status",
        ]
        missing = [key for key in required if not str(payload.get(key) or "").strip()]
        if missing:
            raise ValueError("Missing deliverable fields: " + ", ".join(missing))
        if payload["sensitivity"] not in SENSITIVITY_LEVELS:
            raise ValueError(f"Unsupported sensitivity: {payload['sensitivity']}")
        now = _now()
        existing = None
        try:
            existing = self.get_deliverable(str(payload["deliverable_id"]))
        except KeyError:
            pass
        created_at = str((existing or {}).get("created_at") or payload.get("created_at") or now)
        current_version = int(payload.get("current_version", (existing or {}).get("current_version", 0)) or 0)
        row_payload = {
            **(existing or {}).get("payload", {}),
            **payload,
            "created_at": created_at,
            "updated_at": now,
            "current_version": current_version,
        }
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                """
                insert into deliverables(
                  id, org_id, project_id, topic_id, author_id, sensitivity,
                  milestone_id, title, type_label, required, due_date,
                  acceptance_criteria, status, sort_order, current_version,
                  created_at, updated_at, payload
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                  org_id=excluded.org_id,
                  project_id=excluded.project_id,
                  topic_id=excluded.topic_id,
                  author_id=excluded.author_id,
                  sensitivity=excluded.sensitivity,
                  milestone_id=excluded.milestone_id,
                  title=excluded.title,
                  type_label=excluded.type_label,
                  required=excluded.required,
                  due_date=excluded.due_date,
                  acceptance_criteria=excluded.acceptance_criteria,
                  status=excluded.status,
                  sort_order=excluded.sort_order,
                  current_version=excluded.current_version,
                  updated_at=excluded.updated_at,
                  payload=excluded.payload
                """,
                (
                    payload["deliverable_id"], payload["org_id"], payload["project_id"],
                    payload.get("topic_id"), payload["author_id"], payload["sensitivity"],
                    payload["milestone_id"], payload["title"], payload.get("type_label", ""),
                    int(bool(payload.get("required", True))), payload.get("due_date", ""),
                    payload.get("acceptance_criteria", ""), payload["status"],
                    int(payload.get("sort_order", 0) or 0), current_version,
                    created_at, now, _json(row_payload),
                ),
            )
            if existing and any(
                (existing.get(key) or None) != (payload.get(key) or None)
                for key in ("org_id", "project_id", "topic_id", "sensitivity")
            ):
                scope_patch = {
                    "org_id": payload["org_id"],
                    "project_id": payload["project_id"],
                    "topic_id": payload.get("topic_id"),
                    "sensitivity": payload["sensitivity"],
                }
                for table in ("deliverable_versions", "deliverable_files"):
                    tagged_rows = conn.execute(
                        f"select id, payload from {table} where deliverable_id = ?",
                        (payload["deliverable_id"],),
                    ).fetchall()
                    for tagged_row in tagged_rows:
                        tagged_payload = {**_loads(tagged_row["payload"], {}), **scope_patch}
                        conn.execute(
                            f"""
                            update {table}
                            set org_id = ?, project_id = ?, topic_id = ?, sensitivity = ?, payload = ?
                            where id = ?
                            """,
                            (
                                scope_patch["org_id"], scope_patch["project_id"],
                                scope_patch["topic_id"], scope_patch["sensitivity"],
                                _json(tagged_payload), tagged_row["id"],
                            ),
                        )
            conn.commit()
        action = "deliverable_updated" if existing else "deliverable_created"
        self._append_event("deliverable", str(payload["deliverable_id"]), action, {
            "actor_id": str(payload.get("updated_by") or payload["author_id"]),
            "milestone_id": payload["milestone_id"],
        })
        return self.get_deliverable(str(payload["deliverable_id"]))

    def get_deliverable(self, deliverable_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("select * from deliverables where id = ?", (deliverable_id,)).fetchone()
        if row is None:
            raise KeyError(deliverable_id)
        return _deliverable_row(row)

    def list_deliverable_records(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select * from deliverables
                where project_id = ? and status <> 'archived'
                order by sort_order, created_at, id
                """,
                (project_id,),
            ).fetchall()
        return [_deliverable_row(row) for row in rows]

    def reserve_deliverable_version_record(
        self,
        deliverable_id: str,
        *,
        submitted_by: str,
        note: str,
        actor: User,
        access_context: AccessContext,
    ) -> dict[str, Any]:
        now = _now()
        with _STORE_LOCK, self._connect() as conn:
            conn.execute("begin immediate")
            deliverable_row = conn.execute(
                "select * from deliverables where id = ?",
                (deliverable_id,),
            ).fetchone()
            if deliverable_row is None:
                conn.rollback()
                raise KeyError(deliverable_id)
            deliverable = _deliverable_row(deliverable_row)
            self._require_deliverable_submission_access(
                deliverable,
                actor=actor,
                access_context=access_context,
                submitted_by=submitted_by,
            )
            current = conn.execute(
                "select coalesce(max(version), 0) as max_version from deliverable_versions where deliverable_id = ?",
                (deliverable_id,),
            ).fetchone()
            version = int(current["max_version"] or 0) + 1
            version_id = "deliverable_version_" + hashlib.sha256(
                f"{deliverable_id}\0{version}".encode("utf-8")
            ).hexdigest()[:24]
            version_payload = {
                "version_id": version_id,
                "deliverable_id": deliverable_id,
                "version": version,
                "org_id": deliverable["org_id"],
                "project_id": deliverable["project_id"],
                "topic_id": deliverable.get("topic_id"),
                "author_id": deliverable["author_id"],
                "sensitivity": deliverable["sensitivity"],
                "submitted_by": submitted_by,
                "status": "staging",
                "note": note,
                "created_at": now,
            }
            conn.execute(
                """
                insert into deliverable_versions(
                  id, deliverable_id, version, org_id, project_id, topic_id,
                  author_id, sensitivity, submitted_by, status, note, created_at, payload
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, 'staging', ?, ?, ?)
                """,
                (
                    version_id, deliverable_id, version, deliverable["org_id"],
                    deliverable["project_id"], deliverable.get("topic_id"),
                    deliverable["author_id"], deliverable["sensitivity"], submitted_by,
                    note, now, _json(version_payload),
                ),
            )
            conn.commit()
        return self.get_deliverable_version(version_id)

    def complete_deliverable_version_record(
        self,
        version_id: str,
        *,
        files: list[dict[str, Any]],
        actor: User,
        access_context: AccessContext,
    ) -> dict[str, Any]:
        now = _now()
        with _STORE_LOCK, self._connect() as conn:
            conn.execute("begin immediate")
            version_row = conn.execute(
                "select * from deliverable_versions where id = ?",
                (version_id,),
            ).fetchone()
            if version_row is None:
                conn.rollback()
                raise KeyError(version_id)
            if version_row["status"] != "staging":
                conn.rollback()
                raise ValueError(f"Deliverable version is not staging: {version_row['status']}")
            deliverable_row = conn.execute(
                "select * from deliverables where id = ?",
                (version_row["deliverable_id"],),
            ).fetchone()
            if deliverable_row is None:
                conn.rollback()
                raise KeyError(str(version_row["deliverable_id"]))
            deliverable = _deliverable_row(deliverable_row)
            self._require_deliverable_submission_access(
                deliverable,
                actor=actor,
                access_context=access_context,
                submitted_by=str(version_row["submitted_by"]),
            )
            version = int(version_row["version"])
            version_payload = {
                **_loads(version_row["payload"], {}),
                "org_id": deliverable["org_id"],
                "project_id": deliverable["project_id"],
                "topic_id": deliverable.get("topic_id"),
                "author_id": deliverable["author_id"],
                "sensitivity": deliverable["sensitivity"],
                "status": "submitted",
            }
            conn.execute(
                """
                update deliverable_versions
                set org_id = ?, project_id = ?, topic_id = ?, author_id = ?,
                    sensitivity = ?, status = 'submitted', payload = ?
                where id = ?
                """,
                (
                    deliverable["org_id"], deliverable["project_id"],
                    deliverable.get("topic_id"), deliverable["author_id"],
                    deliverable["sensitivity"], _json(version_payload), version_id,
                ),
            )
            for index, file_payload in enumerate(files):
                file_id = "deliverable_file_" + hashlib.sha256(
                    "\0".join([
                        version_id,
                        str(index),
                        str(file_payload["original_name"]),
                        str(file_payload["content_hash"]),
                    ]).encode("utf-8")
                ).hexdigest()[:24]
                stored = {
                    **file_payload,
                    "file_id": file_id,
                    "deliverable_id": deliverable["deliverable_id"],
                    "version_id": version_id,
                    "org_id": deliverable["org_id"],
                    "project_id": deliverable["project_id"],
                    "topic_id": deliverable.get("topic_id"),
                    "author_id": str(version_row["submitted_by"]),
                    "sensitivity": deliverable["sensitivity"],
                    "created_at": now,
                }
                conn.execute(
                    """
                    insert into deliverable_files(
                      id, deliverable_id, version_id, org_id, project_id, topic_id,
                      author_id, sensitivity, artifact_role, original_name,
                      relative_path, content_hash, content_type, position, size, created_at, payload
                    ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id, deliverable["deliverable_id"], version_id, deliverable["org_id"],
                        deliverable["project_id"], deliverable.get("topic_id"), str(version_row["submitted_by"]),
                        deliverable["sensitivity"], stored["artifact_role"],
                        stored["original_name"], stored["relative_path"], stored["content_hash"],
                        stored.get("content_type", ""), index, int(stored["size"]), now, _json(stored),
                    ),
                )
            next_payload = {
                **deliverable["payload"],
                "status": "submitted",
                "current_version": max(int(deliverable["current_version"]), version),
                "updated_at": now,
            }
            conn.execute(
                """
                update deliverables
                set status = 'submitted', current_version = ?, updated_at = ?, payload = ?
                where id = ?
                """,
                (
                    max(int(deliverable["current_version"]), version),
                    now,
                    _json(next_payload),
                    deliverable["deliverable_id"],
                ),
            )
            self._insert_event(
                conn,
                "deliverable",
                deliverable["deliverable_id"],
                "deliverable_version_submitted",
                {
                    "version_id": version_id,
                    "version": version,
                    "submitted_by": str(version_row["submitted_by"]),
                    "file_count": len(files),
                },
            )
            conn.commit()
        return self.get_deliverable_version(version_id)

    def fail_deliverable_version_record(self, version_id: str, error: str) -> None:
        with _STORE_LOCK, self._connect() as conn:
            row = conn.execute(
                "select payload from deliverable_versions where id = ?",
                (version_id,),
            ).fetchone()
            if row is None:
                return
            payload = {**_loads(row["payload"], {}), "status": "failed", "error": str(error)}
            conn.execute(
                "update deliverable_versions set status = 'failed', payload = ? where id = ? and status = 'staging'",
                (_json(payload), version_id),
            )
            conn.commit()

    def get_deliverable_version(self, version_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "select * from deliverable_versions where id = ?",
                (version_id,),
            ).fetchone()
            files = conn.execute(
                "select * from deliverable_files where version_id = ? order by position, id",
                (version_id,),
            ).fetchall()
        if row is None:
            raise KeyError(version_id)
        return {**_deliverable_version_row(row), "files": [_deliverable_file_row(file) for file in files]}

    def list_deliverable_versions(self, deliverable_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "select id from deliverable_versions where deliverable_id = ? and status = 'submitted' order by version desc",
                (deliverable_id,),
            ).fetchall()
        return [self.get_deliverable_version(str(row["id"])) for row in rows]

    def get_deliverable_file(self, file_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("select * from deliverable_files where id = ?", (file_id,)).fetchone()
        if row is None:
            raise KeyError(file_id)
        return _deliverable_file_row(row)

    def create_project_skill_version(
        self,
        *,
        skill_id: str,
        slug: str,
        name: str,
        source_method_id: str,
        description: str,
        when_to_use: str,
        tool_names: list[str],
        body_markdown: str,
        readiness: dict[str, Any],
        evidence_refs: list[dict[str, Any]],
        pressure_tests: list[dict[str, Any]],
        tags: dict[str, Any],
        actor: Any,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scope = _validate_ingest_tags(tags, actor)
        if not all(str(value or "").strip() for value in (skill_id, slug, name, source_method_id)):
            raise ValueError("skill_id, slug, name and source_method_id are required")
        now = _now()
        content_hash = hashlib.sha256(body_markdown.encode("utf-8")).hexdigest()
        with _STORE_LOCK, self._connect() as conn:
            existing = conn.execute("select * from project_skills where id = ?", (skill_id,)).fetchone()
            current = conn.execute(
                "select coalesce(max(version), 0) as max_version from project_skill_versions where skill_id = ?",
                (skill_id,),
            ).fetchone()
            version = int(current["max_version"] or 0) + 1
            version_id = f"{skill_id}_v{version}"
            skill_status = existing["status"] if existing is not None and existing["active_version_id"] else "candidate"
            skill_payload = {
                "source_method_id": source_method_id,
                "latest_version_id": version_id,
            }
            conn.execute(
                """
                insert into project_skills(
                  id, org_id, project_id, topic_id, author_id, sensitivity,
                  slug, name, status, source_method_id, active_version_id,
                  created_at, updated_at, payload
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
                on conflict(id) do update set
                  org_id = case when project_skills.active_version_id = '' then excluded.org_id else project_skills.org_id end,
                  project_id = case when project_skills.active_version_id = '' then excluded.project_id else project_skills.project_id end,
                  topic_id = case when project_skills.active_version_id = '' then excluded.topic_id else project_skills.topic_id end,
                  author_id = case when project_skills.active_version_id = '' then excluded.author_id else project_skills.author_id end,
                  sensitivity = case when project_skills.active_version_id = '' then excluded.sensitivity else project_skills.sensitivity end,
                  name = case when project_skills.active_version_id = '' then excluded.name else project_skills.name end,
                  updated_at = excluded.updated_at,
                  payload = excluded.payload,
                  status = case when project_skills.active_version_id = '' then 'candidate' else project_skills.status end
                """,
                (
                    skill_id, scope["org_id"], scope["project_id"], scope.get("topic_id"),
                    scope["author_id"], scope["sensitivity"], slug, name, skill_status,
                    source_method_id, (existing["created_at"] if existing else now), now,
                    _json(skill_payload),
                ),
            )
            conn.execute(
                """
                insert into project_skill_versions(
                  id, skill_id, version, org_id, project_id, topic_id, author_id,
                  sensitivity, status, description, when_to_use, tool_names,
                  body_markdown, content_hash, readiness, created_by, created_at,
                  published_at, payload
                ) values(?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?, ?, '', ?)
                """,
                (
                    version_id, skill_id, version, scope["org_id"], scope["project_id"],
                    scope.get("topic_id"), scope["author_id"], scope["sensitivity"],
                    description, when_to_use, _json(tool_names), body_markdown, content_hash,
                    _json(readiness), _actor_id(actor), now, _json(payload or {}),
                ),
            )
            for index, ref in enumerate(evidence_refs):
                evidence_id = f"{version_id}_e{index + 1:03d}"
                conn.execute(
                    """
                    insert into project_skill_evidence(
                      id, skill_id, version_id, org_id, project_id, topic_id,
                      author_id, sensitivity, source_method_id, source_doc_id,
                      locator, quote, created_at, payload
                    ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence_id, skill_id, version_id, scope["org_id"], scope["project_id"],
                        scope.get("topic_id"), scope["author_id"], scope["sensitivity"],
                        source_method_id, str(ref.get("source_doc_id") or ""),
                        str(ref.get("locator") or ""), str(ref.get("quote") or ""),
                        now, _json(ref),
                    ),
                )
            for index, test in enumerate(pressure_tests):
                test_id = f"{version_id}_t{index + 1:03d}"
                conn.execute(
                    """
                    insert into project_skill_tests(
                      id, skill_id, version_id, org_id, project_id, topic_id,
                      author_id, sensitivity, name, input_text, should_trigger,
                      selected, passed, status, error, created_at, updated_at, payload
                    ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, null, null, 'pending', '', ?, ?, ?)
                    """,
                    (
                        test_id, skill_id, version_id, scope["org_id"], scope["project_id"],
                        scope.get("topic_id"), scope["author_id"], scope["sensitivity"],
                        str(test.get("name") or f"test-{index + 1}"), str(test.get("input") or ""),
                        1 if bool(test.get("should_trigger")) else 0, now, now, _json(test),
                    ),
                )
            conn.commit()
        self._append_event("project_skill", skill_id, "project_skill_version_compiled", {
            "version_id": version_id,
            "source_method_id": source_method_id,
            "ready": bool(readiness.get("ready")),
        })
        return self.get_project_skill_version(version_id)

    def get_project_skill_version(self, version_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("select * from project_skill_versions where id = ?", (version_id,)).fetchone()
            evidence = conn.execute(
                "select * from project_skill_evidence where version_id = ? order by id",
                (version_id,),
            ).fetchall()
            tests = conn.execute(
                "select * from project_skill_tests where version_id = ? order by id",
                (version_id,),
            ).fetchall()
        if row is None:
            raise KeyError(version_id)
        result = _project_skill_version_row(row)
        result["evidence"] = [_project_skill_evidence_row(item) for item in evidence]
        result["pressure_tests"] = [_project_skill_test_row(item) for item in tests]
        return result

    def get_project_skill(self, skill_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("select * from project_skills where id = ?", (skill_id,)).fetchone()
            latest = conn.execute(
                "select * from project_skill_versions where skill_id = ? order by version desc limit 1",
                (skill_id,),
            ).fetchone()
        if row is None:
            raise KeyError(skill_id)
        result = _project_skill_row(row)
        result["latest_version"] = _project_skill_version_row(latest) if latest else None
        if result["active_version_id"]:
            result["active_version"] = self.get_project_skill_version(result["active_version_id"])
        else:
            result["active_version"] = None
        return result

    def list_project_skills(
        self,
        *,
        project_id: str,
        actor: User,
        ctx: AccessContext,
        published_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = ?"]
        params: list[Any] = [project_id]
        if published_only:
            clauses.append("status = 'published'")
            clauses.append("active_version_id <> ''")
        with self._connect() as conn:
            rows = conn.execute(
                f"select * from project_skills where {' and '.join(clauses)} order by updated_at desc, id",
                tuple(params),
            ).fetchall()
        visible_rows = visible_filter(actor, [_project_skill_row(row) for row in rows], ctx)
        result: list[dict[str, Any]] = []
        for row in visible_rows:
            skill = self.get_project_skill(row["id"])
            for version_key in ("latest_version", "active_version"):
                version = skill.get(version_key)
                if version is not None and not visible(actor, version, ctx):
                    skill[version_key] = None
            result.append(skill)
        return result

    def update_project_skill_test_results(
        self,
        version_id: str,
        results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        current = self.get_project_skill_version(version_id)
        if current["status"] == "published":
            raise ValueError("Published Project Skill versions are immutable")
        expected_ids = {test["id"] for test in current["pressure_tests"]}
        supplied_ids = {str(result.get("id") or "") for result in results}
        if expected_ids != supplied_ids:
            raise ValueError("Pressure test results must cover the complete test set")
        now = _now()
        all_passed = True
        with _STORE_LOCK, self._connect() as conn:
            for result in results:
                passed = bool(result.get("passed"))
                all_passed = all_passed and passed
                conn.execute(
                    """
                    update project_skill_tests
                    set selected = ?, passed = ?, status = ?, error = ?, updated_at = ?, payload = ?
                    where id = ? and version_id = ?
                    """,
                    (
                        1 if bool(result.get("selected")) else 0,
                        1 if passed else 0,
                        "passed" if passed else "failed",
                        str(result.get("error") or ""),
                        now,
                        _json(result),
                        result["id"],
                        version_id,
                    ),
                )
            conn.execute(
                "update project_skill_versions set status = ?, payload = ? where id = ?",
                (
                    "tested" if all_passed else "candidate",
                    _json({**current["payload"], "pressure_tests_passed": all_passed}),
                    version_id,
                ),
            )
            skill = conn.execute("select * from project_skills where id = ?", (current["skill_id"],)).fetchone()
            if skill is not None and not skill["active_version_id"]:
                conn.execute(
                    "update project_skills set status = ?, updated_at = ? where id = ?",
                    ("tested" if all_passed else "candidate", now, current["skill_id"]),
                )
            conn.commit()
        self._append_event("project_skill", current["skill_id"], "project_skill_pressure_tested", {
            "version_id": version_id,
            "passed": all_passed,
        })
        result = self.get_project_skill_version(version_id)
        result["passed"] = all_passed
        return result

    def publish_project_skill_version(self, version_id: str, *, actor_id: str) -> dict[str, Any]:
        current = self.get_project_skill_version(version_id)
        if not current["readiness"].get("ready"):
            raise ValueError("Project Skill readiness gate has not passed")
        tests = current["pressure_tests"]
        if not tests or any(test["status"] != "passed" for test in tests):
            raise ValueError("Project Skill pressure tests have not passed")
        now = _now()
        compiled_name = str(current["payload"].get("compiled_name") or "").strip()
        with _STORE_LOCK, self._connect() as conn:
            conn.execute(
                "update project_skill_versions set status = 'published', published_at = ? where id = ?",
                (now, version_id),
            )
            conn.execute(
                """
                update project_skills
                set status = 'published', active_version_id = ?,
                    org_id = ?, project_id = ?, topic_id = ?, author_id = ?, sensitivity = ?,
                    name = case when ? = '' then name else ? end,
                    updated_at = ?
                where id = ?
                """,
                (
                    version_id, current["org_id"], current["project_id"], current.get("topic_id"),
                    current["author_id"], current["sensitivity"], compiled_name, compiled_name,
                    now, current["skill_id"],
                ),
            )
            conn.commit()
        self._append_event("project_skill", current["skill_id"], "project_skill_published", {
            "version_id": version_id,
            "actor_id": actor_id,
        })
        return self.get_project_skill(current["skill_id"])["active_version"]

    def memory_projection(
        self,
        items: list[InspectionItem] | None = None,
    ) -> dict[str, Any]:
        active_items = (
            self.list_items()
            if items is None
            else list(items)
        )
        active_items = [
            item for item in active_items if item.status != CandidateStatus.ARCHIVED
        ]
        summary = _build_dynamic_summary(active_items)
        return {
            "summary": summary,
            "index": _build_dynamic_index(active_items),
            "resume": _build_dynamic_resume(active_items),
            "context_layers": _build_context_layers(),
            "host_memory": {"semantics": ["create", "update", "delete"], "memories": []},
            "schema": {
                "schema_version": 2,
                "authority": "SQLite project.db",
                "projection": "动态生成，不再维护 summary.json 等 JSON 投影文件。",
            },
            "storage_files": self.storage_files(),
        }

    def storage_files(self) -> dict[str, str]:
        return {
            "database": str(self.database_path),
            "archive_dir": str(self.archive_dir),
            "vault_dir": str(self.vault_dir),
            "method_vault_dir": str(self.vault_dir / "方法库"),
            "meeting_vault_dir": str(self.vault_dir / "会议"),
            "brief_vault_dir": str(self.vault_dir / "晨报"),
            "weekly_review_vault_dir": str(self.vault_dir / "周复盘"),
        }

    def search_source_evidence(
        self,
        query: str,
        *,
        actor: User,
        access_context: AccessContext,
        project_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        query_text = str(query or "").strip()
        if not query_text:
            return []
        candidate_limit = max(20, int(limit) * 5)
        with self._connect() as conn:
            rows = conn.execute(
                """
                select
                  id, source_id, org_id, project_id, topic_id,
                  author_id, sensitivity
                from source_chunks
                where project_id = ?
                order by source_id, chunk_index
                """,
                (project_id,),
            ).fetchall()
        visible_rows = visible_filter(
            actor,
            [dict(row) for row in rows],
            access_context,
        )
        if not visible_rows:
            return []
        eligible_ids = [str(row["id"]) for row in visible_rows]
        ranked: dict[str, dict[str, Any]] = {}
        with self._connect() as conn:
            conn.execute(
                "create temp table if not exists eligible_source_chunk_ids(id text primary key)"
            )
            conn.execute("delete from eligible_source_chunk_ids")
            conn.executemany(
                "insert into eligible_source_chunk_ids(id) values(?)",
                ((chunk_id,) for chunk_id in eligible_ids),
            )
            for expression in _fts_match_expressions(query_text):
                try:
                    matches = conn.execute(
                        """
                        select
                          source_chunks_fts.id,
                          bm25(source_chunks_fts) as rank,
                          snippet(
                            source_chunks_fts, -1, '<mark>', '</mark>', ' … ', 24
                          ) as highlight
                        from source_chunks_fts
                        join eligible_source_chunk_ids eligible
                          on eligible.id = source_chunks_fts.id
                        where source_chunks_fts match ?
                        order by rank
                        limit ?
                        """,
                        (expression, candidate_limit),
                    ).fetchall()
                except sqlite3.OperationalError as exc:
                    if not _is_fts_query_error(exc):
                        raise
                    matches = []
                for match in matches:
                    _add_ranked_match(
                        ranked,
                        str(match["id"]),
                        _fts_rank_score(float(match["rank"] or 0.0)),
                        "fts",
                        _normalize_fts_highlight(
                            query_text,
                            str(match["highlight"] or ""),
                        ),
                    )
            if not ranked:
                fallback_tokens = _like_tokens(query_text)
                if fallback_tokens:
                    clauses = " or ".join(
                        "(instr(chunks.title, ?) > 0 or instr(chunks.content, ?) > 0)"
                        for _token in fallback_tokens
                    )
                    fallback_matches = conn.execute(
                        f"""
                        select chunks.id
                        from source_chunks chunks
                        join eligible_source_chunk_ids eligible
                          on eligible.id = chunks.id
                        where {clauses}
                        order by chunks.source_id, chunks.chunk_index
                        limit ?
                        """,
                        (
                            *[
                                value
                                for token in fallback_tokens
                                for value in (token, token)
                            ],
                            candidate_limit,
                        ),
                    ).fetchall()
                    for match in fallback_matches:
                        _add_ranked_match(
                            ranked,
                            str(match["id"]),
                            1.0,
                            "like",
                            "",
                        )
        if not ranked:
            return []
        candidate_ids = list(ranked)
        placeholders = ",".join("?" for _ in candidate_ids)
        with self._connect() as conn:
            candidate_rows = conn.execute(
                f"""
                select *
                from source_chunks
                where id in ({placeholders})
                """,
                tuple(candidate_ids),
            ).fetchall()
        visible_candidates = visible_filter(
            actor,
            [dict(row) for row in candidate_rows],
            access_context,
        )
        visible_by_id = {
            str(row["id"]): row
            for row in visible_candidates
        }
        for chunk_id, row in visible_by_id.items():
            if "like" in ranked[chunk_id]["reasons"]:
                ranked[chunk_id]["score"] = _source_chunk_like_score(
                    query_text,
                    str(row["content"]),
                )
                _add_ranked_match(
                    ranked,
                    chunk_id,
                    0.0,
                    "like",
                    _source_chunk_highlight(query_text, str(row["content"])),
                )
        output: list[dict[str, Any]] = []
        source_cache: dict[str, dict[str, Any] | None] = {}
        for chunk_id, match in ranked.items():
            row = visible_by_id.get(chunk_id)
            if row is None:
                continue
            source_id = str(row["source_id"])
            source = source_cache.setdefault(source_id, self.get_source(source_id))
            output.append(
                {
                    "id": chunk_id,
                    "chunk_id": chunk_id,
                    "source_id": source_id,
                    "source_title": str((source or {}).get("title") or row["title"]),
                    "meeting_date": str((source or {}).get("meeting_date") or ""),
                    "locator": str(row["locator"]),
                    "content": str(row["content"]),
                    "highlight": str(
                        match.get("highlight")
                        or _source_chunk_highlight(query_text, str(row["content"]))
                    ),
                    "score": round(
                        _normalize_lexical_score(float(match["score"])),
                        6,
                    ),
                    "match_reasons": list(match["reasons"]),
                    "org_id": str(row["org_id"]),
                    "project_id": str(row["project_id"]),
                    "topic_id": row["topic_id"],
                    "author_id": str(row["author_id"]),
                    "sensitivity": str(row["sensitivity"]),
                }
            )
        output.sort(
            key=lambda row: (
                -float(row["score"]),
                str(row["source_id"]),
                str(row["chunk_id"]),
            )
        )
        return output[: max(1, int(limit))]

    def search_memory(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        limit: int = 10,
        *,
        actor: User | None = None,
        access_context: AccessContext | None = None,
        as_of: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        filters = filters or {}
        embedding_enabled = _embedding_requested()
        query_text = (query or "").strip()
        expected_type = filters.get("type") or filters.get("category")
        requested_status = filters.get("status") or CandidateStatus.CONFIRMED.value
        if isinstance(requested_status, CandidateStatus):
            requested_status = requested_status.value
        requested_status = str(requested_status)
        project_id = str(filters.get("project_id") or "").strip()
        candidate_pool_limit = max(50, max(1, int(limit)))
        filters_applied = [
            f"{key}={value}"
            for key, value in (
                ("project_id", project_id),
                ("status", requested_status),
                ("type", str(expected_type or "")),
            )
            if value
        ]
        if diagnostics is not None:
            diagnostics.clear()
            diagnostics.update(
                {
                    "query": query_text,
                    "degraded": False,
                    "embedding_error": "",
                    "degradation_reasons": [],
                    "retrieval_mode": (
                        "fts_semantic_union"
                        if query_text and embedding_enabled
                        else ("fts" if query_text else "recent")
                    ),
                    "access_scope_applied": actor is not None,
                    "filters_applied": filters_applied,
                    "candidate_pool_limit": candidate_pool_limit,
                    "visible_candidate_count": 0,
                    "fts_candidate_count": 0,
                    "semantic_candidate_count": 0,
                    "union_candidate_count": 0,
                    "thread_collapsed_count": 0,
                    "returned_count": 0,
                    "embedding_model": _embedding_model() if embedding_enabled else "",
                    "embedding_index": {
                        "enabled": embedding_enabled,
                        "model": _embedding_model() if embedding_enabled else "",
                        "eligible": 0,
                        "missing": 0,
                        "pending": 0,
                        "running": 0,
                        "retrying": 0,
                        "indexed": 0,
                        "error": 0,
                        "complete": not embedding_enabled,
                    },
                }
            )
        if requested_status not in {CandidateStatus.CONFIRMED.value, CandidateStatus.CANDIDATE.value}:
            return []
        if (actor is None) != (access_context is None):
            raise ValueError("actor and access_context must be provided together")
        as_of_value = as_of or _now()
        as_of_dt = _parse_search_datetime(as_of_value)
        clauses = ["status = ?"]
        params: list[Any] = [requested_status]
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        with self._connect() as conn:
            rows = conn.execute(
                "select * from items where " + " and ".join(clauses),
                tuple(params),
            ).fetchall()
        eligible_items = [
            _inspection_item_from_dict(_item_payload_from_row(row))
            for row in rows
        ]
        if expected_type:
            eligible_items = [
                item
                for item in eligible_items
                if _item_type(item) == expected_type or item.category == expected_type
            ]
        if actor is not None and access_context is not None:
            eligible_items = visible_filter(actor, eligible_items, access_context)
        eligible_items = [
            item
            for item in eligible_items
            if _item_effective_datetime(item, self.get_source(_first_source_id(item))) <= as_of_dt
        ]
        eligible_by_id = {item.item_id: item for item in eligible_items}
        embedding_index = self._embedding_index_status_for_ids(
            list(eligible_by_id),
            enabled=embedding_enabled,
        )
        degradation_reasons: list[str] = []
        if embedding_enabled and not embedding_index["complete"]:
            degradation_reasons.append("embedding_index_incomplete")
        if diagnostics is not None:
            diagnostics.update(
                {
                    "visible_candidate_count": len(eligible_by_id),
                    "embedding_index": embedding_index,
                    "degraded": bool(degradation_reasons),
                    "degradation_reasons": list(degradation_reasons),
                }
            )
        if not eligible_by_id:
            return []

        match_meta: dict[str, dict[str, Any]] = {}
        if query_text:
            with self._connect() as conn:
                lexical_matches = _search_item_matches(
                    conn,
                    query_text,
                    limit=candidate_pool_limit,
                    eligible_ids=list(eligible_by_id),
                )
            for match in lexical_matches:
                lexical_score = _normalize_lexical_score(float(match["score"]))
                match["lexical_score"] = lexical_score
                match["relevance_score"] = lexical_score
                match["semantic_score"] = 0.0
            matches = lexical_matches
            semantic_matches: list[dict[str, Any]] = []
            degraded = bool(degradation_reasons)
            embedding_error = ""
            if embedding_enabled:
                try:
                    semantic_matches = self._semantic_item_matches(
                        query_text,
                        list(eligible_by_id),
                        limit=candidate_pool_limit,
                    )
                    matches = _merge_hybrid_memory_matches(
                        lexical_matches,
                        semantic_matches,
                    )
                except Exception as exc:
                    degraded = True
                    embedding_error = str(exc)
                    degradation_reasons.append("embedding_api_failure")
            if diagnostics is not None:
                diagnostics.update(
                    {
                        "degraded": degraded,
                        "embedding_error": embedding_error,
                        "degradation_reasons": list(dict.fromkeys(degradation_reasons)),
                        "fts_candidate_count": len(lexical_matches),
                        "semantic_candidate_count": len(semantic_matches),
                        "union_candidate_count": len(matches),
                    }
                )
            for match in matches:
                item_id = match["item_id"]
                if item_id not in eligible_by_id:
                    continue
                match_meta[item_id] = {**match, "degraded": degraded, "embedding_error": embedding_error}
        else:
            match_meta = {
                item.item_id: {
                    "score": 1.0,
                    "lexical_score": 1.0,
                    "semantic_score": 0.0,
                    "relevance_score": 1.0,
                    "reasons": ["recent"],
                    "highlight": _highlight_text(item, query_text),
                    "degraded": False,
                    "embedding_error": "",
                }
                for item in eligible_items
            }
            if diagnostics is not None:
                diagnostics["union_candidate_count"] = len(match_meta)
        half_life_days = max(1.0, float(os.getenv("MEMORY_RECENCY_HALF_LIFE_DAYS", "90")))
        grouped: dict[str, dict[str, Any]] = {}
        thread_cache: dict[str, dict[str, Any]] = {}
        for matched_item_id, meta in match_meta.items():
            item = eligible_by_id[matched_item_id]
            relevance = float(meta.get("relevance_score", meta.get("score", 0.0)))
            reasons = list(meta.get("reasons", []))
            if query_text and relevance <= 0:
                continue
            display_item = item
            thread = None
            if requested_status == CandidateStatus.CONFIRMED.value and item.thread_id:
                try:
                    thread = thread_cache.setdefault(
                        item.thread_id,
                        self.get_memory_thread(item.thread_id),
                    )
                except KeyError:
                    thread = None
                current_item_id = thread.get("current_item_id") if thread else None
                if current_item_id:
                    try:
                        current = self.get_item(current_item_id)
                    except KeyError:
                        current = None
                    if (
                        current is not None
                        and current.status == CandidateStatus.CONFIRMED
                        and current.item_id in eligible_by_id
                        and _item_effective_datetime(current, self.get_source(_first_source_id(current))) <= as_of_dt
                    ):
                        display_item = current
            matched_effective = _item_effective_datetime(
                item,
                self.get_source(_first_source_id(item)),
            )
            is_current = not thread or display_item.item_id == thread.get("current_item_id")
            source_count = _confirmed_thread_source_count(self, thread["id"]) if thread else max(
                1,
                len({ref.source_doc_id for ref in display_item.evidence_refs}),
            )
            support_score = 0.05 * min(source_count, 5) / 5.0
            if display_item.item_id != item.item_id:
                reasons.append("historical_thread_match")
            source_id = _first_source_id(display_item)
            display_effective = _item_effective_datetime(
                display_item,
                self.get_source(source_id),
            )
            age_days = max(0.0, (as_of_dt - display_effective).total_seconds() / 86400.0)
            recency_score = 2.0 ** (-age_days / half_life_days)
            score = min(
                1.0,
                0.65 * relevance + 0.20 * recency_score + 0.10 * (1.0 if is_current else 0.0) + support_score,
            )
            row = {
                "item_id": display_item.item_id,
                "id": display_item.item_id,
                "org_id": display_item.org_id,
                "project_id": display_item.project_id,
                "topic_id": display_item.topic_id,
                "author_id": display_item.author_id,
                "sensitivity": display_item.sensitivity,
                "type": _item_type(display_item),
                "category": display_item.category,
                "status": display_item.status.value,
                "title": display_item.title,
                "description": display_item.description,
                "owner": "、".join(display_item.owner_candidates or []),
                "due_date": display_item.due_date or "",
                "deliverable": display_item.deliverable or "",
                "acceptance": display_item.acceptance_criteria or "",
                "score": round(score, 6),
                "relevance_score": round(relevance, 6),
                "lexical_score": round(float(meta.get("lexical_score", 0.0)), 6),
                "semantic_score": round(float(meta.get("semantic_score", 0.0)), 6),
                "recency_score": round(recency_score, 6),
                "age_days": round(age_days, 3),
                "as_of": as_of_dt.isoformat(),
                "effective_at": display_effective.isoformat(),
                "matched_effective_at": matched_effective.isoformat(),
                "matched_item_id": item.item_id,
                "is_current": bool(is_current),
                "thread_id": thread["id"] if thread else display_item.thread_id,
                "current_item_id": thread.get("current_item_id") if thread else display_item.item_id,
                "lifecycle_status": thread.get("status") if thread else "active",
                "first_seen_at": thread.get("first_seen_at") if thread else display_effective.isoformat(),
                "last_seen_at": thread.get("last_seen_at") if thread else display_effective.isoformat(),
                "source_count": source_count,
                "delta_type": display_item.thread_event,
                "changed_fields": list(display_item.changed_fields),
                "match_reasons": reasons,
                "highlight": meta.get("highlight") or _highlight_text(item, query_text),
                "degraded": bool(meta.get("degraded", False)),
                "embedding_error": str(meta.get("embedding_error", "")),
                "evidence_refs": [to_plain(ref) for ref in display_item.evidence_refs],
                "evidence_timeline": _confirmed_thread_timeline(self, thread["id"]) if thread else [],
                "source": _source_citation(display_item, self.get_source(source_id), source_id),
            }
            group_key = thread["id"] if thread else display_item.item_id
            existing = grouped.get(group_key)
            if existing is None or (
                -row["relevance_score"],
                -row["score"],
                row["matched_item_id"],
            ) < (
                -existing["relevance_score"],
                -existing["score"],
                existing["matched_item_id"],
            ):
                grouped[group_key] = row
        scored = list(grouped.values())
        scored.sort(
            key=lambda row: (
                -row["score"],
                -_parse_search_datetime(str(row.get("last_seen_at") or "1970-01-01")).timestamp(),
                str(row.get("thread_id") or row["item_id"]),
            )
        )
        output = scored[: max(1, int(limit))]
        if diagnostics is not None:
            diagnostics.update(
                {
                    "thread_collapsed_count": len(scored),
                    "returned_count": len(output),
                }
            )
        return output

    def read_context(
        self,
        purpose: str,
        query: str,
        token_budget: int,
        *,
        actor: User | None = None,
        access_context: AccessContext | None = None,
        project_id: str | None = None,
        include_retrieval: bool = True,
        additional_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        budget = max(64, int(token_budget or 0))
        token_counter = get_token_counter()
        resume_items = [
            item
            for item in self.list_items()
            if item.status != CandidateStatus.ARCHIVED
            and (project_id is None or item.project_id == project_id)
        ]
        if actor is not None and access_context is not None:
            resume_items = visible_filter(actor, resume_items, access_context)
        resume = _build_dynamic_resume(resume_items).get(
            "user_facing_one_liner",
            "当前没有可恢复的项目记忆。",
        )
        resume = token_counter.truncate(resume, min(80, budget))
        milestone = None
        milestone_source = "none"
        if project_id:
            project_config = self.get_project_config(project_id)
            if project_config:
                milestone = milestone_from_dict(project_config["milestone_payload"])
                milestone_source = "project_configs"
        else:
            latest_run = self.latest_run()
            if latest_run:
                milestone = latest_run.milestone_plan
                milestone_source = "latest_run_compatibility"
        milestone_text = token_counter.truncate(
            _milestone_context_text(milestone),
            min(180, budget),
        )
        retrieval_diagnostics: dict[str, Any] = {}
        retrieval_rows = []
        if include_retrieval and query:
            retrieval_rows = self.search_memory(
                query,
                {"project_id": project_id},
                limit=10,
                actor=actor,
                access_context=access_context,
                diagnostics=retrieval_diagnostics,
            )
        header = f"上下文预算：purpose={purpose}，budget={budget} tokens。"
        base_lines = [
            header,
            f"会话恢复：{resume}",
            f"里程碑上下文：{milestone_text or '当前没有已运行里程碑上下文。'}",
        ]
        additional_text = ""
        if additional_context:
            additional_prefix = "角色工作上下文："
            remaining = max(
                0,
                budget
                - token_counter.count("\n".join(base_lines))
                - token_counter.count(additional_prefix)
                - 24,
            )
            additional_text = token_counter.truncate(
                json.dumps(
                    additional_context,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
                min(300, remaining),
            )
            if additional_text:
                base_lines.append(f"{additional_prefix}{additional_text}")
        if include_retrieval:
            retrieval_prefix = "检索结果：\n"
            base_with_prefix = "\n".join([*base_lines, retrieval_prefix])
            retrieval_budget = max(
                0,
                budget - token_counter.count(base_with_prefix),
            )
            retrieval_text = token_counter.truncate(
                _retrieval_context_text(retrieval_rows)
                or "本轮没有命中项目记忆。",
                retrieval_budget,
            )
            content = "\n".join([*base_lines, f"{retrieval_prefix}{retrieval_text}"])
        else:
            retrieval_text = ""
            content = "\n".join(
                [
                    *base_lines,
                    "详细项目事实：未自动预取；需要时由模型调用检索工具。",
                ]
            )
        content = token_counter.truncate(content, budget)
        token_count = token_counter.count(content)
        snapshot_payload = {
            "project_id": project_id or "",
            "milestone": to_plain(milestone) if milestone else {},
            "resume": resume,
            "include_retrieval": include_retrieval,
            "retrieval_item_ids": [row.get("item_id", "") for row in retrieval_rows],
            "additional_context": additional_context or {},
        }
        snapshot_id = "ctx_" + hashlib.sha256(
            _json(snapshot_payload).encode("utf-8")
        ).hexdigest()[:16]
        return {
            "snapshot_id": snapshot_id,
            "project_id": project_id or "",
            "as_of": _now(),
            "milestone_source": milestone_source,
            "purpose": purpose,
            "query": query,
            "budget": budget,
            "budget_unit": "tokens",
            "token_counter": token_counter.encoding_name,
            "token_count": token_count,
            "char_count": len(content),
            "content": content,
            "budgets": {
                "resume_tokens": token_counter.count(resume),
                "milestone_tokens": token_counter.count(milestone_text),
                "retrieval_tokens": token_counter.count(retrieval_text),
                "additional_tokens": token_counter.count(additional_text),
                "resume_chars": len(resume),
                "milestone_chars": len(milestone_text),
                "retrieval_chars": len(retrieval_text),
                "additional_chars": len(additional_text),
            },
            "retrieval_items": retrieval_rows,
            "degraded": bool(
                retrieval_diagnostics.get("degraded")
                or any(row.get("degraded") for row in retrieval_rows)
            ),
            "retrieval_diagnostics": retrieval_diagnostics,
        }

    def generate_vault_mirror(self) -> None:
        with _STORE_LOCK:
            self._generate_vault_mirror()

    def _generate_vault_mirror(self) -> None:
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        method_dir = self.vault_dir / "方法库"
        meeting_dir = self.vault_dir / "会议"
        brief_dir = self.vault_dir / "晨报"
        weekly_review_dir = self.vault_dir / "周复盘"
        method_dir.mkdir(parents=True, exist_ok=True)
        meeting_dir.mkdir(parents=True, exist_ok=True)
        brief_dir.mkdir(parents=True, exist_ok=True)
        weekly_review_dir.mkdir(parents=True, exist_ok=True)
        for directory in (method_dir, meeting_dir, brief_dir, weekly_review_dir):
            for markdown_file in directory.glob("*.md"):
                markdown_file.unlink()

        confirmed_methods = [
            item
            for item in self.list_items(status=CandidateStatus.CONFIRMED)
            if _item_type(item) == "method"
        ]
        for item in confirmed_methods:
            (method_dir / f"{_safe_filename(item.title or item.item_id)}.md").write_text(
                _method_markdown(item),
                encoding="utf-8",
            )

        for source in self.list_sources():
            if source.get("kind") not in {"minutes", "transcript"}:
                continue
            related = [
                item
                for item in self.list_items()
                if any(ref.source_doc_id == source["id"] for ref in item.evidence_refs)
            ]
            date_prefix = source.get("meeting_date") or "未注明日期"
            filename = f"{date_prefix}-{_safe_filename(source.get('title') or source['id'])}.md"
            (meeting_dir / filename).write_text(
                _meeting_markdown(source, related),
                encoding="utf-8",
            )
        for source in self.list_sources():
            if source.get("kind") != "brief":
                continue
            date_prefix = source.get("meeting_date") or "未注明日期"
            filename = f"{date_prefix}-{_safe_filename(source.get('title') or source['id'])}.md"
            (brief_dir / filename).write_text(
                _brief_markdown(source),
                encoding="utf-8",
            )
        for source in self.list_sources():
            if source.get("kind") != "weekly_review":
                continue
            date_prefix = source.get("meeting_date") or "未注明日期"
            filename = f"{date_prefix}-{_safe_filename(source.get('title') or source['id'])}.md"
            (weekly_review_dir / filename).write_text(
                _brief_markdown(source),
                encoding="utf-8",
            )

    def _merge_items(self, incoming: list[InspectionItem]) -> list[str]:
        with _STORE_LOCK:
            confirmed_ids: list[str] = []
            for item in incoming:
                existing_payload = self._get_item_payload(item.item_id)
                if existing_payload:
                    stored = _inspection_item_from_dict(existing_payload)
                    item.status = stored.status
                    item.confirmation_notes = stored.confirmation_notes
                    item.confirmation_editor = stored.confirmation_editor
                    item.updated_at = stored.updated_at
                    if stored.confirmation_editor:
                        item.title = stored.title
                        item.description = stored.description
                    if stored.status == CandidateStatus.CONFIRMED:
                        confirmed_ids.append(stored.item_id)
                payload = to_plain(item)
                if existing_payload != payload:
                    self._upsert_item_payload(payload)
                    self._append_event("item", item.item_id, "candidate_upserted", payload)
            self.generate_vault_mirror()
            return confirmed_ids

    def _set_item_status(
        self,
        item_id: str,
        status: CandidateStatus,
        editor: str,
        notes: str,
    ) -> InspectionItem:
        with _STORE_LOCK:
            item = self.get_item(item_id)
            item.status = status
            item.confirmation_editor = editor
            item.confirmation_notes = notes
            item.updated_at = _now()
            payload = to_plain(item)
            self._upsert_item_payload(payload)
            self._append_event(
                "item",
                item.item_id,
                _item_status_action(status),
                payload,
            )
            if item.thread_id:
                self.refresh_memory_thread(item.thread_id)
            self.generate_vault_mirror()
            return item

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self.database.connect() as conn:
            yield conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists runs(
                  id text primary key,
                  milestone_id text,
                  project_id text not null default 'project_mvp',
                  status text,
                  archived integer not null default 0,
                  created_at text,
                  payload text not null
                );

                create table if not exists sources(
                  id text primary key,
                  kind text not null,
                  title text not null,
                  meeting_date text,
                  path text,
                  org_id text,
                  project_id text,
                  topic_id text,
                  author_id text,
                  sensitivity text default 'l1',
                  tag_origin text not null default 'runtime_default',
                  input_kind text not null default 'auto',
                  current_ingestion_job_id text not null default '',
                  created_at text,
                  updated_at text not null default '',
                  payload text not null
                );

                create table if not exists calendar_events(
                  id text primary key,
                  project_id text not null,
                  event_date text not null,
                  title text not null,
                  org_id text not null,
                  topic_id text not null default '',
                  author_id text not null,
                  sensitivity text not null default 'l1',
                  payload text not null,
                  created_at text not null,
                  updated_at text not null
                );

                create index if not exists idx_calendar_events_project_date
                  on calendar_events(project_id, event_date);

                create table if not exists source_chunks(
                  id text primary key,
                  source_id text not null,
                  chunk_index integer not null,
                  title text not null,
                  locator text not null,
                  content text not null,
                  content_hash text not null,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  created_at text not null,
                  updated_at text not null,
                  unique(source_id, chunk_index)
                );

                create table if not exists source_chunk_index_status(
                  source_id text primary key,
                  path text not null default '',
                  status text not null,
                  reason text not null default '',
                  content_hash text not null default '',
                  chunk_count integer not null default 0,
                  updated_at text not null
                );

                create table if not exists items(
                  id text primary key,
                  type text not null,
                  status text not null,
                  title text not null,
                  body text,
                  owner text,
                  due_date text,
                  deliverable text,
                  acceptance text,
                  evidence_refs text,
                  source_id text,
                  edited_by_human integer not null default 0,
                  org_id text,
                  project_id text,
                  topic_id text,
                  author_id text,
                  sensitivity text default 'l1',
                  tag_origin text not null default 'runtime_default',
                  created_at text not null,
                  updated_at text not null,
                  payload text not null
                );

                create table if not exists tasks(
                  id text primary key,
                  item_id text,
                  title text not null,
                  status text not null,
                  owner text,
                  due_date text,
                  deliverable text,
                  acceptance text,
                  milestone_id text,
                  payload text not null,
                  created_at text,
                  updated_at text
                );

                create table if not exists events(
                  id text primary key,
                  entity text not null,
                  entity_id text not null,
                  action text not null,
                  payload text not null,
                  created_at text not null
                );

                create table if not exists sessions(
                  id text primary key,
                  project_id text not null default 'project_mvp',
                  actor_id text not null default 'u_pm',
                  title text not null,
                  created_at text not null,
                  updated_at text not null,
                  archived integer not null default 0,
                  title_edited_by_human integer not null default 0
                );

                create table if not exists messages(
                  id text primary key,
                  session_id text not null,
                  role text not null,
                  content text not null,
                  tool_calls text,
                  created_at text not null,
                  metadata text,
                  foreign key(session_id) references sessions(id) on delete cascade
                );

                create table if not exists people(
                  name text primary key,
                  org text,
                  role text,
                  responsibilities text,
                  aliases text,
                  payload text
                );

                create table if not exists person_entities(
                  org_id text not null,
                  project_id text not null,
                  person_id text not null,
                  name text not null,
                  identity_status text not null,
                  source_id text not null,
                  author_id text not null,
                  sensitivity text not null,
                  payload text not null,
                  updated_at text not null,
                  primary key(org_id, project_id, person_id)
                );

                create table if not exists person_assignments(
                  org_id text not null,
                  project_id text not null,
                  assignment_id text not null,
                  person_id text not null,
                  group_name text not null,
                  role text not null,
                  path text not null,
                  responsibility_note text not null,
                  source_id text not null,
                  author_id text not null,
                  sensitivity text not null,
                  payload text not null,
                  updated_at text not null,
                  primary key(org_id, project_id, assignment_id)
                );

                create table if not exists orgs(
                  id text primary key,
                  name text not null,
                  created_at text not null
                );

                create table if not exists users(
                  id text primary key,
                  org_id text not null,
                  name text not null,
                  feishu_id text,
                  created_at text not null
                );

                create table if not exists projects(
                  id text primary key,
                  org_id text not null,
                  name text not null,
                  owner_id text not null,
                  created_at text not null
                );

                create table if not exists project_configs(
                  project_id text primary key,
                  org_id text not null,
                  source_profile text not null default 'generic',
                  milestone_payload text not null,
                  migration_origin text not null default 'runtime',
                  updated_by text not null,
                  updated_at text not null
                );

                create table if not exists project_members(
                  id text not null,
                  org_id text not null,
                  project_id text not null,
                  user_id text not null,
                  role text not null,
                  manager_id text,
                  primary key(project_id, user_id)
                );

                create table if not exists topics(
                  id text primary key,
                  org_id text not null,
                  project_id text not null,
                  name text not null,
                  created_by text not null,
                  created_at text not null
                );

                create table if not exists topic_members(
                  org_id text not null,
                  project_id text not null,
                  topic_id text not null,
                  user_id text not null,
                  primary key(topic_id, user_id)
                );

                create table if not exists ingestion_jobs(
                  id text primary key,
                  source_id text not null,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  actor_id text not null,
                  input_kind text not null,
                  content_hash text not null,
                  processor_version text not null,
                  input_path text not null,
                  status text not null,
                  stage text not null default 'queued',
                  total_chunks integer not null default 0,
                  processed_chunks integer not null default 0,
                  candidate_count integer not null default 0,
                  delta_new integer not null default 0,
                  delta_updated integer not null default 0,
                  delta_conflict integer not null default 0,
                  delta_resolved integer not null default 0,
                  delta_auto_merged integer not null default 0,
                  attempts integer not null default 0,
                  max_attempts integer not null default 3,
                  retry_at text not null default '',
                  lease_owner text not null default '',
                  lease_expires_at text not null default '',
                  minutes_path text not null default '',
                  minutes_hash text not null default '',
                  error text not null default '',
                  created_at text not null,
                  updated_at text not null,
                  payload text not null,
                  unique(source_id, content_hash, processor_version)
                );

                create table if not exists ingestion_chunks(
                  id text primary key,
                  job_id text not null,
                  source_id text not null,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  chunk_index integer not null,
                  start_char integer not null,
                  end_char integer not null,
                  content_hash text not null,
                  status text not null,
                  attempts integer not null default 0,
                  summary text not null default '',
                  error text not null default '',
                  payload text not null,
                  updated_at text not null,
                  unique(job_id, chunk_index)
                );

                create table if not exists memory_threads(
                  id text primary key,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  category text not null,
                  thread_key text not null,
                  title text not null,
                  status text not null,
                  current_item_id text,
                  first_seen_at text not null,
                  last_seen_at text not null,
                  payload text not null
                );

                create table if not exists memory_thread_items(
                  thread_id text not null,
                  item_id text not null,
                  ingestion_job_id text not null default '',
                  source_id text not null,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  relation text not null,
                  supersedes_item_id text not null default '',
                  effective_at text not null,
                  review_required integer not null default 1,
                  review_status text not null default 'candidate',
                  changed_fields text not null default '[]',
                  evidence_refs text not null default '[]',
                  created_at text not null,
                  updated_at text not null,
                  primary key(thread_id, item_id)
                );

                create table if not exists memory_thread_sources(
                  thread_id text not null,
                  ingestion_job_id text not null,
                  source_id text not null,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  relation text not null,
                  effective_at text not null,
                  evidence_refs text not null default '[]',
                  created_at text not null,
                  updated_at text not null,
                  primary key(thread_id, ingestion_job_id, source_id)
                );

                create table if not exists project_skills(
                  id text primary key,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  slug text not null,
                  name text not null,
                  status text not null,
                  source_method_id text not null,
                  active_version_id text not null default '',
                  created_at text not null,
                  updated_at text not null,
                  payload text not null,
                  unique(project_id, slug)
                );

                create table if not exists project_skill_versions(
                  id text primary key,
                  skill_id text not null,
                  version integer not null,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  status text not null,
                  description text not null,
                  when_to_use text not null,
                  tool_names text not null,
                  body_markdown text not null,
                  content_hash text not null,
                  readiness text not null,
                  created_by text not null,
                  created_at text not null,
                  published_at text not null default '',
                  payload text not null,
                  unique(skill_id, version)
                );

                create table if not exists project_skill_evidence(
                  id text primary key,
                  skill_id text not null,
                  version_id text not null,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  source_method_id text not null,
                  source_doc_id text not null,
                  locator text not null,
                  quote text not null,
                  created_at text not null,
                  payload text not null
                );

                create table if not exists project_skill_tests(
                  id text primary key,
                  skill_id text not null,
                  version_id text not null,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  name text not null,
                  input_text text not null,
                  should_trigger integer not null,
                  selected integer,
                  passed integer,
                  status text not null,
                  error text not null default '',
                  created_at text not null,
                  updated_at text not null,
                  payload text not null
                );

                create table if not exists daily_journals(
                  id text primary key,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  report_date text not null,
                  active_version_id text not null default '',
                  status text not null,
                  last_error text not null default '',
                  created_at text not null,
                  updated_at text not null,
                  payload text not null,
                  unique(project_id, author_id, report_date)
                );

                create table if not exists daily_journal_versions(
                  id text primary key,
                  journal_id text not null,
                  version integer not null,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  report_date text not null,
                  snapshot_hash text not null,
                  message_count integer not null,
                  status text not null,
                  organization_status text not null default 'pending',
                  source_id text not null default '',
                  ingestion_job_id text not null default '',
                  content_markdown text not null default '',
                  error text not null default '',
                  created_at text not null,
                  updated_at text not null,
                  payload text not null,
                  unique(journal_id, version),
                  unique(journal_id, snapshot_hash)
                );

                create table if not exists daily_work_records(
                  id text primary key,
                  series_id text not null,
                  source_id text not null,
                  source_origin text not null,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  subject_user_id text not null default '',
                  subject_name text not null,
                  record_date text not null,
                  kind text not null,
                  text text not null,
                  solution_options text not null default '[]',
                  evidence_refs text not null,
                  source_locator text not null default '',
                  status text not null default 'active',
                  edited_by_human integer not null default 0,
                  updated_by text not null default '',
                  created_at text not null,
                  updated_at text not null,
                  payload text not null
                );

                create table if not exists deliverables(
                  id text primary key,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  milestone_id text not null,
                  title text not null,
                  type_label text not null default '',
                  required integer not null default 1,
                  due_date text not null default '',
                  acceptance_criteria text not null default '',
                  status text not null,
                  sort_order integer not null default 0,
                  current_version integer not null default 0,
                  created_at text not null,
                  updated_at text not null,
                  payload text not null
                );

                create table if not exists deliverable_versions(
                  id text primary key,
                  deliverable_id text not null,
                  version integer not null,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  submitted_by text not null,
                  status text not null,
                  note text not null default '',
                  created_at text not null,
                  payload text not null,
                  unique(deliverable_id, version)
                );

                create table if not exists deliverable_files(
                  id text primary key,
                  deliverable_id text not null,
                  version_id text not null,
                  org_id text not null,
                  project_id text not null,
                  topic_id text,
                  author_id text not null,
                  sensitivity text not null,
                  artifact_role text not null,
                  original_name text not null,
                  relative_path text not null,
                  content_hash text not null,
                  content_type text not null default '',
                  position integer not null default 0,
                  size integer not null,
                  created_at text not null,
                  payload text not null
                );

                create unique index if not exists memory_threads_scope_key
                on memory_threads(
                  org_id, project_id, ifnull(topic_id, ''), sensitivity, category, thread_key
                );

                create index if not exists ingestion_jobs_status_updated
                on ingestion_jobs(status, retry_at, updated_at, id);

                create index if not exists memory_thread_items_source
                on memory_thread_items(source_id, ingestion_job_id, thread_id);

                create index if not exists memory_thread_sources_source
                on memory_thread_sources(source_id, ingestion_job_id, thread_id);

                create index if not exists source_chunks_scope_source
                on source_chunks(project_id, source_id, chunk_index);

                create index if not exists project_skills_scope_status
                on project_skills(project_id, status, updated_at, id);

                create index if not exists project_skill_versions_skill
                on project_skill_versions(skill_id, version desc);

                create index if not exists daily_journals_scope_date
                on daily_journals(project_id, author_id, report_date desc);

                create index if not exists daily_work_records_project_date
                on daily_work_records(project_id, record_date desc, id);

                create index if not exists daily_work_records_project_subject
                on daily_work_records(project_id, subject_user_id, record_date desc);

                create index if not exists daily_work_records_source
                on daily_work_records(source_id, source_origin, status);

                create index if not exists deliverables_project_milestone
                on deliverables(project_id, milestone_id, status, sort_order, id);

                create index if not exists deliverable_versions_deliverable
                on deliverable_versions(deliverable_id, version desc);

                create index if not exists deliverable_files_version
                on deliverable_files(version_id, id);

                """
            )
            _ensure_column(conn, "people", "profile_payload", "text")
            _ensure_column(conn, "people", "current_load", "integer")
            _ensure_column(conn, "people", "avg_completion_days", "real")
            _ensure_column(conn, "people", "profile_updated_at", "text")
            _ensure_column(conn, "runs", "project_id", "text not null default 'project_mvp'")
            _ensure_column(conn, "runs", "archived", "integer not null default 0")
            _ensure_column(conn, "sessions", "project_id", "text not null default 'project_mvp'")
            _ensure_column(conn, "sessions", "actor_id", "text not null default 'u_pm'")
            _ensure_column(conn, "sessions", "archived", "integer not null default 0")
            _ensure_column(conn, "sessions", "title_edited_by_human", "integer not null default 0")
            _ensure_column(conn, "project_members", "manager_id", "text")
            _ensure_column(conn, "deliverable_files", "position", "integer not null default 0")
            for table in ("sources", "items"):
                _ensure_column(conn, table, "org_id", "text")
                _ensure_column(conn, table, "project_id", "text")
                _ensure_column(conn, table, "topic_id", "text")
                _ensure_column(conn, table, "author_id", "text")
                _ensure_column(conn, table, "sensitivity", "text default 'l1'")
                _ensure_column(conn, table, "tag_origin", "text not null default 'runtime_default'")
            _ensure_column(conn, "sources", "input_kind", "text not null default 'auto'")
            _ensure_column(conn, "sources", "current_ingestion_job_id", "text not null default ''")
            _ensure_column(conn, "sources", "updated_at", "text not null default ''")
            try:
                conn.execute(
                    """
                    create virtual table if not exists items_fts
                    using fts5(id unindexed, title, body, owner, evidence, tokenize='unicode61')
                    """
                )
            except sqlite3.OperationalError as exc:
                raise RuntimeError("SQLite FTS5 is required for memory search") from exc
            try:
                conn.execute(
                    """
                    create virtual table if not exists source_chunks_fts
                    using fts5(
                      id unindexed,
                      source_id unindexed,
                      title,
                      content,
                      tokenize='unicode61'
                    )
                    """
                )
            except sqlite3.OperationalError as exc:
                raise RuntimeError("SQLite FTS5 is required for source evidence search") from exc
            conn.execute(
                """
                create table if not exists embeddings(
                  item_id text primary key,
                  model text not null default '',
                  dimensions integer not null default 0,
                  vector text not null,
                  updated_at text not null
                )
                """
            )
            _ensure_column(conn, "embeddings", "model", "text not null default ''")
            _ensure_column(conn, "embeddings", "dimensions", "integer not null default 0")
            _ensure_column(conn, "embeddings", "updated_at", "text not null default ''")
            _ensure_column(conn, "embeddings", "content_hash", "text not null default ''")
            _ensure_column(conn, "embeddings", "status", "text not null default 'pending'")
            _ensure_column(conn, "embeddings", "last_error", "text not null default ''")
            _ensure_column(conn, "embeddings", "attempts", "integer not null default 0")
            _ensure_column(conn, "embeddings", "next_retry_at", "text not null default ''")
            _ensure_column(conn, "embeddings", "worker_id", "text not null default ''")
            conn.execute(
                """
                delete from embeddings
                where trim(coalesce(model, '')) = ''
                   or (
                     status = 'indexed'
                     and (
                       dimensions <= 0
                       or trim(coalesce(vector, '')) in ('', '[]')
                     )
                   )
                """
            )
            _backfill_access_tags(conn)
            conn.commit()

    def _migrate_legacy_files(self) -> None:
        legacy_paths = [self.root_dir / name for name in self.LEGACY_JSON_FILES]
        memory_paths = [self.root_dir / "memory" / name for name in self.LEGACY_MEMORY_FILES]
        existing_paths = [path for path in [*legacy_paths, *memory_paths] if path.exists()]
        if not existing_paths:
            return

        runs_path = self.root_dir / "agent_runs.json"
        if runs_path.exists():
            for run_id, payload in _loads(runs_path.read_text(encoding="utf-8"), {}).items():
                payload.setdefault("run_id", run_id)
                self._upsert_run_payload(payload)

        items_path = self.root_dir / "candidate_items.json"
        if items_path.exists():
            for item_id, payload in _loads(items_path.read_text(encoding="utf-8"), {}).items():
                payload.setdefault("item_id", item_id)
                self._upsert_item_payload(payload)

        work_items_path = self.root_dir / "work_items.json"
        if work_items_path.exists():
            for work_item_id, payload in _loads(work_items_path.read_text(encoding="utf-8"), {}).items():
                payload.setdefault("work_item_id", work_item_id)
                self._upsert_task_payload(payload)

        work_events_path = self.root_dir / "work_item_events.jsonl"
        if work_events_path.exists():
            for record in _read_jsonl(work_events_path):
                entity_id = record.get("work_item_id") or record.get("source_candidate_id") or "legacy_task_event"
                self._append_event("task", entity_id, record.get("event_type", "legacy_work_item_event"), record)

        sessions_path = self.root_dir / "sessions.jsonl"
        if sessions_path.exists():
            self._migrate_sessions(_read_jsonl(sessions_path))

        memory_events_path = self.root_dir / "memory" / "events.jsonl"
        if memory_events_path.exists():
            for record in _read_jsonl(memory_events_path):
                entity_id = record.get("item_id") or record.get("entity_id") or "legacy_memory_event"
                self._append_event("item", entity_id, record.get("event_type", "legacy_memory_event"), record)

        self._archive_paths(existing_paths)
        self.generate_vault_mirror()

    def _migrate_sessions(self, records: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            for record in records:
                if record.get("record_type") == "session":
                    conn.execute(
                        """
                        insert into sessions(id, title, created_at, updated_at, archived, title_edited_by_human)
                        values(?, ?, ?, ?, ?, ?)
                        on conflict(id) do update set
                          title=excluded.title,
                          updated_at=excluded.updated_at
                        """,
                        (
                            record["session_id"],
                            record.get("title", "未命名会话"),
                            record.get("created_at", _now()),
                            record.get("updated_at", record.get("created_at", _now())),
                            1 if record.get("archived") else 0,
                            1 if record.get("title_edited_by_human") else 0,
                        ),
                    )
                elif record.get("record_type") == "message":
                    session_id = record.get("session_id", "")
                    if not session_id:
                        continue
                    created_at = record.get("created_at", _now())
                    conn.execute(
                        """
                        insert into sessions(id, title, created_at, updated_at, archived, title_edited_by_human)
                        values(?, ?, ?, ?, 0, 0)
                        on conflict(id) do update set updated_at=max(updated_at, excluded.updated_at)
                        """,
                        (session_id, _session_title(record.get("content", "")), created_at, created_at),
                    )
                    conn.execute(
                        """
                        insert or ignore into messages(id, session_id, role, content, tool_calls, created_at, metadata)
                        values(?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.get("message_id") or f"msg_{uuid4().hex[:12]}",
                            session_id,
                            record.get("role", "user"),
                            record.get("content", ""),
                            _json(record.get("tool_calls", [])),
                            created_at,
                            _json(record.get("metadata", {})),
                        ),
                    )
            conn.commit()

    def _archive_paths(self, paths: list[Path]) -> None:
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        for path in paths:
            if not path.exists():
                continue
            if path.parent.name == "memory":
                target_dir = self.archive_dir / "memory"
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / path.name
            else:
                target = self.archive_dir / path.name
            if target.exists():
                target = target.with_name(f"{target.stem}-{datetime.now().strftime('%Y%m%d%H%M%S')}{target.suffix}")
            shutil.move(str(path), str(target))
        memory_dir = self.root_dir / "memory"
        if memory_dir.exists() and not any(memory_dir.iterdir()):
            memory_dir.rmdir()

    def _upsert_run_payload(self, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into runs(id, milestone_id, status, created_at, payload)
                values(?, ?, ?, ?, ?)
                on conflict(id) do update set
                  milestone_id=excluded.milestone_id,
                  status=excluded.status,
                  created_at=excluded.created_at,
                  payload=excluded.payload
                """,
                (
                    payload["run_id"],
                    payload.get("milestone_id", ""),
                    payload.get("status", "completed"),
                    payload.get("created_at", _now()),
                    _json(payload),
                ),
            )
            conn.commit()

    def _get_item_payload(self, item_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("select * from items where id = ?", (item_id,)).fetchone()
        return _item_payload_from_row(row) if row else None

    def _upsert_item_payload(self, payload: dict[str, Any]) -> None:
        item = _inspection_item_from_dict(payload)
        now = _now()
        existing = self._get_item_payload(item.item_id)
        created_at = payload.get("created_at") or (existing or {}).get("created_at") or now
        updated_at = item.updated_at or payload.get("updated_at") or now
        source_id = item.evidence_refs[0].source_doc_id if item.evidence_refs else payload.get("source_id", "")
        evidence_refs = _json([to_plain(ref) for ref in item.evidence_refs])
        owner = "、".join(item.owner_candidates or [])
        edited_by_human = 1 if item.confirmation_editor else int(payload.get("edited_by_human", 0) or 0)
        stored_payload = {
            **payload,
            "created_at": created_at,
            "updated_at": updated_at,
            "org_id": item.org_id,
            "project_id": item.project_id,
            "topic_id": item.topic_id,
            "author_id": item.author_id,
            "sensitivity": item.sensitivity,
            "proposed_sensitivity": item.proposed_sensitivity,
            "sensitivity_reason": item.sensitivity_reason,
            "tag_origin": item.tag_origin,
        }
        with self._connect() as conn:
            conn.execute(
                """
                insert into items(
                  id, type, status, title, body, owner, due_date, deliverable, acceptance,
                  evidence_refs, source_id, edited_by_human, org_id, project_id, topic_id,
                  author_id, sensitivity, tag_origin, created_at, updated_at, payload
                )
                values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                  type=excluded.type,
                  status=excluded.status,
                  title=excluded.title,
                  body=excluded.body,
                  owner=excluded.owner,
                  due_date=excluded.due_date,
                  deliverable=excluded.deliverable,
                  acceptance=excluded.acceptance,
                  evidence_refs=excluded.evidence_refs,
                  source_id=excluded.source_id,
                  edited_by_human=excluded.edited_by_human,
                  org_id=excluded.org_id,
                  project_id=excluded.project_id,
                  topic_id=excluded.topic_id,
                  author_id=excluded.author_id,
                  sensitivity=excluded.sensitivity,
                  tag_origin=excluded.tag_origin,
                  updated_at=excluded.updated_at,
                  payload=excluded.payload
                """,
                (
                    item.item_id,
                    _item_type(item),
                    item.status.value,
                    item.title,
                    item.description,
                    owner,
                    item.due_date or "",
                    item.deliverable or "",
                    item.acceptance_criteria or "",
                    evidence_refs,
                    source_id,
                    edited_by_human,
                    item.org_id,
                    item.project_id,
                    item.topic_id,
                    item.author_id,
                    item.sensitivity,
                    item.tag_origin,
                    created_at,
                    updated_at,
                    _json(stored_payload),
                ),
            )
            conn.execute("delete from items_fts where id = ?", (item.item_id,))
            conn.execute(
                "insert into items_fts(id, title, body, owner, evidence) values(?, ?, ?, ?, ?)",
                (
                    item.item_id,
                    _fts_index_text(item.title),
                    _fts_index_text(item.description),
                    _fts_index_text(owner),
                    _fts_index_text(evidence_refs),
                ),
            )
            conn.commit()
        if _embedding_requested():
            self._queue_item_embedding(item)

    def _get_task_payload(self, work_item_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("select payload from tasks where id = ?", (work_item_id,)).fetchone()
        return _loads(row["payload"], {}) if row else None

    def get_work_item(self, work_item_id: str) -> WorkItem:
        payload = self._get_task_payload(work_item_id)
        if payload is None:
            raise KeyError(work_item_id)
        return _work_item_from_dict(payload)

    def _upsert_task_payload(self, payload: dict[str, Any]) -> None:
        item = _work_item_from_dict(payload)
        owner = "、".join(item.owner_candidates or [])
        with self._connect() as conn:
            conn.execute(
                """
                insert into tasks(
                  id, item_id, title, status, owner, due_date, deliverable, acceptance,
                  milestone_id, payload, created_at, updated_at
                )
                values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                  item_id=excluded.item_id,
                  title=excluded.title,
                  status=excluded.status,
                  owner=excluded.owner,
                  due_date=excluded.due_date,
                  deliverable=excluded.deliverable,
                  acceptance=excluded.acceptance,
                  milestone_id=excluded.milestone_id,
                  payload=excluded.payload,
                  updated_at=excluded.updated_at
                """,
                (
                    item.work_item_id,
                    item.source_candidate_id,
                    item.title,
                    item.status.value,
                    owner,
                    item.due_date or "",
                    item.deliverable or "",
                    item.acceptance_criteria or "",
                    item.milestone_id,
                    _json(payload),
                    item.created_at or _now(),
                    item.updated_at or _now(),
                ),
            )
            conn.commit()

    def _append_event(self, entity: str, entity_id: str, action: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            self._insert_event(conn, entity, entity_id, action, payload)
            conn.commit()

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        entity: str,
        entity_id: str,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            "insert into events(id, entity, entity_id, action, payload, created_at) values(?, ?, ?, ?, ?, ?)",
            (f"event_{uuid4().hex[:16]}", entity, entity_id, action, _json(payload), _now()),
        )

    def _require_deliverable_submission_access(
        self,
        deliverable: dict[str, Any],
        *,
        actor: User,
        access_context: AccessContext,
        submitted_by: str,
    ) -> None:
        if submitted_by != actor.id:
            raise PermissionError("Deliverable submitter does not match the resolved actor")
        if not visible(actor, deliverable, access_context):
            raise PermissionError("Deliverable is outside the actor's visible scope")
        role = access_context.role_of(actor, str(deliverable["project_id"]))
        if ROLE_RANK.get(role, -1) < ROLE_RANK["exec"]:
            raise PermissionError("Actor role cannot submit deliverables")
        if deliverable.get("status") == "archived":
            raise ValueError("Archived deliverables cannot receive new versions")

    def _require_actor_visible_confirmed_memory_refs(
        self,
        memory_refs: list[Any],
        *,
        actor: User,
        access_context: AccessContext,
    ) -> None:
        normalized = [str(value).strip() for value in memory_refs if str(value).strip()]
        if not normalized:
            raise WorkRecordValidationError(
                "Solution options require actor-visible confirmed memory references"
            )
        for item_id in normalized:
            try:
                item = self.get_item(item_id)
            except KeyError as exc:
                raise WorkRecordValidationError(
                    "Solution options require actor-visible confirmed memory references"
                ) from exc
            if item.status != CandidateStatus.CONFIRMED or not visible(actor, item, access_context):
                raise WorkRecordValidationError(
                    "Solution options require actor-visible confirmed memory references"
                )

    def _message_count(self, conn: sqlite3.Connection, session_id: str) -> int:
        return int(conn.execute("select count(*) from messages where session_id = ?", (session_id,)).fetchone()[0])

    def _project_org_id(self, project_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute("select org_id from projects where id = ?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown project: {project_id}")
        return str(row["org_id"])

    def _topic_scope(self, topic_id: str, *, org_id: str | None = None, project_id: str | None = None) -> tuple[str, str]:
        if org_id and project_id:
            return org_id, project_id
        with self._connect() as conn:
            row = conn.execute("select org_id, project_id from topics where id = ?", (topic_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown topic: {topic_id}")
        return str(org_id or row["org_id"]), str(project_id or row["project_id"])

    def embedding_index_entry(self, item_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "select * from embeddings where item_id = ?",
                (item_id,),
            ).fetchone()
        if row is None:
            raise KeyError(item_id)
        return _embedding_index_row(row)

    def embedding_index_status(
        self,
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        clauses = ["i.status in ('candidate', 'confirmed')", "e.model = ?"]
        params: list[Any] = [_embedding_model()]
        if project_id:
            clauses.append("i.project_id = ?")
            params.append(project_id)
        with self._connect() as conn:
            eligible = conn.execute(
                (
                    "select count(*) from items i "
                    "where i.status in ('candidate', 'confirmed')"
                    + (" and i.project_id = ?" if project_id else "")
                ),
                (project_id,) if project_id else (),
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                select e.status, count(*) as value
                from embeddings e
                join items i on i.id = e.item_id
                where {" and ".join(clauses)}
                group by e.status
                """,
                tuple(params),
            ).fetchall()
        counts = {
            "pending": 0,
            "running": 0,
            "retrying": 0,
            "indexed": 0,
            "error": 0,
        }
        counts.update({str(row["status"]): int(row["value"]) for row in rows})
        missing = max(0, int(eligible) - sum(counts.values()))
        return {
            "enabled": _embedding_requested(),
            "model": _embedding_model(),
            "eligible": int(eligible),
            "missing": missing,
            **counts,
            "complete": (
                counts["indexed"] == int(eligible)
                and missing == 0
            ),
        }

    def queue_memory_embeddings(
        self,
        *,
        project_id: str | None = None,
        retry_errors: bool = False,
    ) -> dict[str, Any]:
        if not _embedding_requested():
            return self.embedding_index_status(project_id=project_id)
        sql = "select payload from items where status in ('candidate', 'confirmed')"
        params: tuple[Any, ...] = ()
        if project_id:
            sql += " and project_id = ?"
            params = (project_id,)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        for row in rows:
            self._queue_item_embedding(
                _inspection_item_from_dict(_loads(row["payload"], {}))
            )
        if retry_errors:
            clauses = ["model = ?", "status = 'error'"]
            values: list[Any] = [_embedding_model()]
            if project_id:
                clauses.append(
                    "item_id in (select id from items where project_id = ?)"
                )
                values.append(project_id)
            with _STORE_LOCK, self._connect() as conn:
                conn.execute(
                    f"""
                    update embeddings
                    set status = 'pending', attempts = 0, last_error = '',
                        next_retry_at = '', worker_id = '', updated_at = ?
                    where {" and ".join(clauses)}
                    """,
                    (_now(), *values),
                )
                conn.commit()
        return self.embedding_index_status(project_id=project_id)

    def recover_interrupted_embeddings(self) -> int:
        with _STORE_LOCK, self._connect() as conn:
            cursor = conn.execute(
                """
                update embeddings
                set status = 'pending',
                    last_error = 'Recovered after process restart',
                    next_retry_at = '', worker_id = '', updated_at = ?
                where status = 'running'
                """,
                (_now(),),
            )
            conn.commit()
        return int(cursor.rowcount)

    def process_embedding_batch(
        self,
        *,
        worker_id: str,
        batch_size: int = 20,
        embedder: Callable[[list[str]], list[list[float]]] | None = None,
        max_attempts: int = 3,
        retry_delay_seconds: int = 30,
    ) -> dict[str, Any] | None:
        if not _embedding_requested():
            return None
        active_batch_size = max(
            1,
            min(int(batch_size), _embedding_max_batch_size()),
        )
        active_max_attempts = max(1, int(max_attempts))
        active_model = _embedding_model()
        now = _now()
        with _STORE_LOCK, self._connect() as conn:
            conn.execute("begin immediate")
            rows = conn.execute(
                """
                select e.item_id, e.content_hash, e.attempts
                from embeddings e
                join items i on i.id = e.item_id
                where e.model = ?
                  and i.status in ('candidate', 'confirmed')
                  and (
                    e.status = 'pending'
                    or (
                      e.status = 'retrying'
                      and (e.next_retry_at = '' or e.next_retry_at <= ?)
                    )
                  )
                order by e.updated_at, e.item_id
                limit ?
                """,
                (active_model, now, active_batch_size),
            ).fetchall()
            item_ids = [str(row["item_id"]) for row in rows]
            claims = {
                str(row["item_id"]): {
                    "content_hash": str(row["content_hash"]),
                    "attempts": int(row["attempts"] or 0) + 1,
                }
                for row in rows
            }
            if not item_ids:
                conn.commit()
                return None
            placeholders = ",".join("?" for _item_id in item_ids)
            conn.execute(
                f"""
                update embeddings
                set status = 'running', attempts = attempts + 1,
                    worker_id = ?, next_retry_at = '', updated_at = ?
                where item_id in ({placeholders})
                """,
                (worker_id, now, *item_ids),
            )
            conn.commit()

        items = [self.get_item(item_id) for item_id in item_ids]
        active_embedder = embedder or _embed_texts
        try:
            vectors = active_embedder([_embedding_text(item) for item in items])
            if len(vectors) != len(items):
                raise RuntimeError(
                    "Embedding API returned a different number of vectors than inputs"
                )
            normalized_vectors: list[list[float]] = []
            for vector in vectors:
                normalized = [float(value) for value in vector]
                if not normalized:
                    raise RuntimeError("Embedding API returned an empty vector")
                normalized_vectors.append(normalized)
            dimensions = {len(vector) for vector in normalized_vectors}
            if len(dimensions) != 1:
                raise RuntimeError(
                    "Embedding API returned inconsistent vector dimensions"
                )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            retry_at = (
                _now()
                if int(retry_delay_seconds) <= 0
                else _iso_after_seconds(int(retry_delay_seconds))
            )
            with _STORE_LOCK, self._connect() as conn:
                failed_count = 0
                superseded_count = 0
                for item_id in item_ids:
                    claim = claims[item_id]
                    attempts = int(claim["attempts"])
                    status = (
                        "error"
                        if attempts >= active_max_attempts
                        else "retrying"
                    )
                    cursor = conn.execute(
                        """
                        update embeddings
                        set status = ?, dimensions = 0, vector = '[]',
                            last_error = ?, next_retry_at = ?, worker_id = '',
                            updated_at = ?
                        where item_id = ? and model = ? and status = 'running'
                          and worker_id = ? and content_hash = ?
                        """,
                        (
                            status,
                            error,
                            "" if status == "error" else retry_at,
                            _now(),
                            item_id,
                            active_model,
                            worker_id,
                            claim["content_hash"],
                        ),
                    )
                    if cursor.rowcount:
                        failed_count += 1
                    else:
                        superseded_count += 1
                conn.commit()
            return {
                "claimed": len(item_ids),
                "indexed": 0,
                "failed": failed_count,
                "superseded": superseded_count,
                "item_ids": item_ids,
                "error": error,
            }

        with _STORE_LOCK, self._connect() as conn:
            indexed_count = 0
            superseded_count = 0
            for item, vector in zip(items, normalized_vectors):
                claim = claims[item.item_id]
                cursor = conn.execute(
                    """
                    update embeddings
                    set status = 'indexed', dimensions = ?, vector = ?,
                        last_error = '', next_retry_at = '',
                        worker_id = '', updated_at = ?
                    where item_id = ? and model = ? and status = 'running'
                      and worker_id = ? and content_hash = ?
                    """,
                    (
                        len(vector),
                        _json(vector),
                        _now(),
                        item.item_id,
                        active_model,
                        worker_id,
                        claim["content_hash"],
                    ),
                )
                if cursor.rowcount:
                    indexed_count += 1
                else:
                    superseded_count += 1
            conn.commit()
        return {
            "claimed": len(item_ids),
            "indexed": indexed_count,
            "failed": 0,
            "superseded": superseded_count,
            "item_ids": item_ids,
            "error": "",
        }

    def _queue_item_embedding(self, item: InspectionItem) -> None:
        if item.status not in {
            CandidateStatus.CANDIDATE,
            CandidateStatus.CONFIRMED,
        }:
            with _STORE_LOCK, self._connect() as conn:
                conn.execute(
                    "delete from embeddings where item_id = ?",
                    (item.item_id,),
                )
                conn.commit()
            return
        model = _embedding_model()
        content_hash = _embedding_content_hash(item)
        with _STORE_LOCK, self._connect() as conn:
            current = conn.execute(
                "select * from embeddings where item_id = ?",
                (item.item_id,),
            ).fetchone()
            if (
                current is not None
                and str(current["model"]) == model
                and str(current["content_hash"]) == content_hash
            ):
                return
            conn.execute(
                """
                insert into embeddings(
                  item_id, model, dimensions, vector, updated_at, content_hash,
                  status, last_error, attempts, next_retry_at, worker_id
                ) values(?, ?, 0, '[]', ?, ?, 'pending', '', 0, '', '')
                on conflict(item_id) do update set
                  model=excluded.model,
                  dimensions=0,
                  vector='[]',
                  updated_at=excluded.updated_at,
                  content_hash=excluded.content_hash,
                  status='pending',
                  last_error='',
                  attempts=0,
                  next_retry_at='',
                  worker_id=''
                """,
                (
                    item.item_id,
                    model,
                    _now(),
                    content_hash,
                ),
            )
            conn.commit()

    def _semantic_item_matches(
        self,
        query: str,
        eligible_ids: list[str],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not eligible_ids:
            return []
        query_vector = _embed_texts([query])[0]
        vectors = self._embedding_vectors(eligible_ids)
        matches: list[dict[str, Any]] = []
        for item_id in eligible_ids:
            vector = vectors.get(item_id)
            if not vector:
                continue
            semantic_score = max(0.0, _cosine_similarity(query_vector, vector))
            if semantic_score <= 0:
                continue
            matches.append(
                {
                    "item_id": item_id,
                    "score": semantic_score,
                    "lexical_score": 0.0,
                    "semantic_score": semantic_score,
                    "relevance_score": semantic_score,
                    "reasons": ["semantic"],
                    "highlight": "",
                }
            )
        matches.sort(
            key=lambda row: (
                -float(row["semantic_score"]),
                str(row["item_id"]),
            )
        )
        return matches[: max(1, int(limit))]

    def _embedding_index_status_for_ids(
        self,
        item_ids: list[str],
        *,
        enabled: bool,
    ) -> dict[str, Any]:
        counts = {
            "pending": 0,
            "running": 0,
            "retrying": 0,
            "indexed": 0,
            "error": 0,
        }
        if item_ids:
            placeholders = ",".join("?" for _item_id in item_ids)
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    select status, count(*) as value
                    from embeddings
                    where item_id in ({placeholders}) and model = ?
                    group by status
                    """,
                    (*item_ids, _embedding_model()),
                ).fetchall()
            counts.update(
                {
                    str(row["status"]): int(row["value"])
                    for row in rows
                    if str(row["status"]) in counts
                }
            )
        missing = max(0, len(item_ids) - sum(counts.values()))
        return {
            "enabled": enabled,
            "model": _embedding_model() if enabled else "",
            "eligible": len(item_ids),
            "missing": missing,
            **counts,
            "complete": (
                not enabled
                or (
                    counts["indexed"] == len(item_ids)
                    and missing == 0
                )
            ),
        }

    def _rerank_matches_with_embeddings(self, query: str, matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        semantic = self._semantic_item_matches(
            query,
            [str(match["item_id"]) for match in matches],
            limit=max(1, len(matches)),
        )
        return _merge_hybrid_memory_matches(matches, semantic)

    def _embedding_vectors(self, item_ids: list[str]) -> dict[str, list[float]]:
        if not item_ids:
            return {}
        placeholders = ",".join("?" for _ in item_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select item_id, vector
                from embeddings
                where item_id in ({placeholders}) and model = ?
                  and status = 'indexed' and dimensions > 0
                """,
                (*item_ids, _embedding_model()),
            ).fetchall()
        return {
            row["item_id"]: [float(value) for value in _loads(row["vector"], [])]
            for row in rows
        }


def _json(value: Any) -> str:
    return json.dumps(normalize_repository_paths(value), ensure_ascii=False, sort_keys=True)


def _loads(value: str | bytes | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _embedding_index_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "item_id": str(row["item_id"]),
        "model": str(row["model"]),
        "dimensions": int(row["dimensions"] or 0),
        "content_hash": str(row["content_hash"]),
        "status": str(row["status"]),
        "last_error": str(row["last_error"]),
        "attempts": int(row["attempts"] or 0),
        "next_retry_at": str(row["next_retry_at"]),
        "worker_id": str(row["worker_id"]),
        "updated_at": str(row["updated_at"]),
    }


def _topic_members_from_rows(rows: list[sqlite3.Row]) -> dict[str, set[str]]:
    members: dict[str, set[str]] = {}
    for row in rows:
        members.setdefault(row["topic_id"], set()).add(row["user_id"])
    return members


def _unique_people_groups(assignments: list[sqlite3.Row]) -> list[str]:
    groups: list[str] = []
    for assignment in assignments:
        group = str(assignment["group_name"] or "").strip()
        if group and group not in groups:
            groups.append(group)
    return groups


def _infer_people_access_role(assignments: list[sqlite3.Row]) -> str:
    texts = [
        " ".join([
            str(assignment["group_name"] or ""),
            str(assignment["role"] or ""),
            str(assignment["path"] or ""),
        ])
        for assignment in assignments
    ]
    combined = "\n".join(texts)
    role_text = "\n".join(str(assignment["role"] or "") for assignment in assignments)
    if "专业统筹" in role_text:
        return "professional_lead"
    if "专题负责人" in role_text:
        return "topic_lead"
    if "实施人员" in role_text:
        return "exec"
    if any(keyword in role_text for keyword in ("项目经理", "主管领导")) or any(
        token in role_text.lower().split() for token in ("pm", "pmo")
    ):
        return "pmo"
    if any(keyword in combined for keyword in ("总设计师", "总体负责人", "专业负责人", "架构师")):
        return "professional_lead"
    if any(keyword in role_text for keyword in ("负责人", "主导人员", "组长")):
        return "topic_lead"
    return "exec"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_after_seconds(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1, int(seconds)))).isoformat()


def _session_title(value: str) -> str:
    cleaned = " ".join((value or "未命名会话").strip().split())
    return cleaned[:28] or "未命名会话"


def _session_row(row: sqlite3.Row, message_count: int) -> dict[str, Any]:
    return {
        "session_id": row["id"],
        "project_id": row["project_id"],
        "actor_id": row["actor_id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "message_count": message_count,
        "archived": bool(row["archived"]),
    }


def _daily_journal_version_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "version_id": row["id"],
        "journal_id": row["journal_id"],
        "version": int(row["version"]),
        "org_id": row["org_id"],
        "project_id": row["project_id"],
        "topic_id": row["topic_id"] or None,
        "author_id": row["author_id"],
        "sensitivity": row["sensitivity"],
        "report_date": row["report_date"],
        "snapshot_hash": row["snapshot_hash"],
        "message_count": int(row["message_count"]),
        "status": row["status"],
        "organization_status": row["organization_status"],
        "source_id": row["source_id"],
        "ingestion_job_id": row["ingestion_job_id"],
        "content_markdown": row["content_markdown"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "payload": _loads(row["payload"], {}),
    }


def _daily_work_record_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "record_id": row["id"],
        "id": row["id"],
        "series_id": row["series_id"],
        "source_id": row["source_id"],
        "source_origin": row["source_origin"],
        "org_id": row["org_id"],
        "project_id": row["project_id"],
        "topic_id": row["topic_id"] or None,
        "author_id": row["author_id"],
        "sensitivity": row["sensitivity"],
        "subject_user_id": row["subject_user_id"],
        "subject_name": row["subject_name"],
        "record_date": row["record_date"],
        "kind": row["kind"],
        "text": row["text"],
        "solution_options": _loads(row["solution_options"], []),
        "evidence_refs": _loads(row["evidence_refs"], []),
        "source_locator": row["source_locator"],
        "status": row["status"],
        "edited_by_human": bool(row["edited_by_human"]),
        "updated_by": row["updated_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "payload": _loads(row["payload"], {}),
    }


def _deliverable_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "deliverable_id": row["id"],
        "id": row["id"],
        "org_id": row["org_id"],
        "project_id": row["project_id"],
        "topic_id": row["topic_id"] or None,
        "author_id": row["author_id"],
        "sensitivity": row["sensitivity"],
        "milestone_id": row["milestone_id"],
        "title": row["title"],
        "type_label": row["type_label"],
        "required": bool(row["required"]),
        "due_date": row["due_date"],
        "acceptance_criteria": row["acceptance_criteria"],
        "status": row["status"],
        "sort_order": int(row["sort_order"]),
        "current_version": int(row["current_version"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "payload": _loads(row["payload"], {}),
    }


def _deliverable_version_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "version_id": row["id"],
        "id": row["id"],
        "deliverable_id": row["deliverable_id"],
        "version": int(row["version"]),
        "org_id": row["org_id"],
        "project_id": row["project_id"],
        "topic_id": row["topic_id"] or None,
        "author_id": row["author_id"],
        "sensitivity": row["sensitivity"],
        "submitted_by": row["submitted_by"],
        "status": row["status"],
        "note": row["note"],
        "created_at": row["created_at"],
        "payload": _loads(row["payload"], {}),
    }


def _deliverable_file_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "file_id": row["id"],
        "id": row["id"],
        "deliverable_id": row["deliverable_id"],
        "version_id": row["version_id"],
        "org_id": row["org_id"],
        "project_id": row["project_id"],
        "topic_id": row["topic_id"] or None,
        "author_id": row["author_id"],
        "sensitivity": row["sensitivity"],
        "artifact_role": row["artifact_role"],
        "original_name": row["original_name"],
        "relative_path": row["relative_path"],
        "content_hash": row["content_hash"],
        "content_type": row["content_type"],
        "position": int(row["position"]),
        "size": int(row["size"]),
        "created_at": row["created_at"],
        "payload": _loads(row["payload"], {}),
    }


def _project_skill_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "skill_id": row["id"],
        "org_id": row["org_id"],
        "project_id": row["project_id"],
        "topic_id": row["topic_id"] or None,
        "author_id": row["author_id"],
        "sensitivity": row["sensitivity"],
        "slug": row["slug"],
        "name": row["name"],
        "status": row["status"],
        "source_method_id": row["source_method_id"],
        "active_version_id": row["active_version_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "payload": _loads(row["payload"], {}),
    }


def _project_skill_version_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "version_id": row["id"],
        "skill_id": row["skill_id"],
        "version": int(row["version"]),
        "org_id": row["org_id"],
        "project_id": row["project_id"],
        "topic_id": row["topic_id"] or None,
        "author_id": row["author_id"],
        "sensitivity": row["sensitivity"],
        "status": row["status"],
        "description": row["description"],
        "when_to_use": row["when_to_use"],
        "tool_names": _loads(row["tool_names"], []),
        "body_markdown": row["body_markdown"],
        "content_hash": row["content_hash"],
        "readiness": _loads(row["readiness"], {}),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "published_at": row["published_at"],
        "payload": _loads(row["payload"], {}),
    }


def _project_skill_evidence_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "skill_id": row["skill_id"],
        "version_id": row["version_id"],
        "org_id": row["org_id"],
        "project_id": row["project_id"],
        "topic_id": row["topic_id"] or None,
        "author_id": row["author_id"],
        "sensitivity": row["sensitivity"],
        "source_method_id": row["source_method_id"],
        "source_doc_id": row["source_doc_id"],
        "locator": row["locator"],
        "quote": row["quote"],
        "created_at": row["created_at"],
        "payload": _loads(row["payload"], {}),
    }


def _project_skill_test_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "skill_id": row["skill_id"],
        "version_id": row["version_id"],
        "org_id": row["org_id"],
        "project_id": row["project_id"],
        "topic_id": row["topic_id"] or None,
        "author_id": row["author_id"],
        "sensitivity": row["sensitivity"],
        "name": row["name"],
        "input": row["input_text"],
        "should_trigger": bool(row["should_trigger"]),
        "selected": None if row["selected"] is None else bool(row["selected"]),
        "passed": None if row["passed"] is None else bool(row["passed"]),
        "status": row["status"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "payload": _loads(row["payload"], {}),
    }


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["id"],
        "entity": row["entity"],
        "entity_id": row["entity_id"],
        "action": row["action"],
        "payload": _loads(row["payload"], {}),
        "created_at": row["created_at"],
    }


def _ingestion_job_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source_id": row["source_id"],
        "org_id": row["org_id"],
        "project_id": row["project_id"],
        "topic_id": row["topic_id"] or None,
        "author_id": row["author_id"],
        "sensitivity": row["sensitivity"],
        "actor_id": row["actor_id"],
        "input_kind": row["input_kind"],
        "content_hash": row["content_hash"],
        "processor_version": row["processor_version"],
        "input_path": row["input_path"],
        "status": row["status"],
        "stage": row["stage"],
        "total_chunks": int(row["total_chunks"]),
        "processed_chunks": int(row["processed_chunks"]),
        "candidate_count": int(row["candidate_count"]),
        "delta_new": int(row["delta_new"]),
        "delta_updated": int(row["delta_updated"]),
        "delta_conflict": int(row["delta_conflict"]),
        "delta_resolved": int(row["delta_resolved"]),
        "delta_auto_merged": int(row["delta_auto_merged"]),
        "attempts": int(row["attempts"]),
        "max_attempts": int(row["max_attempts"]),
        "retry_at": row["retry_at"],
        "lease_owner": row["lease_owner"],
        "lease_expires_at": row["lease_expires_at"],
        "minutes_path": row["minutes_path"],
        "minutes_hash": row["minutes_hash"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "payload": _loads(row["payload"], {}),
    }


def _ingestion_chunk_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "job_id": row["job_id"],
        "source_id": row["source_id"],
        "org_id": row["org_id"],
        "project_id": row["project_id"],
        "topic_id": row["topic_id"] or None,
        "author_id": row["author_id"],
        "sensitivity": row["sensitivity"],
        "chunk_index": int(row["chunk_index"]),
        "start_char": int(row["start_char"]),
        "end_char": int(row["end_char"]),
        "content_hash": row["content_hash"],
        "status": row["status"],
        "attempts": int(row["attempts"]),
        "summary": row["summary"],
        "error": row["error"],
        "payload": _loads(row["payload"], {}),
        "updated_at": row["updated_at"],
    }


def _memory_thread_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "org_id": row["org_id"],
        "project_id": row["project_id"],
        "topic_id": row["topic_id"] or None,
        "author_id": row["author_id"],
        "sensitivity": row["sensitivity"],
        "category": row["category"],
        "thread_key": row["thread_key"],
        "title": row["title"],
        "status": row["status"],
        "current_item_id": row["current_item_id"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "payload": _loads(row["payload"], {}),
    }


def _memory_thread_item_row(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "thread_id": row["thread_id"],
        "item_id": row["item_id"],
        "ingestion_job_id": row["ingestion_job_id"],
        "source_id": row["source_id"],
        "org_id": row["org_id"],
        "project_id": row["project_id"],
        "topic_id": row["topic_id"] or None,
        "author_id": row["author_id"],
        "sensitivity": row["sensitivity"],
        "relation": row["relation"],
        "supersedes_item_id": row["supersedes_item_id"],
        "effective_at": row["effective_at"],
        "review_required": bool(row["review_required"]),
        "review_status": row["item_status"] if "item_status" in keys and row["item_status"] else row["review_status"],
        "changed_fields": _loads(row["changed_fields"], []),
        "evidence_refs": _loads(row["evidence_refs"], []),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _memory_thread_source_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "thread_id": row["thread_id"],
        "ingestion_job_id": row["ingestion_job_id"],
        "source_id": row["source_id"],
        "org_id": row["org_id"],
        "project_id": row["project_id"],
        "topic_id": row["topic_id"] or None,
        "author_id": row["author_id"],
        "sensitivity": row["sensitivity"],
        "relation": row["relation"],
        "effective_at": row["effective_at"],
        "evidence_refs": _loads(row["evidence_refs"], []),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _item_status_action(status: CandidateStatus) -> str:
    return {
        CandidateStatus.CONFIRMED: "item_confirmed",
        CandidateStatus.REJECTED: "item_rejected",
        CandidateStatus.ARCHIVED: "item_archived",
    }.get(status, "item_status_updated")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {declaration}")


def _backfill_access_tags(conn: sqlite3.Connection) -> None:
    defaults = _migration_tag_defaults(conn)
    for table in ("sources", "items"):
        rows = conn.execute(
            f"""
            select id, payload, org_id, project_id, topic_id, author_id, sensitivity, tag_origin
            from {table}
            where org_id is null or org_id = ''
               or project_id is null or project_id = ''
               or author_id is null or author_id = ''
               or sensitivity is null or sensitivity = ''
               or tag_origin is null or tag_origin = ''
            """
        ).fetchall()
        for row in rows:
            tags = {
                "org_id": row["org_id"] or defaults["org_id"],
                "project_id": row["project_id"] or defaults["project_id"],
                "topic_id": row["topic_id"],
                "author_id": row["author_id"] or defaults["author_id"],
                "sensitivity": row["sensitivity"] or "l1",
                "tag_origin": "migration_backfill",
            }
            payload = _loads(row["payload"], {})
            payload.update(tags)
            conn.execute(
                f"""
                update {table}
                set org_id = ?,
                    project_id = ?,
                    topic_id = ?,
                    author_id = ?,
                    sensitivity = ?,
                    tag_origin = ?,
                    payload = ?
                where id = ?
                """,
                (
                    tags["org_id"],
                    tags["project_id"],
                    tags["topic_id"],
                    tags["author_id"],
                    tags["sensitivity"],
                    tags["tag_origin"],
                    _json(payload),
                    row["id"],
                ),
            )


def _migration_tag_defaults(conn: sqlite3.Connection) -> dict[str, str]:
    row = conn.execute(
        "select id, org_id, owner_id from projects order by created_at, id limit 1"
    ).fetchone()
    if row:
        return {
            "org_id": row["org_id"] or "org_mvp",
            "project_id": row["id"] or "project_mvp",
            "author_id": row["owner_id"] or "u_pm",
        }
    return {"org_id": "org_mvp", "project_id": "project_mvp", "author_id": "u_pm"}


def _source_payload(source: SourceDocument | dict[str, Any]) -> dict[str, Any]:
    if isinstance(source, dict):
        payload = dict(source)
    else:
        payload = to_plain(source)
    payload.setdefault("doc_id", payload.get("id", ""))
    payload.setdefault("title", payload.get("doc_id", "未命名来源"))
    payload.setdefault("meeting_date", "")
    payload.setdefault("org_id", "org_mvp")
    payload.setdefault("project_id", "project_mvp")
    payload.setdefault("topic_id", None)
    payload.setdefault("author_id", "system")
    payload.setdefault("sensitivity", "l1")
    payload.setdefault("tag_origin", "runtime_default")
    payload.setdefault("input_kind", "auto")
    payload.setdefault("content_hash", "")
    return payload


def _source_path(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("path") or "")
    return str(value or "")


def _source_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = _loads(row["payload"], {})
    tagged_payload = _merge_row_access_tags(payload, row)
    return {
        "id": row["id"],
        "doc_id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "meeting_date": row["meeting_date"] or "",
        "path": row["path"] or "",
        "org_id": tagged_payload["org_id"],
        "project_id": tagged_payload["project_id"],
        "topic_id": tagged_payload.get("topic_id"),
        "author_id": tagged_payload["author_id"],
        "sensitivity": tagged_payload["sensitivity"],
        "tag_origin": tagged_payload["tag_origin"],
        "input_kind": (row["input_kind"] if "input_kind" in row.keys() else "auto") or "auto",
        "current_ingestion_job_id": (
            row["current_ingestion_job_id"] if "current_ingestion_job_id" in row.keys() else ""
        ) or "",
        "content_hash": str(tagged_payload.get("content_hash") or ""),
        "created_at": row["created_at"] or "",
        "updated_at": (row["updated_at"] if "updated_at" in row.keys() else "") or "",
        "payload": tagged_payload,
    }


def _item_payload_from_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = _loads(row["payload"], {})
    payload.setdefault("item_id", row["id"])
    payload.setdefault("category", row["type"])
    payload.setdefault("status", row["status"])
    payload.setdefault("title", row["title"])
    payload.setdefault("description", row["body"] or "")
    payload.setdefault("evidence_refs", _loads(row["evidence_refs"], []))
    payload.setdefault("due_date", row["due_date"] or None)
    payload.setdefault("deliverable", row["deliverable"] or None)
    payload.setdefault("acceptance_criteria", row["acceptance"] or None)
    return _merge_row_access_tags(payload, row)


def _merge_row_access_tags(payload: dict[str, Any], row: sqlite3.Row) -> dict[str, Any]:
    merged = dict(payload)
    for field in ACCESS_TAG_FIELDS:
        value = row[field] if field in row.keys() else None
        if value is not None and value != "":
            merged[field] = value
        else:
            merged.setdefault(field, None if field == "topic_id" else _default_tag_value(field))
    tag_origin = row["tag_origin"] if "tag_origin" in row.keys() else None
    if tag_origin:
        merged["tag_origin"] = tag_origin
    else:
        merged.setdefault("tag_origin", "runtime_default")
    return merged


def _default_tag_value(field: str) -> str:
    return {
        "org_id": "org_mvp",
        "project_id": "project_mvp",
        "author_id": "system",
        "sensitivity": "l1",
    }.get(field, "")


def _validate_ingest_tags(tags: dict[str, Any], actor: Any) -> dict[str, Any]:
    normalized = {
        "org_id": str(tags.get("org_id") or "").strip(),
        "project_id": str(tags.get("project_id") or "").strip(),
        "topic_id": str(tags.get("topic_id") or "").strip() or None,
        "author_id": str(tags.get("author_id") or "").strip(),
        "sensitivity": str(tags.get("sensitivity") or "").strip(),
    }
    missing = [field for field in REQUIRED_INGEST_TAG_FIELDS if not normalized.get(field)]
    if missing:
        raise ValueError(f"Missing required ingest tags: {', '.join(missing)}")
    if normalized["sensitivity"] not in SENSITIVITY_LEVELS:
        raise ValueError(f"Invalid sensitivity: {normalized['sensitivity']}")
    actor_id = _actor_id(actor)
    if actor_id and normalized["author_id"] != actor_id:
        raise ValueError("Ingest author_id must match actor id")
    return normalized


def _apply_access_tags(payload: dict[str, Any], tags: dict[str, Any], *, tag_origin: str) -> dict[str, Any]:
    result = dict(payload)
    for field in ACCESS_TAG_FIELDS:
        result[field] = tags.get(field)
    result["tag_origin"] = tag_origin
    return result


def _actor_id(actor: Any) -> str:
    if isinstance(actor, dict):
        return str(actor.get("id") or "")
    return str(getattr(actor, "id", "") or "")


def _actor_org_id(actor: Any) -> str:
    if isinstance(actor, dict):
        return str(actor.get("org_id") or "")
    return str(getattr(actor, "org_id", "") or "")


def _report_items(report: InspectionReport) -> list[InspectionItem]:
    return report.chain_gaps + report.responsibility_gaps + report.followup_drafts


def _candidate_to_inspection(item: CandidateItem) -> InspectionItem:
    return InspectionItem(
        item_id=item.item_id,
        category=item.category,
        title=item.title,
        description=item.description,
        evidence_refs=item.evidence_refs,
        status=item.status,
        owner_candidates=item.owner_candidates,
        matter_type=item.matter_type,
        facet_types=item.facet_types,
        due_date=item.due_date,
        deliverable=item.deliverable,
        acceptance_criteria=item.acceptance_criteria,
        inference_note=item.inference_note,
        observed_fields=item.observed_fields,
        proposed_fields=item.proposed_fields,
        inference_basis=item.inference_basis,
        inference_confidence=item.inference_confidence,
        business_goal=item.business_goal,
        principles=item.principles,
        reasoning_chain=item.reasoning_chain,
        applicable_scope=item.applicable_scope,
        linked_issue_ids=item.linked_issue_ids,
        linked_task_ids=item.linked_task_ids,
        org_id=item.org_id,
        project_id=item.project_id,
        topic_id=item.topic_id,
        author_id=item.author_id,
        sensitivity=item.sensitivity,
        proposed_sensitivity=item.proposed_sensitivity,
        sensitivity_reason=item.sensitivity_reason,
        tag_origin=item.tag_origin,
        thread_id=item.thread_id,
        thread_title=item.thread_title,
        thread_key=item.thread_key,
        thread_event=item.thread_event,
        effective_at=item.effective_at,
        supersedes_item_id=item.supersedes_item_id,
        review_required=item.review_required,
        changed_fields=item.changed_fields,
        claim_hash=item.claim_hash,
    )


def _evidence_from_dict(data: dict[str, Any]) -> EvidenceRef:
    return EvidenceRef(
        source_doc_id=data["source_doc_id"],
        source_kind=data["source_kind"],
        locator=data["locator"],
        quote=data["quote"],
        raw_source_doc_id=data.get("raw_source_doc_id", ""),
        raw_locator=data.get("raw_locator", ""),
        evidence_level=data.get("evidence_level", "curated_pending_raw"),
        ingestion_job_id=data.get("ingestion_job_id", ""),
    )


def _inspection_item_from_dict(data: dict[str, Any]) -> InspectionItem:
    return InspectionItem(
        item_id=data["item_id"],
        category=data["category"],
        title=data["title"],
        description=data["description"],
        evidence_refs=[_evidence_from_dict(item) for item in data.get("evidence_refs", [])],
        status=CandidateStatus(data.get("status", "candidate")),
        owner_candidates=data.get("owner_candidates"),
        next_step=data.get("next_step"),
        confirmation_notes=data.get("confirmation_notes", ""),
        confirmation_editor=data.get("confirmation_editor", ""),
        updated_at=data.get("updated_at", ""),
        matter_type=data.get("matter_type"),
        facet_types=data.get("facet_types"),
        due_date=data.get("due_date"),
        deliverable=data.get("deliverable"),
        acceptance_criteria=data.get("acceptance_criteria"),
        professional_id=data.get("professional_id", ""),
        board_id=data.get("board_id", ""),
        inference_note=data.get("inference_note"),
        observed_fields=list(data.get("observed_fields") or []),
        proposed_fields=list(data.get("proposed_fields") or []),
        inference_basis=list(data.get("inference_basis") or []),
        inference_confidence=data.get("inference_confidence", ""),
        business_goal=data.get("business_goal"),
        principles=data.get("principles"),
        reasoning_chain=data.get("reasoning_chain"),
        applicable_scope=data.get("applicable_scope"),
        linked_issue_ids=data.get("linked_issue_ids"),
        linked_task_ids=data.get("linked_task_ids"),
        org_id=data.get("org_id", "org_mvp"),
        project_id=data.get("project_id", "project_mvp"),
        topic_id=data.get("topic_id"),
        author_id=data.get("author_id", "system"),
        sensitivity=data.get("sensitivity", "l1"),
        proposed_sensitivity=data.get("proposed_sensitivity"),
        sensitivity_reason=data.get("sensitivity_reason"),
        tag_origin=data.get("tag_origin", "runtime_default"),
        thread_id=data.get("thread_id"),
        thread_title=data.get("thread_title"),
        thread_key=data.get("thread_key"),
        thread_event=data.get("thread_event", "new"),
        effective_at=data.get("effective_at"),
        supersedes_item_id=data.get("supersedes_item_id"),
        review_required=bool(data.get("review_required", True)),
        changed_fields=list(data.get("changed_fields") or []),
        claim_hash=data.get("claim_hash", ""),
    )


def _inspection_report_from_dict(data: dict[str, Any] | None) -> InspectionReport | None:
    if data is None:
        return None
    return InspectionReport(
        scenario_id=data["scenario_id"],
        chain_name=data["chain_name"],
        chain_gaps=[_inspection_item_from_dict(item) for item in data.get("chain_gaps", [])],
        responsibility_gaps=[
            _inspection_item_from_dict(item)
            for item in data.get("responsibility_gaps", [])
        ],
        followup_drafts=[
            _inspection_item_from_dict(item)
            for item in data.get("followup_drafts", [])
        ],
        status=CandidateStatus(data.get("status", "candidate")),
    )


def _verification_from_dict(data: dict[str, Any] | None) -> EvidenceVerificationResult | None:
    if data is None:
        return None
    return EvidenceVerificationResult(
        ok=bool(data.get("ok", False)),
        checked_count=int(data.get("checked_count", 0)),
        errors=list(data.get("errors", [])),
        warnings=list(data.get("warnings", [])),
        raw_evidence_count=int(data.get("raw_evidence_count", 0)),
        raw_pending_count=int(data.get("raw_pending_count", 0)),
    )


def _agent_run_from_dict(data: dict[str, Any]) -> AgentRun:
    milestone = _run_milestone_from_dict(data)
    return AgentRun(
        run_id=data["run_id"],
        milestone_id=data.get("milestone_id", milestone.milestone_id),
        milestone_plan=milestone,
        objective=data.get("objective") or data.get("goal") or milestone.name,
        plan=[
            AgentPlanStep(
                step_id=item["step_id"],
                tool_name=item["tool_name"],
                reason=item["reason"],
            )
            for item in data.get("plan", [])
        ],
        steps=[
            AgentStep(
                step_id=item["step_id"],
                tool_name=item["tool_name"],
                status=item["status"],
                input_summary=item["input_summary"],
                output_summary=item["output_summary"],
            )
            for item in data.get("steps", [])
        ],
        observations=[
            AgentObservation(
                observation_id=item["observation_id"],
                tool_name=item["tool_name"],
                summary=item["summary"],
                data=item.get("data", {}),
            )
            for item in data.get("observations", [])
        ],
        final_report=_inspection_report_from_dict(data.get("final_report")),
        confirmed_item_ids=data.get("confirmed_item_ids", []),
        created_at=data["created_at"],
        status=data.get("status", "completed"),
        runtime_kind=data.get("runtime_kind", "legacy_local_harness"),
        verification=_verification_from_dict(data.get("verification")),
        error=data.get("error", ""),
        agent_trace=data.get("agent_trace", []),
        raw_response_count=int(data.get("raw_response_count", 0)),
        input_snapshot=data.get("input_snapshot", {}),
        loop_rounds=[
            AgentLoopRound(
                round_index=int(item.get("round_index", index)),
                phase=item.get("phase", "legacy"),
                objective=item.get("objective", ""),
                model_input_summary=item.get("model_input_summary", ""),
                model_output_summary=item.get("model_output_summary", ""),
                tool_name=item.get("tool_name", ""),
                tool_result_summary=item.get("tool_result_summary", ""),
                decision=item.get("decision", ""),
                state_changes=list(item.get("state_changes", [])),
                errors=list(item.get("errors", [])),
                stop_reason=item.get("stop_reason", ""),
                round_kind=item.get("round_kind", "tool"),
                raw_response_id=item.get("raw_response_id", ""),
                token_usage=dict(item.get("token_usage", {})),
                tool_calls=list(item.get("tool_calls", [])),
            )
            for index, item in enumerate(data.get("loop_rounds", []), start=1)
        ],
        stop_reason=data.get("stop_reason", ""),
        structured_output=data.get("structured_output", {}),
        harness_state=data.get("harness_state", {}),
        model_io_events=list(data.get("model_io_events", [])),
        no_change_reason=data.get("no_change_reason", ""),
    )


def _run_milestone_from_dict(data: dict[str, Any]) -> MilestonePlan:
    if "milestone_plan" in data:
        return milestone_from_dict(data["milestone_plan"])
    return legacy_milestone_from_objective(data.get("goal", ""))


def _run_project_id(run: AgentRun) -> str:
    configured = str((run.input_snapshot or {}).get("project_id") or "").strip()
    if configured:
        return configured
    if run.final_report:
        for item in _report_items(run.final_report):
            if item.project_id:
                return item.project_id
    return "project_mvp"


def _work_item_id_from_candidate(item_id: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in item_id)
    return f"work_{cleaned}"


def _work_item_from_dict(data: dict[str, Any]) -> WorkItem:
    return WorkItem(
        work_item_id=data["work_item_id"],
        title=data["title"],
        description=data.get("description", ""),
        status=WorkItemStatus(data.get("status", "open")),
        evidence_refs=[_evidence_from_dict(item) for item in data.get("evidence_refs", [])],
        owner_candidates=data.get("owner_candidates"),
        collaborators=data.get("collaborators"),
        confirmers=data.get("confirmers"),
        due_date=data.get("due_date"),
        deliverable=data.get("deliverable"),
        acceptance_criteria=data.get("acceptance_criteria"),
        professional_id=data.get("professional_id", ""),
        board_id=data.get("board_id", ""),
        planned_start=data.get("planned_start", ""),
        progress_percent=(int(data["progress_percent"]) if data.get("progress_percent") is not None else None),
        status_updated_at=data.get("status_updated_at", ""),
        milestone_id=data.get("milestone_id", ""),
        source_candidate_id=data.get("source_candidate_id", ""),
        source_run_id=data.get("source_run_id", ""),
        version=int(data.get("version", 1)),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        confirmation_notes=data.get("confirmation_notes", ""),
        confirmation_editor=data.get("confirmation_editor", ""),
        linked_issue_ids=data.get("linked_issue_ids"),
        linked_task_ids=data.get("linked_task_ids"),
        org_id=data.get("org_id", "org_mvp"),
        project_id=data.get("project_id", "project_mvp"),
        topic_id=data.get("topic_id"),
        author_id=data.get("author_id", "system"),
        sensitivity=data.get("sensitivity", "l1"),
        proposed_sensitivity=data.get("proposed_sensitivity"),
        sensitivity_reason=data.get("sensitivity_reason"),
        tag_origin=data.get("tag_origin", "runtime_default"),
    )


def _item_type(item: InspectionItem) -> str:
    category = (item.category or "").lower()
    if category in {"methods", "method", "方法", "法"}:
        return "method"
    if category in {"people", "person", "人员", "人"}:
        return "person"
    if category in {"followup", "task", "tasks", "todo", "待办"}:
        return "todo"
    if category in {"issue", "open_questions", "question", "问题"}:
        return "issue"
    return "thing"


def _item_search_text(item: InspectionItem) -> str:
    evidence = " ".join(ref.quote for ref in item.evidence_refs)
    owners = " ".join(item.owner_candidates or [])
    return " ".join([
        item.item_id,
        item.title,
        item.description,
        item.category,
        item.matter_type or "",
        " ".join(item.facet_types or []),
        item.due_date or "",
        item.deliverable or "",
        item.acceptance_criteria or "",
        owners,
        evidence,
    ])


def _normalize_lexical_score(raw_score: float) -> float:
    return max(0.0, min(1.0, raw_score / 200.0))


def _parse_search_datetime(value: str) -> datetime:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T00:00:00+00:00"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid search as_of datetime: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _item_effective_datetime(
    item: InspectionItem,
    source: dict[str, Any] | None,
) -> datetime:
    candidates = [
        item.effective_at or "",
        str((source or {}).get("meeting_date") or ""),
        item.updated_at or "",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return _parse_search_datetime(candidate)
        except ValueError:
            continue
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _first_source_id(item: InspectionItem) -> str:
    return item.evidence_refs[0].source_doc_id if item.evidence_refs else ""


def _confirmed_thread_source_count(store: ProjectSQLiteStore, thread_id: str) -> int:
    timeline = store.list_memory_thread_items(thread_id)
    source_ids = {
        row["source_id"]
        for row in timeline
        if row["review_status"] == CandidateStatus.CONFIRMED.value
    }
    source_ids.update(
        row["source_id"]
        for row in store.list_memory_thread_sources(thread_id)
        if row["relation"] == "reinforce"
    )
    return max(1, len(source_ids))


def _confirmed_thread_timeline(
    store: ProjectSQLiteStore,
    thread_id: str,
) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for event in store.list_memory_thread_items(thread_id):
        if event["review_status"] != CandidateStatus.CONFIRMED.value:
            continue
        try:
            item = store.get_item(event["item_id"])
        except KeyError:
            continue
        source = store.get_source(event["source_id"])
        history.append(
            {
                "item_id": item.item_id,
                "org_id": item.org_id,
                "project_id": item.project_id,
                "topic_id": item.topic_id,
                "author_id": item.author_id,
                "sensitivity": item.sensitivity,
                "title": item.title,
                "relation": event["relation"],
                "effective_at": event["effective_at"],
                "source_id": event["source_id"],
                "source_title": str((source or {}).get("title") or event["source_id"]),
                "meeting_date": str((source or {}).get("meeting_date") or ""),
            }
        )
    return history


def _search_item_ids(conn: sqlite3.Connection, query: str, limit: int) -> list[tuple[str, float, list[str]]]:
    return [
        (match["item_id"], float(match["score"]), list(match["reasons"]))
        for match in _search_item_matches(conn, query, limit)
    ]


def _merge_hybrid_memory_matches(
    lexical_matches: list[dict[str, Any]],
    semantic_matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for match in lexical_matches:
        item_id = str(match["item_id"])
        lexical_score = float(
            match.get(
                "lexical_score",
                _normalize_lexical_score(float(match.get("score", 0.0))),
            )
        )
        merged[item_id] = {
            **match,
            "item_id": item_id,
            "lexical_score": lexical_score,
            "semantic_score": 0.0,
            "relevance_score": lexical_score,
        }
    for match in semantic_matches:
        item_id = str(match["item_id"])
        semantic_score = float(
            match.get("semantic_score", match.get("score", 0.0))
        )
        existing = merged.setdefault(
            item_id,
            {
                "item_id": item_id,
                "score": 0.0,
                "lexical_score": 0.0,
                "semantic_score": 0.0,
                "relevance_score": 0.0,
                "reasons": [],
                "highlight": "",
            },
        )
        existing["semantic_score"] = max(
            float(existing.get("semantic_score", 0.0)),
            semantic_score,
        )
        existing["reasons"] = _dedupe(
            [*list(existing.get("reasons", [])), *list(match.get("reasons", []))]
        )
    output: list[dict[str, Any]] = []
    for match in merged.values():
        lexical_score = float(match.get("lexical_score", 0.0))
        semantic_score = float(match.get("semantic_score", 0.0))
        if lexical_score > 0 and semantic_score > 0:
            relevance_score = 0.65 * lexical_score + 0.35 * semantic_score
        elif semantic_score > 0:
            relevance_score = 0.85 * semantic_score
        else:
            relevance_score = lexical_score
        output.append(
            {
                **match,
                "score": relevance_score,
                "relevance_score": relevance_score,
            }
        )
    output.sort(
        key=lambda row: (
            -float(row["relevance_score"]),
            str(row["item_id"]),
        )
    )
    return output


def _search_item_matches(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    eligible_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    if eligible_ids is not None:
        if not eligible_ids:
            return []
        conn.execute("create temp table if not exists eligible_memory_search_ids(id text primary key)")
        conn.execute("delete from eligible_memory_search_ids")
        conn.executemany(
            "insert into eligible_memory_search_ids(id) values(?)",
            ((item_id,) for item_id in eligible_ids),
        )
        eligible_fts_join = "join eligible_memory_search_ids eligible on eligible.id = items_fts.id"
        eligible_items_join = "join eligible_memory_search_ids eligible on eligible.id = items.id"
    else:
        eligible_fts_join = ""
        eligible_items_join = ""
    ranked: dict[str, dict[str, Any]] = {}
    for expression in _fts_match_expressions(query):
        try:
            rows = conn.execute(
                f"""
                select
                  items_fts.id,
                  bm25(items_fts) as rank,
                  snippet(
                    items_fts, -1, '<mark>', '</mark>', ' … ', 24
                  ) as highlight
                from items_fts
                join items on items.id = items_fts.id
                {eligible_fts_join}
                where items_fts match ? and items.status <> 'archived'
                order by rank
                limit ?
                """,
                (expression, limit),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if not _is_fts_query_error(exc):
                raise
            rows = []
        for row in rows:
            _add_ranked_match(
                ranked,
                row["id"],
                _fts_rank_score(float(row["rank"] or 0.0)),
                "fts",
                _normalize_fts_highlight(
                    query,
                    str(row["highlight"] or ""),
                ),
            )

    tokens = _like_tokens(query)
    if tokens:
        clauses = []
        params: list[Any] = []
        for token in tokens:
            pattern = f"%{token}%"
            clauses.append(
                "(title like ? or body like ? or owner like ? or evidence_refs like ? or deliverable like ? or acceptance like ?)"
            )
            params.extend([pattern, pattern, pattern, pattern, pattern, pattern])
        rows = conn.execute(
            f"""
            select items.id, items.title, items.body, items.owner,
                   items.evidence_refs, items.deliverable, items.acceptance
            from items
            {eligible_items_join}
            where items.status <> 'archived' and ({" or ".join(clauses)})
            limit ?
            """,
            (*params, limit),
        ).fetchall()
        for row in rows:
            score = _like_score(query, tokens, row)
            if score > 0:
                _add_ranked_match(ranked, row["id"], score, "like", _like_highlight(query, row))

    return [
        {
            "item_id": item_id,
            "score": float(payload["score"]),
            "reasons": list(payload["reasons"]),
            "highlight": payload.get("highlight", ""),
        }
        for item_id, payload in sorted(ranked.items(), key=lambda item: (-float(item[1]["score"]), item[0]))
    ][:limit]


def _add_ranked_match(ranked: dict[str, dict[str, Any]], item_id: str, score: float, reason: str, highlight: str = "") -> None:
    payload = ranked.setdefault(item_id, {"score": 0.0, "reasons": [], "highlight": ""})
    payload["score"] = float(payload["score"]) + score
    if reason not in payload["reasons"]:
        payload["reasons"].append(reason)
    if highlight and not payload.get("highlight"):
        payload["highlight"] = highlight


def _fts_rank_score(rank: float) -> float:
    relevance = max(0.0, -float(rank))
    return 80.0 + 20.0 * relevance / (1.0 + relevance)


def _normalize_fts_highlight(query: str, snippet: str) -> str:
    plain = str(snippet or "").replace("<mark>", "").replace("</mark>", "")
    target = str(query or "").strip()
    if target and target in plain:
        plain = plain.replace(target, f"<mark>{target}</mark>", 1)
        return _truncate_text(plain, 240)
    return _truncate_text(str(snippet or ""), 240)


def _is_fts_query_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "syntax error" in message or "unterminated" in message


def _fts_match_expressions(query: str) -> list[str]:
    cleaned = query.replace('"', " ").strip()
    if not cleaned:
        return []
    compact = "".join(
        char
        for char in cleaned
        if char.isalnum() or "\u4e00" <= char <= "\u9fff"
    )
    expressions = [f'"{cleaned}"']
    if compact:
        expressions.append(f"{compact}*")
    cjk_chars = [char for char in compact if "\u4e00" <= char <= "\u9fff"]
    if cjk_chars:
        expressions.append(" OR ".join(cjk_chars))
    bigrams = [compact[index:index + 2] for index in range(len(compact) - 1)]
    if bigrams:
        expressions.append(" OR ".join(f"{item}*" for item in bigrams))
    if all(char.isalnum() or char.isspace() or "\u4e00" <= char <= "\u9fff" for char in cleaned):
        expressions.append(cleaned)
    seen: list[str] = []
    for expression in expressions:
        if expression and expression not in seen:
            seen.append(expression)
    return seen


def _like_tokens(query: str) -> list[str]:
    compact = "".join(char for char in query if char.strip())
    tokens = [query.strip(), compact]
    if len(compact) > 1:
        tokens.extend(compact[index:index + 2] for index in range(len(compact) - 1))
    seen: list[str] = []
    for token in tokens:
        if token and token not in seen:
            seen.append(token)
    return seen[:10]


def _like_score(query: str, tokens: list[str], row: sqlite3.Row) -> float:
    title = row["title"] or ""
    body = row["body"] or ""
    owner = row["owner"] or ""
    evidence = row["evidence_refs"] or ""
    deliverable = row["deliverable"] or ""
    acceptance = row["acceptance"] or ""
    full_text = " ".join([title, body, owner, evidence, deliverable, acceptance])
    score = 0.0
    if query and query in full_text:
        score += 40.0
    for index, token in enumerate(tokens):
        weight = 20.0 if index == 0 else 10.0
        if token in title:
            score += weight + max(0.0, 30.0 - index * 5.0)
        elif token in body or token in deliverable or token in acceptance:
            score += weight / 2
        elif token in owner or token in evidence:
            score += weight / 3
    return score


def _like_highlight(query: str, row: sqlite3.Row) -> str:
    text = " ".join(str(row[key] or "") for key in ["title", "body", "owner", "deliverable", "acceptance"])
    if not text:
        return ""
    target = query.strip()
    if target and target in text:
        return _truncate_text(text.replace(target, f"<mark>{target}</mark>", 1), 180)
    return _truncate_text(text, 180)


def _highlight_text(item: InspectionItem, query: str) -> str:
    text = " ".join([item.title, item.description, item.deliverable or "", item.acceptance_criteria or ""])
    query_text = (query or "").strip()
    if query_text and query_text in text:
        return _truncate_text(text.replace(query_text, f"<mark>{query_text}</mark>", 1), 180)
    return _truncate_text(text, 180)


def _source_text_chunks(
    content: str,
    *,
    max_chars: int = 1800,
    overlap_chars: int = 200,
) -> list[dict[str, Any]]:
    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return []
    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            boundary = text.rfind("\n\n", start + max_chars // 2, end)
            if boundary > start:
                end = boundary
        raw_chunk = text[start:end]
        chunk = raw_chunk.strip()
        if chunk:
            chunks.append(
                {
                    "locator": f"chars:{start}-{end}",
                    "content": chunk,
                    "content_hash": hashlib.sha256(
                        chunk.encode("utf-8")
                    ).hexdigest(),
                }
            )
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap_chars)
        start = next_start
    return chunks


def _source_chunk_like_score(query: str, content: str) -> float:
    text = str(content or "")
    target = str(query or "").strip()
    if not target or not text:
        return 0.0
    score = 40.0 if target in text else 0.0
    for index, token in enumerate(_like_tokens(target)):
        if token in text:
            score += max(2.0, 12.0 - index)
    return score


def _source_chunk_highlight(query: str, content: str) -> str:
    text = str(content or "")
    target = str(query or "").strip()
    if target and target in text:
        marked = text.replace(target, f"<mark>{target}</mark>", 1)
        return _truncate_text(marked, 240)
    for token in _like_tokens(target):
        if token and token in text:
            marked = text.replace(token, f"<mark>{token}</mark>", 1)
            return _truncate_text(marked, 240)
    return _truncate_text(text, 240)


def _fts_index_text(value: str) -> str:
    compact = str(value or "")
    tokens = [compact]
    cjk_chars = [char for char in compact if "\u4e00" <= char <= "\u9fff"]
    if cjk_chars:
        tokens.append(" ".join(cjk_chars))
    cjk_text = "".join(cjk_chars)
    if len(cjk_text) > 1:
        tokens.append(" ".join(cjk_text[index:index + 2] for index in range(len(cjk_text) - 1)))
    return " ".join(token for token in tokens if token)


def _embedding_requested() -> bool:
    load_project_env()
    return os.getenv("MEMORY_EMBEDDING", "").strip().lower() in {"1", "true", "on", "yes"}


def _embedding_api_key() -> str:
    return (
        os.getenv("MEMORY_EMBEDDING_API_KEY")
        or os.getenv("OPENAI_COMPAT_API_KEY")
        or os.getenv("GLM_API_KEY")
        or os.getenv("ZHIPUAI_API_KEY")
        or ""
    )


def _embedding_model() -> str:
    return os.getenv("MEMORY_EMBEDDING_MODEL", "embedding-3")


def _embedding_base_url() -> str:
    return (
        os.getenv("MEMORY_EMBEDDING_BASE_URL", "").strip()
        or os.getenv("OPENAI_COMPAT_BASE_URL", "").strip()
        or os.getenv(
            "GLM_BASE_URL",
            "https://open.bigmodel.cn/api/paas/v4/",
        ).strip()
    )


def _embedding_max_batch_size() -> int:
    return max(
        1,
        min(
            int(os.getenv("MEMORY_EMBEDDING_MAX_BATCH_SIZE", "20")),
            128,
        ),
    )


def _embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    api_key = _embedding_api_key()
    if not api_key:
        raise RuntimeError("MEMORY_EMBEDDING=on requires MEMORY_EMBEDDING_API_KEY, GLM_API_KEY, ZHIPUAI_API_KEY, or OPENAI_COMPAT_API_KEY")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is required when MEMORY_EMBEDDING=on") from exc
    client = OpenAI(
        api_key=api_key,
        base_url=_embedding_base_url(),
        timeout=float(os.getenv("MEMORY_EMBEDDING_TIMEOUT_SECONDS", "30")),
        max_retries=int(os.getenv("MEMORY_EMBEDDING_API_RETRIES", "1")),
    )
    response = client.embeddings.create(model=_embedding_model(), input=texts)
    return [[float(value) for value in item.embedding] for item in response.data]


def _embedding_text(item: InspectionItem) -> str:
    text = " ".join(
        [
            item.title,
            item.description,
            " ".join(item.owner_candidates or []),
            item.deliverable or "",
            item.acceptance_criteria or "",
            " ".join(ref.quote for ref in item.evidence_refs),
        ]
    )
    return text[: max(256, int(os.getenv("MEMORY_EMBEDDING_MAX_CHARS", "6000")))]


def _embedding_content_hash(item: InspectionItem) -> str:
    return hashlib.sha256(_embedding_text(item).encode("utf-8")).hexdigest()


def _cosine_similarity(left: list[float], right: list[float] | None) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = sqrt(sum(a * a for a in left))
    right_norm = sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def _milestone_context_text(milestone: MilestonePlan | None) -> str:
    if milestone is None:
        return ""
    return (
        f"当前里程碑：{milestone.name}；项目：{milestone.project}；"
        f"时间：{milestone.date_start} 至 {milestone.date_end}；"
        f"链路：{milestone.scenario_id} / {milestone.chain_name}；"
        f"验收：{'；'.join(milestone.acceptance_criteria)}"
    )


def _source_citation(
    item: InspectionItem,
    source: dict[str, Any] | None,
    source_id: str,
) -> dict[str, Any]:
    if source is None:
        return {"id": source_id, "title": source_id, "meeting_date": ""}
    item_scope = (
        item.org_id,
        item.project_id,
        item.topic_id or None,
        item.sensitivity,
    )
    source_scope = (
        source.get("org_id"),
        source.get("project_id"),
        source.get("topic_id") or None,
        source.get("sensitivity"),
    )
    if item_scope != source_scope:
        return {"id": source_id, "title": "受限来源", "meeting_date": ""}
    return {
        "id": str(source.get("id") or source_id),
        "title": str(source.get("title") or source_id),
        "meeting_date": str(source.get("meeting_date") or ""),
    }


def _retrieval_context_text(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, row in enumerate(rows[:10], start=1):
        source = row.get("source") or {}
        lines.append(
            f"{index}. {row.get('title')} [{row.get('status')}/{row.get('type')}] "
            f"来源={source.get('title') or source.get('id')} {source.get('meeting_date') or ''}；"
            f"摘要={row.get('highlight') or row.get('description') or ''}"
        )
    return "\n".join(lines)


def _truncate_text(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    text = str(value or "")
    return text if len(text) <= limit else text[: max(0, limit - 16)] + "...[truncated]"


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _message_locator_ids(locator: str) -> set[str]:
    prefix = "messages:"
    if not locator.startswith(prefix):
        return set()
    return {
        value.strip()
        for value in locator[len(prefix):].split(",")
        if value.strip()
    }


def _work_record_kind_match_score(
    record: dict[str, Any],
    incoming_kind: str,
) -> int:
    payload = record.get("payload", {})
    generated_kind = str(payload.get("generated_kind") or "")
    if generated_kind and incoming_kind == generated_kind:
        return 2
    if incoming_kind == str(record.get("kind") or ""):
        return 1
    return 0


def _work_record_entry_descriptors(
    entries: list[dict[str, Any]],
    *,
    series_id: str,
    source_origin: str,
) -> list[dict[str, Any]]:
    occurrences: dict[str, int] = {}
    descriptors: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        locator = str(entry.get("source_locator") or "")
        locator_key = locator or f"entry:{index}"
        occurrence = occurrences.get(locator_key, 0)
        occurrences[locator_key] = occurrence + 1
        descriptors.append({
            "locator": locator,
            "logical_key": "\0".join([
                series_id,
                source_origin,
                locator_key,
                str(occurrence),
                str(entry["kind"]),
            ]),
        })
    return descriptors


def _reconcile_work_record_entries(
    entries: list[dict[str, Any]],
    descriptors: list[dict[str, Any]],
    human_rows: list[dict[str, Any]],
    *,
    source_origin: str,
) -> dict[int, dict[str, Any]]:
    edges: list[tuple[tuple[int, ...], int, int, int, int]] = []
    for entry_index, (entry, descriptor) in enumerate(zip(entries, descriptors)):
        entry_locator = str(descriptor["locator"])
        entry_message_ids = _message_locator_ids(entry_locator)
        for human_index, human in enumerate(human_rows):
            payload = human.get("payload", {})
            human_locator = str(human.get("source_locator") or "")
            exact_logical = bool(
                payload.get("logical_key")
                and payload.get("logical_key") == descriptor["logical_key"]
            )
            exact_locator = bool(entry_locator and entry_locator == human_locator)
            human_message_ids = _message_locator_ids(human_locator)
            overlap_count = (
                len(entry_message_ids & human_message_ids)
                if source_origin == "conversation_daily_journal"
                else 0
            )
            if not exact_logical and not exact_locator and overlap_count == 0:
                continue
            kind_score = _work_record_kind_match_score(
                human,
                str(entry["kind"]),
            )
            if kind_score <= 0:
                continue
            generated_text = str(payload.get("generated_text") or "")
            generated_text_match = int(
                bool(generated_text) and generated_text == str(entry["text"])
            )
            difference_size = len(entry_message_ids ^ human_message_ids)
            rank = (
                kind_score,
                int(exact_logical),
                generated_text_match,
                int(exact_locator),
                overlap_count,
                -difference_size,
            )
            edges.append((rank, -entry_index, -human_index, entry_index, human_index))

    assigned_entries: set[int] = set()
    assigned_humans: set[int] = set()
    matches: dict[int, dict[str, Any]] = {}
    for _, _, _, entry_index, human_index in sorted(edges, reverse=True):
        if entry_index in assigned_entries or human_index in assigned_humans:
            continue
        assigned_entries.add(entry_index)
        assigned_humans.add(human_index)
        matches[entry_index] = human_rows[human_index]
    return matches


def _build_dynamic_summary(items: list[InspectionItem]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    evidence_counts: dict[str, int] = {}
    compact_items: list[dict[str, Any]] = []
    method_memory: list[dict[str, Any]] = []
    for item in items:
        status_counts[item.status.value] = status_counts.get(item.status.value, 0) + 1
        for ref in item.evidence_refs:
            evidence_counts[ref.evidence_level] = evidence_counts.get(ref.evidence_level, 0) + 1
        compact = {
            "item_id": item.item_id,
            "type": _item_type(item),
            "status": item.status.value,
            "title": item.title,
            "description": item.description[:260],
            "owners": item.owner_candidates or [],
            "evidence_count": len(item.evidence_refs),
            "source_doc_ids": sorted({ref.source_doc_id for ref in item.evidence_refs}),
            "org_id": item.org_id,
            "project_id": item.project_id,
            "topic_id": item.topic_id,
            "author_id": item.author_id,
            "sensitivity": item.sensitivity,
        }
        compact_items.append(compact)
        if _item_type(item) == "method" and item.status == CandidateStatus.CONFIRMED:
            method_memory.append({
                "item_id": item.item_id,
                "title": item.title,
                "business_goal": item.business_goal or item.description,
                "principles": item.principles or [],
                "reasoning_chain": item.reasoning_chain or [],
                "applicable_scope": item.applicable_scope or "",
                "memory_target": "给人查看 + 长期记忆",
                "evidence_count": len(item.evidence_refs),
            })
    return {
        "schema_version": 2,
        "item_count": len(items),
        "status_counts": status_counts,
        "evidence_level_counts": evidence_counts,
        "compact_items": compact_items,
        "method_memory": method_memory,
        "candidate_backlog": [item for item in compact_items if item["status"] == "candidate"][:20],
        "projection_note": "从 SQLite 动态生成，不写 summary.json。",
    }


def _build_dynamic_index(items: list[InspectionItem]) -> dict[str, Any]:
    by_status: dict[str, list[str]] = {}
    by_owner: dict[str, list[str]] = {}
    by_source_doc: dict[str, list[str]] = {}
    for item in items:
        by_status.setdefault(item.status.value, []).append(item.item_id)
        for owner in item.owner_candidates or []:
            by_owner.setdefault(owner, []).append(item.item_id)
        for ref in item.evidence_refs:
            by_source_doc.setdefault(ref.source_doc_id, []).append(item.item_id)
    return {
        "by_status": by_status,
        "by_owner": by_owner,
        "by_source_doc": by_source_doc,
    }


def _build_dynamic_resume(items: list[InspectionItem]) -> dict[str, Any]:
    candidates = [item for item in items if item.status == CandidateStatus.CANDIDATE]
    return {
        "user_facing_one_liner": f"当前有 {len(candidates)} 条候选需要人工确认。",
        "primary_options": [item.title for item in candidates[:3]],
    }


def _build_context_layers() -> dict[str, Any]:
    return {
        "layers": [
            {"layer_id": "user_visible_brief", "source": "SQLite summary projection"},
            {"layer_id": "agent_decision_memory", "source": "SQLite FTS/embedding/LIKE search"},
            {"layer_id": "evidence_truth", "source": "SQLite items/sources/events"},
            {"layer_id": "debug_audit", "source": "SQLite runs/messages/events"},
        ],
        "prompt_layer_cache_contract": {"enabled": True, "generated_from": "project.db"},
    }


def _safe_filename(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value.strip())
    return cleaned[:80] or "未命名"


def _method_markdown(item: InspectionItem) -> str:
    evidence = "\n".join(
        f"- {ref.source_doc_id} / {ref.locator}: {ref.quote}"
        for ref in item.evidence_refs
    ) or "- 暂无证据"
    principles = "\n".join(f"- {value}" for value in item.principles or []) or "- 暂无"
    reasoning = "\n".join(f"- {value}" for value in item.reasoning_chain or []) or "- 暂无"
    return (
        f"# {item.title}\n\n"
        f"- 状态：{item.status.value}\n"
        f"- 维护人：{item.confirmation_editor or '未记录'}\n"
        f"- 适用范围：{item.applicable_scope or '未记录'}\n\n"
        f"## 业务目标\n\n{item.business_goal or item.description}\n\n"
        f"## 原则\n\n{principles}\n\n"
        f"## 推理链\n\n{reasoning}\n\n"
        f"## 证据\n\n{evidence}\n"
    )


def _meeting_markdown(source: dict[str, Any], related: list[InspectionItem]) -> str:
    lines = [
        f"# {source.get('title') or source['id']}",
        "",
        f"- 日期：{source.get('meeting_date') or '未注明'}",
        f"- 来源：{source['id']}",
        f"- 类型：{source.get('kind') or 'minutes'}",
        "",
        "## 本次沉淀",
        "",
    ]
    if not related:
        lines.append("- 暂无已入库条目")
    for item in related:
        lines.append(f"- [{item.status.value}] {item.item_id} / {item.title}")
    lines.append("")
    return "\n".join(lines)


def _brief_markdown(source: dict[str, Any]) -> str:
    payload = source.get("payload", {})
    content = payload.get("content_markdown", "")
    if content:
        return content if content.endswith("\n") else f"{content}\n"
    return (
        f"# {source.get('title') or source['id']}\n\n"
        f"- 日期：{source.get('meeting_date') or '未注明'}\n"
        f"- 来源：{source['id']}\n\n"
        "今天没有需要你处理的事。\n"
    )
