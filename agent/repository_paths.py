from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXTERNAL_ROOT_VARIABLES = (
    "OBSIDIAN_VAULT_PATH",
    "PROJECT_SOURCE_DIR",
    "LEGACY_FIXTURE_ROOT",
    "LEGACY_MEETING_MINUTES_REPO",
    "PROJECT_AGENT_OUTPUT_DIR",
    "PROJECT_AGENT_SOURCE_DIR",
    "PROJECT_AGENT_STORE_DIR",
    "PROJECT_AGENT_UPLOAD_ROOT",
)
_ENV_PATH_PATTERN = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}(?:/(.*))?$")
PATH_FIELD_NAMES = {
    "artifact_path",
    "artifact_paths",
    "canonical_snapshot",
    "curated_source",
    "directory",
    "file_path",
    "file_paths",
    "files",
    "manifest_path",
    "minutes_path",
    "path",
    "paths",
    "people_asset_path",
    "raw_source",
    "source_dir",
    "source_path",
    "source_paths",
    "upload_path",
    "vault_path",
}
STORAGE_FILE_FIELDS = {
    "archive_dir",
    "brief_vault_dir",
    "correction_candidates",
    "database",
    "events",
    "index",
    "lexicon",
    "meeting_vault_dir",
    "method_vault_dir",
    "methodology_candidates",
    "minutes_directory",
    "runs",
    "schema",
    "summary",
    "vault_dir",
    "weekly_review_vault_dir",
}


def portable_repository_path(value: str | Path, *, root: Path = REPOSITORY_ROOT) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser() if text in {"~"} or text.startswith("~/") else Path(text)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def resolve_repository_path(
    value: str | Path,
    *,
    root: Path = REPOSITORY_ROOT,
    allowed_roots: Iterable[str | Path] = (),
    configured_roots: Mapping[str, str | Path] | None = None,
) -> Path:
    text = str(value or "")
    root_aliases = dict(configured_roots or {})
    _validate_root_aliases(root_aliases)
    match = _ENV_PATH_PATTERN.match(text)
    if match:
        variable, relative = match.groups()
        if variable not in EXTERNAL_ROOT_VARIABLES:
            raise ValueError(f"Path environment variable is not allowed: {variable}")
        configured_root = str(root_aliases.get(variable) or os.environ.get(variable, "")).strip()
        if not configured_root:
            raise ValueError(f"Path requires environment variable {variable}")
        return _resolve_within(Path(configured_root).expanduser(), relative or "")
    if text.startswith("${"):
        raise ValueError(f"Invalid environment path reference: {text}")
    path = Path(text).expanduser() if text in {"~"} or text.startswith("~/") else Path(text)
    if not path.is_absolute():
        return _resolve_within(root, path)
    allowed_root_paths = [
        root,
        *(Path(allowed_root) for allowed_root in allowed_roots),
        *(Path(configured_root) for configured_root in root_aliases.values()),
    ]
    allowed_root_paths.extend(
        Path(configured).expanduser()
        for variable in EXTERNAL_ROOT_VARIABLES
        if variable not in root_aliases
        and (configured := os.environ.get(variable, "").strip())
    )
    if any(_is_within(path, allowed_root) for allowed_root in allowed_root_paths):
        return path.resolve()
    raise ValueError(f"Absolute path is outside configured roots: {path}")


def normalize_repository_paths(
    value,
    *,
    root: Path = REPOSITORY_ROOT,
    field_name: str | None = None,
    parent_field: str | None = None,
    configured_roots: Mapping[str, str | Path] | None = None,
):
    """Recursively make repository-owned path values portable before persistence."""
    root_aliases = dict(configured_roots or {})
    _validate_root_aliases(root_aliases)
    if isinstance(value, dict):
        return {
            key: normalize_repository_paths(
                item,
                root=root,
                field_name=str(key),
                parent_field=field_name,
                configured_roots=root_aliases,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            normalize_repository_paths(
                item,
                root=root,
                field_name=field_name,
                parent_field=parent_field,
                configured_roots=root_aliases,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            normalize_repository_paths(
                item,
                root=root,
                field_name=field_name,
                parent_field=parent_field,
                configured_roots=root_aliases,
            )
            for item in value
        )
    is_path_field = field_name in PATH_FIELD_NAMES or (
        parent_field in {"storage_files", "stored_files"}
        and field_name in STORAGE_FILE_FIELDS
    )
    if isinstance(value, str) and is_path_field:
        path = Path(value)
        if path.is_absolute():
            try:
                path.resolve().relative_to(root.resolve())
            except ValueError:
                configured_values = [
                    *root_aliases.items(),
                    *(
                        (variable, os.environ.get(variable, "").strip())
                        for variable in EXTERNAL_ROOT_VARIABLES
                        if variable not in root_aliases
                    ),
                ]
                configured_values = [
                    (variable, str(configured_root).strip())
                    for variable, configured_root in configured_values
                    if str(configured_root).strip()
                ]
                configured_values.sort(
                    key=lambda item: len(Path(item[1]).expanduser().resolve().parts),
                    reverse=True,
                )
                for variable, configured_root in configured_values:
                    try:
                        relative = path.resolve().relative_to(Path(configured_root).expanduser().resolve())
                    except ValueError:
                        continue
                    suffix = relative.as_posix()
                    return f"${{{variable}}}/{suffix}" if suffix != "." else f"${{{variable}}}"
                return value
            return portable_repository_path(value, root=root)
    return value


def _resolve_within(base: Path, relative: str | Path) -> Path:
    resolved_base = base.resolve()
    candidate = (resolved_base / relative).resolve()
    if not _is_within(candidate, resolved_base):
        raise ValueError(f"Path escapes configured root: {relative}")
    return candidate


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _validate_root_aliases(configured_roots: Mapping[str, str | Path]) -> None:
    invalid = set(configured_roots) - set(EXTERNAL_ROOT_VARIABLES)
    if invalid:
        raise ValueError(f"Path environment variable is not allowed: {sorted(invalid)[0]}")


def normalize_source_payload_paths(payload: dict) -> dict:
    normalized = normalize_repository_paths(payload)
    for key in ("curated_source", "raw_source"):
        ref = normalized.get(key)
        if isinstance(ref, dict):
            normalized_ref = dict(ref)
            if normalized_ref.get("path"):
                normalized_ref["path"] = portable_repository_path(normalized_ref["path"])
            normalized[key] = normalized_ref
    if normalized.get("people_asset_path"):
        normalized["people_asset_path"] = portable_repository_path(normalized["people_asset_path"])
    return normalized
