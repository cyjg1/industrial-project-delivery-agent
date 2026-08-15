from __future__ import annotations

from typing import Any

from skills.artifacts import EvidenceBackedAnswer, ProjectFact
from skills.contracts import SkillContext, SkillResult


class KnowledgeTraceSkillRunner:
    def run(self, payload: dict[str, Any], context: SkillContext | None = None) -> SkillResult:
        question = str(payload.get("question") or "").strip()
        if not question:
            raise ValueError("C15 需要 question")
        fact_rows, retrieval_source, retrieval_warnings = _ranked_fact_rows(question, payload, context)
        facts = [ProjectFact.from_dict(item) for item in fact_rows]
        confirmed = [fact for fact in facts if fact.status == "confirmed" and not fact.valid_to]
        candidates = [fact for fact in facts if fact.status == "candidate" and not fact.valid_to]

        if confirmed:
            selected = confirmed[:3]
            answer_text = "；".join(fact.statement or fact.title for fact in selected)
            status = "confirmed"
            unknown_reason = ""
        elif candidates:
            selected = candidates[:3]
            answer_text = "现有材料仅形成候选结论，尚未人工确认。"
            status = "pending_confirmation"
            unknown_reason = "缺少已确认的当前有效结论"
        else:
            selected = []
            answer_text = "现有可见材料中没有足够依据回答该问题。"
            status = "unknown"
            unknown_reason = "未检索到当前有效事实"

        evidence_refs = [ref for fact in selected for ref in fact.evidence_refs]
        confidences = [fact.confidence for fact in selected if fact.confidence is not None]
        answer = EvidenceBackedAnswer(
            question=question,
            answer=answer_text,
            conclusion_status=status,
            evidence_refs=evidence_refs,
            related_object_ids=[fact.object_id for fact in selected if fact.object_id],
            confidence=min(confidences) if confidences else None,
            unknown_reason=unknown_reason,
        )
        return SkillResult(
            capability_id="C15",
            skill_name="knowledge_trace",
            artifact_type="EvidenceBackedAnswer",
            artifact=answer.as_dict(),
            candidates=[{"object_id": fact.object_id, "title": fact.title} for fact in candidates[:3]],
            evidence_refs=[
                {"source_id": ref.source_id, "locator": ref.locator, "quote": ref.quote, "version": ref.version}
                for ref in evidence_refs
            ],
            confidence=answer.confidence,
            warnings=[*retrieval_warnings, *([unknown_reason] if unknown_reason else [])],
            requires_confirmation=status == "pending_confirmation",
            audit={
                "input_fact_count": len(facts),
                "selected_fact_count": len(selected),
                "retrieval_source": retrieval_source,
            },
        )


def _ranked_fact_rows(
    question: str,
    payload: dict[str, Any],
    context: SkillContext | None,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    if context is not None and context.store is not None:
        if context.actor is None or context.access_context is None:
            raise RuntimeError("C15 使用 store 检索时必须提供 actor 和 access_context")
        filters = {"project_id": context.project_id, "status": "confirmed"}
        rows = context.store.search_memory(
            question,
            filters,
            limit=3,
            actor=context.actor,
            access_context=context.access_context,
        )
        if not rows:
            rows = context.store.search_memory(
                question,
                {**filters, "status": "candidate"},
                limit=3,
                actor=context.actor,
                access_context=context.access_context,
            )
        warnings = [
            f"检索降级：{row.get('embedding_error') or '仅使用 FTS'}"
            for row in rows
            if row.get("degraded")
        ]
        return [_memory_row_as_fact(row) for row in rows], "actor_scoped_store", list(dict.fromkeys(warnings))
    if "facts" in payload:
        raise ValueError("C15 不接受未排序 facts；请提供带 store/actor 的 SkillContext 或 ranked_facts")
    return list(payload.get("ranked_facts") or []), "caller_ranked_facts", []


def _memory_row_as_fact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "object_id": row.get("item_id") or row.get("id") or "",
        "object_type": row.get("type") or row.get("category") or "unknown",
        "title": row.get("title") or "",
        "statement": row.get("description") or row.get("title") or "",
        "status": row.get("status") or "candidate",
        "confidence": row.get("score"),
        "evidence_refs": row.get("evidence_refs") or [],
    }
