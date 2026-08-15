from agent.project_config import generic_acceptance_criteria
from agent.schemas import MilestonePlan


def s3_material_entry_milestone() -> MilestonePlan:
    """Legacy project data kept only as an explicit regression fixture."""
    return MilestonePlan(
        milestone_id="rf_uat_s3_material_entry_20260617",
        project="示例钢厂 UAT 拉通",
        name="S3 铁前 / 物料进厂 UAT链路巡检",
        date_start="2026-05-30",
        date_end="2026-06-17",
        scenario_id="S3",
        chain_name="铁前 / 物料进厂",
        acceptance_criteria=generic_acceptance_criteria(),
        trigger_policy={
            "type": "manual",
            "schedule": None,
            "notes": "回归测试手动触发。",
        },
    )
