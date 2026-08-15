from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent.access_policy import AccessContext, ROLE_RANK, User, visible


ARTIFACT_ROLES = {"formal", "process", "evidence", "reference"}
DELIVERABLE_STATUSES = {"expected", "draft", "submitted", "accepted", "rejected", "archived"}
MAX_DELIVERABLE_FILES = 20
DEFAULT_MAX_DELIVERABLE_FILE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_DELIVERABLE_TOTAL_BYTES = 200 * 1024 * 1024


class DeliverableValidationError(ValueError):
    pass


def deliverable_file_size_limit() -> int:
    raw = os.getenv("DELIVERABLE_MAX_FILE_BYTES", str(DEFAULT_MAX_DELIVERABLE_FILE_BYTES)).strip()
    try:
        limit = int(raw)
    except ValueError as exc:
        raise DeliverableValidationError("DELIVERABLE_MAX_FILE_BYTES must be a positive integer") from exc
    if limit <= 0:
        raise DeliverableValidationError("DELIVERABLE_MAX_FILE_BYTES must be a positive integer")
    return limit


def deliverable_total_size_limit() -> int:
    raw = os.getenv(
        "DELIVERABLE_MAX_TOTAL_BYTES",
        str(DEFAULT_MAX_DELIVERABLE_TOTAL_BYTES),
    ).strip()
    try:
        limit = int(raw)
    except ValueError as exc:
        raise DeliverableValidationError(
            "DELIVERABLE_MAX_TOTAL_BYTES must be a positive integer"
        ) from exc
    if limit <= 0:
        raise DeliverableValidationError(
            "DELIVERABLE_MAX_TOTAL_BYTES must be a positive integer"
        )
    return limit


@dataclass(frozen=True)
class DeliverableUpload:
    filename: str
    content: bytes
    content_type: str = ""
    artifact_role: str = "formal"


class DeliverableService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def create_requirement(
        self,
        *,
        actor: User,
        access_context: AccessContext,
        project_id: str,
        milestone_id: str,
        title: str,
        type_label: str = "",
        acceptance_criteria: str = "",
        due_date: str = "",
        required: bool = True,
        topic_id: str | None = None,
        sensitivity: str = "l1",
        sort_order: int = 0,
    ) -> dict[str, Any]:
        self._require_management(actor, access_context, project_id)
        project = self.store.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        if project["org_id"] != actor.org_id:
            raise PermissionError("Deliverable project belongs to another organization")
        normalized_title = str(title or "").strip()
        normalized_milestone = str(milestone_id or "").strip()
        if not normalized_title or not normalized_milestone:
            raise DeliverableValidationError("Deliverable title and milestone_id are required")
        deliverable_id = "deliverable_" + uuid4().hex[:24]
        return self.store.save_deliverable_record({
            "deliverable_id": deliverable_id,
            "org_id": actor.org_id,
            "project_id": project_id,
            "topic_id": topic_id,
            "author_id": actor.id,
            "sensitivity": sensitivity,
            "milestone_id": normalized_milestone,
            "title": normalized_title,
            "type_label": str(type_label or "").strip(),
            "required": bool(required),
            "due_date": str(due_date or "").strip(),
            "acceptance_criteria": str(acceptance_criteria or "").strip(),
            "status": "expected",
            "sort_order": int(sort_order),
            "current_version": 0,
            "updated_by": actor.id,
        })

    def update_requirement(
        self,
        deliverable_id: str,
        *,
        actor: User,
        access_context: AccessContext,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        current = self.store.get_deliverable(deliverable_id)
        self._require_management(actor, access_context, current["project_id"])
        if not visible(actor, current, access_context):
            raise PermissionError("Deliverable is outside the actor's visible scope")
        allowed = {
            "title", "type_label", "required", "due_date", "acceptance_criteria",
            "status", "sort_order", "topic_id", "sensitivity",
        }
        unsupported = set(patch) - allowed
        if unsupported:
            raise DeliverableValidationError(
                "Unsupported deliverable fields: " + ", ".join(sorted(unsupported))
            )
        next_payload = {**current, **patch, "updated_by": actor.id}
        next_payload.pop("id", None)
        next_payload.pop("payload", None)
        if not str(next_payload.get("title") or "").strip():
            raise DeliverableValidationError("Deliverable title is required")
        if next_payload.get("status") not in DELIVERABLE_STATUSES:
            raise DeliverableValidationError(
                f"Unsupported deliverable status: {next_payload.get('status')}"
            )
        return self.store.save_deliverable_record(next_payload)

    def list_requirements(
        self,
        *,
        actor: User,
        access_context: AccessContext,
        project_id: str,
        milestone_id: str = "",
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.store.list_deliverable_records(project_id)
            if visible(actor, row, access_context)
            and (not milestone_id or row["milestone_id"] == milestone_id)
        ]
        return [
            {
                **row,
                "versions": self.store.list_deliverable_versions(row["deliverable_id"]),
                "can_manage": self._is_management(actor, access_context, project_id),
                "can_submit": ROLE_RANK.get(
                    access_context.role_of(actor, project_id), -1
                ) >= ROLE_RANK["exec"],
            }
            for row in rows
        ]

    def submit_version(
        self,
        deliverable_id: str,
        *,
        actor: User,
        access_context: AccessContext,
        files: list[DeliverableUpload],
        note: str = "",
    ) -> dict[str, Any]:
        deliverable = self.store.get_deliverable(deliverable_id)
        if not visible(actor, deliverable, access_context):
            raise PermissionError("Deliverable is outside the actor's visible scope")
        role = access_context.role_of(actor, deliverable["project_id"])
        if ROLE_RANK.get(role, -1) < ROLE_RANK["exec"]:
            raise PermissionError("Actor role cannot submit deliverables")
        if deliverable["status"] == "archived":
            raise DeliverableValidationError("Archived deliverables cannot receive new versions")
        if not files:
            raise DeliverableValidationError("At least one deliverable file is required")
        if len(files) > MAX_DELIVERABLE_FILES:
            raise DeliverableValidationError(
                f"At most {MAX_DELIVERABLE_FILES} files can be submitted in one version"
            )
        file_size_limit = deliverable_file_size_limit()
        total_size_limit = deliverable_total_size_limit()
        if sum(len(upload.content) for upload in files) > total_size_limit:
            raise DeliverableValidationError(
                f"Deliverable upload exceeds configured total limit of {total_size_limit} bytes"
            )
        for upload in files:
            if upload.artifact_role not in ARTIFACT_ROLES:
                raise DeliverableValidationError(
                    f"Unsupported artifact_role: {upload.artifact_role}"
                )
            if not upload.content:
                raise DeliverableValidationError(f"Deliverable file is empty: {upload.filename}")
            if len(upload.content) > file_size_limit:
                raise DeliverableValidationError(
                    f"Deliverable file {upload.filename} exceeds configured limit of {file_size_limit} bytes"
                )
            if not Path(str(upload.filename or "")).name.strip():
                raise DeliverableValidationError("Deliverable filename is required")

        reserved = self.store.reserve_deliverable_version_record(
            deliverable_id,
            submitted_by=actor.id,
            note=str(note or "").strip(),
            actor=actor,
            access_context=access_context,
        )
        version = int(reserved["version"])
        version_id = str(reserved["version_id"])
        version_dir = (
            self.store.root_dir
            / "artifacts"
            / "deliverables"
            / deliverable["project_id"]
            / deliverable_id
            / f"v{version}"
        )
        stored_files: list[dict[str, Any]] = []
        newly_written: list[Path] = []
        try:
            root = self.store.root_dir.resolve()
            if root not in version_dir.resolve().parents:
                raise DeliverableValidationError("Deliverable path escaped the project store")
            version_dir.mkdir(parents=True, exist_ok=True)
            for index, upload in enumerate(files):
                original_name = Path(str(upload.filename or "")).name.strip()
                safe_name = _safe_filename(original_name)
                target = version_dir / f"{index + 1:02d}_{safe_name}"
                content_hash = hashlib.sha256(upload.content).hexdigest()
                if target.exists():
                    existing_hash = hashlib.sha256(target.read_bytes()).hexdigest()
                    if existing_hash != content_hash:
                        raise DeliverableValidationError(
                            f"Immutable deliverable path conflict: {original_name}"
                        )
                else:
                    target.write_bytes(upload.content)
                    newly_written.append(target)
                stored_files.append({
                    "artifact_role": upload.artifact_role,
                    "original_name": original_name,
                    "relative_path": target.relative_to(self.store.root_dir).as_posix(),
                    "content_hash": content_hash,
                    "content_type": str(upload.content_type or ""),
                    "size": len(upload.content),
                })
            return self.store.complete_deliverable_version_record(
                version_id,
                files=stored_files,
                actor=actor,
                access_context=access_context,
            )
        except Exception as exc:
            cleanup_uncommitted = False
            try:
                cleanup_uncommitted = (
                    self.store.get_deliverable_version(version_id).get("status") != "submitted"
                )
            except Exception:
                # If commit state cannot be established, preserve bytes to avoid deleting
                # a version whose metadata may already be durable.
                cleanup_uncommitted = False
            if cleanup_uncommitted:
                for path in newly_written:
                    path.unlink(missing_ok=True)
                if version_dir.exists() and not any(version_dir.iterdir()):
                    version_dir.rmdir()
                try:
                    self.store.fail_deliverable_version_record(
                        version_id,
                        f"{type(exc).__name__}: {exc}",
                    )
                except Exception:
                    pass
            raise

    def resolve_download(
        self,
        file_id: str,
        *,
        actor: User,
        access_context: AccessContext,
    ) -> Path:
        row = self.store.get_deliverable_file(file_id)
        deliverable = self.store.get_deliverable(row["deliverable_id"])
        if not visible(actor, deliverable, access_context) or not visible(actor, row, access_context):
            raise PermissionError("Deliverable file is outside the actor's visible scope")
        root = self.store.root_dir.resolve()
        path = (self.store.root_dir / row["relative_path"]).resolve()
        if root not in path.parents:
            raise DeliverableValidationError("Stored deliverable path escaped the project store")
        if not path.is_file():
            raise FileNotFoundError(f"Stored deliverable file is missing: {file_id}")
        actual_hash = _file_sha256(path)
        if actual_hash != row["content_hash"]:
            raise DeliverableValidationError(f"Stored deliverable hash mismatch: {file_id}")
        return path

    @staticmethod
    def _is_management(actor: User, access_context: AccessContext, project_id: str) -> bool:
        return ROLE_RANK.get(
            access_context.role_of(actor, project_id), -1
        ) >= ROLE_RANK["pmo"]

    def _require_management(
        self,
        actor: User,
        access_context: AccessContext,
        project_id: str,
    ) -> None:
        if project_id not in access_context.projects_of(actor) or not self._is_management(
            actor,
            access_context,
            project_id,
        ):
            raise PermissionError("Only project management can maintain deliverable requirements")


def _safe_filename(filename: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z._()\-\u3400-\u9fff]+", "_", filename).strip("._")
    return normalized or "deliverable_file"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
