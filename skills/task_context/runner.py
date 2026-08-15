from __future__ import annotations

from typing import Any

from skills.artifacts import TaskContextPackage, TaskNode
from skills.contracts import SkillContext, SkillResult


class TaskContextSkillRunner:
    def run(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult:
        raw_task = dict(payload.get("task") or {})
        task = TaskNode.from_dict(raw_task)
        if not task.task_id or not task.title:
            raise ValueError("C05 需要包含 task_id 和 title 的 task")
        missing_inputs = [str(item) for item in payload.get("missing_inputs") or []]
        if not task.inputs:
            missing_inputs.append("任务输入未定义")
        if not task.outputs:
            missing_inputs.append("任务输出未定义")
        if not task.acceptance_criteria:
            missing_inputs.append("完成标准未定义")
        if not task.owners:
            missing_inputs.append("责任人未确认")
        missing_inputs = list(dict.fromkeys(missing_inputs))
        package = TaskContextPackage(
            task=task,
            background=str(payload.get("background") or ""),
            current_conclusions=list(payload.get("current_conclusions") or []),
            materials=list(payload.get("materials") or []),
            related_people=list(payload.get("related_people") or []),
            templates=list(payload.get("templates") or []),
            risks=list(payload.get("risks") or []),
            pending_confirmations=list(payload.get("pending_confirmations") or []),
            missing_inputs=missing_inputs,
            next_steps=[str(item) for item in payload.get("next_steps") or []],
        )
        evidence_refs = [
            dict(item)
            for group in (package.current_conclusions, package.materials, package.risks)
            for item in group
            if isinstance(item, dict) and (item.get("source_id") or item.get("source_doc_id"))
        ]
        return SkillResult(
            capability_id="C05",
            skill_name="task_context",
            artifact_type="TaskContextPackage",
            artifact=package.as_dict(),
            evidence_refs=evidence_refs,
            warnings=missing_inputs,
            requires_confirmation=bool(missing_inputs or package.pending_confirmations),
            audit={"material_count": len(package.materials), "missing_input_count": len(missing_inputs)},
        )
