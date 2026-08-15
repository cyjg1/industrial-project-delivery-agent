from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from agent.progress_dashboard import BOARDS, PROFESSIONS, infer_task_profession
from skills.artifacts import TaskGraphArtifact, TaskNode
from skills.contracts import SkillContext, SkillResult


TASK_STATUS_POLICY = {
    "published_initial_status": "open",
    "formal_statuses": ["open", "in_progress", "pending_acceptance", "done", "canceled"],
    "blocked_status_supported": False,
    "status_sequence_enforced": False,
    "status_edit_scope": "current_owners_only",
}


class TaskPlanningSkillRunner:
    def run(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult:
        project_id = str(payload.get("project_id") or (context.project_id if context else ""))
        goal = str(payload.get("goal") or "").strip()
        if not project_id or not goal:
            raise ValueError("C04 需要 project_id 和 goal")
        raw_tasks = list(payload.get("tasks") or [])
        extraction_warnings: list[str] = []
        extraction_audit: dict[str, Any] = {}
        meeting_markdown = str(payload.get("meeting_markdown") or payload.get("source_text") or "")
        if meeting_markdown:
            raw_tasks, extraction_warnings, extraction_audit = _tasks_from_meeting(
                meeting_markdown,
                source_id=str(payload.get("source_id") or ""),
                people_structure=payload.get("people_structure"),
                fallback_owner=str(payload.get("fallback_owner") or "项目协调人A"),
            )
        if not raw_tasks:
            raise ValueError("C04 需要 tasks，或包含待办表格的 meeting_markdown/source_text")

        source_item_count = len(raw_tasks)
        raw_tasks, status_warnings = _normalize_task_statuses(raw_tasks)
        raw_tasks, calendar_warnings, calendar_item_ids, calendar_items = _exclude_calendar_arrangements(raw_tasks)
        raw_tasks, classification_warnings, classification_audit = _classify_task_dimensions(
            raw_tasks,
            payload.get("people_structure"),
        )
        tasks = [TaskNode.from_dict(item) for item in raw_tasks]
        ids = {task.task_id for task in tasks if task.task_id}
        warnings = [*extraction_warnings, *status_warnings, *calendar_warnings, *classification_warnings]
        for task in tasks:
            if not task.task_id or not task.title:
                warnings.append("存在缺少 task_id 或 title 的任务")
            if task.parent_task_id and task.parent_task_id not in ids:
                warnings.append(f"任务 {task.task_id} 的父任务不存在：{task.parent_task_id}")
            missing_predecessors = [item for item in task.predecessor_ids if item not in ids]
            if missing_predecessors:
                warnings.append(f"任务 {task.task_id} 的前置任务不存在：{', '.join(missing_predecessors)}")
            if not task.owners or not task.date_end or not task.acceptance_criteria:
                warnings.append(f"任务 {task.task_id or task.title} 的责任人、截止时间或完成标准不完整")
        granularity_warnings, non_leaf_task_ids = _execution_granularity_warnings(tasks)
        warnings.extend(granularity_warnings)
        graph = TaskGraphArtifact(
            project_id=project_id,
            goal=goal,
            tasks=tasks,
            root_task_ids=[task.task_id for task in tasks if task.task_id and not task.parent_task_id],
            critical_path_task_ids=[str(item) for item in payload.get("critical_path_task_ids") or []],
        )
        candidates = [
            {
                "task_id": task.task_id,
                "title": task.title,
                "professional_id": task.professional_id,
                "board_id": task.board_id,
            }
            for task in tasks
            if task.status == "candidate"
        ]
        return SkillResult(
            capability_id="C04",
            skill_name="task_planning",
            artifact_type="TaskGraph",
            artifact=graph.as_dict(),
            candidates=candidates,
            evidence_refs=[
                {"source_id": ref.source_id, "locator": ref.locator, "quote": ref.quote, "version": ref.version}
                for task in tasks
                for ref in task.evidence_refs
            ],
            warnings=warnings,
            requires_confirmation=bool(candidates or warnings),
            audit={
                "task_count": len(tasks),
                "root_task_count": len(graph.root_task_ids),
                "source_task_coverage": {
                    "source_count": extraction_audit.get("source_row_count", source_item_count),
                    "generated_count": len(tasks),
                    "excluded_completed_count": len(extraction_audit.get("skipped_completed_task_ids", [])),
                    "excluded_calendar_count": len(calendar_item_ids),
                    "balanced": (
                        extraction_audit.get("source_row_count", source_item_count)
                        == len(tasks)
                        + len(extraction_audit.get("skipped_completed_task_ids", []))
                        + len(calendar_item_ids)
                    ),
                },
                "calendar_item_ids": calendar_item_ids,
                "calendar_items": calendar_items,
                "non_leaf_task_ids": non_leaf_task_ids,
                "task_status_policy": TASK_STATUS_POLICY,
                "task_classification": classification_audit,
                **extraction_audit,
            },
        )


_TEAM_MARKERS = ("团队", "各模块负责人", "相关业务模块", "数据结构组", "总体组", "模块")
_EXTERNAL_PARTIES = ("外部软件供应商", "客户信息化部门", "业务部门", "现场施工单位")


def _classify_task_dimensions(
    raw_tasks: list[Any],
    people_structure: Any,
) -> tuple[list[Any], list[str], dict[str, int]]:
    profession_ids = {profession_id for profession_id, _ in PROFESSIONS}
    owner_boards: dict[str, set[str]] = {}
    for person in _people_records(people_structure):
        if person["group"] in BOARDS:
            owner_boards.setdefault(person["name"], set()).add(person["group"])

    classified: list[Any] = []
    warnings: list[str] = []
    missing_profession = 0
    missing_board = 0
    for raw_task in raw_tasks:
        if not isinstance(raw_task, dict):
            classified.append(raw_task)
            continue
        task = dict(raw_task)
        title = str(task.get("title") or "")
        profession = str(task.get("professional_id") or "")
        if profession not in profession_ids:
            outputs = task.get("outputs") or task.get("deliverables") or []
            if isinstance(outputs, str):
                outputs = [outputs]
            profession = infer_task_profession(title) or infer_task_profession(
                " ".join((str(task.get("description") or ""), *(str(item) for item in outputs)))
            )
        board = str(task.get("board_id") or "")
        if board not in BOARDS:
            board = next((candidate for candidate in BOARDS if title.strip().startswith(candidate)), "")
        if not board:
            owners = task.get("owners") or task.get("owner_candidates") or []
            candidate_boards = {
                candidate
                for owner in owners
                for candidate in owner_boards.get(str(owner), set())
            }
            board = next(iter(candidate_boards)) if len(candidate_boards) == 1 else ""
        task["professional_id"] = profession
        task["board_id"] = board
        task_id = str(task.get("task_id") or title or "未编号任务")
        if not profession:
            missing_profession += 1
            warnings.append(f"任务 {task_id} 无法唯一归入专业，已保留待归类。")
        if not board:
            missing_board += 1
            warnings.append(f"任务 {task_id} 无法唯一归入板块，已保留待归类。")
        classified.append(task)
    return classified, warnings, {
        "classified_count": len(classified),
        "missing_profession_count": missing_profession,
        "missing_board_count": missing_board,
    }


def _normalize_task_statuses(raw_tasks: list[Any]) -> tuple[list[Any], list[str]]:
    normalized: list[Any] = []
    warnings: list[str] = []
    for raw_task in raw_tasks:
        if not isinstance(raw_task, dict) or str(raw_task.get("status") or "") != "blocked":
            normalized.append(raw_task)
            continue
        task = dict(raw_task)
        task["status"] = "in_progress"
        normalized.append(task)
        warnings.append(
            f"任务 {task.get('task_id') or task.get('title') or ''} 的 blocked 已转换为 in_progress；"
            "阻碍应记录为风险或依赖，不作为任务状态。"
        )
    return normalized, warnings


_RECURRENCE_MARKERS = ("每日", "每天", "每周", "周例会", "例会", "站会", "定期", "周期性")
_MEETING_MARKERS = (
    "会议", "例会", "站会", "沟通会", "专题会", "评审会", "协调会", "复盘会",
    "宣贯会", "启动会", "碰头会", "专题对碰",
)
_MEETING_ACTION_MARKERS = ("召开", "参加", "组织", "出席", "开会", "专题对碰")
_MEETING_ACTION_PATTERNS = (
    r"携.+?与.+?(?:对碰|沟通|评审|确认)",
    r"与.+?(?:专题)?(?:对碰|沟通|评审)",
)
_PREPARATION_MARKERS = ("会前", "准备", "编制", "整理", "提交", "输出", "更新", "回写", "整改")
_TIME_OF_DAY_MARKERS = ("上午", "中午", "下午", "晚上", "全天")
_COLLECTIVE_SCOPE_MARKERS = (
    "各板块",
    "各模块",
    "各专业",
    "多个板块",
    "跨板块",
    "四板块",
    "整体梳理",
    "总体盘点",
    "盘点初筛",
)
_MANAGEMENT_ACTION_MARKERS = ("统筹", "汇总", "归拢", "审核", "过堂", "整体完成", "统一整理")


def _exclude_calendar_arrangements(
    raw_tasks: list[Any],
) -> tuple[list[Any], list[str], list[str], list[dict[str, Any]]]:
    filtered: list[Any] = []
    warnings: list[str] = []
    calendar_item_ids: list[str] = []
    calendar_items: list[dict[str, Any]] = []
    for raw_task in raw_tasks:
        if not isinstance(raw_task, dict):
            filtered.append(raw_task)
            continue
        title = str(raw_task.get("title") or "")
        description = str(raw_task.get("description") or "")
        due = str(raw_task.get("date_end") or raw_task.get("due") or "")
        evidence_text = " ".join(
            str(ref.get("quote") or "")
            for ref in raw_task.get("evidence_refs") or []
            if isinstance(ref, dict)
        )
        text = " ".join((title, description, due, evidence_text))
        is_recurring = any(marker in text for marker in _RECURRENCE_MARKERS)
        is_meeting_body = _is_meeting_body(raw_task, title=title, evidence_text=evidence_text)
        if not is_recurring and not is_meeting_body:
            filtered.append(raw_task)
            continue
        item_id = str(raw_task.get("task_id") or title or "未编号事项")
        calendar_item_ids.append(item_id)
        time_label = next((marker for marker in _TIME_OF_DAY_MARKERS if marker in text), "会议")
        calendar_items.append({
            "event_id": item_id,
            "date": due,
            "time_label": time_label,
            "title": title or "项目会议",
            "owner_names": [str(name) for name in raw_task.get("owners") or [] if str(name)],
            "description": description,
            "evidence_refs": list(raw_task.get("evidence_refs") or []),
            "classification_reason": "recurring_arrangement" if is_recurring else "meeting_body",
        })
        warnings.append(f"事项 {item_id} 属于会议本体或例行安排，已从任务图移出并应写入项目日历。")
    return filtered, warnings, calendar_item_ids, calendar_items


def _is_meeting_body(raw_task: dict[str, Any], *, title: str, evidence_text: str) -> bool:
    explicit_type = str(
        raw_task.get("item_type") or raw_task.get("kind") or raw_task.get("category") or ""
    ).strip().lower()
    if explicit_type in {"meeting", "calendar", "calendar_event", "event", "会议", "日历"}:
        return True

    compact_title = re.sub(r"\s+", "", title)
    title_is_preparation = any(marker in compact_title for marker in _PREPARATION_MARKERS)
    title_is_meeting = any(marker in compact_title for marker in _MEETING_MARKERS)
    if title_is_meeting and not title_is_preparation:
        return True

    evidence_has_meeting = any(marker in evidence_text for marker in _MEETING_MARKERS)
    evidence_has_action = (
        any(marker in evidence_text for marker in _MEETING_ACTION_MARKERS)
        or any(re.search(pattern, evidence_text) for pattern in _MEETING_ACTION_PATTERNS)
    )
    evidence_is_preparation = bool(re.search(r"(?:会前|会议前).*(?:准备|编制|整理|提交|输出)", evidence_text))
    return evidence_has_meeting and evidence_has_action and not evidence_is_preparation


def _execution_granularity_warnings(tasks: list[TaskNode]) -> tuple[list[str], list[str]]:
    child_parent_ids = {task.parent_task_id for task in tasks if task.parent_task_id}
    warnings: list[str] = []
    non_leaf_task_ids: list[str] = []
    for task in tasks:
        if task.task_id in child_parent_ids:
            continue
        task_id = task.task_id or task.title or "未编号任务"
        text = " ".join((task.title, task.description, " ".join(task.outputs)))
        reasons: list[str] = []
        if not task.outputs:
            reasons.append("没有明确交付物")
        if len(task.owners) > 1 and len(task.outputs) <= 1:
            reasons.append("多个负责人共同承担单一交付物，但未分别写清个人动作")
        if any(marker in text for marker in _COLLECTIVE_SCOPE_MARKERS):
            reasons.append("包含跨板块或集合范围，却没有对应实施子任务")
        if any(marker in text for marker in _MANAGEMENT_ACTION_MARKERS) and any(
            marker in text for marker in ("各", "板块", "模块", "专业", "跨")
        ):
            reasons.append("管理层汇总或审核动作缺少输入侧子任务")
        if not reasons:
            continue
        non_leaf_task_ids.append(task_id)
        warnings.append(
            f"任务 {task_id} 未通过实施人员测试：{'；'.join(dict.fromkeys(reasons))}。"
            "请继续拆分到个人拿到后无需二次分工即可直接执行的叶子任务。"
        )
    return warnings, non_leaf_task_ids


def _tasks_from_meeting(
    markdown: str,
    *,
    source_id: str,
    people_structure: Any,
    fallback_owner: str,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    section = _section_51(markdown)
    rows = _markdown_task_rows(section)
    people = _people_records(people_structure)
    tasks: list[dict[str, Any]] = []
    warnings: list[str] = []
    skipped_completed: list[str] = []
    fallback_assignments: list[str] = []
    meeting_date = _meeting_date(markdown)

    for row in rows:
        task_id, title, responsibility, due, deliverable, acceptance = row
        if _is_completed(title, due):
            skipped_completed.append(task_id)
            continue
        owners, collaborators, unresolved = _resolve_responsibility(
            responsibility,
            task_text=" ".join((task_id, title, deliverable, acceptance)),
            people=people,
        )
        if unresolved:
            if fallback_owner and fallback_owner not in owners:
                owners.append(fallback_owner)
            fallback_assignments.append(task_id)
            warnings.append(
                f"任务 {task_id} 的责任项“{'、'.join(unresolved)}”无法从人员结构唯一映射；"
                f"暂由{fallback_owner}负责在正式派发前指定具体执行人。"
            )
        if not owners and fallback_owner:
            owners = [fallback_owner]
            fallback_assignments.append(task_id)
            warnings.append(f"任务 {task_id} 未识别到具体负责人；暂由{fallback_owner}负责补齐分工。")

        description = _human_task_description(title, deliverable, acceptance)
        if unresolved:
            description += (
                f" 正式开始前，由{fallback_owner}确认“{'、'.join(unresolved)}”对应的实际执行人。"
            )
        tasks.append(
            {
                "task_id": task_id,
                "title": title,
                "description": description,
                "owners": owners,
                "collaborators": collaborators,
                "confirmers": [fallback_owner] if fallback_owner else [],
                "outputs": [deliverable] if deliverable else [],
                "acceptance_criteria": [acceptance] if acceptance else [],
                "date_end": _clean_due_date(due, meeting_date=meeting_date),
                "status": "candidate",
                "evidence_refs": [
                    {
                        "source_id": source_id,
                        "locator": f"5.1/{task_id}",
                        "quote": " | ".join(row),
                    }
                ],
            }
        )

    return tasks, warnings, {
        "source_row_count": len(rows),
        "skipped_completed_task_ids": skipped_completed,
        "fallback_assignment_task_ids": fallback_assignments,
        "meeting_section": "5.1",
    }


def _section_51(markdown: str) -> str:
    match = re.search(r"(?m)^#{1,6}\s*5\.1\b.*$", markdown)
    if not match:
        raise ValueError("C04 未找到会议纪要 5.1 待办章节")
    start = match.start()
    next_section = re.search(r"(?m)^#{1,6}\s*5\.2\b.*$", markdown[match.end():])
    end = match.end() + next_section.start() if next_section else len(markdown)
    return markdown[start:end]


def _markdown_task_rows(section: str) -> list[tuple[str, str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str, str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [_clean_cell(cell) for cell in stripped.strip("|").split("|")]
        if len(cells) != 6:
            continue
        if cells[0] in {"编号", ""} or all(re.fullmatch(r"[-: ]*", cell) for cell in cells):
            continue
        if not re.fullmatch(r"[A-Za-z]+-\d+", cells[0]):
            continue
        rows.append(tuple(cells))  # type: ignore[arg-type]
    return rows


def _clean_cell(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "、", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"、{2,}", "、", value)
    return value.strip(" 、")


def _is_completed(title: str, due: str) -> bool:
    return "已完成" in due or any(marker in title for marker in ("纳入已解决", "问题已解决"))


def _meeting_date(markdown: str) -> date | None:
    match = re.search(r"(?m)^date:\s*(\d{4}-\d{2}-\d{2})\s*$", markdown)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _clean_due_date(value: str, *, meeting_date: date | None) -> str:
    cleaned = value.replace("、", "，").strip()
    full = re.search(r"(\d{4}-\d{2}-\d{2})", cleaned)
    if full:
        parsed = date.fromisoformat(full.group(1))
        if "当周" in cleaned:
            return (parsed + timedelta(days=6)).isoformat()
        return parsed.isoformat()
    short = re.search(r"(?<!\d)(\d{2})-(\d{2})(?!\d)", cleaned)
    if short and meeting_date:
        return f"{meeting_date.year:04d}-{int(short.group(1)):02d}-{int(short.group(2)):02d}"
    return cleaned


def _human_task_description(title: str, deliverable: str, acceptance: str) -> str:
    task = title.strip().rstrip("。；;")
    output = deliverable.strip().rstrip("。；;")
    criterion = acceptance.strip().rstrip("。；;")

    if "继续观察" in task:
        before, after = task.split("继续观察", 1)
        subject = before.strip() or after.split("并", 1)[0].strip()
        action = f"持续跟踪{subject}，记录观察过程、异常情况和最终判断，给出是否可以关闭的明确结论"
    elif "三阶段重新确认" in task:
        subject = task.split("按", 1)[0].strip()
        action = f"重新核查{subject}，分别确认方案、现场施工和最终验证的完成情况，列清剩余工作及责任人"
    elif task.startswith("重新确认"):
        action = f"重新核对{task.removeprefix('重新确认').strip()}，确认对接双方、处理范围和下一步安排"
    elif "取得" in task and "反馈" in task:
        subject = task.split("取得", 1)[0].strip()
        action = f"跟进{subject}的最终反馈，记录反馈结论、未回复事项和需要升级协调的问题"
    elif "协调到位" in task:
        action = f"协调落实{task.replace('协调到位', '').strip()}，明确到位时间、交接范围和后续工作安排"
    elif task.startswith("按") and "形成" in task:
        basis, result = task.split("形成", 1)
        action = f"{basis.strip()}梳理业务内容并形成{result.strip()}，逐项写清范围、规则和模块间的衔接关系"
    elif task.startswith("将") and "收敛为" in task:
        source, result = task.removeprefix("将").split("收敛为", 1)
        action = f"梳理{source.strip()}，提炼共性和差异后收敛为{result.strip()}，避免重复罗列相似场景"
    elif task.startswith("输出"):
        action = f"梳理并形成{task.removeprefix('输出').strip()}，写清涉及范围、关键节点、参与角色和待确认事项"
    elif task.startswith("列出"):
        action = f"梳理{task.removeprefix('列出').strip()}并逐项列明，避免遗漏相关模块和责任边界"
    elif "确认" in task and "是否具备" in task:
        subject = task.replace("责任人确认", "").replace("确认", "", 1)
        action = f"逐项核查{subject.strip()}，记录已具备项、缺失项及后续处理人"
    elif task.startswith("拆解"):
        action = f"围绕{task.removeprefix('拆解').strip()}逐项拆解，说明数据来源、处理规则、流向和异常情况"
    elif task.startswith("梳理"):
        action = f"梳理{task.removeprefix('梳理').strip()}，明确上下游关系、关键字段和未闭环事项"
    elif task.endswith("准备"):
        action = f"围绕{task.removesuffix('准备').strip()}完成资料和议题准备，列清已知情况、待确认内容和需要作出的决定"
    elif task.startswith("将") and "关联" in task:
        source, target = task.removeprefix("将").split("关联", 1)
        action = f"建立{source.strip()}与{target.strip()}的对应关系，标明来源、去向和匹配规则"
    elif task.startswith("用") and "反推" in task:
        basis, result = task.removeprefix("用").split("反推", 1)
        action = f"以{basis.strip()}为依据反推{result.strip()}，逐项说明业务触发点、输入输出和异常处理"
    elif task.startswith("每个模块明确"):
        action = f"逐个模块明确{task.removeprefix('每个模块明确').strip()}，将责任落实到具体人员并记录分工边界"
    elif task.startswith("准备"):
        action = f"围绕{task.removeprefix('准备').strip()}完成会前准备，列清需要讨论的衔接问题、责任边界和待决策事项"
    else:
        action = f"推进{task}，明确执行范围、处理结果和仍需协调的事项"

    sentences = [f"{action}。"]
    if output:
        sentences.append(f"完成后提交{output}。")
    if criterion:
        sentences.append(_criterion_sentence(criterion))
    return "".join(sentences)


def _criterion_sentence(criterion: str) -> str:
    if criterion.startswith("覆盖"):
        return f"内容应{criterion}。"
    if criterion.startswith("能"):
        return f"结果应{criterion}。"
    if criterion.startswith(("明确", "列明", "标明", "写清")):
        return f"结果中需{criterion}。"
    if criterion.startswith("先"):
        return f"处理时需{criterion}。"
    return f"最终结果需满足：{criterion}。"


def _people_records(value: Any) -> list[dict[str, str]]:
    if not value:
        return []
    if hasattr(value, "people"):
        candidates = getattr(value, "people")
    elif isinstance(value, dict):
        nested = value.get("people_structure")
        if isinstance(nested, dict):
            candidates = nested.get("people") or []
        else:
            candidates = value.get("people") or []
    else:
        candidates = []
    records: list[dict[str, str]] = []
    for item in candidates:
        if isinstance(item, dict):
            records.append(
                {
                    "name": str(item.get("name") or "").strip(),
                    "group": str(item.get("group") or "").strip(),
                    "role": str(item.get("role") or "").strip(),
                    "responsibility_note": str(item.get("responsibility_note") or "").strip(),
                }
            )
        else:
            records.append(
                {
                    "name": str(getattr(item, "name", "") or "").strip(),
                    "group": str(getattr(item, "group", "") or "").strip(),
                    "role": str(getattr(item, "role", "") or "").strip(),
                    "responsibility_note": str(getattr(item, "responsibility_note", "") or "").strip(),
                }
            )
    return [item for item in records if item["name"]]


def _resolve_responsibility(
    responsibility: str,
    *,
    task_text: str,
    people: list[dict[str, str]],
) -> tuple[list[str], list[str], list[str]]:
    owners: list[str] = []
    collaborators: list[str] = []
    unresolved: list[str] = []
    known_names = {person["name"] for person in people}
    tokens = [
        token.strip()
        for token in re.split(r"[,，、]|(?:\s+及\s+)", responsibility.replace("及数据结构组", "、数据结构组"))
        if token.strip()
    ]
    for token in tokens:
        if token in _EXTERNAL_PARTIES:
            collaborators.append(token)
            continue
        if token in known_names:
            owners.append(token)
            continue
        resolved = _resolve_team_token(token, task_text=task_text, people=people)
        if resolved:
            owners.extend(resolved)
            continue
        if _looks_like_person_name(token):
            owners.append(token)
        else:
            unresolved.append(token)
    return list(dict.fromkeys(owners)), list(dict.fromkeys(collaborators)), list(dict.fromkeys(unresolved))


def _resolve_team_token(token: str, *, task_text: str, people: list[dict[str, str]]) -> list[str]:
    if "测试" in token:
        test_leads = [
            person["name"]
            for person in people
            if "测试负责人" in person["responsibility_note"]
        ]
        if test_leads:
            return test_leads[:1]
    if "总体组" in token:
        business_leads = [
            person["name"]
            for person in people
            if person["group"] == "总体组" and person["responsibility_note"] == "总业务架构师"
        ]
        return business_leads[:1]
    if "接口负责人" in token or "接口团队" in token:
        return []
    if "数据结构组" in token:
        architects = [
            person["name"]
            for person in people
            if person["group"] == "总体组" and "架构" in person["responsibility_note"]
        ]
        return architects[:1]
    if any(marker in token for marker in ("各模块负责人", "相关业务模块", "实施团队")):
        return _all_board_leads(people)

    normalized = token
    for marker in ("交付方", "团队", "相关", "业务", "模块", "负责人", "数据结构组"):
        normalized = normalized.replace(marker, "")
    normalized = normalized.strip()
    if "质量" in token:
        normalized = "质量"
    elif "平台" in token:
        normalized = "平台"
    if normalized:
        leads = _group_leads(normalized, people)
        if leads:
            return leads
    return []


def _all_board_leads(people: list[dict[str, str]]) -> list[str]:
    return list(dict.fromkeys(
        person["name"]
        for person in people
        if "专题负责人" in person["role"]
    ))


def _group_leads(group: str, people: list[dict[str, str]]) -> list[str]:
    matches = [person for person in people if person["group"] == group]
    if not matches:
        return []
    topic_leads = [person["name"] for person in matches if "专题负责人" in person["role"]]
    if topic_leads:
        return topic_leads
    technical_leads = [
        person["name"]
        for person in matches
        if "负责人" in person["responsibility_note"] or "负责人" in person["role"]
    ]
    return technical_leads[:1]


def _looks_like_person_name(value: str) -> bool:
    non_person_markers = (
        "部", "组", "司", "团队", "模块", "负责人", "测试", "实施", "接口",
        "平台", "质量", "仓储", "物流", "原料", "单位",
    )
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", value)) and not any(
        marker in value for marker in non_person_markers
    )
