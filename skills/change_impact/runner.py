from typing import Any

from skills.artifacts import EvidenceLink
from skills.contracts import SkillContext, SkillResult


class ChangeImpactSkillRunner:
    def run(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult:
        change = str(payload.get("change") or "").strip()
        baseline = str(payload.get("baseline") or "").strip()
        if not change or not baseline:
            raise ValueError("C10 需要 change 和 baseline")
        refs = [EvidenceLink.from_dict(item) for item in payload.get("evidence_refs") or []]
        approved = bool(payload.get("approved", False))
        artifact = {
            "change_id": str(payload.get("change_id") or ""), "change": change, "baseline": baseline,
            "classification": str(payload.get("classification") or "待分类"), "source": str(payload.get("source") or ""),
            "affected_tasks": list(payload.get("affected_tasks") or []), "affected_people": list(payload.get("affected_people") or []),
            "affected_materials": list(payload.get("affected_materials") or []), "affected_dependencies": list(payload.get("affected_dependencies") or []),
            "affected_milestones": list(payload.get("affected_milestones") or []), "suggested_actions": list(payload.get("suggested_actions") or []),
            "approved": approved, "evidence_refs": [vars(ref) for ref in refs],
        }
        warnings = []
        if not refs: warnings.append("变更缺少来源证据")
        if artifact["classification"] == "待分类": warnings.append("尚未区分说明性变化、范围变化或已批准变更")
        if not any(artifact[key] for key in ("affected_tasks", "affected_people", "affected_materials", "affected_dependencies", "affected_milestones")):
            warnings.append("尚未识别受影响对象")
        return SkillResult("C10", "change_impact", "ChangeEvent", artifact,
            candidates=[] if approved else [{"change_id": artifact["change_id"], "change": change}],
            evidence_refs=[vars(ref) for ref in refs], warnings=warnings,
            requires_confirmation=not approved or bool(warnings), audit={"approved": approved})
