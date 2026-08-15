from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.access_scope import request_scope, require_role


class EpisodeFeedbackRequest(BaseModel):
    feedback_status: str
    note: str = Field(default="", max_length=1000)


@dataclass(frozen=True)
class MemorySkillRouterServices:
    store_factory: Callable[[], Any]
    service_factory: Callable[[Any], Any]


def create_memory_skill_router(services: MemorySkillRouterServices) -> APIRouter:
    router = APIRouter(
        prefix="/api/memory-skill-evolution",
        tags=["memory-skill-evolution"],
    )

    @router.get("")
    def snapshot(request: Request) -> dict[str, Any]:
        scope = request_scope(request)
        require_role(scope, "exec")
        service = services.service_factory(services.store_factory())
        return service.snapshot(
            actor=scope.actor,
            ctx=scope.access_context,
            project_id=scope.project_id,
        )

    @router.post("/episodes/{episode_id}/feedback")
    def feedback(
        request: Request,
        episode_id: str,
        payload: EpisodeFeedbackRequest,
    ) -> dict[str, Any]:
        scope = request_scope(request)
        require_role(scope, "exec")
        service = services.service_factory(services.store_factory())
        try:
            episode = service.feedback_episode(
                episode_id,
                feedback_status=payload.feedback_status,
                feedback_note=payload.note,
                actor=scope.actor,
                ctx=scope.access_context,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown episode: {episode_id}") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"episode": episode}

    @router.post("/policies/{policy_id}/approve")
    def approve_policy(request: Request, policy_id: str) -> dict[str, Any]:
        scope = request_scope(request)
        require_role(scope, "pmo")
        service = services.service_factory(services.store_factory())
        try:
            policy = service.approve_policy(
                policy_id,
                actor=scope.actor,
                ctx=scope.access_context,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown policy: {policy_id}") from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"policy": policy}

    @router.post("/policies/{policy_id}/retire")
    def retire_policy(request: Request, policy_id: str) -> dict[str, Any]:
        scope = request_scope(request)
        require_role(scope, "pmo")
        service = services.service_factory(services.store_factory())
        try:
            policy = service.retire_policy(
                policy_id,
                actor=scope.actor,
                ctx=scope.access_context,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown policy: {policy_id}") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {"policy": policy}

    @router.post("/cognitions/{cognition_id}/approve")
    def approve_cognition(request: Request, cognition_id: str) -> dict[str, Any]:
        scope = request_scope(request)
        require_role(scope, "pmo")
        service = services.service_factory(services.store_factory())
        try:
            cognition = service.approve_cognition(
                cognition_id,
                actor=scope.actor,
                ctx=scope.access_context,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown cognition: {cognition_id}",
            ) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"cognition": cognition}

    @router.post("/cognitions/{cognition_id}/retire")
    def retire_cognition(request: Request, cognition_id: str) -> dict[str, Any]:
        scope = request_scope(request)
        require_role(scope, "pmo")
        service = services.service_factory(services.store_factory())
        try:
            cognition = service.retire_cognition(
                cognition_id,
                actor=scope.actor,
                ctx=scope.access_context,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown cognition: {cognition_id}",
            ) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {"cognition": cognition}

    return router
