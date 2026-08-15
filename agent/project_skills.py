from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from agent.access_policy import AccessContext, ROLE_RANK, User, visible
from agent.llm_provider import LLMProvider, get_provider
from agent.schemas import CandidateStatus, InspectionItem, to_plain
from agent.tool_registry import ToolRegistry
from store.sqlite_store import ProjectSQLiteStore


COMPILER_VERSION = "project-method-skill-v2"


class ProjectSkillValidationError(ValueError):
    pass


class ProjectSkillService:
    def __init__(
        self,
        store: ProjectSQLiteStore,
        tool_registry: ToolRegistry,
        *,
        provider: LLMProvider | None = None,
    ) -> None:
        self.store = store
        self.tool_registry = tool_registry
        self.provider = provider

    def compile_method(
        self,
        method_id: str,
        actor: User,
        ctx: AccessContext,
    ) -> dict[str, Any]:
        method = self.store.get_item(method_id)
        self._require_manager(actor, ctx, method.project_id)
        if not _is_method(method):
            raise ProjectSkillValidationError(f"Item is not a method: {method_id}")
        if not visible(actor, method, ctx):
            raise KeyError(method_id)

        readiness = _method_readiness(method)
        package: dict[str, Any]
        if not readiness["ready"]:
            package = _blocked_package(method)
        else:
            try:
                provider = self._provider()
                package = self._generate_package(method, provider)
                readiness = _merge_readiness(
                    readiness,
                    _package_failures(package, registered_tools=set(self.tool_registry.tool_names())),
                )
            except Exception as exc:
                readiness = _merge_readiness(
                    readiness,
                    {"model_compile": f"{type(exc).__name__}: {exc}"},
                )
                package = _blocked_package(method)

        slug = _skill_slug(method)
        skill_id = _skill_id(method)
        body = _render_skill_markdown(method, package, readiness, slug=slug)
        result = self.store.create_project_skill_version(
            skill_id=skill_id,
            slug=slug,
            name=str(package.get("name") or method.title),
            source_method_id=method.item_id,
            description=str(package.get("description") or method.description),
            when_to_use=str(package.get("when_to_use") or ""),
            tool_names=[str(name) for name in package.get("tool_names") or []],
            body_markdown=body,
            readiness=readiness,
            evidence_refs=[to_plain(ref) for ref in method.evidence_refs],
            pressure_tests=list(package.get("pressure_tests") or []),
            tags=_method_tags(method, actor),
            actor=actor,
            payload={
                "compiler_version": COMPILER_VERSION,
                "compiled_name": str(package.get("name") or method.title),
                "source_method_id": method.item_id,
                "source_method_author_id": method.author_id,
                "when_not_to_use": package.get("when_not_to_use") or [],
                "steps": package.get("steps") or [],
                "stop_conditions": package.get("stop_conditions") or [],
                "boundaries": package.get("boundaries") or [],
                "anti_patterns": package.get("anti_patterns") or [],
            },
        )
        self._write_mirror(result, slug=slug)
        return result

    def run_pressure_tests(
        self,
        skill_id: str,
        actor: User,
        ctx: AccessContext,
    ) -> dict[str, Any]:
        skill = self.store.get_project_skill(skill_id)
        self._require_manager(actor, ctx, skill["project_id"])
        if not visible(actor, skill, ctx):
            raise KeyError(skill_id)
        version = skill["latest_version"]
        if version is None:
            raise ProjectSkillValidationError("Project Skill has no compiled version")
        full_version = self.store.get_project_skill_version(version["version_id"])
        if full_version["status"] == "published":
            raise ProjectSkillValidationError(
                "Published Project Skill versions are immutable; compile a new version before retesting"
            )
        if not full_version["readiness"].get("ready"):
            raise ProjectSkillValidationError("Project Skill readiness gate has not passed")
        tests = full_version["pressure_tests"]
        if not tests:
            raise ProjectSkillValidationError("Project Skill has no pressure tests")
        provider = self._provider()

        results: list[dict[str, Any]] = []
        for test in tests:
            test_payload = {
                "skill": {
                    "name": skill["name"],
                    "description": full_version["description"],
                    "when_to_use": full_version["when_to_use"],
                    "boundaries": full_version["payload"].get("boundaries") or [],
                    "anti_patterns": full_version["payload"].get("anti_patterns") or [],
                },
                "input": test["input"],
            }
            prompt = (
                "Decide whether this one project Skill should be selected for the test input. "
                "Return JSON only: {\"selected\": boolean, \"reason\": string}. "
                "Do not execute the Skill.\nPRESSURE_TEST="
                + json.dumps(test_payload, ensure_ascii=False)
            )
            error = ""
            selected = False
            reason = ""
            try:
                response = _json_object(provider.complete_json(prompt))
                if not isinstance(response.get("selected"), bool):
                    raise ProjectSkillValidationError("pressure-test selector omitted boolean selected")
                selected = bool(response["selected"])
                reason = str(response.get("reason") or "").strip()
                if not reason:
                    raise ProjectSkillValidationError("pressure-test selector omitted reason")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            passed = not error and selected == bool(test["should_trigger"])
            results.append({
                "id": test["id"],
                "selected": selected,
                "passed": passed,
                "reason": reason,
                "error": error,
            })
        return self.store.update_project_skill_test_results(full_version["version_id"], results)

    def publish(
        self,
        skill_id: str,
        actor: User,
        ctx: AccessContext,
    ) -> dict[str, Any]:
        skill = self.store.get_project_skill(skill_id)
        self._require_manager(actor, ctx, skill["project_id"])
        if not visible(actor, skill, ctx):
            raise KeyError(skill_id)
        latest = skill["latest_version"]
        if latest is None:
            raise ProjectSkillValidationError("Project Skill has no compiled version")
        published = self.store.publish_project_skill_version(latest["version_id"], actor_id=actor.id)
        self._write_mirror(published, slug=skill["slug"])
        return published

    def list_skills(
        self,
        actor: User,
        ctx: AccessContext,
        *,
        project_id: str,
        published_only: bool = False,
    ) -> list[dict[str, Any]]:
        return self.store.list_project_skills(
            project_id=project_id,
            actor=actor,
            ctx=ctx,
            published_only=published_only,
        )

    def search_published(
        self,
        query: str,
        actor: User,
        ctx: AccessContext,
        *,
        project_id: str,
        allowed_tool_names: set[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        terms = _query_terms(query)
        rows = self.list_skills(actor, ctx, project_id=project_id, published_only=True)
        ranked: list[tuple[float, dict[str, Any]]] = []
        for skill in rows:
            active = skill.get("active_version") or {}
            text = " ".join([
                str(skill.get("name") or ""),
                str(active.get("description") or ""),
                str(active.get("when_to_use") or ""),
            ]).lower()
            score = sum(1 for term in terms if term in text)
            if terms and score == 0:
                continue
            tool_names = list(active.get("tool_names") or [])
            if allowed_tool_names is not None:
                tool_names = [name for name in tool_names if name in allowed_tool_names]
            reliability = self.store.memory_skill_evolution.skill_reliability(
                skill["id"],
                str(active.get("version_id") or ""),
            )
            lifecycle_bonus = {
                "active": 0.25,
                "probationary": 0.0,
                "revalidation_required": -0.25,
            }.get(reliability["lifecycle"], 0.0)
            retrieval_score = float(score) + reliability["reliability"] * 0.35 + lifecycle_bonus
            ranked.append((retrieval_score, {
                "id": skill["id"],
                "skill_id": skill["id"],
                "version_id": active.get("version_id") or "",
                "name": skill["name"],
                "description": active.get("description") or "",
                "when_to_use": active.get("when_to_use") or "",
                "body_markdown": active.get("body_markdown") or "",
                "tool_names": tool_names,
                "version": active.get("version"),
                "org_id": skill["org_id"],
                "project_id": skill["project_id"],
                "topic_id": skill.get("topic_id"),
                "author_id": skill["author_id"],
                "sensitivity": skill["sensitivity"],
                "retrieval_tier": "published_project_skill",
                "retrieval_score": round(retrieval_score, 4),
                "reliability": reliability,
            }))
        ranked.sort(key=lambda pair: (-pair[0], pair[1]["name"], pair[1]["skill_id"]))
        return [row for _, row in ranked[: max(1, min(limit, 10))]]

    def _generate_package(self, method: InspectionItem, provider: LLMProvider) -> dict[str, Any]:
        prompt = (
            "You compile one confirmed project method into a project-scoped Agent Skill candidate. "
            "Use only the supplied method and evidence. Preserve the method's business goal, principles, "
            "reasoning chain, applicable scope, and evidence boundary; do not collapse them into generic "
            "project-management advice. Return one JSON object with: name, description, "
            "when_to_use, when_not_to_use (array), steps (array of instruction/done_when), "
            "stop_conditions (array), boundaries (array), anti_patterns (array), "
            "tool_names (array using only REGISTERED_TOOLS), "
            "and pressure_tests (array of name/input/should_trigger). Include at least one positive and one "
            "negative pressure test. A negative input must be clearly outside when_to_use or explicitly inside "
            "when_not_to_use; missing details in an otherwise applicable case is not a valid negative. "
            "Each done_when must describe observable business evidence, a human decision, an accepted "
            "deliverable, or a concrete unresolved gap; 'analysis completed' is not sufficient. "
            "anti_patterns must name plausible shallow behaviors such as repeating meeting wording without "
            "deriving an actionable conclusion. "
            "The Skill may guide tool use but never grants permission.\n"
            f"REGISTERED_TOOLS={json.dumps(self.tool_registry.tool_names(), ensure_ascii=False)}\n"
            f"CONFIRMED_METHOD={json.dumps(to_plain(method), ensure_ascii=False)}"
        )
        return _json_object(provider.complete_json(prompt))

    def _provider(self) -> LLMProvider:
        if self.provider is None:
            self.provider = get_provider(role="classification")
        return self.provider

    def _require_manager(self, actor: User, ctx: AccessContext, project_id: str) -> None:
        role = ctx.role_of(actor, project_id)
        if ROLE_RANK.get(role, -1) < ROLE_RANK["pmo"]:
            raise PermissionError("Project Skill lifecycle requires pmo or pm role")

    def _write_mirror(self, version: dict[str, Any], *, slug: str) -> None:
        target = self.store.vault_dir / "技能库" / slug / f"v{version['version']}" / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(version["body_markdown"], encoding="utf-8")


def method_skill_readiness(method: InspectionItem, *, registered_tools: set[str] | None = None) -> dict[str, Any]:
    readiness = _method_readiness(method)
    if registered_tools is not None and readiness["ready"]:
        readiness = _merge_readiness(readiness, {})
    return readiness


def _method_readiness(method: InspectionItem) -> dict[str, Any]:
    failures: dict[str, str] = {}
    if method.status != CandidateStatus.CONFIRMED:
        failures["confirmed_method"] = "method must be human-confirmed"
    independent_sources = {ref.source_doc_id for ref in method.evidence_refs if ref.source_doc_id and ref.quote.strip()}
    if len(independent_sources) < 2:
        failures["independent_evidence"] = "at least two independently sourced evidence records are required"
    raw_sources = {
        ref.source_doc_id
        for ref in method.evidence_refs
        if ref.source_doc_id
        and ref.quote.strip()
        and ref.evidence_level == "raw_traceable"
        and ref.raw_source_doc_id
        and ref.raw_locator
    }
    if len(raw_sources) < 2:
        failures["raw_evidence"] = "at least two independent sources must be traceable to raw transcript evidence"
    if not str(method.business_goal or "").strip():
        failures["business_goal"] = "business goal is missing"
    if len([value for value in method.principles or [] if str(value).strip()]) < 2:
        failures["principles"] = "at least two principles are required"
    if len([value for value in method.reasoning_chain or [] if str(value).strip()]) < 2:
        failures["reasoning_chain"] = "at least two reasoning steps are required"
    if not str(method.applicable_scope or "").strip():
        failures["applicable_scope"] = "applicable scope is missing"
    return {
        "ready": not failures,
        "failed_checks": list(failures),
        "details": failures,
        "compiler_version": COMPILER_VERSION,
    }


def _package_failures(package: dict[str, Any], *, registered_tools: set[str]) -> dict[str, str]:
    failures: dict[str, str] = {}
    for field in ("name", "description", "when_to_use"):
        if not str(package.get(field) or "").strip():
            failures[f"package_{field}"] = f"compiled package is missing {field}"
    if not _string_list(package.get("when_not_to_use")):
        failures["when_not_to_use"] = "at least one non-trigger boundary is required"
    steps = package.get("steps")
    if not isinstance(steps, list) or len(steps) < 2 or any(
        not isinstance(step, dict)
        or not str(step.get("instruction") or "").strip()
        or not str(step.get("done_when") or "").strip()
        for step in steps
    ):
        failures["execution_steps"] = "at least two steps with deterministic done_when checks are required"
    if not _string_list(package.get("stop_conditions")):
        failures["stop_conditions"] = "at least one explicit stop condition is required"
    if not _string_list(package.get("boundaries")):
        failures["boundaries"] = "at least one boundary is required"
    if not _string_list(package.get("anti_patterns")):
        failures["anti_patterns"] = "at least one shallow or unsafe anti-pattern is required"
    tool_names = _string_list(package.get("tool_names"), allow_empty=True)
    if any(name not in registered_tools for name in tool_names):
        failures["registered_tools"] = "compiled Skill references a tool outside the unified registry"
    tests = package.get("pressure_tests")
    if (
        not isinstance(tests, list)
        or len(tests) < 2
        or any(not isinstance(test, dict) for test in tests)
    ):
        failures["pressure_tests"] = "positive and negative pressure tests are required"
    else:
        triggers = {bool(test.get("should_trigger")) for test in tests}
        if triggers != {False, True} or any(
            not str(test.get("name") or "").strip()
            or not str(test.get("input") or "").strip()
            or not isinstance(test.get("should_trigger"), bool)
            for test in tests
        ):
            failures["pressure_tests"] = "pressure tests must include valid positive and negative inputs"
    return failures


def _merge_readiness(readiness: dict[str, Any], failures: dict[str, str]) -> dict[str, Any]:
    details = {**dict(readiness.get("details") or {}), **failures}
    return {
        **readiness,
        "ready": not details,
        "failed_checks": list(details),
        "details": details,
    }


def _render_skill_markdown(
    method: InspectionItem,
    package: dict[str, Any],
    readiness: dict[str, Any],
    *,
    slug: str,
) -> str:
    description = str(package.get("description") or method.description).strip()
    lines = [
        "---",
        f"name: {slug}",
        f"description: {json.dumps(description, ensure_ascii=False)}",
        "metadata:",
        f"  project_id: {method.project_id}",
        f"  source_method_id: {method.item_id}",
        f"  sensitivity: {method.sensitivity}",
        f"  compiler: {COMPILER_VERSION}",
        "---",
        "",
        f"# {package.get('name') or method.title}",
        "",
        "## Trigger",
        str(package.get("when_to_use") or "尚未达到编译门槛。"),
        "",
        "## Do Not Trigger",
        *_bullet_lines(package.get("when_not_to_use") or ["方法成熟度门禁未通过时不得执行。"]),
        "",
        "## RIA++",
        f"- Original evidence: {len(method.evidence_refs)} 条已保存证据。",
        f"- Interpretation: {method.business_goal or '待补业务目标'}",
        f"- Application: {method.applicable_scope or '待补适用范围'}",
        "",
        "## Execution",
    ]
    steps = package.get("steps") or []
    if steps:
        for index, step in enumerate(steps, start=1):
            lines.append(f"{index}. {step['instruction']}")
            lines.append(f"   - Done when: {step['done_when']}")
    else:
        lines.append("1. 编译门禁未通过，补齐证据和执行契约后重新编译。")
    lines.extend([
        "",
        "## Stop Conditions",
        *_bullet_lines(package.get("stop_conditions") or ["门禁未通过时停止。"]),
        "",
        "## Boundary",
        *_bullet_lines(package.get("boundaries") or ["不扩大数据可见范围；不替代统一工具注册表的权限判断。"]),
        "",
        "## Anti-patterns",
        *_bullet_lines(package.get("anti_patterns") or ["只复述会议原话，不形成可执行结论。"]),
        "",
        "## Evidence",
    ])
    for ref in method.evidence_refs:
        lines.append(f"- {ref.source_doc_id} · {ref.locator}: {ref.quote}")
    if not readiness["ready"]:
        lines.extend(["", "## Publication Gate", "当前不得发布："])
        lines.extend(f"- {key}: {value}" for key, value in readiness["details"].items())
    return "\n".join(lines).strip() + "\n"


def _blocked_package(method: InspectionItem) -> dict[str, Any]:
    return {
        "name": method.title,
        "description": method.description,
        "when_to_use": "",
        "when_not_to_use": [],
        "steps": [],
        "stop_conditions": [],
        "boundaries": [],
        "anti_patterns": [],
        "tool_names": [],
        "pressure_tests": [],
    }


def _method_tags(method: InspectionItem, actor: User) -> dict[str, Any]:
    return {
        "org_id": method.org_id,
        "project_id": method.project_id,
        "topic_id": method.topic_id,
        "author_id": actor.id,
        "sensitivity": method.sensitivity,
    }


def _skill_id(method: InspectionItem) -> str:
    digest = hashlib.sha256(f"{method.project_id}\0{method.item_id}".encode("utf-8")).hexdigest()[:20]
    return f"project_skill_{digest}"


def _skill_slug(method: InspectionItem) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", method.item_id.lower()).strip("-")[:36]
    digest = hashlib.sha256(method.item_id.encode("utf-8")).hexdigest()[:8]
    return f"project-{cleaned or 'method'}-{digest}"


def _is_method(item: InspectionItem) -> bool:
    category = str(item.category or "").strip().lower()
    facets = {str(value).strip().lower() for value in item.facet_types or []}
    return category in {"method", "methods", "methodology", "方法", "法"} or bool(
        facets & {"method", "methodology", "方法", "法"}
    )


def _json_object(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProjectSkillValidationError("Project Skill model output is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ProjectSkillValidationError("Project Skill model output must be one JSON object")
    return payload


def _string_list(value: Any, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        return []
    result = [str(item).strip() for item in value if str(item).strip()]
    return result if result or allow_empty else []


def _bullet_lines(values: list[Any]) -> list[str]:
    return [f"- {str(value).strip()}" for value in values if str(value).strip()]


def _query_terms(query: str) -> list[str]:
    normalized = " ".join(str(query or "").lower().split())
    if not normalized:
        return []
    terms = [part for part in re.split(r"[\s,，。；;：:、]+", normalized) if part]
    if len(terms) == 1 and len(terms[0]) > 2:
        terms.extend(terms[0][index:index + 2] for index in range(len(terms[0]) - 1))
    return list(dict.fromkeys(terms))
