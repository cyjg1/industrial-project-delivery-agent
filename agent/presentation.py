from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from agent.access_policy import AccessContext, User, visible_filter
from agent.evolution import build_team_profiles
from agent.memory_skill_evolution import MemorySkillEvolutionService
from agent.project_people import load_project_people
from agent.project_skills import method_skill_readiness
from agent.progress_dashboard import build_progress_dashboard, classify_person_group
from agent.role_views import role_view_key, role_view_profile
from agent.schemas import AgentRun, EvidenceRef, InspectionItem, MilestonePlan, PeopleStructure, SourceDocument, to_plain
from agent.work_item_impact import analyze_work_item_impact
from ingestion.source_manifest import obsidian_sync_status, upload_root
from store.sqlite_store import ProjectSQLiteStore


def build_human_review_workspace(
    run: AgentRun | None,
    store: ProjectSQLiteStore,
    manifest: list[SourceDocument],
    milestone: MilestonePlan | None = None,
    project_id: str | None = None,
    actor: User | None = None,
    access_context: AccessContext | None = None,
) -> dict[str, Any]:
    """Create a business-facing view of one agent run."""
    access = _workspace_access(actor, access_context, project_id=project_id)
    project_manifest = _project_rows(manifest, project_id)
    project_items = _project_rows(store.list_items(), project_id)
    project_work_items = _project_rows(store.list_work_items(), project_id)
    visible_manifest = _visible_rows(actor, access_context, project_manifest)
    visible_items = _visible_rows(actor, access_context, project_items)
    visible_work_items = _visible_rows(actor, access_context, project_work_items)
    visible_run_items = _visible_rows(actor, access_context, _project_rows(_run_report_items(run), project_id))
    visible_ingestion_jobs = store.list_ingestion_jobs(
        project_id=project_id,
        actor=actor,
        ctx=access_context,
        limit=100,
    )
    dashboard_items = _role_scoped_items(actor, access_context, visible_items, access["view_mode"])
    dashboard_work_items = _role_scoped_items(actor, access_context, visible_work_items, access["view_mode"])
    dashboard_run_items = _role_scoped_items(actor, access_context, visible_run_items, access["view_mode"])
    workspace_items = _limit_workspace_items(dashboard_items)
    workspace_work_items = _limit_workspace_items(dashboard_work_items)
    workspace_run_items = _limit_workspace_items(dashboard_run_items)
    memory_items = [item for item in visible_items if item.status.value != "archived"]
    confirmed = [item for item in memory_items if item.status.value == "confirmed"]
    rejected = [item for item in memory_items if item.status.value == "rejected"]
    candidates = [item for item in memory_items if item.status.value == "candidate"]
    memory_projection = store.memory_projection()
    memory_files = memory_projection["storage_files"]
    active_milestone = _workspace_milestone(run, milestone)
    people_asset = load_project_people(
        store,
        project_id or "project_mvp",
        actor=actor,
        access_context=access_context,
    )
    is_concrete_person = bool(
        actor and any(person.person_id == actor.id for person in people_asset.people)
    )
    # A concrete person normally gets a person-scoped dashboard. PMO is the
    # exception: selecting a named PMO reviewer must preserve the project-wide
    # review queue instead of narrowing it to items that mention that person.
    if is_concrete_person and access["view_mode"] != "pmo":
        access["capabilities"]["cross_person_load"] = True
        access["capabilities"]["progress_dashboard"] = True
        dashboard_items = [
            row for row in visible_items
            if _row_status(row) != "archived" and _actor_related_to_row(actor, access_context, row)
        ]
        dashboard_work_items = [
            row for row in visible_work_items
            if _row_status(row) != "archived" and _actor_related_to_row(actor, access_context, row)
        ]
        dashboard_run_items = [
            row for row in visible_run_items
            if _row_status(row) != "archived" and _actor_related_to_row(actor, access_context, row)
        ]
        workspace_items = _limit_workspace_items(dashboard_items)
        workspace_work_items = _limit_workspace_items(dashboard_work_items)
        workspace_run_items = _limit_workspace_items(dashboard_run_items)
    shared_items = project_items if is_concrete_person else dashboard_items
    shared_work_items = project_work_items if is_concrete_person else dashboard_work_items
    memory_skill_evolution = (
        MemorySkillEvolutionService(store).snapshot(
            actor=actor,
            ctx=access_context,
            project_id=project_id or "project_mvp",
        )
        if actor is not None and access_context is not None
        else _empty_memory_skill_evolution()
    )

    return {
        "run_id": run.run_id if run else "",
        "run_status": _run_status(run),
        "access": access,
        "milestone": {
            "name": active_milestone.name,
            "date_range": (
                f"{active_milestone.date_start} 至 "
                f"{active_milestone.date_end}"
            ),
            "scenario": active_milestone.scenario_id,
            "chain": active_milestone.chain_name,
        },
        "storage": {
            "store_dir": str(store.root_dir),
            "agent_charter_path": str(Path(__file__).resolve().parents[1] / "AGENT.md"),
            "database_path": memory_files["database"],
            "archive_dir": memory_files["archive_dir"],
            "vault_dir": memory_files["vault_dir"],
            "method_vault_dir": memory_files["method_vault_dir"],
            "meeting_vault_dir": memory_files["meeting_vault_dir"],
            "brief_vault_dir": memory_files["brief_vault_dir"],
            "weekly_review_vault_dir": memory_files["weekly_review_vault_dir"],
        },
        "daily_brief": (
            _latest_daily_brief(store, project_id=project_id)
            if access["capabilities"]["generate_management_brief"]
            else _empty_daily_brief()
        ),
        "daily_journal": _latest_daily_journal_status(
            store,
            project_id=project_id or "project_mvp",
            actor_id=actor.id if actor else "",
        ),
        "memory": {
            "candidate_count": len(candidates),
            "confirmed_count": len(confirmed),
            "rejected_count": len(rejected),
            "latest_confirmed_item_ids": [item.item_id for item in confirmed[-5:]],
        },
        "memory_skill_evolution": memory_skill_evolution,
        "inputs": _build_input_status(visible_manifest, people_asset, visible_ingestion_jobs),
        "milestone_control": _build_milestone_control(
            run,
            visible_manifest,
            store,
            active_milestone,
            visible_items=dashboard_items,
            followups=dashboard_run_items,
            include_management=access["capabilities"]["management_summary"],
        ),
        "milestone_workspace": _build_milestone_workspace(
            run,
            store,
            active_milestone,
            work_items=workspace_work_items,
            candidate_items=workspace_run_items,
        ),
        "progress_dashboard": (
            build_progress_dashboard(
                shared_work_items,
                shared_items,
                people_asset,
            )
            if access["capabilities"]["progress_dashboard"]
            else {
                "summary": {},
                "professions": [],
                "boards": [],
                "owners": [],
                "statuses": [],
                "source_batches": [],
                "legend": {},
            }
        ),
        "people_workspace": (
            _build_people_workspace(
                run,
                store,
                people_asset=people_asset,
                work_items=shared_work_items,
                candidate_items=_dedupe_inspection_items(shared_items),
                include_all_people=True,
            )
            if access["capabilities"]["cross_person_load"]
            else _empty_people_workspace()
        ),
        "team_profiles": (
            build_team_profiles(store, work_items=shared_work_items)
            if access["capabilities"]["cross_person_load"]
            else {}
        ),
        "task_pool": _build_task_pool(workspace_work_items),
        "daily_report_workspace": _build_daily_report_workspace(
            access["view_mode"],
            dashboard_work_items=dashboard_work_items,
            workspace_work_items=dashboard_work_items,
            actor=actor,
            access_context=access_context,
        ),
        "impact_analysis": (
            _build_impact_analysis(run, store, active_milestone, followups=workspace_run_items, work_items=workspace_work_items)
            if access["capabilities"]["management_summary"]
            else _empty_impact_analysis()
        ),
        "three_lists": _build_three_lists(
            run,
            store,
            items=workspace_items,
            work_items=workspace_work_items,
            report_items=workspace_run_items,
            actor=actor,
            access_context=access_context,
            project_id=project_id or "project_mvp",
        ),
        "sedimentation": _build_sedimentation_summary(run, visible_manifest, store, items=workspace_items, followups=workspace_run_items),
        "context": _build_context_summary(
            run,
            visible_manifest,
            store,
            milestone,
            include_debug=access["capabilities"]["debug_context"],
        ),
        "confirmation_cards": (
            _build_confirmation_cards(run, visible_manifest, store, visible_items=workspace_items, visible_run_items=workspace_run_items)
            if access["capabilities"]["review_queue"]
            else []
        ),
    }


def _empty_memory_skill_evolution() -> dict[str, Any]:
    return {
        "summary": {
            "episode_count": 0,
            "trace_count": 0,
            "policy_candidate_count": 0,
            "policy_approved_count": 0,
            "policy_revalidation_count": 0,
            "cognition_candidate_count": 0,
            "cognition_approved_count": 0,
            "cognition_revalidation_count": 0,
            "active_skill_count": 0,
            "probationary_skill_count": 0,
        },
        "recent_episodes": [],
        "policies": [],
        "cognitions": [],
        "skill_reliability": [],
        "governance": {},
    }


def _workspace_access(
    actor: User | None,
    ctx: AccessContext | None,
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    if actor is None or ctx is None:
        role = "pm"
        view_mode = "pmo"
        actor_payload = {"id": "system", "org_id": "org_mvp", "name": "System"}
    else:
        active_project_id = project_id
        if active_project_id is None:
            memberships = sorted(ctx.projects_of(actor))
            active_project_id = memberships[0] if len(memberships) == 1 else ""
        role = ctx.role_of(actor, active_project_id) if active_project_id else "viewer"
        view_mode = _view_mode(role)
        actor_payload = {"id": actor.id, "org_id": actor.org_id, "name": actor.name}
    profile = role_view_profile(role)
    return {
        "actor": actor_payload,
        "role": role,
        "view_mode": view_mode,
        "role_profile": profile.as_dict(),
        "capabilities": {
            "milestone_overview": view_mode in {"pmo", "professional_lead", "topic_lead", "exec", "viewer"},
            "cross_person_load": view_mode in {"pmo", "professional_lead", "topic_lead"},
            "management_summary": view_mode in {"pmo", "professional_lead", "topic_lead"},
            "generate_management_brief": view_mode == "pmo",
            "review_queue": view_mode in {"pmo", "professional_lead", "topic_lead"},
            "edit_workspace": view_mode in {"pmo", "professional_lead", "topic_lead"},
            "debug_context": view_mode == "pmo",
            "daily_report_workspace": view_mode in {"pmo", "professional_lead", "topic_lead", "exec"},
            "progress_dashboard": view_mode in {"pmo", "professional_lead", "topic_lead"},
        },
    }

def _view_mode(role: str) -> str:
    return role_view_key(role)


def _project_rows(rows: list[Any], project_id: str | None) -> list[Any]:
    if project_id is None:
        return list(rows)
    return [
        row
        for row in rows
        if (
            row.get("project_id") if isinstance(row, dict) else getattr(row, "project_id", None)
        ) == project_id
    ]


def _visible_rows(actor: User | None, ctx: AccessContext | None, rows: list[Any]) -> list[Any]:
    if actor is None or ctx is None:
        return list(rows)
    return visible_filter(actor, list(rows), ctx)


def _role_scoped_items(actor: User | None, ctx: AccessContext | None, rows: list[Any], view_mode: str) -> list[Any]:
    if actor is None or ctx is None or view_mode == "pmo":
        return [row for row in rows if _row_status(row) != "archived"]
    if view_mode == "professional_lead":
        return [
            row
            for row in rows
            if _row_status(row) != "archived"
            and (
                _row_author(row) == actor.id
                or (_row_topic(row) and actor.id in ctx.members_of(_row_topic(row)))
            )
        ]
    if view_mode == "viewer":
        return []
    if view_mode == "topic_lead":
        return [
            row
            for row in rows
            if _row_status(row) != "archived"
            and (_row_author(row) == actor.id or (_row_topic(row) and actor.id in ctx.members_of(_row_topic(row))))
        ]
    if view_mode == "exec":
        return [
            row
            for row in rows
            if _row_status(row) != "archived" and _actor_related_to_row(actor, ctx, row)
        ]
    return []


def _limit_workspace_items(rows: list[Any], limit: int = 240) -> list[Any]:
    def sort_key(row: Any) -> tuple[int, str, str]:
        status_rank = {"candidate": 0, "confirmed": 1, "rejected": 2}.get(_row_status(row), 3)
        due_date = str(getattr(row, "due_date", "") or "9999-12-31")
        updated_at = str(getattr(row, "updated_at", "") or "")
        return (status_rank, due_date, updated_at)

    return sorted([row for row in rows if _row_status(row) != "archived"], key=sort_key)[:limit]


def _row_category(row: Any) -> str:
    return str(getattr(row, "category", "") or getattr(row, "type", "") or "")


def _row_sensitivity(row: Any) -> str:
    return str(getattr(row, "sensitivity", "") or "")


def _actor_related_to_row(actor: User, ctx: AccessContext, row: Any) -> bool:
    if _row_author(row) == actor.id:
        return True
    owner_text = " ".join(str(value) for value in (getattr(row, "owner_candidates", None) or []))
    searchable = " ".join([
        owner_text,
        str(getattr(row, "title", "") or ""),
        str(getattr(row, "description", "") or ""),
    ])
    return bool(actor.name and actor.name in searchable) or actor.id in searchable


def _row_status(row: Any) -> str:
    status = getattr(row, "status", "")
    return str(getattr(status, "value", status) or "")


def _row_author(row: Any) -> str:
    return str(getattr(row, "author_id", "") or "")


def _row_topic(row: Any) -> str:
    return str(getattr(row, "topic_id", "") or "")


def _build_milestone_control(
    run: AgentRun | None,
    manifest: list[SourceDocument],
    store: ProjectSQLiteStore,
    milestone: MilestonePlan,
    *,
    visible_items: list[InspectionItem] | None = None,
    followups: list[InspectionItem] | None = None,
    include_management: bool = True,
) -> dict[str, Any]:
    report_items = _run_report_items(run)
    followups = list(followups if followups is not None else (run.final_report.followup_drafts if run and run.final_report else []))
    all_items = list(visible_items if visible_items is not None else store.list_items())
    raw_pending = sum(1 for source in manifest if source.raw_source.status != "matched")
    raw_matched = sum(1 for source in manifest if source.raw_source.status == "matched")
    todo_summary = _todo_backschedule(followups, milestone)
    suggestions = _milestone_adjustment_suggestions(
        milestone=milestone,
        material_raw_pending=raw_pending,
        todo_summary=todo_summary,
        candidate_count=len([item for item in all_items if item.status.value == "candidate"]),
        run=run,
    )
    return {
        "plan": {
            "milestone_id": milestone.milestone_id,
            "project": milestone.project,
            "name": milestone.name,
            "date_start": milestone.date_start,
            "date_end": milestone.date_end,
            "scenario": milestone.scenario_id,
            "chain": milestone.chain_name,
            "status": milestone.status,
            "trigger_policy": milestone.trigger_policy,
            "acceptance_criteria": milestone.acceptance_criteria,
        },
        "time_progress": _time_progress(milestone),
        "material_progress": {
            "source_count": len(manifest),
            "curated_ready": sum(1 for source in manifest if source.curated_source.path),
            "raw_matched": raw_matched,
            "raw_pending": raw_pending,
            "raw_coverage_percent": _percent(raw_matched, len(manifest)),
            "uploaded_count": sum(1 for source in manifest if source.doc_id.startswith("uploaded_")),
            "source_titles": [source.title for source in manifest[:12]],
        },
        "todo_backschedule": todo_summary,
        "run_progress": {
            "status": run.status if run else "idle",
            "report_item_count": len(report_items),
            "candidate_memory_count": len([item for item in all_items if item.status.value == "candidate"]),
            "confirmed_memory_count": len([item for item in all_items if item.status.value == "confirmed"]),
            "rejected_memory_count": len([item for item in all_items if item.status.value == "rejected"]),
            "verification_ok": bool(run and run.verification and run.verification.ok),
        },
        "adjustment_suggestions": suggestions if include_management else [],
        "context_brief": [
            f"当前里程碑：{milestone.name}",
            f"时间范围：{milestone.date_start} 至 {milestone.date_end}",
            f"链路：{milestone.scenario_id} / {milestone.chain_name}",
            f"材料进度：{len(manifest)} 份材料，原始转写覆盖 {raw_matched}/{len(manifest)}",
            f"待办倒排：{todo_summary['total']} 项，逾期 {todo_summary['overdue']} 项，缺截止日期 {todo_summary['missing_due_date']} 项",
        ],
    }


def _latest_daily_brief(store: ProjectSQLiteStore, *, project_id: str | None) -> dict[str, Any]:
    briefs = [
        source
        for source in store.list_sources()
        if source.get("kind") == "brief"
        and (project_id is None or source.get("project_id") == project_id)
    ]
    if not briefs:
        return _empty_daily_brief()
    latest = sorted(briefs, key=lambda item: (item.get("meeting_date") or "", item.get("created_at") or "", item.get("id") or ""))[-1]
    payload = latest.get("payload", {})
    return {
        "brief_id": latest["id"],
        "brief_date": latest.get("meeting_date") or payload.get("brief_date", ""),
        "title": latest.get("title") or payload.get("title", ""),
        "content_markdown": payload.get("content_markdown", ""),
        "sections": payload.get("sections", []),
        "notification_count": int(payload.get("notification_count", 0) or 0),
        "ai_commentary": payload.get("ai_commentary", ""),
        "generated_at": latest.get("created_at", ""),
    }


def _empty_daily_brief() -> dict[str, Any]:
    return {
        "brief_id": "",
        "brief_date": "",
        "title": "",
        "content_markdown": "",
        "sections": [],
        "notification_count": 0,
        "ai_commentary": "",
        "generated_at": "",
    }


def _latest_daily_journal_status(
    store: ProjectSQLiteStore,
    *,
    project_id: str,
    actor_id: str,
) -> dict[str, Any]:
    latest = store.latest_daily_journal(project_id=project_id, actor_id=actor_id) if actor_id else None
    if latest is None:
        return {
            "journal_id": "",
            "version_id": "",
            "version": 0,
            "report_date": "",
            "status": "collecting",
            "organization_status": "pending",
            "message_count": 0,
            "source_id": "",
            "ingestion_job_id": "",
            "error": "",
            "updated_at": "",
        }
    return {
        "journal_id": latest["journal_id"],
        "version_id": latest["version_id"],
        "version": latest["version"],
        "report_date": latest["report_date"],
        "status": latest["status"],
        "organization_status": latest["organization_status"],
        "message_count": latest["message_count"],
        "source_id": latest["source_id"],
        "ingestion_job_id": latest["ingestion_job_id"],
        "error": latest["error"],
        "updated_at": latest["updated_at"],
    }


def _time_progress(milestone: MilestonePlan) -> dict[str, Any]:
    start = _parse_date(milestone.date_start)
    end = _parse_date(milestone.date_end)
    today = date.today()
    if not start or not end:
        return {
            "today": today.isoformat(),
            "days_total": 0,
            "days_elapsed": 0,
            "days_remaining": 0,
            "percent": 0,
            "status_label": "时间未设定",
        }
    days_total = max((end - start).days + 1, 1)
    days_elapsed = min(max((today - start).days + 1, 0), days_total)
    days_remaining = max((end - today).days, 0)
    if today < start:
        status_label = "未开始"
    elif today > end:
        status_label = "已到期"
    else:
        status_label = "进行中"
    return {
        "today": today.isoformat(),
        "days_total": days_total,
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
        "percent": _percent(days_elapsed, days_total),
        "status_label": status_label,
    }


def _todo_backschedule(items: list[InspectionItem], milestone: MilestonePlan) -> dict[str, Any]:
    today = date.today()
    milestone_start = _parse_date(milestone.date_start)
    milestone_end = _parse_date(milestone.date_end)
    rows: list[dict[str, Any]] = []
    overdue = 0
    due_this_week = 0
    missing_due_date = 0
    outside_milestone = 0
    for item in items:
        due = _parse_date(item.due_date or "")
        if not due:
            missing_due_date += 1
        else:
            if due < today:
                overdue += 1
            if 0 <= (due - today).days <= 7:
                due_this_week += 1
            if (milestone_start and due < milestone_start) or (milestone_end and due > milestone_end):
                outside_milestone += 1
        rows.append({
            "item_id": item.item_id,
            "title": item.title,
            "owner_text": "、".join(_dedupe(item.owner_candidates or [])) or "待人工确认责任人",
            "due_date": item.due_date or "",
            "days_to_due": (due - today).days if due else None,
            "deliverable": item.deliverable or "",
            "acceptance_criteria": item.acceptance_criteria or "",
        })
    rows.sort(key=lambda row: (row["days_to_due"] is None, row["days_to_due"] if row["days_to_due"] is not None else 99999))
    return {
        "total": len(items),
        "overdue": overdue,
        "due_this_week": due_this_week,
        "missing_due_date": missing_due_date,
        "outside_milestone": outside_milestone,
        "items": rows[:10],
    }


def _milestone_adjustment_suggestions(
    *,
    milestone: MilestonePlan,
    material_raw_pending: int,
    todo_summary: dict[str, Any],
    candidate_count: int,
    run: AgentRun | None,
) -> list[dict[str, str]]:
    suggestions: list[dict[str, str]] = []
    if material_raw_pending:
        suggestions.append({
            "level": "warning",
            "type": "material_gap",
            "message": f"当前里程碑仍有 {material_raw_pending} 份材料缺原始转写覆盖，确认前需要补齐或明确接受 candidate 边界。",
            "action": "上传原始转写，或在里程碑说明中标记本轮只做整理版候选巡检。",
        })
    if todo_summary["missing_due_date"]:
        suggestions.append({
            "level": "warning",
            "type": "todo_due_date_gap",
            "message": f"{todo_summary['missing_due_date']} 个待办缺截止日期，无法做时间倒排。",
            "action": "补充截止日期，或调整里程碑结束日期和验收节奏。",
        })
    if todo_summary["outside_milestone"]:
        suggestions.append({
            "level": "warning",
            "type": "todo_outside_milestone",
            "message": f"{todo_summary['outside_milestone']} 个待办截止日期落在里程碑范围外。",
            "action": "修改待办截止日期，或调整里程碑起止时间。",
        })
    if todo_summary["overdue"] and _parse_date(milestone.date_end) and date.today() > _parse_date(milestone.date_end):
        suggestions.append({
            "level": "critical",
            "type": "milestone_expired_with_open_todos",
            "message": f"里程碑已到期，但仍有 {todo_summary['overdue']} 个待办按当前日期判断已逾期。",
            "action": "重新确认里程碑是否延期、拆分，或关闭不再推进的候选待办。",
        })
    if run and run.status != "completed":
        suggestions.append({
            "level": "critical",
            "type": "run_not_completed",
            "message": "最近一次运行未完成，里程碑进度不能作为有效判断。",
            "action": "先处理运行错误，再更新里程碑状态。",
        })
    if candidate_count and not suggestions:
        suggestions.append({
            "level": "info",
            "type": "candidate_review",
            "message": f"当前有 {candidate_count} 条候选记忆，里程碑验收前需要人工确认或驳回。",
            "action": "进入候选卡片逐条确认。",
        })
    return suggestions


def _run_report_items(run: AgentRun | None) -> list[InspectionItem]:
    if not run or not run.final_report:
        return []
    return run.final_report.chain_gaps + run.final_report.responsibility_gaps + run.final_report.followup_drafts


def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _percent(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        return 0
    return int(round((numerator / denominator) * 100))


def _build_context_summary(
    run: AgentRun | None,
    manifest: list[SourceDocument],
    store: ProjectSQLiteStore,
    milestone: MilestonePlan | None,
    *,
    include_debug: bool = True,
) -> dict[str, Any]:
    curated_count = sum(1 for source in manifest if source.curated_source.path)
    raw_matched_count = sum(1 for source in manifest if source.raw_source.status == "matched")
    raw_pending_count = sum(1 for source in manifest if source.raw_source.status != "matched")
    item_count = 0
    if run and run.final_report:
        item_count = (
            len(run.final_report.chain_gaps)
            + len(run.final_report.responsibility_gaps)
            + len(run.final_report.followup_drafts)
        )
    active_milestone = _workspace_milestone(run, milestone)
    assembly_steps = [
        (
            f"1. 里程碑计划：{active_milestone.name}，范围 "
            f"{active_milestone.date_start} 至 {active_milestone.date_end}。"
        ),
        (
            f"2. Obsidian 会议纪要：从材料清单读取 {curated_count} 篇整理版，"
            f"原始材料 matched={raw_matched_count}，pending={raw_pending_count}。"
        ),
        "3. 人员资产：只读取当前项目已归档并带访问标签的人员来源；Excel、XMind、CSV、手工修改均先归档到项目。",
        "4. 候选事实：从整理版会议纪要抽取人、事、法、链路、追问，默认都是 candidate。",
        (
            f"5. 记忆层：读取 {store.items_path}，把上次确认/驳回状态合并回本次运行。"
        ),
        f"6. Function-calling runtime：模型按当前状态选择工具，生成 {item_count} 张待确认卡片；独立 verification gate 通过后才写候选记忆。",
    ]
    tool_chain = [
        {
            "tool": step.tool_name,
            "input": step.input_summary,
            "output": step.output_summary,
        }
        for step in (run.steps if run else [])
    ]
    harness = _harness_state(run)
    rounds = _build_rounds(run)

    return {
        "assembly_steps": assembly_steps,
        "tool_chain": tool_chain if include_debug else [],
        "agent_trace": run.agent_trace if run and include_debug else [],
        "model_io_events": run.model_io_events if run and include_debug else [],
        "harness": harness,
        "rounds": rounds if include_debug else [],
        "audit_trail": _build_audit_trail(
            run=run,
            store=store,
            assembly_steps=assembly_steps,
            tool_chain=tool_chain,
            harness=harness,
            rounds=rounds,
        ) if include_debug else [],
    }


def _build_audit_trail(
    *,
    run: AgentRun | None,
    store: ProjectSQLiteStore,
    assembly_steps: list[str],
    tool_chain: list[dict[str, str]],
    harness: dict[str, Any],
    rounds: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    audit: list[dict[str, Any]] = []
    run_status = run.status if run else "idle"
    audit.append({
        "step_index": 1,
        "stage": "输入组装",
        "title": "本次巡检输入和上下文拼接",
        "status": run_status,
        "sent": "发给模型/工具：里程碑计划、材料清单、人员资产、项目记忆、整理版纪要、循环预算。",
        "assembled_context": assembly_steps,
        "returned": "已形成本次运行上下文；不会展示模型隐藏思维链，只展示可审计输入、工具返回和结构化结果。",
        "raw_reference": "input_snapshot",
        "detail": _preview_data(run.input_snapshot if run else {}),
    })

    trace_by_name = {
        item.get("name"): item
        for item in (run.agent_trace if run else [])
    }
    for item in tool_chain:
        trace = trace_by_name.get(item.get("tool", ""), {})
        audit.append({
            "step_index": len(audit) + 1,
            "stage": "工具调用",
            "title": item.get("tool", ""),
            "status": trace.get("status", "completed"),
            "sent": f"发给模型/工具：{item.get('input', '') or '无输入摘要'}",
            "assembled_context": _context_refs_for_trace(trace, harness),
            "returned": item.get("output", "") or trace.get("output_summary", ""),
            "raw_reference": str(trace.get("context_ref", "")),
            "detail": _preview_data(trace.get("data", {})),
        })

    for round_item in rounds:
        if round_item.get("round_kind") != "model" and not round_item.get("raw_response_id"):
            continue
        audit.append({
            "step_index": len(audit) + 1,
            "stage": "模型轮次",
            "title": f"第 {round_item.get('round_index')} 轮模型响应",
            "status": round_item.get("status", ""),
            "sent": f"发给模型/工具：{round_item.get('input_summary', '') or '当前运行状态摘要'}",
            "assembled_context": [
                f"阶段：{round_item.get('phase', '')}",
                f"目标：{round_item.get('objective', '')}",
                f"决策：{round_item.get('decision', '')}",
            ],
            "returned": round_item.get("model_result", "") or round_item.get("output_summary", ""),
            "raw_reference": round_item.get("raw_response_id", ""),
            "detail": _preview_data({
                "token_usage": round_item.get("token_usage", {}),
                "tool_calls": round_item.get("tool_calls", []),
                "events": round_item.get("events", []),
                "error": round_item.get("error", ""),
            }),
        })

    model_io_events = list(run.model_io_events if run else [])
    audit.append({
        "step_index": len(audit) + 1,
        "stage": "模型 API 输入输出",
        "title": "调试期模型 API 请求与返回",
        "status": run_status,
        "sent": "发给模型 API：当前消息、完成契约和 actor 有权使用的工具 schema；不包含模型隐藏思维链。",
        "assembled_context": [
            f"模型调用次数：{len(model_io_events)}",
            f"运行方式：{run.runtime_kind if run else '未运行'}",
        ],
        "returned": "已记录模型 API 返回文本和工具调用；provider 未返回的 response id 或 token 用量保持为空。" if model_io_events else "本次运行没有模型 API 返回记录。",
        "raw_reference": "model_io_events",
        "detail": _preview_data(model_io_events),
    })

    audit.append({
        "step_index": len(audit) + 1,
        "stage": "持久化",
        "title": "写入运行记录和候选记忆",
        "status": run_status,
        "sent": "发给模型/工具：不再调用模型；将运行结果写入 SQLite 权威库。",
        "assembled_context": [
            f"运行记录：{store.runs_path}",
            f"候选/确认/驳回记忆：{store.items_path}",
        ],
        "returned": (
            f"运行状态={run_status}；"
            f"确认项={len(run.confirmed_item_ids) if run else 0}；"
            f"停止原因={(run.stop_reason if run else '') or '未运行'}"
        ),
        "raw_reference": str(store.runs_path),
        "detail": _preview_data({
            "run_id": run.run_id if run else "",
            "runtime_kind": run.runtime_kind if run else "",
            "structured_output_keys": list((run.structured_output if run else {}).keys()),
            "verification": to_plain(run.verification) if run and run.verification else {},
        }),
    })
    return audit


def _build_task_pool(work_items: list[Any]) -> dict[str, Any]:
    work_items = [item for item in work_items if item.status.value != "archived"]
    status_counts: dict[str, int] = {}
    for item in work_items:
        status_counts[item.status.value] = status_counts.get(item.status.value, 0) + 1
    return {
        "summary": {
            "total": len(work_items),
            "status_counts": status_counts,
        },
        "work_items": [to_plain(item) for item in work_items[:50]],
    }


def _build_daily_report_workspace(
    view_mode: str,
    *,
    dashboard_work_items: list[Any],
    workspace_work_items: list[Any],
    actor: User | None,
    access_context: AccessContext | None,
) -> dict[str, Any]:
    project_items = [item for item in dashboard_work_items if item.status.value != "archived"]
    scoped_items = [item for item in workspace_work_items if item.status.value != "archived"]
    today = date.today()
    active_project_items = [item for item in project_items if item.status.value not in {"done", "canceled"}]
    active_scoped_items = [item for item in scoped_items if item.status.value not in {"done", "canceled"}]
    stale_project_items = [item for item in active_project_items if _days_since_update(item, today) >= 1]
    stale_scoped_items = [item for item in active_scoped_items if _days_since_update(item, today) >= 1]
    risk_items = [item for item in active_project_items if _is_daily_risk_item(item, today)]
    scoped_risk_items = [item for item in active_scoped_items if _is_daily_risk_item(item, today)]
    missing_progress_items = [item for item in active_project_items if item.progress_percent is None]
    scoped_missing_progress_items = [item for item in active_scoped_items if item.progress_percent is None]
    owner_rollup = _owner_report_rollup(active_scoped_items if view_mode in {"topic_lead", "professional_lead"} else active_project_items, today)
    return {
        "view_mode": view_mode,
        "title": _daily_report_title(view_mode),
        "focus": _daily_report_focus(view_mode),
        "summary": {
            "project_active_work_count": len(active_project_items),
            "role_active_work_count": len(active_scoped_items),
            "project_stale_report_count": len(stale_project_items),
            "role_stale_report_count": len(stale_scoped_items),
            "project_risk_count": len(risk_items),
            "role_risk_count": len(scoped_risk_items),
            "project_missing_progress_count": len(missing_progress_items),
            "role_missing_progress_count": len(scoped_missing_progress_items),
        },
        "my_report_required": view_mode == "exec" and bool(active_scoped_items),
        "items_requiring_update": [_daily_report_item(item, today) for item in stale_scoped_items[:12]],
        "risk_items": [_daily_report_item(item, today) for item in (scoped_risk_items if view_mode != "pmo" else risk_items)[:12]],
        "owner_rollup": owner_rollup[:20],
        "recommended_actions": _daily_report_actions(
            view_mode,
            bool(active_scoped_items),
            len(stale_scoped_items),
            len(scoped_risk_items),
            len(scoped_missing_progress_items),
        ),
    }


def _daily_report_title(view_mode: str) -> str:
    return {
        "pmo": "项目管理日报总览",
        "professional_lead": "专业统筹日报洞察",
        "topic_lead": "板块/专题日报检查",
        "exec": "我的日报提醒",
    }.get(view_mode, "日报工作台")


def _daily_report_focus(view_mode: str) -> str:
    return {
        "pmo": "看项目全景、总体填报、项目进展、人力资源安排是否正常，以及风险是否需要升级。",
        "professional_lead": "从日报中识别专业口径、质量、评审、变更和跨专题风险。",
        "topic_lead": "看本板块谁没更新、任务进展质量如何、哪些需要协调。",
        "exec": "提醒我更新日报、说明今天完成了什么、卡在哪里、下一步做什么。",
    }.get(view_mode, "查看当前角色相关日报事项。")


def _daily_report_actions(
    view_mode: str,
    has_active: bool,
    stale_count: int,
    risk_count: int,
    missing_progress_count: int,
) -> list[str]:
    if view_mode == "exec":
        if not has_active:
            return ["当前没有需要填写日报的进行中任务。"]
        actions = ["更新今日日报：已完成、进行中、阻塞、下一步、需谁支持。"]
        if stale_count:
            actions.append("补充长期未更新任务的最新状态。")
        if risk_count:
            actions.append("把阻塞原因和需要协调的人写清楚。")
        if missing_progress_count:
            actions.append(f"补充 {missing_progress_count} 项任务的实际完成百分比。")
        return actions
    if view_mode == "topic_lead":
        return ["检查本板块未更新人员和未填写完成度的任务。", "抽查日报是否说明输入、输出、阻塞和下一步。", "把需要外部协同的问题升级给 PMO 或专业统筹。"]
    if view_mode == "professional_lead":
        return ["识别日报中暴露的专业口径、质量、评审和变更风险。", "要求相关专题补充实际完成度、专业依据或复核结论。"]
    return ["查看项目日报完成率和进度填写完整率。", "关注风险任务、未填写完成度和长期未更新任务。", "必要时按责任人发起催办或升级。"]


def _owner_report_rollup(work_items: list[Any], today: date) -> list[dict[str, Any]]:
    rollup: dict[str, dict[str, Any]] = {}
    for item in work_items:
        owners = _dedupe(item.owner_candidates or []) or ["责任人待确认"]
        for owner in owners:
            row = rollup.setdefault(owner, {"owner": owner, "active_count": 0, "stale_count": 0, "risk_count": 0})
            row["active_count"] += 1
            if _days_since_update(item, today) >= 1:
                row["stale_count"] += 1
            if _is_daily_risk_item(item, today):
                row["risk_count"] += 1
    return sorted(rollup.values(), key=lambda row: (-row["stale_count"], -row["risk_count"], row["owner"]))


def _daily_report_item(item: Any, today: date) -> dict[str, Any]:
    return {
        "work_item_id": item.work_item_id,
        "title": item.title,
        "owner_text": "、".join(_dedupe(item.owner_candidates or [])) or "责任人待确认",
        "status": item.status.value,
        "due_date": item.due_date or "",
        "days_since_update": _days_since_update(item, today),
        "overdue": _is_overdue_work_item(item, today),
        "missing_progress": item.progress_percent is None,
        "deliverable": item.deliverable or "",
    }


def _days_since_update(item: Any, today: date) -> int:
    updated = _parse_date(str(getattr(item, "updated_at", "") or ""))
    if not updated:
        return 999
    return max((today - updated).days, 0)


def _is_overdue_work_item(item: Any, today: date) -> bool:
    due = _parse_date(getattr(item, "due_date", "") or "")
    return bool(due and due < today and item.status.value not in {"done", "canceled", "archived"})


def _is_daily_risk_item(item: Any, today: date) -> bool:
    return (
        item.progress_percent is None
        or _is_overdue_work_item(item, today)
    )


def _build_impact_analysis(
    run: AgentRun | None,
    store: ProjectSQLiteStore,
    milestone: MilestonePlan,
    *,
    followups: list[InspectionItem] | None = None,
    work_items: list[Any] | None = None,
) -> dict[str, Any]:
    followups = list(followups if followups is not None else (run.final_report.followup_drafts if run and run.final_report else []))
    work_items = list(work_items if work_items is not None else store.list_work_items())
    return analyze_work_item_impact(
        followups,
        [item for item in work_items if item.status.value != "archived"],
        milestone_id=milestone.milestone_id,
    )


def _empty_impact_analysis() -> dict[str, Any]:
    return {
        "summary": {
            "source_followup_count": 0,
            "work_item_count": 0,
            "suggestion_count": 0,
            "matched_existing_count": 0,
            "new_task_count": 0,
        },
        "suggestions": [],
    }


def _build_three_lists(
    run: AgentRun | None,
    store: ProjectSQLiteStore,
    *,
    items: list[InspectionItem] | None = None,
    work_items: list[Any] | None = None,
    report_items: list[InspectionItem] | None = None,
    actor: User | None = None,
    access_context: AccessContext | None = None,
    project_id: str = "project_mvp",
) -> dict[str, Any]:
    """Build the issue -> task -> method projection used by the project sheet.

    The reference workbook has three sync sheets: 问题清单同步、任务清单同步、
    工具方法清单同步. This projection keeps the same business shape without
    making the Excel file a runtime dependency.
    """
    stored_items = [item for item in (items if items is not None else store.list_items()) if item.status.value != "archived"]
    run_items = report_items if report_items is not None else _run_report_items(run)
    all_items = _dedupe_inspection_items([*stored_items, *run_items])
    issues = [item for item in all_items if _is_issue_list_item(item)]
    methods = [item for item in all_items if _is_method_list_item(item)]
    candidate_tasks = [item for item in all_items if _is_task_list_item(item)]
    work_items = [item for item in (work_items if work_items is not None else store.list_work_items()) if item.status.value != "archived"]
    published_source_ids = {item.source_candidate_id for item in work_items if item.source_candidate_id}
    visible_issues = issues[:80]
    visible_methods = methods[:80]
    unpublished_candidate_tasks = [item for item in candidate_tasks if item.item_id not in published_source_ids]
    # Task boards paginate in the client, so keep the complete visible task set here.
    # Truncating this projection made valid meeting tasks disappear once the project
    # accumulated more than 80 formal tasks.
    visible_work_items = work_items
    visible_candidate_tasks = unpublished_candidate_tasks
    full_task_count = len(work_items) + len(unpublished_candidate_tasks)

    task_rows: list[dict[str, Any]] = []
    for work_item in visible_work_items:
        source_doc_id_set = _evidence_source_ids(work_item.evidence_refs)
        source_doc_ids = sorted(source_doc_id_set)
        source_batch_id = _source_batch_id(source_doc_ids, work_item.confirmation_notes)
        linked_issue_ids = _dedupe([
            *(work_item.linked_issue_ids or []),
            *_linked_issue_ids(
                _work_item_link_text(work_item),
                source_doc_id_set,
                visible_issues,
            ),
        ])[:8]
        linked_task_ids = _dedupe(work_item.linked_task_ids or [])
        if work_item.work_item_id not in linked_task_ids:
            linked_task_ids.insert(0, work_item.work_item_id)
        task_rows.append({
            "task_id": work_item.work_item_id,
            "source_sheet": "任务清单同步",
            "task_description": work_item.title,
            "task_detail": work_item.description,
            "task_type": "正式任务",
            "owner_text": "、".join(_dedupe(work_item.owner_candidates or [])) or "责任人待确认",
            "cedi_people": "、".join(_dedupe(work_item.collaborators or [])),
            "responsible_department": "",
            "linked_issue_ids": linked_issue_ids,
            "linked_issue_titles": _issue_titles(linked_issue_ids, issues),
            "linked_task_ids": linked_task_ids[:8],
            "due_date": work_item.due_date or "",
            "acceptance_criteria": work_item.acceptance_criteria or "",
            "priority": "",
            "progress": work_item.progress_percent,
            "task_status": work_item.status.value,
            "deliverable": work_item.deliverable or "",
            "parent_task": "",
            "source_candidate_id": work_item.source_candidate_id,
            "source_doc_ids": source_doc_ids,
            "source_batch_id": source_batch_id,
            "evidence_count": len(work_item.evidence_refs),
        })
    for item in visible_candidate_tasks:
        source_doc_id_set = _evidence_source_ids(item.evidence_refs)
        source_doc_ids = sorted(source_doc_id_set)
        source_batch_id = _source_batch_id(source_doc_ids)
        linked_issue_ids = _dedupe([
            *(item.linked_issue_ids or []),
            *_linked_issue_ids(
                _inspection_link_text(item),
                source_doc_id_set,
                visible_issues,
            ),
        ])[:8]
        linked_task_ids = _dedupe(item.linked_task_ids or [])
        if item.item_id not in linked_task_ids:
            linked_task_ids.insert(0, item.item_id)
        task_rows.append({
            "task_id": item.item_id,
            "source_sheet": "任务清单同步",
            "task_description": item.title,
            "task_detail": item.description,
            "task_type": "候选任务",
            "owner_text": "、".join(_dedupe(item.owner_candidates or [])) or "责任人待确认",
            "cedi_people": "",
            "responsible_department": "",
            "linked_issue_ids": linked_issue_ids,
            "linked_issue_titles": _issue_titles(linked_issue_ids, issues),
            "linked_task_ids": linked_task_ids[:8],
            "due_date": item.due_date or "",
            "acceptance_criteria": item.acceptance_criteria or "",
            "priority": "",
            "progress": 0,
            "task_status": item.status.value,
            "deliverable": item.deliverable or "",
            "parent_task": "",
            "source_candidate_id": item.item_id,
            "source_doc_ids": source_doc_ids,
            "source_batch_id": source_batch_id,
            "evidence_count": len(item.evidence_refs),
        })

    skills_by_method: dict[str, dict[str, Any]] = {}
    if actor is not None and access_context is not None:
        skills_by_method = {
            skill["source_method_id"]: skill
            for skill in store.list_project_skills(
                project_id=project_id,
                actor=actor,
                ctx=access_context,
            )
        }
    method_rows: list[dict[str, Any]] = []
    for item in visible_methods:
        linked_issue_ids = _dedupe([
            *(item.linked_issue_ids or []),
            *_linked_issue_ids(
                _inspection_link_text(item),
                _evidence_source_ids(item.evidence_refs),
                visible_issues,
                allow_evidence_only=True,
            ),
        ])[:8]
        linked_task_ids = [
            row["task_id"]
            for row in task_rows
            if set(row["linked_issue_ids"]) & set(linked_issue_ids)
            or _link_score(_task_row_link_text(row), _evidence_source_ids(item.evidence_refs), item) >= 0.2
        ]
        visible_linked_task_ids = _dedupe([*(item.linked_task_ids or []), *linked_task_ids])[:8]
        skill = skills_by_method.get(item.item_id)
        maturity, gate_failures, skill_version = _method_skill_maturity(item, skill)
        method_rows.append({
            "method_id": item.item_id,
            "source_sheet": "工具方法清单同步",
            "overview": item.title,
            "content_tags": "、".join(_dedupe([*(item.principles or []), *(item.facet_types or [])])) or "工具方法清单",
            "detail": item.description,
            "date": _first_evidence_locator(item),
            "proposer": "、".join(_dedupe(item.owner_candidates or [])) or "提出人待确认",
            "linked_issue_ids": linked_issue_ids,
            "linked_issue_titles": _issue_titles(linked_issue_ids, issues),
            "linked_task_ids": visible_linked_task_ids,
            "linked_task_count": len(set(linked_task_ids)),
            "memory_target": (
                "给人查看 + 长期记忆"
                if item.status.value == "confirmed"
                else "给人查看，确认后进入长期记忆"
            ),
            "status": item.status.value,
            "business_goal": item.business_goal or "",
            "principles": item.principles or [],
            "reasoning_chain": item.reasoning_chain or [],
            "applicable_scope": item.applicable_scope or "",
            "evidence_count": len(item.evidence_refs),
            "evidence_refs": _method_evidence_rows(store, item),
            "observed_fields": item.observed_fields,
            "proposed_fields": item.proposed_fields,
            "inference_basis": item.inference_basis,
            "inference_confidence": item.inference_confidence,
            "inference_note": item.inference_note or "",
            "skill_id": skill["skill_id"] if skill else "",
            "skill_maturity": maturity,
            "skill_gate_failures": gate_failures,
            "skill_version": skill_version,
        })

    issue_rows: list[dict[str, Any]] = []
    for item in visible_issues:
        linked_task_ids = _dedupe([
            *(item.linked_task_ids or []),
            *[row["task_id"] for row in task_rows if item.item_id in row["linked_issue_ids"]],
        ])
        visible_linked_task_ids = _dedupe(linked_task_ids)[:8]
        linked_method_ids = [row["method_id"] for row in method_rows if item.item_id in row["linked_issue_ids"]]
        issue_rows.append({
            "issue_id": item.item_id,
            "source_sheet": "问题清单同步",
            "issue_description": item.title,
            "parent_issue": "",
            "issue_type": _issue_type(item),
            "priority": "",
            "status": item.status.value,
            "issue_source": _first_source_doc(item),
            "registered_at": _first_evidence_locator(item),
            "owner_text": "、".join(_dedupe(item.owner_candidates or [])) or "责任人待确认",
            "department_text": "",
            "planned_resolution_date": item.due_date or "",
            "confirmers": "",
            "notes": item.inference_note or "",
            "linked_task_ids": visible_linked_task_ids,
            "linked_task_titles": _task_titles(visible_linked_task_ids, task_rows),
            "task_count": len(set(linked_task_ids)),
            "current_difficulty": item.description,
            "method_ids": _dedupe(linked_method_ids),
            "evidence_count": len(item.evidence_refs),
        })

    return {
        "summary": {
            "issue_count": len(issues),
            "task_count": full_task_count,
            "method_count": len(methods),
            "linked_issue_count": len([row for row in issue_rows if row["linked_task_ids"]]),
            "unlinked_issue_count": len([row for row in issue_rows if not row["linked_task_ids"]]),
            "method_memory_count": len([item for item in methods if item.status.value == "confirmed"]),
            "reference_sheets": ["问题清单同步", "任务清单同步", "工具方法清单同步"],
        },
        "issues": issue_rows,
        "tasks": task_rows,
        "methods": method_rows,
    }


def _dedupe_inspection_items(items: list[InspectionItem]) -> list[InspectionItem]:
    seen: set[str] = set()
    result: list[InspectionItem] = []
    for item in items:
        if item.item_id in seen:
            continue
        seen.add(item.item_id)
        result.append(item)
    return result


def _method_skill_maturity(
    item: InspectionItem,
    skill: dict[str, Any] | None,
) -> tuple[str, list[str], int]:
    if skill is None:
        readiness = method_skill_readiness(item)
        gate_failures = [
            f"{name}: {message}"
            for name, message in (readiness.get("details") or {}).items()
        ]
        return (
            "memory" if item.status.value == "confirmed" else "candidate",
            gate_failures,
            0,
        )
    latest = skill.get("latest_version") or {}
    active = skill.get("active_version") or {}
    readiness = latest.get("readiness") or {}
    gate_failures = [
        f"{name}: {message}"
        for name, message in (readiness.get("details") or {}).items()
    ]
    version = int(latest.get("version") or 0)
    if active and latest.get("version_id") != active.get("version_id"):
        return "revalidation_required", gate_failures, version
    if skill.get("status") == "published" and active:
        return "published", [], int(active.get("version") or version)
    if latest.get("status") == "tested":
        return "tested", gate_failures, version
    return "candidate", gate_failures, version


def _method_evidence_rows(
    store: ProjectSQLiteStore,
    item: InspectionItem,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for ref in item.evidence_refs:
        source = store.get_source(ref.source_doc_id) or {}
        payload = dict(source.get("payload") or {})
        curated = dict(payload.get("curated_source") or {})
        raw = dict(payload.get("raw_source") or {})
        rows.append({
            "source_doc_id": ref.source_doc_id,
            "meeting_title": str(source.get("title") or ref.source_doc_id),
            "meeting_date": str(source.get("meeting_date") or ""),
            "curated_source": str(curated.get("path") or ""),
            "raw_source": str(raw.get("path") or ""),
            "raw_source_status": str(raw.get("status") or ""),
            "evidence_level": ref.evidence_level,
            "raw_locator": ref.raw_locator,
            "locator": ref.locator,
            "quote": ref.quote,
        })
    return rows


def _is_issue_list_item(item: InspectionItem) -> bool:
    category = item.category.lower()
    if category in {"issue", "open_questions", "question", "questions", "问题"}:
        return True
    if _is_method_list_item(item) or _is_task_list_item(item):
        return False
    facet_types = {facet.lower() for facet in item.facet_types or []}
    return (
        category in {"thing", "things", "chain_gap", "responsibility_gap", "链路", "责任"}
        or bool(facet_types & {"problem", "risk", "dependency"})
    )


def _is_task_list_item(item: InspectionItem) -> bool:
    category = item.category.lower()
    facet_types = {facet.lower() for facet in item.facet_types or []}
    return (
        category in {"followup", "task", "tasks", "todo", "todos", "待办", "追问草稿"}
        or item.item_id.startswith("task_")
        or "task" in facet_types
        or bool(item.due_date or item.deliverable or item.acceptance_criteria)
    )


def _is_method_list_item(item: InspectionItem) -> bool:
    category = item.category.lower()
    return (
        category in {"method", "methods", "法", "方法"}
        or item.item_id.startswith("method_")
        or bool(item.business_goal or item.principles or item.reasoning_chain or item.applicable_scope)
    )


def _linked_issue_ids(
    text: str,
    source_ids: set[str],
    issues: list[InspectionItem],
    *,
    allow_evidence_only: bool = False,
) -> list[str]:
    scored = [
        (issue.item_id, _link_score(text, source_ids, issue, allow_evidence_only=allow_evidence_only))
        for issue in issues
    ]
    return [
        item_id
        for item_id, score in sorted(scored, key=lambda row: (-row[1], row[0]))
        if score >= 0.2
    ][:8]


def _link_score(
    text: str,
    source_ids: set[str],
    target: InspectionItem,
    *,
    allow_evidence_only: bool = False,
) -> float:
    normalized_text = text.lower()
    target_text = _inspection_link_text(target)
    score = 0.0
    if target.title and target.title.lower() in normalized_text:
        score += 0.8
    overlap = _tokens(normalized_text) & _tokens(target_text)
    if overlap:
        score += min(0.5, len(overlap) * 0.12)
    if source_ids and source_ids & _evidence_source_ids(target.evidence_refs):
        score += 0.25 if allow_evidence_only or score else 0.12
    return score


def _inspection_link_text(item: InspectionItem) -> str:
    return " ".join([
        item.item_id,
        item.title,
        item.description,
        item.deliverable or "",
        item.acceptance_criteria or "",
        item.business_goal or "",
        " ".join(item.owner_candidates or []),
        " ".join(item.principles or []),
        " ".join(item.reasoning_chain or []),
    ])


def _work_item_link_text(item: Any) -> str:
    return " ".join([
        item.work_item_id,
        item.source_candidate_id,
        item.title,
        item.description,
        item.deliverable or "",
        item.acceptance_criteria or "",
        " ".join(item.owner_candidates or []),
    ])


def _task_row_link_text(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(key, "")) for key in ["task_id", "task_description", "task_detail", "deliverable", "acceptance_criteria"])


def _evidence_source_ids(evidence_refs: list[EvidenceRef]) -> set[str]:
    return {ref.source_doc_id for ref in evidence_refs if ref.source_doc_id}


def _source_batch_id(source_doc_ids: list[str], notes: str = "") -> str:
    meeting_source = next(
        (source_id for source_id in source_doc_ids if "meeting" in source_id.lower()),
        "",
    )
    if meeting_source:
        return meeting_source
    meeting_date = re.search(r"meeting\s+(\d{4})-(\d{2})-(\d{2})", notes, flags=re.IGNORECASE)
    if meeting_date:
        return f"meeting-{''.join(meeting_date.groups())}"
    return source_doc_ids[0] if source_doc_ids else ""


def _tokens(value: str) -> set[str]:
    normalized = re.sub(r"[\s，。；：、/\\\-_\(\)（）【】]+", " ", value.lower())
    tokens = {token for token in normalized.split() if len(token) >= 2}
    for shard in [
        "问题", "任务", "方法", "清单", "标准层", "数据结构", "里程碑",
        "责任", "验收", "进度", "交付物", "接口", "字段", "权限",
    ]:
        if shard in value:
            tokens.add(shard)
    return tokens


def _issue_titles(issue_ids: list[str], issues: list[InspectionItem]) -> list[str]:
    by_id = {item.item_id: item.title for item in issues}
    return [by_id[item_id] for item_id in issue_ids if item_id in by_id]


def _task_titles(task_ids: list[str], tasks: list[dict[str, Any]]) -> list[str]:
    by_id = {item["task_id"]: item["task_description"] for item in tasks}
    return [by_id[item_id] for item_id in _dedupe(task_ids) if item_id in by_id]


def _issue_type(item: InspectionItem) -> str:
    if item.matter_type:
        return item.matter_type
    if item.facet_types:
        return "、".join(item.facet_types)
    return item.category


def _first_source_doc(item: InspectionItem) -> str:
    return item.evidence_refs[0].source_doc_id if item.evidence_refs else ""


def _first_evidence_locator(item: InspectionItem) -> str:
    return item.evidence_refs[0].locator if item.evidence_refs else ""


def _build_milestone_workspace(
    run: AgentRun | None,
    store: ProjectSQLiteStore,
    milestone: MilestonePlan,
    *,
    work_items: list[Any] | None = None,
    candidate_items: list[InspectionItem] | None = None,
) -> dict[str, Any]:
    root_row_id = milestone.milestone_id
    work_items = [
        item
        for item in (work_items if work_items is not None else store.list_work_items())
        if not item.milestone_id or item.milestone_id == milestone.milestone_id
        if item.status.value != "archived"
    ]
    candidate_items = [item for item in (candidate_items if candidate_items is not None else _run_report_items(run)) if item.status.value != "archived"]
    max_task_rows = 240
    work_items = work_items[:max_task_rows]
    candidate_items = candidate_items[:max(0, max_task_rows - len(work_items))]
    work_titles = [item.title for item in work_items]
    candidate_titles = [item.title for item in candidate_items]
    linked_ids = [item.work_item_id for item in work_items] + [item.item_id for item in candidate_items]
    linked_titles = work_titles + candidate_titles
    rows: list[dict[str, Any]] = [
        {
            "row_id": root_row_id,
            "level": 0,
            "parent_id": "",
            "row_type": "milestone",
            "title": milestone.name,
            "date_start": milestone.date_start,
            "date_end": milestone.date_end,
            "due_date": milestone.date_end,
            "owner_text": "里程碑负责人待人工指定",
            "status": milestone.status,
            "deliverable": "；".join(milestone.acceptance_criteria),
            "acceptance_criteria": "\n".join(milestone.acceptance_criteria),
            "planned_start": milestone.date_start,
            "professional_id": "",
            "board_id": "",
            "progress_percent": 0,
            "linked_task_ids": linked_ids,
            "linked_task_titles": linked_titles,
            "editable": True,
        }
    ]
    for item in work_items:
        rows.append({
            "row_id": item.work_item_id,
            "level": 1,
            "parent_id": root_row_id,
            "row_type": "work_item",
            "title": item.title,
            "date_start": item.planned_start or str(item.created_at or "")[:10],
            "date_end": item.due_date or "",
            "due_date": item.due_date or "",
            "owner_text": "、".join(_dedupe(item.owner_candidates or [])) or "责任人待确认",
            "status": item.status.value,
            "deliverable": item.deliverable or "",
            "acceptance_criteria": item.acceptance_criteria or "",
            "planned_start": item.planned_start,
            "professional_id": item.professional_id,
            "board_id": item.board_id,
            "progress_percent": item.progress_percent,
            "linked_task_ids": [item.work_item_id],
            "linked_task_titles": [item.title],
            "editable": True,
        })
    for item in candidate_items:
        rows.append({
            "row_id": item.item_id,
            "level": 1,
            "parent_id": root_row_id,
            "row_type": "candidate_task",
            "title": item.title,
            "date_start": "",
            "date_end": item.due_date or "",
            "due_date": item.due_date or "",
            "owner_text": "、".join(_dedupe(item.owner_candidates or [])) or "责任人待确认",
            "status": item.status.value,
            "deliverable": item.deliverable or "",
            "acceptance_criteria": item.acceptance_criteria or "",
            "planned_start": "",
            "professional_id": "",
            "board_id": "",
            "progress_percent": 0,
            "linked_task_ids": [item.item_id],
            "linked_task_titles": [item.title],
            "editable": False,
        })
    deliverable_count = len([row for row in rows[1:] if row["deliverable"]])
    return {
        "summary": {
            "milestone_id": milestone.milestone_id,
            "name": milestone.name,
            "date_start": milestone.date_start,
            "date_end": milestone.date_end,
            "linked_task_count": len(rows) - 1,
            "formal_task_count": len(work_items),
            "candidate_task_count": len(candidate_items),
            "deliverable_count": deliverable_count,
        },
        "rows": rows,
    }


def _build_people_workspace(
    run: AgentRun | None,
    store: ProjectSQLiteStore,
    *,
    people_asset: PeopleStructure,
    work_items: list[Any] | None = None,
    candidate_items: list[InspectionItem] | None = None,
    include_all_people: bool = True,
) -> dict[str, Any]:
    work_items = [item for item in (work_items if work_items is not None else store.list_work_items()) if item.status.value != "archived"]
    candidate_items = [item for item in (candidate_items if candidate_items is not None else store.list_items()) if item.status.value != "archived"]
    people_rows: list[dict[str, Any]] = []
    for person in people_asset.people:
        source_group = str(person.group or "").strip() or "待归类"
        taxonomy_group = classify_person_group(person)
        related_work_items = [
            item
            for item in work_items
            if _person_matches_work_item(person.name, item)
        ]
        related_candidate_items = [
            item
            for item in candidate_items
            if _person_matches_candidate_item(person.name, item)
        ]
        active_count = len(related_work_items) + len(related_candidate_items)
        if not include_all_people and active_count == 0:
            continue
        responsibility_summary = person.responsibility_note or _default_responsibility_summary(
            group=taxonomy_group if taxonomy_group != "待归类" else source_group,
            role=person.role,
            active_count=active_count,
        )
        people_rows.append({
            "person_id": person.person_id,
            "name": person.name,
            "identity_status": person.identity_status,
            "group": source_group,
            "taxonomy_group": taxonomy_group,
            "role": person.role,
            "path": person.path,
            "source_id": person.source_id,
            "responsibility_note": person.responsibility_note,
            "responsibility_summary": responsibility_summary,
            "assignments": [to_plain(assignment) for assignment in person.assignments],
            "active_work_count": active_count,
            "work_items": [
                {
                    "work_item_id": item.work_item_id,
                    "title": item.title,
                    "status": item.status.value,
                    "due_date": item.due_date or "",
                    "deliverable": item.deliverable or "",
                }
                for item in related_work_items[:8]
            ],
            "candidate_items": [
                {
                    "item_id": item.item_id,
                    "title": item.title,
                    "status": item.status.value,
                    "category": item.category,
                    "due_date": item.due_date or "",
                    "deliverable": item.deliverable or "",
                }
                for item in related_candidate_items[:8]
            ],
            "editable": True,
        })
    group_priority = {
        str(person.group or "").strip() or "待归类": index
        for index, person in enumerate(people_asset.people)
    }
    role_priority = {"专题负责人": 0, "专业统筹": 1, "PMO": 2, "实施人员": 3}
    people_rows.sort(key=lambda row: (
        group_priority.get(row["group"], len(group_priority)),
        role_priority.get(str(row["role"]).strip(), 4),
        row["name"],
    ))
    declared_group_names = [group.name for group in people_asset.groups if group.name]
    for row in people_rows:
        if row["group"] not in declared_group_names:
            declared_group_names.append(row["group"])
    group_rows = [
        {
            "name": name,
            "path": next((group.path for group in people_asset.groups if group.name == name), name),
            "people_count": sum(1 for row in people_rows if row["group"] == name),
            "editable": True,
        }
        for name in declared_group_names
    ]
    return {
        "summary": {
            "root_title": people_asset.root_title,
            "people_count": len(people_rows),
            "active_work_total": sum(row["active_work_count"] for row in people_rows),
            "group_count": len(group_rows),
            "scenario_count": len(people_asset.scenarios),
            "duplicate_review_count": sum(
                1 for row in people_rows if row["identity_status"] == "needs_merge"
            ),
        },
        "groups": group_rows,
        "people": people_rows,
    }


def _empty_people_workspace() -> dict[str, Any]:
    return {
        "summary": {
            "root_title": "当前身份不展示跨人负载",
            "people_count": 0,
            "active_work_total": 0,
            "group_count": 0,
            "scenario_count": 0,
            "duplicate_review_count": 0,
        },
        "groups": [],
        "people": [],
    }


def _person_matches_work_item(name: str, item: Any) -> bool:
    owners = list(item.owner_candidates or []) + list(item.collaborators or []) + list(item.confirmers or [])
    return name in owners or name in item.title or name in item.description


def _person_matches_candidate_item(name: str, item: InspectionItem) -> bool:
    owners = item.owner_candidates or []
    return name in owners or name in item.title or name in item.description


def _default_responsibility_summary(*, group: str, role: str, active_count: int) -> str:
    base = "，".join(part for part in [group, role] if part)
    if not base:
        base = "责任信息待补"
    if active_count:
        return f"{base}；当前关联 {active_count} 项进行中/候选工作。"
    return f"{base}；当前暂无系统关联工作。"


def _context_refs_for_trace(trace: dict[str, Any], harness: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    context_ref = trace.get("context_ref")
    if context_ref:
        refs.append(f"上下文引用：{context_ref}")
    data = trace.get("data") if isinstance(trace.get("data"), dict) else {}
    summary = data.get("summary") if isinstance(data, dict) else ""
    if summary:
        refs.append(f"摘要：{summary}")
    context_window = harness.get("context_window", {})
    if isinstance(context_window, dict) and context_ref:
        if context_ref in context_window.get("active_refs", []):
            refs.append("上下文窗口：活动上下文")
        if context_ref in context_window.get("archived_refs", []):
            refs.append("上下文窗口：已归档")
    return refs or ["使用当前运行上下文"]


def _preview_data(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 12:
                result["..."] = "已截断，完整内容见 project.db"
                break
            result[str(key)] = _preview_data(item, depth + 1)
        return result
    if isinstance(value, list):
        result = [_preview_data(item, depth + 1) for item in value[:6]]
        if len(value) > 6:
            result.append(f"... 已截断 {len(value) - 6} 项，完整内容见 project.db")
        return result
    if isinstance(value, str) and len(value) > 700:
        return value[:700] + "...（已截断，完整内容见 project.db）"
    return value


def _build_confirmation_cards(
    run: AgentRun | None,
    manifest: list[SourceDocument],
    store: ProjectSQLiteStore,
    *,
    visible_items: list[InspectionItem] | None = None,
    visible_run_items: list[InspectionItem] | None = None,
) -> list[dict[str, Any]]:
    source_by_doc = {source.doc_id: source for source in manifest}
    cards: list[dict[str, Any]] = []
    persisted_items = visible_items if visible_items is not None else store.list_items()
    persisted_by_id = {item.item_id: item for item in persisted_items}
    all_persisted_ids = {item.item_id for item in store.list_items()}
    stored_by_id = {
        item_id: item
        for item_id, item in persisted_by_id.items()
        if item.status.value == "candidate"
    }
    visible_run_by_id = {
        item.item_id: item
        for item in (visible_run_items if visible_run_items is not None else _run_report_items(run))
        if item.status.value == "candidate" and item.item_id not in all_persisted_ids
    }
    seen: set[str] = set()
    if run is not None and run.final_report is not None:
        for section, items in [
            ("链路缺口", run.final_report.chain_gaps),
            ("责任缺口", run.final_report.responsibility_gaps),
            ("追问草稿", run.final_report.followup_drafts),
        ]:
            for item in items:
                current = stored_by_id.get(item.item_id) or visible_run_by_id.get(item.item_id)
                if current is None:
                    continue
                cards.append(_card_from_item(section, current, source_by_doc))
                seen.add(item.item_id)
        for key, section in [
            ("people", "人"),
            ("things", "事"),
            ("methods", "法"),
            ("tasks", "待办"),
            ("open_questions", "待判断"),
        ]:
            for row in run.structured_output.get(key, []):
                item_id = row.get("item_id", "")
                if not item_id or item_id in seen or item_id not in stored_by_id:
                    continue
                cards.append(_card_from_item(section, stored_by_id[item_id], source_by_doc))
                seen.add(item_id)
    for item in sorted(stored_by_id.values(), key=lambda value: (value.updated_at or "", value.item_id)):
        if item.item_id in seen:
            continue
        cards.append(_card_from_item(_candidate_section(item), item, source_by_doc))
        seen.add(item.item_id)
    return cards


def _run_status(run: AgentRun | None) -> dict[str, Any]:
    if run is None:
        return {
            "status": "idle",
            "runtime_kind": "",
            "error": "",
            "raw_response_count": 0,
            "stop_reason": "",
            "verification": {
                "ok": False,
                "checked_count": 0,
                "errors": [],
                "warnings": [],
                "raw_evidence_count": 0,
                "raw_pending_count": 0,
            },
        }
    verification = run.verification
    return {
        "status": run.status,
        "runtime_kind": run.runtime_kind,
        "error": run.error,
        "raw_response_count": run.raw_response_count,
        "stop_reason": run.stop_reason,
        "verification": to_plain(verification) if verification else {
            "ok": False,
            "checked_count": 0,
            "errors": [],
            "warnings": [],
            "raw_evidence_count": 0,
            "raw_pending_count": 0,
        },
    }


def _build_input_status(
    manifest: list[SourceDocument],
    people_asset: PeopleStructure,
    ingestion_jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    uploaded = [source for source in manifest if source.doc_id.startswith("uploaded_")]
    return {
        "source_counts": {
            "total": len(manifest),
            "uploaded": len(uploaded),
            "curated_ready": sum(1 for source in manifest if source.curated_source.path),
            "raw_matched": sum(1 for source in manifest if source.raw_source.status == "matched"),
            "raw_pending": sum(1 for source in manifest if source.raw_source.status != "matched"),
        },
        "uploaded_sources": [
            {
                "doc_id": source.doc_id,
                "title": source.title,
                "meeting_date": source.meeting_date,
                "curated_status": source.curated_source.status,
                "raw_status": source.raw_source.status,
            }
            for source in uploaded[-6:]
        ],
        "ingestion_jobs": [ingestion_job_view(job) for job in ingestion_jobs],
        "source_documents": [
            {
                "doc_id": source.doc_id,
                "title": source.title,
                "meeting_date": source.meeting_date,
                "curated_status": source.curated_source.status,
                "raw_status": source.raw_source.status,
            }
            for source in manifest
        ],
        "upload_dir": str(upload_root()),
        "obsidian": obsidian_sync_status(),
        "people_asset": {
            "path": "project-scoped people_structure source" if people_asset.people else "",
            "root_title": people_asset.root_title,
            "group_count": len(people_asset.groups),
            "people_count": len(people_asset.people),
            "scenario_count": len(people_asset.scenarios),
            "scenario_ids": [scenario.scenario_id for scenario in people_asset.scenarios],
        },
    }


def ingestion_job_view(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    total_chunks = max(0, int(job.get("total_chunks") or 0))
    processed_chunks = max(0, int(job.get("processed_chunks") or 0))
    progress_percent = (
        min(100, round(processed_chunks * 100 / total_chunks))
        if total_chunks
        else (100 if job.get("status") == "completed" else 0)
    )
    return {
        "id": str(job.get("id") or ""),
        "source_id": str(job.get("source_id") or ""),
        "source_title": str(payload.get("source_title") or job.get("source_id") or ""),
        "project_id": str(job.get("project_id") or ""),
        "topic_id": job.get("topic_id") or None,
        "sensitivity": str(job.get("sensitivity") or "l1"),
        "input_kind": str(job.get("input_kind") or "auto"),
        "status": str(job.get("status") or "queued"),
        "stage": str(job.get("stage") or "queued"),
        "total_chunks": total_chunks,
        "processed_chunks": processed_chunks,
        "progress_percent": progress_percent,
        "candidate_count": int(job.get("candidate_count") or 0),
        "delta_new": int(job.get("delta_new") or 0),
        "delta_updated": int(job.get("delta_updated") or 0),
        "delta_conflict": int(job.get("delta_conflict") or 0),
        "delta_resolved": int(job.get("delta_resolved") or 0),
        "delta_auto_merged": int(job.get("delta_auto_merged") or 0),
        "row_count": int(payload.get("row_count") or 0),
        "import_summary": str(payload.get("import_summary") or ""),
        "attempts": int(job.get("attempts") or 0),
        "max_attempts": int(job.get("max_attempts") or 0),
        "error": str(job.get("error") or ""),
        "created_at": str(job.get("created_at") or ""),
        "updated_at": str(job.get("updated_at") or ""),
    }


def _build_sedimentation_summary(
    run: AgentRun | None,
    manifest: list[SourceDocument],
    store: ProjectSQLiteStore,
    *,
    items: list[InspectionItem] | None = None,
    followups: list[InspectionItem] | None = None,
) -> dict[str, Any]:
    visible_items = items if items is not None else store.list_items()
    if run and run.structured_output:
        return _sedimentation_from_structured_output(
            run.structured_output,
            {item.item_id: item for item in visible_items},
        )
    active_items = [item for item in visible_items if item.status.value != "archived"]
    people = _sediment_items(
        item for item in active_items if item.category in {"people", "person"}
    )
    things = _sediment_items(
        item
        for item in active_items
        if item.category in {"things", "thing", "issue", "chain_gap", "responsibility_gap"}
    )
    methods = _sediment_items(
        item for item in active_items if item.category in {"methods", "method"}
    )
    todos = _sediment_items(
        item
        for item in active_items
        if item.category in {"tasks", "task", "followup", "questions", "question"}
    )
    if run and run.final_report:
        todos.extend(
            _sediment_from_inspection(item)
            for item in (followups if followups is not None else run.final_report.followup_drafts)
        )
    return {
        "people": {
            "label": "人",
            "count": len(people),
            "items": people[:6],
        },
        "things": {
            "label": "事",
            "count": len(things),
            "items": things[:6],
        },
        "methods": {
            "label": "法",
            "count": len(methods),
            "items": methods[:6],
        },
        "todos": {
            "label": "待办",
            "count": len(todos),
            "items": todos[:8],
        },
    }


def _build_rounds(run: AgentRun | None) -> list[dict[str, Any]]:
    if run is None:
        return []
    if run.loop_rounds:
        return [
            {
                "round_index": item.round_index,
                "step_id": f"loop_round_{item.round_index:02d}",
                "phase": item.phase,
                "objective": item.objective,
                "tool_name": item.tool_name,
                "status": "failed" if item.errors else "completed",
                "attempts": 1 if item.tool_name else 0,
                "input_summary": item.model_input_summary,
                "output_summary": item.tool_result_summary,
                "model_result": item.model_output_summary,
                "decision": item.decision,
                "state_changes": item.state_changes,
                "stop_reason": item.stop_reason,
                "round_kind": item.round_kind,
                "raw_response_id": item.raw_response_id,
                "token_usage": item.token_usage,
                "tool_calls": item.tool_calls,
                "events": [
                    {
                        "event_type": "state_change",
                        "status": "ok",
                        "message": change,
                    }
                    for change in item.state_changes
                ],
                "error": "; ".join(item.errors),
            }
            for item in run.loop_rounds
        ]
    harness = _harness_state(run)
    traces = list(run.agent_trace)
    events = harness.get("events", [])
    tasks = harness.get("tasks") or [
        {
            "step_id": step.step_id,
            "tool_name": step.tool_name,
            "status": step.status,
            "attempts": 0,
            "input_summary": step.input_summary,
            "output_summary": step.output_summary,
            "error": "",
        }
        for step in run.steps
    ]
    rounds: list[dict[str, Any]] = []
    for index, task in enumerate(tasks, start=1):
        trace = next(
            (item for item in traces if item.get("name") == task.get("tool_name")),
            {},
        )
        step_events = [
            {
                "event_type": event.get("event_type", ""),
                "status": event.get("status", ""),
                "message": event.get("message", ""),
            }
            for event in events
            if event.get("step_id") == task.get("step_id")
        ]
        rounds.append(
            {
                "round_index": index,
                "step_id": task.get("step_id", ""),
                "tool_name": task.get("tool_name", ""),
                "status": task.get("status", ""),
                "attempts": task.get("attempts", 0),
                "input_summary": task.get("input_summary", ""),
                "output_summary": task.get("output_summary", ""),
                "model_result": trace.get("output_summary") or task.get("output_summary", ""),
                "events": step_events,
                "error": task.get("error", ""),
                "round_kind": "legacy_tool",
                "raw_response_id": "",
                "token_usage": {},
                "tool_calls": [],
            }
        )
    return rounds


def _sediment_items(items: Any) -> list[dict[str, Any]]:
    rows = []
    seen: set[str] = set()
    for item in items:
        if item.item_id in seen:
            continue
        seen.add(item.item_id)
        rows.append(_sediment_from_inspection(item))
    return rows


def _sediment_from_inspection(item: Any) -> dict[str, Any]:
    ref = item.evidence_refs[0] if item.evidence_refs else None
    return {
        "item_id": item.item_id,
        "title": item.title,
        "description": item.description,
        "owners": _dedupe(item.owner_candidates or []),
        "source_doc_id": ref.source_doc_id if ref else "",
        "quote": ref.quote if ref else "",
    }


def _empty_harness_state() -> dict[str, Any]:
    return {
        "phase": "idle",
        "max_steps": 0,
        "completed_steps": 0,
        "failure_count": 0,
        "max_tool_attempts": 0,
        "tasks": [],
        "events": [],
        "hooks": [],
        "context_window": {
            "max_active_chars": 0,
            "active_chars": 0,
            "active_count": 0,
            "archived_count": 0,
            "active_refs": [],
            "archived_refs": [],
            "last_summary": "",
        },
    }


def _harness_state(run: AgentRun | None) -> dict[str, Any]:
    if run is None:
        return _empty_harness_state()
    if run.harness_state:
        return run.harness_state
    state = _empty_harness_state()
    state["phase"] = "legacy"
    state["max_steps"] = len(run.plan)
    state["completed_steps"] = len([step for step in run.steps if step.status == "completed"])
    state["tasks"] = [
        {
            "step_id": step.step_id,
            "tool_name": step.tool_name,
            "reason": "",
            "status": step.status,
            "attempts": 0,
            "input_summary": step.input_summary,
            "output_summary": step.output_summary,
            "error": "",
            "updated_at": "",
        }
        for step in run.steps
    ]
    return state


def _workspace_milestone(run: AgentRun | None, milestone: MilestonePlan | None) -> MilestonePlan:
    if milestone is not None:
        return milestone
    if run is not None:
        return run.milestone_plan
    raise ValueError("milestone is required when workspace has no run")


def _card_from_item(
    section: str,
    item: InspectionItem,
    source_by_doc: dict[str, SourceDocument],
) -> dict[str, Any]:
    owners = _dedupe(item.owner_candidates or [])
    evidence_rows = [
        _evidence_row(ref, source_by_doc)
        for ref in item.evidence_refs
    ]
    field_labels = {
        "title": "任务事项",
        "description": "任务说明",
        "owner_candidates": "负责人",
        "due_date": "截止时间",
        "deliverable": "交付物",
        "acceptance_criteria": "验收口径",
    }
    confirmation_field_keys = list(item.proposed_fields or [])
    observed_field_keys = list(item.observed_fields or [])
    if item.category in {"task", "tasks", "followup"}:
        # 任务事项是候选任务的标识与编辑入口，不作为需要人工补充确认的业务字段。
        confirmation_field_keys = [field for field in confirmation_field_keys if field != "title"]
        classified_fields = set(confirmation_field_keys + observed_field_keys)
        required_task_fields = [
            "description",
            "deliverable",
            "acceptance_criteria",
            "due_date",
            "owner_candidates",
        ]
        confirmation_field_keys.extend(
            field for field in required_task_fields if field not in classified_fields
        )
    confirmation_fields = [
        field_labels.get(field, field)
        for field in confirmation_field_keys
    ]
    observed_fields = [
        field_labels.get(field, field)
        for field in observed_field_keys
    ]
    return {
        "section": section,
        "category": item.category,
        "storage_item_id": item.item_id,
        "status": item.status.value,
        "title": item.title,
        "agent_understanding": item.description,
        "owner_candidates": owners,
        "owner_text": "、".join(owners) if owners else "未识别，需要人工补",
        "next_step": item.next_step or "",
        "matter_type": item.matter_type or "",
        "facet_types": item.facet_types or [],
        "task_contract": {
            "due_date": item.due_date or "",
            "deliverable": item.deliverable or "",
            "acceptance_criteria": item.acceptance_criteria or "",
        },
        "methodology": {
            "business_goal": item.business_goal or "",
            "principles": item.principles or [],
            "reasoning_chain": item.reasoning_chain or [],
            "applicable_scope": item.applicable_scope or "",
        },
        "inference_note": item.inference_note or "",
        "confirmation_fields": confirmation_fields,
        "observed_fields": observed_fields,
        "confirmation_note": (
            f"需要人工确认：{'、'.join(confirmation_fields)}。"
            if confirmation_fields
            else (
                "无需补充待确认字段，可直接发布或编辑。"
                if item.category in {"task", "tasks", "followup"}
                else "请确认候选内容是否应进入正式项目记录。"
            )
        ),
        "evidence": evidence_rows,
        "confirmation_question": _confirmation_question(section, item),
        "raw": to_plain(item),
    }


def _candidate_section(item: InspectionItem) -> str:
    return {
        "people": "人",
        "person": "人",
        "things": "事",
        "thing": "事",
        "methods": "法",
        "method": "法",
        "tasks": "待办",
        "task": "待办",
        "followup": "待办",
        "questions": "待判断",
        "question": "待判断",
        "issue": "问题",
    }.get(item.category, "待确认")


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _confirmation_question(section: str, item: InspectionItem) -> str:
    if section == "追问草稿":
        return f"你要确认：这条追问要不要发出去？责任人是否是 {', '.join(item.owner_candidates or ['待补'])}？"
    return (
        "你要确认：这个判断是否成立？责任人是否正确？"
        "证据是否够支撑从 candidate 升级为 confirmed？"
    )


def _evidence_row(
    ref: EvidenceRef,
    source_by_doc: dict[str, SourceDocument],
) -> dict[str, str]:
    source = source_by_doc.get(ref.source_doc_id)
    return {
        "source_doc_id": ref.source_doc_id,
        "meeting_title": source.title if source else ref.source_doc_id,
        "meeting_date": source.meeting_date if source else "",
        "curated_source": source.curated_source.path if source and source.curated_source.path else "",
        "raw_source": source.raw_source.path if source and source.raw_source.path else "",
        "raw_source_status": source.raw_source.status if source else "",
        "evidence_level": ref.evidence_level,
        "raw_locator": ref.raw_locator,
        "locator": ref.locator,
        "quote": ref.quote,
    }


def _sedimentation_from_structured_output(
    output: dict[str, Any],
    stored_by_id: dict[str, InspectionItem],
) -> dict[str, Any]:
    sections = {
        "people": ("\u4eba", output.get("people", []), 6),
        "things": ("\u4e8b", output.get("things", []), 6),
        "methods": ("\u6cd5", output.get("methods", []), 6),
        "todos": (
            "\u5f85\u529e",
            _dedupe_plain_items(
                list(output.get("tasks", [])) + list(output.get("open_questions", []))
            ),
            8,
        ),
    }
    result: dict[str, Any] = {}
    for key, (label, items, limit) in sections.items():
        rows = [
            _plain_sediment_item(item, stored_by_id.get(item.get("item_id", "")))
            for item in items
            if item.get("item_id", "") in stored_by_id
        ]
        result[key] = {
            "label": label,
            "count": len(rows),
            "items": rows[:limit],
        }
    return result


def _plain_sediment_item(
    item: dict[str, Any],
    stored: InspectionItem | None = None,
) -> dict[str, Any]:
    evidence_refs = item.get("evidence_refs", [])
    ref = evidence_refs[0] if evidence_refs else {}
    return {
        "item_id": item.get("item_id", ""),
        "title": stored.title if stored else item.get("title", ""),
        "description": stored.description if stored else item.get("description", ""),
        "owners": _dedupe(list((stored.owner_candidates or []) if stored else (item.get("owner_candidates") or []))),
        "status": stored.status.value if stored else item.get("status", "candidate"),
        "matter_type": item.get("matter_type", ""),
        "facet_types": item.get("facet_types", []),
        "due_date": item.get("due_date", ""),
        "deliverable": item.get("deliverable", ""),
        "acceptance_criteria": item.get("acceptance_criteria", ""),
        "business_goal": item.get("business_goal", ""),
        "principles": item.get("principles", []),
        "reasoning_chain": item.get("reasoning_chain", []),
        "source_doc_id": ref.get("source_doc_id", ""),
        "quote": ref.get("quote", ""),
    }


def _dedupe_plain_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        item_id = item.get("item_id", "")
        if item_id and item_id in seen:
            continue
        if item_id:
            seen.add(item_id)
        result.append(item)
    return result
