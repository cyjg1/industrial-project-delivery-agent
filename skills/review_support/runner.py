from typing import Any

from skills.contracts import SkillContext, SkillResult


class ReviewSupportSkillRunner:
    def run(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult:
        object_id = str(payload.get("review_object_id") or "")
        if not object_id:
            raise ValueError("C11 需要 review_object_id")
        findings = list(payload.get("findings") or [])
        warnings = []
        for finding in findings:
            if not finding.get("owner") or not finding.get("due_date") or not finding.get("close_criteria"):
                warnings.append(f"评审意见 {finding.get('finding_id') or finding.get('title') or ''} 缺少责任、期限或关闭标准")
            if finding.get("status") == "closed" and not finding.get("reviewer_confirmation"):
                warnings.append(f"评审意见 {finding.get('finding_id') or ''} 未经复核人确认关闭")
        artifact = {"review_id": str(payload.get("review_id") or ""), "review_object_id": object_id,
            "version": str(payload.get("version") or ""), "checklist_ids": list(payload.get("checklist_ids") or []),
            "findings": findings, "reviewers": list(payload.get("reviewers") or []), "status": str(payload.get("status") or "candidate")}
        return SkillResult("C11", "review_support", "ReviewPackage", artifact,
            candidates=[{"finding_id": item.get("finding_id"), "title": item.get("title")} for item in findings if item.get("status") != "closed"],
            evidence_refs=[ref for item in findings for ref in item.get("evidence_refs") or []], warnings=warnings,
            requires_confirmation=bool(findings or warnings), audit={"finding_count": len(findings)})
