from __future__ import annotations

import json
from typing import Any, Iterable, Protocol

from agent.runtime.contracts import PlannerTurn, ToolCallRequest
from agent.runtime.state import RuntimeState


class RuntimeMiddleware(Protocol):
    def before_run(self, state: RuntimeState) -> None:
        ...

    def before_model(self, state: RuntimeState) -> None:
        ...

    def after_model(self, state: RuntimeState, turn: PlannerTurn) -> None:
        ...

    def before_tool(self, state: RuntimeState, call: ToolCallRequest) -> None:
        ...

    def after_tool(
        self,
        state: RuntimeState,
        call: ToolCallRequest,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    def on_error(
        self,
        state: RuntimeState,
        error: Exception,
        call: ToolCallRequest | None,
    ) -> None:
        ...

    def after_run(self, state: RuntimeState) -> None:
        ...


class MiddlewareStack:
    def __init__(self, middleware: Iterable[RuntimeMiddleware] = ()) -> None:
        self._middleware = list(middleware)

    def before_run(self, state: RuntimeState) -> None:
        for item in self._middleware:
            item.before_run(state)

    def before_model(self, state: RuntimeState) -> None:
        for item in self._middleware:
            item.before_model(state)

    def after_model(self, state: RuntimeState, turn: PlannerTurn) -> None:
        for item in reversed(self._middleware):
            item.after_model(state, turn)

    def before_tool(self, state: RuntimeState, call: ToolCallRequest) -> None:
        for item in self._middleware:
            item.before_tool(state, call)

    def after_tool(
        self,
        state: RuntimeState,
        call: ToolCallRequest,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        transformed = result
        for item in reversed(self._middleware):
            transformed = item.after_tool(state, call, transformed)
        return transformed

    def on_error(
        self,
        state: RuntimeState,
        error: Exception,
        call: ToolCallRequest | None,
    ) -> None:
        for item in reversed(self._middleware):
            item.on_error(state, error, call)

    def after_run(self, state: RuntimeState) -> None:
        for item in reversed(self._middleware):
            item.after_run(state)


class BaseRuntimeMiddleware:
    def before_run(self, state: RuntimeState) -> None:
        return None

    def before_model(self, state: RuntimeState) -> None:
        return None

    def after_model(self, state: RuntimeState, turn: PlannerTurn) -> None:
        return None

    def before_tool(self, state: RuntimeState, call: ToolCallRequest) -> None:
        return None

    def after_tool(
        self,
        state: RuntimeState,
        call: ToolCallRequest,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return result

    def on_error(
        self,
        state: RuntimeState,
        error: Exception,
        call: ToolCallRequest | None,
    ) -> None:
        return None

    def after_run(self, state: RuntimeState) -> None:
        return None


class ToolResultBudgetMiddleware(BaseRuntimeMiddleware):
    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars

    def after_tool(
        self,
        state: RuntimeState,
        call: ToolCallRequest,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        raw = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
        if len(raw) <= self.max_chars:
            return result
        return {
            "ok": bool(result.get("ok", True)),
            "summary": str(result.get("summary") or "tool result compacted"),
            "context_compacted": True,
            "original_char_count": len(raw),
            "preview": raw[: self.max_chars],
        }
