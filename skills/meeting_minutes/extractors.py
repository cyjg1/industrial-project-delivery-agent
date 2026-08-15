from __future__ import annotations

import json
import re
from typing import Any

from skills.meeting_minutes.schemas import PromptBundle


CORRECTION_EXTRACTION_SYSTEM_PROMPT = """你是一个严格的 ASR 术语纠错抽取器。

你只抽取会议原始转写中明显属于语音识别错误的词，并结合最终纪要推断正确写法。
允许类型：人名、公司名、系统名、字段名、业务术语、行业术语。
禁止类型：业务观点纠偏、普通润色、长句重写、没有上下文支撑的猜测。

请严格输出 JSON：
{
  "candidates": [
    {
      "wrong": "错误词",
      "correct": "正确词",
      "category": "人名",
      "confidence": 0.91,
      "reason": "简短原因",
      "source_snippet": "原始转写短片段"
    }
  ]
}
如果没有高置信候选，返回 {"candidates": []}。"""


METHODOLOGY_EXTRACTION_SYSTEM_PROMPT = """你是项目管理 Agent 的方法论候选抽取器。

请从会议纪要中抽取可以长期复用的“法”：管理方法、UAT 验收方法、数据治理方法、责任闭环方法、跨团队协同原则。
不要抽普通待办，不要把单次会议结论包装成方法论。

请严格输出 JSON：
{
  "methodologies": [
    {
      "name": "方法论名称",
      "business_goal": "业务目标",
      "principles": ["原则1", "原则2"],
      "reasoning_chain": ["目标=...", "方法=...", "验收=..."],
      "applicable_scope": "适用范围",
      "evidence_refs": ["meeting_id: 纪要中的短证据"]
    }
  ]
}
如果没有候选，返回 {"methodologies": []}。"""


def build_correction_extraction_prompt(
    *,
    transcript_text: str,
    minutes_markdown: str,
    known_terms: str,
) -> PromptBundle:
    user_message = (
        f"【已知纠错词】\n{known_terms or '（暂无既有术语纠错）'}\n\n"
        f"【最终纪要】\n{_truncate(minutes_markdown, 12000)}\n\n"
        f"【原始转写】\n{_truncate(transcript_text, 16000)}"
    )
    return PromptBundle(
        system_prompt=CORRECTION_EXTRACTION_SYSTEM_PROMPT,
        user_message=user_message,
    )


def build_methodology_extraction_prompt(*, minutes_markdown: str, meeting_id: str) -> PromptBundle:
    user_message = (
        f"会议编号：{meeting_id}\n\n"
        f"【会议纪要全文】\n{_truncate(minutes_markdown, 16000)}"
    )
    return PromptBundle(
        system_prompt=METHODOLOGY_EXTRACTION_SYSTEM_PROMPT,
        user_message=user_message,
    )


def parse_correction_candidates(raw_response: str) -> list[dict[str, Any]]:
    payload = _parse_json_object(raw_response)
    candidates = payload.get("candidates", []) if isinstance(payload, dict) else []
    if not isinstance(candidates, list):
        return []
    normalized = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        wrong = str(item.get("wrong", "")).strip()
        correct = str(item.get("correct", "")).strip()
        if not wrong or not correct or wrong == correct:
            continue
        normalized.append(
            {
                "wrong": wrong,
                "correct": correct,
                "category": str(item.get("category", "")).strip() or "自动沉淀术语",
                "confidence": _safe_float(item.get("confidence", 0)),
                "reason": str(item.get("reason", "")).strip(),
                "source_snippet": str(item.get("source_snippet", "")).strip(),
            }
        )
    return normalized


def parse_methodology_candidates(raw_response: str) -> list[dict[str, Any]]:
    payload = _parse_json_object(raw_response)
    candidates = payload.get("methodologies", []) if isinstance(payload, dict) else []
    if not isinstance(candidates, list):
        return []
    normalized = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        normalized.append(
            {
                "name": name,
                "business_goal": str(item.get("business_goal", "")).strip(),
                "principles": _string_list(item.get("principles", [])),
                "reasoning_chain": _string_list(item.get("reasoning_chain", [])),
                "applicable_scope": str(item.get("applicable_scope", "")).strip(),
                "evidence_refs": _string_list(item.get("evidence_refs", [])),
            }
        )
    return normalized


def _parse_json_object(raw_response: str) -> dict[str, Any]:
    text = (raw_response or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
    if fence:
        try:
            payload = json.loads(fence.group(1))
            return payload if isinstance(payload, dict) else {}
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            payload = json.loads(text[start:end + 1])
            return payload if isinstance(payload, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _truncate(value: str, limit: int) -> str:
    text = value or ""
    return text if len(text) <= limit else text[:limit] + "\n\n（内容过长，已截断用于抽取）"

