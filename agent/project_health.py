from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from agent.schemas import AgentRun, MilestonePlan


_CLOSED_STATUSES = {"done", "canceled", "archived"}


def build_project_health(*, milestone: MilestonePlan, tasks: list[dict[str, Any]], latest_run: AgentRun | None, as_of: date) -> dict[str, Any]:
    active_tasks = [task for task in tasks if str(task.get("status") or "") != "archived"]
    status_counts = Counter(str(task.get("status") or "unknown") for task in active_tasks)
    overdue_items = []
    for task in active_tasks:
        due = _parse_date(str(task.get("due_date") or ""))
        status = str(task.get("status") or "unknown")
        if due is None or due >= as_of or status in _CLOSED_STATUSES:
            continue
        overdue_items.append({
            "work_item_id": str(task.get("work_item_id") or task.get("id") or ""),
            "title": str(task.get("title") or ""),
            "status": status,
            "owners": list(task.get("owners") or []),
            "due_date": due.isoformat(),
            "days_overdue": (as_of - due).days,
            "deliverable": str(task.get("deliverable") or ""),
            "acceptance_criteria": str(task.get("acceptance_criteria") or ""),
            "evidence": list(task.get("evidence") or []),
        })
    overdue_items.sort(key=lambda item: (-item["days_overdue"], item["work_item_id"]))
    milestone_end = _parse_date(milestone.date_end)
    days_to_end = (milestone_end - as_of).days if milestone_end else None
    total = len(active_tasks)
    overdue_count = len(overdue_items)
    return {
        "as_of": as_of.isoformat(),
        "project": {"name": milestone.project, "milestone_id": milestone.milestone_id, "milestone_name": milestone.name},
        "task_counts": {
            "total": total,
            "by_status": dict(sorted(status_counts.items())),
            "completed": status_counts.get("done", 0),
            "active": sum(count for status, count in status_counts.items() if status not in _CLOSED_STATUSES),
        },
        "overdue": {
            "count": overdue_count,
            "by_status": dict(sorted(Counter(item["status"] for item in overdue_items).items())),
            "percent": round((overdue_count / total * 100.0) if total else 0.0, 1),
            "items": overdue_items,
        },
        "data_gaps": {
            "missing_owner": sum(1 for task in active_tasks if not list(task.get("owners") or [])),
            "missing_due_date": sum(1 for task in active_tasks if not str(task.get("due_date") or "").strip()),
            "missing_deliverable": sum(1 for task in active_tasks if not str(task.get("deliverable") or "").strip()),
            "missing_acceptance": sum(1 for task in active_tasks if not str(task.get("acceptance_criteria") or "").strip()),
        },
        "milestone": {
            "date_start": milestone.date_start,
            "date_end": milestone.date_end,
            "days_to_end": days_to_end,
            "overdue": bool(days_to_end is not None and days_to_end < 0),
            "days_overdue": abs(days_to_end) if days_to_end is not None and days_to_end < 0 else 0,
            "status": milestone.status,
        },
        "verification": _verification_payload(latest_run),
    }


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _verification_payload(run: AgentRun | None) -> dict[str, Any]:
    if run is None or run.verification is None:
        return {"status": "not_run", "ok": False, "checked_count": 0, "errors": []}
    return {
        "status": "passed" if run.verification.ok else "failed",
        "ok": bool(run.verification.ok),
        "checked_count": int(run.verification.checked_count),
        "errors": list(run.verification.errors),
    }
