from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from agent.llm_provider import get_provider
from agent.schemas import CandidateStatus, InspectionItem, MilestonePlan, WorkItem, WorkItemStatus
from store.sqlite_store import ProjectSQLiteStore


BRIEF_JOB_ID = "daily_project_manager_brief"


@dataclass(frozen=True)
class DailyBriefJob:
    id: str
    hour: int
    minute: int


class DailyBriefScheduler:
    def __init__(self, job_func: Callable[[], Any], hour: int = 8, minute: int = 30, timezone_name: str = "Asia/Shanghai") -> None:
        self._jobs = [DailyBriefJob(id=BRIEF_JOB_ID, hour=hour, minute=minute)]
        self._scheduler: Any | None = None
        self._timezone_name = timezone_name
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except Exception as exc:
            raise RuntimeError("apscheduler is required for daily brief scheduling; install requirements before starting the API.") from exc

        scheduler = BackgroundScheduler(timezone=timezone_name)
        scheduler.add_job(job_func, "cron", id=BRIEF_JOB_ID, hour=hour, minute=minute, replace_existing=True)
        self._scheduler = scheduler

    def get_jobs(self) -> list[DailyBriefJob]:
        return self._jobs

    def status(self) -> dict[str, Any]:
        job = self._jobs[0]
        next_run_time = ""
        if self._scheduler is not None:
            scheduled_job = self._scheduler.get_job(BRIEF_JOB_ID)
            next_time = getattr(scheduled_job, "next_run_time", None) if scheduled_job else None
            if next_time:
                next_run_time = next_time.isoformat()
        return {
            "enabled": self._scheduler is not None,
            "running": bool(self._scheduler is not None and self._scheduler.running),
            "job_id": job.id,
            "hour": job.hour,
            "minute": job.minute,
            "timezone": self._timezone_name,
            "next_run_time": next_run_time,
            "error": "",
        }

    def start(self) -> None:
        if self._scheduler is not None and not self._scheduler.running:
            self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            # Do not let a brief generation keep writing after the API lifecycle ends.
            self._scheduler.shutdown(wait=True)


def create_daily_brief_scheduler(job_func: Callable[[], Any], hour: int = 8, minute: int = 30) -> DailyBriefScheduler:
    return DailyBriefScheduler(job_func=job_func, hour=hour, minute=minute)


def ensure_today_daily_brief(
    store: ProjectSQLiteStore,
    milestone: MilestonePlan,
    *,
    project_id: str,
) -> dict[str, Any]:
    today = date.today()
    latest = latest_daily_brief(store, project_id=project_id)
    if latest and latest.get("brief_date") == today.isoformat():
        return latest
    return generate_daily_brief(
        store=store,
        milestone=milestone,
        project_id=project_id,
        today=today,
        push_feishu=False,
    )


def latest_daily_brief(store: ProjectSQLiteStore, *, project_id: str) -> dict[str, Any] | None:
    briefs = [
        source
        for source in store.list_sources()
        if source.get("kind") == "brief" and source.get("project_id") == project_id
    ]
    if not briefs:
        return None
    latest = sorted(briefs, key=lambda item: (item.get("meeting_date") or "", item.get("created_at") or "", item.get("id") or ""))[-1]
    return _brief_from_source(latest)


def generate_daily_brief(
    *,
    store: ProjectSQLiteStore,
    milestone: MilestonePlan,
    project_id: str,
    today: date | None = None,
    push_feishu: bool = False,
    webhook_url: str | None = None,
) -> dict[str, Any]:
    active_today = today or date.today()
    project = store.get_project(project_id)
    if project is None:
        raise KeyError(f"Unknown project: {project_id}")
    sections = _brief_sections(store, milestone, active_today, project_id=project_id)
    content = _brief_markdown(active_today, milestone, sections)
    ai_commentary = _ai_commentary(active_today, milestone, sections)
    if ai_commentary:
        content = f"{content.rstrip()}\n\n## AI 点评\n{ai_commentary.strip()}"
    notification_count = sum(len(section["items"]) for section in sections if section["kind"] in {"decision", "due", "candidate"})
    brief = {
        "brief_id": f"daily_brief_{project_id}_{active_today.strftime('%Y%m%d')}",
        "project_id": project_id,
        "brief_date": active_today.isoformat(),
        "title": f"{active_today.isoformat()} 项目经理晨报",
        "content_markdown": content,
        "sections": sections,
        "notification_count": notification_count,
        "ai_commentary": ai_commentary,
        "generated_at": _now(),
    }
    store.save_source(
        {
            "doc_id": brief["brief_id"],
            "title": brief["title"],
            "meeting_date": brief["brief_date"],
            "topic": "项目经理晨报",
            "content_markdown": content,
            "sections": sections,
            "notification_count": notification_count,
            "ai_commentary": ai_commentary,
            "org_id": project["org_id"],
            "project_id": project_id,
            "topic_id": None,
            "author_id": project["owner_id"],
            "sensitivity": "l3",
            "tag_origin": "system_generated",
        },
        kind="brief",
    )
    brief["feishu"] = _maybe_push_feishu(brief, push_feishu=push_feishu, webhook_url=webhook_url)
    return brief


def _brief_sections(
    store: ProjectSQLiteStore,
    milestone: MilestonePlan,
    today: date,
    *,
    project_id: str,
) -> list[dict[str, Any]]:
    items = [
        item
        for item in store.list_items()
        if item.project_id == project_id and item.status != CandidateStatus.ARCHIVED
    ]
    tasks = [
        item
        for item in store.list_work_items()
        if item.project_id == project_id and item.status != WorkItemStatus.ARCHIVED
    ]
    linked_candidate_ids = {task.source_candidate_id for task in tasks if task.source_candidate_id}
    candidate_items = [item for item in items if item.status == CandidateStatus.CANDIDATE]

    sections: list[dict[str, Any]] = []
    decision_items = [
        _item_row(item)
        for item in candidate_items
        if item.item_id not in linked_candidate_ids and _item_kind(item) in {"issue", "thing"}
    ][:5]
    if decision_items:
        sections.append({"kind": "decision", "title": "🔴 需要你今天决策的事", "items": decision_items})

    due_items = [_task_due_row(task, today) for task in tasks if _task_is_due(task, today)]
    due_items = [item for item in due_items if item is not None]
    if due_items:
        sections.append({"kind": "due", "title": "⏰ 超期与今明两天到期的任务", "items": due_items[:8]})

    milestone_risk = _milestone_risk(tasks, milestone)
    if milestone_risk:
        sections.append({"kind": "milestone", "title": "📅 里程碑风险", "items": [milestone_risk]})

    people = _people_to_find(store, tasks, candidate_items, today)
    if people:
        sections.append({"kind": "people", "title": "👤 建议你今天去找的人", "items": people[:6]})

    if candidate_items:
        sections.append({
            "kind": "candidate",
            "title": "📋 待你确认的候选",
            "items": [{"count": len(candidate_items), "action": "进入审核队列逐条确认或驳回。"}],
        })

    meeting_items = [_meeting_suggestion(item, today) for item in candidate_items if _should_suggest_meeting(item, today)]
    if meeting_items:
        sections.append({"kind": "meeting", "title": "💡 建议召开的会", "items": meeting_items[:3]})

    return sections


def _brief_markdown(today: date, milestone: MilestonePlan, sections: list[dict[str, Any]]) -> str:
    lines = [
        f"# {today.isoformat()} 项目经理晨报",
        "",
        f"- 项目：{milestone.project or '未命名项目'}",
        f"- 里程碑：{milestone.name}",
        "",
    ]
    if not sections:
        lines.append("今天没有需要你处理的事。")
        return "\n".join(lines)
    for section in sections:
        lines.append(f"## {section['title']}")
        for item in section["items"]:
            lines.extend(_section_item_lines(section["kind"], item))
        lines.append("")
    return "\n".join(lines).strip()


def _ai_commentary(today: date, milestone: MilestonePlan, sections: list[dict[str, Any]]) -> str:
    try:
        provider = get_provider()
    except Exception as exc:
        return f"AI 点评生成失败：{exc}"
    data = {
        "date": today.isoformat(),
        "milestone": {
            "project": milestone.project,
            "name": milestone.name,
            "date_start": milestone.date_start,
            "date_end": milestone.date_end,
        },
        "sections": sections,
    }
    prompt = (
        "你是务实直接的交付项目经理。只基于下面晨报结构化数据，写 3-5 句“经理视角点评”："
        "今天最重要的一件事是什么、为什么、建议先做什么。不要虚构人名、日期或数字。\n\n"
        f"{json.dumps(data, ensure_ascii=False)}"
    )
    try:
        return provider.complete(prompt).strip()
    except Exception as exc:
        return f"AI 点评生成失败：{exc}"


def _section_item_lines(kind: str, item: dict[str, Any]) -> list[str]:
    if kind == "due":
        return [
            f"- {item['title']}：{item['owner_text']}，截止 {item['due_date']}，{item['delta_text']}。",
            f"  - 交付物：{item['deliverable'] or '待补'}",
        ]
    if kind == "candidate":
        return [f"- 当前有 {item['count']} 条候选待确认。{item['action']}"]
    if kind == "milestone":
        return [f"- {item['message']} 建议：{item['action']}"]
    if kind == "people":
        return [f"- {item['owner']}：{item['reason']}"]
    if kind == "meeting":
        return [
            f"- {item['title']}：涉及 {item['owner_text']}，已 {item['stale_days']} 天无进展。",
            f"  - 议程草稿：{item['agenda']}",
        ]
    return [
        f"- {item['title']}：{item['owner_text']}",
        f"  - 证据：{item['evidence']}",
    ]


def _item_row(item: InspectionItem) -> dict[str, Any]:
    ref = item.evidence_refs[0] if item.evidence_refs else None
    return {
        "item_id": item.item_id,
        "title": item.title,
        "owner_text": _owner_text(item.owner_candidates),
        "evidence": f"{ref.source_doc_id} / {ref.locator}" if ref else "证据待补",
    }


def _task_due_row(task: WorkItem, today: date) -> dict[str, Any] | None:
    due = _parse_date(task.due_date or "")
    if due is None:
        return None
    delta = (due - today).days
    if delta < 0:
        delta_text = f"已超期 {abs(delta)} 天"
    elif delta == 0:
        delta_text = "今天到期"
    else:
        delta_text = f"{delta} 天后到期"
    return {
        "work_item_id": task.work_item_id,
        "title": task.title,
        "owner_text": _owner_text(task.owner_candidates),
        "due_date": task.due_date or "",
        "delta_days": delta,
        "delta_text": delta_text,
        "deliverable": task.deliverable or "",
    }


def _task_is_due(task: WorkItem, today: date) -> bool:
    if task.status in {WorkItemStatus.DONE, WorkItemStatus.CANCELED}:
        return False
    due = _parse_date(task.due_date or "")
    return bool(due and (due - today).days <= 2)


def _milestone_risk(tasks: list[WorkItem], milestone: MilestonePlan) -> dict[str, str] | None:
    if not tasks:
        return None
    done = len([task for task in tasks if task.status == WorkItemStatus.DONE])
    delivery_percent = int(round((done / len(tasks)) * 100))
    time_percent = _time_progress_percent(milestone)
    if time_percent - delivery_percent < 20:
        return None
    return {
        "message": f"时间进度 {time_percent}%，任务完成 {delivery_percent}%，存在交付进度落后。",
        "action": "先处理超期和无责任人任务，必要时调整里程碑范围。",
    }


def _people_to_find(
    store: ProjectSQLiteStore,
    tasks: list[WorkItem],
    candidates: list[InspectionItem],
    today: date,
) -> list[dict[str, str]]:
    from agent.evolution import build_team_profiles

    profiles = build_team_profiles(store, work_items=tasks)
    reasons: dict[str, list[str]] = {}
    for task in tasks:
        due = _parse_date(task.due_date or "")
        if due and due < today and task.status not in {WorkItemStatus.DONE, WorkItemStatus.CANCELED}:
            for owner in task.owner_candidates or ["责任人待确认"]:
                reasons.setdefault(owner, []).append(f"有超期任务：{task.title}")
    for item in candidates:
        if len(item.owner_candidates or []) >= 3:
            for owner in item.owner_candidates or []:
                reasons.setdefault(owner, []).append(f"卡在跨团队问题：{item.title}")
    for owner, profile in profiles.items():
        load = int(profile.get("current_load", 0) or 0)
        if load >= 3:
            reasons.setdefault(owner, []).append(
                f"当前负载 {load} 项，数据来源：{profile.get('data_source', '任务事件统计')}"
            )
    return [{"owner": owner, "reason": "；".join(values[:2])} for owner, values in reasons.items()]


def _should_suggest_meeting(item: InspectionItem, today: date) -> bool:
    if len(item.owner_candidates or []) < 3:
        return False
    updated = _parse_datetime(item.updated_at)
    if updated is None:
        return True
    return (today - updated.date()).days >= 5


def _meeting_suggestion(item: InspectionItem, today: date) -> dict[str, Any]:
    updated = _parse_datetime(item.updated_at)
    stale_days = (today - updated.date()).days if updated else 5
    return {
        "title": item.title,
        "owner_text": _owner_text(item.owner_candidates),
        "stale_days": stale_days,
        "agenda": "确认问题口径；明确责任人与截止日期；确定交付物和验收标准。",
    }


def _maybe_push_feishu(brief: dict[str, Any], *, push_feishu: bool, webhook_url: str | None) -> dict[str, Any]:
    target = webhook_url if webhook_url is not None else os.getenv("FEISHU_WEBHOOK_URL", "")
    if not push_feishu or not target:
        return {"status": "skipped"}
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": brief["title"]}},
            "elements": [{"tag": "markdown", "content": brief["content_markdown"]}],
        },
    }
    secret = os.getenv("FEISHU_WEBHOOK_SECRET", "")
    if secret:
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        payload["timestamp"] = timestamp
        payload["sign"] = _feishu_sign(timestamp, secret)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(target, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        raw = _read_urlopen_response(request)
        return {"status": "sent", "response": raw[:500]}
    except Exception as exc:
        if _is_certificate_error(exc) and target.startswith("https://open.feishu.cn/"):
            try:
                raw = _read_urlopen_response(request, insecure=True)
                return {"status": "sent", "tls": "unverified_retry", "response": raw[:500]}
            except Exception as retry_exc:
                return {"status": "failed", "error": str(retry_exc)}
        return {"status": "failed", "error": str(exc)}


def _read_urlopen_response(request: Request, insecure: bool = False) -> str:
    kwargs: dict[str, Any] = {"timeout": 10}
    if insecure:
        kwargs["context"] = ssl._create_unverified_context()
    with urlopen(request, **kwargs) as response:
        return response.read().decode("utf-8", errors="ignore")


def _is_certificate_error(exc: Exception) -> bool:
    reason = getattr(exc, "reason", None)
    return (
        isinstance(exc, ssl.SSLError)
        or isinstance(reason, ssl.SSLError)
        or "CERTIFICATE_VERIFY_FAILED" in str(exc)
    )


def _feishu_sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _brief_from_source(source: dict[str, Any]) -> dict[str, Any]:
    payload = source.get("payload", {})
    return {
        "brief_id": source["id"],
        "brief_date": source.get("meeting_date") or payload.get("brief_date") or "",
        "title": source.get("title") or payload.get("title") or "",
        "content_markdown": payload.get("content_markdown", ""),
        "sections": payload.get("sections", []),
        "notification_count": int(payload.get("notification_count", 0) or 0),
        "ai_commentary": payload.get("ai_commentary", ""),
        "generated_at": source.get("created_at", ""),
        "feishu": {"status": "not_requested"},
    }


def _item_kind(item: InspectionItem) -> str:
    category = (item.category or "").lower()
    if category in {"issue", "open_questions", "question", "问题", "链路", "责任"}:
        return "issue"
    if category in {"followup", "task", "tasks", "todo", "待办"}:
        return "todo"
    if category in {"methods", "method", "方法", "法"}:
        return "method"
    return "thing"


def _time_progress_percent(milestone: MilestonePlan) -> int:
    start = _parse_date(milestone.date_start)
    end = _parse_date(milestone.date_end)
    today = date.today()
    if not start or not end:
        return 0
    total = max((end - start).days + 1, 1)
    elapsed = min(max((today - start).days + 1, 0), total)
    return int(round((elapsed / total) * 100))


def _owner_text(owners: list[str] | None) -> str:
    return "、".join(owner for owner in (owners or []) if owner) or "责任人待确认"


def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
