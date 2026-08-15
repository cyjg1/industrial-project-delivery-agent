from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
import re
from typing import Any, Iterable

from agent.schemas import InspectionItem, PeopleStructure, Person, WorkItem


PROFESSIONS = (
    ("product_design", "产品/需求设计"),
    ("software_development", "软件开发"),
    ("data_integration", "数据与接口"),
    ("testing_uat", "测试/UAT"),
    ("implementation_go_live", "实施上线"),
)

BOARDS = (
    "原料", "铁区", "炼钢", "热轧", "冷轧", "仓储", "物流", "质量", "能源", "设备",
    "安全", "环保", "生产实绩", "计划", "成本",
)

PEOPLE_GROUPS = ("PMO", "专业统筹", *BOARDS)
STATUS_LABELS = {
    "candidate": "待确认",
    "open": "待开始",
    "in_progress": "进行中",
    "pending_acceptance": "待验收",
    "done": "已完成",
    "canceled": "已取消",
}

def build_progress_dashboard(
    work_items: Iterable[WorkItem],
    inspection_items: Iterable[InspectionItem],
    people: PeopleStructure,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    active_today = today or date.today()
    active_work_items = [
        item
        for item in work_items
        if item.status.value not in {"archived", "canceled"}
    ]
    published_candidate_ids = {
        item.source_candidate_id
        for item in active_work_items
        if item.source_candidate_id
    }
    candidate_tasks = [
        item
        for item in inspection_items
        if item.status.value == "candidate"
        and _item_kind(item) == "task"
        and item.item_id not in published_candidate_ids
    ]
    owner_boards = _owner_board_index(people)
    classified_formal_tasks = [
        _classify_task(item, active_today, owner_boards)
        for item in active_work_items
    ]
    classified_candidate_tasks = [
        _classify_candidate_task(item, active_today, owner_boards)
        for item in candidate_tasks
    ]
    classified_tasks = [*classified_formal_tasks, *classified_candidate_tasks]
    task_boards = {item["task_id"]: item["board_id"] for item in classified_tasks if item["board_id"]}
    classified_items = [
        (board_id, item)
        for item in inspection_items
        if item.status.value != "archived"
        for board_id in _inspection_board_ids(item, task_boards)
    ]

    professions = []
    for profession_id, profession_name in PROFESSIONS:
        profession_tasks = [item for item in classified_tasks if item["professional_id"] == profession_id]
        board_rows = [
            _task_group_summary(board, [item for item in profession_tasks if item["board_id"] == board])
            for board in BOARDS
        ]
        professions.append({
            "id": profession_id,
            "name": profession_name,
            **_task_group_summary(profession_id, profession_tasks, include_key=False),
            "boards": [row for row in board_rows if row["task_count"]],
            "time_nodes": _time_nodes(profession_tasks),
        })

    boards = []
    for board in BOARDS:
        board_tasks = [item for item in classified_tasks if item["board_id"] == board]
        formal_board_tasks = [item for item in classified_formal_tasks if item["board_id"] == board]
        profession_rows = [
            _task_group_summary(
                profession_id,
                [item for item in board_tasks if item["professional_id"] == profession_id],
                label=profession_name,
            )
            for profession_id, profession_name in PROFESSIONS
        ]
        boards.append({
            "id": board,
            "name": board,
            **_task_group_summary(board, board_tasks, include_key=False),
            "professions": profession_rows,
            "three_lists": _three_list_summary(
                [item for item_board, item in classified_items if item_board == board],
                formal_board_tasks,
            ),
            "time_nodes": _time_nodes(board_tasks),
        })

    owner_names = sorted({
        owner
        for item in classified_tasks
        for owner in item["owner_names"]
    })
    owners = [
        _task_group_summary(owner, [item for item in classified_tasks if owner in item["owner_names"]], label=owner)
        for owner in owner_names
    ]
    statuses = [
        _task_group_summary(
            status,
            [item for item in classified_tasks if item["status"] == status],
            label=label,
        )
        for status, label in STATUS_LABELS.items()
        if any(item["status"] == status for item in classified_tasks)
    ]
    source_batch_ids = sorted({
        item["source_batch_id"]
        for item in classified_tasks
        if item["source_batch_id"]
    })
    source_batches = [
        _task_group_summary(
            batch_id,
            [item for item in classified_tasks if item["source_batch_id"] == batch_id],
            label=_source_batch_name(batch_id),
        )
        for batch_id in source_batch_ids
    ]
    unclassified_profession = [item for item in classified_tasks if not item["professional_id"]]
    unclassified_board = [item for item in classified_tasks if not item["board_id"]]
    return {
        "summary": {
            "task_count": len(classified_tasks),
            "profession_count": len(PROFESSIONS),
            "board_count": len(BOARDS),
            "owner_count": len(owners),
            "status_count": len(statuses),
            "source_batch_count": len(source_batches),
            "unclassified_profession_count": len(unclassified_profession),
            "unclassified_board_count": len(unclassified_board),
            "missing_progress_count": sum(1 for item in classified_tasks if item["missing_progress"]),
            "updated_at": max((item["updated_at"] for item in classified_tasks), default=""),
        },
        "professions": professions,
        "boards": boards,
        "owners": owners,
        "statuses": statuses,
        "source_batches": source_batches,
        "legend": {
            "green": "按计划或已完成",
            "yellow": "临近节点、轻微滞后或信息不完整",
            "red": "已逾期、阻塞或实际进度明显落后",
            "gray": "暂无任务或缺少计划数据",
        },
    }


def classify_person_group(person: Person) -> str:
    group = str(person.group or "").strip()
    if group.upper() == "PMO":
        return "PMO"
    if group in {"专业统筹", "总体组"}:
        return "专业统筹"
    if group in BOARDS:
        return group
    normalized = re.sub(r"^\d+\s*[-－]\s*", "", group).strip()
    if normalized.endswith("组"):
        normalized = normalized[:-1].strip()
    if normalized in BOARDS:
        return normalized
    return "待归类"


def _classify_task(
    item: WorkItem,
    today: date,
    owner_boards: dict[str, set[str]],
) -> dict[str, Any]:
    owners = item.owner_candidates or []
    source_batch_id = _source_batch_id(item.evidence_refs, item.confirmation_notes)
    professional_id, board_id, classification_source = _classification_fields(
        title=item.title,
        description=item.description,
        deliverable=item.deliverable or "",
        explicit_profession=item.professional_id,
        explicit_board=item.board_id,
        owners=owners,
        owner_boards=owner_boards,
    )
    missing_progress = item.progress_percent is None and item.status.value != "canceled"
    actual = _task_progress(item)
    planned = _planned_progress(item, today)
    due = _parse_date(item.due_date)
    overdue = bool(due and due < today and item.status.value not in {"done", "canceled"})
    missing_plan = not item.due_date
    health = _task_health(actual, planned, overdue, missing_plan, missing_progress, item.status.value)
    return {
        "task_id": item.work_item_id,
        "title": item.title,
        "professional_id": professional_id,
        "board_id": board_id,
        "classification_source": classification_source,
        "progress": actual,
        "planned_progress": planned,
        "health": health,
        "status": item.status.value,
        "due_date": item.due_date or "",
        "overdue": overdue,
        "missing_plan": missing_plan,
        "missing_progress": missing_progress,
        "missing_owner": not bool(owners),
        "owner_names": owners,
        "owner_text": "、".join(owners) or "责任人待确认",
        "source_batch_id": source_batch_id,
        "updated_at": item.status_updated_at or item.updated_at or "",
    }


def _classify_candidate_task(
    item: InspectionItem,
    today: date,
    owner_boards: dict[str, set[str]],
) -> dict[str, Any]:
    owners = item.owner_candidates or []
    professional_id, board_id, classification_source = _classification_fields(
        title=item.title,
        description=item.description,
        deliverable=item.deliverable or "",
        explicit_profession=item.professional_id,
        explicit_board=item.board_id,
        owners=owners,
        owner_boards=owner_boards,
    )
    due = _parse_date(item.due_date)
    overdue = bool(due and due < today)
    planned = _planned_progress_values(_parse_date(item.updated_at), due, today)
    return {
        "task_id": item.item_id,
        "title": item.title,
        "professional_id": professional_id,
        "board_id": board_id,
        "classification_source": classification_source,
        "progress": 0,
        "planned_progress": planned,
        "health": _task_health(0, planned, overdue, not bool(item.due_date), True, "candidate"),
        "status": "candidate",
        "due_date": item.due_date or "",
        "overdue": overdue,
        "missing_plan": not bool(item.due_date),
        "missing_progress": True,
        "missing_owner": not bool(owners),
        "owner_names": owners,
        "owner_text": "、".join(owners) or "负责人待确认",
        "source_batch_id": _source_batch_id(item.evidence_refs, item.confirmation_notes),
        "updated_at": item.updated_at or "",
    }


def _classification_fields(
    *,
    title: str,
    description: str,
    deliverable: str,
    explicit_profession: str,
    explicit_board: str,
    owners: list[str],
    owner_boards: dict[str, set[str]],
) -> tuple[str, str, str]:
    profession_ids = {profession_id for profession_id, _ in PROFESSIONS}
    professional_id = explicit_profession if explicit_profession in profession_ids else ""
    board_id = explicit_board if explicit_board in BOARDS else ""
    inferred_profession = False
    inferred_board = False
    if not professional_id:
        professional_id = infer_task_profession(title) or infer_task_profession(" ".join((description, deliverable)))
        inferred_profession = bool(professional_id)
    if not board_id:
        board_id = next((board for board in BOARDS if title.strip().startswith(board)), "")
        if not board_id:
            candidate_boards = {
                board
                for owner in owners
                for board in owner_boards.get(owner, set())
            }
            board_id = next(iter(candidate_boards)) if len(candidate_boards) == 1 else ""
        inferred_board = bool(board_id)
    if inferred_profession or inferred_board:
        source = "inferred"
        if explicit_profession in profession_ids or explicit_board in BOARDS:
            source = "explicit+inferred"
    elif professional_id or board_id:
        source = "explicit"
    else:
        source = "unclassified"
    return professional_id, board_id, source


def infer_task_profession(text: str) -> str:
    normalized = str(text or "").upper()
    rules = (
        ("testing_uat", ("UAT", "测试", "验收", "验证", "试运行")),
        ("data_integration", ("接口", "主题域", "字段", "主键", "数据", "编码", "数值")),
        ("software_development", ("开发", "改造", "重构", "技术设计", "程序")),
        ("implementation_go_live", ("上线", "投产", "部署", "切换", "应急", "周计划", "问题清单", "功能状态", "技术就绪")),
        ("product_design", ("需求", "业务", "方案", "功能", "清单", "口径", "闭环", "送审", "模板")),
    )
    return next(
        (profession_id for profession_id, keywords in rules if any(keyword in normalized for keyword in keywords)),
        "",
    )


def _owner_board_index(people: PeopleStructure) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for person in people.people:
        group = classify_person_group(person)
        if group in BOARDS:
            result[person.name].add(group)
    return result


def _source_batch_id(evidence_refs: Iterable[Any], confirmation_notes: str) -> str:
    source_ids = list(dict.fromkeys(
        str(ref.source_doc_id)
        for ref in evidence_refs or []
        if str(ref.source_doc_id or "").strip()
    ))
    source_batch_id = next(
        (source_id for source_id in source_ids if "meeting" in source_id.lower()),
        "",
    )
    if source_batch_id:
        return source_batch_id
    meeting_date = re.search(
        r"meeting\s+(\d{4})-(\d{2})-(\d{2})",
        confirmation_notes or "",
        flags=re.IGNORECASE,
    )
    if meeting_date:
        return f"meeting-{''.join(meeting_date.groups())}"
    return source_ids[0] if source_ids else ""


def _task_group_summary(
    key: str,
    tasks: list[dict[str, Any]],
    *,
    label: str = "",
    include_key: bool = True,
) -> dict[str, Any]:
    task_count = len(tasks)
    progress = round(sum(item["progress"] for item in tasks) / task_count) if task_count else 0
    planned = round(sum(item["planned_progress"] for item in tasks) / task_count) if task_count else 0
    health = _worst_health([item["health"] for item in tasks])
    result = {
        "task_count": task_count,
        "progress": progress,
        "planned_progress": planned,
        "health": health,
        "overdue_count": sum(1 for item in tasks if item["overdue"]),
        "missing_plan_count": sum(1 for item in tasks if item["missing_plan"]),
        "missing_progress_count": sum(1 for item in tasks if item["missing_progress"]),
        "missing_owner_count": sum(1 for item in tasks if item["missing_owner"]),
        "top_risks": [item for item in tasks if item["health"] in {"red", "yellow"}][:3],
    }
    if include_key:
        result.update({"id": key, "name": label or key})
    return result


def _time_nodes(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in tasks:
        if item["due_date"]:
            grouped[item["due_date"]].append(item)
    return [
        {"date": node_date, **_task_group_summary(node_date, grouped[node_date], include_key=False)}
        for node_date in sorted(grouped)[:8]
    ]


def _three_list_summary(items: list[InspectionItem], work_items: list[dict[str, Any]]) -> dict[str, Any]:
    issues = [item for item in items if _item_kind(item) == "issue"]
    candidate_tasks = [item for item in items if _item_kind(item) == "task"]
    methods = [item for item in items if _item_kind(item) == "method"]
    inspection_items = [*issues, *candidate_tasks, *methods]
    confirmed = [item for item in inspection_items if item.status.value in {"confirmed", "rejected"}]
    issue_progress = _inspection_progress(issues)
    candidate_task_progress = _inspection_progress(candidate_tasks)
    formal_task_progress = (
        round(sum(item["progress"] for item in work_items) / len(work_items)) if work_items else None
    )
    task_progress = formal_task_progress if formal_task_progress is not None else candidate_task_progress
    method_progress = _inspection_progress(methods)
    available_progress = [
        value
        for value, count in (
            (issue_progress, len(issues)),
            (task_progress, len(work_items) or len(candidate_tasks)),
            (method_progress, len(methods)),
        )
        if count
    ]
    progress = round(sum(available_progress) / len(available_progress)) if available_progress else 0
    missing_owner = sum(1 for item in inspection_items if not item.owner_candidates)
    missing_owner += sum(1 for item in work_items if item["missing_owner"])
    missing_due = sum(1 for item in [*issues, *candidate_tasks] if not item.due_date)
    missing_due += sum(1 for item in work_items if item["missing_plan"])
    unlinked = sum(1 for item in issues if not item.linked_task_ids)
    total_count = len(inspection_items) + len(work_items)
    health = "gray" if not total_count else "red" if unlinked or missing_owner else "yellow" if missing_due else "green"
    return {
        "progress": progress,
        "issue_progress": issue_progress,
        "task_progress": task_progress,
        "method_progress": method_progress,
        "health": health,
        "issue_count": len(issues),
        "task_count": len(work_items) or len(candidate_tasks),
        "method_count": len(methods),
        "confirmed_count": len(confirmed),
        "unlinked_issue_count": unlinked,
        "missing_owner_count": missing_owner,
        "missing_due_date_count": missing_due,
    }


def _inspection_progress(items: list[InspectionItem]) -> int:
    if not items:
        return 0
    completed = sum(1 for item in items if item.status.value in {"confirmed", "rejected"})
    return round(completed * 100 / len(items))


def _inspection_board_ids(item: InspectionItem, task_boards: dict[str, str]) -> list[str]:
    explicit = str(getattr(item, "board_id", "") or "")
    if explicit in BOARDS:
        return [explicit]
    if item.item_id in task_boards:
        return [task_boards[item.item_id]]
    return list(dict.fromkeys(
        task_boards[task_id]
        for task_id in item.linked_task_ids or []
        if task_id in task_boards
    ))


def _task_progress(item: WorkItem) -> int:
    if item.progress_percent is not None:
        return max(0, min(100, int(item.progress_percent)))
    return 0


def _planned_progress(item: WorkItem, today: date) -> int:
    start = _parse_date(item.planned_start) or _parse_date(item.created_at)
    end = _parse_date(item.due_date)
    return _planned_progress_values(start, end, today)


def _planned_progress_values(start: date | None, end: date | None, today: date) -> int:
    if not end:
        return 0
    if not start or start >= end:
        return 100 if today >= end else 0
    if today <= start:
        return 0
    if today >= end:
        return 100
    return round((today - start).days * 100 / max(1, (end - start).days))


def _task_health(
    actual: int,
    planned: int,
    overdue: bool,
    missing_plan: bool,
    missing_progress: bool,
    status: str,
) -> str:
    if status == "canceled":
        return "green"
    if overdue or planned - actual >= 20:
        return "red"
    if missing_progress or missing_plan or planned > actual:
        return "yellow"
    if status == "done":
        return "green"
    return "green"


def _worst_health(values: list[str]) -> str:
    rank = {"gray": 0, "green": 1, "yellow": 2, "red": 3}
    return max(values, key=lambda value: rank.get(value, 0), default="gray")


def _item_kind(item: InspectionItem) -> str:
    category = (item.category or "").lower()
    if category in {"issue", "question", "questions", "open_questions", "问题"}:
        return "issue"
    if category in {"methods", "method", "方法", "法"}:
        return "method"
    if category in {"followup", "task", "tasks", "todo", "待办"}:
        return "task"
    return "other"


def _parse_date(value: str | None) -> date | None:
    text = str(value or "")[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _source_batch_name(batch_id: str) -> str:
    match = re.search(r"(\d{4})(\d{2})(\d{2})", batch_id)
    if match and "meeting" in batch_id.lower():
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)} 会议任务"
    return batch_id
