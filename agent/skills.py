"""Read-only view over the built-in C01-C17 capability contracts.

These specs describe what each capability promises; only C02 has an active
runtime. They are not injected into the conversation system prompt. Runtime
Skill discovery goes through `search_project_skills`, which loads published
project Skills and external Skill packages. `GET /api/skills/packages` exposes
this catalog next to those two so the contract-only capabilities stay visible
without pretending to be callable.
"""

from __future__ import annotations

from dataclasses import dataclass

from skills.registry import list_project_skill_specs


@dataclass(frozen=True)
class Skill:
    name: str
    when_to_use: str
    tool_names: list[str]
    system_hint: str
    capability_id: str = ""
    description: str = ""
    version: str = ""
    runtime_status: str = "contract_only"


def list_skills() -> list[Skill]:
    return [
        Skill(
            name=spec.name,
            when_to_use=spec.when_to_use,
            tool_names=list(spec.tool_names),
            system_hint=spec.system_hint,
            capability_id=spec.capability_id,
            description=spec.description,
            version=spec.version,
            runtime_status=spec.runtime_status,
        )
        for spec in list_project_skill_specs()
    ]
