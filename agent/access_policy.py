from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


# `pm` and `pmo` are compatibility aliases for the same project-management role.
ROLE_RANK = {"viewer": 0, "exec": 1, "topic_lead": 2, "professional_lead": 3, "pmo": 5, "pm": 5}

SENSITIVITY_MIN_ROLE = {
    "l1": "exec",
    "l2": "exec",
    "l3": "topic_lead",
    "l4": "pm",
}


@dataclass(frozen=True)
class User:
    id: str
    org_id: str
    name: str
    feishu_id: str = ""


@dataclass(frozen=True)
class Taggable:
    id: str
    org_id: str
    project_id: str
    topic_id: str | None
    author_id: str
    sensitivity: str


@dataclass(frozen=True)
class AccessContext:
    project_roles: Mapping[tuple[str, str], str] = field(default_factory=dict)
    topic_members: Mapping[str, set[str]] = field(default_factory=dict)
    item_blocklists: Mapping[str, set[str]] = field(default_factory=dict)
    reporting_managers: Mapping[tuple[str, str], str] = field(default_factory=dict)

    def projects_of(self, actor: User) -> set[str]:
        return {
            project_id
            for (user_id, project_id), role in self.project_roles.items()
            if user_id == actor.id and role in ROLE_RANK
        }

    def members_of(self, topic_id: str) -> set[str]:
        return set(self.topic_members.get(topic_id, set()))

    def role_of(self, actor: User, project_id: str) -> str:
        return self.project_roles.get((actor.id, project_id), "viewer")

    def blocklist_of(self, item: Any) -> set[str]:
        return set(self.item_blocklists.get(_item_id(item), set()))

    def managed_user_ids(self, actor: User, project_id: str) -> set[str]:
        """Return the recursive reporting subtree from preloaded relationships."""
        children: dict[str, set[str]] = {}
        for (user_id, scoped_project_id), manager_id in self.reporting_managers.items():
            if scoped_project_id != project_id or not manager_id:
                continue
            children.setdefault(manager_id, set()).add(user_id)
        managed: set[str] = set()
        pending = list(children.get(actor.id, set()))
        while pending:
            user_id = pending.pop()
            if user_id == actor.id or user_id in managed:
                continue
            managed.add(user_id)
            pending.extend(children.get(user_id, set()))
        return managed


class IdentityResolutionError(ValueError):
    pass


def visible(actor: User, item: Any, ctx: AccessContext) -> bool:
    if _field(item, "author_id") == actor.id:
        return True
    if _field(item, "org_id") != actor.org_id:
        return False
    project_id = _field(item, "project_id")
    if project_id not in ctx.projects_of(actor):
        return False
    role = ctx.role_of(actor, project_id)
    sensitivity = _field(item, "sensitivity")
    min_role = SENSITIVITY_MIN_ROLE.get(str(sensitivity))
    if min_role is None or ROLE_RANK.get(role, -1) < ROLE_RANK[min_role]:
        return False
    if actor.id in ctx.blocklist_of(item):
        return False
    owners = {str(value) for value in (_field(item, "owner_candidates") or [])}
    if actor.id in owners or actor.name in owners:
        return True
    topic_id = _field(item, "topic_id")
    if topic_id and ROLE_RANK.get(role, -1) < ROLE_RANK["pmo"] and actor.id not in ctx.members_of(topic_id):
        return False
    return True


def visible_filter(actor: User, items: list[Any], ctx: AccessContext) -> list[Any]:
    return [item for item in items if visible(actor, item, ctx)]


def resolve_actor(request: Any, user_lookup: Callable[[str], User | dict[str, Any] | None] | None = None) -> User:
    source = os.getenv("IDENTITY_SOURCE", "local").strip().lower() or "local"
    if source != "local":
        raise IdentityResolutionError(f"IDENTITY_SOURCE={source} is reserved and not implemented")

    actor_id = _header(request, "X-Actor-Id").strip()
    if not actor_id:
        raise IdentityResolutionError("Missing required X-Actor-Id header")

    if user_lookup is None:
        return User(id=actor_id, org_id=_header(request, "X-Org-Id").strip() or "local_org", name=actor_id)

    user = user_lookup(actor_id)
    if user is None:
        raise IdentityResolutionError(f"Unknown actor: {actor_id}")
    if isinstance(user, User):
        return user
    return User(
        id=str(user.get("id") or ""),
        org_id=str(user.get("org_id") or ""),
        name=str(user.get("name") or user.get("id") or ""),
        feishu_id=str(user.get("feishu_id") or ""),
    )


def _field(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _item_id(item: Any) -> str:
    return str(_field(item, "id") or _field(item, "item_id") or "")


def _header(request: Any, name: str) -> str:
    headers = getattr(request, "headers", {}) or {}
    value = ""
    if hasattr(headers, "get"):
        value = headers.get(name) or headers.get(name.lower()) or ""
    return str(value or "")
