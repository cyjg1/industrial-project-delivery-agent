from __future__ import annotations

from dataclasses import replace
from typing import Any

from agent.project_config import ProjectConfig, generic_project_config
from agent.schemas import MilestonePlan, to_plain


def load_project_context(store: Any, project_id: str) -> ProjectConfig:
    """Load one project's runtime context exclusively from SQLite-backed state."""
    project = store.get_project(project_id)
    if project is None:
        raise KeyError(f"Unknown project: {project_id}")

    stored = store.get_project_config(project_id)
    if stored is None:
        default = generic_project_config()
        milestone = replace(
            default.default_milestone,
            milestone_id=f"{project_id}_delivery_inspection",
            project=project["name"],
        )
        stored = store.upsert_project_config(
            project_id,
            org_id=project["org_id"],
            source_profile="generic",
            milestone_payload=to_plain(milestone),
            actor_id=project["owner_id"],
            migration_origin="generated_default",
        )

    milestone = _milestone_from_payload(stored["milestone_payload"], project_name=project["name"])
    return ProjectConfig(
        project_id=project_id,
        project_name=project["name"],
        source_profile=stored["source_profile"],
        default_milestone=milestone,
    )


def update_project_context(
    store: Any,
    project_id: str,
    updates: dict[str, Any],
    *,
    actor_id: str,
) -> ProjectConfig:
    context = load_project_context(store, project_id)
    milestone = context.default_milestone
    allowed_fields = {
        "milestone_id",
        "project",
        "name",
        "date_start",
        "date_end",
        "scenario_id",
        "chain_name",
        "acceptance_criteria",
        "trigger_policy",
        "status",
    }
    normalized: dict[str, Any] = {}
    for key, value in updates.items():
        if key not in allowed_fields or value is None:
            continue
        if key == "acceptance_criteria":
            normalized[key] = _normalize_acceptance_criteria(value)
        elif key == "trigger_policy":
            normalized[key] = dict(value)
        else:
            normalized[key] = str(value)

    next_milestone = replace(milestone, **normalized)
    project = store.get_project(project_id)
    if project is None:
        raise KeyError(f"Unknown project: {project_id}")
    project_name = next_milestone.project or project["name"]
    if project_name != project["name"]:
        store.upsert_project(project_id, project["org_id"], project_name, project["owner_id"])
    store.upsert_project_config(
        project_id,
        org_id=project["org_id"],
        source_profile=context.source_profile,
        milestone_payload=to_plain(next_milestone),
        actor_id=actor_id,
    )
    return load_project_context(store, project_id)


def list_project_contexts(store: Any) -> list[ProjectConfig]:
    return [load_project_context(store, project["id"]) for project in store.list_projects()]


def _milestone_from_payload(payload: dict[str, Any], *, project_name: str) -> MilestonePlan:
    default = generic_project_config().default_milestone
    return MilestonePlan(
        milestone_id=str(payload.get("milestone_id") or default.milestone_id),
        project=str(payload.get("project") or project_name),
        name=str(payload.get("name") or default.name),
        date_start=str(payload.get("date_start") or default.date_start),
        date_end=str(payload.get("date_end") or default.date_end),
        scenario_id=str(payload.get("scenario_id") or default.scenario_id),
        chain_name=str(payload.get("chain_name") or default.chain_name),
        acceptance_criteria=_normalize_acceptance_criteria(
            payload.get("acceptance_criteria") or default.acceptance_criteria
        ),
        trigger_policy=dict(payload.get("trigger_policy") or default.trigger_policy),
        status=str(payload.get("status") or "active"),
    )


def _normalize_acceptance_criteria(value: Any) -> list[str]:
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []
