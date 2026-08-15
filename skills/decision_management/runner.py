from __future__ import annotations

from typing import Any

from skills.artifacts import DecisionItemArtifact, EvidenceLink
from skills.contracts import SkillContext, SkillResult


class DecisionManagementSkillRunner:
    def run(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult:
        question = str(payload.get("question") or "").strip()
        if not question:
            raise ValueError("C09 需要 question")
        status = str(payload.get("status") or "candidate")
        evidence_refs = [EvidenceLink.from_dict(item) for item in payload.get("evidence_refs") or []]
        item = DecisionItemArtifact(
            decision_id=str(payload.get("decision_id") or ""),
            question=question,
            decision_level=str(payload.get("decision_level") or "待判断"),
            decision_makers=[str(value) for value in payload.get("decision_makers") or []],
            participants=[str(value) for value in payload.get("participants") or []],
            options=list(payload.get("options") or []),
            due_date=str(payload.get("due_date") or ""),
            affected_object_ids=[str(value) for value in payload.get("affected_object_ids") or []],
            status=status,
            final_conclusion=str(payload.get("final_conclusion") or ""),
            evidence_refs=evidence_refs,
        )
        warnings = []
        if not item.decision_makers:
            warnings.append("决策人未确认")
        if not item.options and status != "confirmed":
            warnings.append("尚未整理可选方案")
        if not item.due_date and status != "confirmed":
            warnings.append("决策截止时间未确认")
        if not evidence_refs:
            warnings.append("决策事项缺少证据来源")
        if status == "confirmed" and not item.final_conclusion:
            warnings.append("已确认决策缺少最终结论")

        return SkillResult(
            capability_id="C09",
            skill_name="decision_management",
            artifact_type="DecisionItem",
            artifact=item.as_dict(),
            candidates=[] if status == "confirmed" else [{"decision_id": item.decision_id, "question": question}],
            evidence_refs=[
                {"source_id": ref.source_id, "locator": ref.locator, "quote": ref.quote, "version": ref.version}
                for ref in evidence_refs
            ],
            warnings=warnings,
            requires_confirmation=status != "confirmed" or bool(warnings),
            audit={"option_count": len(item.options), "affected_object_count": len(item.affected_object_ids)},
        )
