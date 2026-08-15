from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.repository_paths import normalize_repository_paths
from agent.env_loader import PROJECT_ROOT


DEFAULT_MEETING_MINUTES_STORE_DIR = PROJECT_ROOT / "data" / "skills" / "meeting_minutes"
AUTO_APPLY_MIN_OCCURRENCES = 2
AUTO_APPLY_MIN_CONFIDENCE = 0.85
MAX_SOURCE_SNIPPETS = 5


class MeetingMinutesSkillStore:
    def __init__(self, root_dir: str | Path = DEFAULT_MEETING_MINUTES_STORE_DIR):
        self.root_dir = Path(root_dir)
        self.lexicon_path = self.root_dir / "lexicon.json"
        self.correction_candidates_path = self.root_dir / "correction_candidates.json"
        self.methodology_candidates_path = self.root_dir / "methodology_candidates.json"
        self.runs_path = self.root_dir / "runs.jsonl"
        self.schema_path = self.root_dir / "schema.json"
        self.minutes_dir = self.root_dir / "minutes"
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.minutes_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_files()

    def save_minutes(self, *, meeting_id: str, meeting_date: str, markdown: str) -> Path:
        if not markdown.strip():
            raise ValueError("Generated meeting minutes are empty")
        safe_meeting_id = _safe_path_component(meeting_id)
        safe_date = _safe_path_component(meeting_date) or "undated"
        content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:16]
        meeting_dir = self.minutes_dir / safe_meeting_id
        meeting_dir.mkdir(parents=True, exist_ok=True)
        target = meeting_dir / f"{safe_date}-{content_hash}.md"
        if target.exists():
            if target.read_text(encoding="utf-8") != markdown:
                raise RuntimeError(f"Meeting minutes hash collision: {target}")
            return target
        temp_path = target.with_suffix(".md.tmp")
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(markdown)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        return target

    def add_lexicon_term(self, *, category: str, wrong: str, correct: str) -> dict[str, Any]:
        payload = self._read_json(self.lexicon_path, _default_lexicon())
        terms = payload.setdefault("terms", {}).setdefault("语音识别常见错误 → 正确写法", {})
        category_terms = terms.setdefault(_normalize_space(category) or "项目术语", {})
        correct_term = _normalize_space(correct)
        wrong_term = _normalize_space(wrong)
        if not correct_term or not wrong_term or correct_term == wrong_term:
            raise ValueError("纠错词必须包含不同的 wrong 和 correct")
        wrongs = category_terms.setdefault(correct_term, [])
        if wrong_term not in wrongs:
            wrongs.append(wrong_term)
        self._write_json(self.lexicon_path, payload)
        return payload

    def render_corrections_for_prompt(self) -> str:
        terms = self._combined_terms_for_prompt()
        if not terms:
            return ""
        lines = ["【行业术语纠错表】（转写中遇到左列词时，应修正为右列词）"]
        for category, mappings in terms.items():
            lines.append(f"\n{category}：")
            for correct, wrongs in mappings.items():
                lines.append(f"  {' / '.join(wrongs)} → {correct}")
        return "\n".join(lines)

    def merge_correction_candidates(
        self,
        candidates: list[dict[str, Any]],
        *,
        meeting_id: str,
    ) -> list[dict[str, Any]]:
        store = self._read_json(self.correction_candidates_path, _default_correction_candidates())
        by_id = {item.get("id"): item for item in store.get("candidates", [])}
        touched: list[dict[str, Any]] = []
        now = _now()
        official_pairs = self._official_pairs()

        for candidate in candidates:
            wrong = _normalize_space(str(candidate.get("wrong", "")))
            correct = _normalize_space(str(candidate.get("correct", "")))
            category = _normalize_space(str(candidate.get("category", ""))) or "自动沉淀术语"
            if not wrong or not correct or wrong == correct:
                continue
            if (_normalize_key(wrong), _normalize_key(correct)) in official_pairs:
                continue
            candidate_id = _entry_id(category, wrong, correct)
            entry = by_id.get(candidate_id)
            if entry is None:
                entry = {
                    "id": candidate_id,
                    "wrong": wrong,
                    "correct": correct,
                    "category": category,
                    "confidence": 0.0,
                    "reason": "",
                    "source_snippets": [],
                    "meeting_ids": [],
                    "occurrences": 0,
                    "status": "candidate",
                    "created_at": now,
                    "updated_at": now,
                }
                store.setdefault("candidates", []).append(entry)
                by_id[candidate_id] = entry
            entry["confidence"] = round(
                max(float(entry.get("confidence", 0) or 0), float(candidate.get("confidence", 0) or 0)),
                4,
            )
            if not entry.get("reason") and candidate.get("reason"):
                entry["reason"] = _normalize_space(str(candidate.get("reason", "")))
            meeting_ids = entry.setdefault("meeting_ids", [])
            if meeting_id not in meeting_ids:
                meeting_ids.append(meeting_id)
            entry["occurrences"] = len(meeting_ids)
            snippet = _normalize_space(str(candidate.get("source_snippet", "")))
            if snippet:
                snippets = entry.setdefault("source_snippets", [])
                if snippet not in snippets:
                    snippets.append(snippet)
                    del snippets[MAX_SOURCE_SNIPPETS:]
            entry["updated_at"] = now
            touched.append(dict(entry))

        store["candidates"] = sorted(
            store.get("candidates", []),
            key=lambda item: (item.get("status", ""), item.get("category", ""), item.get("correct", "")),
        )
        self._write_json(self.correction_candidates_path, store)
        return touched

    def merge_methodology_candidates(
        self,
        candidates: list[dict[str, Any]],
        *,
        meeting_id: str,
    ) -> list[dict[str, Any]]:
        store = self._read_json(self.methodology_candidates_path, _default_methodologies())
        by_id = {item.get("id"): item for item in store.get("methodologies", [])}
        touched: list[dict[str, Any]] = []
        now = _now()
        for candidate in candidates:
            name = _normalize_space(str(candidate.get("name", "")))
            if not name:
                continue
            methodology_id = "method_" + hashlib.sha1(_normalize_key(name).encode("utf-8")).hexdigest()[:16]
            entry = by_id.get(methodology_id)
            if entry is None:
                entry = {
                    "id": methodology_id,
                    "name": name,
                    "business_goal": "",
                    "principles": [],
                    "reasoning_chain": [],
                    "applicable_scope": "",
                    "evidence_refs": [],
                    "meeting_ids": [],
                    "occurrences": 0,
                    "status": "candidate",
                    "created_at": now,
                    "updated_at": now,
                }
                store.setdefault("methodologies", []).append(entry)
                by_id[methodology_id] = entry
            for field in ["business_goal", "applicable_scope"]:
                if candidate.get(field):
                    entry[field] = _normalize_space(str(candidate[field]))
            for field in ["principles", "reasoning_chain", "evidence_refs"]:
                values = candidate.get(field) or []
                if isinstance(values, str):
                    values = [values]
                entry[field] = _dedupe([*entry.get(field, []), *[str(item).strip() for item in values if str(item).strip()]])
            meeting_ids = entry.setdefault("meeting_ids", [])
            if meeting_id not in meeting_ids:
                meeting_ids.append(meeting_id)
            entry["occurrences"] = len(meeting_ids)
            entry["updated_at"] = now
            touched.append(dict(entry))

        store["methodologies"] = sorted(
            store.get("methodologies", []),
            key=lambda item: (item.get("status", ""), item.get("name", "")),
        )
        self._write_json(self.methodology_candidates_path, store)
        return touched

    def append_run(self, payload: dict[str, Any]) -> None:
        self.runs_path.parent.mkdir(parents=True, exist_ok=True)
        event = normalize_repository_paths(
            {
                "created_at": _now(),
                **payload,
            },
            root=self.root_dir,
        )
        with self.runs_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def storage_files(self) -> dict[str, str]:
        return {
            "directory": str(self.root_dir),
            "minutes_directory": str(self.minutes_dir),
            "lexicon": str(self.lexicon_path),
            "correction_candidates": str(self.correction_candidates_path),
            "methodology_candidates": str(self.methodology_candidates_path),
            "runs": str(self.runs_path),
            "schema": str(self.schema_path),
        }

    def _combined_terms_for_prompt(self) -> dict[str, dict[str, list[str]]]:
        payload = self._read_json(self.lexicon_path, _default_lexicon())
        terms = json.loads(json.dumps(
            payload.get("terms", {}).get("语音识别常见错误 → 正确写法", {}),
            ensure_ascii=False,
        ))
        candidate_store = self._read_json(self.correction_candidates_path, _default_correction_candidates())
        for entry in candidate_store.get("candidates", []):
            if not _should_auto_apply(entry):
                continue
            category = _normalize_space(entry.get("category", "")) or "自动沉淀术语"
            wrong = _normalize_space(entry.get("wrong", ""))
            correct = _normalize_space(entry.get("correct", ""))
            if not wrong or not correct or wrong == correct:
                continue
            wrongs = terms.setdefault(category, {}).setdefault(correct, [])
            if wrong not in wrongs:
                wrongs.append(wrong)
        return terms

    def _official_pairs(self) -> set[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        for mappings in self._combined_terms_for_prompt().values():
            for correct, wrongs in mappings.items():
                for wrong in wrongs:
                    pairs.add((_normalize_key(wrong), _normalize_key(correct)))
        return pairs

    def _ensure_files(self) -> None:
        if not self.lexicon_path.exists():
            self._write_json(self.lexicon_path, _default_lexicon())
        if not self.correction_candidates_path.exists():
            self._write_json(self.correction_candidates_path, _default_correction_candidates())
        if not self.methodology_candidates_path.exists():
            self._write_json(self.methodology_candidates_path, _default_methodologies())
        if not self.schema_path.exists():
            self._write_json(self.schema_path, _schema())
        self.runs_path.touch(exist_ok=True)

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, path: Path, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)


def _default_lexicon() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "_comment": "会议转写 skill 的人名和专业术语纠错库；人工确认后可写入这里。",
        "terms": {"语音识别常见错误 → 正确写法": {}},
    }


def _default_correction_candidates() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "_comment": "ASR 纠错候选；重复出现或人工确认后可进入 prompt。",
        "auto_apply_rule": {
            "min_occurrences": AUTO_APPLY_MIN_OCCURRENCES,
            "min_confidence": AUTO_APPLY_MIN_CONFIDENCE,
        },
        "candidates": [],
    }


def _default_methodologies() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "_comment": "从会议纪要中抽取的方法论候选，默认 candidate，后续由主 Agent 或人工确认。",
        "methodologies": [],
    }


def _schema() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "files": {
            "lexicon.json": "人工确认的人名、公司名、系统名、专业术语纠错库。",
            "correction_candidates.json": "模型从原始转写和最终纪要中抽取的 ASR 纠错候选。",
            "methodology_candidates.json": "模型从纪要中抽取的管理方法论、业务方法论和实施原则候选。",
            "runs.jsonl": "meeting_minutes skill 每次运行的追加式审计日志。",
            "minutes/": "按 meeting_id 和内容哈希保存的不可变正式会议纪要 Markdown。",
        },
        "boundary": "该目录只服务会议转写 skill；项目交付结论仍写入 data/store/candidate_items.json 并走人工确认。",
    }


def _safe_path_component(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in str(value).strip()
    )
    return cleaned[:96] or "meeting"


def _should_auto_apply(entry: dict[str, Any]) -> bool:
    status = entry.get("status", "candidate")
    if status == "rejected":
        return False
    if status == "approved":
        return True
    return (
        int(entry.get("occurrences", 0) or 0) >= AUTO_APPLY_MIN_OCCURRENCES
        and float(entry.get("confidence", 0) or 0) >= AUTO_APPLY_MIN_CONFIDENCE
    )


def _entry_id(category: str, wrong: str, correct: str) -> str:
    raw = f"{_normalize_key(category)}|{_normalize_key(wrong)}|{_normalize_key(correct)}"
    return "corr_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _normalize_space(text: str) -> str:
    return " ".join((text or "").split())


def _normalize_key(text: str) -> str:
    return _normalize_space(text).casefold()


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
