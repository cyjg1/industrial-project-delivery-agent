from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable


QUESTION_HEADER = re.compile(r"^### (?P<id>Q\d{2}) (?P<title>.+)$")
QUESTION_FIELD = re.compile(
    r"^- (?P<label>问题|检索查询|检索意图|预期工具|证据约束|不可接受|人工结论)：(?P<value>.*)$"
)
TOOL_NAME = re.compile(r"`([^`]+)`")

MUTATING_TOOL_NAMES = {
    "ingest_file",
    "propose_candidates",
    "archive_deliverable",
    "update_task_status",
}
ACCEPTED_STOP_REASONS = {
    "model_completed",
    "max_rounds_reached",
    "max_tool_calls_reached",
    "max_elapsed_time_reached",
    "no_progress",
}


def load_agent_evaluation_cases(path: str | Path) -> list[dict[str, Any]]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    cases: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    section = ""
    for line in lines:
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        header = QUESTION_HEADER.match(line)
        if header:
            if current:
                cases.append(_normalize_case(current))
            current = {
                "id": header.group("id"),
                "title": header.group("title").strip(),
                "section": section,
            }
            continue
        if current is None:
            continue
        field = QUESTION_FIELD.match(line)
        if field:
            current[_field_key(field.group("label"))] = field.group("value").strip()
    if current:
        cases.append(_normalize_case(current))
    return cases


def actors_for_case(case: dict[str, Any], default_actor_id: str) -> list[str]:
    case_id = str(case.get("id") or "")
    if case_id in {"Q19", "Q20"}:
        return ["u_exec"]
    if case_id == "Q21":
        return ["u_exec", "u_pm"]
    return [default_actor_id]


def evaluate_conversation_result(
    case: dict[str, Any],
    result: dict[str, Any] | None,
    *,
    actor_id: str,
    max_rounds: int,
    precondition_error: str = "",
) -> dict[str, Any]:
    if precondition_error:
        return {
            "case_id": case["id"],
            "actor_id": actor_id,
            "status": "skipped",
            "precondition_error": precondition_error,
            "machine_passed": False,
            "semantic_quality_auto_passed": False,
            "human_judgement": "pending",
            "checks": [],
        }
    if result is None:
        return {
            "case_id": case["id"],
            "actor_id": actor_id,
            "status": "runtime_failed",
            "machine_passed": False,
            "semantic_quality_auto_passed": False,
            "human_judgement": "pending",
            "checks": [
                _check("runtime_result", False, "Agent did not return a result.")
            ],
        }

    reply = str(result.get("reply") or "").strip()
    runtime_error = str(result.get("runtime_error") or "")
    tool_steps = [
        step for step in result.get("tool_steps") or [] if isinstance(step, dict)
    ]
    actual_tools = _ordered_unique(
        str(step.get("tool_name") or "")
        for step in tool_steps
        if step.get("tool_name")
    )
    expected_tools = [str(name) for name in case.get("expected_tools") or []]
    missing_tools = [name for name in expected_tools if name not in actual_tools]
    failed_tools = [
        str(step.get("tool_name") or "")
        for step in tool_steps
        if step.get("status") not in {"completed", "skipped"}
        or _tool_result_failed(step.get("result"))
    ]
    verification = (
        result.get("verification")
        if isinstance(result.get("verification"), dict)
        else {}
    )
    unsupported = [str(value) for value in verification.get("unsupported") or []]
    invalid_fact_ids = [
        str(value) for value in verification.get("invalid_fact_ids") or []
    ]
    rounds = (
        result.get("debug", {}).get("rounds", [])
        if isinstance(result.get("debug"), dict)
        else []
    )
    round_count = len(rounds) if isinstance(rounds, list) else 0
    stop_reason = str(result.get("stop_reason") or "")
    evidence_tool_names = {
        "search_memory",
        "search_source_evidence",
        "search_methods",
        "search_project_skills",
        "get_tasks",
        "get_person",
        "get_project_health",
        "get_milestone_status",
    }
    evidence_steps = [
        step
        for step in tool_steps
        if str(step.get("tool_name") or "") in evidence_tool_names
        and _tool_result_has_rows(step.get("result"))
    ]
    memory_skill_retrieval = {}
    for step in tool_steps:
        if str(step.get("tool_name") or "") != "search_project_skills":
            continue
        result_payload = step.get("result")
        if isinstance(result_payload, dict) and isinstance(
            result_payload.get("retrieval_trace"),
            dict,
        ):
            memory_skill_retrieval = dict(result_payload["retrieval_trace"])
    checks = [
        _check(
            "runtime_completion",
            not runtime_error,
            (
                "Conversation runtime returned a final payload."
                if not runtime_error
                else runtime_error
            ),
        ),
        _check(
            "non_empty_answer",
            bool(reply),
            "Agent returned a non-empty final answer."
            if reply
            else "Final answer is empty.",
        ),
        _check(
            "expected_tool_coverage",
            not missing_tools,
            (
                f"Expected tools covered: {', '.join(expected_tools)}."
                if not missing_tools
                else f"Missing expected tools: {', '.join(missing_tools)}."
            ),
            details={"expected": expected_tools, "actual": actual_tools},
        ),
        _check(
            "tool_execution",
            not failed_tools,
            (
                "All selected tools completed or were explicitly skipped."
                if not failed_tools
                else f"Failed tools: {', '.join(failed_tools)}."
            ),
        ),
        _check(
            "answer_verification",
            not unsupported and not invalid_fact_ids,
            (
                "Concrete claims passed fact-reference verification."
                if not unsupported and not invalid_fact_ids
                else "Unsupported claims or invalid fact references remain."
            ),
            details={
                "unsupported": unsupported,
                "invalid_fact_ids": invalid_fact_ids,
                "citation_coverage": verification.get("citation_coverage") or {},
            },
        ),
        _check(
            "bounded_loop",
            0 < round_count <= max_rounds + 1
            and stop_reason in ACCEPTED_STOP_REASONS,
            (
                f"Stopped after {round_count} rounds: {stop_reason}."
                if round_count
                else f"No round trace was recorded: {stop_reason or 'unknown'}."
            ),
            details={"round_count": round_count, "max_rounds": max_rounds},
        ),
        _check(
            "evidence_observed",
            bool(evidence_steps) or not expected_tools,
            (
                f"{len(evidence_steps)} evidence-producing tool steps returned data."
                if evidence_steps
                else "No evidence-producing tool result was observed."
            ),
        ),
    ]
    if not MUTATING_TOOL_NAMES.intersection(expected_tools):
        checks.append(
            _check(
                "read_only_case",
                not MUTATING_TOOL_NAMES.intersection(actual_tools),
                (
                    "The evaluation case did not invoke mutating tools."
                    if not MUTATING_TOOL_NAMES.intersection(actual_tools)
                    else "A read-only evaluation case invoked a mutating tool."
                ),
            )
        )

    machine_passed = all(bool(check["passed"]) for check in checks)
    return {
        "case_id": case["id"],
        "actor_id": actor_id,
        "status": (
            "completed"
            if machine_passed
            else "runtime_failed"
            if runtime_error
            else "machine_failed"
        ),
        "machine_passed": machine_passed,
        "semantic_quality_auto_passed": False,
        "human_judgement": "pending",
        "human_notes": "",
        "question": case["question"],
        "evidence_constraint": case["evidence_constraint"],
        "unacceptable": case["unacceptable"],
        "actual_tools": actual_tools,
        "round_count": round_count,
        "stop_reason": stop_reason,
        "reply": reply,
        "error": runtime_error,
        "checks": checks,
        "trace": rounds,
        "tool_steps": tool_steps,
        "memory_skill_retrieval": memory_skill_retrieval,
    }


def _normalize_case(row: dict[str, Any]) -> dict[str, Any]:
    expected_tools = TOOL_NAME.findall(str(row.get("expected_tools_text") or ""))
    return {
        "id": str(row.get("id") or ""),
        "title": str(row.get("title") or ""),
        "section": str(row.get("section") or ""),
        "question": str(row.get("question") or ""),
        "search_query": str(row.get("search_query") or ""),
        "intent": str(row.get("intent") or ""),
        "expected_tools": expected_tools,
        "evidence_constraint": str(row.get("evidence_constraint") or ""),
        "unacceptable": str(row.get("unacceptable") or ""),
        "human_judgement": str(row.get("human_judgement") or "待评审"),
    }


def _field_key(label: str) -> str:
    return {
        "问题": "question",
        "检索查询": "search_query",
        "检索意图": "intent",
        "预期工具": "expected_tools_text",
        "证据约束": "evidence_constraint",
        "不可接受": "unacceptable",
        "人工结论": "human_judgement",
    }[label]


def _ordered_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _tool_result_failed(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return value.get("ok") is False or bool(value.get("error"))


def _tool_result_has_rows(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("ok") is False:
        return False
    if any(
        isinstance(item, list) and bool(item)
        for item in value.values()
    ):
        return True
    return any(
        key in value
        for key in {
            "summary",
            "health",
            "milestone",
            "person",
            "task",
            "diagnostics",
        }
    )


def _check(
    name: str,
    passed: bool,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": name,
        "passed": passed,
        "message": message,
    }
    if details:
        row["details"] = details
    return row
