from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agent.env_loader import PROJECT_ROOT, load_project_env
from agent.schemas import MilestonePlan, to_plain


DEFAULT_PROJECT_CONFIG_PATH = PROJECT_ROOT / "data" / "project_config.json"


@dataclass(frozen=True)
class ProjectConfig:
    project_id: str
    project_name: str
    default_milestone: MilestonePlan
    source_profile: str = "generic"


def generic_acceptance_criteria() -> list[str]:
    return [
        "允许零缺口，不得为满足数量制造问题",
        "有效结果必须是可行动结论，或已转为人工可处理下一步的具体缺口",
        "事项先按设计、功能、架构分类，再标注决策、风险、依赖、问题、任务和事实",
        "所有人、事、法、待办必须有 evidence_refs，并标明原始转写覆盖状态",
        "待办必须包含责任人、截止日期、交付物和验收口径",
        "方法论必须包含业务目标、原则、推理链、适用范围和证据",
        "verification 通过前不得写候选记忆",
    ]


def generic_project_config() -> ProjectConfig:
    return ProjectConfig(
        project_id="generic_project",
        project_name="通用项目交付巡检",
        source_profile="generic",
        default_milestone=MilestonePlan(
            milestone_id="generic_delivery_inspection",
            project="通用项目交付巡检",
            name="通用项目交付链路巡检",
            date_start="未设定",
            date_end="未设定",
            scenario_id="GENERAL",
            chain_name="项目交付链路",
            acceptance_criteria=generic_acceptance_criteria(),
            trigger_policy={
                "type": "manual",
                "schedule": None,
                "notes": "通用项目默认手动触发；实际项目可通过 data/project_config.json 覆盖。",
            },
        ),
    )


def configured_project_config_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    configured = os.getenv("PROJECT_AGENT_CONFIG_PATH", "").strip()
    return Path(configured) if configured else DEFAULT_PROJECT_CONFIG_PATH


def load_project_config(path: str | Path | None = None) -> ProjectConfig:
    load_project_env()
    config_path = configured_project_config_path(path)
    if not config_path.exists():
        return generic_project_config()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    return project_config_from_dict(payload)


def save_project_config(config: ProjectConfig, path: str | Path | None = None) -> None:
    config_path = configured_project_config_path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "project_id": config.project_id,
        "project_name": config.project_name,
        "source_profile": config.source_profile,
        "default_milestone": to_plain(config.default_milestone),
    }
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def update_default_milestone_config(updates: dict[str, Any], path: str | Path | None = None) -> ProjectConfig:
    config = load_project_config(path)
    milestone = config.default_milestone
    allowed_fields = {
        "milestone_id",
        "project",
        "name",
        "date_start",
        "date_end",
        "scenario_id",
        "chain_name",
        "acceptance_criteria",
        "trigger_policy",
        "status",
    }
    normalized: dict[str, Any] = {}
    for key, value in updates.items():
        if key not in allowed_fields or value is None:
            continue
        if key == "acceptance_criteria":
            normalized[key] = _normalize_acceptance_criteria(value)
        elif key == "trigger_policy":
            normalized[key] = dict(value)
        else:
            normalized[key] = str(value)
    next_milestone = replace(milestone, **normalized)
    next_config = replace(
        config,
        project_name=next_milestone.project or config.project_name,
        default_milestone=next_milestone,
    )
    save_project_config(next_config, path=path)
    return next_config


def project_config_from_dict(payload: dict[str, Any]) -> ProjectConfig:
    default = generic_project_config()
    milestone_payload = payload.get("default_milestone") or {}
    milestone = _milestone_from_config(
        milestone_payload,
        project_name=payload.get("project_name") or default.project_name,
    )
    return ProjectConfig(
        project_id=str(payload.get("project_id") or default.project_id),
        project_name=str(payload.get("project_name") or milestone.project or default.project_name),
        source_profile=str(payload.get("source_profile") or "generic"),
        default_milestone=milestone,
    )


def initialize_project_workspace(
    *,
    root_dir: str | Path,
    project_id: str,
    project_name: str,
    milestone_id: str,
    milestone_name: str,
    scenario_id: str,
    chain_name: str,
    date_start: str = "",
    date_end: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    root = Path(root_dir)
    data_dir = root / "data"
    from skills.meeting_minutes.store import MeetingMinutesSkillStore
    from store.sqlite_store import ProjectSQLiteStore

    store = ProjectSQLiteStore(str(data_dir / "store"))
    if store.get_project(project_id) is not None and not overwrite:
        raise FileExistsError(f"项目已存在于 SQLite：{project_id}")

    milestone = MilestonePlan(
        milestone_id=milestone_id,
        project=project_name,
        name=milestone_name,
        date_start=date_start,
        date_end=date_end,
        scenario_id=scenario_id,
        chain_name=chain_name,
        acceptance_criteria=generic_acceptance_criteria(),
        trigger_policy={"type": "manual", "schedule": None},
    )
    org_id = "org_local"
    owner_id = "u_local_pm"
    store.upsert_org(org_id, "Local Organization")
    store.upsert_user(owner_id, org_id, "Local Project Manager")
    store.upsert_project(project_id, org_id, project_name, owner_id)
    store.upsert_project_member(project_id, owner_id, "pm")
    store.upsert_project_config(
        project_id,
        org_id=org_id,
        source_profile="generic",
        milestone_payload=to_plain(milestone),
        actor_id=owner_id,
        migration_origin="workspace_initializer",
    )
    storage_files = store.memory_projection()["storage_files"]
    meeting_store = MeetingMinutesSkillStore(data_dir / "skills" / "meeting_minutes")
    upload_dir = data_dir / "sources" / "uploads"
    asset_upload_dir = data_dir / "assets" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    asset_upload_dir.mkdir(parents=True, exist_ok=True)

    return {
        "project_id": project_id,
        "project_name": project_name,
        "created_paths": {
            "project_authority": f"{store.database_path}#project_configs/{project_id}",
            "store_dir": str(store.root_dir),
            "database_path": storage_files["database"],
            "vault_dir": storage_files["vault_dir"],
            "meeting_minutes_skill_dir": meeting_store.storage_files()["directory"],
            "source_upload_dir": str(upload_dir),
            "asset_upload_dir": str(asset_upload_dir),
        },
        "required_inputs": [
            "data/assets/people_structure.json",
            "data/sources/uploads 或 OBSIDIAN_VAULT_PATH",
            "LLM_PROVIDER 与 OpenAI/GLM 兼容真实模型配置",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化一个通用项目交付 Agent 工作区。")
    parser.add_argument("--root", default=str(PROJECT_ROOT), help="项目根目录，默认当前仓库根目录。")
    parser.add_argument("--project-id", required=True, help="项目编号，例如 customer_success_platform。")
    parser.add_argument("--project-name", required=True, help="项目名称。")
    parser.add_argument("--milestone-id", required=True, help="默认里程碑编号。")
    parser.add_argument("--milestone-name", required=True, help="默认里程碑名称。")
    parser.add_argument("--scenario-id", default="GENERAL", help="默认场景编号。")
    parser.add_argument("--chain-name", default="项目交付链路", help="默认链路名称。")
    parser.add_argument("--date-start", default="", help="默认材料开始日期。")
    parser.add_argument("--date-end", default="", help="默认材料结束日期。")
    parser.add_argument("--overwrite", action="store_true", help="覆盖 SQLite 中已有的同 ID 项目配置。")
    args = parser.parse_args()

    report = initialize_project_workspace(
        root_dir=args.root,
        project_id=args.project_id,
        project_name=args.project_name,
        milestone_id=args.milestone_id,
        milestone_name=args.milestone_name,
        scenario_id=args.scenario_id,
        chain_name=args.chain_name,
        date_start=args.date_start,
        date_end=args.date_end,
        overwrite=args.overwrite,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _milestone_from_config(payload: dict[str, Any], *, project_name: str) -> MilestonePlan:
    default = generic_project_config().default_milestone
    return MilestonePlan(
        milestone_id=str(payload.get("milestone_id") or default.milestone_id),
        project=str(payload.get("project") or project_name),
        name=str(payload.get("name") or default.name),
        date_start=str(payload.get("date_start") or default.date_start),
        date_end=str(payload.get("date_end") or default.date_end),
        scenario_id=str(payload.get("scenario_id") or default.scenario_id),
        chain_name=str(payload.get("chain_name") or default.chain_name),
        acceptance_criteria=list(payload.get("acceptance_criteria") or generic_acceptance_criteria()),
        trigger_policy=dict(payload.get("trigger_policy") or default.trigger_policy),
        status=str(payload.get("status") or "active"),
    )


def _normalize_acceptance_criteria(value: Any) -> list[str]:
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return generic_acceptance_criteria()


if __name__ == "__main__":
    raise SystemExit(main())
