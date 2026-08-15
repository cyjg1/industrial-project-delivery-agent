from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

from agent.access_policy import AccessContext, ROLE_RANK, User, resolve_actor, visible


@dataclass(frozen=True)
class ActorScope:
    actor: User
    access_context: AccessContext
    project_id: str
    role: str


def resolve_request_scope(request: Request, store: Any) -> ActorScope:
    actor = resolve_actor(request, user_lookup=store.get_access_user)
    access_context = store.access_context_for_actor(actor.id)
    project_id = store.default_project_id_for_actor(actor.id, actor.org_id)
    if not project_id or project_id not in access_context.projects_of(actor):
        raise HTTPException(status_code=403, detail="Actor is not a member of an active project")
    return ActorScope(
        actor=actor,
        access_context=access_context,
        project_id=project_id,
        role=access_context.role_of(actor, project_id),
    )


def request_scope(request: Request) -> ActorScope:
    scope = getattr(request.state, "actor_scope", None)
    if not isinstance(scope, ActorScope):
        raise HTTPException(status_code=401, detail="Missing authenticated actor scope")
    return scope


def require_role(scope: ActorScope, minimum_role: str) -> None:
    if ROLE_RANK.get(scope.role, -1) < ROLE_RANK.get(minimum_role, 999):
        raise HTTPException(status_code=403, detail="Actor role is not allowed to perform this action")


def require_visible(scope: ActorScope, value: Any) -> None:
    if not visible(scope.actor, value, scope.access_context):
        raise HTTPException(status_code=403, detail="Target is outside the actor's visible project scope")


def require_review(scope: ActorScope, item: Any) -> None:
    require_visible(scope, item)
    require_role(scope, "topic_lead")


def require_item_edit(scope: ActorScope, item: Any) -> None:
    require_visible(scope, item)
    if ROLE_RANK.get(scope.role, -1) >= ROLE_RANK["topic_lead"]:
        return
    if str(getattr(item, "author_id", "") or "") == scope.actor.id:
        return
    raise HTTPException(status_code=403, detail="Only the author or a topic lead can edit this item")


def require_task_edit(scope: ActorScope, task: Any) -> None:
    require_visible(scope, task)
    if ROLE_RANK.get(scope.role, -1) >= ROLE_RANK["topic_lead"]:
        return
    owners = {str(value) for value in (getattr(task, "owner_candidates", None) or [])}
    if (
        str(getattr(task, "author_id", "") or "") == scope.actor.id
        or scope.actor.id in owners
        or scope.actor.name in owners
    ):
        return
    raise HTTPException(status_code=403, detail="Only an owner, author, or topic lead can edit this task")


def require_task_status_edit(scope: ActorScope, task: Any, next_status: str) -> None:
    require_visible(scope, task)
    owners = {str(value) for value in (getattr(task, "owner_candidates", None) or [])}
    if scope.actor.id not in owners and scope.actor.name not in owners:
        raise HTTPException(status_code=403, detail="Only a current task owner can change task status")
    current_status = str(getattr(getattr(task, "status", None), "value", "") or "")
    if next_status == current_status:
        return
    editable_statuses = {"open", "in_progress", "pending_acceptance", "done", "canceled"}
    if next_status not in editable_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Task status cannot be changed to {next_status}",
        )


def require_session(scope: ActorScope, session: dict[str, Any]) -> None:
    if session.get("project_id") != scope.project_id:
        raise HTTPException(status_code=403, detail="Session belongs to another project")
    if session.get("actor_id") != scope.actor.id and ROLE_RANK.get(scope.role, -1) < ROLE_RANK["pmo"]:
        raise HTTPException(status_code=403, detail="Session belongs to another actor")
