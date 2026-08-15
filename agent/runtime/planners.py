from __future__ import annotations

from typing import Any, Iterator, Sequence

from agent.llm_provider import LLMProvider, LLMStreamInterruptedError
from agent.runtime.contracts import PlannerStreamEvent, PlannerTurn, ToolCallRequest


class FunctionCallingPlanner:
    def __init__(self, provider: LLMProvider, *, partial_retry_attempts: int = 2) -> None:
        self.provider = provider
        self.name = str(getattr(provider, "name", "model"))
        self.partial_retry_attempts = max(1, int(partial_retry_attempts))

    def stream_turn(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> Iterator[PlannerStreamEvent]:
        for attempt_index in range(self.partial_retry_attempts):
            try:
                for event in self.provider.stream_chat(list(messages), tools=list(tools)):
                    kind = str(_value(event, "kind") or "")
                    text = str(_value(event, "text") or "")
                    if kind == "delta" and text:
                        yield PlannerStreamEvent(
                            kind="delta",
                            text=text,
                            provider=self.name,
                            model=str(getattr(self.provider, "model", "")),
                        )
                    elif kind == "tool_calls":
                        yield PlannerStreamEvent(
                            kind="tool_calls",
                            tool_calls=tuple(
                                _tool_call(call)
                                for call in (_value(event, "tool_calls") or [])
                            ),
                            provider=self.name,
                            model=str(getattr(self.provider, "model", "")),
                        )
                return
            except LLMStreamInterruptedError as exc:
                if not exc.partial or attempt_index + 1 >= self.partial_retry_attempts:
                    raise
                yield PlannerStreamEvent(
                    kind="reset",
                    provider=self.name,
                    model=str(getattr(self.provider, "model", "")),
                    error=str(exc),
                )

    def next_turn(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> PlannerTurn:
        text_parts: list[str] = []
        calls: list[ToolCallRequest] = []
        provider = self.name
        model = str(getattr(self.provider, "model", ""))
        for event in self.stream_turn(messages, tools):
            provider = event.provider or provider
            model = event.model or model
            if event.kind == "reset":
                text_parts = []
                calls = []
            elif event.kind == "delta" and event.text:
                text_parts.append(event.text)
            elif event.kind == "tool_calls":
                calls.extend(event.tool_calls)
        return PlannerTurn(
            text="".join(text_parts),
            tool_calls=tuple(calls),
            provider=provider,
            model=model,
        )


def _value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _tool_call(value: Any) -> ToolCallRequest:
    arguments = dict(_value(value, "arguments") or {})
    reason = str(_value(value, "reason") or arguments.pop("reason", "") or "")
    return ToolCallRequest(
        call_id=str(_value(value, "call_id") or _value(value, "id") or "call_unknown"),
        name=str(_value(value, "name") or _value(value, "tool_name") or ""),
        arguments=arguments,
        reason=reason,
    )
