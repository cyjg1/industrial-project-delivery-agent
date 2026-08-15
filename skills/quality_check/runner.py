from typing import Any

from skills.contracts import SkillContext, SkillResult


class QualityCheckSkillRunner:
    def run(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult:
        deliverable_id = str(payload.get("deliverable_id") or "")
        if not deliverable_id:
            raise ValueError("C12 需要 deliverable_id")
        findings = list(payload.get("findings") or [])
        warnings = []
        for finding in findings:
            kind = str(finding.get("finding_kind") or "")
            if kind not in {"deterministic", "semantic"}: warnings.append("质量发现未区分确定性规则或语义推断")
            if not finding.get("evidence_refs"): warnings.append(f"质量发现 {finding.get('finding_id') or finding.get('title') or ''} 缺少证据")
        artifact = {"deliverable_id": deliverable_id, "version": str(payload.get("version") or ""),
            "standard_ids": [str(item) for item in payload.get("standard_ids") or []], "findings": findings,
            "checked_at": str(payload.get("checked_at") or "")}
        semantic = [item for item in findings if item.get("finding_kind") == "semantic"]
        return SkillResult("C12", "quality_check", "QualityFindings", artifact,
            candidates=[{"finding_id": item.get("finding_id"), "title": item.get("title")} for item in findings],
            evidence_refs=[ref for item in findings for ref in item.get("evidence_refs") or []], warnings=warnings,
            requires_confirmation=bool(semantic or warnings), audit={"finding_count": len(findings), "semantic_count": len(semantic)})
