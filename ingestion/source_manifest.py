from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from agent.repository_paths import normalize_repository_paths, resolve_repository_path
from agent.schemas import SourceDocument, SourceRef, source_row


REPO_ROOT = Path(__file__).resolve().parents[1]
UPLOAD_TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
UPLOAD_RAW_SUFFIXES = {".docx", ".doc", ".pdf", ".xlsx", ".xls", ".xlsm", ".xmind"}
TRANSCRIPT_SUFFIXES = {".txt", ".docx"}
MINUTES_SUFFIXES = {".md", ".markdown", ".txt", ".docx"}
STRUCTURED_UPLOAD_KINDS = {"three_list_tasks", "three_list_issues", "work_logs"}


def upload_root() -> Path:
    return Path(
        os.getenv(
            "PROJECT_AGENT_UPLOAD_ROOT",
            REPO_ROOT / "data" / "sources" / "uploads",
        )
    )


def upload_manifest_path() -> Path:
    return Path(
        os.getenv(
            "PROJECT_AGENT_UPLOAD_MANIFEST",
            upload_root() / "manifest.json",
        )
    )


def build_default_manifest(date_start: str = "", date_end: str = "") -> list[SourceDocument]:
    groups = [load_uploaded_manifest()]
    if os.getenv("OBSIDIAN_VAULT_PATH", "").strip():
        groups.append(discover_obsidian_sources())
    manifest = _merge_source_manifests(*groups)
    return [
        source
        for source in manifest
        if _in_date_range(source, _date_filter(date_start), _date_filter(date_end))
    ]


def build_project_manifest(
    store: Any,
    project_id: str,
    date_start: str = "",
    date_end: str = "",
) -> list[SourceDocument]:
    supported_kinds = {
        "minutes",
        "transcript",
        "three_list_workbook",
        "three_list_tasks",
        "three_list_issues",
        "work_logs",
        "people_structure",
    }
    sources = [
        source_from_stored_row(row)
        for row in store.list_sources()
        if row.get("project_id") == project_id
        and row.get("kind") in supported_kinds
        and row.get("tag_origin") in {"human_source_import", "actor_ingest", "synthetic_demo"}
    ]
    return [
        source
        for source in sources
        if _in_date_range(source, _date_filter(date_start), _date_filter(date_end))
    ]


def source_from_stored_row(row: dict[str, Any]) -> SourceDocument:
    payload = row.get("payload") or {}
    return SourceDocument(
        doc_id=str(row.get("id") or payload.get("doc_id") or ""),
        title=str(row.get("title") or payload.get("title") or "Untitled source"),
        meeting_date=str(row.get("meeting_date") or payload.get("meeting_date") or ""),
        topic=str(payload.get("topic") or row.get("kind") or "Project material"),
        curated_source=_stored_source_ref(payload.get("curated_source"), "Project curated source"),
        raw_source=_stored_source_ref(payload.get("raw_source"), "Project raw source"),
        tags=list(payload.get("tags") or []),
        org_id=str(row.get("org_id") or payload.get("org_id") or "org_demo"),
        project_id=str(row.get("project_id") or payload.get("project_id") or "project_demo"),
        topic_id=row.get("topic_id") or payload.get("topic_id") or None,
        author_id=str(row.get("author_id") or payload.get("author_id") or "system"),
        sensitivity=str(row.get("sensitivity") or payload.get("sensitivity") or "l1"),
        tag_origin=str(row.get("tag_origin") or payload.get("tag_origin") or "runtime_default"),
        input_kind=str(row.get("input_kind") or payload.get("input_kind") or "auto"),
        content_hash=str(payload.get("content_hash") or ""),
    )


_source_from_stored_row = source_from_stored_row


def register_uploaded_meeting_note(
    filename: str,
    content: bytes,
    *,
    title: str = "",
    meeting_date: str = "",
    topic: str = "",
    access_tags: dict | None = None,
    raw_filename: str = "",
    raw_content: bytes | None = None,
    input_kind: str = "auto",
) -> SourceDocument:
    safe_name = _safe_filename(filename)
    resolved_kind = _resolve_input_kind(Path(safe_name).suffix.lower(), input_kind)
    return _register_uploaded_file(
        filename=safe_name,
        content=content,
        title=title,
        meeting_date=meeting_date,
        topic=topic,
        access_tags=access_tags,
        raw_filename=raw_filename,
        raw_content=raw_content,
        input_kind=resolved_kind,
    )


def register_uploaded_project_file(
    filename: str,
    content: bytes,
    *,
    title: str = "",
    meeting_date: str = "",
    topic: str = "",
    access_tags: dict | None = None,
    raw_filename: str = "",
    raw_content: bytes | None = None,
    input_kind: str,
) -> SourceDocument:
    normalized_kind = str(input_kind or "").strip().lower()
    if normalized_kind not in {"minutes", "transcript", *STRUCTURED_UPLOAD_KINDS}:
        raise ValueError(f"Unsupported project file type: {input_kind}")
    safe_name = _safe_filename(filename)
    suffix = Path(safe_name).suffix.lower()
    if normalized_kind in STRUCTURED_UPLOAD_KINDS and suffix not in {".xlsx", ".xlsm"}:
        raise ValueError(f"{normalized_kind} requires .xlsx or .xlsm, got {suffix or 'no extension'}.")
    if normalized_kind in {"minutes", "transcript"}:
        _resolve_input_kind(suffix, normalized_kind)
    return _register_uploaded_file(
        filename=safe_name,
        content=content,
        title=title,
        meeting_date=meeting_date,
        topic=topic,
        access_tags=access_tags,
        raw_filename=raw_filename,
        raw_content=raw_content,
        input_kind=normalized_kind,
    )


def load_uploaded_manifest() -> list[SourceDocument]:
    return [_source_from_upload_row(row) for row in _read_uploaded_rows()]


def upload_manifest_rows() -> list[dict]:
    return [source_row(source) for source in load_uploaded_manifest()]


def manifest_rows() -> list[dict]:
    return [source_row(source) for source in build_default_manifest()]


def read_curated_text(
    source: SourceDocument,
    *,
    allowed_roots: tuple[str | Path, ...] = (),
) -> str:
    if not is_narrative_source(source) or not source.curated_source.path:
        return ""
    path = resolve_repository_path(source.curated_source.path, allowed_roots=allowed_roots)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def is_narrative_source(source: SourceDocument) -> bool:
    return source.input_kind not in STRUCTURED_UPLOAD_KINDS


def obsidian_vault_root() -> Path:
    configured = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
    return Path(configured) if configured else REPO_ROOT / "data" / "external_vault"


def discover_obsidian_sources() -> list[SourceDocument]:
    vault_root = obsidian_vault_root()
    project_subpath = os.getenv("OBSIDIAN_PROJECT_SUBPATH", "").strip()
    root = vault_root / project_subpath if project_subpath else vault_root
    if not root.exists():
        return []
    excluded = {
        value.strip().lower()
        for value in os.getenv("OBSIDIAN_EXCLUDE_DIRS", ".obsidian,.trash,templates").split(",")
        if value.strip()
    }
    required_tags = [
        value.strip()
        for value in os.getenv("OBSIDIAN_PROJECT_TAGS", "").split(",")
        if value.strip()
    ]
    max_files = min(max(int(os.getenv("OBSIDIAN_MAX_FILES", "500")), 1), 5000)
    sources: list[SourceDocument] = []
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        relative = path.relative_to(vault_root)
        if any(part.lower() in excluded for part in relative.parts[:-1]):
            continue
        content = path.read_bytes()
        decoded = content.decode("utf-8", errors="replace")
        if required_tags and not any(tag in decoded for tag in required_tags):
            continue
        digest = hashlib.sha256(content).hexdigest()
        sources.append(
            SourceDocument(
                doc_id=f"external_{hashlib.sha1(str(relative).encode('utf-8')).hexdigest()[:12]}",
                title=_first_heading(path) or path.stem,
                meeting_date=_meeting_date_from_name(path.name),
                topic="External Markdown note",
                curated_source=_curated(str(path)),
                raw_source=_raw_ref(
                    None,
                    status="raw_source_pending",
                    notes="External Markdown note; no raw transcript was registered.",
                ),
                tags=["external_markdown", f"content_hash:{digest}"],
            )
        )
        if len(sources) >= max_files:
            break
    return sources


def obsidian_sync_status() -> dict[str, Any]:
    root = obsidian_vault_root()
    files = sorted(root.rglob("*.md")) if root.exists() else []
    latest = max((path.stat().st_mtime for path in files), default=0)
    return {
        "root": str(root),
        "configured": bool(os.getenv("OBSIDIAN_VAULT_PATH", "").strip()),
        "exists": root.exists(),
        "markdown_count": len(files),
        "latest_mtime": latest,
    }


def _register_uploaded_file(
    *,
    filename: str,
    content: bytes,
    title: str,
    meeting_date: str,
    topic: str,
    access_tags: dict | None,
    raw_filename: str,
    raw_content: bytes | None,
    input_kind: str,
) -> SourceDocument:
    root = upload_root()
    root.mkdir(parents=True, exist_ok=True)
    content_hash = hashlib.sha256(content).hexdigest()
    stored_path = root / "versions" / content_hash / filename
    _write_immutable_bytes(stored_path, content)
    source = _uploaded_source_from_file(
        stored_path=stored_path,
        title=title or Path(filename).stem,
        meeting_date=meeting_date or "To be confirmed",
        topic=topic or _default_upload_topic(input_kind),
        input_kind=input_kind,
        content_hash=content_hash,
    )
    if access_tags:
        _apply_source_access_tags(source, access_tags, tag_origin="actor_ingest")
    if raw_content:
        raw_name = _safe_filename(raw_filename or f"raw_{filename}")
        raw_hash = hashlib.sha256(raw_content).hexdigest()
        raw_path = root / "versions" / raw_hash / f"raw_{raw_name}"
        _write_immutable_bytes(raw_path, raw_content)
        source.raw_source = _raw_ref(
            str(raw_path),
            status="matched",
            notes="Raw transcript uploaded with the curated note.",
        )
    rows = [row for row in _read_uploaded_rows() if row.get("doc_id") != source.doc_id]
    rows.append(_source_to_upload_row(source))
    _write_uploaded_rows(rows)
    return source


def _uploaded_source_from_file(
    *,
    stored_path: Path,
    title: str,
    meeting_date: str,
    topic: str,
    input_kind: str,
    content_hash: str,
) -> SourceDocument:
    suffix = stored_path.suffix.lower()
    if input_kind in STRUCTURED_UPLOAD_KINDS:
        curated = SourceRef(
            source_type="Uploaded structured project workbook",
            path=str(stored_path),
            status="matched",
            notes="Human-maintained structured workbook for deterministic import.",
        )
        raw = _raw_ref(str(stored_path), status="matched", notes="Immutable uploaded workbook version.")
        tags = ["user_upload", input_kind]
    elif input_kind == "minutes" or (input_kind == "auto" and suffix in UPLOAD_TEXT_SUFFIXES):
        curated = _curated(str(stored_path))
        raw = _raw_ref(None, status="raw_source_pending", notes="No raw transcript was registered.")
        tags = ["user_upload", "meeting_minutes"]
    else:
        curated = _curated(None, status="curated_source_pending")
        raw = _raw_ref(str(stored_path), status="matched", notes="Uploaded raw project material.")
        tags = ["user_upload", "raw_material"]
    return SourceDocument(
        doc_id=_uploaded_doc_id(stored_path),
        title=title,
        meeting_date=meeting_date,
        topic=topic,
        curated_source=curated,
        raw_source=raw,
        tags=tags,
        input_kind=input_kind,
        content_hash=content_hash,
    )


def _stored_source_ref(value: Any, fallback_type: str) -> SourceRef:
    if not isinstance(value, dict):
        return SourceRef(
            fallback_type,
            str(value or "") or None,
            "matched" if value else "raw_source_pending",
        )
    return SourceRef(
        source_type=str(value.get("source_type") or fallback_type),
        path=value.get("path") or None,
        status=str(value.get("status") or "matched"),
        notes=str(value.get("notes") or ""),
    )


def _curated(path: str | None, status: str = "matched") -> SourceRef:
    return SourceRef("Curated project source", path, status)


def _raw_ref(path: str | None, status: str = "matched", notes: str = "") -> SourceRef:
    return SourceRef("Raw project source", path, status, notes)


def _apply_source_access_tags(source: SourceDocument, tags: dict, *, tag_origin: str) -> None:
    source.org_id = str(tags.get("org_id") or source.org_id)
    source.project_id = str(tags.get("project_id") or source.project_id)
    source.topic_id = tags.get("topic_id") or None
    source.author_id = str(tags.get("author_id") or source.author_id)
    source.sensitivity = str(tags.get("sensitivity") or source.sensitivity)
    source.tag_origin = tag_origin


def _source_to_upload_row(source: SourceDocument) -> dict[str, Any]:
    return {
        "doc_id": source.doc_id,
        "title": source.title,
        "meeting_date": source.meeting_date,
        "topic": source.topic,
        "org_id": source.org_id,
        "project_id": source.project_id,
        "topic_id": source.topic_id,
        "author_id": source.author_id,
        "sensitivity": source.sensitivity,
        "tag_origin": source.tag_origin,
        "input_kind": source.input_kind,
        "content_hash": source.content_hash,
        "curated_source": source.curated_source.path,
        "curated_source_status": source.curated_source.status,
        "raw_source": source.raw_source.path,
        "raw_source_status": source.raw_source.status,
        "raw_source_notes": source.raw_source.notes,
        "tags": source.tags,
    }


def _source_from_upload_row(row: dict[str, Any]) -> SourceDocument:
    return SourceDocument(
        doc_id=str(row["doc_id"]),
        title=str(row["title"]),
        meeting_date=str(row.get("meeting_date") or "To be confirmed"),
        topic=str(row.get("topic") or "Uploaded project material"),
        curated_source=_curated(
            row.get("curated_source"),
            status=str(row.get("curated_source_status") or "curated_source_pending"),
        ),
        raw_source=_raw_ref(
            row.get("raw_source"),
            status=str(row.get("raw_source_status") or "raw_source_pending"),
            notes=str(row.get("raw_source_notes") or ""),
        ),
        tags=list(row.get("tags") or ["user_upload"]),
        org_id=str(row.get("org_id") or "org_demo"),
        project_id=str(row.get("project_id") or "project_demo"),
        topic_id=row.get("topic_id") or None,
        author_id=str(row.get("author_id") or "system"),
        sensitivity=str(row.get("sensitivity") or "l1"),
        tag_origin=str(row.get("tag_origin") or "runtime_default"),
        input_kind=str(row.get("input_kind") or "auto"),
        content_hash=str(row.get("content_hash") or ""),
    )


def _read_uploaded_rows() -> list[dict[str, Any]]:
    path = upload_manifest_path()
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("sources") or [])


def _write_uploaded_rows(rows: list[dict[str, Any]]) -> None:
    path = upload_manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = normalize_repository_paths({"sources": rows})
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _safe_filename(filename: str) -> str:
    path = Path(filename)
    stem = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", path.stem).strip("._")
    return f"{stem or 'uploaded_project_file'}{path.suffix.lower()}"


def _resolve_input_kind(suffix: str, input_kind: str) -> str:
    normalized = str(input_kind or "auto").strip().lower()
    if normalized not in {"auto", "minutes", "transcript"}:
        raise ValueError(f"Unsupported input_kind: {input_kind}")
    if normalized == "transcript" and suffix not in TRANSCRIPT_SUFFIXES:
        raise ValueError(f"Unsupported transcript format: {suffix}; use .txt or .docx")
    if normalized == "minutes" and suffix not in MINUTES_SUFFIXES:
        raise ValueError(f"Unsupported meeting-minutes format: {suffix}; use .md, .txt, or .docx")
    return normalized


def _write_immutable_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError(f"Content-addressed upload hash collision: {path}")
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _uploaded_doc_id(stored_path: Path) -> str:
    stem = re.sub(r"[^0-9A-Za-z]+", "_", stored_path.stem).strip("_").lower()
    if stem:
        return f"uploaded_{stem}"
    digest = hashlib.sha1(stored_path.name.encode("utf-8")).hexdigest()[:12]
    return f"uploaded_file_{digest}"


def _default_upload_topic(input_kind: str) -> str:
    return {
        "three_list_tasks": "Task list",
        "three_list_issues": "Issue list",
        "work_logs": "Work log",
        "transcript": "Meeting transcript",
    }.get(input_kind, "Meeting minutes")


def _merge_source_manifests(*groups: list[SourceDocument]) -> list[SourceDocument]:
    merged: list[SourceDocument] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for source in group:
            key = (source.doc_id, _normalized_path(source.curated_source.path))
            if key in seen:
                continue
            seen.add(key)
            merged.append(source)
    return merged


def _normalized_path(value: str | None) -> str:
    if not value:
        return ""
    try:
        return os.path.normcase(str(Path(value).resolve()))
    except OSError:
        return os.path.normcase(str(Path(value)))


def _in_date_range(source: SourceDocument, date_start: str, date_end: str) -> bool:
    if source.doc_id.startswith("uploaded_"):
        return True
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", source.meeting_date or ""):
        return not (source.doc_id.startswith("external_") and (date_start or date_end))
    if date_start and source.meeting_date < date_start:
        return False
    if date_end and source.meeting_date > date_end:
        return False
    return True


def _date_filter(value: str) -> str:
    return value if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or "") else ""


def _first_heading(path: Path) -> str:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _meeting_date_from_name(name: str) -> str:
    full = re.search(r"20(\d{2})(\d{2})(\d{2})", name)
    if full:
        return f"20{full.group(1)}-{full.group(2)}-{full.group(3)}"
    short = re.search(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)", name)
    if short:
        return f"20{short.group(1)}-{short.group(2)}-{short.group(3)}"
    return ""

