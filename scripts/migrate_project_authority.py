from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.project_config import load_project_config  # noqa: E402
from agent.schemas import to_plain  # noqa: E402
from store.sqlite_store import ProjectSQLiteStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate legacy data/project_config.json into SQLite project authority."
    )
    parser.add_argument("--store-dir", required=True, help="Directory containing project.db.")
    parser.add_argument("--config", required=True, help="Legacy project_config.json path.")
    parser.add_argument("--actor-id", default="", help="Audit actor; defaults to the project's owner.")
    parser.add_argument("--project-id", default="", help="Target SQLite project ID; overrides the legacy file ID.")
    parser.add_argument("--project-name", default="", help="Authoritative project name.")
    parser.add_argument("--source-profile", default="", help="Authoritative source profile.")
    parser.add_argument("--milestone-id", default="", help="Authoritative milestone ID.")
    parser.add_argument("--milestone-name", default="", help="Authoritative milestone name.")
    parser.add_argument("--date-start", default="", help="Milestone start date.")
    parser.add_argument("--date-end", default="", help="Milestone end date.")
    parser.add_argument("--scenario-id", default="", help="Milestone scenario ID.")
    parser.add_argument("--chain-name", default="", help="Milestone delivery chain name.")
    parser.add_argument(
        "--acceptance-criterion",
        action="append",
        default=[],
        help="Repeat to replace the legacy milestone acceptance checklist.",
    )
    parser.add_argument("--apply", action="store_true", help="Apply changes. Without this flag the command is read-only.")
    args = parser.parse_args()

    config = load_project_config(args.config)
    store = ProjectSQLiteStore(args.store_dir)
    project_id = args.project_id or config.project_id
    project = store.get_project(project_id)
    if project is None:
        parser.error(f"project {project_id!r} must exist in SQLite before migration")
    project_name = args.project_name or config.project_name
    milestone = replace(
        config.default_milestone,
        milestone_id=args.milestone_id or config.default_milestone.milestone_id,
        project=project_name,
        name=args.milestone_name or config.default_milestone.name,
        date_start=args.date_start or config.default_milestone.date_start,
        date_end=args.date_end or config.default_milestone.date_end,
        scenario_id=args.scenario_id or config.default_milestone.scenario_id,
        chain_name=args.chain_name or config.default_milestone.chain_name,
        acceptance_criteria=(
            list(args.acceptance_criterion)
            if args.acceptance_criterion
            else config.default_milestone.acceptance_criteria
        ),
    )
    source_profile = args.source_profile or config.source_profile
    actor_id = args.actor_id or project["owner_id"]
    report = {
        "mode": "apply" if args.apply else "dry_run",
        "legacy_project_id": config.project_id,
        "project_id": project_id,
        "project_name_before": project["name"],
        "project_name_after": project_name,
        "milestone_id": milestone.milestone_id,
        "milestone_name": milestone.name,
        "date_start": milestone.date_start,
        "date_end": milestone.date_end,
        "scenario_id": milestone.scenario_id,
        "chain_name": milestone.chain_name,
        "acceptance_criteria": milestone.acceptance_criteria,
        "source_profile": source_profile,
    }

    if args.apply:
        store.upsert_project(
            project_id,
            project["org_id"],
            project_name,
            project["owner_id"],
        )
        store.upsert_project_config(
            project_id,
            org_id=project["org_id"],
            source_profile=source_profile,
            milestone_payload=to_plain(milestone),
            actor_id=actor_id,
            migration_origin="legacy_project_config",
        )

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
