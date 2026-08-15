from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from agent.schemas import AgentPlanStep, to_plain


class HarnessToolError(RuntimeError):
    pass


@dataclass
class HarnessTask:
    step_id: str
    tool_name: str
    reason: str
    status: str = "pending"
    attempts: int = 0
    input_summary: str = ""
    output_summary: str = ""
    error: str = ""
    updated_at: str = ""


@dataclass
class ContextRecord:
    ref: str
    step_id: str
    tool_name: str
    summary: str
    char_count: int
    archived: bool


@dataclass
class AgentHarness:
    run_id: str
    plan: list[AgentPlanStep]
    max_active_chars: int = 6000
    max_tool_attempts: int = 2
    phase: str = "initialized"
    tasks: list[HarnessTask] = field(init=False)
    events: list[dict[str, Any]] = field(default_factory=list)
    hooks: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    active_context: list[ContextRecord] = field(default_factory=list)
    archived_context: list[ContextRecord] = field(default_factory=list)
    failure_count: int = 0

    def __post_init__(self) -> None:
        self.tasks = [
            HarnessTask(
                step_id=step.step_id,
                tool_name=step.tool_name,
                reason=step.reason,
                updated_at=_now(),
            )
            for step in self.plan
        ]
        self._event("run_initialized", message=f"planned {len(self.tasks)} steps")

    def start(self) -> None:
        self.phase = "running"
        self._event("run_started", message="harness started bounded agent loop")

    def complete(self, *, success: bool | None = None) -> None:
        if success is False or (success is None and self.failure_count):
            self.phase = "failed"
        else:
            self.phase = "completed"
        self._event("run_completed", message=f"harness phase={self.phase}")

    def add_step(self, step: AgentPlanStep) -> None:
        if any(task.step_id == step.step_id for task in self.tasks):
            raise ValueError(f"Duplicate harness step: {step.step_id}")
        self.plan.append(step)
        self.tasks.append(
            HarnessTask(
                step_id=step.step_id,
                tool_name=step.tool_name,
                reason=step.reason,
                updated_at=_now(),
            )
        )
        self._event("step_planned", step=step, message=f"{step.tool_name} selected by planner")

    def start_step(self, step: AgentPlanStep, *, input_summary: str) -> None:
        task = self._task(step.step_id)
        task.attempts += 1
        self._mark_task(task, "running", input_summary=input_summary)
        self._hook("pre_step", step, "checked permissions, state, and context budget")
        self._event(
            "step_started",
            step=step,
            message=f"{step.tool_name} started",
            payload={"input_summary": input_summary},
        )

    def complete_step(
        self,
        step: AgentPlanStep,
        output: dict[str, Any],
    ) -> str:
        task = self._task(step.step_id)
        summary = str(output.get("summary", "completed"))
        context_ref = self._record_context(step, output, summary)
        self._mark_task(task, "completed", output_summary=summary)
        self._hook("post_step", step, f"stored result as {context_ref}")
        self._event(
            "step_completed",
            step=step,
            message=f"{step.tool_name} completed",
            payload={"output_summary": summary, "context_ref": context_ref},
        )
        self.trace.append(
            {
                "kind": "tool_call",
                "name": step.tool_name,
                "status": "completed",
                "input_summary": task.input_summary,
                "output_summary": summary,
                "context_ref": context_ref,
                "attempts": task.attempts,
                "data": _trace_data(output),
            }
        )
        return context_ref

    def fail_step(self, step: AgentPlanStep, error: str) -> None:
        task = self._task(step.step_id)
        self.failure_count += 1
        self._mark_task(task, "failed", error=error, output_summary=error)
        self._event(
            "tool_error",
            step=step,
            status="failed",
            message=error,
            payload={"attempt": task.attempts},
        )

    def run_step(
        self,
        step: AgentPlanStep,
        tool: Callable[[Any], dict[str, Any]],
        context: Any,
        input_summary: str,
    ) -> dict[str, Any]:
        task = self._task(step.step_id)
        self.start_step(step, input_summary=input_summary)

        output: dict[str, Any] | None = None
        last_error = ""
        for attempt in range(1, self.max_tool_attempts + 1):
            task.attempts = attempt
            try:
                output = tool(context)
                break
            except Exception as exc:  # pragma: no cover - exercised by unit test
                last_error = f"{type(exc).__name__}: {exc}"
                self.failure_count += 1
                self._event(
                    "tool_error",
                    step=step,
                    status="failed",
                    message=last_error,
                    payload={"attempt": attempt},
                )
                if getattr(exc, "non_retryable", False) or attempt >= self.max_tool_attempts:
                    self._mark_task(task, "failed", error=last_error)
                    raise HarnessToolError(
                        f"{step.tool_name} failed after {attempt} attempts: {last_error}"
                    ) from exc

        if output is None:
            self._mark_task(task, "failed", error=last_error or "tool returned no output")
            raise HarnessToolError(f"{step.tool_name} failed without structured output")

        self.complete_step(step, output)
        return output

    def snapshot(self) -> dict[str, Any]:
        active_chars = sum(record.char_count for record in self.active_context)
        completed_steps = sum(1 for task in self.tasks if task.status == "completed")
        return {
            "phase": self.phase,
            "max_steps": len(self.tasks),
            "completed_steps": completed_steps,
            "failure_count": self.failure_count,
            "max_tool_attempts": self.max_tool_attempts,
            "tasks": [to_plain(task) for task in self.tasks],
            "events": self.events,
            "hooks": self.hooks,
            "context_window": {
                "max_active_chars": self.max_active_chars,
                "active_chars": active_chars,
                "active_count": len(self.active_context),
                "archived_count": len(self.archived_context),
                "active_refs": [record.ref for record in self.active_context],
                "archived_refs": [record.ref for record in self.archived_context],
                "last_summary": (
                    self.active_context[-1].summary
                    if self.active_context
                    else self.archived_context[-1].summary
                    if self.archived_context
                    else ""
                ),
            },
        }

    def _record_context(
        self,
        step: AgentPlanStep,
        output: dict[str, Any],
        summary: str,
    ) -> str:
        raw = json.dumps(to_plain(output), ensure_ascii=False, sort_keys=True, default=str)
        ref = f"ctx_{len(self.active_context) + len(self.archived_context) + 1:02d}_{step.step_id}"
        active_chars = sum(record.char_count for record in self.active_context)
        should_archive = len(raw) > self.max_active_chars or active_chars + len(raw) > self.max_active_chars
        record = ContextRecord(
            ref=ref,
            step_id=step.step_id,
            tool_name=step.tool_name,
            summary=summary,
            char_count=len(raw),
            archived=should_archive,
        )
        if should_archive:
            self.archived_context.append(record)
            self._hook("context_compaction", step, f"archived {len(raw)} chars")
        else:
            self.active_context.append(record)
            self._hook("context_active", step, f"kept {len(raw)} chars active")
        return ref

    def _task(self, step_id: str) -> HarnessTask:
        for task in self.tasks:
            if task.step_id == step_id:
                return task
        raise KeyError(f"Unknown harness step: {step_id}")

    def _mark_task(
        self,
        task: HarnessTask,
        status: str,
        input_summary: str | None = None,
        output_summary: str | None = None,
        error: str | None = None,
    ) -> None:
        task.status = status
        if input_summary is not None:
            task.input_summary = input_summary
        if output_summary is not None:
            task.output_summary = output_summary
        if error is not None:
            task.error = error
        task.updated_at = _now()

    def _event(
        self,
        event_type: str,
        step: AgentPlanStep | None = None,
        status: str = "ok",
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            {
                "event_id": f"evt_{len(self.events) + 1:04d}",
                "event_type": event_type,
                "status": status,
                "step_id": step.step_id if step else "",
                "tool_name": step.tool_name if step else "",
                "message": message,
                "payload": payload or {},
                "created_at": _now(),
            }
        )

    def _hook(self, hook_type: str, step: AgentPlanStep, message: str) -> None:
        self.hooks.append(
            {
                "hook_id": f"hook_{len(self.hooks) + 1:04d}",
                "hook_type": hook_type,
                "step_id": step.step_id,
                "tool_name": step.tool_name,
                "message": message,
                "created_at": _now(),
            }
        )


def _trace_data(output: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in output.items()
        if key not in {"manifest", "people_structure", "report"}
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
