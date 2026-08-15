from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from skills.meeting_minutes.extractors import (
    build_correction_extraction_prompt,
    build_methodology_extraction_prompt,
    parse_correction_candidates,
    parse_methodology_candidates,
)
from skills.meeting_minutes.llm import MeetingMinutesModelClient, create_meeting_minutes_client
from skills.meeting_minutes.prompting import (
    MeetingMinutesPromptInput,
    MeetingMinutesReducePromptInput,
    TranscriptMapPromptInput,
    build_meeting_minutes_prompt,
    build_meeting_minutes_reduce_prompt,
    build_transcript_map_prompt,
)
from skills.meeting_minutes.store import MeetingMinutesSkillStore
from skills.meeting_minutes.transcript import (
    chunk_transcript,
    estimate_tokens,
    needs_preprocessing,
    parse_transcript,
    transcript_coverage,
)


class MeetingMinutesSkillRunner:
    def __init__(
        self,
        *,
        store: MeetingMinutesSkillStore,
        model_client: MeetingMinutesModelClient | None = None,
    ):
        self.store = store
        self.model_client = model_client or create_meeting_minutes_client()

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        meeting_id = str(payload.get("meeting_id") or _new_meeting_id(payload.get("doc_nature", "meeting")))
        meeting_date = str(payload.get("meeting_date") or datetime.now(timezone.utc).date().isoformat())
        transcript_text = self._load_transcript(payload)
        if payload.get("ai_summary_text"):
            transcript_text = (
                transcript_text
                + "\n\n【以下为会议自动纪要，可作为补充参考，但以原始转写为准】\n\n"
                + str(payload["ai_summary_text"])
            )
        audit: dict[str, Any] = {
            "skill": "meeting_minutes",
            "meeting_id": meeting_id,
            "model_calls": [],
        }
        threshold = _configured_int(
            payload,
            "preprocess_threshold_tokens",
            "MEETING_MINUTES_PREPROCESS_THRESHOLD_TOKENS",
            20000,
        )
        is_long = needs_preprocessing(transcript_text, threshold)
        map_summaries: list[str] = []
        if is_long:
            chunks = chunk_transcript(
                transcript_text,
                max_chunk_tokens=_configured_int(
                    payload,
                    "chunk_max_tokens",
                    "MEETING_MINUTES_CHUNK_MAX_TOKENS",
                    8000,
                ),
                overlap_chars=_configured_int(
                    payload,
                    "chunk_overlap_chars",
                    "MEETING_MINUTES_CHUNK_OVERLAP_CHARS",
                    200,
                    minimum=0,
                ),
            )
            coverage = transcript_coverage(transcript_text, chunks)
            _notify_chunks(payload, chunks, coverage)
            reduce_budget = max(
                4000,
                _configured_int(
                    payload,
                    "reduce_summary_budget_chars",
                    "MEETING_MINUTES_REDUCE_SUMMARY_BUDGET_CHARS",
                    60000,
                ),
            )
            configured_map_limit = max(
                300,
                _configured_int(
                    payload,
                    "map_summary_max_chars",
                    "MEETING_MINUTES_MAP_SUMMARY_MAX_CHARS",
                    2500,
                ),
            )
            per_summary_limit = min(
                configured_map_limit,
                max(300, reduce_budget // max(1, len(chunks))),
            )
            resumed_summaries = dict(payload.get("resume_chunk_summaries") or {})
            for chunk in chunks:
                resumed = resumed_summaries.get(chunk.index)
                if resumed is None:
                    resumed = resumed_summaries.get(str(chunk.index))
                if resumed is not None:
                    summary = _bounded_map_summary(str(resumed), per_summary_limit)
                    map_summaries.append(summary)
                    audit.setdefault("resumed_chunk_indexes", []).append(chunk.index)
                    continue
                map_prompt = build_transcript_map_prompt(
                    TranscriptMapPromptInput(
                        chunk_text=chunk.text,
                        chunk_index=chunk.index,
                        chunk_count=len(chunks),
                        start_char=chunk.start_char,
                        end_char=chunk.end_char,
                    )
                )
                _notify_chunk_started(payload, chunk.index)
                map_response = self._call_model(
                    stage="transcript_map",
                    system_prompt=map_prompt.system_prompt,
                    user_message=map_prompt.user_message,
                    audit=audit,
                )
                summary = _bounded_map_summary(map_response, per_summary_limit)
                map_summaries.append(summary)
                _notify_chunk_completed(payload, chunk.index, summary)
            reduce_prompt = build_meeting_minutes_reduce_prompt(
                MeetingMinutesReducePromptInput(
                    summaries=map_summaries,
                    doc_nature=str(payload.get("doc_nature") or "会议纪要"),
                    continuation_info=str(payload.get("continuation_info") or "（无）"),
                    output_format=str(
                        payload.get("output_format")
                        or "输出正式会议纪要、决策清单、待办事项和方法论候选。"
                    ),
                    project_background=str(payload.get("project_background") or "（暂无项目固定背景）"),
                    style_sample=str(payload.get("style_sample") or "（未提供参考样本）"),
                ),
                store=self.store,
            )
            minutes_markdown = self._call_model(
                stage="minutes_reduce",
                system_prompt=reduce_prompt.system_prompt,
                user_message=reduce_prompt.user_message,
                audit=audit,
            )
            correction_context = "\n\n".join(map_summaries)
        else:
            chunks = chunk_transcript(
                transcript_text,
                max_chunk_tokens=max(1, estimate_tokens(transcript_text) + 1),
                overlap_chars=0,
            )
            coverage = transcript_coverage(transcript_text, chunks)
            _notify_chunks(payload, chunks, coverage)
            prompt = build_meeting_minutes_prompt(
                MeetingMinutesPromptInput(
                    transcript=transcript_text,
                    doc_nature=str(payload.get("doc_nature") or "会议纪要"),
                    continuation_info=str(payload.get("continuation_info") or "（无）"),
                    output_format=str(
                        payload.get("output_format")
                        or "输出正式会议纪要、决策清单、待办事项和方法论候选。"
                    ),
                    project_background=str(payload.get("project_background") or "（暂无项目固定背景）"),
                    style_sample=str(payload.get("style_sample") or "（未提供参考样本）"),
                ),
                store=self.store,
            )
            minutes_markdown = self._call_model(
                stage="minutes_generation",
                system_prompt=prompt.system_prompt,
                user_message=prompt.user_message,
                audit=audit,
            )
            _notify_chunk_completed(payload, chunks[0].index, "minutes_generation_complete")
            correction_context = transcript_text

        minutes_path = self.store.save_minutes(
            meeting_id=meeting_id,
            meeting_date=meeting_date,
            markdown=minutes_markdown,
        )

        correction_prompt = build_correction_extraction_prompt(
            transcript_text=correction_context,
            minutes_markdown=minutes_markdown,
            known_terms=self.store.render_corrections_for_prompt(),
        )
        correction_response = self._call_model(
            stage="asr_correction_extraction",
            system_prompt=correction_prompt.system_prompt,
            user_message=correction_prompt.user_message,
            audit=audit,
        )
        correction_candidates = parse_correction_candidates(correction_response)
        stored_corrections = self.store.merge_correction_candidates(
            correction_candidates,
            meeting_id=meeting_id,
        )

        methodology_prompt = build_methodology_extraction_prompt(
            minutes_markdown=minutes_markdown,
            meeting_id=meeting_id,
        )
        methodology_response = self._call_model(
            stage="methodology_extraction",
            system_prompt=methodology_prompt.system_prompt,
            user_message=methodology_prompt.user_message,
            audit=audit,
        )
        methodology_candidates = parse_methodology_candidates(methodology_response)
        stored_methodologies = self.store.merge_methodology_candidates(
            methodology_candidates,
            meeting_id=meeting_id,
        )

        result = {
            "skill_name": "meeting_minutes",
            "meeting_id": meeting_id,
            "meeting_date": meeting_date,
            "transcript_text": transcript_text,
            "minutes_markdown": minutes_markdown,
            "minutes_path": str(minutes_path),
            "model_used": self.model_client.model_name,
            "tokens_estimated": estimate_tokens(transcript_text),
            "chunk_count": len(chunks),
            "processed_chunk_count": len(chunks),
            "coverage": coverage,
            "chunk_metadata": [
                {
                    "index": chunk.index,
                    "start_char": chunk.start_char,
                    "end_char": chunk.end_char,
                    "content_hash": chunk.content_hash,
                }
                for chunk in chunks
            ],
            "asr_correction_candidates": stored_corrections,
            "methodology_candidates": stored_methodologies,
            "prompt_audit": audit,
            "stored_files": self.store.storage_files(),
        }
        self.store.append_run(
            {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "model_used": self.model_client.model_name,
                "tokens_estimated": result["tokens_estimated"],
                "chunk_count": result["chunk_count"],
                "processed_chunk_count": result["processed_chunk_count"],
                "coverage": result["coverage"],
                "minutes_path": str(minutes_path),
                "correction_candidate_count": len(stored_corrections),
                "methodology_candidate_count": len(stored_methodologies),
                "stored_files": self.store.storage_files(),
            }
        )
        return result

    def _load_transcript(self, payload: dict[str, Any]) -> str:
        if payload.get("transcript_text"):
            return str(payload["transcript_text"])
        if payload.get("transcript_path"):
            return parse_transcript(Path(str(payload["transcript_path"])))
        raise ValueError("meeting_minutes skill 需要 transcript_text 或 transcript_path。")

    def _call_model(
        self,
        *,
        stage: str,
        system_prompt: str,
        user_message: str,
        audit: dict[str, Any],
    ) -> str:
        response = self.model_client.generate(system_prompt, user_message)
        audit["model_calls"].append(
            {
                "stage": stage,
                "system_prompt_chars": len(system_prompt),
                "user_message_chars": len(user_message),
                "system_prompt_preview": _preview(system_prompt),
                "user_message_preview": _preview(user_message),
                "response_chars": len(response),
                "response_preview": _preview(response),
            }
        )
        return response


def _preview(value: str, limit: int = 600) -> str:
    text = value or ""
    return text if len(text) <= limit else text[:limit] + "...（已截断）"


def _configured_int(
    payload: dict[str, Any],
    payload_key: str,
    environment_key: str,
    default: int,
    *,
    minimum: int = 1,
) -> int:
    raw = payload.get(payload_key)
    if raw is None or raw == "":
        raw = os.getenv(environment_key, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{environment_key} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{environment_key} must be >= {minimum}")
    return value


def _bounded_map_summary(value: str, limit: int) -> str:
    text = (value or "").strip()
    if not text:
        return "（该分块模型未抽取到可用内容）"
    if len(text) <= limit:
        return text
    marker = "\n...[分块摘要已按预算截断]"
    return text[: max(1, limit - len(marker))] + marker


def _notify_chunks(payload: dict[str, Any], chunks: list[Any], coverage: dict[str, int]) -> None:
    callback = payload.get("on_chunks")
    if callable(callback):
        callback(chunks, coverage)


def _notify_chunk_completed(payload: dict[str, Any], chunk_index: int, summary: str) -> None:
    callback = payload.get("on_chunk_completed")
    if callable(callback):
        callback(chunk_index, summary)


def _notify_chunk_started(payload: dict[str, Any], chunk_index: int) -> None:
    callback = payload.get("on_chunk_started")
    if callable(callback):
        callback(chunk_index)


def _new_meeting_id(doc_nature: str) -> str:
    safe = "".join(character for character in str(doc_nature)[:24] if character.isalnum() or character in "-_")
    safe = safe or "meeting"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"meeting_{timestamp}_{safe}_{uuid4().hex[:6]}"
