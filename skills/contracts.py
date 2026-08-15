from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class ConfirmationPolicy(str, Enum):
    NONE = "none"
    CANDIDATE = "candidate"
    HUMAN_CONFIRM = "human_confirm"


@dataclass(frozen=True)
class SkillSpec:
    capability_id: str
    name: str
    description: str
    when_to_use: str
    output_type: str
    required_inputs: tuple[str, ...] = ()
    optional_inputs: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    required_role: str = "exec"
    confirmation_policy: ConfirmationPolicy = ConfirmationPolicy.CANDIDATE
    system_hint: str = ""
    version: str = "1.0.0"
    runtime_status: str = "contract_only"

    def __post_init__(self) -> None:
        if len(self.capability_id) != 3 or not self.capability_id.startswith("C") or not self.capability_id[1:].isdigit():
            raise ValueError(f"能力 ID 必须使用 C01 形式：{self.capability_id}")
        if not self.name.strip():
            raise ValueError("skill name 不能为空")
        if not self.output_type.strip():
            raise ValueError("skill output_type 不能为空")
        if self.runtime_status not in {"active", "contract_only"}:
            raise ValueError(f"不支持的 skill runtime_status：{self.runtime_status}")

    def as_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class SkillContext:
    project_id: str
    actor: Any = None
    access_context: Any = None
    tool_registry: Any = None
    store: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.tool_registry is None or self.actor is None or self.access_context is None:
            raise RuntimeError("SkillContext 缺少 tool_registry、actor 或 access_context")
        return self.tool_registry.call(
            name,
            self.actor,
            self.access_context,
            dict(arguments or {}),
            project_id=self.project_id,
        )


@dataclass
class SkillResult:
    capability_id: str
    skill_name: str
    artifact_type: str
    artifact: dict[str, Any]
    candidates: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    confidence: float | None = None
    warnings: list[str] = field(default_factory=list)
    requires_confirmation: bool = False
    audit: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return _plain(self)


@runtime_checkable
class SkillRunner(Protocol):
    def run(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult | dict[str, Any]: ...


def validate_required_inputs(spec: SkillSpec, payload: dict[str, Any]) -> None:
    missing = [name for name in spec.required_inputs if payload.get(name) in (None, "", [], {})]
    if missing:
        raise ValueError(f"{spec.capability_id} 缺少必需输入：{', '.join(missing)}")


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value
