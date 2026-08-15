from __future__ import annotations

from typing import Any

from skills.artifacts import ProjectFact, ProjectGraphArtifact, ProjectRelationship
from skills.contracts import SkillContext, SkillResult


class ProjectKnowledgeSkillRunner:
    def run(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult:
        project_id = str(payload.get("project_id") or (context.project_id if context else ""))
        if not project_id:
            raise ValueError("C01 需要 project_id")

        facts = [ProjectFact.from_dict(item) for item in payload.get("facts") or payload.get("objects") or []]
        relationships = [ProjectRelationship.from_dict(item) for item in payload.get("relationships") or []]
        graph = ProjectGraphArtifact(
            project_id=project_id,
            facts=facts,
            relationships=relationships,
            source_ids=[str(value) for value in payload.get("source_ids") or []],
        )
        warnings = []
        for fact in facts:
            if not fact.evidence_refs:
                warnings.append(f"事实 {fact.object_id or fact.title} 缺少证据引用")
        for relationship in relationships:
            if not relationship.source_object_id or not relationship.target_object_id:
                warnings.append(f"关系 {relationship.relationship_id} 缺少起点或终点")

        candidates = [
            {"object_id": fact.object_id, "object_type": fact.object_type, "title": fact.title}
            for fact in facts
            if fact.status == "candidate"
        ]
        evidence_refs = [
            {
                "source_id": ref.source_id,
                "locator": ref.locator,
                "quote": ref.quote,
                "version": ref.version,
            }
            for fact in facts
            for ref in fact.evidence_refs
        ]
        return SkillResult(
            capability_id="C01",
            skill_name="project_knowledge",
            artifact_type="ProjectGraph",
            artifact=graph.as_dict(),
            candidates=candidates,
            evidence_refs=evidence_refs,
            warnings=warnings,
            requires_confirmation=bool(candidates or warnings),
            audit={"fact_count": len(facts), "relationship_count": len(relationships)},
        )
