from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.schemas import (
    AgentLoopRound,
    EvidenceVerificationResult,
    ExtractionResult,
    InspectionItem,
    InspectionReport,
    MilestonePlan,
    PeopleStructure,
    SourceDocument,
)
from store.sqlite_store import ProjectSQLiteStore


@dataclass(frozen=True)
class InspectionTrigger:
    trigger_type: str
    entity_type: str = ""
    entity_id: str = ""
    event_id: str = ""
    source_ids: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class InspectionContext:
    run_id: str
    milestone: MilestonePlan
    store: ProjectSQLiteStore
    project_id: str
    trigger: InspectionTrigger = field(
        default_factory=lambda: InspectionTrigger(trigger_type="manual")
    )
    manifest: list[SourceDocument] = field(default_factory=list)
    people_structure: PeopleStructure | None = None
    extractions: list[ExtractionResult] = field(default_factory=list)
    memory_items: list[InspectionItem] | None = None
    note_materials: list[dict[str, Any]] | None = None
    report: InspectionReport | None = None
    verification: EvidenceVerificationResult | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)
    harness_state: dict[str, Any] = field(default_factory=dict)
    loop_rounds: list[AgentLoopRound] = field(default_factory=list)
    stop_reason: str = ""
    input_snapshot: dict[str, Any] = field(default_factory=dict)
    source_texts: dict[str, str] = field(default_factory=dict)
    model_io_events: list[dict[str, Any]] = field(default_factory=list)
    tool_failures: dict[str, str] = field(default_factory=dict)
    no_change_reason: str = ""
