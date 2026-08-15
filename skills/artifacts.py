from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


FACT_STATUSES = {"candidate", "confirmed", "rejected", "archived"}


@dataclass(frozen=True)
class EvidenceLink:
    source_id: str
    locator: str
    quote: str = ""
    version: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvidenceLink":
        return cls(
            source_id=str(value.get("source_id") or value.get("source_doc_id") or ""),
            locator=str(value.get("locator") or ""),
            quote=str(value.get("quote") or ""),
            version=str(value.get("version") or ""),
        )


@dataclass(frozen=True)
class ProjectFact:
    object_id: str
    object_type: str
    title: str
    statement: str
    status: str = "candidate"
    version: str = ""
    confidence: float | None = None
    evidence_refs: tuple[EvidenceLink, ...] = ()
    valid_from: str = ""
    valid_to: str = ""

    def __post_init__(self) -> None:
        if self.status not in FACT_STATUSES:
            raise ValueError(f"不支持的事实状态：{self.status}")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProjectFact":
        return cls(
            object_id=str(value.get("object_id") or value.get("id") or ""),
            object_type=str(value.get("object_type") or value.get("type") or "unknown"),
            title=str(value.get("title") or ""),
            statement=str(value.get("statement") or value.get("description") or ""),
            status=str(value.get("status") or "candidate"),
            version=str(value.get("version") or ""),
            confidence=float(value["confidence"]) if value.get("confidence") is not None else None,
            evidence_refs=tuple(EvidenceLink.from_dict(item) for item in value.get("evidence_refs") or []),
            valid_from=str(value.get("valid_from") or ""),
            valid_to=str(value.get("valid_to") or ""),
        )


@dataclass(frozen=True)
class ProjectRelationship:
    relationship_id: str
    relationship_type: str
    source_object_id: str
    target_object_id: str
    status: str = "candidate"
    evidence_refs: tuple[EvidenceLink, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProjectRelationship":
        return cls(
            relationship_id=str(value.get("relationship_id") or value.get("id") or ""),
            relationship_type=str(value.get("relationship_type") or value.get("type") or "related_to"),
            source_object_id=str(value.get("source_object_id") or value.get("source_id") or ""),
            target_object_id=str(value.get("target_object_id") or value.get("target_id") or ""),
            status=str(value.get("status") or "candidate"),
            evidence_refs=tuple(EvidenceLink.from_dict(item) for item in value.get("evidence_refs") or []),
        )


@dataclass
class ProjectGraphArtifact:
    project_id: str
    facts: list[ProjectFact] = field(default_factory=list)
    relationships: list[ProjectRelationship] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceBackedAnswer:
    question: str
    answer: str
    conclusion_status: str
    evidence_refs: list[EvidenceLink] = field(default_factory=list)
    related_object_ids: list[str] = field(default_factory=list)
    confidence: float | None = None
    unknown_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TaskNode:
    task_id: str
    title: str
    description: str = ""
    parent_task_id: str = ""
    milestone_id: str = ""
    owners: tuple[str, ...] = ()
    collaborators: tuple[str, ...] = ()
    confirmers: tuple[str, ...] = ()
    predecessor_ids: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    priority: str = ""
    professional_id: str = ""
    board_id: str = ""
    date_start: str = ""
    date_end: str = ""
    status: str = "candidate"
    evidence_refs: tuple[EvidenceLink, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TaskNode":
        acceptance = value.get("acceptance_criteria") or []
        if isinstance(acceptance, str):
            acceptance = [acceptance]
        outputs = value.get("outputs") or value.get("deliverables") or []
        if isinstance(outputs, str):
            outputs = [outputs]
        return cls(
            task_id=str(value.get("task_id") or value.get("work_item_id") or value.get("id") or ""),
            title=str(value.get("title") or ""),
            description=str(value.get("description") or ""),
            parent_task_id=str(value.get("parent_task_id") or ""),
            milestone_id=str(value.get("milestone_id") or ""),
            owners=tuple(str(item) for item in value.get("owners") or value.get("owner_candidates") or []),
            collaborators=tuple(str(item) for item in value.get("collaborators") or []),
            confirmers=tuple(str(item) for item in value.get("confirmers") or []),
            predecessor_ids=tuple(str(item) for item in value.get("predecessor_ids") or []),
            inputs=tuple(str(item) for item in value.get("inputs") or []),
            outputs=tuple(str(item) for item in outputs),
            acceptance_criteria=tuple(str(item) for item in acceptance),
            priority=str(value.get("priority") or ""),
            professional_id=str(value.get("professional_id") or ""),
            board_id=str(value.get("board_id") or ""),
            date_start=str(value.get("date_start") or ""),
            date_end=str(value.get("date_end") or value.get("due_date") or ""),
            status=str(value.get("status") or "candidate"),
            evidence_refs=tuple(EvidenceLink.from_dict(item) for item in value.get("evidence_refs") or []),
        )


@dataclass
class TaskGraphArtifact:
    project_id: str
    goal: str
    tasks: list[TaskNode] = field(default_factory=list)
    root_task_ids: list[str] = field(default_factory=list)
    critical_path_task_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskContextPackage:
    task: TaskNode
    background: str = ""
    current_conclusions: list[dict[str, Any]] = field(default_factory=list)
    materials: list[dict[str, Any]] = field(default_factory=list)
    related_people: list[dict[str, Any]] = field(default_factory=list)
    templates: list[dict[str, Any]] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)
    pending_confirmations: list[dict[str, Any]] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DecisionItemArtifact:
    decision_id: str
    question: str
    decision_level: str
    decision_makers: list[str] = field(default_factory=list)
    participants: list[str] = field(default_factory=list)
    options: list[dict[str, Any]] = field(default_factory=list)
    due_date: str = ""
    affected_object_ids: list[str] = field(default_factory=list)
    status: str = "candidate"
    final_conclusion: str = ""
    evidence_refs: list[EvidenceLink] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProgressEvidenceArtifact:
    task_id: str
    inferred_status: str
    completion_percent: float | None = None
    latest_progress: str = ""
    next_step: str = ""
    blockers: list[str] = field(default_factory=list)
    evidence_refs: list[EvidenceLink] = field(default_factory=list)
    confidence: float | None = None
    observed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DependencyEdge:
    dependency_id: str
    provider_id: str
    consumer_id: str
    subject: str
    input_output: str = ""
    planned_at: str = ""
    status: str = "candidate"
    owners: tuple[str, ...] = ()
    waiting_days: int = 0
    evidence_refs: tuple[EvidenceLink, ...] = ()


@dataclass
class DependencyGraphArtifact:
    project_id: str
    dependencies: list[DependencyEdge] = field(default_factory=list)
    broken_dependency_ids: list[str] = field(default_factory=list)
    critical_dependency_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReportPackageArtifact:
    report_type: str
    audience_role: str
    period: str
    summary: list[str] = field(default_factory=list)
    progress: list[dict[str, Any]] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    next_actions: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[EvidenceLink] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
