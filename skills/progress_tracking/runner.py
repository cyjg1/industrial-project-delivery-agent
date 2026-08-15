from typing import Any

from skills.artifacts import EvidenceLink, ProgressEvidenceArtifact
from skills.contracts import SkillContext, SkillResult


class ProgressTrackingSkillRunner:
    def run(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult:
        task_id = str(payload.get("task_id") or "")
        status = str(payload.get("inferred_status") or "unknown")
        if not task_id:
            raise ValueError("C06 需要 task_id")
        warnings = []
        if status == "blocked":
            status = "in_progress"
            warnings.append("blocked 不作为任务状态；已保留为进行中，阻碍仅记录到 blockers、风险或依赖对象。")
        refs = [EvidenceLink.from_dict(item) for item in payload.get("evidence_refs") or []]
        confidence = float(payload["confidence"]) if payload.get("confidence") is not None else None
        artifact = ProgressEvidenceArtifact(
            task_id=task_id,
            inferred_status=status,
            completion_percent=float(payload["completion_percent"]) if payload.get("completion_percent") is not None else None,
            latest_progress=str(payload.get("latest_progress") or ""),
            next_step=str(payload.get("next_step") or ""),
            blockers=[str(item) for item in payload.get("blockers") or []],
            evidence_refs=refs,
            confidence=confidence,
            observed_at=str(payload.get("observed_at") or ""),
        )
        if not refs:
            warnings.append("进展推断缺少证据")
        if confidence is None:
            warnings.append("进展推断缺少置信度")
        return SkillResult("C06", "progress_tracking", "ProgressEvidence", artifact.as_dict(),
            evidence_refs=[{"source_id": r.source_id, "locator": r.locator, "quote": r.quote} for r in refs],
            confidence=confidence, warnings=warnings, requires_confirmation=True,
            audit={
                "evidence_count": len(refs),
                "blocker_count": len(artifact.blockers),
                "blocked_status_supported": False,
                "status_sequence_enforced": False,
                "status_edit_scope": "current_owners_only",
            })
