from __future__ import annotations

from pathlib import Path
from typing import Iterable

from agent.repository_paths import resolve_repository_path
from agent.schemas import (
    CandidateItem,
    EvidenceRef,
    EvidenceVerificationResult,
    InspectionItem,
    InspectionReport,
    SourceDocument,
)


def has_evidence(item: InspectionItem) -> bool:
    return bool(item.evidence_refs)


def evidence_labels(refs: list[EvidenceRef]) -> list[str]:
    return [
        f"{ref.source_doc_id} / {ref.source_kind} / {ref.locator}"
        for ref in refs
    ]


def verify_report_evidence(
    report: InspectionReport,
    manifest: list[SourceDocument],
    *,
    allowed_roots: Iterable[str | Path] = (),
) -> EvidenceVerificationResult:
    return verify_items_evidence(
        report.chain_gaps + report.responsibility_gaps + report.followup_drafts,
        manifest,
        allowed_roots=allowed_roots,
    )


def verify_items_evidence(
    items: list[InspectionItem] | list[CandidateItem],
    manifest: list[SourceDocument],
    *,
    allowed_roots: Iterable[str | Path] = (),
) -> EvidenceVerificationResult:
    source_by_id = {source.doc_id: source for source in manifest}
    errors: list[str] = []
    warnings: list[str] = []
    checked_count = 0
    raw_evidence_count = 0
    raw_pending_count = 0
    text_cache: dict[str, str] = {}
    for item in items:
        checked_count += 1
        if not item.evidence_refs:
            errors.append(f"{item.item_id} has no evidence_refs")
            continue
        for ref in item.evidence_refs:
            source = source_by_id.get(ref.source_doc_id)
            if source is None:
                errors.append(f"{item.item_id} references unknown source {ref.source_doc_id}")
                continue
            if ref.source_kind not in {"curated_source", "raw_source"}:
                errors.append(f"{item.item_id} has invalid source_kind {ref.source_kind}")
            if not ref.locator:
                errors.append(f"{item.item_id} has empty evidence locator")
            if not ref.quote:
                errors.append(f"{item.item_id} has empty evidence quote")
            source_path = _source_path(source, ref.source_kind)
            if ref.source_kind == "curated_source":
                path = source.curated_source.path
                if not path or not resolve_repository_path(path, allowed_roots=allowed_roots).exists():
                    errors.append(f"{item.item_id} curated_source is missing on disk")
            if ref.source_kind == "raw_source":
                path = source.raw_source.path
                if source.raw_source.status == "raw_source_pending":
                    errors.append(f"{item.item_id} references pending raw_source")
                elif not path or not resolve_repository_path(path, allowed_roots=allowed_roots).exists():
                    errors.append(f"{item.item_id} raw_source is missing on disk")
            if source_path and resolve_repository_path(source_path, allowed_roots=allowed_roots).exists() and ref.locator != "source_manifest":
                normalized_source = text_cache.get(source_path)
                if normalized_source is None:
                    normalized_source = _normalize(
                        resolve_repository_path(source_path, allowed_roots=allowed_roots).read_text(
                            encoding="utf-8",
                            errors="ignore",
                        )
                    )
                    text_cache[source_path] = normalized_source
                if ref.quote and _normalize(ref.quote) not in normalized_source:
                    errors.append(f"{item.item_id} evidence quote is not present in {ref.source_kind}")

            if (
                source.raw_source.status == "matched"
                and source.raw_source.path
                and resolve_repository_path(
                    source.raw_source.path,
                    allowed_roots=allowed_roots,
                ).exists()
            ):
                raw_evidence_count += 1
            else:
                raw_pending_count += 1
                warnings.append(
                    f"{item.item_id} has no matched raw transcript; keep as candidate and collect raw evidence"
                )
    return EvidenceVerificationResult(
        ok=not errors,
        checked_count=checked_count,
        errors=errors,
        warnings=warnings,
        raw_evidence_count=raw_evidence_count,
        raw_pending_count=raw_pending_count,
    )


def _source_path(source: SourceDocument, source_kind: str) -> str:
    if source_kind == "raw_source":
        return source.raw_source.path or ""
    return source.curated_source.path or ""


def _normalize(value: str) -> str:
    return "".join(value.split())
