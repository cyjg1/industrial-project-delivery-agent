from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.access_policy import User  # noqa: E402
from agent.env_loader import load_project_env  # noqa: E402
from store.sqlite_store import ProjectSQLiteStore  # noqa: E402


QUESTION_PATTERN = re.compile(
    r"^### (?P<id>Q\d{2}) (?P<title>.+?)\n"
    r"- 问题：(?P<question>.+?)\n"
    r"- 检索查询：(?P<search_query>.+?)\n"
    r"- 检索意图：(?P<intent>.+?)\n"
    r"- 预期工具：(?P<tools>.+?)\n",
    re.MULTILINE,
)


def load_questions(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    questions: list[dict[str, Any]] = []
    for match in QUESTION_PATTERN.finditer(text):
        questions.append(
            {
                "id": match.group("id"),
                "title": match.group("title").strip(),
                "question": match.group("question").strip(),
                "search_query": match.group("search_query").strip(),
                "intent": match.group("intent").strip(),
                "expected_tools": re.findall(r"`([^`]+)`", match.group("tools")),
            }
        )
    return questions


def evaluate(
    *,
    store: ProjectSQLiteStore,
    questions: list[dict[str, Any]],
    actor_id: str,
    limit: int,
) -> dict[str, Any]:
    actor_row = store.get_access_user(actor_id)
    if actor_row is None:
        raise ValueError(f"Unknown actor: {actor_id}")
    actor = User(
        id=str(actor_row["id"]),
        org_id=str(actor_row["org_id"]),
        name=str(actor_row["name"]),
        feishu_id=str(actor_row.get("feishu_id") or ""),
    )
    access_context = store.access_context_for_actor(actor.id)
    project_id = store.default_project_id_for_actor(actor.id, actor.org_id)
    results: list[dict[str, Any]] = []
    for question in questions:
        diagnostics: dict[str, Any] = {}
        rows = store.search_memory(
            question["search_query"],
            {"project_id": project_id},
            limit=limit,
            actor=actor,
            access_context=access_context,
            diagnostics=diagnostics,
        )
        source_rows = store.search_source_evidence(
            question["search_query"],
            actor=actor,
            access_context=access_context,
            project_id=project_id,
            limit=min(limit, 5),
        )
        results.append(
            {
                **question,
                "retrieval": [
                    {
                        "item_id": row["item_id"],
                        "title": row["title"],
                        "source": row.get("source") or {},
                        "score": row.get("score"),
                        "lexical_score": row.get("lexical_score"),
                        "semantic_score": row.get("semantic_score"),
                        "recency_score": row.get("recency_score"),
                        "match_reasons": row.get("match_reasons") or [],
                    }
                    for row in rows
                ],
                "source_evidence": [
                    {
                        "source_id": row["source_id"],
                        "source_title": row["source_title"],
                        "meeting_date": row["meeting_date"],
                        "locator": row["locator"],
                        "highlight": row["highlight"],
                        "score": row["score"],
                    }
                    for row in source_rows
                ],
                "diagnostics": diagnostics,
                "human_judgement": "pending",
                "human_notes": "",
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "actor_id": actor.id,
        "project_id": project_id,
        "question_count": len(results),
        "semantic_quality_auto_passed": False,
        "results": results,
    }


def main() -> int:
    load_project_env()
    parser = argparse.ArgumentParser(
        description="Record memory retrieval evidence for human review; never auto-pass answer quality."
    )
    parser.add_argument(
        "--store",
        "--store-dir",
        dest="store_dir",
        default=str(PROJECT_ROOT / "data" / "store"),
    )
    parser.add_argument(
        "--questions",
        default=str(
            PROJECT_ROOT
            / "docs"
            / "test_reports"
            / "memory_retrieval_question_set.md"
        ),
    )
    parser.add_argument(
        "--output",
        default=str(
            PROJECT_ROOT
            / "docs"
            / "test_reports"
            / "memory_retrieval_evaluation.json"
        ),
    )
    parser.add_argument("--actor-id", default="u_pmo")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    questions = load_questions(Path(args.questions))
    if not questions:
        parser.error("No review questions were parsed.")
    report = evaluate(
        store=ProjectSQLiteStore(args.store_dir),
        questions=questions,
        actor_id=args.actor_id,
        limit=max(1, min(args.limit, 20)),
    )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output_path),
                "question_count": report["question_count"],
                "human_judgement": "pending",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
