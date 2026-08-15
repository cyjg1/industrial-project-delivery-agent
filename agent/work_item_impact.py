from __future__ import annotations

import re
from typing import Any

from agent.schemas import InspectionItem, WorkItem, to_plain


def analyze_work_item_impact(
    followup_items: list[InspectionItem],
    work_items: list[WorkItem],
    *,
    milestone_id: str,
) -> dict[str, Any]:
    suggestions = [
        _suggestion_for_followup(item, work_items, milestone_id=milestone_id)
        for item in followup_items
    ]
    return {
        "summary": {
            "source_followup_count": len(followup_items),
            "work_item_count": len(work_items),
            "suggestion_count": len(suggestions),
            "matched_existing_count": len([item for item in suggestions if item["matched_work_item_id"]]),
            "new_task_count": len([item for item in suggestions if item["impact_type"] == "create_new_task"]),
        },
        "suggestions": suggestions[:30],
    }


def _suggestion_for_followup(
    item: InspectionItem,
    work_items: list[WorkItem],
    *,
    milestone_id: str,
) -> dict[str, Any]:
    match = _best_match(item, work_items)
    if match is None:
        return {
            "source_item_id": item.item_id,
            "source_title": item.title,
            "impact_type": "create_new_task",
            "match_type": "no_match",
            "matched_work_item_id": "",
            "matched_work_item_title": "",
            "reason": "未命中正式任务池，建议作为新增任务候选进入人工确认。",
            "suggested_action": "发布为新任务",
            "milestone_id": milestone_id,
            "human_confirmation_required": True,
            "evidence_refs": [to_plain(ref) for ref in item.evidence_refs],
        }
    work_item, score = match
    match_type = "exact_match" if score >= 0.72 else "related_match"
    return {
        "source_item_id": item.item_id,
        "source_title": item.title,
        "impact_type": "update_existing_task",
        "match_type": match_type,
        "matched_work_item_id": work_item.work_item_id,
        "matched_work_item_title": work_item.title,
        "reason": f"新内容与已有任务“{work_item.title}”存在关键词重合，建议人工判断是否更新范围、责任人、交付物或截止日期。",
        "suggested_action": "作为已有任务变更建议",
        "milestone_id": milestone_id or work_item.milestone_id,
        "human_confirmation_required": True,
        "score": round(score, 3),
        "evidence_refs": [to_plain(ref) for ref in item.evidence_refs],
    }


def _best_match(item: InspectionItem, work_items: list[WorkItem]) -> tuple[WorkItem, float] | None:
    if not work_items:
        return None
    scored = [
        (work_item, _similarity(_item_text(item), _work_item_text(work_item)))
        for work_item in work_items
    ]
    work_item, score = max(scored, key=lambda pair: pair[1])
    return (work_item, score) if score >= 0.18 else None


def _item_text(item: InspectionItem) -> str:
    return " ".join([
        item.title,
        item.description,
        item.deliverable or "",
        item.acceptance_criteria or "",
        " ".join(item.owner_candidates or []),
    ])


def _work_item_text(item: WorkItem) -> str:
    return " ".join([
        item.title,
        item.description,
        item.deliverable or "",
        item.acceptance_criteria or "",
        " ".join(item.owner_candidates or []),
    ])


def _similarity(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = left_tokens & right_tokens
    return len(overlap) / max(len(left_tokens), len(right_tokens))


def _tokens(value: str) -> set[str]:
    normalized = re.sub(r"[\s，。；：、/\\\-_\(\)（）]+", " ", value.lower())
    tokens = {token for token in normalized.split() if len(token) >= 2}
    # Chinese project phrases often have no spaces; add short semantic shards.
    for shard in ["字段", "映射", "单据", "权限", "责任", "验收", "样例", "里程碑", "交付物"]:
        if shard in value:
            tokens.add(shard)
    return tokens
