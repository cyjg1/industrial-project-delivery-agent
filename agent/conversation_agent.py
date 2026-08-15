from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from agent.access_policy import AccessContext, User, visible, visible_filter
from agent.conversation.service import ConversationLoopService
from agent.conversation.context import build_initial_chat_messages
from agent.daily_brief import generate_daily_brief
from agent.deliverables import DeliverableService, DeliverableUpload, DeliverableValidationError
from agent.evolution import build_rejection_prompt_context, search_confirmed_methods
from agent.inspection_jobs import InspectionDispatcher
from agent.presentation import build_human_review_workspace
from agent.project_skills import ProjectSkillService
from agent.project_health import build_project_health
from agent.meeting_ingestion import enqueue_source_ingestion, source_ingestion_view
from agent.memory_skill_evolution import MemorySkillEvolutionService
from agent.project_people import load_project_people
from agent.repository_paths import resolve_repository_path
from agent.role_views import role_view_key
from agent.runtime import AgentRuntime
from agent.runtime.contracts import CompletionDecision, RuntimeFeatures
from agent.runtime.event_sink import RuntimeEventSink
from agent.schemas import (
    AgentLoopRound,
    AgentObservation,
    AgentRun,
    CandidateStatus,
    EvidenceRef,
    InspectionItem,
    MilestonePlan,
    WorkItem,
    WorkItemStatus,
    to_plain,
)
from agent.skill_packages import search_skill_packages
from agent.tool_registry import ToolRegistry, conversation_tool_specs, register_specs
from ingestion.source_manifest import build_project_manifest, register_uploaded_project_file
from ingestion.structured_workbook import resolve_project_input_kind
from store.sqlite_store import ProjectSQLiteStore
from skills.meeting_minutes.transcript import parse_transcript_bytes


@dataclass(frozen=True)
class ConversationUpload:
    filename: str
    content: bytes
    content_type: str = ""
    input_kind: str = "auto"


class _ConversationAnswerGate:
    def __init__(self, user_text: str) -> None:
        self.user_text = user_text

    def evaluate(
        self,
        domain_state: Any,
        runtime_state: Any | None = None,
    ) -> CompletionDecision:
        if runtime_state is None or not str(runtime_state.final_text or "").strip():
            return CompletionDecision(
                complete=False,
                reason="final answer is empty",
                missing=["non_empty_final_answer"],
            )
        tool_steps = [
            {"result": dict(record.get("result") or {})}
            for record in runtime_state.tool_results
        ]
        verification = _verify_reply_against_tool_results(
            runtime_state.final_text,
            tool_steps,
            user_text=self.user_text,
        )
        if isinstance(domain_state, dict):
            domain_state["answer_verification"] = verification
        if verification["unsupported"]:
            return CompletionDecision(
                complete=False,
                reason="answer contains unsupported concrete claims",
                errors=list(verification["unsupported"]),
            )
        return CompletionDecision(
            complete=True,
            reason="answer facts verified against visible tool results",
        )


def run_conversation_agent(
    *,
    store: ProjectSQLiteStore,
    runtime: AgentRuntime,
    model_id: str = "",
    view: str,
    message: str,
    session_id: str | None = None,
    upload: ConversationUpload | None = None,
    uploads: list[ConversationUpload] | None = None,
    milestone: MilestonePlan,
    actor: User,
    access_context: AccessContext,
    ingestion_wake: Any | None = None,
    inspection_wake: Any | None = None,
    max_rounds: int = 8,
    runtime_run_id: str | None = None,
    event_sink: RuntimeEventSink | None = None,
    turn_id: str | None = None,
    persist_user_message: bool = True,
    runtime_checkpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    final_payload: dict[str, Any] | None = None
    for event in iter_conversation_agent_events(
        store=store,
        runtime=runtime,
        model_id=model_id,
        view=view,
        message=message,
        session_id=session_id,
        upload=upload,
        uploads=uploads,
        milestone=milestone,
        actor=actor,
        access_context=access_context,
        ingestion_wake=ingestion_wake,
        inspection_wake=inspection_wake,
        max_rounds=max_rounds,
        runtime_run_id=runtime_run_id,
        event_sink=event_sink,
        turn_id=turn_id,
        persist_user_message=persist_user_message,
        runtime_checkpoint=runtime_checkpoint,
    ):
        if event["event"] == "final":
            final_payload = event["data"]
    if final_payload is None:
        raise RuntimeError("conversation agent finished without a final payload")
    return final_payload


def iter_conversation_agent_events(
    *,
    store: ProjectSQLiteStore,
    runtime: AgentRuntime,
    model_id: str = "",
    view: str,
    message: str,
    session_id: str | None = None,
    upload: ConversationUpload | None = None,
    uploads: list[ConversationUpload] | None = None,
    milestone: MilestonePlan,
    actor: User,
    access_context: AccessContext,
    ingestion_wake: Any | None = None,
    inspection_wake: Any | None = None,
    max_rounds: int = 8,
    runtime_run_id: str | None = None,
    event_sink: RuntimeEventSink | None = None,
    turn_id: str | None = None,
    persist_user_message: bool = True,
    runtime_checkpoint: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    active_milestone = milestone
    active_uploads = _normalize_uploads(upload, uploads)
    started_at = _now()
    active_run_id = runtime_run_id or (
        f"conversation_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    )
    active_turn_id = turn_id or f"turn_{uuid4().hex[:16]}"
    title = message or (active_uploads[0].filename if active_uploads else "") or "未命名会话"
    project_id = store.default_project_id_for_actor(actor.id, actor.org_id)
    session = store.ensure_session(
        session_id=session_id,
        title=title,
        project_id=project_id,
        actor_id=actor.id,
    )
    session_id = session["session_id"]
    user_content = _user_content(message, active_uploads)
    if persist_user_message:
        store.append_session_message(
            session_id,
            "user",
            user_content,
            metadata={"view": view, "model": model_id},
            turn_id=active_turn_id,
            run_id=active_run_id,
            state="running",
        )
    history = store.list_model_session_messages(
        session_id,
        current_turn_id=active_turn_id,
    )

    runner = ConversationToolRunner(
        store=store,
        runtime=runtime,
        milestone=active_milestone,
        uploads=active_uploads,
        actor=actor,
        access_context=access_context,
        ingestion_wake=ingestion_wake,
        inspection_wake=inspection_wake,
        runtime_run_id=active_run_id,
        session_id=session_id,
        turn_id=active_turn_id,
    )
    registry = _conversation_registry(runtime, runner, access_context)
    provider = runtime.provider("conversation", model=model_id)
    active_model = str(getattr(provider, "model", "") or provider.name)
    evolution_context = build_rejection_prompt_context(
        store,
        project_id=project_id,
        actor=actor,
        access_context=access_context,
    )
    role_daily_workspace = runner.workspace_payload().get(
        "daily_report_workspace",
        {},
    )
    context_payload = store.read_context(
        "conversation",
        message or (active_uploads[0].filename if active_uploads else ""),
        token_budget=int(os.getenv("CONVERSATION_CONTEXT_TOKEN_BUDGET", "900")),
        actor=actor,
        access_context=access_context,
        project_id=project_id,
        include_retrieval=False,
        additional_context={
            "role_daily_workspace": role_daily_workspace,
        },
    )
    messages = build_initial_chat_messages(
        view=view,
        history=history,
        has_upload=bool(active_uploads),
        evolution_context=evolution_context,
        context_payload=context_payload,
        actor=actor,
        access_context=access_context,
        project_id=project_id,
    )
    tool_steps: list[dict[str, Any]] = []
    model_inputs: list[str] = []
    model_outputs: list[str] = []
    stop_reason = "final_answer_ready"
    reply = ""
    runtime_result = None
    steps_by_call_id: dict[str, dict[str, Any]] = {}
    loop = ConversationLoopService(
        registry=registry,
        provider=provider,
        actor=actor,
        access_context=access_context,
        project_id=project_id,
        features=RuntimeFeatures(
            max_rounds=max_rounds,
            max_tool_calls=_conversation_tool_call_budget(),
            max_elapsed_seconds=_conversation_elapsed_budget(),
            final_answer_on_stop=True,
        ),
        event_sink=event_sink,
        model_tool_result=lambda result, budget: json.loads(
            _tool_message_content(result, max_chars=budget)
        ),
        completion_gate=_ConversationAnswerGate(user_content),
    )
    model_inputs.append(_messages_debug(messages))
    for runtime_event in loop.stream(
        run_id=active_run_id,
        messages=messages,
        resume_checkpoint=runtime_checkpoint,
    ):
        if runtime_event.kind == "model_delta":
            yield {"event": "delta", "data": {"text": runtime_event.payload.get("text", "")}}
        elif runtime_event.kind == "model_output_reset":
            yield {"event": "reset", "data": {"reason": runtime_event.payload.get("reason", "")}}
        elif runtime_event.kind == "tool_start":
            step = {
                "round_index": runtime_event.round_index,
                "call_id": str(runtime_event.payload.get("call_id") or ""),
                "tool_name": str(runtime_event.payload.get("name") or ""),
                "arguments": dict(runtime_event.payload.get("arguments") or {}),
                "reason": str(runtime_event.payload.get("reason") or ""),
                "status": "running",
                "summary": "",
                "result": {},
                "elapsed_ms": 0,
            }
            tool_steps.append(step)
            steps_by_call_id[step["call_id"]] = step
            yield {"event": "tool_start", "data": dict(step)}
        elif runtime_event.kind == "tool_result":
            call_id = str(runtime_event.payload.get("call_id") or "")
            step = steps_by_call_id.get(call_id)
            if step is None:
                step = {
                    "round_index": runtime_event.round_index,
                    "call_id": call_id,
                    "tool_name": str(runtime_event.payload.get("name") or ""),
                    "arguments": {},
                    "reason": "",
                    "status": "running",
                    "summary": "",
                    "result": {},
                    "elapsed_ms": 0,
                }
                tool_steps.append(step)
                steps_by_call_id[call_id] = step
            step["status"] = "skipped" if runtime_event.payload.get("skipped") else "completed"
            step["summary"] = str(runtime_event.payload.get("summary") or "")
            step["result"] = _compact_tool_result(dict(runtime_event.payload.get("result") or {}))
            step["elapsed_ms"] = int(runtime_event.payload.get("elapsed_ms") or 0)
            yield {"event": "tool_result", "data": dict(step)}
            yield {"event": "tool", "data": dict(step)}
        elif runtime_event.kind == "runtime_completed":
            runtime_result = runtime_event.result

    if runtime_result is None:
        raise RuntimeError("conversation runtime finished without a terminal result")
    if not runtime_result.completed or not runtime_result.final_text.strip():
        raise RuntimeError(
            "conversation runtime stopped without a verified final answer: "
            f"{runtime_result.stop_reason or 'unknown'}"
        )
    for record in runtime_result.tool_results:
        call_id = str(record.get("call_id") or "")
        if call_id in steps_by_call_id:
            continue
        result = dict(record.get("result") or {})
        restored_step = {
            "round_index": int(record.get("round_index") or 0),
            "call_id": call_id,
            "tool_name": str(record.get("name") or ""),
            "arguments": dict(record.get("arguments") or {}),
            "reason": str(record.get("reason") or ""),
            "status": "skipped" if result.get("skipped") else "completed",
            "summary": str(result.get("summary") or ""),
            "result": _compact_tool_result(result),
            "elapsed_ms": 0,
        }
        tool_steps.append(restored_step)
        steps_by_call_id[call_id] = restored_step
    reply = runtime_result.final_text.strip()
    stop_reason = runtime_result.stop_reason
    model_outputs = [
        turn.text or json.dumps(
            {"tool_calls": [_tool_call_debug(call) for call in turn.tool_calls]},
            ensure_ascii=False,
        )
        for turn in runtime_result.model_turns
    ]
    model_rounds = _conversation_round_trace(
        runtime_result.events,
        tool_steps,
    )

    verification = _verify_reply_against_tool_results(reply, tool_steps, user_text=user_content)
    if verification["unsupported"]:
        note = "\n\n待核实：" + "；".join(verification["unsupported"])
        reply = f"{reply}{note}"
        yield {"event": "delta", "data": {"text": note}}

    workspace_payload = runner.workspace_payload()
    assistant_message = store.append_session_message(
        session_id,
        "assistant",
        reply,
        metadata={
            "tool_steps": tool_steps,
            "stop_reason": stop_reason,
            "verification": verification,
            "provider": provider.name,
            "model": active_model,
            "model_rounds": model_rounds,
        },
        turn_id=active_turn_id,
        run_id=active_run_id,
        state="running",
    )
    run = _conversation_run(
        run_id=active_run_id,
        milestone=active_milestone,
        objective=message or "处理对话输入",
        created_at=started_at,
        provider_name=provider.name,
        model_name=active_model,
        model_inputs=model_inputs,
        model_outputs=model_outputs,
        model_rounds=model_rounds,
        tool_steps=tool_steps,
        stop_reason=stop_reason,
        tool_names=registry.tool_names(),
        project_id=project_id,
    )
    store.save_run(run)
    store.update_session_turn_state(active_turn_id, "committed")

    yield {"event": "final", "data": {
        "view": view,
        "next_view": "review" if any(step["tool_name"] in {"ingest_file", "propose_candidates"} for step in tool_steps) else view,
        "session_id": session_id,
        "message_id": assistant_message["message_id"],
        "reply": reply,
        "tool_steps": tool_steps,
        "verification": verification,
        "stop_reason": stop_reason,
        "context_bundle": _context_bundle(message, view, history, tool_steps),
        "workspace": runner.workspace_payload(),
        "debug": {
            "provider": provider.name,
            "model": active_model,
            "model_input": "\n\n--- round ---\n\n".join(model_inputs),
            "model_output": "\n".join(model_outputs + [reply]),
            "rounds": model_rounds,
            "context_char_count": context_payload["char_count"],
            "context_token_count": context_payload["token_count"],
            "context_budget": context_payload["budget"],
            "context_budget_unit": context_payload["budget_unit"],
            "context_token_counter": context_payload["token_counter"],
            "context_retrieval_count": len(context_payload["retrieval_items"]),
            "context_degraded": context_payload["degraded"],
            "actor_id": actor.id,
        },
    }}


def _conversation_registry(
    runtime: AgentRuntime,
    runner: "ConversationToolRunner",
    access_context: AccessContext,
) -> ToolRegistry:
    registry = runtime.tool_registry
    if registry is None:
        raise RuntimeError("Conversation agent requires the shared ToolRegistry")
    registry.default_context = access_context
    return register_specs(registry, conversation_tool_specs(runner))


def _conversation_tool_call_budget() -> int:
    raw = str(os.getenv("CONVERSATION_MAX_TOOL_CALLS", "12")).strip()
    try:
        value = int(raw)
    except ValueError:
        value = 12
    return max(3, min(value, 24))


def _conversation_elapsed_budget() -> float:
    raw = str(os.getenv("CONVERSATION_MAX_ELAPSED_SECONDS", "180")).strip()
    try:
        value = float(raw)
    except ValueError:
        value = 180.0
    return max(30.0, min(value, 300.0))


class ConversationToolRunner:
    def __init__(
        self,
        *,
        store: ProjectSQLiteStore,
        runtime: AgentRuntime,
        milestone: MilestonePlan,
        uploads: list[ConversationUpload],
        actor: User,
        access_context: AccessContext,
        ingestion_wake: Any | None = None,
        inspection_wake: Any | None = None,
        runtime_run_id: str = "",
        session_id: str = "",
        turn_id: str = "",
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.milestone = milestone
        self.uploads = list(uploads)
        self.upload = self.uploads[0] if self.uploads else None
        self.actor = actor
        self.access_context = access_context
        self.ingestion_wake = ingestion_wake
        self.inspection_wake = inspection_wake
        self.runtime_run_id = runtime_run_id
        self.session_id = session_id
        self.turn_id = turn_id
        self.project_id = store.default_project_id_for_actor(actor.id, actor.org_id)
        self.latest_project_run = store.latest_run(project_id=self.project_id)
        self._visible_deliverable_ids: set[str] = set()

    def workspace_payload(self) -> dict[str, Any]:
        return build_human_review_workspace(
            run=self.latest_project_run,
            store=self.store,
            manifest=build_project_manifest(
                self.store,
                self.project_id,
                date_start=self.milestone.date_start,
                date_end=self.milestone.date_end,
            ),
            milestone=self.milestone,
            project_id=self.project_id,
            actor=self.actor,
            access_context=self.access_context,
        )

    def search_memory(self, query: str = "", filters: dict[str, Any] | None = None) -> dict[str, Any]:
        scoped_filters = dict(filters or {})
        scoped_filters["project_id"] = self.project_id
        diagnostics: dict[str, Any] = {}
        rows = self.store.search_memory(
            query,
            scoped_filters,
            limit=10,
            actor=self.actor,
            access_context=self.access_context,
            diagnostics=diagnostics,
        )
        summary = f"检索到 {len(rows)} 条记忆候选。"
        if diagnostics.get("degraded"):
            reasons = set(diagnostics.get("degradation_reasons") or [])
            if "embedding_api_failure" in reasons:
                summary += " Embedding API 调用失败，已明确降级为 FTS 结果。"
            elif "embedding_index_incomplete" in reasons:
                summary += " 语义索引尚未完整建立，当前结果可能仅覆盖已索引条目和 FTS 命中。"
        return {
            "summary": summary,
            "items": rows[:10],
            **diagnostics,
        }

    def search_source_evidence(self, query: str = "") -> dict[str, Any]:
        rows = self.store.search_source_evidence(
            query,
            actor=self.actor,
            access_context=self.access_context,
            project_id=self.project_id,
            limit=10,
        )
        return {
            "summary": f"从归档原文中检索到 {len(rows)} 条证据片段。",
            "items": rows,
        }

    def search_methods(self, query: str = "") -> dict[str, Any]:
        return search_confirmed_methods(
            self.store,
            query,
            limit=5,
            project_id=self.project_id,
            actor=self.actor,
            access_context=self.access_context,
        )

    def search_project_skills(self, query: str = "") -> dict[str, Any]:
        registry = self.runtime.tool_registry
        if registry is None:
            raise RuntimeError("Project Skill search requires the shared ToolRegistry")
        allowed_tool_names = {
            tool["function"]["name"]
            for tool in registry.openai_tools(self.actor, project_id=self.project_id)
        }
        rows = ProjectSkillService(self.store, registry).search_published(
            query,
            self.actor,
            self.access_context,
            project_id=self.project_id,
            allowed_tool_names=allowed_tool_names,
            limit=5,
        )
        for row in rows:
            row["origin"] = "project"
        evolution = MemorySkillEvolutionService(self.store)
        supplement = evolution.retrieve_supplement(
            query,
            actor=self.actor,
            ctx=self.access_context,
            project_id=self.project_id,
            project_skill_count=len(rows),
            limit=5,
        )
        evolution.record_project_skill_retrievals(
            run_id=self.runtime_run_id,
            query=query,
            rows=rows,
            actor_id=self.actor.id,
        )
        external_rows: list[dict[str, Any]] = []
        external_error = ""
        try:
            external_rows = [
                {
                    **row,
                    "org_id": self.actor.org_id,
                    "project_id": self.project_id,
                    "topic_id": None,
                    "author_id": "",
                    "sensitivity": "l1",
                }
                for row in search_skill_packages(
                    query,
                    allowed_tool_names=allowed_tool_names,
                    limit=3,
                )
            ]
        except Exception as exc:
            external_error = f"{type(exc).__name__}: {exc}"
        return {
            "summary": (
                f"检索到 {len(rows)} 个已测试并发布的项目 Skill，"
                f"和 {len(external_rows)} 个外部导入 Skill 包。"
            ),
            "items": rows,
            "external_items": external_rows,
            "external_error": external_error,
            **supplement,
        }

    def get_tasks(
        self,
        status: str | None = None,
        owner: str | None = None,
        overdue: bool | None = None,
    ) -> dict[str, Any]:
        rows = []
        today = date.today()
        project_tasks = [
            item
            for item in self.store.list_work_items()
            if item.project_id == self.project_id and item.status != WorkItemStatus.ARCHIVED
        ]
        visible_tasks = visible_filter(self.actor, project_tasks, self.access_context)
        role_scoped_tasks = [
            item
            for item in visible_tasks
            if _task_in_actor_role_scope(self.actor, self.access_context, self.project_id, item)
        ]
        for item in role_scoped_tasks:
            due = _parse_date(item.due_date or "")
            is_overdue = bool(due and due < today and item.status.value not in {"done", "canceled"})
            owners = item.owner_candidates or []
            if status and item.status.value != status:
                continue
            if owner and owner not in owners and owner not in "、".join(owners):
                continue
            if overdue is not None and is_overdue != overdue:
                continue
            rows.append(_task_row(item, is_overdue))
        rows.sort(key=lambda row: (not row["overdue"], row.get("due_date") or "9999-12-31"))
        return {"summary": f"查到 {len(rows)} 条任务。", "items": rows}

    def get_milestone_status(self) -> dict[str, Any]:
        workspace = self.workspace_payload()
        control = workspace["milestone_control"]
        return {
            "summary": f"里程碑 {control['plan']['name']}，时间进度 {control['time_progress']['percent']}%。",
            "milestone": control,
        }

    def get_project_health(self) -> dict[str, Any]:
        task_rows = visible_filter(
            self.actor,
            self.get_tasks().get("items", []),
            self.access_context,
        )
        health = build_project_health(
            milestone=self.milestone,
            tasks=task_rows,
            latest_run=self.store.latest_verified_run(self.project_id),
            as_of=date.today(),
        )
        return {
            "summary": (
                f"项目任务 {health['task_counts']['total']} 项，"
                f"超期 {health['overdue']['count']} 项，"
                f"里程碑距结束 {health['milestone']['days_to_end']} 天。"
            ),
            "health": health,
        }

    def get_person(self, name: str = "") -> dict[str, Any]:
        target_name = name.strip()
        people_asset = load_project_people(
            self.store,
            self.project_id,
            actor=self.actor,
            access_context=self.access_context,
        )
        directory_people = [
            person
            for person in people_asset.people
            if not target_name or target_name in person.name or target_name == person.person_id
        ]
        all_task_rows = visible_filter(
            self.actor,
            self.get_tasks().get("items", []),
            self.access_context,
        )
        all_related_items = visible_filter(
            self.actor,
            [
                to_plain(item)
                for item in self.store.list_items()
                if item.project_id == self.project_id
                and item.status != CandidateStatus.ARCHIVED
            ],
            self.access_context,
        )
        if target_name:
            people = directory_people
        else:
            visible_work_names = {
                owner
                for row in [*all_task_rows, *all_related_items]
                for owner in (row.get("owners") or row.get("owner_candidates") or [])
            }
            people = [person for person in directory_people if person.name in visible_work_names]
        matched_names = {person.name for person in people}
        task_rows = [row for row in all_task_rows if matched_names.intersection(row.get("owners", []))]
        related_items = [
            row
            for row in all_related_items
            if matched_names.intersection(row.get("owner_candidates") or [])
        ]
        methods = [row for row in related_items if _is_method_item(row)]
        issues = [row for row in related_items if not _is_task_item(row) and not _is_method_item(row)]
        rows = [
            {
                "person_id": person.person_id,
                "name": person.name,
                "identity_status": person.identity_status,
                "group": person.group,
                "role": person.role,
                "responsibility_note": person.responsibility_note,
                "path": person.path,
                "source_id": person.source_id,
                "assignments": [to_plain(assignment) for assignment in person.assignments],
                "org_id": self.actor.org_id,
                "project_id": self.project_id,
                "topic_id": None,
                "author_id": "system",
                "sensitivity": "l1",
            }
            for person in people
        ]
        return {
            "summary": (
                f"查到 {len(rows)} 个匹配人员，关联任务 {len(task_rows)} 条、"
                f"问题 {len(issues)} 条、方法 {len(methods)} 条。"
            ),
            "people": rows,
            "tasks": task_rows,
            "issues": issues,
            "methods": methods,
        }

    def ingest_file(self, path: str = "") -> dict[str, Any]:
        if not self.uploads:
            return {"summary": "没有收到可处理的上传文件。", "candidate_count": 0}
        if any(upload.input_kind == "deliverable" for upload in self.uploads):
            return {
                "ok": False,
                "summary": "交付物附件不能进入资料抽取队列；请先查询交付物定义再归档版本。",
                "error": {
                    "code": "deliverable_requires_archive_tool",
                    "message": "Use get_deliverables and archive_deliverable for deliverable attachments.",
                },
            }
        tags = self._current_ingest_tags(sensitivity="l1")
        prepared = [
            (
                upload,
                resolve_project_input_kind(upload.filename, upload.content, upload.input_kind),
            )
            for upload in self.uploads
        ]
        queued: list[dict[str, Any]] = []
        for upload, input_kind in prepared:
            source = register_uploaded_project_file(
                filename=Path(upload.filename or path or "conversation_upload.md").name,
                content=upload.content,
                title=Path(upload.filename or "conversation_upload.md").stem,
                meeting_date="",
                topic="对话上传材料",
                access_tags=tags,
                raw_filename="",
                raw_content=None,
                input_kind=input_kind,
            )
            self.store.ingest(
                source,
                tags=tags,
                actor=self.actor,
                kind=source.input_kind,
                materialize=False,
            )
            job = enqueue_source_ingestion(store=self.store, source=source, actor=self.actor)
            queued.append({
                "filename": upload.filename,
                "input_kind": input_kind,
                "source": source_ingestion_view(source),
                "job_id": job["id"],
                "status": job["status"],
                "stage": job["stage"],
            })
        if self.ingestion_wake is not None:
            self.ingestion_wake()
        result: dict[str, Any] = {
            "summary": f"已将 {len(queued)} 个附件加入入库队列。",
            "jobs": queued,
            "file_count": len(queued),
        }
        if len(queued) == 1:
            result.update({
                "source": queued[0]["source"],
                "job_id": queued[0]["job_id"],
                "status": queued[0]["status"],
                "stage": queued[0]["stage"],
            })
        return result

    def get_deliverables(self) -> dict[str, Any]:
        rows = DeliverableService(self.store).list_requirements(
            actor=self.actor,
            access_context=self.access_context,
            project_id=self.project_id,
            milestone_id=self.milestone.milestone_id,
        )
        items = [_deliverable_tool_row(row) for row in rows]
        self._visible_deliverable_ids = {row["deliverable_id"] for row in items}
        return {
            "summary": f"查到 {len(items)} 个当前可见的交付物定义。",
            "items": items,
        }

    def archive_deliverable(
        self,
        *,
        deliverable_id: str,
        files: list[dict[str, Any]],
        note: str = "",
    ) -> dict[str, Any]:
        if not self.uploads:
            return {
                "ok": False,
                "summary": "本轮没有可归档的附件。",
                "error": {"code": "missing_attachments", "message": "No attached files are available."},
            }
        if not deliverable_id.strip() or not isinstance(files, list) or not files:
            return {
                "ok": False,
                "summary": "归档前必须从可见定义中选择交付物，并明确附件角色。",
                "error": {"code": "invalid_archive_request", "message": "deliverable_id and files are required."},
            }
        if deliverable_id not in self._visible_deliverable_ids:
            return {
                "ok": False,
                "summary": "归档前必须先查询当前里程碑且当前身份可见的交付物定义。",
                "error": {
                    "code": "deliverable_lookup_required",
                    "message": "Call get_deliverables and use an ID from that result.",
                },
            }
        uploads_by_name: dict[str, list[ConversationUpload]] = {}
        for upload in self.uploads:
            uploads_by_name.setdefault(upload.filename, []).append(upload)
        selected: list[DeliverableUpload] = []
        used_names: set[str] = set()
        for raw in files:
            if not isinstance(raw, dict):
                return {
                    "ok": False,
                    "summary": "附件归档参数格式不正确。",
                    "error": {"code": "invalid_file_mapping", "message": "Each file mapping must be an object."},
                }
            filename = Path(str(raw.get("filename") or "")).name
            matches = uploads_by_name.get(filename, [])
            if len(matches) != 1 or filename in used_names:
                return {
                    "ok": False,
                    "summary": f"附件 {filename or '未命名'} 无法与本轮上传唯一匹配。",
                    "error": {"code": "attachment_not_unique", "message": filename},
                }
            used_names.add(filename)
            upload = matches[0]
            selected.append(DeliverableUpload(
                filename=upload.filename,
                content=upload.content,
                content_type=upload.content_type,
                artifact_role=str(raw.get("artifact_role") or ""),
            ))
        try:
            version = DeliverableService(self.store).submit_version(
                deliverable_id,
                actor=self.actor,
                access_context=self.access_context,
                files=selected,
                note=note,
            )
        except KeyError:
            return {
                "ok": False,
                "summary": "交付物定义不存在或已不可用，请重新查询。",
                "error": {"code": "unknown_deliverable", "message": "Refresh with get_deliverables."},
            }
        except PermissionError as exc:
            return {
                "ok": False,
                "summary": "当前身份无权向该交付物归档文件。",
                "error": {"code": "forbidden", "message": str(exc)},
            }
        except DeliverableValidationError as exc:
            return {
                "ok": False,
                "summary": f"交付物归档失败：{exc}",
                "error": {"code": "invalid_deliverable", "message": str(exc)},
            }
        public_version = {
            key: value
            for key, value in version.items()
            if key not in {"payload", "files"}
        }
        public_version["files"] = [
            {key: value for key, value in row.items() if key not in {"payload", "relative_path"}}
            for row in version.get("files", [])
        ]
        return {
            "summary": (
                f"已将 {len(public_version['files'])} 个附件归档到交付物 "
                f"{deliverable_id} 的 v{public_version['version']}。"
            ),
            "version": public_version,
        }

    def draft(self, kind: str = "催办消息", target: str = "", topic: str = "") -> dict[str, Any]:
        content = f"{target or '相关负责人'}，请同步{topic or '当前任务'}的最新进展、风险和预计完成时间。"
        return {"summary": f"已起草{kind}。", "draft": content}

    def generate_daily_brief(self, push_feishu: bool = False) -> dict[str, Any]:
        brief = generate_daily_brief(
            store=self.store,
            milestone=self.milestone,
            project_id=self.project_id,
            push_feishu=push_feishu,
        )
        stored_source = self.store.get_source(brief["brief_id"]) or {}
        public_brief = {
            key: brief.get(key)
            for key in (
                "brief_id",
                "brief_date",
                "title",
                "content_markdown",
                "notification_count",
                "ai_commentary",
                "generated_at",
                "feishu",
            )
        }
        public_brief.update(
            {
                "org_id": str(stored_source.get("org_id") or self.actor.org_id),
                "project_id": self.project_id,
                "topic_id": stored_source.get("topic_id"),
                "author_id": str(
                    stored_source.get("author_id")
                    or self.actor.id
                ),
                "sensitivity": str(
                    stored_source.get("sensitivity")
                    or "l3"
                ),
            }
        )
        return {
            "summary": f"已生成 {brief['brief_date']} 项目经理晨报。",
            "brief": public_brief,
        }

    def request_project_inspection(self, reason: str = "") -> dict[str, Any]:
        job = InspectionDispatcher(self.store).enqueue_manual(
            actor=self.actor,
            project_id=self.project_id,
            reason=reason,
        )
        if self.inspection_wake is not None:
            self.inspection_wake()
        return {
            "summary": "已创建主动巡检作业，结果将在证据校验后进入待确认区。",
            "job_id": job["job_id"],
            "status": job["status"],
            "trigger_type": job["trigger_type"],
        }

    def propose_candidates(self, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        saved = 0
        rejected: list[dict[str, Any]] = []
        skipped: list[str] = []
        for index, raw in enumerate(items or []):
            try:
                refs, source_rows = self._validated_candidate_evidence(
                    list(raw.get("evidence_refs") or [])
                )
                category = _normalize_candidate_category(str(raw.get("category") or "things"))
                observed_fields = _clean_string_list(raw.get("observed_fields"))
                proposed_fields = _clean_string_list(raw.get("proposed_fields"))
                if category not in {"methods", "task"} and not observed_fields and not proposed_fields:
                    observed_fields = ["title", "description"]
                inference_basis = _clean_string_list(raw.get("inference_basis"))
                inference_confidence = str(raw.get("inference_confidence") or "").strip().lower()
                _validate_candidate_proposal_contract(
                    category=category,
                    raw=raw,
                    observed_fields=observed_fields,
                    proposed_fields=proposed_fields,
                    inference_basis=inference_basis,
                    inference_confidence=inference_confidence,
                )
                item_id = _candidate_proposal_id(
                    category,
                    str(raw.get("title") or ""),
                    refs,
                )
                try:
                    existing = self.store.get_item(item_id)
                except KeyError:
                    existing = None
                if existing is not None and (
                    existing.status != CandidateStatus.CANDIDATE
                    or bool(existing.confirmation_editor)
                ):
                    skipped.append(item_id)
                    continue
                sensitivity = _strictest_sensitivity(source_rows)
                proposed_sensitivity = str(raw.get("proposed_sensitivity") or "").strip()
                if (
                    proposed_sensitivity
                    and _SENSITIVITY_RANK.get(proposed_sensitivity, 0)
                    <= _SENSITIVITY_RANK.get(sensitivity, 0)
                ):
                    raise ValueError(
                        "proposed_sensitivity must be stricter than inherited evidence sensitivity"
                    )
                topic_ids = {
                    str(source.get("topic_id") or "")
                    for source in source_rows
                    if source.get("topic_id")
                }
                tags = self._current_ingest_tags(sensitivity=sensitivity)
                if len(topic_ids) == 1:
                    tags["topic_id"] = next(iter(topic_ids))
                item = InspectionItem(
                    item_id=item_id,
                    category=category,
                    title=str(raw.get("title") or "").strip(),
                    description=str(raw.get("description") or "").strip(),
                    evidence_refs=refs,
                    status=CandidateStatus.CANDIDATE,
                    owner_candidates=_clean_string_list(raw.get("owner_candidates")) or None,
                    matter_type=str(raw.get("matter_type") or "").strip() or None,
                    facet_types=_clean_string_list(raw.get("facet_types")) or None,
                    due_date=str(raw.get("due_date") or "").strip() or None,
                    deliverable=str(raw.get("deliverable") or "").strip() or None,
                    acceptance_criteria=str(raw.get("acceptance_criteria") or "").strip() or None,
                    business_goal=str(raw.get("business_goal") or "").strip() or None,
                    principles=_clean_string_list(raw.get("principles")) or None,
                    reasoning_chain=_clean_string_list(raw.get("reasoning_chain")) or None,
                    applicable_scope=str(raw.get("applicable_scope") or "").strip() or None,
                    inference_note=_candidate_inference_note(
                        proposed_fields,
                        inference_basis,
                        inference_confidence,
                    ),
                    observed_fields=observed_fields,
                    proposed_fields=proposed_fields,
                    inference_basis=inference_basis,
                    inference_confidence=inference_confidence,
                    org_id=tags["org_id"],
                    project_id=tags["project_id"],
                    topic_id=tags.get("topic_id"),
                    author_id=tags["author_id"],
                    sensitivity=tags["sensitivity"],
                    proposed_sensitivity=proposed_sensitivity or None,
                    sensitivity_reason=str(raw.get("sensitivity_reason") or "").strip() or None,
                    tag_origin="actor_ingest",
                    review_required=True,
                )
                self.store.ingest(item, tags=tags, actor=self.actor)
                saved += 1
            except (KeyError, TypeError, ValueError) as exc:
                rejected.append({
                    "index": index,
                    "title": str(raw.get("title") or "")[:160],
                    "error": str(exc),
                })
        summary = f"已写入 {saved} 条候选，等待人工确认。"
        if rejected:
            summary += f" {len(rejected)} 条因证据或字段契约不合格被拒绝。"
        if skipped:
            summary += f" {len(skipped)} 条已存在人工结论，未覆盖。"
        return {
            "summary": summary,
            "candidate_count": saved,
            "rejected_count": len(rejected),
            "rejected": rejected,
            "skipped_item_ids": skipped,
        }

    def _validated_candidate_evidence(
        self,
        evidence_rows: list[dict[str, Any]],
    ) -> tuple[list[EvidenceRef], list[dict[str, Any]]]:
        if not evidence_rows:
            raise ValueError("candidate must include evidence_refs")
        refs: list[EvidenceRef] = []
        sources: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for raw in evidence_rows:
            source_id = str(raw.get("source_doc_id") or "").strip()
            source_kind = str(raw.get("source_kind") or "curated_source").strip()
            locator = str(raw.get("locator") or "").strip()
            quote = str(raw.get("quote") or "").strip()
            if source_kind not in {"curated_source", "raw_source"}:
                raise ValueError(f"unsupported evidence source_kind: {source_kind}")
            source = self.store.get_source(source_id)
            if source is None or not visible(self.actor, source, self.access_context):
                raise ValueError(f"unknown or invisible evidence source: {source_id}")
            ref = _verified_candidate_evidence(
                store=self.store,
                source=source,
                source_kind=source_kind,
                locator=locator,
                quote=quote,
            )
            signature = (ref.source_doc_id, ref.locator, ref.quote)
            if signature in seen:
                continue
            seen.add(signature)
            refs.append(ref)
            sources.append(source)
        if not refs:
            raise ValueError("candidate evidence contains no distinct visible references")
        return refs, sources

    def _current_ingest_tags(self, *, sensitivity: str) -> dict[str, Any]:
        return {
            "org_id": self.actor.org_id,
            "project_id": self.store.default_project_id_for_actor(self.actor.id, self.actor.org_id),
            "topic_id": None,
            "author_id": self.actor.id,
            "sensitivity": sensitivity,
        }


_PROPOSAL_FIELDS = {
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
_SENSITIVITY_RANK = {"l1": 1, "l2": 2, "l3": 3, "l4": 4}


def _normalize_candidate_category(value: str) -> str:
    category = value.strip().lower()
    aliases = {
        "method": "methods",
        "methodology": "methods",
        "方法": "methods",
        "法": "methods",
        "tasks": "task",
        "todo": "task",
        "待办": "task",
        "issues": "issue",
        "问题": "issue",
        "people": "people",
        "person": "people",
        "人": "people",
        "thing": "things",
        "事": "things",
    }
    normalized = aliases.get(category, category)
    if normalized not in {"people", "things", "methods", "task", "issue"}:
        raise ValueError(f"unsupported candidate category: {value}")
    return normalized


def _clean_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("candidate list fields must be arrays")
    result: list[str] = []
    for item in value:
        text = " ".join(str(item or "").strip().split())
        if text and text not in result:
            result.append(text)
    return result


def _validate_candidate_proposal_contract(
    *,
    category: str,
    raw: dict[str, Any],
    observed_fields: list[str],
    proposed_fields: list[str],
    inference_basis: list[str],
    inference_confidence: str,
) -> None:
    title = str(raw.get("title") or "").strip()
    description = str(raw.get("description") or "").strip()
    if not title or not description:
        raise ValueError("candidate title and description are required")
    observed = set(observed_fields)
    proposed = set(proposed_fields)
    unknown = (observed | proposed) - _PROPOSAL_FIELDS
    if unknown:
        raise ValueError(
            "unsupported observed/proposed fields: " + ", ".join(sorted(unknown))
        )
    overlap = observed & proposed
    if overlap:
        raise ValueError(
            "fields cannot be both observed and proposed: " + ", ".join(sorted(overlap))
        )
    if proposed:
        if not inference_basis:
            raise ValueError("inference_basis is required when proposed_fields is non-empty")
        if inference_confidence not in {"low", "medium", "high"}:
            raise ValueError("inference_confidence must be low, medium, or high")
    elif inference_basis or inference_confidence:
        raise ValueError("inference metadata requires proposed_fields")

    required_values: set[str] = {"title", "description"}
    if category == "methods":
        required_values.update({
            "business_goal",
            "principles",
            "reasoning_chain",
            "applicable_scope",
        })
    elif category == "task":
        required_values.update({
            "owner_candidates",
            "due_date",
            "deliverable",
            "acceptance_criteria",
        })
    missing_values = [
        field_name
        for field_name in required_values
        if not _proposal_field_populated(raw.get(field_name))
    ]
    if missing_values:
        raise ValueError(
            f"{category} candidate is missing required fields: {', '.join(sorted(missing_values))}"
        )
    if category in {"methods", "task"}:
        missing_provenance = required_values - observed - proposed
        if missing_provenance:
            raise ValueError(
                "method/task fields must be classified as observed or proposed: "
                + ", ".join(sorted(missing_provenance))
            )
    proposed_sensitivity = str(raw.get("proposed_sensitivity") or "").strip()
    if proposed_sensitivity and not str(raw.get("sensitivity_reason") or "").strip():
        raise ValueError("sensitivity_reason is required for proposed_sensitivity")


def _proposal_field_populated(value: Any) -> bool:
    if isinstance(value, list):
        return any(str(item or "").strip() for item in value)
    return bool(str(value or "").strip())


def _candidate_proposal_id(
    category: str,
    title: str,
    refs: list[EvidenceRef],
) -> str:
    evidence_key = "|".join(sorted(
        f"{ref.source_doc_id}:{ref.locator}:{ref.quote}"
        for ref in refs
    ))
    digest = hashlib.sha256(
        f"{category}\0{title.strip()}\0{evidence_key}".encode("utf-8")
    ).hexdigest()[:20]
    prefix = "method" if category == "methods" else "task" if category == "task" else "candidate"
    return f"conversation_{prefix}_{digest}"


def _candidate_inference_note(
    proposed_fields: list[str],
    inference_basis: list[str],
    confidence: str,
) -> str:
    if not proposed_fields:
        return "字段来自已登记来源的观察，仍需人工确认结论。"
    return (
        f"模型建议字段：{'、'.join(proposed_fields)}；"
        f"推断依据：{'；'.join(inference_basis)}；"
        f"置信度：{confidence}。需人工确认后才能成为项目事实。"
    )


def _strictest_sensitivity(sources: list[dict[str, Any]]) -> str:
    values = [str(source.get("sensitivity") or "l1") for source in sources]
    return max(values or ["l1"], key=lambda value: _SENSITIVITY_RANK.get(value, 0))


def _verified_candidate_evidence(
    *,
    store: ProjectSQLiteStore,
    source: dict[str, Any],
    source_kind: str,
    locator: str,
    quote: str,
) -> EvidenceRef:
    if not locator or not quote:
        raise ValueError("evidence locator and exact quote are required")
    payload = dict(source.get("payload") or {})
    selected = dict(payload.get(source_kind) or {})
    selected_path = str(selected.get("path") or "").strip()
    if not selected_path and source_kind == "curated_source":
        selected_path = str(source.get("path") or "").strip()
    content = _read_evidence_text(store, selected_path)
    if _normalize_evidence_text(quote) not in _normalize_evidence_text(content):
        raise ValueError(
            f"evidence quote is not present in {source['id']} {source_kind}"
        )

    raw_entry = dict(payload.get("raw_source") or {})
    raw_path = str(raw_entry.get("path") or "").strip()
    curated_path = str((payload.get("curated_source") or {}).get("path") or "").strip()
    raw_traceable = False
    raw_locator = ""
    if source_kind == "raw_source" and _is_raw_authority(
        source,
        raw_path=selected_path,
        curated_path=curated_path,
    ):
        raw_traceable = True
        raw_locator = _quote_locator(content, quote)
    elif raw_entry.get("status") == "matched" and raw_path:
        try:
            raw_content = _read_evidence_text(store, raw_path)
        except ValueError:
            raw_content = ""
        if (
            raw_content
            and _normalize_evidence_text(quote) in _normalize_evidence_text(raw_content)
            and _is_raw_authority(source, raw_path=raw_path, curated_path=curated_path)
        ):
            raw_traceable = True
            raw_locator = _quote_locator(raw_content, quote)
    return EvidenceRef(
        source_doc_id=str(source["id"]),
        source_kind=source_kind,
        locator=locator,
        quote=quote[:300],
        raw_source_doc_id=str(source["id"]) if raw_traceable else "",
        raw_locator=raw_locator,
        evidence_level="raw_traceable" if raw_traceable else "curated_pending_raw",
    )


def _read_evidence_text(store: ProjectSQLiteStore, path_value: str) -> str:
    if not path_value:
        raise ValueError("evidence source has no readable path")
    path = resolve_repository_path(
        path_value,
        allowed_roots=(store.root_dir, store.root_dir.parent),
    )
    if path.suffix.lower() not in {".md", ".markdown", ".txt", ".docx"} or not path.is_file():
        raise ValueError(f"evidence source is not a readable text file: {path_value}")
    try:
        if path.suffix.lower() == ".docx":
            return parse_transcript_bytes(path.read_bytes(), path.name)
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"unable to read evidence source: {path_value}") from exc


def _is_raw_authority(
    source: dict[str, Any],
    *,
    raw_path: str,
    curated_path: str,
) -> bool:
    if str(source.get("input_kind") or "") == "transcript":
        return True
    marker_text = f"{source.get('title', '')} {raw_path}".lower()
    if any(marker in marker_text for marker in (
        "原始转写",
        "文字转写",
        "会议转写",
        "录音转写",
        "转写提取",
    )):
        return True
    return bool(raw_path and raw_path != curated_path)


def _normalize_evidence_text(value: str) -> str:
    return " ".join(str(value or "").strip().split()).lower()


def _quote_locator(content: str, quote: str) -> str:
    normalized_quote = _normalize_evidence_text(quote)
    if not normalized_quote:
        return ""
    for line_number, line in enumerate(str(content or "").splitlines(), start=1):
        if normalized_quote in _normalize_evidence_text(line):
            return f"line:{line_number}"
    normalized_content = _normalize_evidence_text(content)
    start = normalized_content.find(normalized_quote)
    if start < 0:
        return ""
    return f"normalized_chars:{start}-{start + len(normalized_quote)}"


def _tool_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(arguments)
    cleaned.pop("reason", None)
    return cleaned


def _tool_call_debug(call: Any) -> dict[str, Any]:
    return {
        "name": call.name,
        "arguments": _tool_arguments(call.arguments),
        "reason": call.reason,
        "call_id": call.call_id,
    }


def _tool_message_content(
    tool_result: dict[str, Any],
    *,
    max_chars: int = 2048,
) -> str:
    text = json.dumps(_compact_tool_result(tool_result), ensure_ascii=False, sort_keys=True)
    if len(text) <= max_chars:
        return text
    ordered_facts = sorted(
        [_compact_fact(fact) for fact in tool_result.get("facts", []) if isinstance(fact, dict)],
        key=lambda fact: _fact_priority(
            fact,
            summary=str(tool_result.get("summary") or ""),
        ),
    )
    facts: list[dict[str, Any]] = []
    seen_fact_values: set[tuple[str, str]] = set()
    for fact in ordered_facts:
        signature = (str(fact.get("kind") or ""), json.dumps(fact.get("value"), ensure_ascii=False, sort_keys=True))
        if signature in seen_fact_values:
            continue
        seen_fact_values.add(signature)
        facts.append(fact)
    for row_limit in (2, 1, 0):
        for fact_limit in range(min(len(facts), 12), -1, -1):
            payload = _model_tool_payload(
                tool_result,
                facts[:fact_limit],
                row_limit=row_limit,
            )
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if len(text) <= max_chars:
                return text
    return json.dumps(
        {
            "summary": str(tool_result.get("summary", ""))[:160],
            "truncated": True,
            "truncation_reason": "context_budget",
        },
        ensure_ascii=False,
    )


def _messages_debug(messages: list[dict[str, Any]]) -> str:
    user_inputs = [
        f"用户输入：{message.get('content')}"
        for message in messages
        if message.get("role") == "user"
    ][-3:]
    return ("\n".join(user_inputs) + "\n" + json.dumps(messages, ensure_ascii=False, indent=2, default=str))[:12000]


def _verify_reply_against_tool_results(
    reply: str,
    tool_steps: list[dict[str, Any]],
    *,
    user_text: str = "",
) -> dict[str, Any]:
    facts = {
        str(fact.get("fact_id")): fact
        for step in tool_steps
        for fact in (step.get("result", {}).get("facts", []) if isinstance(step.get("result"), dict) else [])
        if isinstance(fact, dict) and fact.get("fact_id")
    }
    cited = sorted(set(re.findall(r"F_[0-9A-Za-z_\-\u4e00-\u9fff]+", reply)))
    invalid_fact_ids = [fact_id for fact_id in cited if fact_id not in facts]
    unsupported: list[str] = []
    unsupported.extend(f"事实引用 [{fact_id}] 不存在于本轮权限过滤后的工具结果" for fact_id in invalid_fact_ids)
    fact_values = {str(fact.get("value")) for fact in facts.values()}
    source_dates = {
        str(ref.get("meeting_date") or ref.get("date"))
        for fact in facts.values()
        for ref in (fact.get("source_refs") or [])
        if isinstance(ref, dict) and (ref.get("meeting_date") or ref.get("date"))
    }
    numeric_fact_values = {
        normalized
        for fact in facts.values()
        if fact.get("kind") == "number"
        for normalized in [_normalize_number(fact.get("value"))]
        if normalized is not None
    }
    for value in sorted(set(re.findall(r"\d{4}-\d{2}-\d{2}", reply))):
        if value not in user_text and value not in fact_values and value not in source_dates:
            unsupported.append(f"日期 {value} 未在本轮工具结果或用户输入中找到")
    count_text = re.sub(r"\d{4}-\d{2}-\d{2}", "", reply)
    for value in sorted(
        set(
            re.findall(
                r"\d+(?:\.\d+)?\s*(?:条|项(?!目)|个|天|人|%)",
                count_text,
            )
        )
    ):
        number = re.match(r"\d+(?:\.\d+)?", value)
        if number and number.group(0) not in user_text and number.group(0) not in numeric_fact_values:
            unsupported.append(f"数字 {value} 未在本轮工具结果或用户输入中找到")
    claim_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for fact in facts.values():
        kind = str(fact.get("kind") or "")
        if (
            kind not in {"person", "date", "number", "status", "text"}
            or _is_citation_metadata_fact(fact)
        ):
            continue
        value = _normalize_number(fact.get("value")) if kind == "number" else str(fact.get("value"))
        if value is None or value in user_text or not _fact_value_appears(fact, reply):
            continue
        claim_groups.setdefault((kind, value), []).append(fact)
    cited_claim_count = sum(
        1
        for grouped_facts in claim_groups.values()
        if any(fact.get("fact_id") in cited for fact in grouped_facts)
    )
    for (kind, value), grouped_facts in claim_groups.items():
        if not any(fact.get("fact_id") in cited for fact in grouped_facts):
            unsupported.append(
                f"具体{kind}事实 {value} 缺少本轮事实引用"
            )
    claim_count = len(claim_groups)
    return {
        "checked": True,
        "unsupported": unsupported,
        "tool_step_count": len(tool_steps),
        "cited_fact_ids": [fact_id for fact_id in cited if fact_id in facts],
        "invalid_fact_ids": invalid_fact_ids,
        "citation_coverage": {
            "claim_count": claim_count,
            "cited_claim_count": cited_claim_count,
            "ratio": round(cited_claim_count / claim_count, 3) if claim_count else 1.0,
        },
    }


def _normalize_number(value: Any) -> str | None:
    try:
        normalized = Decimal(str(value)).normalize()
    except (InvalidOperation, ValueError):
        return None
    text = format(normalized, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _is_citation_metadata_fact(fact: dict[str, Any]) -> bool:
    path = str(fact.get("path") or "")
    return (
        ".source." in path
        or path.endswith((".source_id", ".source_title", ".source_doc_id", ".locator"))
    )


def _fact_value_appears(fact: dict[str, Any], reply: str) -> bool:
    if fact.get("kind") != "number":
        return str(fact.get("value")) in reply
    target = _normalize_number(fact.get("value"))
    if target is None:
        return False
    numeric_text = re.sub(r"\d{4}-\d{2}-\d{2}", "", reply)
    numeric_text = re.sub(r"F_[0-9A-Za-z_\-\u4e00-\u9fff]+", "", numeric_text)
    return target in {
        normalized
        for raw in re.findall(
            (
                r"(?<![\dA-Za-z_.])-?\d+(?:\.\d+)?"
                r"(?=\s*(?:条|项(?!目)|个|天|人|%|次|份|轮|阶段|小时|分钟))"
            ),
            numeric_text,
        )
        for normalized in [_normalize_number(raw)]
        if normalized is not None
    }


def _conversation_round_trace(
    runtime_events: list[dict[str, Any]],
    tool_steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rounds: dict[int, dict[str, Any]] = {}
    run_elapsed_ms = 0
    for event in runtime_events:
        kind = str(event.get("kind") or "")
        payload = dict(event.get("payload") or {})
        if kind == "run_stopped":
            run_elapsed_ms = int(payload.get("elapsed_ms") or 0)
            continue
        round_index = int(payload.get("round_index") or 0)
        if round_index <= 0:
            continue
        row = rounds.setdefault(round_index, {
            "round_index": round_index,
            "provider": "",
            "model": "",
            "input_message_count": 0,
            "input_char_count": 0,
            "input_token_count": 0,
            "model_output": "",
            "output_char_count": 0,
            "output_token_count": 0,
            "model_elapsed_ms": 0,
            "tool_calls": [],
            "blocked_tool_calls": [],
            "tool_results": [],
            "verification_errors": [],
            "context_compaction": {},
            "status": "running",
        })
        if kind == "model_turn":
            row.update({
                "provider": str(payload.get("provider") or ""),
                "model": str(payload.get("model") or ""),
                "input_message_count": int(payload.get("input_message_count") or 0),
                "input_char_count": int(payload.get("input_char_count") or 0),
                "input_token_count": int(payload.get("input_token_count") or 0),
                "model_output": str(payload.get("text") or ""),
                "output_char_count": int(payload.get("text_char_count") or 0),
                "output_token_count": int(payload.get("text_token_count") or 0),
                "model_elapsed_ms": int(payload.get("elapsed_ms") or 0),
                "tool_calls": list(payload.get("tool_calls") or []),
                "status": "tool_selected" if payload.get("tool_calls") else "answer_proposed",
            })
        elif kind == "completion_rejected":
            errors = [
                *list(payload.get("missing") or []),
                *list(payload.get("errors") or []),
            ]
            row["verification_errors"] = errors
            row["status"] = "verification_rejected"
        elif kind == "verification_context_compacted":
            row["context_compaction"] = payload
        elif kind == "verification_repair_tool_calls_blocked":
            row["blocked_tool_calls"] = list(payload.get("tool_calls") or [])
    for step in tool_steps:
        round_index = int(step.get("round_index") or 0)
        if round_index <= 0:
            continue
        row = rounds.setdefault(round_index, {
            "round_index": round_index,
            "provider": "",
            "model": "",
            "input_message_count": 0,
            "input_char_count": 0,
            "input_token_count": 0,
            "model_output": "",
            "output_char_count": 0,
            "output_token_count": 0,
            "model_elapsed_ms": 0,
            "tool_calls": [],
            "blocked_tool_calls": [],
            "tool_results": [],
            "verification_errors": [],
            "context_compaction": {},
            "status": "tool_selected",
        })
        row["tool_results"].append({
            "call_id": str(step.get("call_id") or ""),
            "name": str(step.get("tool_name") or ""),
            "status": str(step.get("status") or ""),
            "summary": str(step.get("summary") or ""),
            "elapsed_ms": int(step.get("elapsed_ms") or 0),
            "result": dict(step.get("result") or {}),
        })
    ordered = [rounds[index] for index in sorted(rounds)]
    if ordered:
        ordered[-1]["run_elapsed_ms"] = run_elapsed_ms
    return ordered


def _conversation_agent_trace(
    model_rounds: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    for round_row in model_rounds:
        trace.append({
            "kind": "model",
            "name": str(round_row.get("model") or round_row.get("provider") or "model"),
            "status": str(round_row.get("status") or ""),
            "round_index": int(round_row.get("round_index") or 0),
            "input_summary": (
                f"{round_row.get('input_message_count', 0)} messages / "
                f"{round_row.get('input_token_count', 0)} estimated tokens"
            ),
            "output_summary": str(round_row.get("model_output") or "")[:500],
            "data": {
                "tool_calls": list(round_row.get("tool_calls") or []),
                "blocked_tool_calls": list(round_row.get("blocked_tool_calls") or []),
                "verification_errors": list(round_row.get("verification_errors") or []),
                "context_compaction": dict(round_row.get("context_compaction") or {}),
                "elapsed_ms": int(round_row.get("model_elapsed_ms") or 0),
            },
        })
        for tool_result in round_row.get("tool_results", []):
            trace.append({
                "kind": "tool",
                "name": str(tool_result.get("name") or ""),
                "status": str(tool_result.get("status") or ""),
                "round_index": int(round_row.get("round_index") or 0),
                "input_summary": "",
                "output_summary": str(tool_result.get("summary") or ""),
                "data": dict(tool_result.get("result") or {}),
            })
    return trace


def _conversation_run(
    *,
    run_id: str,
    milestone: MilestonePlan,
    objective: str,
    created_at: str,
    provider_name: str,
    model_name: str,
    model_inputs: list[str],
    model_outputs: list[str],
    model_rounds: list[dict[str, Any]],
    tool_steps: list[dict[str, Any]],
    stop_reason: str,
    tool_names: list[str],
    project_id: str = "project_mvp",
) -> AgentRun:
    loop_rounds = [
        AgentLoopRound(
            round_index=int(round_row["round_index"]),
            phase="conversation",
            objective=objective,
            model_input_summary=(
                f"{round_row.get('input_message_count', 0)} messages / "
                f"{round_row.get('input_token_count', 0)} estimated tokens"
            ),
            model_output_summary=str(round_row.get("model_output") or "")[:500],
            tool_name="、".join(
                str(call.get("name") or "")
                for call in round_row.get("tool_calls", [])
            ),
            tool_result_summary="；".join(
                str(result.get("summary") or "")
                for result in round_row.get("tool_results", [])
            )[:500],
            decision="；".join(
                str(call.get("reason") or "")
                for call in round_row.get("tool_calls", [])
                if call.get("reason")
            ),
            state_changes=[
                str(call.get("name") or "")
                for call in round_row.get("tool_calls", [])
            ],
            errors=list(round_row.get("verification_errors") or []),
            stop_reason=stop_reason if index == len(model_rounds) - 1 else "",
            tool_calls=list(round_row.get("tool_calls") or []),
        )
        for index, round_row in enumerate(model_rounds)
    ]
    return AgentRun(
        run_id=run_id,
        milestone_id=milestone.milestone_id,
        milestone_plan=milestone,
        objective=objective,
        plan=[],
        steps=[],
        observations=[
            AgentObservation(
                observation_id=f"conversation_tool_{step['round_index']}",
                tool_name=step["tool_name"],
                summary=step.get("summary", ""),
                data=step.get("result", {}),
            )
            for step in tool_steps
        ],
        final_report=None,
        confirmed_item_ids=[],
        created_at=created_at,
        status="completed",
        runtime_kind="conversation_agent_loop",
        input_snapshot={"project_id": project_id},
        agent_trace=_conversation_agent_trace(model_rounds),
        loop_rounds=loop_rounds,
        stop_reason=stop_reason,
        model_io_events=[
            {
                "event_id": "conversation_model_io",
                "provider": provider_name,
                "model": model_name,
                "api_surface": "conversation_agent_loop",
                "request": {
                    "agent_name": "project_conversation_agent",
                    "input": "\n\n".join(model_inputs),
                    "instructions": "模型选择下一步工具，工具结果用于生成有证据回答。",
                    "tools": tool_names,
                    "output_type": "tool_decision",
                },
                "responses": [
                    {
                        "raw_response_id": f"round_{round_row['round_index']}",
                        "output_text": str(round_row.get("model_output") or ""),
                        "token_usage": {
                            "input_estimated": int(round_row.get("input_token_count") or 0),
                            "output_estimated": int(round_row.get("output_token_count") or 0),
                            "source": "local_tokenizer_estimate",
                        },
                        "tool_calls": list(round_row.get("tool_calls") or []),
                    }
                    for round_row in model_rounds
                ],
                "final_output_type": "conversation_answer",
            }
        ],
    )


def _context_bundle(message: str, view: str, history: list[dict[str, Any]], tool_steps: list[dict[str, Any]]) -> list[str]:
    return [
        f"用户输入：{message or '用户上传了文件'}",
        f"当前页面：{view}",
        f"会话历史：{len(history)} 条",
        "工具调用：" + "；".join(f"{step['tool_name']}({step['summary']})" for step in tool_steps),
    ]


def _compact_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = dict(result)
    if isinstance(compact.get("items"), list):
        compact["items"] = compact["items"][:10]
    if isinstance(compact.get("external_items"), list):
        compact["external_items"] = compact["external_items"][:5]
    if isinstance(compact.get("facts"), list):
        compact["facts"] = compact["facts"][:160]
    return compact


def _compact_fact(fact: dict[str, Any]) -> dict[str, Any]:
    refs = fact.get("source_refs") or []
    compact_ref = dict(refs[0]) if refs and isinstance(refs[0], dict) else {}
    compact_ref.pop("quote", None)
    return {
        "fact_id": fact.get("fact_id"),
        "kind": fact.get("kind"),
        "value": fact.get("value"),
        "path": fact.get("path"),
        "source_ref": compact_ref,
    }


def _fact_priority(
    fact: dict[str, Any],
    *,
    summary: str = "",
) -> tuple[int, str]:
    kind = str(fact.get("kind") or "")
    path = str(fact.get("path") or "")
    value = str(fact.get("value") or "")
    if summary and (
        (kind == "number" and _fact_value_appears(fact, summary))
        or (kind != "number" and value and value in summary)
    ):
        priority = -1
    elif kind == "person" or (
        kind == "text"
        and any(
            path.endswith(suffix)
            for suffix in (
                ".role",
                ".group",
                ".responsibility_note",
                ".responsibility_summary",
            )
        )
    ) or (
        kind == "text"
        and path.endswith(".title")
        and "[0].title" in path
    ):
        priority = 0
    elif kind == "date":
        priority = 1
    elif kind == "number" and path in {
        "$.items.length",
        "$.rows.length",
        "$.tasks.length",
        "$.people.length",
        "$.issues.length",
        "$.methods.length",
    }:
        priority = 1
    elif kind == "status":
        priority = 2
    elif kind == "text" and path.endswith(".title"):
        priority = 3
    elif kind == "number" and not path.endswith(".length"):
        priority = 4
    elif kind == "text":
        priority = 5
    else:
        priority = 6
    return priority, path


def _model_tool_payload(
    result: dict[str, Any],
    facts: list[dict[str, Any]],
    *,
    row_limit: int = 2,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "summary": result.get("summary", ""),
        "facts": facts,
        "fact_count": result.get("fact_count", len(facts)),
        "truncated": True,
        "truncation_reason": "context_budget",
    }
    for key in ("items", "external_items", "rows", "tasks", "people", "issues", "methods"):
        rows = result.get(key)
        if isinstance(rows, list) and row_limit > 0:
            payload[key] = [
                _compact_model_row(row)
                for row in rows[:row_limit]
                if isinstance(row, dict)
            ]
    return payload


def _compact_model_row(row: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id",
        "item_id",
        "work_item_id",
        "deliverable_id",
        "milestone_id",
        "person_id",
        "title",
        "name",
        "owners",
        "owner_candidates",
        "status",
        "overdue",
        "due_date",
        "deliverable",
        "acceptance_criteria",
        "type_label",
        "current_version",
        "group",
        "role",
        "responsibility_note",
        "description",
        "source_id",
        "source_title",
        "meeting_date",
        "locator",
        "content",
        "highlight",
        "match_reasons",
    )
    compact = {key: row[key] for key in fields if row.get(key) not in (None, "", [])}
    for key in ("description", "responsibility_note", "acceptance_criteria"):
        if isinstance(compact.get(key), str):
            compact[key] = compact[key][:100]
    if isinstance(compact.get("content"), str):
        compact["content"] = compact["content"][:600]
    if isinstance(compact.get("highlight"), str):
        compact["highlight"] = compact["highlight"][:320]
    ref_key = "evidence" if row.get("evidence") else "evidence_refs"
    refs = row.get(ref_key) or []
    if refs and isinstance(refs[0], dict):
        compact[ref_key] = [{
            key: refs[0].get(key)
            for key in ("source_doc_id", "meeting_date", "locator")
            if refs[0].get(key)
        }]
    source = row.get("source")
    if isinstance(source, dict):
        compact["source"] = {
            key: source.get(key)
            for key in ("id", "title", "meeting_date")
            if source.get(key)
        }
    return compact


def _user_content(message: str, uploads: list[ConversationUpload]) -> str:
    parts = [message.strip() or "请处理上传文件"]
    for upload in uploads:
        parts.append(f"附件：{upload.filename}")
        parts.append(f"附件用途：{upload.input_kind}")
    return "\n".join(parts)


def _deliverable_tool_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["deliverable_id"],
        "deliverable_id": row["deliverable_id"],
        "org_id": row["org_id"],
        "project_id": row["project_id"],
        "topic_id": row.get("topic_id"),
        "author_id": row["author_id"],
        "sensitivity": row["sensitivity"],
        "milestone_id": row["milestone_id"],
        "title": row["title"],
        "type_label": row["type_label"],
        "acceptance_criteria": row["acceptance_criteria"],
        "due_date": row["due_date"],
        "status": row["status"],
        "current_version": row["current_version"],
        "submitted_files": [
            {
                "file_id": file_row["file_id"],
                "original_name": file_row["original_name"],
                "artifact_role": file_row["artifact_role"],
            }
            for version in row.get("versions", [])
            for file_row in version.get("files", [])
        ],
    }


def _normalize_uploads(
    upload: ConversationUpload | None,
    uploads: list[ConversationUpload] | None,
) -> list[ConversationUpload]:
    result: list[ConversationUpload] = []
    seen: set[tuple[str, str, str]] = set()
    for item in [*(uploads or []), *([upload] if upload is not None else [])]:
        key = (
            item.filename,
            hashlib.sha256(item.content).hexdigest(),
            item.content_type,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _task_row(item: WorkItem, overdue: bool) -> dict[str, Any]:
    return {
        "id": item.work_item_id,
        "work_item_id": item.work_item_id,
        "org_id": item.org_id,
        "project_id": item.project_id,
        "topic_id": item.topic_id,
        "author_id": item.author_id,
        "sensitivity": item.sensitivity,
        "title": item.title,
        "description": item.description,
        "status": item.status.value,
        "owners": item.owner_candidates or [],
        "due_date": item.due_date or "",
        "deliverable": item.deliverable or "",
        "acceptance_criteria": item.acceptance_criteria or "",
        "overdue": overdue,
        "evidence": _evidence_rows(item.evidence_refs),
    }


def _task_in_actor_role_scope(
    actor: User,
    ctx: AccessContext,
    project_id: str,
    item: WorkItem,
) -> bool:
    view_mode = role_view_key(ctx.role_of(actor, project_id))
    if view_mode == "pmo":
        return True
    if view_mode == "viewer":
        return False
    if view_mode in {"professional_lead", "topic_lead"}:
        return bool(
            item.author_id == actor.id
            or (item.topic_id and actor.id in ctx.members_of(item.topic_id))
        )
    if view_mode == "exec":
        searchable = " ".join([
            " ".join(str(value) for value in (item.owner_candidates or [])),
            item.title,
            item.description,
        ])
        return item.author_id == actor.id or bool(
            (actor.id and actor.id in searchable)
            or (actor.name and actor.name in searchable)
        )
    return False


def _evidence_rows(refs: list[EvidenceRef]) -> list[dict[str, str]]:
    return [
        {
            "source_doc_id": ref.source_doc_id,
            "source_kind": ref.source_kind,
            "locator": ref.locator,
            "quote": ref.quote,
            "evidence_level": ref.evidence_level,
        }
        for ref in refs
    ]


def _is_task_item(item: dict[str, Any]) -> bool:
    category = str(item.get("category") or "").lower()
    return (
        category in {"followup", "task", "tasks", "todo", "todos", "待办", "追问草稿"}
        or str(item.get("item_id") or "").startswith("task_")
        or bool(item.get("due_date") or item.get("deliverable") or item.get("acceptance_criteria"))
    )


def _is_method_item(item: dict[str, Any]) -> bool:
    category = str(item.get("category") or "").lower()
    return (
        category in {"method", "methods", "methodology", "法", "方法"}
        or str(item.get("item_id") or "").startswith("method_")
        or bool(item.get("business_goal") or item.get("principles") or item.get("reasoning_chain") or item.get("applicable_scope"))
    )


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
