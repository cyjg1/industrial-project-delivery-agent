from __future__ import annotations

from typing import Iterable, Sequence

from agent.runtime.contracts import (
    CompletionGate,
    RuntimeFeatures,
    RuntimePlanner,
    ToolInvoker,
)
from agent.runtime.executor import BoundedAgentRuntime
from agent.runtime.event_sink import RuntimeEventSink
from agent.runtime.middleware import (
    MiddlewareStack,
    RuntimeMiddleware,
    ToolResultBudgetMiddleware,
)


def create_agent_runtime(
    *,
    planner: RuntimePlanner,
    tools: Sequence[dict],
    invoke_tool: ToolInvoker,
    completion_gate: CompletionGate,
    features: RuntimeFeatures | None = None,
    middleware: Iterable[RuntimeMiddleware] = (),
    event_sink: RuntimeEventSink | None = None,
) -> BoundedAgentRuntime:
    """Create a runtime from explicit dependencies; this function never reads global config."""
    active_features = features or RuntimeFeatures()
    stack = MiddlewareStack(
        [ToolResultBudgetMiddleware(active_features.max_tool_result_chars), *middleware]
    )
    return BoundedAgentRuntime(
        planner=planner,
        tools=tools,
        invoke_tool=invoke_tool,
        completion_gate=completion_gate,
        features=active_features,
        middleware=stack,
        event_sink=event_sink,
    )
