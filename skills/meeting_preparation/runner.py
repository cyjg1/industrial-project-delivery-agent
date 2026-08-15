from typing import Any

from skills.contracts import SkillContext, SkillResult


class MeetingPreparationSkillRunner:
    def run(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult:
        objective = str(payload.get("objective") or "").strip()
        if not objective:
            raise ValueError("C03 需要 objective")
        participants = list(payload.get("recommended_participants") or [])
        materials = list(payload.get("materials") or [])
        artifact = {"meeting_id": str(payload.get("meeting_id") or ""), "objective": objective,
            "topics": list(payload.get("topics") or []), "recommended_participants": participants,
            "decision_makers": list(payload.get("decision_makers") or []), "materials": materials,
            "pre_meeting_tasks": list(payload.get("pre_meeting_tasks") or []),
            "role_packages": dict(payload.get("role_packages") or {}), "visibility_scope": str(payload.get("visibility_scope") or "inherit")}
        warnings = []
        if not artifact["topics"]: warnings.append("会议议题未明确")
        if not participants: warnings.append("必要参会角色未识别")
        if artifact["visibility_scope"] != "inherit": warnings.append("会议信息包不得默认扩大原材料可见范围")
        return SkillResult("C03", "meeting_preparation", "MeetingPlan", artifact,
            candidates=[{"meeting_id": artifact["meeting_id"], "objective": objective}], warnings=warnings,
            requires_confirmation=True, audit={"participant_count": len(participants), "material_count": len(materials)})
