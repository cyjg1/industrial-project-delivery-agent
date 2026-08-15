from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from agent.milestones import milestone_to_objective
from agent.inspection_agent.state import InspectionContext
from agent.schemas import (
    AgentObservation,
    AgentPlanStep,
    AgentRun,
    AgentStep,
    CandidateStatus,
    EvidenceVerificationResult,
    InspectionItem,
    InspectionReport,
    ProjectAgentOutput,
    SourceDocument,
    to_plain,
)
from agent.verifier import verify_items_evidence, verify_report_evidence


def build_project_output(
    context: InspectionContext,
    *,
    summary: str,
) -> ProjectAgentOutput:
    people = _dedupe(
        item for extraction in context.extractions for item in extraction.people
    )
    things = _dedupe(
        item
        for extraction in context.extractions
        for item in extraction.things + extraction.chain_links
    )
    methods = _dedupe(
        item for extraction in context.extractions for item in extraction.methods
    )
    question_candidates = _dedupe(
        item for extraction in context.extractions for item in extraction.questions
    )
    tasks = [
        item
        for item in question_candidates
        if item.category in {"task", "tasks", "todo"}
    ]
    open_questions = [
        item
        for item in question_candidates
        if item.category not in {"task", "tasks", "todo"}
    ]
    return ProjectAgentOutput(
        report=context.report,
        verification=context.verification,
        summary=summary,
        people=people,
        things=things,
        methods=methods,
        tasks=tasks,
        open_questions=open_questions,
        stop_reason=context.stop_reason,
        no_change_reason=str(getattr(context, "no_change_reason", "") or ""),
    )


def validate_project_output(
    output: ProjectAgentOutput,
    manifest: list[SourceDocument],
    *,
    allowed_roots: tuple[str | Path, ...] = (),
    allow_no_change: bool = False,
) -> list[str]:
    errors: list[str] = []
    if output.report is None and allow_no_change:
        return []
    if output.report is None:
        return ["agent output is missing report"]
    structured_items = (
        output.people + output.things + output.methods + output.tasks + output.open_questions
    )
    known_source_ids = {source.doc_id for source in manifest}
    for item in [*_report_items(output.report), *structured_items]:
        status = item.status.value if isinstance(item.status, CandidateStatus) else str(item.status)
        if status != CandidateStatus.CANDIDATE.value:
            errors.append(f"{item.item_id} status must remain candidate")
        if not item.evidence_refs:
            errors.append(f"{item.item_id} has no evidence_refs")
        for ref in item.evidence_refs:
            if ref.source_doc_id not in known_source_ids:
                errors.append(f"{item.item_id} references unknown source {ref.source_doc_id}")
    errors.extend(
        verify_items_evidence(
            structured_items,
            manifest,
            allowed_roots=allowed_roots,
        ).errors
    )
    for item in output.methods:
        if not item.business_goal or not item.principles or not item.reasoning_chain or not item.applicable_scope:
            errors.append(
                f"{item.item_id} methodology is missing goal, principles, reasoning chain, or scope"
            )
    for item in output.tasks:
        if not item.owner_candidates or not item.due_date or not item.deliverable or not item.acceptance_criteria:
            errors.append(
                f"{item.item_id} task is missing owner, due_date, deliverable, or acceptance_criteria"
            )
    report_verification = verify_report_evidence(
        output.report,
        manifest,
        allowed_roots=allowed_roots,
    )
    if not report_verification.ok:
        errors.extend(report_verification.errors)
    if output.verification and not output.verification.ok:
        errors.extend(output.verification.errors)
    return errors


def persist_completed_run(
    context: InspectionContext,
    plan: list[AgentPlanStep],
    output: ProjectAgentOutput,
    *,
    runtime_kind: str,
    raw_response_count: int,
) -> AgentRun:
    verification = (
        verify_report_evidence(
            output.report,
            context.manifest,
            allowed_roots=(context.store.root_dir,),
        )
        if output.report is not None
        else EvidenceVerificationResult(
            ok=True,
            checked_count=0,
            errors=[],
        )
        if context.no_change_reason
        else EvidenceVerificationResult(
            ok=False,
            checked_count=0,
            errors=["agent output is missing report"],
        )
    )
    context.verification = verification
    output.verification = verification
    validation_errors = validate_project_output(
        output,
        context.manifest,
        allowed_roots=(context.store.root_dir,),
        allow_no_change=bool(context.no_change_reason),
    )
    status = "completed" if not validation_errors else "verification_failed"
    confirmed_item_ids = (
        context.store.merge_project_output(output)
        if status == "completed" and output.report is not None
        else []
    )
    run = _run_record(
        context,
        plan,
        status=status,
        runtime_kind=runtime_kind,
        raw_response_count=raw_response_count,
        final_report=output.report,
        verification=verification,
        error="; ".join(validation_errors),
        confirmed_item_ids=confirmed_item_ids,
        structured_output=to_plain(output),
    )
    context.store.save_run(run)
    return run


def persist_failed_run(
    context: InspectionContext,
    plan: list[AgentPlanStep],
    *,
    error: str,
    runtime_kind: str,
    raw_response_count: int = 0,
) -> AgentRun:
    run = _run_record(
        context,
        plan,
        status="failed",
        runtime_kind=runtime_kind,
        raw_response_count=raw_response_count,
        final_report=context.report,
        verification=context.verification,
        error=error,
        confirmed_item_ids=[],
        structured_output={},
    )
    context.store.save_run(run)
    return run


def persist_running_checkpoint(
    context: InspectionContext,
    plan: list[AgentPlanStep],
    *,
    runtime_kind: str,
) -> None:
    context.store.save_run(
        AgentRun(
            run_id=context.run_id,
            milestone_id=context.milestone.milestone_id,
            milestone_plan=context.milestone,
            objective=milestone_to_objective(context.milestone),
            plan=list(plan),
            steps=_steps_from_harness(plan, context.harness_state),
            observations=[],
            final_report=None,
            confirmed_item_ids=[],
            created_at=datetime.now(timezone.utc).isoformat(),
            status="running",
            runtime_kind=runtime_kind,
            harness_state=context.harness_state,
            input_snapshot=context.input_snapshot,
            stop_reason="running",
        )
    )


def _run_record(
    context: InspectionContext,
    plan: list[AgentPlanStep],
    *,
    status: str,
    runtime_kind: str,
    raw_response_count: int,
    final_report: InspectionReport | None,
    verification: EvidenceVerificationResult | None,
    error: str,
    confirmed_item_ids: list[str],
    structured_output: dict[str, Any],
) -> AgentRun:
    return AgentRun(
        run_id=context.run_id,
        milestone_id=context.milestone.milestone_id,
        milestone_plan=context.milestone,
        objective=milestone_to_objective(context.milestone),
        plan=list(plan),
        steps=_steps_from_harness(plan, context.harness_state),
        observations=_observations(context.trace),
        final_report=final_report,
        confirmed_item_ids=confirmed_item_ids,
        created_at=datetime.now(timezone.utc).isoformat(),
        status=status,
        runtime_kind=runtime_kind,
        verification=verification,
        error=error,
        agent_trace=context.trace,
        raw_response_count=raw_response_count,
        harness_state=context.harness_state,
        input_snapshot=context.input_snapshot,
        loop_rounds=context.loop_rounds,
        stop_reason=context.stop_reason,
        structured_output=structured_output,
        model_io_events=context.model_io_events,
        no_change_reason=context.no_change_reason,
    )


def _steps_from_harness(
    plan: list[AgentPlanStep],
    harness_state: dict[str, Any],
) -> list[AgentStep]:
    tasks = {item.get("step_id"): item for item in harness_state.get("tasks", [])}
    return [
        AgentStep(
            step_id=step.step_id,
            tool_name=step.tool_name,
            status=tasks.get(step.step_id, {}).get("status", "pending"),
            input_summary=tasks.get(step.step_id, {}).get("input_summary", ""),
            output_summary=tasks.get(step.step_id, {}).get("output_summary", ""),
        )
        for step in plan
    ]


def _observations(trace: list[dict[str, Any]]) -> list[AgentObservation]:
    return [
        AgentObservation(
            observation_id=f"obs_{index:02d}_{item.get('name', 'agent')}",
            tool_name=str(item.get("name", "agent")),
            summary=str(item.get("output_summary", item.get("summary", "completed"))),
            data=dict(item.get("data") or {}),
        )
        for index, item in enumerate(trace, start=1)
    ]


def _report_items(report: InspectionReport) -> list[InspectionItem]:
    return report.chain_gaps + report.responsibility_gaps + report.followup_drafts


def _dedupe(items: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for item in items:
        if item.item_id in seen:
            continue
        seen.add(item.item_id)
        if item.owner_candidates:
            item.owner_candidates = list(dict.fromkeys(item.owner_candidates))
        result.append(item)
    return result


