from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from agent.access_policy import AccessContext, User
from agent.milestones import milestone_to_objective
from agent.policy import render_agent_instructions
from agent.inspection_agent.completion import InspectionCompletionGate
from agent.inspection_agent.results import (
    build_project_output,
    persist_completed_run,
    persist_failed_run,
    persist_running_checkpoint,
)
from agent.inspection_agent.state import InspectionContext, InspectionTrigger
from agent.runtime.contracts import (
    RuntimeFeatures,
    RuntimePlanner,
    SelectedToolStep,
    ToolCallRequest,
)
from agent.runtime.factory import create_agent_runtime
from agent.runtime.harness import AgentHarness
from agent.runtime.middleware import BaseRuntimeMiddleware
from agent.schemas import AgentLoopRound, AgentPlanStep, AgentRun, MilestonePlan, to_plain
from agent.tool_registry import INSPECTION_SURFACE, ToolRegistry
from store.sqlite_store import ProjectSQLiteStore


class AgentRuntimeConfigurationError(RuntimeError):
    pass


class AgentOutputValidationError(RuntimeError):
    pass


class InspectionRunMiddleware(BaseRuntimeMiddleware):
    def __init__(
        self,
        *,
        context: InspectionContext,
        harness: AgentHarness,
        runtime_kind: str,
        features: RuntimeFeatures,
        tool_names: list[str],
        model_calls_are_real: bool,
    ) -> None:
        self.context = context
        self.harness = harness
        self.runtime_kind = runtime_kind
        self.features = features
        self.tool_names = tool_names
        self.model_calls_are_real = model_calls_are_real

    def before_run(self, state: Any) -> None:
        self.harness.start()
        self._sync(state)
        persist_running_checkpoint(
            self.context,
            self.harness.plan,
            runtime_kind=self.runtime_kind,
        )

    def after_model(self, state: Any, turn: Any) -> None:
        if self.model_calls_are_real:
            self.context.model_io_events.append(
                {
                    "event_id": f"planning_turn_{len(self.context.model_io_events) + 1:02d}",
                    "provider": turn.provider,
                    "model": turn.model,
                    "api_surface": "chat_completions_function_calling",
                    "api_called": True,
                    "request": {
                        "messages": _debug_messages(state.messages),
                        "tools": list(self.tool_names),
                        "message_char_count": _message_char_count(state.messages),
                    },
                    "responses": [
                        {
                            "raw_response_id": "",
                            "output_text": turn.text,
                            "token_usage": {},
                            "tool_calls": [
                                {
                                    "name": call.name,
                                    "arguments": call.arguments,
                                    "reason": call.reason,
                                }
                                for call in turn.tool_calls
                            ],
                        }
                    ],
                }
            )
        self._sync(state)

    def before_tool(self, state: Any, call: ToolCallRequest) -> None:
        selected: SelectedToolStep = state.plan[-1]
        step = AgentPlanStep(
            step_id=selected.step_id,
            tool_name=selected.tool_name,
            reason=selected.reason,
        )
        self.harness.add_step(step)
        self.harness.start_step(step, input_summary=_state_summary(self.context))
        self._sync(state)

    def after_tool(
        self,
        state: Any,
        call: ToolCallRequest,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        step = self.harness.plan[-1]
        if result.get("ok", True):
            self.harness.complete_step(step, result)
            self.context.tool_failures.pop(call.name, None)
        else:
            message = str(result.get("summary") or "tool failed")
            self.context.tool_failures[call.name] = message
            self.harness.fail_step(step, message)
        self._sync(state)
        persist_running_checkpoint(
            self.context,
            self.harness.plan,
            runtime_kind=self.runtime_kind,
        )
        return result

    def after_run(self, state: Any) -> None:
        self.harness.complete(success=bool(state.completed))
        self._sync(state)

    def _sync(self, state: Any) -> None:
        self.context.trace = list(self.harness.trace)
        self.context.harness_state = self.harness.snapshot()
        self.context.input_snapshot = {
            "project_id": self.context.project_id,
            "trigger": {
                "type": self.context.trigger.trigger_type,
                "entity_type": self.context.trigger.entity_type,
                "entity_id": self.context.trigger.entity_id,
                "event_id": self.context.trigger.event_id,
                "source_ids": list(self.context.trigger.source_ids),
                "evidence": self.context.trigger.evidence,
            },
            "milestone": to_plain(self.context.milestone),
            "source_documents": [
                {
                    "doc_id": source.doc_id,
                    "title": source.title,
                    "meeting_date": source.meeting_date,
                    "curated_status": source.curated_source.status,
                    "raw_status": source.raw_source.status,
                    "tags": list(source.tags),
                    "org_id": source.org_id,
                    "project_id": source.project_id,
                    "topic_id": source.topic_id,
                    "author_id": source.author_id,
                    "sensitivity": source.sensitivity,
                }
                for source in self.context.manifest
            ],
            "people_asset": {
                "available": self.context.people_structure is not None,
                "people_count": (
                    len(self.context.people_structure.people)
                    if self.context.people_structure
                    else 0
                ),
                "scenario_count": (
                    len(self.context.people_structure.scenarios)
                    if self.context.people_structure
                    else 0
                ),
            },
            "project_memory": {
                "item_count": len(self.context.memory_items or []),
            },
            "note_context": {
                "chunk_count": len(self.context.note_materials or []),
                "loaded_source_count": len(self.context.source_texts),
                "active_chars": sum(
                    len(str(item.get("content") or ""))
                    for item in (self.context.note_materials or [])
                ),
            },
            "loop_budget": {
                "max_rounds": self.features.max_rounds,
                "no_progress_limit": self.features.duplicate_call_limit,
            },
            "state": {
                "source_count": len(self.context.manifest),
                "people_ready": self.context.people_structure is not None,
                "memory_count": (
                    None
                    if self.context.memory_items is None
                    else len(self.context.memory_items)
                ),
                "note_chunk_count": (
                    None
                    if self.context.note_materials is None
                    else len(self.context.note_materials)
                ),
                "extraction_count": len(self.context.extractions),
                "report_ready": self.context.report is not None,
                "no_change_reason": self.context.no_change_reason,
                "verification": (
                    None
                    if self.context.verification is None
                    else self.context.verification.ok
                ),
            },
            "runtime": {
                "kind": self.runtime_kind,
                "planner": getattr(state, "model_turns", [])[-1].provider
                if getattr(state, "model_turns", [])
                else "pending",
                "available_tools": list(self.tool_names),
                "max_rounds": self.features.max_rounds,
                "duplicate_call_limit": self.features.duplicate_call_limit,
                "max_tool_result_chars": self.features.max_tool_result_chars,
                "message_char_count": _message_char_count(
                    getattr(state, "messages", [])
                ),
            },
        }


class InspectionAgentService:
    def __init__(
        self,
        *,
        store: ProjectSQLiteStore,
        registry: ToolRegistry,
        planner: RuntimePlanner,
        runtime_kind: str,
        features: RuntimeFeatures,
        model_calls_are_real: bool,
    ) -> None:
        self.store = store
        self.registry = registry
        self.planner = planner
        self.runtime_kind = runtime_kind
        self.features = features
        self.model_calls_are_real = model_calls_are_real

    def run(
        self,
        milestone: MilestonePlan,
        *,
        actor: User,
        access_context: AccessContext,
        project_id: str,
        trigger: InspectionTrigger | None = None,
    ) -> AgentRun:
        if project_id not in access_context.projects_of(actor):
            raise AgentRuntimeConfigurationError(
                "Actor is not a member of the requested project"
            )
        context = InspectionContext(
            run_id=_new_run_id(),
            milestone=milestone,
            store=self.store,
            project_id=project_id,
            trigger=trigger or InspectionTrigger(trigger_type="manual"),
        )
        schemas = self.registry.schemas(
            actor,
            surface=INSPECTION_SURFACE,
            project_id=project_id,
            ctx=access_context,
        )
        if not schemas:
            raise AgentRuntimeConfigurationError(
                "No inspection tools are available for the resolved actor"
            )
        tool_names = [str(item["function"]["name"]) for item in schemas]
        harness = AgentHarness(
            run_id=context.run_id,
            plan=[],
            max_active_chars=max(6000, self.features.max_tool_result_chars * 3),
        )
        middleware = InspectionRunMiddleware(
            context=context,
            harness=harness,
            runtime_kind=self.runtime_kind,
            features=self.features,
            tool_names=tool_names,
            model_calls_are_real=self.model_calls_are_real,
        )

        def invoke(call: ToolCallRequest) -> dict[str, Any]:
            arguments = {
                key: value
                for key, value in call.arguments.items()
                if key not in {"context", "project_id", "reason"}
            }
            arguments.update({"context": context, "project_id": project_id})
            result = self.registry.call(
                call.name,
                actor,
                access_context,
                arguments,
                project_id=project_id,
            )
            if not result.get("ok", True):
                error = result.get("error") or {}
                message = str(
                    error.get("message")
                    or result.get("summary")
                    or "tool invocation rejected"
                )
                raise PermissionError(message)
            return result

        runtime = create_agent_runtime(
            planner=self.planner,
            tools=schemas,
            invoke_tool=invoke,
            completion_gate=InspectionCompletionGate(),
            features=self.features,
            middleware=[middleware],
        )
        try:
            result = runtime.run(
                run_id=context.run_id,
                system_prompt=render_agent_instructions(),
                user_prompt=_inspection_prompt(milestone, context.trigger),
                domain_state=context,
            )
        except Exception as exc:
            context.stop_reason = "runtime_failed"
            return persist_failed_run(
                context,
                harness.plan,
                error=f"{type(exc).__name__}: {exc}",
                runtime_kind=self.runtime_kind,
                raw_response_count=len(context.model_io_events),
            )

        context.stop_reason = result.stop_reason
        context.loop_rounds = _loop_rounds(result, context)
        context.trace = list(harness.trace)
        context.harness_state = harness.snapshot()
        raw_response_count = (
            result.model_turn_count if self.model_calls_are_real else 0
        )
        if not result.completed:
            if (
                context.verification is not None
                and not context.verification.ok
                and not context.tool_failures
            ):
                output = build_project_output(
                    context,
                    summary=_runtime_failure_message(
                        result.stop_reason,
                        result.completion,
                        result.tool_results,
                    ),
                )
                return persist_completed_run(
                    context,
                    harness.plan,
                    output,
                    runtime_kind=self.runtime_kind,
                    raw_response_count=raw_response_count,
                )
            return persist_failed_run(
                context,
                harness.plan,
                error=_runtime_failure_message(
                    result.stop_reason,
                    result.completion,
                    result.tool_results,
                ),
                runtime_kind=self.runtime_kind,
                raw_response_count=raw_response_count,
            )
        output = build_project_output(
            context,
            summary=result.final_text or "Project inspection completed and verified.",
        )
        return persist_completed_run(
            context,
            harness.plan,
            output,
            runtime_kind=self.runtime_kind,
            raw_response_count=raw_response_count,
        )


def _inspection_prompt(
    milestone: MilestonePlan,
    trigger: InspectionTrigger,
) -> str:
    return (
        "Inspect the project change described by the trigger. Select only the next useful tool from "
        "the supplied catalog, inspect each tool result, and stop only after the runtime "
        "verification contract passes. The order is not predetermined. Do not fabricate a "
        "candidate when the evidence shows no material change; a verified no-change result is valid.\n\n"
        f"Trigger JSON: {json.dumps(to_plain(trigger), ensure_ascii=False, sort_keys=True)}\n"
        f"Objective: {milestone_to_objective(milestone)}\n"
        f"Milestone JSON: {json.dumps(to_plain(milestone), ensure_ascii=False, sort_keys=True)}"
    )


def _loop_rounds(result: Any, context: InspectionContext) -> list[AgentLoopRound]:
    tool_results = {
        str(item.get("call_id") or ""): item for item in result.tool_results
    }
    rounds: list[AgentLoopRound] = []
    for index, turn in enumerate(result.model_turns, start=1):
        calls = [
            {
                "name": call.name,
                "arguments": call.arguments,
                "reason": call.reason,
            }
            for call in turn.tool_calls
        ]
        summaries = [
            str(tool_results.get(call.call_id, {}).get("result", {}).get("summary", ""))
            for call in turn.tool_calls
        ]
        rounds.append(
            AgentLoopRound(
                round_index=index,
                phase="model_turn",
                objective="Select the next useful action and inspect the resulting state.",
                model_input_summary=_state_summary(context),
                model_output_summary=turn.text
                or "; ".join(call.reason or call.name for call in turn.tool_calls),
                tool_name="、".join(call.name for call in turn.tool_calls),
                tool_result_summary="; ".join(value for value in summaries if value),
                decision="call_tool" if turn.tool_calls else "finish",
                state_changes=[value for value in summaries if value],
                errors=[],
                stop_reason=(
                    result.stop_reason if index == len(result.model_turns) else ""
                ),
                round_kind="model",
                raw_response_id="",
                token_usage={},
                tool_calls=calls,
            )
        )
    return rounds


def _runtime_failure_message(
    stop_reason: str,
    completion: Any,
    tool_results: list[dict[str, Any]],
) -> str:
    missing = ", ".join(completion.missing) or "none"
    errors = "; ".join(completion.errors) or "none"
    tool_errors = "; ".join(
        f"{item.get('name')}: {item.get('result', {}).get('summary', 'tool failed')}"
        for item in tool_results
        if not item.get("result", {}).get("ok", True)
    ) or "none"
    return (
        f"{stop_reason}; missing={missing}; verification_errors={errors}; "
        f"tool_errors={tool_errors}"
    )


def _state_summary(context: InspectionContext) -> str:
    return (
        f"sources={len(context.manifest)}; "
        f"people={'ready' if context.people_structure else 'missing'}; "
        f"memory={None if context.memory_items is None else len(context.memory_items)}; "
        f"notes={None if context.note_materials is None else len(context.note_materials)}; "
        f"extractions={len(context.extractions)}; "
        f"report={'ready' if context.report else 'missing'}; "
        f"verification={None if context.verification is None else context.verification.ok}"
    )


def _message_char_count(messages: list[dict[str, Any]]) -> int:
    return sum(len(str(message.get("content") or "")) for message in messages)


def _debug_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        value = dict(message)
        content = str(value.get("content") or "")
        if len(content) > 2400:
            value["content"] = content[:2400]
            value["content_compacted"] = True
            value["original_char_count"] = len(content)
        result.append(value)
    return result


def _new_run_id() -> str:
    return (
        f"run_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_"
        f"{uuid4().hex[:8]}"
    )
