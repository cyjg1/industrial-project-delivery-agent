from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any, Callable

from agent.inspection import inspect_delivery_chain
from agent.llm_provider import get_provider
from agent.repository_paths import normalize_repository_paths, resolve_repository_path
from agent.schemas import (
    CandidateItem,
    EvidenceVerificationResult,
    ExtractionResult,
    InspectionItem,
    PeopleStructure,
    SourceDocument,
    SourceRef,
    to_plain,
)
from agent.verifier import verify_items_evidence
from agent.semantic_extraction import extract_document_semantically


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "source"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"
TEXT_SUFFIXES = {".md", ".markdown", ".txt"}


@dataclass(frozen=True)
class FileAnalysisStepSpec:
    step_id: str
    title: str
    description: str


STEP_SPECS = [
    FileAnalysisStepSpec(
        "01_source_manifest",
        "读取 source 文件夹",
        "扫描 source/ 中的输入文件，生成材料清单。",
    ),
    FileAnalysisStepSpec(
        "02_extract_candidates",
        "抽取人事法待办",
        "从可读会议纪要中抽取人员、事项、方法论和待确认问题。",
    ),
    FileAnalysisStepSpec(
        "03_verify_evidence",
        "校验证据",
        "检查每个候选是否能追溯到 source 文件中的原文。",
    ),
    FileAnalysisStepSpec(
        "04_build_review_cards",
        "生成确认卡片",
        "把候选事项转成给人逐条确认的卡片。",
    ),
    FileAnalysisStepSpec(
        "05_write_output_summary",
        "生成输出汇总",
        "汇总本轮 source 到 output 的所有产物和待人工判断点。",
    ),
]


def configured_source_dir(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    configured = os.getenv("PROJECT_AGENT_SOURCE_DIR", "").strip()
    return Path(configured) if configured else DEFAULT_SOURCE_DIR


def configured_output_dir(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    configured = os.getenv("PROJECT_AGENT_OUTPUT_DIR", "").strip()
    return Path(configured) if configured else DEFAULT_OUTPUT_DIR


class FileAnalysisWorkspace:
    def __init__(
        self,
        *,
        source_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
    ) -> None:
        self.source_dir = configured_source_dir(source_dir)
        self.output_dir = configured_output_dir(output_dir)
        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.path_roots = {
            "PROJECT_AGENT_SOURCE_DIR": self.source_dir.resolve(),
            "PROJECT_AGENT_OUTPUT_DIR": self.output_dir.resolve(),
        }
        self.state_path = self.output_dir / "analysis_state.json"

    def status(self) -> dict[str, Any]:
        state = self._load_state()
        steps = [self._step_payload(spec, state) for spec in STEP_SPECS]
        return {
            "source_dir": str(self.source_dir),
            "output_dir": str(self.output_dir),
            "state_path": str(self.state_path),
            "source_files": self._source_file_rows(),
            "steps": steps,
            "current_step_id": next((step["step_id"] for step in steps if not step["locked"] and step["status"] != "confirmed"), ""),
        }

    def run_step(self, step_id: str) -> dict[str, Any]:
        spec = self._step_spec(step_id)
        state = self._load_state()

        payload, markdown, summary = self._run_step_payload(step_id)
        json_path = self.output_dir / f"{step_id}.json"
        md_path = self.output_dir / f"{step_id}.md"
        json_path.write_text(
            json.dumps(
                normalize_repository_paths(payload, configured_roots=self.path_roots),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        md_path.write_text(markdown.rstrip() + "\n", encoding="utf-8")

        state["steps"][step_id] = {
            **state["steps"].get(step_id, {}),
            "status": "ready_for_confirmation",
            "ran_at": _now(),
            "artifact_paths": [str(json_path), str(md_path)],
            "summary": summary,
            "artifact_preview": _preview(md_path),
        }
        self._save_state(state)
        return self._step_payload(spec, state)

    def confirm_step(self, step_id: str, notes: str = "") -> dict[str, Any]:
        spec = self._step_spec(step_id)
        state = self._load_state()
        current = state["steps"].get(step_id, {})
        if current.get("status") not in {"ready_for_confirmation", "confirmed"}:
            raise PermissionError("本步还没有可确认的输出，请先运行本步。")
        state["steps"][step_id] = {
            **current,
            "status": "confirmed",
            "confirmed_at": _now(),
            "confirmation_notes": notes,
        }
        self._save_state(state)
        return self.status()

    def _run_step_payload(self, step_id: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
        runners: dict[str, Callable[[], tuple[dict[str, Any], str, dict[str, Any]]]] = {
            "01_source_manifest": self._run_source_manifest,
            "02_extract_candidates": self._run_extract_candidates,
            "03_verify_evidence": self._run_verify_evidence,
            "04_build_review_cards": self._run_review_cards,
            "05_write_output_summary": self._run_output_summary,
        }
        return runners[step_id]()

    def _run_source_manifest(self) -> tuple[dict[str, Any], str, dict[str, Any]]:
        sources = self._source_documents()
        source_files = self._source_file_rows()
        payload = {
            "source_dir": str(self.source_dir),
            "source_files": source_files,
            "source_documents": [to_plain(source) for source in sources],
        }
        markdown = _markdown_table(
            "01 读取 source 文件夹",
            ["文件", "识别类型", "日期", "状态"],
            [[row["name"], row["detected_kind"], row["meeting_date"], row["status"]] for row in source_files],
        )
        summary = {
            "source_file_count": len(source_files),
            "readable_count": sum(1 for row in source_files if row["status"] == "可读"),
            "kind_counts": _count_by_key(source_files, "detected_kind"),
        }
        return payload, markdown, summary

    def _run_extract_candidates(self) -> tuple[dict[str, Any], str, dict[str, Any]]:
        extractions = self._build_extractions()
        candidate_count = sum(_extraction_candidate_count(item) for item in extractions)
        payload = {
            "extractions": [to_plain(item) for item in extractions],
            "candidate_count": candidate_count,
        }
        rows: list[list[Any]] = []
        for extraction in extractions:
            rows.append([
                extraction.title,
                len(extraction.people),
                len(extraction.things),
                len(extraction.methods),
                len(extraction.questions),
            ])
        markdown = _markdown_table(
            "02 抽取人事法待办",
            ["材料", "人", "事", "法", "待确认"],
            rows,
        )
        return payload, markdown, {"candidate_count": candidate_count, "document_count": len(extractions)}

    def _run_verify_evidence(self) -> tuple[dict[str, Any], str, dict[str, Any]]:
        manifest = self._source_documents()
        items = _all_candidate_items(self._build_extractions())
        verification = verify_items_evidence(
            items,
            manifest,
            allowed_roots=(self.source_dir,),
        )
        payload = {
            "verification": to_plain(verification),
            "checked_items": len(items),
        }
        markdown = "\n".join([
            "# 03 校验证据",
            "",
            f"- 通过：{'是' if verification.ok else '否'}",
            f"- 检查项：{verification.checked_count}",
            f"- 错误数：{len(verification.errors)}",
            f"- 原始转写待补：{verification.raw_pending_count}",
            "",
            "## 错误",
            *[f"- {error}" for error in verification.errors],
            "",
            "## 提醒",
            *[f"- {warning}" for warning in verification.warnings[:20]],
        ])
        return payload, markdown, {
            "ok": verification.ok,
            "checked_count": verification.checked_count,
            "error_count": len(verification.errors),
            "raw_pending_count": verification.raw_pending_count,
        }

    def _run_review_cards(self) -> tuple[dict[str, Any], str, dict[str, Any]]:
        manifest = self._source_documents()
        extractions = self._build_extractions()
        people = PeopleStructure(root_title="文件验证人员资产", groups=[], people=[], scenarios=[])
        report = inspect_delivery_chain("GENERAL", "source 文件夹 / output 步骤化分析", manifest, extractions, people)
        cards = [_card_from_item(item, "人") for item in _all_people(extractions)]
        cards.extend(_card_from_item(item, "事") for item in _all_things(extractions))
        cards.extend(_card_from_item(item, "法") for item in _all_methods(extractions))
        cards.extend(_card_from_inspection(item, "待确认") for item in report.chain_gaps + report.responsibility_gaps)
        cards.extend(_card_from_inspection(item, "待办") for item in report.followup_drafts)
        payload = {
            "report": to_plain(report),
            "review_cards": cards,
            "review_card_count": len(cards),
        }
        markdown = _markdown_table(
            "04 生成确认卡片",
            ["分组", "标题", "状态"],
            [[card["section"], card["title"], card["status"]] for card in cards],
        )
        return payload, markdown, {
            "review_card_count": len(cards),
            "followup_count": len(report.followup_drafts),
        }

    def _run_output_summary(self) -> tuple[dict[str, Any], str, dict[str, Any]]:
        state = self._load_state()
        completed = [
            {
                "step_id": step.step_id,
                "title": step.title,
                "status": state["steps"].get(step.step_id, {}).get("status", "pending"),
                "artifact_paths": state["steps"].get(step.step_id, {}).get("artifact_paths", []),
            }
            for step in STEP_SPECS
        ]
        payload = {
            "source_dir": str(self.source_dir),
            "output_dir": str(self.output_dir),
            "completed_steps": completed,
        }
        markdown = _markdown_table(
            "05 生成输出汇总",
            ["步骤", "状态", "输出文件"],
            [[item["title"], item["status"], "；".join(item["artifact_paths"])] for item in completed],
        )
        return payload, markdown, {"completed_step_count": sum(1 for item in completed if item["status"] == "confirmed")}

    def _source_documents(self) -> list[SourceDocument]:
        documents: list[SourceDocument] = []
        for path in self._source_paths():
            suffix = path.suffix.lower()
            is_text = suffix in TEXT_SUFFIXES
            detected_kind = _detect_source_kind(path)
            documents.append(SourceDocument(
                doc_id=f"uploaded_file_{sha1(str(path.name).encode('utf-8')).hexdigest()[:10]}",
                title=_first_heading(path) if is_text else path.stem,
                meeting_date=_meeting_date(path.name),
                topic=detected_kind,
                curated_source=SourceRef(
                    source_type="source_folder_curated_file",
                    path=str(path) if is_text else None,
                    status="matched" if is_text else "curated_source_pending",
                ),
                raw_source=SourceRef(
                    source_type="source_folder_raw_file",
                    path=None if is_text else str(path),
                    status="raw_source_pending" if is_text else "matched",
                    notes="source 文件夹输入",
                ),
                tags=["source_folder", detected_kind, suffix.lstrip(".") or "unknown"],
            ))
        return documents

    def _build_extractions(self) -> list[ExtractionResult]:
        result: list[ExtractionResult] = []
        provider = get_provider(role="extraction")
        for source in self._source_documents():
            if not source.curated_source.path:
                continue
            content = resolve_repository_path(
                source.curated_source.path,
                allowed_roots=(self.source_dir,),
            ).read_text(encoding="utf-8", errors="replace")
            result.append(extract_document_semantically(source, content, provider=provider))
        return result

    def _source_file_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in self._source_paths():
            rows.append({
                "name": path.name,
                "path": str(path),
                "size": path.stat().st_size,
                "meeting_date": _meeting_date(path.name),
                "detected_kind": _detect_source_kind(path),
                "status": "可读" if path.suffix.lower() in TEXT_SUFFIXES else "仅登记",
            })
        return rows

    def _source_paths(self) -> list[Path]:
        return [
            path
            for path in sorted(self.source_dir.iterdir())
            if path.is_file() and not path.name.startswith(".")
        ]

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        else:
            state = {"steps": {}}
        state.setdefault("steps", {})
        for spec in STEP_SPECS:
            state["steps"].setdefault(spec.step_id, {"status": "pending"})
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        portable_state = normalize_repository_paths(state, configured_roots=self.path_roots)
        self.state_path.write_text(
            json.dumps(portable_state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _step_payload(self, spec: FileAnalysisStepSpec, state: dict[str, Any]) -> dict[str, Any]:
        stored = state["steps"].get(spec.step_id, {})
        artifact_paths = list(stored.get("artifact_paths", []))
        artifact_preview = stored.get("artifact_preview", "")
        if not artifact_preview and artifact_paths:
            artifact_preview = _preview(
                resolve_repository_path(
                    artifact_paths[-1],
                    configured_roots=self.path_roots,
                )
            )
        return {
            "step_id": spec.step_id,
            "title": spec.title,
            "description": spec.description,
            "status": stored.get("status", "pending"),
            "locked": False,
            "ran_at": stored.get("ran_at", ""),
            "confirmed_at": stored.get("confirmed_at", ""),
            "confirmation_notes": stored.get("confirmation_notes", ""),
            "artifact_paths": artifact_paths,
            "summary": stored.get("summary", {}),
            "artifact_preview": artifact_preview,
        }

    def _step_spec(self, step_id: str) -> FileAnalysisStepSpec:
        for spec in STEP_SPECS:
            if spec.step_id == step_id:
                return spec
        raise ValueError(f"Unknown file analysis step: {step_id}")


def _all_candidate_items(extractions: list[ExtractionResult]) -> list[CandidateItem]:
    return [*_all_people(extractions), *_all_things(extractions), *_all_methods(extractions), *_all_questions(extractions)]


def _all_people(extractions: list[ExtractionResult]) -> list[CandidateItem]:
    return [item for extraction in extractions for item in extraction.people]


def _all_things(extractions: list[ExtractionResult]) -> list[CandidateItem]:
    return [item for extraction in extractions for item in extraction.things + extraction.chain_links]


def _all_methods(extractions: list[ExtractionResult]) -> list[CandidateItem]:
    return [item for extraction in extractions for item in extraction.methods]


def _all_questions(extractions: list[ExtractionResult]) -> list[CandidateItem]:
    return [item for extraction in extractions for item in extraction.questions]


def _extraction_candidate_count(extraction: ExtractionResult) -> int:
    return len(extraction.people + extraction.things + extraction.methods + extraction.chain_links + extraction.questions)


def _card_from_item(item: CandidateItem, section: str) -> dict[str, Any]:
    return {
        "section": section,
        "item_id": item.item_id,
        "title": item.title,
        "description": item.description,
        "status": item.status.value if hasattr(item.status, "value") else str(item.status),
        "owner_candidates": item.owner_candidates or [],
        "evidence_refs": to_plain(item.evidence_refs),
    }


def _card_from_inspection(item: InspectionItem, section: str) -> dict[str, Any]:
    return {
        "section": section,
        "item_id": item.item_id,
        "title": item.title,
        "description": item.description,
        "status": item.status.value if hasattr(item.status, "value") else str(item.status),
        "owner_candidates": item.owner_candidates or [],
        "due_date": item.due_date or "",
        "deliverable": item.deliverable or "",
        "acceptance_criteria": item.acceptance_criteria or "",
        "evidence_refs": to_plain(item.evidence_refs),
    }


def _first_heading(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or path.stem
    return path.stem


def _meeting_date(name: str) -> str:
    match = re.search(r"(20\d{2})[-_年.]?(\d{2})[-_月.]?(\d{2})", name)
    if not match:
        return "待补充"
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def _detect_source_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    name = path.name.lower()
    chinese_name = path.name
    content = ""
    if suffix in TEXT_SUFFIXES:
        content = path.read_text(encoding="utf-8", errors="replace")[:3000].lower()
    filename_haystack = f"{name}\n{chinese_name}"
    content_haystack = content
    haystack = f"{filename_haystack}\n{content_haystack}"
    if _contains_any(filename_haystack, ["会议", "纪要", "转写", "访谈", "meeting", "minutes", "transcript"]):
        return "会议材料"
    if _contains_any(filename_haystack, ["人员", "职责", "角色", "组织架构", "xmind", "people"]):
        return "人员资产"
    if _contains_any(filename_haystack, ["里程碑", "交付目标", "计划", "进度", "milestone"]):
        return "里程碑计划"
    if _contains_any(filename_haystack, ["问题清单", "任务清单", "方法清单", "需求池", "需求清单"]):
        return "三清单素材"
    if _contains_any(content_haystack, ["会议纪要", "会议主题", "会议议题", "行动项", "会议结论"]):
        return "会议材料"
    if _contains_any(haystack, ["人员", "职责", "角色", "参会人", "组织架构", "xmind", "people"]):
        return "人员资产"
    if _contains_any(haystack, ["里程碑", "交付目标", "计划", "进度", "milestone"]):
        return "里程碑计划"
    if _contains_any(haystack, ["问题清单", "任务清单", "方法清单", "需求池", "需求清单"]):
        return "三清单素材"
    if _contains_any(haystack, ["会议", "纪要", "转写", "访谈", "meeting", "minutes", "transcript"]):
        return "会议材料"
    if suffix in {".xlsx", ".xls", ".xlsm", ".csv"}:
        return "表格材料"
    if suffix in {".xmind"}:
        return "人员资产"
    return "其他材料"


def _contains_any(value: str, needles: list[str]) -> bool:
    return any(needle.lower() in value for needle in needles)


def _count_by_key(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "未识别")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _markdown_table(title: str, headers: list[str], rows: list[list[Any]]) -> str:
    lines = [f"# {title}", ""]
    if not rows:
        return "\n".join([*lines, "暂无数据"])
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(str(value).replace("\n", " ") for value in row) + " |")
    return "\n".join(lines)


def _preview(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:6000]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
