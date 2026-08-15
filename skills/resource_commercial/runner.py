from typing import Any

from skills.contracts import SkillContext, SkillResult


class ResourceCommercialSkillRunner:
    def run(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult:
        project_id = str(payload.get("project_id") or (context.project_id if context else ""))
        if not project_id:
            raise ValueError("C16 需要 project_id")
        people_load = list(payload.get("people_load") or [])
        warnings = []
        for row in people_load:
            if not row.get("evidence_refs"): warnings.append(f"人员 {row.get('person_id') or row.get('name') or ''} 的负荷缺少证据")
            if any(key in row for key in ("personality", "performance_label", "idle_judgement")):
                warnings.append("资源分析不得包含人格化或无证据绩效标签")
        artifact = {"project_id": project_id, "people_load": people_load,
            "skill_coverage": list(payload.get("skill_coverage") or []), "cost_trend": list(payload.get("cost_trend") or []),
            "acceptance_nodes": list(payload.get("acceptance_nodes") or []), "payment_nodes": list(payload.get("payment_nodes") or []),
            "scope_changes": list(payload.get("scope_changes") or []), "data_completeness": payload.get("data_completeness")}
        return SkillResult("C16", "resource_commercial", "ResourceCommercialModel", artifact,
            candidates=[{"analysis_type": "resource_commercial", "project_id": project_id}],
            evidence_refs=[ref for row in people_load for ref in row.get("evidence_refs") or []], warnings=warnings,
            requires_confirmation=True, audit={"people_count": len(people_load), "payment_node_count": len(artifact["payment_nodes"])})
