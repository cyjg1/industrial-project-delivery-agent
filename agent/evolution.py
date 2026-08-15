from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import Any

from agent.access_policy import AccessContext, User, visible_filter
from agent.schemas import CandidateStatus, InspectionItem, WorkItem, WorkItemStatus, to_plain
from store.sqlite_store import ProjectSQLiteStore


def search_confirmed_methods(
    store: ProjectSQLiteStore,
    query: str,
    limit: int = 5,
    *,
    project_id: str | None = None,
    actor: User | None = None,
    access_context: AccessContext | None = None,
) -> dict[str, Any]:
    filters = {"type": "method", "status": CandidateStatus.CONFIRMED.value}
    if project_id:
        filters["project_id"] = project_id
    rows = store.search_memory(
        query,
        filters,
        limit=limit,
        actor=actor,
        access_context=access_context,
    )
    return {
        "summary": f"检索到 {len(rows)} 条已确认方法。",
        "items": rows,
    }


def build_rejection_prompt_context(
    store: ProjectSQLiteStore,
    candidate_type: str | None = None,
    limit_per_type: int = 3,
    *,
    project_id: str | None = None,
    actor: User | None = None,
    access_context: AccessContext | None = None,
) -> dict[str, Any]:
    rejected = store.list_items(status=CandidateStatus.REJECTED)
    if project_id is not None:
        rejected = [item for item in rejected if item.project_id == project_id]
    if actor is not None and access_context is not None:
        rejected = visible_filter(actor, rejected, access_context)
    groups: dict[str, list[InspectionItem]] = defaultdict(list)
    for item in rejected:
        item_type = _item_kind(item)
        if candidate_type and item_type != candidate_type:
            continue
        groups[item_type].append(item)
    if not groups:
        return {"prompt_note": "", "groups": {}}

    lines = ["此前同类判断被驳回，生成新候选前必须吸收这些原因，避免重复犯错："]
    group_payload: dict[str, Any] = {}
    for item_type, items in sorted(groups.items()):
        reasons = _top_rejection_reasons(items, limit_per_type)
        group_payload[item_type] = {
            "count": len(items),
            "reasons": reasons,
            "sample_item_ids": [item.item_id for item in items[:limit_per_type]],
        }
        lines.append(f"- {item_type}：{len(items)} 条；原因摘要：{'；'.join(reasons) or '未记录原因'}")
    return {"prompt_note": "\n".join(lines), "groups": group_payload}


def build_team_profiles(
    store: ProjectSQLiteStore,
    *,
    persist: bool = True,
    work_items: list[WorkItem] | None = None,
) -> dict[str, dict[str, Any]]:
    use_candidate_details = work_items is None
    tasks = [
        task
        for task in (work_items if work_items is not None else store.list_work_items())
        if task.status != WorkItemStatus.ARCHIVED
    ]
    events = store.list_events(entity="task")
    by_owner: dict[str, list[WorkItem]] = defaultdict(list)
    for task in tasks:
        owners = [owner for owner in task.owner_candidates or [] if owner] or ["责任人待确认"]
        for owner in owners:
            by_owner[owner].append(task)

    profiles: dict[str, dict[str, Any]] = {}
    for owner, owner_tasks in sorted(by_owner.items()):
        completed = [task for task in owner_tasks if task.status == WorkItemStatus.DONE]
        active = [task for task in owner_tasks if task.status not in {WorkItemStatus.DONE, WorkItemStatus.CANCELED}]
        profile = {
            "name": owner,
            "common_domains": _common_domains(
                store,
                owner_tasks,
                include_candidate_details=use_candidate_details,
            ),
            "avg_completion_days": _average_completion_days(completed),
            "current_load": len(active),
            "completed_count": len(completed),
            "total_task_count": len(owner_tasks),
            "active_task_titles": [task.title for task in active[:8]],
            "event_count": len([event for event in events if event.get("entity_id") in {task.work_item_id for task in owner_tasks}]),
            "data_source": "任务事件统计：tasks + events，不是主观评价。",
            "updated_at": _now(),
        }
        profiles[owner] = profile
        if persist:
            store.save_people_profile(owner, profile)
    return profiles


def mark_meeting_continuation(
    store: ProjectSQLiteStore,
    *,
    previous_source_id: str,
    next_source_id: str,
    editor: str = "local_user",
    notes: str = "",
) -> dict[str, Any]:
    previous_source = store.get_source(previous_source_id)
    next_source = store.get_source(next_source_id)
    if previous_source is None:
        raise KeyError(previous_source_id)
    if next_source is None:
        raise KeyError(next_source_id)
    payload = {
        "previous_source_id": previous_source_id,
        "next_source_id": next_source_id,
        "editor": editor,
        "notes": notes,
        "created_at": _now(),
    }
    store.append_event("source", next_source_id, "meeting_continuation_marked", payload)
    return {
        "previous_source": previous_source,
        "next_source": next_source,
        "event": payload,
    }


def build_meeting_followup_comparison(
    store: ProjectSQLiteStore,
    *,
    next_source_id: str,
    project_id: str | None = None,
    actor: User | None = None,
    access_context: AccessContext | None = None,
) -> dict[str, Any]:
    next_source = store.get_source(next_source_id)
    if next_source is None:
        raise KeyError(next_source_id)
    previous_source = _previous_continuation_source(
        store,
        next_source,
        project_id=project_id,
        actor=actor,
        access_context=access_context,
    )
    rows = (
        _meeting_task_rows(
            store,
            previous_source["id"],
            project_id=project_id,
            actor=actor,
            access_context=access_context,
        )
        if previous_source
        else []
    )
    return {
        "next_source": next_source,
        "previous_source": previous_source or {},
        "rows": rows,
        "summary": f"找到 {len(rows)} 条上次会议待办用于会前对照。" if previous_source else "未找到可对照的上次会议。",
    }


def generate_weekly_review(
    store: ProjectSQLiteStore,
    *,
    week_end: date | None = None,
    project_id: str | None = None,
    actor: User | None = None,
    access_context: AccessContext | None = None,
) -> dict[str, Any]:
    active_week_end = week_end or date.today()
    tasks = [
        task
        for task in store.list_work_items()
        if project_id is None or task.project_id == project_id
    ]
    items = [
        item
        for item in store.list_items()
        if project_id is None or item.project_id == project_id
    ]
    if actor is not None and access_context is not None:
        tasks = visible_filter(actor, tasks, access_context)
        items = visible_filter(actor, items, access_context)
    solved = [task for task in tasks if task.status == WorkItemStatus.DONE]
    new_methods = [
        item
        for item in items
        if item.status == CandidateStatus.CONFIRMED and _item_kind(item) == "method"
    ]
    stale_issues = [
        item
        for item in items
        if item.status == CandidateStatus.CANDIDATE
        and _item_kind(item) == "issue"
        and _days_since(item.updated_at, active_week_end) >= 14
    ]
    focus = _next_week_focus(tasks, items)
    content = _weekly_review_markdown(active_week_end, solved, new_methods, stale_issues, focus)
    source_id = (
        f"weekly_review_{project_id}_{active_week_end.strftime('%Y%m%d')}"
        if project_id
        else f"weekly_review_{active_week_end.strftime('%Y%m%d')}"
    )
    project = store.get_project(project_id) if project_id else None
    store.save_source(
        {
            "doc_id": source_id,
            "title": f"{active_week_end.isoformat()} 本周复盘",
            "meeting_date": active_week_end.isoformat(),
            "topic": "项目周复盘",
            "content_markdown": content,
            "sections": {
                "solved": [task.work_item_id for task in solved],
                "new_methods": [item.item_id for item in new_methods],
                "stale_issues": [item.item_id for item in stale_issues],
                "next_week_focus": focus,
            },
            "generated_at": _now(),
            "org_id": project["org_id"] if project else "org_mvp",
            "project_id": project_id or "project_mvp",
            "topic_id": None,
            "author_id": actor.id if actor is not None else (project["owner_id"] if project else "system"),
            "sensitivity": "l3" if project_id else "l1",
            "tag_origin": "system_generated" if project_id else "runtime_default",
        },
        kind="weekly_review",
    )
    source = store.get_source(source_id) or {"id": source_id, "kind": "weekly_review"}
    return {
        "weekly_review_id": source_id,
        "week_end": active_week_end.isoformat(),
        "content_markdown": content,
        "source": source,
    }


def _top_rejection_reasons(items: list[InspectionItem], limit: int) -> list[str]:
    reasons = [
        " ".join((item.confirmation_notes or "").split())[:80]
        for item in items
        if (item.confirmation_notes or "").strip()
    ]
    if not reasons:
        reasons = [item.title for item in items[:limit]]
    counter = Counter(reasons)
    return [reason for reason, _count in counter.most_common(limit)]


def _common_domains(
    store: ProjectSQLiteStore,
    tasks: list[WorkItem],
    *,
    include_candidate_details: bool = True,
) -> list[str]:
    words: list[str] = []
    for task in tasks:
        candidate = _safe_get_item(store, task.source_candidate_id) if include_candidate_details else None
        if candidate:
            words.append(_item_kind(candidate))
            words.extend(_keyword_chunks(candidate.title))
        words.extend(_keyword_chunks(task.title))
    counter = Counter(word for word in words if word)
    return [word for word, _count in counter.most_common(5)] or ["未形成稳定领域"]


def _average_completion_days(tasks: list[WorkItem]) -> float | None:
    durations: list[int] = []
    for task in tasks:
        created = _parse_datetime(task.created_at)
        updated = _parse_datetime(task.updated_at)
        if created and updated:
            durations.append(max((updated - created).days, 0))
    if not durations:
        return None
    return round(sum(durations) / len(durations), 1)


def _previous_continuation_source(
    store: ProjectSQLiteStore,
    next_source: dict[str, Any],
    *,
    project_id: str | None,
    actor: User | None,
    access_context: AccessContext | None,
) -> dict[str, Any] | None:
    events = store.list_events(entity="source", entity_id=next_source["id"], action="meeting_continuation_marked")
    if events:
        previous_id = events[-1]["payload"].get("previous_source_id", "")
        previous = store.get_source(previous_id)
        if previous and _source_in_scope(previous, project_id, actor, access_context):
            return previous

    next_title = next_source.get("title") or ""
    next_date = next_source.get("meeting_date") or ""
    candidates = [
        source
        for source in store.list_sources()
        if source["id"] != next_source["id"]
        and _source_in_scope(source, project_id, actor, access_context)
        and source.get("kind") in {"minutes", "transcript"}
        and _similar_title(next_title, source.get("title") or "")
        and (not next_date or (source.get("meeting_date") or "") < next_date)
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item.get("meeting_date") or "", item["id"]))[-1]


def _meeting_task_rows(
    store: ProjectSQLiteStore,
    source_id: str,
    *,
    project_id: str | None,
    actor: User | None,
    access_context: AccessContext | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    tasks = [
        task
        for task in store.list_work_items()
        if project_id is None or task.project_id == project_id
    ]
    if actor is not None and access_context is not None:
        tasks = visible_filter(actor, tasks, access_context)
    for task in tasks:
        if not any(ref.source_doc_id == source_id for ref in task.evidence_refs):
            continue
        rows.append({
            "work_item_id": task.work_item_id,
            "title": task.title,
            "status": task.status.value,
            "owners": task.owner_candidates or [],
            "due_date": task.due_date or "",
            "deliverable": task.deliverable or "",
            "acceptance_criteria": task.acceptance_criteria or "",
            "source_candidate_id": task.source_candidate_id,
            "evidence_refs": [to_plain(ref) for ref in task.evidence_refs],
            "org_id": task.org_id,
            "project_id": task.project_id,
            "topic_id": task.topic_id,
            "author_id": task.author_id,
            "sensitivity": task.sensitivity,
        })
    return rows


def _source_in_scope(
    source: dict[str, Any],
    project_id: str | None,
    actor: User | None,
    access_context: AccessContext | None,
) -> bool:
    if project_id is not None and source.get("project_id") != project_id:
        return False
    if actor is not None and access_context is not None:
        return bool(visible_filter(actor, [source], access_context))
    return True


def _weekly_review_markdown(
    week_end: date,
    solved: list[WorkItem],
    methods: list[InspectionItem],
    stale_issues: list[InspectionItem],
    focus: list[str],
) -> str:
    lines = [f"# {week_end.isoformat()} 本周复盘", ""]
    lines.extend(_section("解决了什么问题", [task.title for task in solved]))
    lines.extend(_section("新增什么方法", [item.title for item in methods]))
    lines.extend(_section("拖了两周以上的问题", [item.title for item in stale_issues]))
    lines.extend(_section("下周建议焦点", focus))
    return "\n".join(lines).strip() + "\n"


def _section(title: str, values: list[str]) -> list[str]:
    lines = [f"## {title}"]
    if not values:
        lines.append("- 暂无")
    else:
        lines.extend(f"- {value}" for value in values[:10])
    lines.append("")
    return lines


def _next_week_focus(tasks: list[WorkItem], items: list[InspectionItem]) -> list[str]:
    overdue_or_open = [
        task.title
        for task in tasks
        if task.status not in {WorkItemStatus.DONE, WorkItemStatus.CANCELED}
    ]
    candidate_issues = [
        item.title
        for item in items
        if item.status == CandidateStatus.CANDIDATE and _item_kind(item) == "issue"
    ]
    focus = overdue_or_open[:3] + candidate_issues[:3]
    return focus or ["保持项目记忆与任务池更新，等待新会议材料进入。"]


def _safe_get_item(store: ProjectSQLiteStore, item_id: str) -> InspectionItem | None:
    if not item_id:
        return None
    try:
        return store.get_item(item_id)
    except KeyError:
        return None


def _item_kind(item: InspectionItem) -> str:
    category = (item.category or "").lower()
    if category in {"methods", "method", "方法", "法"}:
        return "method"
    if category in {"followup", "task", "tasks", "todo", "待办"}:
        return "todo"
    if category in {"issue", "open_questions", "question", "问题", "链路", "责任"}:
        return "issue"
    if category in {"people", "person", "人员", "人"}:
        return "person"
    return "thing"


def _keyword_chunks(value: str) -> list[str]:
    cleaned = " ".join((value or "").replace("-", " ").replace("_", " ").split())
    if not cleaned:
        return []
    tokens = [token for token in cleaned.split(" ") if len(token) >= 2]
    if tokens:
        return tokens[:4]
    return [cleaned[:8]]


def _similar_title(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right or left in right or right in left:
        return True
    left_chars = {char for char in left if char.strip()}
    right_chars = {char for char in right if char.strip()}
    if not left_chars or not right_chars:
        return False
    return len(left_chars & right_chars) / max(len(left_chars | right_chars), 1) >= 0.55


def _days_since(value: str, today: date) -> int:
    parsed = _parse_datetime(value)
    if not parsed:
        return 999
    return (today - parsed.date()).days


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
