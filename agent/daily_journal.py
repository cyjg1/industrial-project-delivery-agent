from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from agent.access_policy import User
from agent.llm_provider import LLMProvider, get_provider
from agent.meeting_ingestion import enqueue_source_ingestion
from agent.schemas import SourceDocument, SourceRef
from store.sqlite_store import ProjectSQLiteStore


JOURNAL_JOB_ID = "daily_human_chat_journal"
JOURNAL_TIMEZONE = "Asia/Shanghai"
JOURNAL_PROCESSOR_VERSION = "daily-human-chat-v2"
MAX_BATCH_CHARS = 12000


class DailyJournalValidationError(ValueError):
    pass


@dataclass(frozen=True)
class DailyJournalJob:
    id: str
    hour: int
    minute: int


class DailyJournalScheduler:
    def __init__(
        self,
        job_func: Callable[[], Any],
        *,
        hour: int = 23,
        minute: int = 55,
        timezone_name: str = JOURNAL_TIMEZONE,
    ) -> None:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except Exception as exc:
            raise RuntimeError(
                "apscheduler is required for daily journal scheduling; install requirements before starting the API."
            ) from exc
        self._job = DailyJournalJob(JOURNAL_JOB_ID, hour, minute)
        self._timezone_name = timezone_name
        self._last_error = ""
        self._last_completed_at = ""
        self._scheduler = BackgroundScheduler(timezone=timezone_name)
        self._job_func = job_func
        self._scheduler.add_job(
            self._run_job,
            "cron",
            id=JOURNAL_JOB_ID,
            hour=hour,
            minute=minute,
            replace_existing=True,
        )

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def get_jobs(self) -> list[DailyJournalJob]:
        return [self._job]

    def status(self) -> dict[str, Any]:
        scheduled = self._scheduler.get_job(JOURNAL_JOB_ID)
        next_time = getattr(scheduled, "next_run_time", None) if scheduled else None
        return {
            "enabled": True,
            "running": bool(self._scheduler.running),
            "job_id": self._job.id,
            "hour": self._job.hour,
            "minute": self._job.minute,
            "timezone": self._timezone_name,
            "next_run_time": next_time.isoformat() if next_time else "",
            "last_completed_at": self._last_completed_at,
            "error": self._last_error,
        }

    def record_error(self, error: str) -> None:
        self._last_error = str(error or "")

    def _run_job(self) -> None:
        try:
            self._job_func()
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise
        self._last_error = ""
        self._last_completed_at = _utc_now()


class DailyJournalService:
    def __init__(
        self,
        store: ProjectSQLiteStore,
        *,
        provider: LLMProvider | None = None,
        ingestion_wake: Callable[[], Any] | None = None,
        timezone_name: str = JOURNAL_TIMEZONE,
    ) -> None:
        self.store = store
        self.provider = provider or get_provider(role="extraction")
        self.ingestion_wake = ingestion_wake
        self.timezone = ZoneInfo(timezone_name)

    def compile_day(
        self,
        *,
        project_id: str,
        actor: User,
        report_date: date,
    ) -> dict[str, Any]:
        project = self.store.get_project(project_id)
        if project is None:
            raise KeyError(f"Unknown project: {project_id}")
        if project["org_id"] != actor.org_id:
            raise PermissionError("Daily journal actor and project must belong to the same organization")
        ctx = self.store.access_context_for_actor(actor.id)
        if project_id not in ctx.projects_of(actor):
            raise PermissionError("Daily journal actor must be a project member")
        messages = self._messages(project_id=project_id, actor_id=actor.id, report_date=report_date)
        if not messages:
            return {
                "status": "empty",
                "project_id": project_id,
                "author_id": actor.id,
                "report_date": report_date.isoformat(),
                "message_count": 0,
                "unchanged": True,
            }
        snapshot_hash = _snapshot_hash(messages)
        tags = {
            "org_id": actor.org_id,
            "project_id": project_id,
            "topic_id": None,
            "author_id": actor.id,
            "sensitivity": "l3",
        }
        version = self.store.begin_daily_journal_version(
            project_id=project_id,
            report_date=report_date.isoformat(),
            snapshot_hash=snapshot_hash,
            messages=messages,
            tags=tags,
            actor=actor,
        )
        if version.get("unchanged"):
            return version
        try:
            entries, organization_status = self._organize(
                messages,
                actor=actor,
                project_id=project_id,
                access_context=ctx,
            )
            content = _render_daily_report(
                report_date=report_date,
                actor=actor,
                messages=messages,
                entries=entries,
                organization_status=organization_status,
            )
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            source_id = f"daily_report_{project_id}_{actor.id}_{report_date.strftime('%Y%m%d')}_v{version['version']}"
            report_path = self._write_report(
                project_id=project_id,
                actor_id=actor.id,
                report_date=report_date,
                content_hash=content_hash,
                content=content,
            )
            source = SourceDocument(
                doc_id=source_id,
                title=f"{report_date.isoformat()} {actor.name} 项目日报",
                meeting_date=report_date.isoformat(),
                topic="日常对话自动整理日报",
                curated_source=SourceRef(
                    source_type="Daily human chat report",
                    path=str(report_path),
                    status="matched",
                    notes="Compiled from persisted human-authored chat messages.",
                ),
                raw_source=SourceRef(
                    source_type="Persisted session messages",
                    path=None,
                    status="stored_in_sqlite",
                    notes="Exact message IDs, timestamps and text remain in SQLite and in the report evidence ledger.",
                ),
                tags=["daily_report", "human_chat"],
                org_id=actor.org_id,
                project_id=project_id,
                topic_id=None,
                author_id=actor.id,
                sensitivity="l3",
                tag_origin="daily_chat_compiler",
                input_kind="minutes",
                content_hash=content_hash,
            )
            self.store.ingest(
                source,
                tags=tags,
                actor=actor,
                kind="daily_report",
                materialize=False,
                tag_origin="daily_chat_compiler",
            )
            job = enqueue_source_ingestion(store=self.store, source=source, actor=actor)
            if self.ingestion_wake is not None:
                self.ingestion_wake()
            self.store.replace_daily_work_records(
                series_id=version["journal_id"],
                source_id=source_id,
                source_origin="conversation_daily_journal",
                org_id=actor.org_id,
                project_id=project_id,
                topic_id=None,
                author_id=actor.id,
                sensitivity="l3",
                subject_user_id=actor.id,
                subject_name=actor.name,
                record_date=report_date.isoformat(),
                entries=[
                    {
                        **entry,
                        "evidence_refs": entry["message_refs"],
                        "source_locator": "messages:" + ",".join(
                            ref["message_id"] for ref in entry["message_refs"]
                        ),
                    }
                    for entry in entries
                ],
            )
            completed = self.store.complete_daily_journal_version(
                version["version_id"],
                organization_status=organization_status,
                source_id=source_id,
                ingestion_job_id=job["id"],
                content_markdown=content,
                payload={"entries": entries, "processor_version": JOURNAL_PROCESSOR_VERSION},
            )
            completed["unchanged"] = False
            return completed
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.store.fail_daily_journal_version(version["version_id"], error)
            if isinstance(exc, DailyJournalValidationError):
                raise
            raise DailyJournalValidationError(error) from exc

    def _messages(self, *, project_id: str, actor_id: str, report_date: date) -> list[dict[str, Any]]:
        start_local = datetime.combine(report_date, time.min, tzinfo=self.timezone)
        end_local = start_local + timedelta(days=1)
        return self.store.list_human_messages(
            project_id=project_id,
            actor_id=actor_id,
            start_at=start_local.astimezone(timezone.utc).isoformat(),
            end_at=end_local.astimezone(timezone.utc).isoformat(),
        )

    def _organize(
        self,
        messages: list[dict[str, Any]],
        *,
        actor: User,
        project_id: str,
        access_context: Any,
    ) -> tuple[list[dict[str, Any]], str]:
        entries: list[dict[str, Any]] = []
        for batch in _message_batches(messages):
            prompt = (
                "Organize only these persisted human-authored project messages into a concise daily work ledger. "
                "Return JSON only: {\"entries\":[{\"kind\":\"work|conclusion|problem|output\","
                "\"text\":string,\"message_refs\":[{\"message_id\":string,\"quote\":exact substring}]}]}. "
                "work means what was done; conclusion means an explicit result or decision; problem means a concrete blocker "
                "or unresolved issue; output means a named artifact or submitted result. Do not invent a message ID, paraphrase "
                "a quote, infer an output that was not stated, or emit images and attachment bodies.\n"
                "HUMAN_MESSAGES=" + json.dumps(batch, ensure_ascii=False)
            )
            payload = _json_object(self.provider.complete_json(prompt))
            raw_entries = payload.get("entries")
            if not isinstance(raw_entries, list):
                raise DailyJournalValidationError("daily journal model output must include entries array")
            entries.extend(_validated_entries(raw_entries, batch))
        deduped = _dedupe_entries(entries)
        for entry in deduped:
            entry["solution_options"] = []
            if entry["kind"] != "problem":
                continue
            memory_rows = self.store.search_memory(
                entry["text"],
                {"project_id": project_id, "status": "confirmed"},
                limit=5,
                actor=actor,
                access_context=access_context,
            )
            if not memory_rows:
                continue
            memory_prompt_rows = [
                {
                    "item_id": str(row.get("item_id") or row.get("id") or ""),
                    "title": str(row.get("title") or ""),
                    "description": str(row.get("description") or row.get("body") or ""),
                    "source_doc_id": str(row.get("source_doc_id") or ""),
                    "meeting_date": str(row.get("meeting_date") or ""),
                    "highlight": str(row.get("highlight") or ""),
                }
                for row in memory_rows
            ]
            option_prompt = (
                "Propose at most three practical solution options for this project problem using only the visible memory rows. "
                "Return JSON only: {\"options\":[{\"text\":string,\"memory_refs\":[\"item_id\"]}]}. "
                "Every option must cite at least one provided item_id; do not use outside facts. "
                f"PROBLEM={json.dumps(entry['text'], ensure_ascii=False)}\n"
                "VISIBLE_MEMORY=" + json.dumps(memory_prompt_rows, ensure_ascii=False)
            )
            option_payload = _json_object(self.provider.complete_json(option_prompt))
            entry["solution_options"] = _validated_solution_options(
                option_payload.get("options"),
                {row["item_id"] for row in memory_prompt_rows if row["item_id"]},
            )
        return deduped, "model_organized"

    def _write_report(
        self,
        *,
        project_id: str,
        actor_id: str,
        report_date: date,
        content_hash: str,
        content: str,
    ) -> Path:
        target = (
            self.store.root_dir
            / "daily_reports"
            / project_id
            / actor_id
            / report_date.isoformat()
            / content_hash
            / "report.md"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_text(encoding="utf-8") != content:
            raise DailyJournalValidationError("immutable daily report path has conflicting content")
        if not target.exists():
            # The content hash is defined over UTF-8 bytes. Writing text would
            # translate newlines on Windows and break the immutable hash check.
            target.write_bytes(content.encode("utf-8"))
        return target


def compile_project_journals(
    *,
    store: ProjectSQLiteStore,
    project_id: str,
    report_date: date,
    provider: LLMProvider | None = None,
    ingestion_wake: Callable[[], Any] | None = None,
    timezone_name: str = JOURNAL_TIMEZONE,
) -> list[dict[str, Any]]:
    zone = ZoneInfo(timezone_name)
    start_local = datetime.combine(report_date, time.min, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    actor_ids = store.human_message_actor_ids(
        project_id=project_id,
        start_at=start_local.astimezone(timezone.utc).isoformat(),
        end_at=end_local.astimezone(timezone.utc).isoformat(),
    )
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for actor_id in actor_ids:
        row = store.get_access_user(actor_id)
        if row is None:
            errors.append(f"{actor_id}: actor no longer exists")
            continue
        actor = User(
            id=str(row["id"]),
            org_id=str(row["org_id"]),
            name=str(row["name"]),
            feishu_id=str(row.get("feishu_id") or ""),
        )
        try:
            results.append(DailyJournalService(
                store,
                provider=provider,
                ingestion_wake=ingestion_wake,
                timezone_name=timezone_name,
            ).compile_day(project_id=project_id, actor=actor, report_date=report_date))
        except Exception as exc:
            errors.append(f"{actor_id}: {type(exc).__name__}: {exc}")
    if errors:
        raise DailyJournalValidationError("Daily journal failures: " + " | ".join(errors))
    return results


def _message_batches(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    active: list[dict[str, Any]] = []
    active_chars = 0
    for message in messages:
        content = str(message.get("content") or "")
        segments = [content[index:index + MAX_BATCH_CHARS] for index in range(0, len(content), MAX_BATCH_CHARS)] or [""]
        for segment_index, segment in enumerate(segments):
            row = {**message, "content": segment, "segment_index": segment_index}
            size = len(json.dumps(row, ensure_ascii=False))
            if active and active_chars + size > MAX_BATCH_CHARS:
                batches.append(active)
                active = []
                active_chars = 0
            active.append(row)
            active_chars += size
    if active:
        batches.append(active)
    return batches


def _validated_entries(
    raw_entries: list[Any],
    batch: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    allowed_kinds = {"work", "conclusion", "problem", "output"}
    by_id: dict[str, list[str]] = {}
    for message in batch:
        by_id.setdefault(str(message["message_id"]), []).append(str(message["content"]))
    result: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            raise DailyJournalValidationError(f"entries[{index}] must be an object")
        kind = str(entry.get("kind") or "").strip()
        text = str(entry.get("text") or "").strip()
        refs = entry.get("message_refs")
        if kind not in allowed_kinds or not text or not isinstance(refs, list) or not refs:
            raise DailyJournalValidationError(f"entries[{index}] is missing kind, text, or message_refs")
        normalized_refs: list[dict[str, str]] = []
        for ref in refs:
            if not isinstance(ref, dict):
                raise DailyJournalValidationError(f"entries[{index}] message reference must be an object")
            message_id = str(ref.get("message_id") or "")
            quote = str(ref.get("quote") or "")
            if message_id not in by_id:
                raise DailyJournalValidationError(f"entries[{index}] references unknown message_id")
            if not quote or not any(quote in content for content in by_id[message_id]):
                raise DailyJournalValidationError(f"entries[{index}] quote is not an exact human-message substring")
            normalized_refs.append({"message_id": message_id, "quote": quote})
        result.append({"kind": kind, "text": text, "message_refs": normalized_refs})
    return result


def _validated_solution_options(raw_options: Any, allowed_memory_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(raw_options, list):
        raise DailyJournalValidationError("daily journal problem options must be an array")
    result: list[dict[str, Any]] = []
    for index, option in enumerate(raw_options[:3]):
        if not isinstance(option, dict):
            raise DailyJournalValidationError(f"problem options[{index}] must be an object")
        text = str(option.get("text") or "").strip()
        raw_refs = option.get("memory_refs")
        if not text or not isinstance(raw_refs, list) or not raw_refs:
            raise DailyJournalValidationError(
                f"problem options[{index}] requires text and memory_refs"
            )
        memory_refs = [str(value) for value in raw_refs if str(value).strip()]
        if not memory_refs or any(value not in allowed_memory_ids for value in memory_refs):
            raise DailyJournalValidationError(
                f"problem options[{index}] contains an unknown memory reference"
            )
        result.append({"text": text, "memory_refs": list(dict.fromkeys(memory_refs))})
    return result


def _dedupe_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for entry in entries:
        key = hashlib.sha256(json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if key not in seen:
            result.append(entry)
            seen.add(key)
    return result


def _render_daily_report(
    *,
    report_date: date,
    actor: User,
    messages: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    organization_status: str,
) -> str:
    lines = [
        f"# {report_date.isoformat()} 项目日报",
        "",
        f"- 记录人：{actor.name} ({actor.id})",
        f"- 人类消息数：{len(messages)}",
        "- 证据范围：仅本人的人类输入，不含 Agent 回复和工具输出",
        "",
        "## 整理结果",
    ]
    if not entries:
        lines.append("模型未识别出需要沉淀的工作、结论、问题或产出。")
    else:
        labels = {"work": "做了什么", "conclusion": "结论", "problem": "问题", "output": "产出"}
        for entry in entries:
            refs = "、".join(ref["message_id"] for ref in entry["message_refs"])
            lines.append(f"- [{labels[entry['kind']]}] {entry['text']}（证据：{refs}）")
            for option in entry.get("solution_options") or []:
                memory_refs = "、".join(option["memory_refs"])
                lines.append(f"  - 解决选项：{option['text']}（记忆依据：{memory_refs}）")
    lines.extend(["", "## 人类原始消息证据"])
    for message in messages:
        lines.extend([
            "",
            f"### {message['created_at']} · {message['message_id']} · 会话 {message['session_id']}",
            str(message["content"]),
        ])
    return "\n".join(lines).strip() + "\n"


def _snapshot_hash(
    messages: list[dict[str, Any]],
    *,
    processor_version: str = JOURNAL_PROCESSOR_VERSION,
) -> str:
    canonical = [
        {
            "message_id": row["message_id"],
            "session_id": row["session_id"],
            "created_at": row["created_at"],
            "content": row["content"],
        }
        for row in messages
    ]
    versioned_snapshot = {
        "processor_version": processor_version,
        "messages": canonical,
    }
    return hashlib.sha256(
        json.dumps(
            versioned_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _json_object(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DailyJournalValidationError("daily journal model output is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise DailyJournalValidationError("daily journal model output must be one JSON object")
    return payload


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
