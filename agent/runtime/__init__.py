from agent.runtime.contracts import (
    CompletionDecision,
    RuntimeFeatures,
    RuntimeResult,
    SelectedToolStep,
    ToolCallRequest,
    ToolSurface,
)
from agent.runtime.factory import create_agent_runtime
from agent.runtime.middleware import BaseRuntimeMiddleware, RuntimeMiddleware
from agent.runtime.planners import FunctionCallingPlanner

__all__ = [
    "AgentRuntime",
    "BaseRuntimeMiddleware",
    "CompletionDecision",
    "FunctionCallingPlanner",
    "RuntimeFeatures",
    "RuntimeMiddleware",
    "RuntimeResult",
    "SelectedToolStep",
    "ToolCallRequest",
    "ToolSurface",
    "create_agent_runtime",
]


def __getattr__(name: str):
    if name == "AgentRuntime":
        from agent.runtime.facade import AgentRuntime

        return AgentRuntime
    raise AttributeError(name)
