from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.schemas import (  # noqa: E402
    CandidateStatus,
    EvidenceRef,
    InspectionItem,
    MilestonePlan,
    PeopleGroup,
    PeopleStructure,
    Person,
    SourceDocument,
    SourceRef,
    to_plain,
)
from ingestion.people_asset import save_people_asset  # noqa: E402
from store.sqlite_store import ProjectSQLiteStore  # noqa: E402


DATASET_PATH = PROJECT_ROOT / "demo" / "demo_dataset.json"
FIXTURE_DIR = PROJECT_ROOT / "demo" / "fixtures"
STORE_DIR = PROJECT_ROOT / "data" / "store"


def initialize_demo(store_dir: str | Path = STORE_DIR) -> dict[str, Any]:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    if dataset.get("metadata", {}).get("synthetic") is not True:
        raise RuntimeError("Demo dataset must be explicitly marked synthetic.")

    store = ProjectSQLiteStore(str(store_dir))
    project = dataset["project"]
    if store.get_project(project["id"]) is not None:
        return {
            "initialized": False,
            "reason": "already_exists",
            "database": str(store.database_path),
        }

    organization = dataset["organization"]
    store.upsert_org(organization["id"], organization["name"])
    for user in dataset["users"]:
        store.upsert_user(user["id"], organization["id"], user["name"])
    store.upsert_project(
        project["id"],
        organization["id"],
        project["name"],
        project["owner_id"],
    )
    for user in dataset["users"]:
        store.upsert_project_member(
            project["id"],
            user["id"],
            user["role"],
            manager_id=user.get("manager_id"),
        )
    for topic in dataset["topics"]:
        store.upsert_topic(topic["id"], project["id"], topic["name"], topic["owner_id"])
        for user_id in topic["members"]:
            store.upsert_topic_member(topic["id"], user_id)

    today = date.today()
    milestone_data = dataset["milestone"]
    milestone = MilestonePlan(
        milestone_id=milestone_data["id"],
        project=project["name"],
        name=milestone_data["name"],
        date_start=_offset_date(today, milestone_data["start_offset"]),
        date_end=_offset_date(today, milestone_data["end_offset"]),
        scenario_id=milestone_data["scenario_id"],
        chain_name=milestone_data["chain_name"],
        acceptance_criteria=list(milestone_data["acceptance_criteria"]),
        trigger_policy={"type": "manual", "schedule": None},
    )
    store.upsert_project_config(
        project["id"],
        org_id=organization["id"],
        source_profile="synthetic_demo",
        milestone_payload=to_plain(milestone),
        actor_id="u_pmo",
        migration_origin="competition_demo_seed",
    )

    people_path = _seed_people(store, dataset)
    source_paths = _seed_meetings(store, dataset, today)
    task_count = _seed_items(store, dataset, milestone, today)
    store.generate_vault_mirror()
    return {
        "initialized": True,
        "synthetic": True,
        "database": str(store.database_path),
        "people_asset": str(people_path),
        "meeting_sources": [str(path) for path in source_paths],
        "published_tasks": task_count,
    }


def _seed_people(store: ProjectSQLiteStore, dataset: dict[str, Any]) -> Path:
    people = PeopleStructure(
        root_title="星河钢铁协同平台演示项目组织",
        groups=[
            PeopleGroup(name="PMO", path="演示项目 / PMO"),
            PeopleGroup(name="总体组", path="演示项目 / 总体组"),
            PeopleGroup(name="物流", path="演示项目 / 物流"),
            PeopleGroup(name="质量", path="演示项目 / 质量"),
        ],
        people=[
            Person("项目经理A", "PMO", "项目经理", "演示项目 / PMO / 项目经理A", "总体决策与资源协调"),
            Person("PMO协调A", "PMO", "PMO", "演示项目 / PMO / PMO协调A", "计划、风险与证据治理"),
            Person("专业统筹A", "总体组", "专业统筹", "演示项目 / 总体组 / 专业统筹A", "跨专题方案一致性"),
            Person("物流负责人A", "物流", "专题负责人", "演示项目 / 物流 / 物流负责人A", "物流流程与接口责任"),
            Person("实施工程师A", "物流", "实施人员", "演示项目 / 物流 / 实施工程师A", "接口开发和字段映射"),
            Person("质量负责人A", "质量", "专题负责人", "演示项目 / 质量 / 质量负责人A", "质量规则与UAT组织"),
            Person("测试工程师A", "质量", "测试人员", "演示项目 / 质量 / 测试工程师A", "测试用例和证据执行"),
        ],
        scenarios=[],
    )
    people_path = store.root_dir / "project_assets" / "project_mvp" / "people_structure.json"
    save_people_asset(
        people,
        asset_path=people_path,
        asset_id="synthetic_demo_people",
        source_type="synthetic_demo",
        notes="Fictional role-based organization generated for competition evaluation.",
    )
    source = SourceDocument(
        doc_id="demo_people_structure",
        title="演示项目组织与角色",
        meeting_date="",
        topic="组织架构",
        curated_source=SourceRef("Synthetic people asset", str(people_path), "matched"),
        raw_source=SourceRef("Synthetic people asset", str(people_path), "matched"),
        tags=["synthetic", "people_structure"],
        org_id="org_mvp",
        project_id="project_mvp",
        author_id="u_pmo",
        sensitivity="l1",
        tag_origin="synthetic_demo",
        input_kind="people_structure",
    )
    store.ingest(
        source,
        tags=_tags(author_id="u_pmo"),
        actor={"id": "u_pmo"},
        kind="people_structure",
        materialize=False,
        tag_origin="synthetic_demo",
    )
    return people_path


def _seed_meetings(
    store: ProjectSQLiteStore,
    dataset: dict[str, Any],
    today: date,
) -> list[Path]:
    runtime_dir = store.root_dir / "demo_sources"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for meeting in dataset["meetings"]:
        content = (FIXTURE_DIR / meeting["file"]).read_bytes()
        target = runtime_dir / meeting["file"]
        target.write_bytes(content)
        source = SourceDocument(
            doc_id=meeting["id"],
            title=meeting["title"],
            meeting_date=_offset_date(today, meeting["date_offset"]),
            topic=meeting["topic"],
            curated_source=SourceRef("Synthetic meeting minutes", str(target), "matched"),
            raw_source=SourceRef(
                "Synthetic raw source",
                None,
                "not_applicable",
                "The public demo was authored as synthetic minutes and has no raw customer transcript.",
            ),
            tags=["synthetic", "competition_demo"],
            org_id="org_mvp",
            project_id="project_mvp",
            topic_id=meeting["topic_id"],
            author_id="u_pmo",
            sensitivity="l1",
            tag_origin="synthetic_demo",
            input_kind="minutes",
            content_hash=hashlib.sha256(content).hexdigest(),
        )
        store.ingest(
            source,
            tags=_tags(author_id="u_pmo", topic_id=meeting["topic_id"]),
            actor={"id": "u_pmo"},
            kind="minutes",
            materialize=False,
            tag_origin="synthetic_demo",
        )
        paths.append(target)
    return paths


def _seed_items(
    store: ProjectSQLiteStore,
    dataset: dict[str, Any],
    milestone: MilestonePlan,
    today: date,
) -> int:
    published = 0
    for row in dataset["tasks"]:
        item = _item_from_row(row, "task", today, CandidateStatus.CONFIRMED)
        store.ingest(
            item,
            tags=_tags(author_id="u_pmo", topic_id=row["topic_id"]),
            actor={"id": "u_pmo"},
            materialize=False,
            tag_origin="synthetic_demo",
        )
        work_item = store.publish_item_as_work_item(
            row["id"],
            editor="u_pmo",
            notes="Synthetic competition demo task.",
            milestone_id=milestone.milestone_id,
        )
        store.update_work_item_fields(
            work_item.work_item_id,
            status=row["status"],
            planned_start=_offset_date(today, row["start_offset"]),
            progress_percent=row["progress"],
            editor="u_pmo",
            notes="Synthetic competition demo progress.",
        )
        published += 1

    for row in dataset["issues"]:
        store.ingest(
            _item_from_row(row, "issue", today, CandidateStatus.CANDIDATE),
            tags=_tags(author_id="u_pmo", topic_id=row["topic_id"]),
            actor={"id": "u_pmo"},
            materialize=False,
            tag_origin="synthetic_demo",
        )

    for row in dataset["methods"]:
        store.ingest(
            _item_from_row(row, "method", today, CandidateStatus.CONFIRMED),
            tags=_tags(author_id="u_pmo", topic_id=row["topic_id"]),
            actor={"id": "u_pmo"},
            materialize=False,
            tag_origin="synthetic_demo",
        )
    return published


def _item_from_row(
    row: dict[str, Any],
    category: str,
    today: date,
    status: CandidateStatus,
) -> InspectionItem:
    source_id = row["source_id"]
    quote = f"Synthetic demo evidence for {row['title']}"
    return InspectionItem(
        item_id=row["id"],
        category=category,
        title=row["title"],
        description=row["description"],
        evidence_refs=[
            EvidenceRef(
                source_doc_id=source_id,
                source_kind="synthetic_minutes",
                locator="demo_fixture",
                quote=quote,
                evidence_level="synthetic_demo",
            )
        ],
        status=status,
        owner_candidates=[row["owner"]] if row.get("owner") else [],
        due_date=_offset_date(today, row["due_offset"]) if "due_offset" in row else None,
        deliverable=row.get("deliverable"),
        acceptance_criteria=row.get("acceptance"),
        professional_id=row.get("profession", ""),
        board_id=row.get("board", ""),
        org_id="org_mvp",
        project_id="project_mvp",
        topic_id=row["topic_id"],
        author_id="u_pmo",
        sensitivity="l1",
        tag_origin="synthetic_demo",
    )


def _tags(*, author_id: str, topic_id: str | None = None) -> dict[str, Any]:
    return {
        "org_id": "org_mvp",
        "project_id": "project_mvp",
        "topic_id": topic_id,
        "author_id": author_id,
        "sensitivity": "l1",
    }


def _offset_date(anchor: date, offset: int) -> str:
    return (anchor + timedelta(days=int(offset))).isoformat()


def main() -> int:
    result = initialize_demo()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

