from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any


@dataclass(frozen=True)
class PromptBundle:
    system_prompt: str
    user_message: str


@dataclass
class CorrectionCandidate:
    wrong: str
    correct: str
    category: str
    confidence: float = 0.0
    reason: str = ""
    source_snippet: str = ""


@dataclass
class MethodologyCandidate:
    name: str
    business_goal: str
    principles: list[str] = field(default_factory=list)
    reasoning_chain: list[str] = field(default_factory=list)
    applicable_scope: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    status: str = "candidate"


def to_plain(value: Any) -> Any:
    if is_dataclass(value):
        return {key: to_plain(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    return value

