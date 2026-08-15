from typing import Any

from skills.artifacts import DependencyEdge, DependencyGraphArtifact, EvidenceLink
from skills.contracts import SkillContext, SkillResult


class DependencyAnalysisSkillRunner:
    def run(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult:
        project_id = str(payload.get("project_id") or (context.project_id if context else ""))
        if not project_id:
            raise ValueError("C08 需要 project_id")
        edges = []
        warnings = []
        for raw in payload.get("dependencies") or []:
            edge = DependencyEdge(
                dependency_id=str(raw.get("dependency_id") or raw.get("id") or ""),
                provider_id=str(raw.get("provider_id") or ""), consumer_id=str(raw.get("consumer_id") or ""),
                subject=str(raw.get("subject") or ""), input_output=str(raw.get("input_output") or ""),
                planned_at=str(raw.get("planned_at") or ""), status=str(raw.get("status") or "candidate"),
                owners=tuple(str(item) for item in raw.get("owners") or []), waiting_days=int(raw.get("waiting_days") or 0),
                evidence_refs=tuple(EvidenceLink.from_dict(item) for item in raw.get("evidence_refs") or []))
            edges.append(edge)
            if not edge.provider_id or not edge.consumer_id or not edge.owners:
                warnings.append(f"依赖 {edge.dependency_id or edge.subject} 的供需方或责任人不完整")
        graph = DependencyGraphArtifact(project_id, edges,
            [e.dependency_id for e in edges if e.status in {"blocked", "overdue"}],
            [e.dependency_id for e in edges if e.waiting_days >= int(payload.get("critical_waiting_days") or 7)])
        return SkillResult("C08", "dependency_analysis", "DependencyGraph", graph.as_dict(),
            candidates=[{"dependency_id": e.dependency_id, "subject": e.subject} for e in edges if e.status == "candidate"],
            warnings=warnings, requires_confirmation=bool(warnings or any(e.status == "candidate" for e in edges)),
            audit={"dependency_count": len(edges), "broken_count": len(graph.broken_dependency_ids)})
