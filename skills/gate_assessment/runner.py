from typing import Any

from skills.contracts import SkillContext, SkillResult


class GateAssessmentSkillRunner:
    def run(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult:
        gate = str(payload.get("gate_name") or "").strip()
        if not gate:
            raise ValueError("C13 需要 gate_name")
        checks = list(payload.get("checks") or [])
        gaps = [item for item in checks if item.get("status") not in {"passed", "approved_exception"}]
        missing_evidence = [item for item in checks if not item.get("evidence_refs")]
        recommendation = "ready" if checks and not gaps and not missing_evidence else "not_ready"
        artifact = {"gate_name": gate, "target_stage": str(payload.get("target_stage") or ""), "checks": checks,
            "gaps": gaps, "approved_exceptions": [item for item in checks if item.get("status") == "approved_exception"],
            "recommendation": recommendation, "authorized_decision": str(payload.get("authorized_decision") or "")}
        warnings = []
        if not checks: warnings.append("准入检查项为空")
        if missing_evidence: warnings.append("部分准入项缺少证据")
        if recommendation == "ready" and not artifact["authorized_decision"]: warnings.append("满足条件仅代表建议准入，仍需授权人决定")
        return SkillResult("C13", "gate_assessment", "GateAssessment", artifact,
            candidates=[{"gate_name": gate, "recommendation": recommendation}],
            evidence_refs=[ref for item in checks for ref in item.get("evidence_refs") or []], warnings=warnings,
            requires_confirmation=True, audit={"check_count": len(checks), "gap_count": len(gaps)})
