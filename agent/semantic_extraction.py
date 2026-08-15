from __future__ import annotations

import json
import os
import re
from hashlib import sha1
from typing import Any

from agent.llm_provider import LLMProvider, get_provider
from agent.schemas import (
    CandidateItem,
    CandidateStatus,
    EvidenceRef,
    ExtractionResult,
    SourceDocument,
)


SECTIONS = ("people", "things", "methods", "tasks", "issues")
SENSITIVITY_RANK = {"l1": 1, "l2": 2, "l3": 3, "l4": 4}
PROVENANCE_FIELDS = {
    "title",
    "description",
    "owner_candidates",
    "due_date",
    "deliverable",
    "acceptance_criteria",
    "business_goal",
    "principles",
    "reasoning_chain",
    "applicable_scope",
}


class SemanticExtractionError(ValueError):
    non_retryable = True


def extract_document_semantically(
    source: SourceDocument,
    content: str,
    *,
    provider: LLMProvider | None = None,
    ingestion_job_id: str = "",
) -> ExtractionResult:
    active_provider = provider or get_provider(role="extraction")
    max_chars = max(1000, int(os.getenv("SEMANTIC_EXTRACTION_MAX_CHARS", "12000")))
    line_lookup = {
        index: line
        for index, line in enumerate(content.splitlines() or [content], start=1)
    }
    chunks = _numbered_line_chunks(line_lookup, max_chars=max_chars)
    extracted: list[ExtractionResult] = []
    for chunk_index, numbered_lines in enumerate(chunks, start=1):
        base_prompt = _extraction_prompt(source, numbered_lines)
        errors: list[str] = []
        for attempt in range(2):
            prompt = base_prompt
            if errors:
                prompt += (
                    "\n\nVALIDATION ERRORS FROM THE PREVIOUS RESPONSE:\n- "
                    + "\n- ".join(errors[-8:])
                    + "\nReturn a corrected JSON object. Do not repeat unsupported claims."
                )
            try:
                raw = active_provider.complete_json(prompt)
                extracted.append(
                    _parse_extraction(
                        source,
                        line_lookup,
                        raw,
                        ingestion_job_id=ingestion_job_id,
                    )
                )
                break
            except (SemanticExtractionError, json.JSONDecodeError, TypeError, ValueError) as exc:
                errors.append(str(exc))
                if attempt == 1:
                    detail = "; ".join(error for error in errors if error) or "unknown validation error"
                    raise SemanticExtractionError(
                        f"Semantic extraction chunk {chunk_index}/{len(chunks)} failed "
                        f"after one correction retry: {detail}"
                    ) from exc
    return _merge_extraction_results(source, extracted)


def _extraction_prompt(source: SourceDocument, numbered_lines: list[tuple[int, str]]) -> str:
    numbered = "\n".join(
        f"{line_number}: {line}"
        for line_number, line in numbered_lines
    )
    return f"""
You extract review candidates from one authorized project document.

Return exactly one JSON object with array keys: people, things, methods, tasks, issues.
Every item must contain title, description, status="candidate", and evidence_refs.
Every evidence ref must contain locator="line:N" and an exact quote copied from that same numbered line.
Evidence supports the observed problem, request, decision, or recurring pattern. You may make a concrete
proposal for an owner, due date, deliverable, acceptance criterion, business goal, principle, reasoning
step, or scope when the source does not state it verbatim. Never present a proposal as a meeting fact.
For methods and tasks, classify every populated business field in observed_fields or proposed_fields.
The two arrays must not overlap. When proposed_fields is non-empty, include non-empty inference_basis
and inference_confidence="low"|"medium"|"high". The basis must explain which source pattern or project
constraint supports the proposal; it is not a substitute for evidence_refs.
Every methods item must include business_goal, non-empty principles, non-empty reasoning_chain,
and applicable_scope. Every tasks item must include non-empty owner_candidates, due_date in
YYYY-MM-DD, deliverable, and acceptance_criteria. Other optional fields: matter_type,
facet_types, proposed_sensitivity, sensitivity_reason, observed_fields, proposed_fields,
inference_basis, inference_confidence.
Never return a final sensitivity. You may only propose l2/l3/l4 with a reason when it is stricter than the source.
Keep at most 30 total items. Empty arrays are valid.

SOURCE METADATA
doc_id: {source.doc_id}
title: {source.title}
meeting_date: {source.meeting_date}
topic: {source.topic}
inherited_sensitivity: {source.sensitivity}

NUMBERED SOURCE
{numbered}
""".strip()


def _parse_extraction(
    source: SourceDocument,
    line_lookup: dict[int, str],
    raw: str,
    *,
    ingestion_job_id: str,
) -> ExtractionResult:
    if len(raw) > 120_000:
        raise SemanticExtractionError("model JSON exceeds 120000 characters")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise SemanticExtractionError("model response must be a JSON object")
    for section in SECTIONS:
        if not isinstance(payload.get(section), list):
            raise SemanticExtractionError(f"missing array section: {section}")
    total = sum(len(payload[section]) for section in SECTIONS)
    if total > 30:
        raise SemanticExtractionError("model response exceeds the 30-item limit")

    buckets: dict[str, list[CandidateItem]] = {section: [] for section in SECTIONS}
    for section in SECTIONS:
        for index, raw_item in enumerate(payload[section]):
            buckets[section].append(
                _candidate_from_payload(
                    source,
                    line_lookup,
                    section,
                    index,
                    raw_item,
                    ingestion_job_id=ingestion_job_id,
                )
            )
    return ExtractionResult(
        source_doc_id=source.doc_id,
        meeting_date=source.meeting_date,
        title=source.title,
        people=buckets["people"],
        things=[*buckets["things"], *buckets["issues"]],
        methods=buckets["methods"],
        chain_links=[],
        questions=buckets["tasks"],
        status=CandidateStatus.CANDIDATE,
    )


def _candidate_from_payload(
    source: SourceDocument,
    line_lookup: dict[int, str],
    section: str,
    index: int,
    raw_item: Any,
    *,
    ingestion_job_id: str,
) -> CandidateItem:
    label = f"{section}[{index}]"
    if not isinstance(raw_item, dict):
        raise SemanticExtractionError(f"{label} must be an object")
    if raw_item.get("status") != CandidateStatus.CANDIDATE.value:
        raise SemanticExtractionError(f"{label} status must be candidate")
    if "sensitivity" in raw_item:
        raise SemanticExtractionError(f"{label} cannot set final sensitivity; use proposed_sensitivity")
    title = _required_text(raw_item, "title", label, limit=160)
    description = _required_text(raw_item, "description", label, limit=1000)
    refs = _evidence_refs(
        source,
        line_lookup,
        raw_item.get("evidence_refs"),
        label,
        ingestion_job_id=ingestion_job_id,
    )
    proposed, reason = _proposed_sensitivity(source, raw_item, label)
    due_date = _optional_text(raw_item.get("due_date"), 10)
    if due_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due_date):
        raise SemanticExtractionError(f"{label} due_date must be YYYY-MM-DD")
    category = "issue" if section == "issues" else "task" if section == "tasks" else section
    facets = _string_list(raw_item.get("facet_types"), limit=8)
    if section == "issues" and "problem" not in facets:
        facets.append("problem")
    if section == "tasks" and "task" not in facets:
        facets.append("task")
    item_id = _stable_item_id(source.doc_id, section, title, refs[0].quote)
    observed_fields = _string_list(raw_item.get("observed_fields"), limit=20)
    proposed_fields = _string_list(raw_item.get("proposed_fields"), limit=20)
    if section not in {"methods", "tasks"} and not observed_fields and not proposed_fields:
        observed_fields = ["title", "description"]
    inference_basis = _string_list(raw_item.get("inference_basis"), limit=12)
    inference_confidence = _optional_text(raw_item.get("inference_confidence"), 12).lower()
    candidate = CandidateItem(
        item_id=item_id,
        category=category,
        title=title,
        description=description,
        evidence_refs=refs,
        status=CandidateStatus.CANDIDATE,
        owner_candidates=_string_list(raw_item.get("owner_candidates"), limit=12) or None,
        related_scenarios=_string_list(raw_item.get("related_scenarios"), limit=12) or None,
        matter_type=_optional_text(raw_item.get("matter_type"), 40) or None,
        facet_types=facets or None,
        business_goal=_optional_text(raw_item.get("business_goal"), 500) or None,
        principles=_string_list(raw_item.get("principles"), limit=12) or None,
        reasoning_chain=_string_list(raw_item.get("reasoning_chain"), limit=12) or None,
        applicable_scope=_optional_text(raw_item.get("applicable_scope"), 300) or None,
        due_date=due_date or None,
        deliverable=_optional_text(raw_item.get("deliverable"), 500) or None,
        acceptance_criteria=_optional_text(raw_item.get("acceptance_criteria"), 800) or None,
        inference_note=(
            "模型提出的字段：" + "、".join(proposed_fields) + "。"
            if proposed_fields
            else "模型语义抽取候选，字段均标记为来源观察。"
        ),
        observed_fields=observed_fields,
        proposed_fields=proposed_fields,
        inference_basis=inference_basis,
        inference_confidence=inference_confidence,
        org_id=source.org_id,
        project_id=source.project_id,
        topic_id=source.topic_id,
        author_id=source.author_id,
        sensitivity=source.sensitivity,
        proposed_sensitivity=proposed,
        sensitivity_reason=reason,
        tag_origin="source_inherited",
    )
    _validate_field_provenance(candidate, section=section, label=label)
    if section == "methods" and not (
        candidate.business_goal
        and candidate.principles
        and candidate.reasoning_chain
        and candidate.applicable_scope
    ):
        raise SemanticExtractionError(
            f"{label} method requires business_goal, principles, reasoning_chain, and applicable_scope"
        )
    if section == "tasks" and not (
        candidate.owner_candidates
        and candidate.due_date
        and candidate.deliverable
        and candidate.acceptance_criteria
    ):
        raise SemanticExtractionError(
            f"{label} task requires owner_candidates, due_date, deliverable, and acceptance_criteria"
        )
    return candidate


def _validate_field_provenance(
    candidate: CandidateItem,
    *,
    section: str,
    label: str,
) -> None:
    observed = set(candidate.observed_fields)
    proposed = set(candidate.proposed_fields)
    unknown = (observed | proposed) - PROVENANCE_FIELDS
    if unknown:
        raise SemanticExtractionError(
            f"{label} field provenance contains unsupported fields: {', '.join(sorted(unknown))}"
        )
    overlap = observed & proposed
    if overlap:
        raise SemanticExtractionError(
            f"{label} fields cannot be both observed and proposed: {', '.join(sorted(overlap))}"
        )
    populated = {
        field_name
        for field_name in PROVENANCE_FIELDS
        if _candidate_field_populated(candidate, field_name)
    }
    required_provenance = populated if section in {"methods", "tasks"} else {"title", "description"}
    missing = required_provenance - observed - proposed
    if missing:
        raise SemanticExtractionError(
            f"{label} must classify populated fields as observed or proposed: {', '.join(sorted(missing))}"
        )
    if proposed:
        if not candidate.inference_basis:
            raise SemanticExtractionError(f"{label} inference_basis is required for proposed fields")
        if candidate.inference_confidence not in {"low", "medium", "high"}:
            raise SemanticExtractionError(
                f"{label} inference_confidence must be low, medium, or high"
            )
    elif candidate.inference_basis or candidate.inference_confidence:
        raise SemanticExtractionError(
            f"{label} inference metadata requires at least one proposed field"
        )


def _candidate_field_populated(candidate: CandidateItem, field_name: str) -> bool:
    value = getattr(candidate, field_name)
    if isinstance(value, list):
        return bool([item for item in value if str(item).strip()])
    return bool(str(value or "").strip())


def _evidence_refs(
    source: SourceDocument,
    line_lookup: dict[int, str],
    value: Any,
    label: str,
    *,
    ingestion_job_id: str,
) -> list[EvidenceRef]:
    if not isinstance(value, list) or not value:
        raise SemanticExtractionError(f"{label} must contain evidence_refs")
    refs: list[EvidenceRef] = []
    for ref_index, raw_ref in enumerate(value[:3]):
        if not isinstance(raw_ref, dict):
            raise SemanticExtractionError(f"{label}.evidence_refs[{ref_index}] must be an object")
        locator = str(raw_ref.get("locator") or "").strip()
        match = re.fullmatch(r"line:(\d+)", locator)
        if not match:
            raise SemanticExtractionError(f"{label}.evidence_refs[{ref_index}] locator must be line:N")
        line_no = int(match.group(1))
        if line_no not in line_lookup:
            raise SemanticExtractionError(f"{label}.evidence_refs[{ref_index}] line is outside the source")
        quote = _required_text(raw_ref, "quote", f"{label}.evidence_refs[{ref_index}]", limit=300)
        if _normalize_quote(quote) not in _normalize_quote(line_lookup[line_no]):
            raise SemanticExtractionError(
                f"{label}.evidence_refs[{ref_index}] quote is not present on {locator}"
            )
        refs.append(EvidenceRef(
            source_doc_id=source.doc_id,
            source_kind="curated_source",
            locator=locator,
            quote=quote,
            ingestion_job_id=ingestion_job_id,
        ))
    return refs


def _proposed_sensitivity(
    source: SourceDocument,
    raw_item: dict[str, Any],
    label: str,
) -> tuple[str | None, str | None]:
    proposed = _optional_text(raw_item.get("proposed_sensitivity"), 2).lower()
    if not proposed:
        return None, None
    if proposed not in SENSITIVITY_RANK:
        raise SemanticExtractionError(f"{label} proposed_sensitivity must be l1..l4")
    if SENSITIVITY_RANK[proposed] <= SENSITIVITY_RANK.get(source.sensitivity, 0):
        raise SemanticExtractionError(f"{label} proposed_sensitivity must be stricter than the source")
    reason = _optional_text(raw_item.get("sensitivity_reason"), 500)
    if not reason:
        raise SemanticExtractionError(f"{label} sensitivity_reason is required for a proposal")
    return proposed, reason


def _required_text(value: dict[str, Any], field: str, label: str, *, limit: int) -> str:
    text = _optional_text(value.get(field), limit)
    if not text:
        raise SemanticExtractionError(f"{label} missing {field}")
    return text


def _optional_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def _string_list(value: Any, *, limit: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SemanticExtractionError("list field must be an array")
    return [_optional_text(item, 200) for item in value[:limit] if _optional_text(item, 200)]


def _normalize_quote(value: str) -> str:
    return " ".join(value.strip().split()).lower()


def _stable_item_id(source_doc_id: str, section: str, title: str, quote: str) -> str:
    digest = sha1(f"{source_doc_id}:{section}:{title}:{quote}".encode("utf-8")).hexdigest()[:16]
    return f"semantic_{section}_{digest}"


def _numbered_line_chunks(
    line_lookup: dict[int, str],
    *,
    max_chars: int,
) -> list[list[tuple[int, str]]]:
    segments: list[tuple[int, str]] = []
    for line_number, line in line_lookup.items():
        if not line:
            segments.append((line_number, ""))
            continue
        for start in range(0, len(line), max_chars):
            segments.append((line_number, line[start:start + max_chars]))

    chunks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    current_chars = 0
    for segment in segments:
        segment_chars = len(segment[1]) + len(str(segment[0])) + 2
        if current and current_chars + segment_chars > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(segment)
        current_chars += segment_chars
    if current:
        chunks.append(current)
    return chunks or [[(1, "")]]


def _merge_extraction_results(
    source: SourceDocument,
    results: list[ExtractionResult],
) -> ExtractionResult:
    return ExtractionResult(
        source_doc_id=source.doc_id,
        meeting_date=source.meeting_date,
        title=source.title,
        people=_dedupe_candidates([item for result in results for item in result.people]),
        things=_dedupe_candidates([item for result in results for item in result.things]),
        methods=_dedupe_candidates([item for result in results for item in result.methods]),
        chain_links=_dedupe_candidates([item for result in results for item in result.chain_links]),
        questions=_dedupe_candidates([item for result in results for item in result.questions]),
        status=CandidateStatus.CANDIDATE,
    )


def _dedupe_candidates(items: list[CandidateItem]) -> list[CandidateItem]:
    seen: set[str] = set()
    result: list[CandidateItem] = []
    for item in items:
        if item.item_id in seen:
            continue
        seen.add(item.item_id)
        result.append(item)
    return result
