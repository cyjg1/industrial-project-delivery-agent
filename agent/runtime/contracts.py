from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator, Protocol, Sequence


class ToolSurface(str, Enum):
    CONVERSATION = "conversation"
    INSPECTION = "inspection"
    MCP = "mcp"


@dataclass(frozen=True)
class RuntimeFeatures:
    max_rounds: int = 8
    duplicate_call_limit: int = 2
    max_tool_calls: int = 24
    max_elapsed_seconds: float = 120.0
    max_verification_rejections: int = 2
    max_tool_result_chars: int = 2048
    verification_retry_context_chars: int = 9000
    final_answer_on_stop: bool = False

    def __post_init__(self) -> None:
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        if self.duplicate_call_limit < 2:
            raise ValueError("duplicate_call_limit must be at least 2")
        if self.max_tool_calls < 1:
            raise ValueError("max_tool_calls must be positive")
        if self.max_elapsed_seconds <= 0:
            raise ValueError("max_elapsed_seconds must be positive")
        if self.max_verification_rejections < 1:
            raise ValueError("max_verification_rejections must be positive")
        if self.max_tool_result_chars < 256:
            raise ValueError("max_tool_result_chars must be at least 256")
        if self.verification_retry_context_chars < 4096:
            raise ValueError("verification_retry_context_chars must be at least 4096")


@dataclass(frozen=True)
class ToolCallRequest:
    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class PlannerTurn:
    text: str = ""
    tool_calls: tuple[ToolCallRequest, ...] = ()
    provider: str = ""
    model: str = ""


@dataclass(frozen=True)
class PlannerStreamEvent:
    kind: str
    text: str = ""
    tool_calls: tuple[ToolCallRequest, ...] = ()
    provider: str = ""
    model: str = ""
    error: str = ""


@dataclass(frozen=True)
class CompletionDecision:
    complete: bool
    reason: str
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SelectedToolStep:
    step_id: str
    tool_name: str
    reason: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeResult:
    run_id: str
    completed: bool
    stop_reason: str
    final_text: str
    plan: list[SelectedToolStep]
    model_turn_count: int
    messages: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    model_turns: list[PlannerTurn]
    events: list[dict[str, Any]]
    completion: CompletionDecision


@dataclass
class RuntimeEvent:
    run_id: str
    kind: str
    payload: dict[str, Any]
    created_at: str
    round_index: int = 0
    result: RuntimeResult | None = None


class RuntimePlanner(Protocol):
    name: str

    def stream_turn(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> Iterator[PlannerStreamEvent]:
        ...

    def next_turn(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> PlannerTurn:
        ...


class CompletionGate(Protocol):
    def evaluate(
        self,
        domain_state: Any,
        runtime_state: Any | None = None,
    ) -> CompletionDecision:
        ...


class ToolInvoker(Protocol):
    def __call__(self, call: ToolCallRequest) -> dict[str, Any]:
        ...
