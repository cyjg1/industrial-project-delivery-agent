from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent.access_policy import AccessContext, User, visible_filter
from agent.people_directory import normalize_people_structure, sync_people_directory
from agent.repository_paths import resolve_repository_path
from agent.schemas import PeopleStructure
from ingestion.people_asset import load_people_asset, load_people_structure_from_table
from ingestion.xmind_loader import load_people_structure_from_xmind


def load_project_people(
    store: Any,
    project_id: str,
    *,
    actor: User | None = None,
    access_context: AccessContext | None = None,
) -> PeopleStructure:
    """Resolve the people asset from a tagged source owned by the active project."""
    sources = _project_people_sources(
        store,
        project_id,
        actor=actor,
        access_context=access_context,
    )
    if not sources:
        if os.getenv("PROJECT_AGENT_LEGACY_FIXTURE_MODE", "").strip().lower() == "on":
            return normalize_people_structure(load_people_asset(), project_id)
        return empty_people_structure()

    source = sources[-1]
    payload = source.get("payload") or {}
    path = _asset_path(payload, source, allowed_roots=_store_roots(store))
    if not path:
        raise RuntimeError(
            f"People source {source.get('id', '')} has no parseable asset path; re-import it for project {project_id}."
        )
    if not path.is_file():
        raise RuntimeError(f"People asset for project {project_id} is missing: {path}")

    suffix = path.suffix.lower()
    if suffix == ".json":
        structure = load_people_asset(path)
    elif suffix == ".xmind":
        structure = load_people_structure_from_xmind(path)
    elif suffix in {".csv", ".xlsx", ".xlsm"}:
        structure = load_people_structure_from_table(path)
    else:
        raise RuntimeError(f"Unsupported people asset for project {project_id}: {path.suffix}")
    normalized = normalize_people_structure(structure, project_id)
    source_id = str(source.get("id") or source.get("doc_id") or "")
    for person in normalized.people:
        person.source_id = source_id
        for assignment in person.assignments:
            if not assignment.source_id:
                assignment.source_id = source_id
    sync_people_directory(
        store,
        normalized,
        org_id=str(source.get("org_id") or ""),
        project_id=project_id,
        source_id=source_id,
        author_id=str(source.get("author_id") or ""),
        sensitivity=str(source.get("sensitivity") or "l1"),
    )
    return normalized


def project_people_asset_path(
    store: Any,
    project_id: str,
    *,
    actor: User | None = None,
    access_context: AccessContext | None = None,
) -> Path | None:
    sources = _project_people_sources(
        store,
        project_id,
        actor=actor,
        access_context=access_context,
    )
    if not sources:
        return None
    return _asset_path(
        sources[-1].get("payload") or {},
        sources[-1],
        allowed_roots=_store_roots(store),
    )


def empty_people_structure() -> PeopleStructure:
    return PeopleStructure(root_title="人员资产待导入", groups=[], people=[], scenarios=[])


def _project_people_sources(
    store: Any,
    project_id: str,
    *,
    actor: User | None,
    access_context: AccessContext | None,
) -> list[dict[str, Any]]:
    sources = [
        source
        for source in store.list_sources()
        if source.get("project_id") == project_id and source.get("kind") == "people_structure"
    ]
    if actor is not None and access_context is not None:
        sources = visible_filter(actor, sources, access_context)
    return sources


def _asset_path(
    payload: dict[str, Any],
    source: dict[str, Any],
    *,
    allowed_roots: tuple[Path, ...] = (),
) -> Path | None:
    direct = str(payload.get("people_asset_path") or "").strip()
    if direct:
        return resolve_repository_path(direct, allowed_roots=allowed_roots)
    for key in ("curated_source", "raw_source"):
        value = payload.get(key)
        if isinstance(value, dict) and str(value.get("path") or "").strip():
            return resolve_repository_path(str(value["path"]), allowed_roots=allowed_roots)
    stored = str(source.get("path") or "").strip()
    return resolve_repository_path(stored, allowed_roots=allowed_roots) if stored else None


def _store_roots(store: Any) -> tuple[Path, ...]:
    root_dir = getattr(store, "root_dir", None)
    return (Path(root_dir),) if root_dir else ()
