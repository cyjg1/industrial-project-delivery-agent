from __future__ import annotations

from typing import Callable

from agent.access_policy import AccessContext, User
from agent.llm_provider import LLMProvider, get_provider
from agent.inspection_agent.service import AgentRuntimeConfigurationError, InspectionAgentService
from agent.inspection_agent.state import InspectionTrigger
from agent.runtime.contracts import RuntimeFeatures
from agent.runtime.planners import FunctionCallingPlanner
from agent.schemas import AgentRun, MilestonePlan
from agent.tool_registry import ToolRegistry
from store.sqlite_store import ProjectSQLiteStore


class AgentRuntime:
    """Application composition boundary for conversation and inspection profiles."""

    def __init__(
        self,
        tool_registry: ToolRegistry | None,
        store: ProjectSQLiteStore,
        *,
        provider_factory: Callable[..., LLMProvider] = get_provider,
        features: RuntimeFeatures | None = None,
    ):
        self.tool_registry = tool_registry
        self.store = store
        self.provider_factory = provider_factory
        self.features = features or RuntimeFeatures()

    def provider(self, role: str, *, model: str = "") -> LLMProvider:
        if model:
            return self.provider_factory(role, model)
        return self.provider_factory(role)

    def run_inspection(
        self,
        milestone: MilestonePlan,
        *,
        actor: User,
        access_context: AccessContext,
        project_id: str,
        trigger: InspectionTrigger | None = None,
    ) -> AgentRun:
        if self.tool_registry is None:
            raise ValueError("AgentRuntime requires an explicit ToolRegistry")
        try:
            provider = self.provider("planning")
        except RuntimeError as exc:
            raise AgentRuntimeConfigurationError(str(exc)) from exc
        service = InspectionAgentService(
            store=self.store,
            registry=self.tool_registry,
            planner=FunctionCallingPlanner(provider),
            runtime_kind="model_function_calling_inspection",
            features=self.features,
            model_calls_are_real=True,
        )
        return service.run(
            milestone,
            actor=actor,
            access_context=access_context,
            project_id=project_id,
            trigger=trigger,
        )
