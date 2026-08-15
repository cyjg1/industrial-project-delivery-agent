from __future__ import annotations

import inspect
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

from agent.memory import get_token_counter
from agent.runtime.contracts import (
    CompletionDecision,
    CompletionGate,
    PlannerTurn,
    RuntimeFeatures,
    RuntimeEvent,
    RuntimePlanner,
    RuntimeResult,
    SelectedToolStep,
    ToolCallRequest,
    ToolInvoker,
)
from agent.runtime.event_sink import NullRuntimeEventSink, RuntimeEventSink
from agent.runtime.middleware import MiddlewareStack
from agent.runtime.state import RuntimeState


class BoundedAgentRuntime:
    def __init__(
        self,
        *,
        planner: RuntimePlanner,
        tools: Sequence[dict[str, Any]],
        invoke_tool: ToolInvoker,
        completion_gate: CompletionGate,
        features: RuntimeFeatures,
        middleware: MiddlewareStack,
        event_sink: RuntimeEventSink | None = None,
    ) -> None:
        self.planner = planner
        self.tools = list(tools)
        self.invoke_tool = invoke_tool
        self.completion_gate = completion_gate
        self.features = features
        self.middleware = middleware
        self.event_sink = event_sink or NullRuntimeEventSink()

    def run(
        self,
        *,
        run_id: str,
        system_prompt: str,
        user_prompt: str,
        domain_state: Any,
        initial_messages: Sequence[dict[str, Any]] | None = None,
        resume_checkpoint: dict[str, Any] | None = None,
    ) -> RuntimeResult:
        final_result: RuntimeResult | None = None
        for event in self.stream(
            run_id=run_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            domain_state=domain_state,
            initial_messages=initial_messages,
            resume_checkpoint=resume_checkpoint,
        ):
            if event.result is not None:
                final_result = event.result
        if final_result is None:
            raise RuntimeError("agent runtime stopped without a terminal result")
        return final_result

    def stream(
        self,
        *,
        run_id: str,
        domain_state: Any,
        initial_messages: Sequence[dict[str, Any]] | None = None,
        system_prompt: str = "",
        user_prompt: str = "",
        resume_checkpoint: dict[str, Any] | None = None,
    ) -> Iterator[RuntimeEvent]:
        messages = list(initial_messages or [])
        if not messages:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        if resume_checkpoint:
            state = RuntimeState.restore(
                checkpoint=resume_checkpoint,
                run_id=run_id,
                domain_state=domain_state,
                tools=list(self.tools),
            )
        else:
            state = RuntimeState(
                run_id=run_id,
                domain_state=domain_state,
                messages=messages,
                tools=list(self.tools),
            )
        completion = _evaluate_completion(
            self.completion_gate,
            domain_state,
            state,
        )
        started_at = time.monotonic()
        self.middleware.before_run(state)
        self._save_checkpoint(
            state,
            stage="completed" if state.completed else "before_model",
            next_round=state.next_round,
        )
        yield self._record_event(
            state,
            "run_started",
            {
                "tool_count": len(state.tools),
                "resumed": bool(resume_checkpoint),
                "next_round": state.next_round,
            },
        )

        try:
            first_round = (
                self.features.max_rounds + 1
                if state.completed
                else state.next_round
            )
            for round_index in range(
                first_round,
                self.features.max_rounds + 1,
            ):
                if time.monotonic() - started_at >= self.features.max_elapsed_seconds:
                    state.stop_reason = "max_elapsed_time_reached"
                    break
                self.middleware.before_model(state)
                model_input_metrics = _message_trace_metrics(state.messages)
                model_started_at = time.monotonic()
                text_parts: list[str] = []
                tool_calls: list[ToolCallRequest] = []
                active_tools = [] if state.verification_repair_pending else state.tools
                provider = str(getattr(self.planner, "name", ""))
                model = ""
                stream_turn = getattr(self.planner, "stream_turn", None)
                if stream_turn is None:
                    turn = self.planner.next_turn(state.messages, active_tools)
                    text_parts.append(turn.text)
                    tool_calls.extend(turn.tool_calls)
                    provider = turn.provider
                    model = turn.model
                else:
                    for planner_event in stream_turn(state.messages, active_tools):
                        provider = planner_event.provider or provider
                        model = planner_event.model or model
                        if planner_event.kind == "reset":
                            text_parts = []
                            tool_calls = []
                            yield self._record_event(
                                state,
                                "model_output_reset",
                                {"reason": planner_event.error},
                                round_index=round_index,
                            )
                        elif planner_event.kind == "delta":
                            text_parts.append(planner_event.text)
                            yield self._record_event(
                                state,
                                "model_delta",
                                {"text": planner_event.text},
                                round_index=round_index,
                            )
                        elif planner_event.kind == "tool_calls":
                            tool_calls.extend(planner_event.tool_calls)
                        if (
                            time.monotonic() - started_at
                            >= self.features.max_elapsed_seconds
                        ):
                            break
                blocked_repair_calls = (
                    tuple(tool_calls)
                    if state.verification_repair_pending and tool_calls
                    else ()
                )
                if blocked_repair_calls:
                    tool_calls = []
                    yield self._record_event(
                        state,
                        "verification_repair_tool_calls_blocked",
                        {
                            "tool_calls": [
                                {
                                    "call_id": call.call_id,
                                    "name": call.name,
                                    "arguments": dict(call.arguments),
                                    "reason": call.reason,
                                }
                                for call in blocked_repair_calls
                            ],
                        },
                        round_index=round_index,
                    )
                turn = PlannerTurn(
                    text="".join(text_parts),
                    tool_calls=tuple(tool_calls),
                    provider=provider,
                    model=model,
                )
                state.model_turns.append(turn)
                yield self._record_event(
                    state,
                    "model_turn",
                    {
                        "round_index": round_index,
                        "provider": turn.provider,
                        "model": turn.model,
                        "tool_names": [call.name for call in turn.tool_calls],
                        "tool_calls": [
                            {
                                "call_id": call.call_id,
                                "name": call.name,
                                "arguments": dict(call.arguments),
                                "reason": call.reason,
                            }
                            for call in turn.tool_calls
                        ],
                        "text": turn.text,
                        "text_char_count": len(turn.text),
                        "text_token_count": _count_tokens(turn.text),
                        "input_message_count": model_input_metrics["message_count"],
                        "input_char_count": model_input_metrics["char_count"],
                        "input_token_count": model_input_metrics["token_count"],
                        "elapsed_ms": int((time.monotonic() - model_started_at) * 1000),
                    },
                    round_index=round_index,
                )
                self.middleware.after_model(state, turn)
                if (
                    time.monotonic() - started_at
                    >= self.features.max_elapsed_seconds
                ):
                    state.stop_reason = "max_elapsed_time_reached"
                    if turn.text or turn.tool_calls:
                        yield self._record_event(
                            state,
                            "model_output_reset",
                            {"reason": "max_elapsed_time_reached"},
                            round_index=round_index,
                        )
                    self._save_checkpoint(
                        state,
                        stage="stopped",
                        next_round=round_index + 1,
                    )
                    break

                if turn.tool_calls:
                    state.messages.append(_assistant_tool_message(turn.text, turn.tool_calls))
                    duplicate_skipped = False
                    executed_progress = False
                    tool_budget_reached = False
                    elapsed_budget_reached = False
                    for call in turn.tool_calls:
                        step = SelectedToolStep(
                            step_id=f"step_{len(state.plan) + 1:02d}",
                            tool_name=call.name,
                            reason=call.reason or "selected by the planning model",
                            arguments=dict(call.arguments),
                        )
                        state.plan.append(step)
                        state.tool_call_count += 1
                        signature = _call_signature(call)
                        state.call_counts[signature] = state.call_counts.get(signature, 0) + 1
                        skipped_reason = ""
                        if state.tool_call_count > self.features.max_tool_calls:
                            tool_budget_reached = True
                            skipped_reason = "max_tool_calls"
                            summary = "Skipped tool call because the run exhausted its tool-call budget."
                        elif time.monotonic() - started_at >= self.features.max_elapsed_seconds:
                            elapsed_budget_reached = True
                            skipped_reason = "max_elapsed_time"
                            summary = "Skipped tool call because the run exhausted its elapsed-time budget."
                        elif state.call_counts[signature] >= self.features.duplicate_call_limit:
                            duplicate_skipped = True
                            skipped_reason = "no_progress"
                            summary = "Skipped repeated tool call because the same call made no progress."
                        if skipped_reason:
                            result = {
                                "ok": False,
                                "summary": summary,
                                "skipped": skipped_reason,
                            }
                            state.tool_results.append(
                                {
                                    "step_id": step.step_id,
                                    "round_index": round_index,
                                    "call_id": call.call_id,
                                    "name": call.name,
                                    "arguments": dict(call.arguments),
                                    "reason": step.reason,
                                    "result": result,
                                }
                            )
                            state.messages.append(_tool_result_message(call, result))
                            yield self._record_event(
                                state,
                                "tool_result",
                                {
                                    "step_id": step.step_id,
                                    "call_id": call.call_id,
                                    "name": call.name,
                                    "ok": False,
                                    "summary": result["summary"],
                                    "skipped": skipped_reason,
                                    "result": result,
                                },
                                round_index=round_index,
                            )
                            continue
                        executed_progress = True
                        self.middleware.before_tool(state, call)
                        self._save_checkpoint(
                            state,
                            stage="tool_inflight",
                            next_round=round_index,
                            inflight_tool={
                                "call_id": call.call_id,
                                "name": call.name,
                                "arguments": dict(call.arguments),
                                "signature": signature,
                            },
                        )
                        yield self._record_event(
                            state,
                            "tool_start",
                            {
                                "step_id": step.step_id,
                                "call_id": call.call_id,
                                "name": call.name,
                                "arguments": dict(call.arguments),
                                "reason": step.reason,
                            },
                            round_index=round_index,
                        )
                        tool_started_at = time.monotonic()
                        try:
                            result = dict(self.invoke_tool(call) or {})
                        except Exception as exc:
                            self.middleware.on_error(state, exc, call)
                            result = {
                                "ok": False,
                                "summary": f"{type(exc).__name__}: {exc}",
                                "error": {
                                    "code": "tool_error",
                                    "message": str(exc),
                                },
                            }
                        result.setdefault("ok", True)
                        # A different executed call is observable progress. Only
                        # consecutive identical calls remain eligible for the
                        # duplicate no-progress stop.
                        state.call_counts = {signature: 1}
                        state.tool_results.append(
                            {
                                "step_id": step.step_id,
                                "round_index": round_index,
                                "call_id": call.call_id,
                                "name": call.name,
                                "arguments": dict(call.arguments),
                                "reason": step.reason,
                                "result": result,
                            }
                        )
                        model_result = self.middleware.after_tool(state, call, result)
                        state.messages.append(_tool_result_message(call, model_result))
                        yield self._record_event(
                            state,
                            "tool_result",
                            {
                                "step_id": step.step_id,
                                "call_id": call.call_id,
                                "name": call.name,
                                "ok": bool(result.get("ok", True)),
                                "summary": str(result.get("summary", "")),
                                "result": result,
                                "elapsed_ms": int((time.monotonic() - tool_started_at) * 1000),
                            },
                            round_index=round_index,
                        )
                        if (
                            time.monotonic() - started_at
                            >= self.features.max_elapsed_seconds
                        ):
                            elapsed_budget_reached = True
                    state.next_round = round_index + 1
                    self._save_checkpoint(
                        state,
                        stage="before_model",
                        next_round=state.next_round,
                    )
                    completion = _evaluate_completion(
                        self.completion_gate,
                        domain_state,
                        state,
                    )
                    if tool_budget_reached:
                        state.stop_reason = "max_tool_calls_reached"
                        break
                    if elapsed_budget_reached:
                        state.stop_reason = "max_elapsed_time_reached"
                        break
                    if duplicate_skipped and not executed_progress:
                        state.stop_reason = "no_progress"
                        break
                    continue

                state.final_text = turn.text
                state.messages.append({"role": "assistant", "content": turn.text})
                completion = _evaluate_completion(
                    self.completion_gate,
                    domain_state,
                    state,
                )
                if completion.complete:
                    state.completed = True
                    state.stop_reason = "model_completed"
                    self._save_checkpoint(
                        state,
                        stage="completed",
                        next_round=round_index + 1,
                    )
                    break
                state.verification_rejections += 1
                feedback = _completion_feedback(completion)
                state.final_text = ""
                yield self._record_event(
                    state,
                    "completion_rejected",
                    {"missing": completion.missing, "errors": completion.errors},
                    round_index=round_index,
                )
                yield self._record_event(
                    state,
                    "model_output_reset",
                    {"reason": "completion_verification_rejected"},
                    round_index=round_index,
                )
                if completion.errors and not completion.missing:
                    before_compaction = _message_trace_metrics(state.messages)
                    state.messages = _verification_retry_messages(
                        state.messages,
                        state.tool_results,
                        feedback=feedback,
                        max_chars=self.features.verification_retry_context_chars,
                    )
                    state.verification_repair_pending = True
                    after_compaction = _message_trace_metrics(state.messages)
                    yield self._record_event(
                        state,
                        "verification_context_compacted",
                        {
                            "before_message_count": before_compaction["message_count"],
                            "before_char_count": before_compaction["char_count"],
                            "before_token_count": before_compaction["token_count"],
                            "after_message_count": after_compaction["message_count"],
                            "after_char_count": after_compaction["char_count"],
                            "after_token_count": after_compaction["token_count"],
                            "tools_disabled_for_retry": True,
                        },
                        round_index=round_index,
                    )
                else:
                    state.messages.append({"role": "system", "content": feedback})
                if (
                    state.verification_rejections
                    >= self.features.max_verification_rejections
                ):
                    state.stop_reason = "verification_rejected"
                    break
                state.next_round = round_index + 1
                self._save_checkpoint(
                    state,
                    stage="before_model",
                    next_round=state.next_round,
                )
            else:
                if not state.completed:
                    state.stop_reason = "max_rounds_reached"

            if not state.stop_reason:
                state.stop_reason = "max_rounds_reached"
            if (
                self.features.final_answer_on_stop
                and not state.final_text.strip()
                and len(state.model_turns) < self.features.max_rounds
                and state.stop_reason in {
                    "no_progress",
                    "max_tool_calls_reached",
                }
                and time.monotonic() - started_at
                < self.features.max_elapsed_seconds
            ):
                state.messages.append({
                    "role": "system",
                    "content": (
                        "The bounded work cycle has stopped. Produce the best final answer now "
                        "from the verified tool results already in the conversation. Do not call "
                        "another tool, invent facts, or hide missing evidence."
                    ),
                })
                final_round = len(state.model_turns) + 1
                self.middleware.before_model(state)
                final_input_metrics = _message_trace_metrics(state.messages)
                final_model_started_at = time.monotonic()
                text_parts: list[str] = []
                provider = str(getattr(self.planner, "name", ""))
                model = ""
                for planner_event in self.planner.stream_turn(state.messages, []):
                    provider = planner_event.provider or provider
                    model = planner_event.model or model
                    if planner_event.kind == "reset":
                        text_parts = []
                        yield self._record_event(
                            state,
                            "model_output_reset",
                            {"reason": planner_event.error},
                            round_index=final_round,
                        )
                    elif planner_event.kind == "delta":
                        text_parts.append(planner_event.text)
                        yield self._record_event(
                            state,
                            "model_delta",
                            {"text": planner_event.text},
                            round_index=final_round,
                        )
                    if (
                        time.monotonic() - started_at
                        >= self.features.max_elapsed_seconds
                    ):
                        break
                final_turn = PlannerTurn(
                    text="".join(text_parts),
                    provider=provider,
                    model=model,
                )
                state.model_turns.append(final_turn)
                self.middleware.after_model(state, final_turn)
                yield self._record_event(
                    state,
                    "model_turn",
                    {
                        "round_index": final_round,
                        "provider": final_turn.provider,
                        "model": final_turn.model,
                        "tool_names": [],
                        "tool_calls": [],
                        "text": final_turn.text,
                        "text_char_count": len(final_turn.text),
                        "text_token_count": _count_tokens(final_turn.text),
                        "input_message_count": final_input_metrics["message_count"],
                        "input_char_count": final_input_metrics["char_count"],
                        "input_token_count": final_input_metrics["token_count"],
                        "elapsed_ms": int((time.monotonic() - final_model_started_at) * 1000),
                        "finalization_after_stop": state.stop_reason,
                    },
                    round_index=final_round,
                )
                if (
                    time.monotonic() - started_at
                    >= self.features.max_elapsed_seconds
                ):
                    state.stop_reason = "max_elapsed_time_reached"
                    state.final_text = ""
                    if final_turn.text:
                        yield self._record_event(
                            state,
                            "model_output_reset",
                            {"reason": "max_elapsed_time_reached"},
                            round_index=final_round,
                        )
                    self._save_checkpoint(
                        state,
                        stage="stopped",
                        next_round=final_round + 1,
                    )
                elif final_turn.text.strip():
                    state.final_text = final_turn.text
                    state.messages.append({"role": "assistant", "content": final_turn.text})
                    completion = _evaluate_completion(
                        self.completion_gate,
                        domain_state,
                        state,
                    )
                    if completion.complete:
                        state.completed = True
                    else:
                        state.verification_rejections += 1
                        state.messages.append(
                            {
                                "role": "system",
                                "content": _completion_feedback(completion),
                            }
                        )
                        state.final_text = ""
                        yield self._record_event(
                            state,
                            "completion_rejected",
                            {
                                "missing": completion.missing,
                                "errors": completion.errors,
                                "finalization_after_stop": state.stop_reason,
                            },
                            round_index=final_round,
                        )
                        yield self._record_event(
                            state,
                            "model_output_reset",
                            {"reason": "completion_verification_rejected"},
                            round_index=final_round,
                        )
                    self._save_checkpoint(
                        state,
                        stage="completed" if state.completed else "stopped",
                        next_round=final_round + 1,
                    )
        except Exception as exc:
            self.middleware.on_error(state, exc, None)
            state.stop_reason = "runtime_failed"
            yield self._record_event(
                state,
                "runtime_error",
                {"error_type": type(exc).__name__, "message": str(exc)},
            )
            raise
        finally:
            yield self._record_event(
                state,
                "run_stopped",
                {
                    "stop_reason": state.stop_reason,
                    "completed": state.completed,
                    "elapsed_ms": int((time.monotonic() - started_at) * 1000),
                },
            )
            self.middleware.after_run(state)

        result = RuntimeResult(
            run_id=run_id,
            completed=state.completed,
            stop_reason=state.stop_reason,
            final_text=state.final_text,
            plan=list(state.plan),
            model_turn_count=len(state.model_turns),
            messages=list(state.messages),
            tool_results=list(state.tool_results),
            model_turns=list(state.model_turns),
            events=list(state.events),
            completion=completion,
        )
        yield self._record_event(
            state,
            "runtime_completed",
            {
                "stop_reason": state.stop_reason,
                "completed": state.completed,
                "elapsed_ms": int((time.monotonic() - started_at) * 1000),
            },
            result=result,
        )

    def _save_checkpoint(
        self,
        state: RuntimeState,
        *,
        stage: str,
        next_round: int,
        inflight_tool: dict[str, Any] | None = None,
    ) -> None:
        checkpoint_writer = getattr(self.event_sink, "checkpoint", None)
        if not callable(checkpoint_writer):
            return
        checkpoint_writer(
            state.run_id,
            state.checkpoint(
                stage=stage,
                next_round=next_round,
                inflight_tool=inflight_tool,
            ),
        )

    def _record_event(
        self,
        state: RuntimeState,
        kind: str,
        payload: dict[str, Any],
        *,
        round_index: int = 0,
        result: RuntimeResult | None = None,
    ) -> RuntimeEvent:
        event_payload = dict(payload)
        if round_index > 0:
            event_payload.setdefault("round_index", round_index)
        raw = _event(kind, event_payload)
        state.events.append(raw)
        event = RuntimeEvent(
            run_id=state.run_id,
            kind=kind,
            payload=event_payload,
            created_at=raw["created_at"],
            round_index=round_index,
            result=result,
        )
        self.event_sink.append(event)
        return event


def _message_trace_metrics(messages: Sequence[dict[str, Any]]) -> dict[str, int]:
    serialized = json.dumps(
        list(messages),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return {
        "message_count": len(messages),
        "char_count": len(serialized),
        "token_count": _count_tokens(serialized),
    }


def _count_tokens(value: str) -> int:
    try:
        return get_token_counter().count(value or "")
    except Exception:
        return max(0, len(value or "") // 4)


def _assistant_tool_message(
    text: str,
    calls: Sequence[ToolCallRequest],
) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": text,
        "tool_calls": [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False, sort_keys=True),
                },
            }
            for call in calls
        ],
    }


def _tool_result_message(
    call: ToolCallRequest,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call.call_id,
        "name": call.name,
        "content": json.dumps(result, ensure_ascii=False, sort_keys=True, default=str),
    }


def _call_signature(call: ToolCallRequest) -> str:
    arguments = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=str)
    return f"{call.name}:{arguments}"


def _evaluate_completion(
    gate: CompletionGate,
    domain_state: Any,
    runtime_state: RuntimeState,
) -> CompletionDecision:
    evaluate = gate.evaluate
    try:
        parameters = list(inspect.signature(evaluate).parameters.values())
    except (TypeError, ValueError):
        parameters = []
    supports_runtime_state = any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL
        for parameter in parameters
    ) or len(
        [
            parameter
            for parameter in parameters
            if parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
        ]
    ) >= 2
    if supports_runtime_state:
        return evaluate(domain_state, runtime_state)
    return evaluate(domain_state)


def _completion_feedback(decision: CompletionDecision) -> str:
    missing = ", ".join(decision.missing) or "none"
    errors = "; ".join(decision.errors) or "none"
    return (
        "Runtime verification rejected completion. "
        f"Missing criteria: {missing}. Verification errors: {errors}. "
        "Revise the answer using only verified facts already available. "
        "Call another supplied tool only when evidence is genuinely missing; "
        "do not repeat a completed call or claim completion yet."
    )


def _verification_retry_messages(
    messages: Sequence[dict[str, Any]],
    tool_results: Sequence[dict[str, Any]],
    *,
    feedback: str,
    max_chars: int,
) -> list[dict[str, Any]]:
    system_content = next(
        (
            str(message.get("content") or "")
            for message in messages
            if message.get("role") == "system"
        ),
        "",
    )
    user_content = next(
        (
            str(message.get("content") or "")
            for message in reversed(messages)
            if message.get("role") == "user"
        ),
        "",
    )
    rejected_answer = next(
        (
            str(message.get("content") or "")
            for message in reversed(messages)
            if message.get("role") == "assistant" and not message.get("tool_calls")
        ),
        "",
    )
    repair_instruction = (
        f"{feedback}\n"
        "This is a verification-only repair pass. Do not call tools. Return one concise "
        "replacement answer, remove every unsupported concrete claim, and copy each used "
        "fact_id exactly as [F_xxx]. Do not report a count, person, date, or status unless "
        "the same sentence contains its complete fact reference. Never repeat an unsupported "
        "value merely to explain that it was removed; omit it entirely when no matching fact "
        "is available."
    )

    fixed_chars = len(system_content) + len(user_content) + len(repair_instruction) + 512
    flexible_budget = max(768, max_chars - fixed_chars)
    answer_budget = min(len(rejected_answer), max(512, flexible_budget // 3))
    rejected_preview = rejected_answer[:answer_budget]
    ledger_budget = max(512, flexible_budget - len(rejected_preview))
    ledger = _verification_evidence_ledger(
        tool_results,
        rejected_answer=rejected_answer,
        max_chars=ledger_budget,
    )
    compacted = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
        {
            "role": "system",
            "content": (
                "Verified evidence ledger from this run. Use only these facts when repairing "
                f"the answer:\n{ledger}"
            ),
        },
        {
            "role": "assistant",
            "content": (
                "[Rejected draft for revision; do not repeat unsupported details]\n"
                f"{rejected_preview}"
            ),
        },
        {"role": "system", "content": repair_instruction},
    ]
    while _message_trace_metrics(compacted)["char_count"] > max_chars:
        ledger_message = compacted[2]["content"]
        draft_message = compacted[3]["content"]
        if len(ledger_message) > 768:
            compacted[2]["content"] = ledger_message[: max(768, len(ledger_message) - 512)]
            continue
        if len(draft_message) > 768:
            compacted[3]["content"] = draft_message[: max(768, len(draft_message) - 512)]
            continue
        break
    return compacted


def _verification_evidence_ledger(
    tool_results: Sequence[dict[str, Any]],
    *,
    rejected_answer: str,
    max_chars: int,
) -> str:
    referenced_ids = list(dict.fromkeys(
        re.findall(r"\[(F_[A-Za-z0-9_]+)\]", rejected_answer or "")
    ))
    facts_by_id: dict[str, dict[str, Any]] = {}
    summaries: list[dict[str, str]] = []
    previews: list[dict[str, str]] = []
    for record in tool_results:
        result = dict(record.get("result") or {})
        name = str(record.get("name") or "")
        summary = str(result.get("summary") or "")
        if summary:
            summaries.append({"tool": name, "summary": summary[:240]})
        facts = [
            fact
            for fact in (result.get("facts") or [])
            if isinstance(fact, dict)
        ]
        for fact in facts:
            fact_id = str(fact.get("fact_id") or "")
            if fact_id and fact_id not in facts_by_id:
                facts_by_id[fact_id] = dict(fact)
        if not facts:
            preview = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
            previews.append({"tool": name, "result_preview": preview[:360]})

    referenced_set = set(referenced_ids)
    insertion_order = {
        fact_id: index
        for index, fact_id in enumerate(facts_by_id)
    }
    ordered_ids = sorted(
        facts_by_id,
        key=lambda fact_id: (
            _verification_fact_priority(
                facts_by_id[fact_id],
                rejected_answer=rejected_answer,
                referenced=fact_id in referenced_set,
            ),
            insertion_order[fact_id],
        ),
    )
    payload: dict[str, Any] = {
        "tool_summaries": summaries[:12],
        "facts": [],
        "non_fact_previews": previews[:4],
    }
    while (
        len(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
        > max_chars
    ):
        if payload["non_fact_previews"]:
            payload["non_fact_previews"].pop()
            continue
        if payload["tool_summaries"]:
            payload["tool_summaries"].pop()
            continue
        break
    for fact_id in ordered_ids:
        payload["facts"].append(_compact_verification_fact(facts_by_id[fact_id]))
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        if len(serialized) > max_chars:
            payload["facts"].pop()
            break
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _verification_fact_priority(
    fact: dict[str, Any],
    *,
    rejected_answer: str,
    referenced: bool,
) -> int:
    if referenced:
        return 0
    value = fact.get("value")
    path = str(fact.get("path") or "")
    kind = str(fact.get("kind") or "")
    if not _verification_fact_value_mentioned(value, rejected_answer):
        return 4
    if (
        kind in {"person", "status", "date"}
        or path.endswith(".title")
        or path.endswith(".name")
        or path.endswith(".owner")
        or path.endswith(".status")
        or path.endswith(".length")
    ):
        return 1
    return 2


def _verification_fact_value_mentioned(value: Any, answer: str) -> bool:
    if value is None or isinstance(value, bool):
        return False
    text = str(value).strip()
    if not text:
        return False
    if isinstance(value, (int, float)):
        return re.search(
            rf"(?<![\d.]){re.escape(text)}(?![\d.])",
            answer or "",
        ) is not None
    return len(text) >= 2 and text in (answer or "")


def _compact_verification_fact(fact: dict[str, Any]) -> dict[str, Any]:
    value = fact.get("value")
    compacted: dict[str, Any] = {
        "fact_id": str(fact.get("fact_id") or ""),
        "kind": fact.get("kind"),
        "path": fact.get("path"),
        "value": value,
        "source_refs": list(fact.get("source_refs") or [])[:2],
    }
    if isinstance(value, str) and len(value) > 600:
        compacted["value"] = value[:600]
        compacted["value_truncated"] = True
    return compacted


def _event(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
