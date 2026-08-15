from typing import Any

from skills.contracts import SkillContext, SkillResult


ALLOWED_CLASSIFICATIONS = {"common", "configuration", "mapping", "adapter", "custom", "pending"}


class CapabilityAssetsSkillRunner:
    def run(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult:
        name = str(payload.get("capability_name") or "").strip()
        if not name:
            raise ValueError("C17 需要 capability_name")
        classification = str(payload.get("classification") or "pending")
        warnings = []
        if classification not in ALLOWED_CLASSIFICATIONS:
            warnings.append("能力分类不在通用、配置、映射、适配、定制或待确认范围内")
            classification = "pending"
        evidence = list(payload.get("evidence_refs") or [])
        if not evidence: warnings.append("能力共性或差异判断缺少跨专题/项目证据")
        artifact = {"capability_id": str(payload.get("capability_id") or ""), "capability_name": name,
            "scenarios": list(payload.get("scenarios") or []), "inputs": list(payload.get("inputs") or []),
            "rules": list(payload.get("rules") or []), "outputs": list(payload.get("outputs") or []),
            "common_points": list(payload.get("common_points") or []), "differences": list(payload.get("differences") or []),
            "implementation": str(payload.get("implementation") or ""), "reuse_boundary": str(payload.get("reuse_boundary") or ""),
            "classification": classification, "recommendation": str(payload.get("recommendation") or ""), "evidence_refs": evidence}
        return SkillResult("C17", "capability_assets", "CapabilityAsset", artifact,
            candidates=[{"capability_id": artifact["capability_id"], "capability_name": name, "classification": classification}],
            evidence_refs=evidence, warnings=warnings, requires_confirmation=True,
            audit={"scenario_count": len(artifact["scenarios"]), "difference_count": len(artifact["differences"])})
