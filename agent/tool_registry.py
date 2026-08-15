from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.access_policy import AccessContext, ROLE_RANK, User, visible, visible_filter
from agent.fact_ledger import build_fact_ledger


ToolHandler = Callable[[User, AccessContext, dict[str, Any]], dict[str, Any]]

DATA_ROW_KEYS = ("items", "rows", "tasks")
CONVERSATION_SURFACE = "conversation"
INSPECTION_SURFACE = "inspection"
MCP_SURFACE = "mcp"
ACCESS_TAG_FIELDS = frozenset(
    {"org_id", "project_id", "topic_id", "author_id", "sensitivity"}
)


class ToolEffect(str, Enum):
    READ = "read"
    WRITE = "write"
    EXTERNAL = "external"


class _StrictToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ReasonInput(_StrictToolInput):
    reason: str = Field(
        default="",
        description="Why this tool is the next useful action for the current goal.",
    )


class _SearchMemoryInput(_ReasonInput):
    query: str = Field(
        min_length=1,
        description="The project-memory query in the user's words.",
    )
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional status, type, or category filters.",
    )


class _SearchQueryInput(_ReasonInput):
    query: str = Field(
        min_length=1,
        description="The exact project question or evidence phrase to retrieve.",
    )


class _GetTasksInput(_ReasonInput):
    status: str | None = Field(default=None, description="Optional task status.")
    owner: str | None = Field(
        default=None,
        description="Optional owner from conversation history or tool results.",
    )
    overdue: bool | None = Field(
        default=None,
        description="Return only overdue tasks when true.",
    )


class _GetPersonInput(_ReasonInput):
    name: str = Field(
        default="",
        description="A person name from history or visible tool results.",
    )


class _IngestFileInput(_ReasonInput):
    path: str = Field(
        default="",
        description="An attached filename; never invent a local filesystem path.",
    )


class _DeliverableFileInput(_StrictToolInput):
    filename: str = Field(min_length=1, description="Attached filename.")
    artifact_role: Literal["formal", "process", "evidence", "reference"]


class _ArchiveDeliverableInput(_ReasonInput):
    deliverable_id: str = Field(
        min_length=1,
        description="Visible ID returned by get_deliverables.",
    )
    files: list[_DeliverableFileInput] = Field(
        min_length=1,
        description="Attached files and their generic artifact roles.",
    )
    note: str = Field(default="", description="Optional submission note.")


class _DraftInput(_ReasonInput):
    kind: str = Field(default="催办消息", description="Draft type.")
    target: str = Field(default="", description="Optional recipient or owner.")
    topic: str = Field(default="", description="Draft topic.")


class _GenerateDailyBriefInput(_ReasonInput):
    push_feishu: bool = Field(
        default=False,
        description="Push to Feishu after generation when explicitly requested.",
    )


class _CandidateEvidenceInput(_StrictToolInput):
    source_doc_id: str = Field(
        min_length=1,
        description="Visible source ID returned by a project evidence tool.",
    )
    source_kind: Literal["curated_source", "raw_source"] = "curated_source"
    locator: str = Field(min_length=1, description="Exact source locator returned by the tool.")
    quote: str = Field(min_length=1, max_length=300, description="Exact source quote.")


class _CandidateProposalInput(_StrictToolInput):
    category: str = Field(
        min_length=1,
        description="people, things, methods, task, or issue.",
    )
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[_CandidateEvidenceInput] = Field(min_length=1, max_length=8)
    owner_candidates: list[str] = Field(default_factory=list, max_length=12)
    matter_type: str = ""
    facet_types: list[str] = Field(default_factory=list, max_length=8)
    due_date: str = Field(default="", pattern=r"^$|^\d{4}-\d{2}-\d{2}$")
    deliverable: str = Field(default="", max_length=500)
    acceptance_criteria: str = Field(default="", max_length=800)
    business_goal: str = Field(default="", max_length=500)
    principles: list[str] = Field(default_factory=list, max_length=12)
    reasoning_chain: list[str] = Field(default_factory=list, max_length=12)
    applicable_scope: str = Field(default="", max_length=300)
    observed_fields: list[str] = Field(default_factory=list, max_length=20)
    proposed_fields: list[str] = Field(default_factory=list, max_length=20)
    inference_basis: list[str] = Field(default_factory=list, max_length=12)
    inference_confidence: Literal["", "low", "medium", "high"] = ""
    proposed_sensitivity: Literal["", "l2", "l3", "l4"] = ""
    sensitivity_reason: str = Field(default="", max_length=500)


class _ProposeCandidatesInput(_ReasonInput):
    items: list[_CandidateProposalInput] = Field(
        default_factory=list,
        description=(
            "Evidence-backed observed facts and explicitly labelled model proposals. "
            "All writes remain candidate and require human confirmation."
        ),
    )


class _ToolResultEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    summary: str
    ok: bool = True


class _ItemsToolResult(_ToolResultEnvelope):
    items: list[dict[str, Any]] = Field(default_factory=list)


class _SkillSearchResult(_ItemsToolResult):
    external_items: list[dict[str, Any]] = Field(default_factory=list)
    policy_items: list[dict[str, Any]] = Field(default_factory=list)
    cognition_items: list[dict[str, Any]] = Field(default_factory=list)
    trace_items: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_trace: dict[str, Any] = Field(default_factory=dict)
    external_error: str = ""


class _MemorySearchResult(_ItemsToolResult):
    degraded: bool = False
    embedding_error: str = ""
    retrieval_mode: Literal["fts", "fts_semantic_union"] = "fts"


class _ProjectHealthResult(_ToolResultEnvelope):
    health: dict[str, Any] = Field(default_factory=dict)


class _MilestoneResult(_ToolResultEnvelope):
    milestone: dict[str, Any] = Field(default_factory=dict)


class _PersonResult(_ToolResultEnvelope):
    people: list[dict[str, Any]] = Field(default_factory=list)
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    methods: list[dict[str, Any]] = Field(default_factory=list)


class _IngestResult(_ToolResultEnvelope):
    jobs: list[dict[str, Any]] = Field(default_factory=list)
    file_count: int = 0
    candidate_count: int = 0


class _ArchiveDeliverableResult(_ToolResultEnvelope):
    version: dict[str, Any] | None = None


class _DraftResult(_ToolResultEnvelope):
    draft: str = ""


class _DailyBriefResult(_ToolResultEnvelope):
    brief: dict[str, Any] = Field(default_factory=dict)


class _InspectionRequestResult(_ToolResultEnvelope):
    job_id: str = ""
    status: str = ""
    trigger_type: str = ""


class _CandidateProposalResult(_ToolResultEnvelope):
    candidate_count: int = 0


@dataclass
class ToolSpec:
    name: str
    description: str
    handler: ToolHandler
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    required_role: str = "exec"
    surfaces: frozenset[str] = frozenset({CONVERSATION_SURFACE})
    access_filter_keys: tuple[str, ...] = DATA_ROW_KEYS
    input_model: type[BaseModel] | None = None
    output_model: type[BaseModel] | None = None
    effect: ToolEffect = ToolEffect.READ
    idempotent: bool | None = None
    parallel_safe: bool | None = None
    result_budget_chars: int = 2048

    def __post_init__(self) -> None:
        if self.input_model is not None:
            self.parameters = self.input_model.model_json_schema()
        self.effect = ToolEffect(self.effect)
        if self.idempotent is None:
            self.idempotent = self.effect == ToolEffect.READ
        if self.parallel_safe is None:
            self.parallel_safe = self.effect == ToolEffect.READ
        if self.result_budget_chars < 256:
            raise ValueError("result_budget_chars must be at least 256")

    def parameter_schema(self) -> dict[str, Any]:
        return deepcopy(self.parameters)


@dataclass
class ToolRegistry:
    default_context: AccessContext = field(default_factory=AccessContext)

    def __post_init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def tool_names(self) -> list[str]:
        return list(self._specs)

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def result_budget(self, name: str) -> int:
        spec = self._specs.get(name)
        if spec is None:
            return 2048
        return spec.result_budget_chars

    def external_specs(
        self,
        actor: User,
        *,
        project_id: str | None = None,
        ctx: AccessContext | None = None,
    ) -> list[ToolSpec]:
        active_context = ctx or self.default_context
        return [
            spec
            for spec in self._specs.values()
            if MCP_SURFACE in spec.surfaces
            and self._allowed(actor, spec, active_context, project_id=project_id)
        ]

    def openai_tools(
        self,
        actor: User,
        *,
        project_id: str | None = None,
        ctx: AccessContext | None = None,
    ) -> list[dict[str, Any]]:
        return self.schemas(
            actor,
            surface=CONVERSATION_SURFACE,
            project_id=project_id,
            ctx=ctx,
        )

    def schemas(
        self,
        actor: User,
        *,
        surface: str | Any,
        project_id: str | None = None,
        ctx: AccessContext | None = None,
    ) -> list[dict[str, Any]]:
        active_surface = str(getattr(surface, "value", surface))
        active_context = ctx or self.default_context
        return [
            _function_schema(spec, include_reason=active_surface == INSPECTION_SURFACE)
            for spec in self._specs.values()
            if active_surface in spec.surfaces and self._allowed(
                actor,
                spec,
                active_context,
                project_id=project_id,
            )
        ]

    def call(
        self,
        name: str,
        actor: User,
        ctx: AccessContext,
        args: dict[str, Any],
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        spec = self._specs.get(name)
        raw_args = dict(args or {})
        requested_project_id = str(raw_args.get("project_id") or "").strip() or None
        active_project_id = project_id or requested_project_id
        if spec is None or not self._allowed(actor, spec, ctx, project_id=active_project_id):
            return {
                "ok": False,
                "summary": "工具不可用或权限不足。",
                "error": {"code": "forbidden", "message": "工具不可用或权限不足。"},
            }
        if project_id and requested_project_id and requested_project_id != project_id:
            return _tool_error(
                "scope_conflict",
                "工具参数中的 project_id 与当前请求作用域不一致。",
            )
        try:
            validated_args = _validate_tool_arguments(spec, raw_args)
        except (ValidationError, ValueError) as exc:
            return _tool_error(
                "invalid_arguments",
                "工具参数不符合注册契约。",
                details=_validation_details(exc),
            )
        try:
            raw_result = dict(spec.handler(actor, ctx, validated_args) or {})
        except Exception:
            raise
        try:
            result = _validate_tool_result(spec, raw_result)
        except ValidationError as exc:
            return _tool_error(
                "invalid_tool_result",
                "工具返回值不符合注册契约。",
                details=_validation_details(exc),
            )
        result.setdefault("ok", True)
        try:
            filtered_result = _filter_result(
                actor,
                ctx,
                result,
                spec.access_filter_keys,
            )
        except _AccessContractError as exc:
            return _tool_error(
                "invalid_access_contract",
                "工具返回的数据行缺少完整访问标签。",
                details=[{"path": exc.path, "message": exc.message}],
            )
        filtered_result["facts"] = build_fact_ledger(name, filtered_result)
        filtered_result["fact_count"] = len(filtered_result["facts"])
        return filtered_result

    def _allowed(
        self,
        actor: User,
        spec: ToolSpec,
        ctx: AccessContext,
        *,
        project_id: str | None = None,
    ) -> bool:
        return ROLE_RANK.get(_actor_role(actor, ctx, project_id=project_id), -1) >= ROLE_RANK.get(
            spec.required_role,
            999,
        )


def access_filtered(handler: ToolHandler) -> ToolHandler:
    def wrapped(actor: User, ctx: AccessContext, args: dict[str, Any]) -> dict[str, Any]:
        return _filter_result(actor, ctx, dict(handler(actor, ctx, args) or {}), DATA_ROW_KEYS)

    return wrapped


def _function_schema(spec: ToolSpec, *, include_reason: bool) -> dict[str, Any]:
    parameters = spec.parameter_schema()
    if include_reason:
        parameters.setdefault("type", "object")
        properties = parameters.setdefault("properties", {})
        properties.setdefault(
            "reason",
            {
                "type": "string",
                "description": "Why this tool is the next useful action for the current goal.",
            },
        )
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": parameters,
        },
    }


def conversation_tool_specs(handlers: Any) -> list[ToolSpec]:
    specs = [
        ToolSpec(
            name="search_memory",
            description=(
                "Use when the user asks about project facts, past meetings, decisions, risks, "
                "issues, or retrieval observability. Besides visible memory items, the result "
                "reports retrieval_mode, degraded/degradation_reasons, FTS and semantic candidate "
                "counts, union/thread-collapse/returned counts, and embedding index status."
            ),
            input_model=_SearchMemoryInput,
            output_model=_MemorySearchResult,
            required_role="exec",
            handler=lambda actor, ctx, args: handlers.search_memory(
                query=args.get("query", ""),
                filters=args.get("filters") or {},
            ),
        ),
        ToolSpec(
            name="search_source_evidence",
            description="Use when the user needs exact wording or detailed evidence from archived meeting minutes or transcripts after project-memory summaries are insufficient.",
            input_model=_SearchQueryInput,
            output_model=_ItemsToolResult,
            required_role="exec",
            access_filter_keys=("items",),
            handler=lambda actor, ctx, args: handlers.search_source_evidence(
                query=args.get("query", ""),
            ),
        ),
        ToolSpec(
            name="search_methods",
            description="Use before answering how-to, method, suggestion, or handling-strategy questions to check confirmed reusable methods.",
            input_model=_SearchQueryInput,
            output_model=_ItemsToolResult,
            required_role="exec",
            handler=lambda actor, ctx, args: handlers.search_methods(query=args.get("query", "")),
        ),
        ToolSpec(
            name="search_project_skills",
            description=(
                "Use for complex how-to or execution questions. Retrieval is governed and hierarchical: "
                "human-published Project Skills first; approved execution policies/environment cognition "
                "only when no Project Skill matches; high-value observable traces only as a final fallback. "
                "Candidates are never returned as active instructions, and no result grants permission."
            ),
            input_model=_SearchQueryInput,
            output_model=_SkillSearchResult,
            required_role="exec",
            access_filter_keys=(
                "items",
                "external_items",
                "policy_items",
                "cognition_items",
                "trace_items",
            ),
            result_budget_chars=6144,
            handler=lambda actor, ctx, args: handlers.search_project_skills(query=args.get("query", "")),
        ),
        ToolSpec(
            name="get_tasks",
            description="Use for task-pool questions: overdue work, due work, owners, status, deliverables, or who should be chased today.",
            input_model=_GetTasksInput,
            output_model=_ItemsToolResult,
            required_role="exec",
            handler=lambda actor, ctx, args: handlers.get_tasks(
                status=args.get("status"),
                owner=args.get("owner"),
                overdue=args.get("overdue"),
            ),
        ),
        ToolSpec(
            name="get_project_health",
            description="Use for current project progress, overall risk, exact task counts, overdue distribution, missing owners or dates, and milestone date calculations; prefer this before explaining project health.",
            input_model=_ReasonInput,
            output_model=_ProjectHealthResult,
            required_role="exec",
            access_filter_keys=(),
            handler=lambda actor, ctx, args: handlers.get_project_health(),
        ),
        ToolSpec(
            name="get_milestone_status",
            description="Use for milestone progress, date risk, back-schedule, material coverage, or milestone adjustment questions.",
            input_model=_ReasonInput,
            output_model=_MilestoneResult,
            required_role="exec",
            access_filter_keys=(),
            handler=lambda actor, ctx, args: handlers.get_milestone_status(),
        ),
        ToolSpec(
            name="get_person",
            description="Use for a person's responsibilities or current work; pass a name only if it came from history or tool results, otherwise pass an empty string.",
            input_model=_GetPersonInput,
            output_model=_PersonResult,
            required_role="exec",
            access_filter_keys=("people", "tasks", "issues", "methods"),
            handler=lambda actor, ctx, args: handlers.get_person(name=args.get("name", "")),
        ),
        ToolSpec(
            name="ingest_file",
            description="Use when the user attached one or more project files; queue every attachment for the matching meeting, three-list, or work-log importer and do not claim asynchronous processing is already complete.",
            input_model=_IngestFileInput,
            output_model=_IngestResult,
            effect=ToolEffect.WRITE,
            idempotent=True,
            parallel_safe=False,
            required_role="exec",
            access_filter_keys=(),
            handler=lambda actor, ctx, args: handlers.ingest_file(path=args.get("path", "")),
        ),
        ToolSpec(
            name="get_deliverables",
            description="Use before answering about milestone deliverables, expected outputs, submitted versions, acceptance criteria, or when an attached file may need a deliverable destination.",
            input_model=_ReasonInput,
            output_model=_ItemsToolResult,
            required_role="exec",
            access_filter_keys=("items",),
            handler=lambda actor, ctx, args: handlers.get_deliverables(),
        ),
        ToolSpec(
            name="archive_deliverable",
            description="Use after selecting a visible deliverable definition to archive attached files as one immutable version; never invent a deliverable_id or filename.",
            input_model=_ArchiveDeliverableInput,
            output_model=_ArchiveDeliverableResult,
            effect=ToolEffect.WRITE,
            idempotent=False,
            parallel_safe=False,
            required_role="exec",
            access_filter_keys=(),
            handler=lambda actor, ctx, args: handlers.archive_deliverable(
                deliverable_id=args.get("deliverable_id", ""),
                files=args.get("files") or [],
                note=args.get("note", ""),
            ),
        ),
        ToolSpec(
            name="draft",
            description="Use when the user asks to draft a reminder, agenda, weekly note, follow-up message, or other text for human review.",
            input_model=_DraftInput,
            output_model=_DraftResult,
            required_role="exec",
            access_filter_keys=(),
            handler=lambda actor, ctx, args: handlers.draft(
                kind=args.get("kind", "催办消息"),
                target=args.get("target", ""),
                topic=args.get("topic", ""),
            ),
        ),
        ToolSpec(
            name="generate_daily_brief",
            description="Use when the user asks to generate, refresh, or push the project manager daily brief.",
            input_model=_GenerateDailyBriefInput,
            output_model=_DailyBriefResult,
            effect=ToolEffect.WRITE,
            idempotent=False,
            parallel_safe=False,
            required_role="pmo",
            access_filter_keys=(),
            handler=lambda actor, ctx, args: handlers.generate_daily_brief(push_feishu=bool(args.get("push_feishu", False))),
        ),
        ToolSpec(
            name="request_project_inspection",
            description="Use when a PM explicitly asks the agent to inspect recent project changes and derive evidence-backed candidate things or methods; this queues a proactive inspection and does not run a nested agent loop.",
            input_model=_ReasonInput,
            output_model=_InspectionRequestResult,
            effect=ToolEffect.WRITE,
            idempotent=True,
            parallel_safe=False,
            required_role="pmo",
            access_filter_keys=(),
            handler=lambda actor, ctx, args: handlers.request_project_inspection(
                reason=str(args.get("reason") or ""),
            ),
        ),
        ToolSpec(
            name="propose_candidates",
            description=(
                "Only when the user explicitly asks to save or sediment a conclusion, save "
                "evidence-backed observations or model proposals for human confirmation. "
                "Do not call this tool for a read-only question about existing method status. "
                "Use category=methods to synthesize a reusable methodology across multiple visible "
                "meeting sources. Separate observed_fields from proposed_fields and provide "
                "inference_basis/confidence for every proposal. This tool never confirms or publishes."
            ),
            input_model=_ProposeCandidatesInput,
            output_model=_CandidateProposalResult,
            effect=ToolEffect.WRITE,
            idempotent=False,
            parallel_safe=False,
            required_role="exec",
            access_filter_keys=(),
            handler=lambda actor, ctx, args: handlers.propose_candidates(items=args.get("items") or []),
        ),
    ]
    return specs


def register_specs(registry: ToolRegistry, specs: Iterable[ToolSpec]) -> ToolRegistry:
    for spec in specs:
        registry.register(spec)
    return registry


def _actor_role(actor: User, ctx: AccessContext, *, project_id: str | None = None) -> str:
    if project_id:
        return ctx.role_of(actor, project_id)
    best = "viewer"
    for (user_id, _project_id), role in ctx.project_roles.items():
        if user_id == actor.id and ROLE_RANK.get(role, -1) > ROLE_RANK[best]:
            best = role
    return best


def _validate_tool_arguments(
    spec: ToolSpec,
    args: dict[str, Any],
) -> dict[str, Any]:
    if spec.input_model is not None:
        return spec.input_model.model_validate(args).model_dump(mode="python")
    validator = Draft202012Validator(spec.parameters or {"type": "object"})
    errors = sorted(
        validator.iter_errors(args),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        messages = [
            f"{'.'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}"
            for error in errors
        ]
        raise ValueError("; ".join(messages))
    return dict(args)


def _validate_tool_result(
    spec: ToolSpec,
    result: dict[str, Any],
) -> dict[str, Any]:
    if spec.output_model is None:
        return dict(result)
    return spec.output_model.model_validate(result).model_dump(mode="python")


def _validation_details(exc: Exception) -> list[dict[str, str]]:
    if isinstance(exc, ValidationError):
        return [
            {
                "path": ".".join(str(part) for part in error.get("loc", ())) or "$",
                "message": str(error.get("msg") or "invalid value"),
            }
            for error in exc.errors(include_input=False, include_url=False)
        ]
    return [{"path": "$", "message": str(exc)}]


def _tool_error(
    code: str,
    message: str,
    *,
    details: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {
        "ok": False,
        "summary": message,
        "error": error,
    }


@dataclass(frozen=True)
class _AccessContractError(Exception):
    path: str
    message: str


_FILTERED = object()
_ROW_IDENTITY_FIELDS = frozenset({"id", "item_id", "work_item_id", "record_id"})


def _filter_result(actor: User, ctx: AccessContext, result: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    strict_paths = {f"$.{key}" for key in keys}
    filtered_stats: dict[str, dict[str, int]] = {}
    filtered_result, _ = _filter_access_tree(
        actor,
        ctx,
        result,
        path="$",
        strict_paths=strict_paths,
        filtered_stats=filtered_stats,
    )
    if filtered_result is _FILTERED or not isinstance(filtered_result, dict):
        raise _AccessContractError("$", "A tool result cannot be hidden as one scoped row")
    public_stats: dict[str, dict[str, int]] = {}
    named_returned = 0
    for path, counts in filtered_stats.items():
        if path in strict_paths:
            key = path.removeprefix("$.")
            public_stats[key] = {"returned": counts["returned"]}
            named_returned += counts["returned"]
        else:
            public_stats[path] = counts
    if public_stats:
        filtered_result["access"] = {"filtered": public_stats}
    if named_returned or any(path in strict_paths for path in filtered_stats):
        if isinstance(result.get("summary"), str):
            filtered_result["summary"] = f"按当前权限返回 {named_returned} 条数据。"
    return filtered_result


def _filter_access_tree(
    actor: User,
    ctx: AccessContext,
    value: Any,
    *,
    path: str,
    strict_paths: set[str],
    filtered_stats: dict[str, dict[str, int]],
    strict_row: bool = False,
) -> tuple[Any, bool]:
    if isinstance(value, dict):
        present = ACCESS_TAG_FIELDS.intersection(value)
        complete = ACCESS_TAG_FIELDS.issubset(value)
        if strict_row and not complete:
            raise _AccessContractError(
                path,
                "Rows returned through an access-filtered field require all five access tags",
            )
        if present and not complete:
            raise _AccessContractError(
                path,
                "A scoped value contains only part of the required access tags",
            )
        scoped = complete
        if complete and not visible(actor, value, ctx):
            return _FILTERED, True
        output: dict[str, Any] = {}
        for key, child in value.items():
            child_path = f"{path}.{key}"
            filtered_child, child_scoped = _filter_access_tree(
                actor,
                ctx,
                child,
                path=child_path,
                strict_paths=strict_paths,
                filtered_stats=filtered_stats,
            )
            scoped = scoped or child_scoped
            if filtered_child is not _FILTERED:
                output[key] = filtered_child
        return output, scoped
    if isinstance(value, list):
        output: list[Any] = []
        scoped = False
        strict_collection = path in strict_paths
        for index, child in enumerate(value):
            child_requires_tags = (
                isinstance(child, dict)
                and (
                    strict_collection
                    or bool(_ROW_IDENTITY_FIELDS.intersection(child))
                )
            )
            filtered_child, child_scoped = _filter_access_tree(
                actor,
                ctx,
                child,
                path=f"{path}[{index}]",
                strict_paths=strict_paths,
                filtered_stats=filtered_stats,
                strict_row=child_requires_tags,
            )
            scoped = scoped or child_scoped
            if filtered_child is not _FILTERED:
                output.append(filtered_child)
        if scoped or strict_collection:
            filtered_stats[path] = {
                "examined": len(value),
                "returned": len(output),
            }
        return output, scoped
    if ACCESS_TAG_FIELDS.issubset(
        {field for field in ACCESS_TAG_FIELDS if hasattr(value, field)}
    ):
        if not visible(actor, value, ctx):
            return _FILTERED, True
        return value, True
    return value, False
