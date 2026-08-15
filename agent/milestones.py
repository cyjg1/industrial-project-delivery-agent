from __future__ import annotations

import json
from typing import Any

from agent.project_config import load_project_config
from agent.schemas import MilestonePlan, to_plain


def default_project_milestone() -> MilestonePlan:
    return load_project_config().default_milestone


def milestone_to_objective(milestone: MilestonePlan) -> str:
    return (
        f"巡检里程碑 {milestone.milestone_id}：{milestone.name}；"
        f"样本范围 {milestone.date_start} 至 {milestone.date_end}；"
        f"场景 {milestone.scenario_id}；链路 {milestone.chain_name}。"
    )


def milestone_to_json(milestone: MilestonePlan) -> str:
    return json.dumps(to_plain(milestone), ensure_ascii=False, indent=2)


def milestone_from_json(raw: str) -> MilestonePlan:
    data = json.loads(raw)
    return milestone_from_dict(data)


def milestone_from_dict(data: dict[str, Any]) -> MilestonePlan:
    return MilestonePlan(
        milestone_id=data["milestone_id"],
        project=data["project"],
        name=data["name"],
        date_start=data["date_start"],
        date_end=data["date_end"],
        scenario_id=data["scenario_id"],
        chain_name=data["chain_name"],
        acceptance_criteria=list(data.get("acceptance_criteria", [])),
        trigger_policy=dict(data.get("trigger_policy", {"type": "manual"})),
        status=data.get("status", "active"),
    )


def legacy_milestone_from_objective(objective: str) -> MilestonePlan:
    milestone = default_project_milestone()
    milestone.name = objective or milestone.name
    milestone.milestone_id = "legacy_objective_run"
    milestone.trigger_policy = {
        "type": "legacy",
        "schedule": None,
        "notes": "由 V0.2 旧执行目标字段迁移生成。",
    }
    return milestone
