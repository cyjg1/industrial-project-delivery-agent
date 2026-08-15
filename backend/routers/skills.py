from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Request

from agent.skill_packages import load_skill_packages
from agent.skills import list_skills
from backend.access_scope import request_scope, require_role


@dataclass(frozen=True)
class SkillRouterServices:
    store_factory: Callable[[], Any]
    skill_service_factory: Callable[[Any], Any]


def create_skills_router(services: SkillRouterServices) -> APIRouter:
    router = APIRouter(prefix="/api/skills", tags=["skills"])

    @router.get("/packages")
    def skill_packages(request: Request) -> dict[str, Any]:
        scope = request_scope(request)
        require_role(scope, "exec")
        store = services.store_factory()
        project_skills = services.skill_service_factory(store).list_skills(
            scope.actor,
            scope.access_context,
            project_id=scope.project_id,
        )
        index = load_skill_packages()
        packages = [_project_package(skill) for skill in project_skills]
        packages.extend(package.as_dict() for package in index.packages)
        packages.extend(issue.as_dict() for issue in index.issues)
        return {
            "packages": packages,
            "external_skills_dir": index.directory,
            "contract_skills": [
                {
                    "capability_id": skill.capability_id,
                    "name": skill.name,
                    "description": skill.description,
                    "version": skill.version,
                    "origin": "contract",
                    "status": skill.runtime_status,
                    "error": "",
                    "when_to_use": skill.when_to_use,
                    "tool_names": list(skill.tool_names),
                }
                for skill in list_skills()
            ],
        }

    return router


def _project_package(skill: dict[str, Any]) -> dict[str, Any]:
    version = skill.get("active_version") or skill.get("latest_version") or {}
    return {
        "name": str(skill.get("name") or ""),
        "description": str(version.get("description") or ""),
        "version": str(version.get("version") or ""),
        "origin": "project",
        "status": str(skill.get("status") or ""),
        "error": "",
        "folder": str(skill.get("slug") or ""),
        "allowed_tools": list(version.get("tool_names") or []),
        "resources": [],
        "body_chars": len(str(version.get("body_markdown") or "")),
    }
