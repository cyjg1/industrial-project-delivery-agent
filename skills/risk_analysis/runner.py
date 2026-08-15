from typing import Any

from skills.artifacts import EvidenceLink
from skills.contracts import SkillContext, SkillResult


class RiskAnalysisSkillRunner:
    def run(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult:
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ValueError("C07 需要 title")
        refs = [EvidenceLink.from_dict(item) for item in payload.get("evidence_refs") or []]
        status = str(payload.get("status") or "candidate")
        artifact = {
            "risk_id": str(payload.get("risk_id") or ""), "title": title,
            "risk_type": str(payload.get("risk_type") or "未分类"), "cause": str(payload.get("cause") or ""),
            "probability": payload.get("probability"), "impact": str(payload.get("impact") or ""),
            "level": str(payload.get("level") or "待评估"),
            "affected_object_ids": [str(item) for item in payload.get("affected_object_ids") or []],
            "owners": [str(item) for item in payload.get("owners") or []],
            "measures": list(payload.get("measures") or []), "escalation_condition": str(payload.get("escalation_condition") or ""),
            "status": status, "evidence_refs": [vars(ref) for ref in refs],
        }
        warnings = []
        if not refs: warnings.append("风险判断缺少证据")
        if artifact["probability"] is None or not artifact["impact"]: warnings.append("风险概率或影响未评估")
        if not artifact["owners"]: warnings.append("风险责任人未确认")
        return SkillResult("C07", "risk_analysis", "RiskObject", artifact,
            candidates=[] if status == "confirmed" else [{"risk_id": artifact["risk_id"], "title": title}],
            evidence_refs=[vars(ref) for ref in refs], warnings=warnings,
            requires_confirmation=status != "confirmed" or bool(warnings),
            audit={"measure_count": len(artifact["measures"]), "affected_count": len(artifact["affected_object_ids"])})
