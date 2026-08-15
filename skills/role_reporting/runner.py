from typing import Any

from skills.artifacts import EvidenceLink, ReportPackageArtifact
from skills.contracts import SkillContext, SkillResult


class RoleReportingSkillRunner:
    def run(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult:
        role = str(payload.get("audience_role") or "")
        if role not in {"pmo", "pm", "professional_lead", "topic_lead", "exec"}:
            raise ValueError("C14 需要有效 audience_role")
        refs = [EvidenceLink.from_dict(item) for item in payload.get("evidence_refs") or []]
        artifact = ReportPackageArtifact(
            report_type=str(payload.get("report_type") or "status_report"), audience_role=role,
            period=str(payload.get("period") or ""), summary=[str(item) for item in payload.get("summary") or []],
            progress=list(payload.get("progress") or []), risks=list(payload.get("risks") or []),
            decisions=list(payload.get("decisions") or []), next_actions=list(payload.get("next_actions") or []),
            evidence_refs=refs)
        warnings = [] if refs else ["汇报材料缺少可追溯证据"]
        return SkillResult("C14", "role_reporting", "ReportPackage", artifact.as_dict(),
            evidence_refs=[{"source_id": r.source_id, "locator": r.locator, "quote": r.quote} for r in refs],
            warnings=warnings, requires_confirmation=bool(payload.get("external") or warnings),
            audit={"audience_role": role, "summary_count": len(artifact.summary)})
