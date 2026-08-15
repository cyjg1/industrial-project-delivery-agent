from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Iterable, List

from agent.schemas import PeopleGroup, PeopleStructure, Person, ScenarioOrg
from ingestion.people_asset import load_people_asset, save_people_asset


GROUP_TITLES = {
    "PMO",
    "总体组",
    "分项实施组",
    "1-原料组",
    "2- 铁区组",
    "3-炼钢组",
    "4-热轧组",
    "5-冷轧组",
    "UAT联调测试场景组织",
    "数据架构组织",
}

ROLE_HINTS = (
    "负责人",
    "业务负责人",
    "技术负责人",
    "业务架构师团队",
    "技术架构师团队",
    "总设计师",
    "测试负责人",
    "主导人员",
    "团队成员",
)

STRUCTURAL_TITLE_HINTS = (
    "PMO",
    "团队",
    "负责人",
    "主管领导",
    "经理",
    "架构",
    "管理",
    "设计",
    "开发",
    "实施",
    "测试",
    "自动化",
    "计算机",
    "服务",
    "总体",
    "原料",
    "铁区",
    "炼钢",
    "热轧",
    "冷轧",
    "仓储",
    "能源",
    "成本",
    "物流",
    "设备",
    "计划",
    "质量",
    "环保",
    "生产实绩",
    "安全",
    "平台",
    "数据采集",
    "数据消费",
    "物料",
    "主导人员",
)

ROLE_CONTEXT_HINTS = (*ROLE_HINTS, "主管领导", "项目经理", "报价经理")


def load_people_structure(xmind_path: str | None = None) -> PeopleStructure:
    if not xmind_path:
        return load_people_asset()

    path = Path(xmind_path)
    if not path.exists():
        return load_people_asset()

    return load_people_structure_from_xmind(path)


def load_people_structure_from_xmind(xmind_path: str | Path) -> PeopleStructure:
    root = _load_root_topic(Path(xmind_path))
    paths = list(_walk(root, []))

    groups = _extract_groups(paths)
    scenarios = _extract_scenarios(paths)
    people = _extract_people(paths, scenarios)

    return PeopleStructure(
        root_title=root.get("title", ""),
        groups=groups,
        people=people,
        scenarios=scenarios,
    )


def refresh_people_asset_from_xmind(xmind_path: str | Path) -> PeopleStructure:
    structure = load_people_structure_from_xmind(xmind_path)
    save_people_asset(
        structure,
        source_type="xmind_import",
        source_path=str(xmind_path),
        notes="Imported into the canonical people asset for meeting understanding, responsibility matching, and task assignment.",
    )
    return structure


def _load_root_topic(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        with archive.open("content.json") as content:
            sheets = json.loads(content.read().decode("utf-8"))
    return sheets[0]["rootTopic"]


def _walk(topic: dict, parents: list[str]) -> Iterable[list[str]]:
    title = _clean_title(topic.get("title", ""))
    path = parents + ([title] if title else [])
    yield path
    children = topic.get("children", {})
    for child in children.get("attached", []):
        yield from _walk(child, path)
    for child in children.get("summary", []):
        yield from _walk(child, path)


def _extract_groups(paths: list[list[str]]) -> List[PeopleGroup]:
    seen = set()
    groups: list[PeopleGroup] = []
    for path in paths:
        title = path[-1]
        if title in GROUP_TITLES and title not in seen:
            seen.add(title)
            groups.append(PeopleGroup(name=title, path=" / ".join(path)))
    return groups


def _extract_scenarios(paths: list[list[str]]) -> List[ScenarioOrg]:
    scenario_paths = [path for path in paths if _scenario_match(path[-1])]
    scenarios: list[ScenarioOrg] = []
    for scenario_path in scenario_paths:
        scenario_title = scenario_path[-1]
        scenario_id, name = _parse_scenario_title(scenario_title)
        prefix_len = len(scenario_path)
        descendants = [
            path for path in paths
            if len(path) > prefix_len and path[:prefix_len] == scenario_path
        ]
        teams: list[str] = []
        leads: list[str] = []
        for path in descendants:
            title = path[-1]
            if title.startswith("涉及团队"):
                teams = _split_names(title.split("：", 1)[-1])
            if "主导人员" in path:
                candidate = title
                if _looks_like_person(candidate, path):
                    leads.extend(_split_names(candidate))
        scenarios.append(
            ScenarioOrg(
                scenario_id=scenario_id,
                name=name,
                teams=_unique(teams),
                leads=_unique(leads),
                path=" / ".join(scenario_path),
            )
        )
    return sorted(scenarios, key=lambda item: item.scenario_id)


def _extract_people(paths: list[list[str]], scenarios: list[ScenarioOrg]) -> List[Person]:
    people: list[Person] = []
    seen = set()
    scenario_leads = {lead: scenario.scenario_id for scenario in scenarios for lead in scenario.leads}
    for path in paths:
        title = path[-1]
        if not _looks_like_person(title, path):
            continue
        group = _nearest_group(path)
        role = _nearest_role(path)
        if title in scenario_leads:
            group = "UAT联调测试场景组织"
            role = f"{scenario_leads[title]}主导人员"
        key = (title, group, role)
        if key in seen:
            continue
        seen.add(key)
        people.append(Person(name=title, group=group, role=role, path=" / ".join(path)))
    return people


def _parse_scenario_title(title: str) -> tuple[str, str]:
    match = re.match(r"^(S\d+)(?:场景)?[：:](.+)$", title)
    if not match:
        raise ValueError(f"Unsupported scenario title: {title}")
    return match.group(1), match.group(2).strip()


def _scenario_match(title: str) -> bool:
    return bool(re.match(r"^S\d+(?:场景)?[：:]", title))


def _nearest_group(path: list[str]) -> str:
    for title in reversed(path[:-1]):
        if title in GROUP_TITLES or re.match(r"^\d+-", title):
            return title
    return path[1] if len(path) > 1 else ""


def _nearest_role(path: list[str]) -> str:
    for title in reversed(path[:-1]):
        if any(hint in title for hint in ROLE_HINTS):
            return title
    return path[-2] if len(path) > 1 else ""


def _looks_like_person(title: str, path: list[str] | None = None) -> bool:
    if not title or title in {"？", "..."}:
        return False
    if not re.fullmatch(r"[\u3400-\u9fff]{2,4}", title):
        return False
    if any(hint in title for hint in STRUCTURAL_TITLE_HINTS):
        return False
    if path and not any(any(hint in ancestor for hint in ROLE_CONTEXT_HINTS) for ancestor in path[:-1]):
        return False
    return True


def _split_names(text: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[、,/，\s]+", text)
        if item.strip() and item.strip() != "？"
    ]


def _clean_title(title: str) -> str:
    return title.strip().replace("\u3000", " ")


def _unique(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
