from __future__ import annotations

from dataclasses import dataclass

from agent.inspection import build_timeline, inspect_delivery_chain
from agent.llm_provider import get_provider
from agent.schemas import ExtractionResult, InspectionReport, PeopleStructure, SourceDocument, TimelineResult
from ingestion.people_asset import load_people_asset
from ingestion.source_manifest import build_default_manifest, is_narrative_source, read_curated_text
from agent.semantic_extraction import extract_document_semantically


@dataclass
class WorkspaceState:
    manifest: list[SourceDocument]
    people_structure: PeopleStructure
    extractions: list[ExtractionResult]
    timeline: TimelineResult
    inspection_report: InspectionReport


def load_workspace(
    people_asset_path: str,
    *,
    scenario_id: str,
    chain_name: str,
) -> WorkspaceState:
    manifest = build_default_manifest()
    people_structure = load_people_asset(people_asset_path)
    provider = get_provider(role="extraction")
    extractions = [
        extract_document_semantically(item, read_curated_text(item), provider=provider)
        for item in manifest
        if is_narrative_source(item) and item.curated_source.path
    ]
    timeline = build_timeline(extractions)
    inspection_report = inspect_delivery_chain(
        scenario_id=scenario_id,
        chain_name=chain_name,
        manifest=manifest,
        extractions=extractions,
        people_structure=people_structure,
    )
    return WorkspaceState(
        manifest=manifest,
        people_structure=people_structure,
        extractions=extractions,
        timeline=timeline,
        inspection_report=inspection_report,
    )
