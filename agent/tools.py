from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent.access_policy import AccessContext, User, visible, visible_filter
from agent.inspection import inspect_delivery_chain
from agent.llm_provider import get_provider
from agent.project_people import load_project_people
from agent.schemas import CandidateStatus, EvidenceVerificationResult, to_plain
from agent.semantic_extraction import extract_document_semantically
from agent.tool_registry import ToolRegistry, ToolSpec, register_specs
from agent.tool_registry import INSPECTION_SURFACE
from agent.verifier import verify_report_evidence
from ingestion.source_manifest import (
    build_project_manifest,
    is_narrative_source,
    read_curated_text,
    source_row,
)
from skills.contracts import SkillContext
from skills.registry import get_project_skill
from store.sqlite_store import ProjectSQLiteStore


def build_default_registry(store: ProjectSQLiteStore) -> ToolRegistry:
    return register_specs(ToolRegistry(), pipeline_tool_specs(store))


def pipeline_tool_specs(store: ProjectSQLiteStore) -> list[ToolSpec]:
    specs = [
        ToolSpec(
            name="SourceManifestTool",
            description="Load only the current actor-visible source manifest for the active project and milestone date range.",
            parameters={"type": "object", "properties": {}},
            handler=lambda actor, ctx, args: _source_manifest(store, actor, ctx, args),
            access_filter_keys=("rows",),
        ),
        ToolSpec(
            name="PeopleAssetTool",
            description="Load the canonical people and responsibility asset before assigning or comparing project ownership.",
            parameters={"type": "object", "properties": {}},
            handler=lambda actor, ctx, args: _people_asset(store, actor, ctx, args),
            access_filter_keys=(),
        ),
        ToolSpec(
            name="ProjectMemoryTool",
            description="Load actor-visible candidate, confirmed, and rejected memory for the active project before reasoning.",
            parameters={"type": "object", "properties": {}},
            handler=lambda actor, ctx, args: _project_memory(store, actor, ctx, args),
            access_filter_keys=("items",),
        ),
        ToolSpec(
            name="CuratedNotesTool",
            description="Read bounded text chunks from the already-authorized source manifest for semantic analysis.",
            parameters={"type": "object", "properties": {}},
            handler=lambda actor, ctx, args: _curated_notes(args),
            access_filter_keys=(),
        ),
        ToolSpec(
            name="ExtractionTool",
            description="Extract evidence-backed people, things, methods, tasks, issues, and questions from authorized notes.",
            parameters={"type": "object", "properties": {}},
            handler=lambda actor, ctx, args: _extract_candidates(args),
            access_filter_keys=(),
        ),
        ToolSpec(
            name="InspectionTool",
            description="Compare extracted candidates with the active milestone delivery chain and produce review candidates.",
            parameters={"type": "object", "properties": {}},
            handler=lambda actor, ctx, args: _inspect_delivery_chain(args),
            access_filter_keys=(),
        ),
        ToolSpec(
            name="EvidenceVerifierTool",
            description="Verify every report candidate against authorized source evidence before any candidate is persisted.",
            parameters={"type": "object", "properties": {}},
            handler=lambda actor, ctx, args: _verify_report(args),
            access_filter_keys=(),
        ),
        ToolSpec(
            name="MeetingMinutesSkillTool",
            description="Run the internal meeting-minutes skill when a pipeline explicitly requests a minutes artifact.",
            parameters={
                "type": "object",
                "properties": {"payload": {"type": "object", "description": "Meeting-minutes skill input."}},
            },
            handler=lambda actor, ctx, args: _meeting_minutes(store, actor, ctx, args),
            access_filter_keys=(),
        ),
        ToolSpec(
            name="ConfirmationTool",
            description="Confirm, reject, or edit an actor-visible candidate after human review.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["confirm", "reject", "edit"]},
                    "item_id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["action", "item_id"],
            },
            required_role="topic_lead",
            handler=lambda actor, ctx, args: _confirm_item(store, actor, ctx, args),
            access_filter_keys=(),
        ),
    ]
    for spec in specs:
        spec.surfaces = frozenset({INSPECTION_SURFACE})
    # Human confirmation is intentionally not exposed to any model surface.
    specs[-1].surfaces = frozenset()
    return specs


def _source_manifest(
    store: ProjectSQLiteStore,
    actor: User,
    ctx: AccessContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    runtime_context = _runtime_context(args)
    project_id = _authorized_project(actor, ctx, args)
    manifest = build_project_manifest(
        store,
        project_id,
        date_start=runtime_context.milestone.date_start,
        date_end=runtime_context.milestone.date_end,
    )
    manifest = visible_filter(actor, manifest, ctx)
    trigger_source_ids = tuple(
        getattr(getattr(runtime_context, "trigger", None), "source_ids", ()) or ()
    )
    if trigger_source_ids:
        scoped_ids = set(trigger_source_ids)
        manifest = [source for source in manifest if source.doc_id in scoped_ids]
    runtime_context.manifest = manifest
    return {
        "rows": [source_row(item) for item in manifest],
        "summary": (
            f"已读取 {len(manifest)} 份当前项目可见材料"
            + ("（按触发来源限定）" if trigger_source_ids else "")
            + "。"
        ),
    }


def _people_asset(
    store: ProjectSQLiteStore,
    actor: User,
    ctx: AccessContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    runtime_context = _runtime_context(args)
    project_id = _authorized_project(actor, ctx, args)
    structure = load_project_people(
        store,
        project_id,
        actor=actor,
        access_context=ctx,
    )
    runtime_context.people_structure = structure
    return {
        "people_structure": to_plain(structure),
        "summary": f"已读取 {len(structure.people)} 人和 {len(structure.scenarios)} 个项目场景。",
    }


def _project_memory(
    store: ProjectSQLiteStore,
    actor: User,
    ctx: AccessContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    runtime_context = _runtime_context(args)
    project_id = _authorized_project(actor, ctx, args)
    items = visible_filter(
        actor,
        [item for item in store.list_items() if item.project_id == project_id],
        ctx,
    )
    runtime_context.memory_items = items
    counts: dict[str, int] = {}
    for item in items:
        status = item.status.value if isinstance(item.status, CandidateStatus) else str(item.status)
        counts[status] = counts.get(status, 0) + 1
    projection = store.memory_projection(items=items)
    return {
        "items": projection["summary"]["compact_items"],
        "status_counts": counts,
        "compressed_memory": projection["summary"],
        "memory_index": projection["index"],
        "confirmed_titles": [item.title for item in items if item.status == CandidateStatus.CONFIRMED][:20],
        "summary": f"已读取 {len(items)} 条当前项目可见记忆。",
    }


def _curated_notes(args: dict[str, Any]) -> dict[str, Any]:
    runtime_context = _runtime_context(args)
    max_chars = int(os.getenv("PROJECT_AGENT_MAX_CONTEXT_CHARS", "24000"))
    chunk_chars = int(os.getenv("PROJECT_AGENT_NOTE_CHUNK_CHARS", "3000"))
    materials: list[dict[str, Any]] = []
    consumed = 0
    ordered_sources = sorted(
        (source for source in runtime_context.manifest if is_narrative_source(source)),
        key=lambda source: (
            0 if source.doc_id.startswith("uploaded_") else 1,
            _descending_date_key(source.meeting_date),
        ),
    )
    for source in ordered_sources:
        if not source.curated_source.path:
            continue
        text = read_curated_text(source, allowed_roots=(runtime_context.store.root_dir,))
        if not text.strip():
            continue
        runtime_context.source_texts[source.doc_id] = text
        for chunk_index, chunk in enumerate(_text_chunks(text, chunk_chars), start=1):
            remaining = max_chars - consumed
            if remaining <= 0:
                break
            selected = chunk[:remaining]
            materials.append({
                "doc_id": source.doc_id,
                "title": source.title,
                "meeting_date": source.meeting_date,
                "chunk_index": chunk_index,
                "content": selected,
            })
            consumed += len(selected)
        if consumed >= max_chars:
            break
    runtime_context.note_materials = materials
    return {
        "documents": materials,
        "summary": f"已装载 {len(materials)} 个授权纪要分块，共 {consumed} 字符。",
    }


def _extract_candidates(args: dict[str, Any]) -> dict[str, Any]:
    runtime_context = _runtime_context(args)
    provider = get_provider(role="extraction")
    extractions = []
    for source in runtime_context.manifest:
        if not is_narrative_source(source) or not source.curated_source.path:
            continue
        content = runtime_context.source_texts.get(source.doc_id) or read_curated_text(
            source,
            allowed_roots=(runtime_context.store.root_dir,),
        )
        extraction = extract_document_semantically(source, content, provider=provider)
        runtime_context.model_io_events.append({
            "event_id": f"semantic_extraction_{source.doc_id}",
            "provider": provider.name,
            "model": str(getattr(provider, "model", "")),
            "api_surface": "chat_completions_json",
            "role": "extraction",
            "source_doc_id": source.doc_id,
            "input_char_count": len(content),
            "candidate_count": sum(len(getattr(extraction, key)) for key in (
                "people", "things", "methods", "chain_links", "questions"
            )),
        })
        extractions.append(extraction)
    runtime_context.extractions = extractions
    return {
        "extraction_count": len(extractions),
        "extractions": [to_plain(item) for item in extractions],
        "summary": f"已从 {len(extractions)} 篇授权纪要抽取候选。",
    }


def _inspect_delivery_chain(args: dict[str, Any]) -> dict[str, Any]:
    runtime_context = _runtime_context(args)
    if runtime_context.people_structure is None:
        raise RuntimeError("PeopleAssetTool must run before InspectionTool")
    if not runtime_context.manifest:
        runtime_context.report = None
        runtime_context.no_change_reason = "触发范围内没有当前身份可见的项目材料。"
        return {
            "report": None,
            "no_change": True,
            "summary": runtime_context.no_change_reason,
        }
    report = inspect_delivery_chain(
        scenario_id=runtime_context.milestone.scenario_id,
        chain_name=runtime_context.milestone.chain_name,
        manifest=runtime_context.manifest,
        extractions=runtime_context.extractions,
        people_structure=runtime_context.people_structure,
    )
    runtime_context.report = report
    if not (report.chain_gaps or report.responsibility_gaps or report.followup_drafts):
        runtime_context.no_change_reason = "证据检查完成，未发现新的链路、责任或追问候选。"
    return {
        "report": to_plain(report),
        "summary": (
            f"发现 {len(report.chain_gaps)} 个链路缺口、"
            f"{len(report.responsibility_gaps)} 个责任缺口、"
            f"{len(report.followup_drafts)} 个追问草稿。"
        ),
    }


def _verify_report(args: dict[str, Any]) -> dict[str, Any]:
    runtime_context = _runtime_context(args)
    if runtime_context.report is None and runtime_context.no_change_reason:
        verification = EvidenceVerificationResult(
            ok=True,
            checked_count=0,
            errors=[],
        )
    elif runtime_context.report is None:
        verification = EvidenceVerificationResult(
            ok=False,
            checked_count=0,
            errors=["agent output is missing report"],
        )
    else:
        verification = verify_report_evidence(
            runtime_context.report,
            runtime_context.manifest,
            allowed_roots=(runtime_context.store.root_dir,),
        )
    runtime_context.verification = verification
    return {
        "verification": to_plain(verification),
        "summary": (
            f"证据校验通过={verification.ok}，已检查={verification.checked_count}，"
            f"错误={len(verification.errors)}。"
        ),
    }


def _meeting_minutes(
    store: ProjectSQLiteStore,
    actor: User,
    ctx: AccessContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    project_id = _authorized_project(actor, ctx, args)
    payload = dict(args.get("payload") or {})
    skill = get_project_skill("meeting_minutes", store_dir=payload.get("skill_store_dir"))
    result = skill.execute(
        payload,
        context=SkillContext(
            project_id=project_id,
            actor=actor,
            access_context=ctx,
            store=store,
        ),
    )
    return {"result": result.as_dict(), "summary": "meeting_minutes skill 已完成。"}


def _confirm_item(
    store: ProjectSQLiteStore,
    actor: User,
    ctx: AccessContext,
    args: dict[str, Any],
) -> dict[str, Any]:
    item = store.get_item(str(args.get("item_id") or ""))
    if not visible(actor, item, ctx):
        return {
            "ok": False,
            "summary": "候选不可用或权限不足。",
            "error": {"code": "forbidden", "message": "候选不可用或权限不足。"},
        }
    action = str(args.get("action") or "")
    if action == "confirm":
        item = store.confirm_item(item.item_id, editor=actor.id, notes=str(args.get("notes") or ""))
    elif action == "reject":
        item = store.reject_item(item.item_id, editor=actor.id, notes=str(args.get("notes") or ""))
    elif action == "edit":
        item = store.update_item_fields(
            item.item_id,
            title=args.get("title"),
            description=args.get("description"),
            editor=actor.id,
            notes=str(args.get("notes") or ""),
        )
    else:
        raise ValueError(f"Unsupported confirmation action: {action}")
    return {"item": to_plain(item), "summary": f"{action} {item.item_id}"}


def _runtime_context(args: dict[str, Any]) -> Any:
    runtime_context = args.get("context")
    if runtime_context is None:
        raise ValueError("Internal pipeline call requires context")
    return runtime_context


def _authorized_project(actor: User, ctx: AccessContext, args: dict[str, Any]) -> str:
    project_id = str(args.get("project_id") or "").strip()
    if not project_id or project_id not in ctx.projects_of(actor):
        raise PermissionError("Active project is outside the actor's membership scope")
    return project_id


def _text_chunks(text: str, chunk_chars: int) -> list[str]:
    paragraphs = [value.strip() for value in text.split("\n\n") if value.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > chunk_chars:
            chunks.append(current)
            current = ""
        if len(paragraph) > chunk_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(
                paragraph[index:index + chunk_chars]
                for index in range(0, len(paragraph), chunk_chars)
            )
        else:
            current = f"{current}\n\n{paragraph}".strip()
    if current:
        chunks.append(current)
    return chunks


def _descending_date_key(value: str) -> int:
    digits = "".join(character for character in value if character.isdigit())
    return -int(digits[:8]) if len(digits) >= 8 else 0


def observation_data(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: to_plain(item)
        for key, item in value.items()
        if key not in {"manifest", "extractions", "people_structure", "context"}
    }
