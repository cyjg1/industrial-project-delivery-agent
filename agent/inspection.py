from __future__ import annotations

from collections import defaultdict
from typing import Iterable, List

from agent.schemas import (
    CandidateItem,
    EvidenceRef,
    ExtractionResult,
    InspectionItem,
    InspectionReport,
    PeopleStructure,
    SourceDocument,
    TimelineEvent,
    TimelineResult,
)


def build_timeline(extractions: List[ExtractionResult]) -> TimelineResult:
    events: list[TimelineEvent] = []
    for extraction in sorted(extractions, key=lambda item: (item.meeting_date, item.source_doc_id)):
        candidates = extraction.things + extraction.methods + extraction.chain_links + extraction.questions
        for item in candidates:
            if _is_project_event(item):
                events.append(
                    TimelineEvent(
                        event_id=f"{extraction.source_doc_id}:{item.item_id}",
                        meeting_date=extraction.meeting_date,
                        title=item.title,
                        category=item.category,
                        source_doc_id=extraction.source_doc_id,
                        evidence_refs=item.evidence_refs,
                        status=item.status,
                    )
                )
    return TimelineResult(events=events)


def inspect_delivery_chain(
    scenario_id: str,
    chain_name: str,
    manifest: List[SourceDocument],
    extractions: List[ExtractionResult],
    people_structure: PeopleStructure,
) -> InspectionReport:
    scenario = next(
        (item for item in people_structure.scenarios if item.scenario_id == scenario_id),
        None,
    )
    scenario_leads = scenario.leads if scenario else []
    chain_gaps: list[InspectionItem] = []
    responsibility_gaps: list[InspectionItem] = []
    followup_drafts: list[InspectionItem] = []

    questions = [question for extraction in extractions for question in extraction.questions]
    for question in questions:
        owners = _dedupe(list(question.owner_candidates or []) + scenario_leads) or ["待人工确认责任人"]
        matter_type = question.matter_type or _infer_matter_type(question.title, question.description)
        is_responsibility = any(
            keyword in f"{question.title} {question.description}"
            for keyword in ["责任", "负责人", "角色", "权限", "人员", "分工"]
        )
        gap = InspectionItem(
            item_id=f"gap_{question.item_id}",
            category="责任缺口" if is_responsibility else "事项缺口",
            title=question.title,
            description=(
                f"{question.description} 该问题若不闭环，会使{_matter_label(matter_type)}"
                "停留在会议表述，无法形成可执行、可验收的项目结论。"
            ),
            evidence_refs=question.evidence_refs,
            owner_candidates=owners,
            next_step="把问题转成责任人、截止日期、交付物和验收口径完整的任务候选。",
            matter_type=matter_type,
            facet_types=_dedupe(list(question.facet_types or []) + ["problem"]),
            inference_note=question.inference_note,
        )
        if is_responsibility:
            responsibility_gaps.append(gap)
        else:
            chain_gaps.append(gap)

        followup_drafts.append(
            InspectionItem(
                item_id=f"task_{question.item_id}",
                category="闭环待办",
                title=_task_title(question.title),
                description=(
                    f"围绕“{question.title}”形成可执行方案，不限于开会；"
                    "可以通过群内确认、文档补充、评审或系统验证完成。"
                ),
                evidence_refs=question.evidence_refs,
                owner_candidates=owners,
                next_step="先形成候选方案，再由人工确认责任人与下发方式。",
                matter_type=matter_type,
                facet_types=["task"],
                due_date=question.due_date or "待人工确认（建议：下一次项目例会前）",
                deliverable=question.deliverable or f"《{question.title}》确认结论及核查附件",
                acceptance_criteria=(
                    question.acceptance_criteria
                    or "责任人已确认；结论可追溯到原始转写；交付物包含明确口径和可复核样例。"
                ),
                inference_note=(
                    question.inference_note
                    or "会议未完整给出闭环字段，Agent 已提出候选方案，需人工确认。"
                ),
            )
        )

    _dedupe_report_owners(chain_gaps + responsibility_gaps + followup_drafts)

    return InspectionReport(
        scenario_id=scenario_id,
        chain_name=chain_name,
        chain_gaps=chain_gaps,
        responsibility_gaps=responsibility_gaps,
        followup_drafts=followup_drafts,
    )


def _infer_matter_type(title: str, description: str) -> str:
    text = f"{title} {description}"
    if any(word in text for word in ["架构", "数据库", "接口", "数据层", "模型", "集成"]):
        return "architecture"
    if any(word in text for word in ["功能", "页面", "权限", "单据", "字段", "操作"]):
        return "function"
    return "design"


def _matter_label(value: str) -> str:
    return {"design": "设计事项", "function": "功能事项", "architecture": "架构事项"}.get(value, "项目事项")


def _task_title(question_title: str) -> str:
    cleaned = question_title.removeprefix("请确认").removeprefix("需要闭环：").strip()
    return f"闭环处理：{cleaned}"


def _is_project_event(item: CandidateItem) -> bool:
    return bool(item.title.strip() and item.evidence_refs)


def _evidence_by_doc(extractions: Iterable[ExtractionResult]) -> dict[str, list[EvidenceRef]]:
    grouped: dict[str, list[EvidenceRef]] = defaultdict(list)
    for extraction in extractions:
        for item in extraction.people + extraction.things + extraction.methods + extraction.chain_links + extraction.questions:
            grouped[extraction.source_doc_id].extend(item.evidence_refs)
    return grouped


def _pick_evidence(evidence_by_doc: dict[str, list[EvidenceRef]], doc_ids: list[str]) -> list[EvidenceRef]:
    refs: list[EvidenceRef] = []
    for doc_id in doc_ids:
        refs.extend(evidence_by_doc.get(doc_id, [])[:1])
    return refs


def _first_uploaded_evidence(extraction: ExtractionResult) -> list[EvidenceRef]:
    for item in extraction.people + extraction.things + extraction.methods + extraction.chain_links + extraction.questions:
        if item.evidence_refs:
            return item.evidence_refs[:1]
    return []


def _dedupe_report_owners(items: list[InspectionItem]) -> None:
    for item in items:
        item.owner_candidates = _dedupe(item.owner_candidates or [])


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result
