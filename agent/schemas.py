from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class CandidateStatus(str, Enum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class WorkItemStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    PENDING_ACCEPTANCE = "pending_acceptance"
    DONE = "done"
    CANCELED = "canceled"
    ARCHIVED = "archived"


@dataclass
class SourceRef:
    source_type: str
    path: Optional[str]
    status: str
    notes: str = ""


@dataclass
class SourceDocument:
    doc_id: str
    title: str
    meeting_date: str
    topic: str
    curated_source: SourceRef
    raw_source: SourceRef
    tags: List[str]
    org_id: str = "org_mvp"
    project_id: str = "project_mvp"
    topic_id: Optional[str] = None
    author_id: str = "system"
    sensitivity: str = "l1"
    tag_origin: str = "runtime_default"
    input_kind: str = "auto"
    content_hash: str = ""


@dataclass
class EvidenceRef:
    source_doc_id: str
    source_kind: str
    locator: str
    quote: str
    raw_source_doc_id: str = ""
    raw_locator: str = ""
    evidence_level: str = "curated_pending_raw"
    ingestion_job_id: str = ""


@dataclass
class CandidateItem:
    item_id: str
    category: str
    title: str
    description: str
    evidence_refs: List[EvidenceRef]
    status: CandidateStatus = CandidateStatus.CANDIDATE
    owner_candidates: Optional[List[str]] = None
    related_scenarios: Optional[List[str]] = None
    matter_type: Optional[str] = None
    facet_types: Optional[List[str]] = None
    business_goal: Optional[str] = None
    principles: Optional[List[str]] = None
    reasoning_chain: Optional[List[str]] = None
    applicable_scope: Optional[str] = None
    linked_issue_ids: Optional[List[str]] = None
    linked_task_ids: Optional[List[str]] = None
    due_date: Optional[str] = None
    deliverable: Optional[str] = None
    acceptance_criteria: Optional[str] = None
    professional_id: str = ""
    board_id: str = ""
    inference_note: Optional[str] = None
    observed_fields: List[str] = field(default_factory=list)
    proposed_fields: List[str] = field(default_factory=list)
    inference_basis: List[str] = field(default_factory=list)
    inference_confidence: str = ""
    org_id: str = "org_mvp"
    project_id: str = "project_mvp"
    topic_id: Optional[str] = None
    author_id: str = "system"
    sensitivity: str = "l1"
    proposed_sensitivity: Optional[str] = None
    sensitivity_reason: Optional[str] = None
    tag_origin: str = "runtime_default"
    thread_id: Optional[str] = None
    thread_title: Optional[str] = None
    thread_key: Optional[str] = None
    thread_event: str = "new"
    effective_at: Optional[str] = None
    supersedes_item_id: Optional[str] = None
    review_required: bool = True
    changed_fields: List[str] = field(default_factory=list)
    claim_hash: str = ""


@dataclass
class ExtractionResult:
    source_doc_id: str
    meeting_date: str
    title: str
    people: List[CandidateItem]
    things: List[CandidateItem]
    methods: List[CandidateItem]
    chain_links: List[CandidateItem]
    questions: List[CandidateItem]
    status: CandidateStatus = CandidateStatus.CANDIDATE


@dataclass
class PersonAssignment:
    assignment_id: str
    group: str
    role: str
    path: str
    responsibility_note: str = ""
    source_id: str = ""


@dataclass
class Person:
    name: str
    group: str
    role: str
    path: str
    responsibility_note: str = ""
    person_id: str = ""
    identity_status: str = "confirmed"
    assignments: List[PersonAssignment] = field(default_factory=list)
    source_id: str = ""


@dataclass
class PeopleGroup:
    name: str
    path: str


@dataclass
class ScenarioOrg:
    scenario_id: str
    name: str
    teams: List[str]
    leads: List[str]
    path: str


@dataclass
class PeopleStructure:
    root_title: str
    groups: List[PeopleGroup]
    people: List[Person]
    scenarios: List[ScenarioOrg]


@dataclass
class TimelineEvent:
    event_id: str
    meeting_date: str
    title: str
    category: str
    source_doc_id: str
    evidence_refs: List[EvidenceRef]
    status: CandidateStatus = CandidateStatus.CANDIDATE


@dataclass
class TimelineResult:
    events: List[TimelineEvent]


@dataclass
class InspectionItem:
    item_id: str
    category: str
    title: str
    description: str
    evidence_refs: List[EvidenceRef]
    status: CandidateStatus = CandidateStatus.CANDIDATE
    owner_candidates: Optional[List[str]] = None
    next_step: Optional[str] = None
    confirmation_notes: str = ""
    confirmation_editor: str = ""
    linked_issue_ids: Optional[List[str]] = None
    linked_task_ids: Optional[List[str]] = None
    updated_at: str = ""
    matter_type: Optional[str] = None
    facet_types: Optional[List[str]] = None
    due_date: Optional[str] = None
    deliverable: Optional[str] = None
    acceptance_criteria: Optional[str] = None
    professional_id: str = ""
    board_id: str = ""
    inference_note: Optional[str] = None
    observed_fields: List[str] = field(default_factory=list)
    proposed_fields: List[str] = field(default_factory=list)
    inference_basis: List[str] = field(default_factory=list)
    inference_confidence: str = ""
    business_goal: Optional[str] = None
    principles: Optional[List[str]] = None
    reasoning_chain: Optional[List[str]] = None
    applicable_scope: Optional[str] = None
    org_id: str = "org_mvp"
    project_id: str = "project_mvp"
    topic_id: Optional[str] = None
    author_id: str = "system"
    sensitivity: str = "l1"
    proposed_sensitivity: Optional[str] = None
    sensitivity_reason: Optional[str] = None
    tag_origin: str = "runtime_default"
    thread_id: Optional[str] = None
    thread_title: Optional[str] = None
    thread_key: Optional[str] = None
    thread_event: str = "new"
    effective_at: Optional[str] = None
    supersedes_item_id: Optional[str] = None
    review_required: bool = True
    changed_fields: List[str] = field(default_factory=list)
    claim_hash: str = ""


@dataclass
class WorkItem:
    work_item_id: str
    title: str
    description: str
    status: WorkItemStatus
    evidence_refs: List[EvidenceRef]
    owner_candidates: Optional[List[str]] = None
    collaborators: Optional[List[str]] = None
    confirmers: Optional[List[str]] = None
    due_date: Optional[str] = None
    deliverable: Optional[str] = None
    acceptance_criteria: Optional[str] = None
    professional_id: str = ""
    board_id: str = ""
    planned_start: str = ""
    progress_percent: Optional[int] = None
    status_updated_at: str = ""
    milestone_id: str = ""
    source_candidate_id: str = ""
    source_run_id: str = ""
    version: int = 1
    created_at: str = ""
    updated_at: str = ""
    confirmation_notes: str = ""
    confirmation_editor: str = ""
    linked_issue_ids: Optional[List[str]] = None
    linked_task_ids: Optional[List[str]] = None
    org_id: str = "org_mvp"
    project_id: str = "project_mvp"
    topic_id: Optional[str] = None
    author_id: str = "system"
    sensitivity: str = "l1"
    proposed_sensitivity: Optional[str] = None
    sensitivity_reason: Optional[str] = None
    tag_origin: str = "runtime_default"


@dataclass
class InspectionReport:
    scenario_id: str
    chain_name: str
    chain_gaps: List[InspectionItem]
    responsibility_gaps: List[InspectionItem]
    followup_drafts: List[InspectionItem]
    status: CandidateStatus = CandidateStatus.CANDIDATE


@dataclass
class AgentPlanStep:
    step_id: str
    tool_name: str
    reason: str


@dataclass
class AgentStep:
    step_id: str
    tool_name: str
    status: str
    input_summary: str
    output_summary: str


@dataclass
class AgentObservation:
    observation_id: str
    tool_name: str
    summary: str
    data: Dict[str, Any]

@dataclass
class AgentLoopRound:
    round_index: int
    phase: str
    objective: str
    model_input_summary: str
    model_output_summary: str
    tool_name: str
    tool_result_summary: str
    decision: str
    state_changes: List[str]
    errors: List[str]
    stop_reason: str = ""
    round_kind: str = "tool"
    raw_response_id: str = ""
    token_usage: Dict[str, Any] = field(default_factory=dict)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class EvidenceVerificationResult:
    ok: bool
    checked_count: int
    errors: List[str]
    warnings: List[str] = field(default_factory=list)
    raw_evidence_count: int = 0
    raw_pending_count: int = 0


@dataclass
class MilestonePlan:
    milestone_id: str
    project: str
    name: str
    date_start: str
    date_end: str
    scenario_id: str
    chain_name: str
    acceptance_criteria: List[str]
    trigger_policy: Dict[str, Any]
    status: str = "active"


@dataclass
class AgentRun:
    run_id: str
    milestone_id: str
    milestone_plan: MilestonePlan
    objective: str
    plan: List[AgentPlanStep]
    steps: List[AgentStep]
    observations: List[AgentObservation]
    final_report: Optional[InspectionReport]
    confirmed_item_ids: List[str]
    created_at: str
    status: str = "completed"
    runtime_kind: str = "model_function_calling_inspection"
    verification: Optional[EvidenceVerificationResult] = None
    error: str = ""
    agent_trace: List[Dict[str, Any]] = field(default_factory=list)
    raw_response_count: int = 0
    input_snapshot: Dict[str, Any] = field(default_factory=dict)
    loop_rounds: List[AgentLoopRound] = field(default_factory=list)
    stop_reason: str = ""
    harness_state: Dict[str, Any] = field(default_factory=dict)
    structured_output: Dict[str, Any] = field(default_factory=dict)
    model_io_events: List[Dict[str, Any]] = field(default_factory=list)
    no_change_reason: str = ""


@dataclass
class ProjectAgentOutput:
    report: InspectionReport
    verification: EvidenceVerificationResult
    summary: str
    people: List[CandidateItem] = field(default_factory=list)
    things: List[CandidateItem] = field(default_factory=list)
    methods: List[CandidateItem] = field(default_factory=list)
    tasks: List[CandidateItem] = field(default_factory=list)
    open_questions: List[CandidateItem] = field(default_factory=list)
    stop_reason: str = ""
    no_change_reason: str = ""


def to_plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: to_plain(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: to_plain(item) for key, item in value.items()}
    return value


def evidence(source: SourceDocument, locator: str, quote: str) -> EvidenceRef:
    raw_matched = source.raw_source.status == "matched" and bool(source.raw_source.path)
    return EvidenceRef(
        source_doc_id=source.doc_id,
        source_kind="curated_source",
        locator=locator,
        quote=quote.strip()[:180],
        raw_source_doc_id=source.doc_id if raw_matched else "",
        raw_locator=locator if raw_matched else "",
        evidence_level="raw_traceable" if raw_matched else "curated_pending_raw",
    )


def source_row(source: SourceDocument) -> Dict[str, Any]:
    return {
        "doc_id": source.doc_id,
        "meeting_date": source.meeting_date,
        "title": source.title,
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
        "tags": ", ".join(source.tags),
    }
