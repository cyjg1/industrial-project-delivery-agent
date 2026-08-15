from __future__ import annotations

from typing import Any, Callable, Iterator, Sequence

from agent.access_policy import AccessContext, User
from agent.runtime.contracts import (
    CompletionDecision,
    CompletionGate,
    RuntimeEvent,
    RuntimeFeatures,
)
from agent.runtime.event_sink import RuntimeEventSink
from agent.runtime.factory import create_agent_runtime
from agent.runtime.middleware import BaseRuntimeMiddleware
from agent.runtime.planners import FunctionCallingPlanner
from agent.tool_registry import ToolRegistry


class _NaturalAnswerGate:
    def evaluate(
        self,
        domain_state: Any,
        runtime_state: Any | None = None,
    ) -> CompletionDecision:
        return CompletionDecision(complete=True, reason="model returned a final answer")


class _ConversationToolResultMiddleware(BaseRuntimeMiddleware):
    def __init__(
        self,
        transform: Callable[[dict[str, Any], int], dict[str, Any]],
        budget_for: Callable[[str], int],
    ) -> None:
        self.transform = transform
        self.budget_for = budget_for

    def after_tool(self, state: Any, call: Any, result: dict[str, Any]) -> dict[str, Any]:
        return self.transform(result, self.budget_for(call.name))


class ConversationLoopService:
    """Conversation profile for the single shared model-tool runtime."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        provider: Any,
        actor: User,
        access_context: AccessContext,
        project_id: str,
        features: RuntimeFeatures | None = None,
        event_sink: RuntimeEventSink | None = None,
        model_tool_result: Callable[[dict[str, Any], int], dict[str, Any]] | None = None,
        completion_gate: CompletionGate | None = None,
    ) -> None:
        self.registry = registry
        self.provider = provider
        self.actor = actor
        self.access_context = access_context
        self.project_id = project_id
        self.features = features or RuntimeFeatures()
        self.event_sink = event_sink
        self.model_tool_result = model_tool_result or (
            lambda result, _budget: result
        )
        self.completion_gate = completion_gate or _NaturalAnswerGate()

    def stream(
        self,
        *,
        run_id: str,
        messages: Sequence[dict[str, Any]],
        resume_checkpoint: dict[str, Any] | None = None,
    ) -> Iterator[RuntimeEvent]:
        schemas = self.registry.openai_tools(
            self.actor,
            project_id=self.project_id,
            ctx=self.access_context,
        )

        def invoke(call: Any) -> dict[str, Any]:
            arguments = dict(call.arguments or {})
            arguments.pop("reason", None)
            return self.registry.call(
                call.name,
                self.actor,
                self.access_context,
                arguments,
                project_id=self.project_id,
            )

        runtime = create_agent_runtime(
            planner=FunctionCallingPlanner(self.provider),
            tools=schemas,
            invoke_tool=invoke,
            completion_gate=self.completion_gate,
            features=self.features,
            middleware=[
                _ConversationToolResultMiddleware(
                    self.model_tool_result,
                    self.registry.result_budget,
                )
            ],
            event_sink=self.event_sink,
        )
        yield from runtime.stream(
            run_id=run_id,
            initial_messages=messages,
            domain_state={},
            resume_checkpoint=resume_checkpoint,
        )
