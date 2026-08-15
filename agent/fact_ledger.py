from __future__ import annotations

import hashlib
import json
import re
from typing import Any


_SKIP_KEYS = {
    "access",
    "evidence",
    "evidence_refs",
    "facts",
    "source_refs",
    "raw_source",
    "curated_source",
}
_TEXT_KEYS = {
    "name",
    "title",
    "description",
    "deliverable",
    "acceptance_criteria",
    "role",
    "group",
    "group_name",
    "responsibility_note",
    "responsibility_summary",
    "milestone_name",
    "project",
}
_PERSON_KEYS = {"owner", "owners", "owner_candidates", "author", "responsible_person"}
_STATUS_KEYS = {"status", "overdue", "running", "verified", "ok", "identity_status"}
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ][^\s]+)?$")


def build_fact_ledger(tool_name: str, result: dict[str, Any], limit: int = 160) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []

    def walk(value: Any, path: str, key: str, inherited_refs: list[dict[str, Any]]) -> None:
        if len(facts) >= limit:
            return
        if isinstance(value, dict):
            refs = _source_refs(value) or inherited_refs
            for child_key, child in value.items():
                if child_key in _SKIP_KEYS:
                    continue
                walk(child, f"{path}.{child_key}", child_key, refs)
            return
        if isinstance(value, list):
            walk(len(value), f"{path}.length", "count", inherited_refs)
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]", key, inherited_refs)
            return

        kind = _fact_kind(key, path, value)
        if kind is None or value in (None, ""):
            return
        refs = inherited_refs or [{"tool": tool_name, "path": path}]
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        ref_text = json.dumps(refs, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(f"{tool_name}\x1f{path}\x1f{canonical}\x1f{ref_text}".encode("utf-8")).hexdigest()[:12]
        facts.append(
            {
                "fact_id": f"F_{digest}",
                "kind": kind,
                "path": path,
                "value": value,
                "source_refs": refs,
            }
        )

    walk(result, "$", "", [])
    return facts


def _fact_kind(key: str, path: str, value: Any) -> str | None:
    normalized = key.lower()
    if isinstance(value, bool):
        return "status" if normalized in _STATUS_KEYS else None
    if isinstance(value, (int, float)):
        return "number"
    if not isinstance(value, str):
        return None
    if _DATE_PATTERN.match(value) or "date" in normalized or normalized in {"as_of", "next_run_time"}:
        return "date"
    if normalized in _PERSON_KEYS or normalized.endswith("_owner"):
        return "person"
    if normalized == "name" and any(token in path for token in (".people[", ".person[")):
        return "person"
    if normalized in _STATUS_KEYS or normalized.endswith("_status"):
        return "status"
    if normalized in _TEXT_KEYS:
        return "text"
    return None


def _source_refs(row: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("evidence", "evidence_refs"):
        value = row.get(key)
        if isinstance(value, list):
            refs = [_normalize_ref(item) for item in value if isinstance(item, dict)]
            if refs:
                return refs
    source = row.get("source")
    if isinstance(source, dict):
        return [_normalize_ref(source)]
    source_id = row.get("source_doc_id") or row.get("source_id") or row.get("doc_id")
    if source_id:
        return [
            {
                key: value
                for key, value in {
                    "source_doc_id": source_id,
                    "meeting_date": row.get("meeting_date"),
                    "locator": row.get("locator"),
                    "title": row.get("source_title"),
                }.items()
                if value not in (None, "")
            }
        ]
    return []


def _normalize_ref(ref: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "source_doc_id": ref.get("source_doc_id") or ref.get("id") or ref.get("doc_id"),
        "source_kind": ref.get("source_kind") or ref.get("kind"),
        "meeting_date": ref.get("meeting_date") or ref.get("date"),
        "locator": ref.get("locator"),
        "title": ref.get("title"),
        "quote": ref.get("quote"),
    }
    return {key: value for key, value in normalized.items() if value not in (None, "")}
