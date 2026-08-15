from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.env_loader import load_project_env  # noqa: E402
from store.sqlite_store import ProjectSQLiteStore  # noqa: E402


Embedder = Callable[[list[str]], list[list[float]]]


def run_backfill(
    store: ProjectSQLiteStore,
    *,
    project_id: str | None = None,
    batch_size: int = 20,
    max_attempts: int = 3,
    retry_errors: bool = False,
    embedder: Embedder | None = None,
) -> dict[str, object]:
    recovered = store.recover_interrupted_embeddings()
    store.queue_memory_embeddings(
        project_id=project_id,
        retry_errors=retry_errors,
    )
    processed_batches = 0
    processed_items = 0
    failed_attempts = 0
    max_batches = max(
        10,
        int(store.embedding_index_status(project_id=project_id)["eligible"])
        * max(1, int(max_attempts))
        + 10,
    )
    while processed_batches < max_batches:
        result = store.process_embedding_batch(
            worker_id="memory-embedding-backfill",
            batch_size=batch_size,
            embedder=embedder,
            max_attempts=max_attempts,
            retry_delay_seconds=0,
        )
        if result is None:
            break
        processed_batches += 1
        processed_items += int(result.get("indexed") or 0)
        failed_attempts += int(result.get("failed") or 0)

    index = store.embedding_index_status(project_id=project_id)
    return {
        "project_id": project_id or "",
        "recovered_items": recovered,
        "processed_batches": processed_batches,
        "processed_items": processed_items,
        "failure_attempts": failed_attempts,
        "failed_items": int(index["error"]),
        "index": index,
    }


def main() -> int:
    load_project_env()
    parser = argparse.ArgumentParser(
        description="Build or repair the real embedding index for active project memory."
    )
    parser.add_argument(
        "--store",
        "--store-dir",
        dest="store_dir",
        default=str(PROJECT_ROOT / "data" / "store"),
        help="Directory containing project.db.",
    )
    parser.add_argument("--project-id", default="", help="Only backfill one project.")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="Reset terminal error rows before processing.",
    )
    args = parser.parse_args()

    store = ProjectSQLiteStore(args.store_dir)
    initial = store.embedding_index_status(
        project_id=args.project_id or None,
    )
    if not initial["enabled"]:
        print(
            json.dumps(
                {
                    "error": "MEMORY_EMBEDDING is off; enable it before backfill.",
                    "index": initial,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    report = run_backfill(
        store,
        project_id=args.project_id or None,
        batch_size=args.batch_size,
        max_attempts=args.max_attempts,
        retry_errors=args.retry_errors,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if bool(report["index"]["complete"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
