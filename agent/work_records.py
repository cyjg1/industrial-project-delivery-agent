from __future__ import annotations

from typing import Any, Mapping

from agent.access_policy import AccessContext, ROLE_RANK, User, visible


WORK_RECORD_KINDS = {"work", "conclusion", "problem", "output"}
WORK_RECORD_SOURCE_ORIGINS = {
    "conversation_daily_journal",
    "historical_excel_backfill",
}


class WorkRecordValidationError(ValueError):
    pass


def validate_work_record_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(entry.get("kind") or "").strip()
    text = str(entry.get("text") or "").strip()
    if kind not in WORK_RECORD_KINDS:
        raise WorkRecordValidationError(f"Unsupported daily work record kind: {kind}")
    if not text:
        raise WorkRecordValidationError("Daily work record text is required")
    solution_options = entry.get("solution_options") or []
    if not isinstance(solution_options, list):
        raise WorkRecordValidationError("solution_options must be an array")
    normalized_options: list[dict[str, Any]] = []
    for index, option in enumerate(solution_options):
        if not isinstance(option, Mapping):
            raise WorkRecordValidationError(f"solution_options[{index}] must be an object")
        option_text = str(option.get("text") or "").strip()
        memory_refs = option.get("memory_refs") or []
        if not option_text or not isinstance(memory_refs, list) or not memory_refs:
            raise WorkRecordValidationError(
                f"solution_options[{index}] requires text and actor-visible confirmed memory_refs"
            )
        normalized_options.append({
            "text": option_text,
            "memory_refs": [str(value) for value in memory_refs if str(value).strip()],
        })
    evidence_refs = entry.get("evidence_refs") or []
    if not isinstance(evidence_refs, list) or not evidence_refs:
        raise WorkRecordValidationError("Daily work record evidence_refs are required")
    return {
        **dict(entry),
        "kind": kind,
        "text": text,
        "solution_options": normalized_options,
        "evidence_refs": [dict(value) for value in evidence_refs if isinstance(value, Mapping)],
        "source_locator": str(entry.get("source_locator") or "").strip(),
    }


def can_view_work_record(
    actor: User,
    record: Any,
    ctx: AccessContext,
) -> bool:
    if not visible(actor, record, ctx):
        return False
    project_id = str(_field(record, "project_id") or "")
    role = ctx.role_of(actor, project_id)
    if ROLE_RANK.get(role, -1) >= ROLE_RANK["pmo"]:
        return True
    subject_user_id = str(_field(record, "subject_user_id") or "")
    if not subject_user_id:
        return False
    return subject_user_id == actor.id or subject_user_id in ctx.managed_user_ids(actor, project_id)


def can_edit_work_record(
    actor: User,
    record: Any,
    ctx: AccessContext,
) -> bool:
    return can_view_work_record(actor, record, ctx)


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)
