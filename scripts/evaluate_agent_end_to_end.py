from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.access_policy import User  # noqa: E402
from agent.conversation_agent import (  # noqa: E402
    _conversation_round_trace,
    iter_conversation_agent_events,
)
from agent.env_loader import load_project_env  # noqa: E402
from agent.evaluation import (  # noqa: E402
    actors_for_case,
    evaluate_conversation_result,
    load_agent_evaluation_cases,
)
from agent.project_context import load_project_context  # noqa: E402
from agent.runtime import AgentRuntime  # noqa: E402
from agent.tools import build_default_registry  # noqa: E402
from store.sqlite_store import ProjectSQLiteStore  # noqa: E402


def main() -> int:
    load_project_env()
    parser = argparse.ArgumentParser(
        description=(
            "Run the real conversation Agent against the project evaluation set. "
            "Machine checks never auto-pass semantic answer quality."
        )
    )
    parser.add_argument(
        "--store",
        default=str(PROJECT_ROOT / "data" / "store"),
        help="Project store directory. It is cloned unless --in-place is set.",
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
            / "agent_end_to_end_evaluation.json"
        ),
    )
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--actor-id", default="u_pm")
    parser.add_argument("--model", default="")
    parser.add_argument("--max-rounds", type=int, default=8)
    parser.add_argument(
        "--memory-skill-mode",
        choices=("off", "governed"),
        default="governed",
        help="Disable or enable governed hierarchical memory-skill retrieval.",
    )
    parser.add_argument("--in-place", action="store_true")
    args = parser.parse_args()
    os.environ["MEMORY_SKILL_RETRIEVAL_MODE"] = args.memory_skill_mode

    cases = load_agent_evaluation_cases(args.questions)
    if args.case_ids:
        selected = set(args.case_ids)
        cases = [case for case in cases if case["id"] in selected]
    if not cases:
        parser.error("No evaluation cases were selected.")

    source_store_dir = Path(args.store).resolve()
    max_rounds = max(1, min(args.max_rounds, 20))
    with _evaluation_store(source_store_dir, in_place=args.in_place) as store_dir:
        store = ProjectSQLiteStore(store_dir)
        runtime = AgentRuntime(
            tool_registry=build_default_registry(store),
            store=store,
        )
        results: list[dict[str, Any]] = []
        for case in cases:
            for actor_id in actors_for_case(case, args.actor_id):
                if case["id"] == "Q12":
                    results.append(
                        evaluate_conversation_result(
                            case,
                            None,
                            actor_id=actor_id,
                            max_rounds=max_rounds,
                            precondition_error=(
                                "Q12 requires a caller-supplied new meeting file; "
                                "the harness will not fabricate evidence."
                            ),
                        )
                    )
                    continue
                try:
                    actor = _load_actor(store, actor_id)
                    project_id = store.default_project_id_for_actor(
                        actor.id,
                        actor.org_id,
                    )
                    milestone = load_project_context(
                        store,
                        project_id,
                    ).default_milestone
                    sink = _EvaluationEventSink()
                    payload: dict[str, Any] | None = None
                    streamed_tool_steps: list[dict[str, Any]] = []
                    runtime_error = ""
                    try:
                        for event in iter_conversation_agent_events(
                            store=store,
                            runtime=runtime,
                            model_id=args.model,
                            view="overview",
                            message=case["question"],
                            milestone=milestone,
                            actor=actor,
                            access_context=store.access_context_for_actor(actor.id),
                            max_rounds=max_rounds,
                            event_sink=sink,
                        ):
                            if event["event"] == "tool_result":
                                streamed_tool_steps.append(dict(event["data"]))
                            elif event["event"] == "final":
                                payload = dict(event["data"])
                    except Exception as exc:
                        runtime_error = f"{type(exc).__name__}: {exc}"
                    if payload is None:
                        payload = _failed_runtime_payload(
                            sink.events,
                            streamed_tool_steps,
                            runtime_error or "Conversation runtime returned no final payload.",
                        )
                    results.append(
                        evaluate_conversation_result(
                            case,
                            payload,
                            actor_id=actor_id,
                            max_rounds=max_rounds,
                        )
                    )
                except Exception as exc:
                    results.append(
                        {
                            "case_id": case["id"],
                            "actor_id": actor_id,
                            "status": "runtime_failed",
                            "machine_passed": False,
                            "semantic_quality_auto_passed": False,
                            "human_judgement": "pending",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_store": str(source_store_dir),
        "store_mode": "in_place" if args.in_place else "temporary_clone",
        "model": args.model or "configured_default",
        "max_rounds": max_rounds,
        "memory_skill_mode": args.memory_skill_mode,
        "case_count": len(cases),
        "run_count": len(results),
        "machine_pass_count": sum(
            1 for row in results if row.get("machine_passed") is True
        ),
        "semantic_quality_auto_passed": False,
        "human_judgement": "pending",
        "results": results,
    }
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
                "case_count": report["case_count"],
                "run_count": report["run_count"],
                "machine_pass_count": report["machine_pass_count"],
                "semantic_quality": "pending_human_review",
            },
            ensure_ascii=False,
        )
    )
    return 0


def _load_actor(store: ProjectSQLiteStore, actor_id: str) -> User:
    row = store.get_access_user(actor_id)
    if row is None:
        raise ValueError(f"Unknown actor: {actor_id}")
    return User(
        id=str(row["id"]),
        org_id=str(row["org_id"]),
        name=str(row["name"]),
        feishu_id=str(row.get("feishu_id") or ""),
    )


class _evaluation_store:
    def __init__(self, source_dir: Path, *, in_place: bool) -> None:
        self.source_dir = source_dir
        self.in_place = in_place
        self.tempdir: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> Path:
        if self.in_place:
            return self.source_dir
        self.tempdir = tempfile.TemporaryDirectory(prefix="project-agent-eval-")
        target = Path(self.tempdir.name) / "store"
        target.mkdir(parents=True, exist_ok=True)
        for source in self.source_dir.iterdir():
            if source.name in {
                "project.db",
                "project.db-shm",
                "project.db-wal",
            }:
                continue
            destination = target / source.name
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
        source_db = self.source_dir / "project.db"
        if source_db.exists():
            with (
                closing(sqlite3.connect(source_db)) as source_connection,
                closing(sqlite3.connect(target / "project.db")) as target_connection,
            ):
                source_connection.backup(target_connection)
        return target

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.tempdir is not None:
            self.tempdir.cleanup()


class _EvaluationEventSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.checkpoints: list[dict[str, Any]] = []

    def append(self, event: Any) -> None:
        self.events.append({
            "kind": str(event.kind),
            "created_at": str(event.created_at),
            "payload": dict(event.payload),
        })

    def checkpoint(self, run_id: str, checkpoint: dict[str, Any]) -> None:
        self.checkpoints.append({
            "run_id": run_id,
            "checkpoint": dict(checkpoint),
        })


def _failed_runtime_payload(
    runtime_events: list[dict[str, Any]],
    tool_steps: list[dict[str, Any]],
    error: str,
) -> dict[str, Any]:
    stop_reason = ""
    for event in reversed(runtime_events):
        if event.get("kind") == "run_stopped":
            stop_reason = str(event.get("payload", {}).get("stop_reason") or "")
            break
    return {
        "reply": "",
        "tool_steps": tool_steps,
        "verification": {
            "checked": False,
            "unsupported": [],
            "invalid_fact_ids": [],
        },
        "stop_reason": stop_reason or "runtime_failed",
        "runtime_error": error,
        "debug": {
            "rounds": _conversation_round_trace(runtime_events, tool_steps),
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
