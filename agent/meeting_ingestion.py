from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from agent.access_policy import User
from agent.llm_provider import LLMProvider, get_provider
from agent.schemas import (
    CandidateItem,
    CandidateStatus,
    ExtractionResult,
    SourceDocument,
    SourceRef,
    to_plain,
)
from agent.semantic_extraction import extract_document_semantically
from ingestion.source_manifest import TRANSCRIPT_SUFFIXES, UPLOAD_TEXT_SUFFIXES, source_from_stored_row
from skills.meeting_minutes.runner import MeetingMinutesSkillRunner
from skills.meeting_minutes.transcript import parse_transcript_bytes
from skills.registry import get_project_skill
from store.sqlite_store import ACCESS_TAG_FIELDS, ProjectSQLiteStore


PROCESSOR_VERSION = "continuous-meeting-memory-v1"
THREAD_RELATIONS = {"new", "update", "reinforce", "conflict", "resolved", "supersede"}
CHANGEABLE_FIELDS = {
    "title",
    "description",
    "owner_candidates",
    "due_date",
    "deliverable",
    "acceptance_criteria",
    "business_goal",
    "principles",
    "reasoning_chain",
    "applicable_scope",
}


class MeetingIngestionError(RuntimeError):
    pass


def ingestion_job_max_attempts() -> int:
    raw = os.getenv("INGESTION_JOB_MAX_ATTEMPTS", "3")
    try:
        value = int(raw)
    except ValueError as exc:
        raise MeetingIngestionError("INGESTION_JOB_MAX_ATTEMPTS must be an integer") from exc
    if value < 1:
        raise MeetingIngestionError("INGESTION_JOB_MAX_ATTEMPTS must be >= 1")
    return value


def resolve_meeting_input_kind(requested: str, path: Path) -> str:
    input_kind = str(requested or "auto").strip().lower()
    if input_kind in {"minutes", "transcript"}:
        return input_kind
    suffix = path.suffix.lower()
    if suffix in UPLOAD_TEXT_SUFFIXES:
        return "minutes"
    if suffix in TRANSCRIPT_SUFFIXES:
        return "transcript"
    raise MeetingIngestionError(
        f"input_kind=auto is ambiguous for {suffix or 'extensionless input'}; "
        "choose minutes or transcript explicitly"
    )


def enqueue_source_ingestion(
    *,
    store: ProjectSQLiteStore,
    source: SourceDocument,
    actor: User,
) -> dict[str, Any]:
    input_path = source.raw_source.path if source.input_kind == "transcript" else source.curated_source.path
    if not input_path:
        input_path = source.raw_source.path or source.curated_source.path
    if not input_path:
        raise MeetingIngestionError("Registered source has no immutable input path")
    if not source.content_hash:
        raise MeetingIngestionError("Registered source has no content hash")
    stored_source = store.get_source(source.doc_id) or {}
    return store.enqueue_ingestion_job(
        source_id=source.doc_id,
        input_kind=source.input_kind,
        content_hash=source.content_hash,
        processor_version=PROCESSOR_VERSION,
        input_path=str(input_path),
        actor=actor,
        max_attempts=ingestion_job_max_attempts(),
        payload={
            "source_title": source.title,
            "source_kind": str(stored_source.get("kind") or source.input_kind),
        },
    )


def source_ingestion_view(source: SourceDocument) -> dict[str, Any]:
    return {
        "doc_id": source.doc_id,
        "title": source.title,
        "meeting_date": source.meeting_date,
        "topic": source.topic,
        "input_kind": source.input_kind,
        "content_hash": source.content_hash,
        "curated_status": source.curated_source.status,
        "raw_status": source.raw_source.status,
        "org_id": source.org_id,
        "project_id": source.project_id,
        "topic_id": source.topic_id,
        "author_id": source.author_id,
        "sensitivity": source.sensitivity,
    }


class MeetingIngestionService:
    processor_version = PROCESSOR_VERSION

    def __init__(
        self,
        *,
        store: ProjectSQLiteStore,
        minutes_runner: MeetingMinutesSkillRunner | None = None,
        extraction_provider: LLMProvider | None = None,
        continuity_provider: LLMProvider | None = None,
    ):
        self.store = store
        self.minutes_runner = minutes_runner or get_project_skill(
            "meeting_minutes",
            store_dir=store.root_dir / "artifacts" / "meeting_minutes",
        ).runner
        self.extraction_provider = extraction_provider or get_provider(role="extraction")
        self.continuity_provider = continuity_provider or get_provider(role="classification")

    def process_next(self, *, worker_id: str) -> dict[str, Any] | None:
        job = self.store.claim_next_ingestion_job(worker_id=worker_id)
        if job is None:
            return None
        try:
            return self.process_job(job["id"])
        except Exception as exc:
            self.store.fail_ingestion_job(job["id"], f"{type(exc).__name__}: {exc}")
            raise

    def process_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_ingestion_job(job_id)
        if job["status"] != "running":
            raise MeetingIngestionError(
                f"Ingestion job must be claimed before processing: {job['status']}"
            )
        actor = self._actor(job)
        source_row = self.store.get_source(job["source_id"])
        if source_row is None:
            raise MeetingIngestionError(f"Source disappeared before processing: {job['source_id']}")
        source = source_from_stored_row(source_row)
        input_path = Path(job["input_path"])
        if not input_path.is_file():
            raise MeetingIngestionError(f"Immutable ingestion input does not exist: {input_path}")
        content = input_path.read_bytes()
        actual_hash = hashlib.sha256(content).hexdigest()
        expected_hash = str(job["content_hash"]).removeprefix("sha256:")
        if actual_hash != expected_hash:
            raise MeetingIngestionError(
                f"Ingestion input hash mismatch: expected {expected_hash}, got {actual_hash}"
            )

        input_kind = self._resolve_job_input_kind(job, input_path)
        self.store.update_ingestion_job(job_id, stage="generating_minutes")
        if input_kind == "transcript":
            transcript_text = parse_transcript_bytes(content, input_path.name)
            resume_summaries = {
                chunk["chunk_index"]: chunk["summary"]
                for chunk in self.store.list_ingestion_chunks(job_id)
                if chunk["status"] == "completed"
                and chunk["summary"]
                and chunk["summary"] != "minutes_generation_complete"
            }
            minutes_result = self.minutes_runner.run(
                {
                    "meeting_id": source.doc_id,
                    "meeting_date": source.meeting_date,
                    "transcript_text": transcript_text,
                    "doc_nature": "项目会议纪要",
                    "project_background": source.topic,
                    "resume_chunk_summaries": resume_summaries,
                    "on_chunks": lambda chunks, coverage: self._prepare_chunks(job_id, chunks),
                    "on_chunk_started": lambda index: self.store.update_ingestion_chunk(
                        job_id, index, status="running", error=""
                    ),
                    "on_chunk_completed": lambda index, summary: self.store.update_ingestion_chunk(
                        job_id, index, status="completed", summary=summary, error=""
                    ),
                }
            )
            minutes_markdown = str(minutes_result["minutes_markdown"])
            minutes_path = Path(str(minutes_result["minutes_path"]))
        else:
            minutes_markdown = _parse_minutes_bytes(content, input_path.name)
            minutes_path = self._save_existing_minutes(source.doc_id, source.meeting_date, minutes_markdown)
            self._persist_single_minutes_chunk(job_id, minutes_markdown)

        minutes_hash = hashlib.sha256(minutes_markdown.encode("utf-8")).hexdigest()
        source.curated_source = SourceRef(
            source_type="Generated meeting minutes" if input_kind == "transcript" else "Uploaded meeting minutes",
            path=str(minutes_path),
            status="matched",
            notes="Generated from the immutable transcript version." if input_kind == "transcript" else "Uploaded as curated meeting minutes.",
        )
        source.input_kind = input_kind
        source.content_hash = actual_hash
        if "generated_minutes" not in source.tags and input_kind == "transcript":
            source.tags.append("generated_minutes")
        source_kind = str(job.get("payload", {}).get("source_kind") or input_kind)
        self.store.save_source(source, kind=source_kind, materialize=False)
        self.store.update_ingestion_job(
            job_id,
            stage="extracting_candidates",
            minutes_path=str(minutes_path),
            minutes_hash=minutes_hash,
        )

        extraction = extract_document_semantically(
            source,
            minutes_markdown,
            provider=self.extraction_provider,
            ingestion_job_id=job_id,
        )

        counts = {
            "candidate_count": 0,
            "delta_new": 0,
            "delta_updated": 0,
            "delta_conflict": 0,
            "delta_resolved": 0,
            "delta_auto_merged": 0,
        }
        for item in _extraction_items(extraction):
            outcome = self._consolidate_item(
                actor=actor,
                source=source,
                job=job,
                item=item,
            )
            if outcome == "auto_merged":
                counts["delta_auto_merged"] += 1
                continue
            counts["candidate_count"] += 1
            if outcome == "new":
                counts["delta_new"] += 1
            elif outcome == "conflict":
                counts["delta_conflict"] += 1
            elif outcome == "resolved":
                counts["delta_resolved"] += 1
            else:
                counts["delta_updated"] += 1

        completed = self.store.complete_ingestion_job(
            job_id,
            minutes_path=str(minutes_path),
            minutes_hash=minutes_hash,
            payload={
            },
            **counts,
        )
        return {
            "job_id": job_id,
            "source_id": source.doc_id,
            "status": completed["status"],
            "minutes_path": str(minutes_path),
            **counts,
        }

    def _actor(self, job: dict[str, Any]) -> User:
        row = self.store.get_access_user(job["actor_id"])
        if row is None:
            raise MeetingIngestionError(f"Unknown ingestion actor: {job['actor_id']}")
        actor = User(
            id=str(row["id"]),
            org_id=str(row["org_id"]),
            name=str(row["name"]),
            feishu_id=str(row.get("feishu_id") or ""),
        )
        if actor.org_id != job["org_id"]:
            raise MeetingIngestionError("Ingestion actor and job belong to different organizations")
        return actor

    def _resolve_job_input_kind(self, job: dict[str, Any], path: Path) -> str:
        return resolve_meeting_input_kind(str(job["input_kind"]), path)

    def _prepare_chunks(self, job_id: str, chunks: list[Any]) -> None:
        expected = [
            {
                "chunk_index": int(chunk.index),
                "start_char": int(chunk.start_char),
                "end_char": int(chunk.end_char),
                "content_hash": str(chunk.content_hash),
            }
            for chunk in chunks
        ]
        existing = self.store.list_ingestion_chunks(job_id)
        if not existing:
            self.store.replace_ingestion_chunks(job_id, expected)
            return
        actual = [
            {
                "chunk_index": chunk["chunk_index"],
                "start_char": chunk["start_char"],
                "end_char": chunk["end_char"],
                "content_hash": chunk["content_hash"],
            }
            for chunk in existing
        ]
        if actual != expected:
            raise MeetingIngestionError("Persisted transcript chunks do not match the immutable input")

    def _persist_single_minutes_chunk(self, job_id: str, minutes: str) -> None:
        digest = hashlib.sha256(minutes.encode("utf-8")).hexdigest()
        existing = self.store.list_ingestion_chunks(job_id)
        if not existing:
            self.store.replace_ingestion_chunks(
                job_id,
                [
                    {
                        "chunk_index": 0,
                        "start_char": 0,
                        "end_char": max(1, len(minutes)),
                        "content_hash": digest,
                    }
                ],
            )
        self.store.update_ingestion_chunk(
            job_id,
            0,
            status="completed",
            summary="uploaded_minutes_ready",
            error="",
        )

    def _save_existing_minutes(self, meeting_id: str, meeting_date: str, minutes: str) -> Path:
        return self.minutes_runner.store.save_minutes(
            meeting_id=meeting_id,
            meeting_date=meeting_date,
            markdown=minutes,
        )

    def _consolidate_item(
        self,
        *,
        actor: User,
        source: Any,
        job: dict[str, Any],
        item: CandidateItem,
    ) -> str:
        item.claim_hash = _claim_hash(item)
        item.effective_at = _effective_at(source.meeting_date, job["created_at"])
        scoped_threads = self._visible_scoped_threads(actor, item)
        exact = self._find_exact_current(scoped_threads, item.claim_hash)
        if exact is not None:
            current = self.store.get_item(exact["current_item_id"])
            current.evidence_refs = _merge_evidence_refs(current.evidence_refs, item.evidence_refs)
            current.claim_hash = current.claim_hash or item.claim_hash
            self.store.save_item(current, materialize=False)
            self.store.link_memory_thread_source(
                thread_id=exact["id"],
                ingestion_job_id=job["id"],
                source_id=source.doc_id,
                relation="reinforce",
                effective_at=item.effective_at,
                evidence_refs=[to_plain(ref) for ref in item.evidence_refs],
            )
            self.store.append_event(
                "memory_thread",
                exact["id"],
                "thread_evidence_auto_merged",
                {"source_id": source.doc_id, "ingestion_job_id": job["id"], "claim_hash": item.claim_hash},
            )
            return "auto_merged"

        proposal = _continuity_proposal(
            provider=self.continuity_provider,
            item=item,
            threads=scoped_threads,
            store=self.store,
        )
        relation = proposal["relation"]
        if relation == "new":
            thread_id = _new_thread_id(item, proposal["thread_key"])
            thread = self.store.upsert_memory_thread(
                thread_id=thread_id,
                category=item.category,
                thread_key=proposal["thread_key"],
                title=proposal["thread_title"],
                tags=_item_tags(item),
                actor=actor,
                first_seen_at=item.effective_at,
            )
        else:
            thread = next(
                (candidate for candidate in scoped_threads if candidate["id"] == proposal["thread_id"]),
                None,
            )
            if thread is None:
                raise MeetingIngestionError("Continuity model selected a thread outside its allowed set")

        item.thread_id = thread["id"]
        item.thread_title = thread["title"]
        item.thread_key = thread["thread_key"]
        item.thread_event = relation
        item.changed_fields = proposal["changed_fields"]
        item.review_required = True
        item.status = CandidateStatus.CANDIDATE
        reason = proposal.get("reason", "")
        if reason:
            item.inference_note = " ".join(
                value for value in [item.inference_note or "", f"连续事项判断：{reason}"] if value
            )
        self.store.ingest(
            item,
            tags=_item_tags(item),
            actor=actor,
            materialize=False,
            tag_origin="source_inherited",
        )
        self.store.link_memory_thread_item(
            thread_id=thread["id"],
            item_id=item.item_id,
            source_id=source.doc_id,
            ingestion_job_id=job["id"],
            relation=relation,
            effective_at=item.effective_at,
            review_required=True,
        )
        self.store.link_memory_thread_source(
            thread_id=thread["id"],
            ingestion_job_id=job["id"],
            source_id=source.doc_id,
            relation=relation,
            effective_at=item.effective_at,
            evidence_refs=[to_plain(ref) for ref in item.evidence_refs],
        )
        return relation

    def _visible_scoped_threads(self, actor: User, item: CandidateItem) -> list[dict[str, Any]]:
        threads = self.store.list_memory_threads(
            project_id=item.project_id,
            actor=actor,
            ctx=self.store.access_context_for_actor(actor.id),
        )
        return [
            thread
            for thread in threads
            if thread["status"] != "archived"
            and all((thread.get(field) or None) == (getattr(item, field) or None) for field in ACCESS_TAG_FIELDS)
            and thread["category"] == item.category
        ]

    def _find_exact_current(
        self,
        threads: list[dict[str, Any]],
        claim_hash: str,
    ) -> dict[str, Any] | None:
        for thread in threads:
            current_item_id = thread.get("current_item_id")
            if not current_item_id:
                continue
            current = self.store.get_item(current_item_id)
            current_hash = current.claim_hash or _claim_hash(current)
            if current.status == CandidateStatus.CONFIRMED and current_hash == claim_hash:
                return thread
        return None


def _continuity_proposal(
    *,
    provider: LLMProvider,
    item: CandidateItem,
    threads: list[dict[str, Any]],
    store: ProjectSQLiteStore,
) -> dict[str, Any]:
    allowed_ids = [thread["id"] for thread in threads]
    visible_threads = []
    for thread in threads:
        current = None
        if thread.get("current_item_id"):
            current_item = store.get_item(thread["current_item_id"])
            current = {
                "item_id": current_item.item_id,
                "title": current_item.title,
                "description": current_item.description,
                "effective_at": current_item.effective_at,
            }
        visible_threads.append(
            {
                "id": thread["id"],
                "title": thread["title"],
                "thread_key": thread["thread_key"],
                "status": thread["status"],
                "current": current,
            }
        )
    prompt = (
        "You classify one newly extracted, evidence-backed project item into a continuous issue thread.\n"
        "Choose only a thread ID listed in ALLOWED_THREAD_IDS_JSON, or relation=new with thread_id=null.\n"
        "Return one JSON object with thread_id, thread_title, thread_key, relation, reason, changed_fields.\n"
        "Allowed relations: new, update, reinforce, conflict, resolved, supersede.\n"
        "A semantic reinforce still requires human review; do not claim that any fact is confirmed.\n"
        f"ALLOWED_THREAD_IDS_JSON={json.dumps(allowed_ids, ensure_ascii=False)}\n"
        f"VISIBLE_THREADS_JSON={json.dumps(visible_threads, ensure_ascii=False)}\n"
        f"NEW_ITEM_JSON={json.dumps(_continuity_item_payload(item), ensure_ascii=False)}"
    )
    payload = _parse_json_object(provider.complete_json(prompt))
    relation = str(payload.get("relation") or "").strip().lower()
    if relation not in THREAD_RELATIONS:
        raise MeetingIngestionError(f"Continuity model returned invalid relation: {relation}")
    thread_id = str(payload.get("thread_id") or "").strip() or None
    if relation == "new":
        if thread_id is not None:
            raise MeetingIngestionError("A new continuity thread cannot reuse a thread_id")
    elif thread_id not in allowed_ids:
        raise MeetingIngestionError("Continuity model selected a thread outside its allowed set")
    thread_title = _required_model_text(payload, "thread_title", 160)
    thread_key = _required_model_text(payload, "thread_key", 160)
    changed_fields = payload.get("changed_fields") or []
    if not isinstance(changed_fields, list):
        raise MeetingIngestionError("Continuity changed_fields must be an array")
    normalized_changed = []
    for field in changed_fields:
        field_name = str(field).strip()
        if field_name not in CHANGEABLE_FIELDS:
            raise MeetingIngestionError(f"Continuity model returned unsupported changed field: {field_name}")
        if field_name not in normalized_changed:
            normalized_changed.append(field_name)
    return {
        "thread_id": thread_id,
        "thread_title": thread_title,
        "thread_key": thread_key,
        "relation": relation,
        "reason": str(payload.get("reason") or "").strip()[:500],
        "changed_fields": normalized_changed,
    }


def _parse_minutes_bytes(content: bytes, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".docx":
        return parse_transcript_bytes(content, filename)
    if suffix in {".md", ".markdown"}:
        return content.decode("utf-8-sig")
    if suffix == ".txt":
        return parse_transcript_bytes(content, filename)
    raise MeetingIngestionError(
        f"Unsupported meeting minutes format: {suffix}; use .md, .txt or .docx"
    )


def _extraction_items(result: ExtractionResult) -> list[CandidateItem]:
    return [
        *result.people,
        *result.things,
        *result.methods,
        *result.chain_links,
        *result.questions,
    ]


def _effective_at(meeting_date: str, fallback: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(meeting_date or "")):
        return f"{meeting_date}T00:00:00+00:00"
    return fallback


def _claim_hash(item: Any) -> str:
    payload = {
        "category": item.category,
        "title": _normalize_claim_text(item.title),
        "description": _normalize_claim_text(item.description),
        "owner_candidates": sorted(item.owner_candidates or []),
        "due_date": item.due_date or "",
        "deliverable": _normalize_claim_text(item.deliverable or ""),
        "acceptance_criteria": _normalize_claim_text(item.acceptance_criteria or ""),
        "business_goal": _normalize_claim_text(item.business_goal or ""),
        "principles": [_normalize_claim_text(value) for value in item.principles or []],
        "reasoning_chain": [_normalize_claim_text(value) for value in item.reasoning_chain or []],
        "applicable_scope": _normalize_claim_text(item.applicable_scope or ""),
        "proposed_sensitivity": item.proposed_sensitivity or "",
        "sensitivity_reason": _normalize_claim_text(item.sensitivity_reason or ""),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _normalize_claim_text(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _merge_evidence_refs(current: list[Any], incoming: list[Any]) -> list[Any]:
    seen = {
        (ref.source_doc_id, ref.ingestion_job_id, ref.locator, ref.quote)
        for ref in current
    }
    merged = list(current)
    for ref in incoming:
        key = (ref.source_doc_id, ref.ingestion_job_id, ref.locator, ref.quote)
        if key not in seen:
            merged.append(ref)
            seen.add(key)
    return merged


def _item_tags(item: Any) -> dict[str, Any]:
    return {field: getattr(item, field) for field in ACCESS_TAG_FIELDS}


def _new_thread_id(item: CandidateItem, thread_key: str) -> str:
    scope = "\0".join(
        [
            item.org_id,
            item.project_id,
            item.topic_id or "",
            item.sensitivity,
            item.category,
            thread_key,
        ]
    )
    return "thread_" + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:20]


def _continuity_item_payload(item: CandidateItem) -> dict[str, Any]:
    return {
        "category": item.category,
        "title": item.title,
        "description": item.description,
        "owner_candidates": item.owner_candidates or [],
        "due_date": item.due_date,
        "deliverable": item.deliverable,
        "acceptance_criteria": item.acceptance_criteria,
        "effective_at": item.effective_at,
    }


def _parse_json_object(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise MeetingIngestionError("Continuity model response must be a JSON object")
    return payload


def _required_model_text(payload: dict[str, Any], field: str, limit: int) -> str:
    value = " ".join(str(payload.get(field) or "").strip().split())[:limit]
    if not value:
        raise MeetingIngestionError(f"Continuity model response is missing {field}")
    return value
