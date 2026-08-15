from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from agent.access_policy import User
from agent.meeting_ingestion import MeetingIngestionError
from agent.schemas import CandidateStatus, EvidenceRef, InspectionItem, SourceRef, WorkItemStatus
from ingestion.source_manifest import source_from_stored_row
from ingestion.structured_workbook import (
    STRUCTURED_INPUT_KINDS,
    WorkbookRow,
    detect_structured_workbook_kind,
    read_structured_workbook,
)
from store.sqlite_store import ProjectSQLiteStore


class StructuredIngestionService:
    def __init__(self, *, store: ProjectSQLiteStore):
        self.store = store

    def backfill_work_log_source(self, source_id: str) -> dict[str, Any]:
        source_row = self.store.get_source(source_id)
        if source_row is None:
            raise KeyError(source_id)
        if source_row.get("kind") != "work_logs" and source_row.get("input_kind") != "work_logs":
            raise MeetingIngestionError(f"Source is not a historical work-log workbook: {source_id}")
        source = source_from_stored_row(source_row)
        source.input_kind = "work_logs"
        job_id = str(source_row.get("current_ingestion_job_id") or "")
        job = self.store.get_ingestion_job(job_id) if job_id else None
        path = Path(
            str((job or {}).get("input_path") or "")
            or source.curated_source.path
            or source.raw_source.path
            or source_row.get("path")
            or ""
        )
        if not path.is_file():
            raise MeetingIngestionError(f"Historical work-log workbook is missing: {path}")
        actor_row = self.store.get_access_user(source.author_id)
        if actor_row is None:
            raise MeetingIngestionError(f"Unknown historical import actor: {source.author_id}")
        actor = User(
            id=str(actor_row["id"]),
            org_id=str(actor_row["org_id"]),
            name=str(actor_row["name"]),
            feishu_id=str(actor_row.get("feishu_id") or ""),
        )
        rows = read_structured_workbook(path, "work_logs")
        job_id = job_id or f"historical_backfill:{source_id}"
        return {
            "source_id": source_id,
            "row_count": len(rows),
            **self._import_work_logs(
                rows,
                source,
                actor,
                job_id,
                backfill_records=True,
            ),
        }

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

        input_kind = str(job["input_kind"])
        if input_kind == "auto":
            input_kind = detect_structured_workbook_kind(input_path.name, content)
        if input_kind not in STRUCTURED_INPUT_KINDS:
            raise MeetingIngestionError(f"Unsupported structured input kind: {input_kind}")

        source = source_from_stored_row(source_row)
        source.title = str(job.get("payload", {}).get("source_title") or source.title)
        source.input_kind = input_kind
        source.content_hash = actual_hash
        source.curated_source = SourceRef(
            "Uploaded structured project workbook",
            str(input_path),
            "matched",
            "Human-maintained structured workbook used for deterministic import.",
        )
        source.raw_source = SourceRef(
            "Immutable uploaded workbook",
            str(input_path),
            "matched",
        )
        if input_kind not in source.tags:
            source.tags.append(input_kind)
        self.store.save_source(source, kind=input_kind, materialize=False)

        rows = read_structured_workbook(input_path, input_kind)
        self.store.update_ingestion_job(
            job_id,
            stage="importing_structured_rows",
            total_chunks=len(rows),
            processed_chunks=0,
            payload={"detected_input_kind": input_kind, "source_title": source.title},
        )
        if input_kind == "three_list_tasks":
            counts = self._import_tasks(rows, source, actor, job_id)
        elif input_kind == "three_list_issues":
            counts = self._import_issues(rows, source, actor, job_id)
        else:
            counts = self._import_work_logs(
                rows,
                source,
                actor,
                job_id,
                backfill_records=False,
            )

        self.store.generate_vault_mirror()
        completed = self.store.complete_ingestion_job(
            job_id,
            processed_chunks=len(rows),
            candidate_count=0,
            delta_new=counts["new"],
            delta_updated=counts["updated"],
            delta_conflict=counts["conflict"],
            delta_resolved=counts["resolved"],
            delta_auto_merged=0,
            payload={
                "detected_input_kind": input_kind,
                "row_count": len(rows),
                "imported_count": counts["new"] + counts["updated"],
                "skipped_count": counts["skipped"],
                "import_summary": counts["summary"],
            },
        )
        return {
            "job_id": job_id,
            "source_id": source.doc_id,
            "input_kind": input_kind,
            "status": completed["status"],
            **counts,
        }

    def _import_tasks(
        self,
        rows: list[WorkbookRow],
        source: Any,
        actor: User,
        job_id: str,
    ) -> dict[str, Any]:
        parent_titles = {
            _text(row.values.get("父任务"))
            for row in rows
            if _text(row.values.get("父任务"))
        }
        title_counts = Counter(_text(row.values.get("任务描述")) for row in rows)
        work_items = [
            item
            for item in self.store.list_work_items()
            if item.project_id == source.project_id and item.status != WorkItemStatus.ARCHIVED
        ]
        work_by_title: dict[str, list[Any]] = defaultdict(list)
        work_by_candidate = {item.source_candidate_id: item for item in work_items if item.source_candidate_id}
        for item in work_items:
            work_by_title[item.title].append(item)
        issue_ids_by_title = {
            item.title: item.item_id
            for item in self.store.list_items()
            if item.project_id == source.project_id
            and item.status != CandidateStatus.ARCHIVED
            and item.category.lower() in {"issue", "question", "questions", "open_questions", "问题"}
        }

        counts = _empty_counts()
        for row in rows:
            title = _text(row.values.get("任务描述"))
            if not title:
                counts["skipped"] += 1
                continue
            qualified = _qualified_task(row.values, parent_titles)
            derived_id = _task_item_id(source.project_id, row)
            existing_item = _get_item(self.store, derived_id)
            existing_work = work_by_candidate.get(derived_id)
            if existing_item is None and title_counts[title] == 1 and len(work_by_title.get(title, [])) == 1:
                matched_work = work_by_title[title][0]
                matched_item = _get_item(self.store, matched_work.source_candidate_id)
                if matched_item is not None:
                    existing_item = matched_item
                    existing_work = matched_work
            elif existing_item is None and work_by_title.get(title) and title_counts[title] > 1 and qualified:
                counts["conflict"] += 1

            if existing_item is None and not qualified:
                counts["skipped"] += 1
                continue

            item = existing_item or InspectionItem(
                item_id=derived_id,
                category="task",
                title=title,
                description=title,
                evidence_refs=[],
                status=CandidateStatus.CONFIRMED,
            )
            item.category = "task"
            item.title = title
            item.description = _task_description(row.values, title)
            item.evidence_refs = [_row_evidence(source, row, title, job_id)]
            item.status = CandidateStatus.CONFIRMED
            item.owner_candidates = _owners(row.values.get("责任人"))
            item.next_step = _text(row.values.get("任务状态"))
            item.matter_type = _text(row.values.get("优先级")) or None
            item.facet_types = ["task", "human_source"]
            item.linked_issue_ids = _relation_ids(
                _first_value(row.values, "关联：关联问题", "关联问题"),
                issue_ids_by_title,
            )
            item.due_date = _date_text(row.values.get("截止时间")) or None
            item.acceptance_criteria = _text(row.values.get("可验收标准")) or None
            item.inference_note = f"由 {source.title} 的 {row.sheet_name} 第 {row.row_number} 行确定性导入。"
            _inherit_source_tags(item, source, actor)
            item.confirmation_editor = actor.id
            item.confirmation_notes = "人工维护任务清单导入"
            item.updated_at = _now()
            self.store.save_item(item, materialize=False)

            prior_status = existing_work.status if existing_work is not None else None
            published = self.store.publish_item_as_work_item(
                item.item_id,
                editor=actor.id,
                notes="人工维护任务清单导入",
            )
            next_status = _task_status(row.values.get("任务状态"))
            self.store.update_work_item_fields(
                published.work_item_id,
                status=next_status,
                planned_start=(
                    _date_text(row.values.get("实际开始时间"))
                    or _date_text(row.values.get("创建时间"))
                ),
                progress_percent=_progress_percent(row.values.get("进度"), next_status),
                editor=actor.id,
                notes="按最新任务清单同步状态",
            )
            if existing_work is None:
                counts["new"] += 1
            else:
                counts["updated"] += 1
                if prior_status not in {WorkItemStatus.DONE, WorkItemStatus.CANCELED} and next_status in {"done", "canceled"}:
                    counts["resolved"] += 1

        counts["summary"] = (
            f"任务节点新增 {counts['new']}、更新 {counts['updated']}、"
            f"状态关闭 {counts['resolved']}、跳过非正式节点 {counts['skipped']}。"
        )
        return counts

    def _import_issues(
        self,
        rows: list[WorkbookRow],
        source: Any,
        actor: User,
        job_id: str,
    ) -> dict[str, Any]:
        title_counts = Counter(_text(row.values.get("问题描述")) for row in rows)
        existing_issues = [
            item
            for item in self.store.list_items()
            if item.project_id == source.project_id
            and item.status != CandidateStatus.ARCHIVED
            and item.category.lower() in {"issue", "question", "questions", "open_questions", "问题"}
        ]
        issues_by_title: dict[str, list[InspectionItem]] = defaultdict(list)
        for item in existing_issues:
            issues_by_title[item.title].append(item)
        task_ids_by_title: dict[str, list[str]] = defaultdict(list)
        for work_item in self.store.list_work_items():
            if work_item.project_id == source.project_id and work_item.status != WorkItemStatus.ARCHIVED:
                task_ids_by_title[work_item.title].append(work_item.work_item_id)

        counts = _empty_counts()
        imported: list[tuple[InspectionItem, WorkbookRow]] = []
        for row in rows:
            title = _text(row.values.get("问题描述"))
            if not title:
                counts["skipped"] += 1
                continue
            derived_id = _issue_item_id(source.project_id, title)
            existing_item = _get_item(self.store, derived_id)
            if existing_item is None and title_counts[title] == 1 and len(issues_by_title.get(title, [])) == 1:
                existing_item = issues_by_title[title][0]
            if existing_item is None and issues_by_title.get(title):
                counts["conflict"] += 1
            prior_state = existing_item.next_step if existing_item is not None else ""
            item = existing_item or InspectionItem(
                item_id=derived_id,
                category="issue",
                title=title,
                description=title,
                evidence_refs=[],
                status=CandidateStatus.CONFIRMED,
            )
            item.category = "issue"
            item.title = title
            item.description = _issue_description(row.values, title)
            item.evidence_refs = [_row_evidence(source, row, title, job_id)]
            item.status = CandidateStatus.CONFIRMED
            item.owner_candidates = _owners(row.values.get("责任人"))
            item.next_step = _text(row.values.get("状态"))
            item.matter_type = _text(row.values.get("问题类型")) or None
            item.facet_types = ["problem", "human_source"]
            item.due_date = _date_text(row.values.get("计划解决时间")) or None
            item.linked_task_ids = _relation_ids_many(
                _first_value(row.values, "关联：任务清单", "关联任务"),
                task_ids_by_title,
            )
            item.inference_note = f"由 {source.title} 的 {row.sheet_name} 第 {row.row_number} 行确定性导入；原表状态：{item.next_step or '未填写'}。"
            _inherit_source_tags(item, source, actor)
            item.confirmation_editor = actor.id
            item.confirmation_notes = "人工维护问题清单导入"
            item.updated_at = _now()
            self.store.save_item(item, materialize=False)
            imported.append((item, row))
            if existing_item is None:
                counts["new"] += 1
            else:
                counts["updated"] += 1
                if not _closed_issue_state(prior_state) and _closed_issue_state(item.next_step or ""):
                    counts["resolved"] += 1

        for issue, _row in imported:
            for work_item_id in issue.linked_task_ids or []:
                try:
                    work_item = self.store.get_work_item(work_item_id)
                except KeyError:
                    continue
                linked = list(dict.fromkeys([*(work_item.linked_issue_ids or []), issue.item_id]))
                self.store.update_work_item_fields(
                    work_item_id,
                    linked_issue_ids=linked,
                    editor=actor.id,
                    notes="按问题清单同步关联",
                )

        counts["summary"] = (
            f"问题新增 {counts['new']}、更新 {counts['updated']}、"
            f"状态关闭 {counts['resolved']}、关联冲突 {counts['conflict']}。"
        )
        return counts

    def _import_work_logs(
        self,
        rows: list[WorkbookRow],
        source: Any,
        actor: User,
        job_id: str,
        *,
        backfill_records: bool,
    ) -> dict[str, Any]:
        groups: dict[tuple[str, str], list[WorkbookRow]] = defaultdict(list)
        skipped = 0
        for row in rows:
            report_date = _date_text(row.values.get("日期"))
            content = _text(row.values.get("工作内容"))
            blocked = _text(row.values.get("卡点问题"))
            deliverable = _text(row.values.get("交付物"))
            if deliverable == "无":
                deliverable = ""
            if not report_date or not any((content, blocked, deliverable)):
                skipped += 1
                continue
            board = _text(row.values.get("板块")) or "未分板块"
            groups[(report_date, board)].append(row)

        counts = _empty_counts()
        counts["skipped"] = skipped
        record_entries: list[dict[str, Any]] = []
        for (report_date, board), grouped_rows in sorted(groups.items()):
            item_id = _work_log_item_id(source.project_id, report_date, board)
            existing_item = _get_item(self.store, item_id)
            owners = list(dict.fromkeys(
                owner
                for row in grouped_rows
                for owner in _owners(row.values.get("姓名"))
            ))
            item = existing_item or InspectionItem(
                item_id=item_id,
                category="activity",
                title=f"{report_date} {board} 工作日志",
                description="",
                evidence_refs=[],
                status=CandidateStatus.CONFIRMED,
            )
            item.category = "activity"
            item.title = f"{report_date} {board} 工作日志"
            item.description = "\n".join(_work_log_line(row.values) for row in grouped_rows)
            item.evidence_refs = [
                _row_evidence(
                    source,
                    row,
                    (
                        _text(row.values.get("工作内容"))
                        or _text(row.values.get("卡点问题"))
                        or _text(row.values.get("交付物"))
                    )[:300],
                    job_id,
                )
                for row in grouped_rows
            ]
            item.status = CandidateStatus.CONFIRMED
            item.owner_candidates = owners
            item.facet_types = ["work_log", "human_source"]
            item.effective_at = report_date
            item.inference_note = f"{len(grouped_rows)} 条人工工作日志按日期和板块确定性聚合；未发布为任务。"
            _inherit_source_tags(item, source, actor)
            item.confirmation_editor = actor.id
            item.confirmation_notes = "人工工作日志聚合导入"
            item.updated_at = _now()
            self.store.save_item(item, materialize=False)
            if existing_item is None:
                counts["new"] += 1
            else:
                counts["updated"] += 1

            if not backfill_records:
                continue
            for row in grouped_rows:
                person_name = _text(row.values.get("姓名")) or "未填写人员"
                subject_user_id = self.store.exact_project_user_id_by_name(
                    source.project_id,
                    person_name,
                )
                base = {
                    "subject_user_id": subject_user_id,
                    "subject_name": person_name,
                    "record_date": report_date,
                    "author_id": source.author_id or actor.id,
                    "solution_options": [],
                }
                content = _text(row.values.get("工作内容"))
                blocked = _text(row.values.get("卡点问题"))
                deliverable = _text(row.values.get("交付物"))
                for kind, text in (
                    ("work", content),
                    ("problem", blocked),
                    ("output", deliverable if deliverable != "无" else ""),
                ):
                    if not text:
                        continue
                    locator = f"{row.sheet_name}!A{row.row_number}"
                    record_entries.append({
                        **base,
                        "kind": kind,
                        "text": text,
                        "evidence_refs": [{
                            "source_id": source.doc_id,
                            "source_kind": source.input_kind,
                            "locator": locator,
                            "quote": text[:300],
                            "evidence_level": "human_source",
                            "ingestion_job_id": job_id,
                        }],
                        "source_locator": locator,
                    })

        if backfill_records:
            self.store.replace_daily_work_records(
                series_id=source.doc_id,
                source_id=source.doc_id,
                source_origin="historical_excel_backfill",
                org_id=source.org_id,
                project_id=source.project_id,
                topic_id=source.topic_id,
                author_id=source.author_id or actor.id,
                sensitivity=source.sensitivity,
                subject_user_id="",
                subject_name="历史日志人员",
                record_date=min((key[0] for key in groups), default=date.today().isoformat()),
                entries=record_entries,
            )

        suffix = (
            f"回填 {len(record_entries)} 条结构化日报记录"
            if backfill_records
            else "未执行历史日报回填"
        )
        counts["summary"] = (
            f"工作日志 {len(rows)} 行聚合为 {len(groups)} 条日期/板块记忆，"
            f"{suffix}，未生成任何待办。"
        )
        counts["work_record_count"] = len(record_entries)
        return counts

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


def _qualified_task(values: dict[str, Any], parent_titles: set[str]) -> bool:
    title = _text(values.get("任务描述"))
    status = _text(values.get("任务状态"))
    return bool(
        title
        and title not in parent_titles
        and status not in {"已完成", "已取消", "取消"}
        and _text(values.get("优先级")) in {"重要", "非常重要"}
        and _owners(values.get("责任人"))
        and any(
            _text(values.get(field))
            for field in ("截止时间", "可验收标准", "任务项目详细说明")
        )
    )


def _task_description(values: dict[str, Any], title: str) -> str:
    parent = _text(values.get("父任务"))
    parent2 = _text(values.get("父任务2"))
    parts = [
        _text(values.get("任务项目详细说明")),
        _text(values.get("备注")),
        f"父任务：{parent}" if parent else "",
        f"上级层级：{parent2}" if parent2 else "",
    ]
    return "；".join(part for part in parts if part) or title


def _issue_description(values: dict[str, Any], title: str) -> str:
    parts = [
        _text(values.get("当前的困难是什么")),
        _text(values.get("备注")),
        f"来源：{_text(values.get('问题来源'))}" if _text(values.get("问题来源")) else "",
        f"层级：{_text(values.get('问题层级'))}" if _text(values.get("问题层级")) else "",
    ]
    return "；".join(part for part in parts if part) or title


def _work_log_line(values: dict[str, Any]) -> str:
    person = _text(values.get("姓名")) or "未填写人员"
    work_type = _text(values.get("工作类型"))
    content = _text(values.get("工作内容"))
    hours = _text(values.get("工作时长"))
    blocked = _text(values.get("卡点问题"))
    deliverable = _text(values.get("交付物"))
    suffix = "；".join(
        part
        for part in (
            f"类型：{work_type}" if work_type else "",
            f"工时：{hours}" if hours else "",
            f"卡点：{blocked}" if blocked else "",
            f"交付物：{deliverable}" if deliverable and deliverable != "无" else "",
        )
        if part
    )
    return f"- {person}：{content}" + (f"（{suffix}）" if suffix else "")


def _row_evidence(source: Any, row: WorkbookRow, quote: str, job_id: str) -> EvidenceRef:
    locator = f"{row.sheet_name}!A{row.row_number}"
    return EvidenceRef(
        source_doc_id=source.doc_id,
        source_kind=source.input_kind,
        locator=locator,
        quote=quote or "结构化表格记录",
        raw_source_doc_id=source.doc_id,
        raw_locator=locator,
        evidence_level="human_source",
        ingestion_job_id=job_id,
    )


def _inherit_source_tags(item: InspectionItem, source: Any, actor: User) -> None:
    item.org_id = source.org_id
    item.project_id = source.project_id
    item.topic_id = source.topic_id
    item.author_id = source.author_id or actor.id
    item.sensitivity = source.sensitivity
    item.tag_origin = source.tag_origin


def _task_item_id(project_id: str, row: WorkbookRow) -> str:
    values = row.values
    created_at = _datetime_text(values.get("创建时间"))
    identity = "\0".join([
        project_id,
        _text(values.get("任务描述")),
        _text(values.get("父任务")),
        _text(values.get("父任务2")),
        created_at or _text(values.get("责任人")) or f"{row.sheet_name}:{row.row_number}",
    ])
    return f"import_task_{hashlib.sha1(identity.encode('utf-8')).hexdigest()[:16]}"


def _issue_item_id(project_id: str, title: str) -> str:
    digest = hashlib.sha1(f"{project_id}\0{title}".encode("utf-8")).hexdigest()[:16]
    return f"import_issue_{digest}"


def _work_log_item_id(project_id: str, report_date: str, board: str) -> str:
    digest = hashlib.sha1(f"{project_id}\0{report_date}\0{board}".encode("utf-8")).hexdigest()[:16]
    return f"work_log_{digest}"


def _task_status(value: Any) -> str:
    status = _text(value).lower()
    if status in {"已完成", "完成", "done", "completed"}:
        return "done"
    if status in {"已取消", "取消", "canceled", "cancelled"}:
        return "canceled"
    if status in {"进行中", "处理中", "in_progress", "doing"}:
        return "in_progress"
    if status in {"阻塞", "已阻塞", "暂停", "blocked"}:
        return "blocked"
    return "open"


def _progress_percent(value: Any, status: str) -> int | None:
    if value in (None, ""):
        return 100 if status == "done" else None
    try:
        number = float(str(value).strip().rstrip("%"))
    except ValueError:
        return 100 if status == "done" else None
    if isinstance(value, str) and value.strip().endswith("%"):
        return max(0, min(100, round(number)))
    if 0 <= number <= 1:
        number *= 100
    return max(0, min(100, round(number)))


def _owners(value: Any) -> list[str]:
    owners: list[str] = []
    for raw in re.split(r"[,，、;/；\n]+", _text(value)):
        name = raw.strip()
        if "-" in name:
            name = name.split("-", 1)[0].strip()
        if name and name not in owners:
            owners.append(name)
    return owners


def _relation_ids(value: Any, ids_by_title: dict[str, str]) -> list[str]:
    return [
        ids_by_title[title]
        for title in _split_relations(value)
        if title in ids_by_title
    ]


def _relation_ids_many(value: Any, ids_by_title: dict[str, list[str]]) -> list[str]:
    return list(dict.fromkeys(
        item_id
        for title in _split_relations(value)
        for item_id in ids_by_title.get(title, [])
    ))


def _split_relations(value: Any) -> list[str]:
    return list(dict.fromkeys(
        part.strip()
        for part in re.split(r"[,，;；\n]+", _text(value))
        if part.strip()
    ))


def _first_value(values: dict[str, Any], *fields: str) -> Any:
    for field in fields:
        value = values.get(field)
        if value not in (None, ""):
            return value
    return ""


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _text(value)
    match = re.search(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", text)
    if not match:
        return ""
    return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def _datetime_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return _text(value)


def _closed_issue_state(value: str) -> bool:
    return _text(value).lower() in {"已解决", "已关闭", "完成", "done", "closed", "resolved"}


def _get_item(store: ProjectSQLiteStore, item_id: str) -> InspectionItem | None:
    if not item_id:
        return None
    try:
        return store.get_item(item_id)
    except KeyError:
        return None


def _empty_counts() -> dict[str, Any]:
    return {"new": 0, "updated": 0, "conflict": 0, "resolved": 0, "skipped": 0, "summary": ""}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
