from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.structured_ingestion import StructuredIngestionService  # noqa: E402
from store.sqlite_store import ProjectSQLiteStore  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Idempotently backfill normalized daily work records from stored historical work-log workbooks."
    )
    parser.add_argument(
        "--store-dir",
        default=str(PROJECT_ROOT / "data" / "store"),
        help="ProjectSQLiteStore root directory.",
    )
    parser.add_argument(
        "--source-id",
        action="append",
        default=[],
        help="Limit backfill to one or more stored source IDs.",
    )
    args = parser.parse_args()

    store = ProjectSQLiteStore(args.store_dir)
    requested = set(args.source_id)
    sources = [
        row
        for row in store.list_sources()
        if (row.get("kind") == "work_logs" or row.get("input_kind") == "work_logs")
        and (not requested or row["id"] in requested)
    ]
    if requested - {row["id"] for row in sources}:
        missing = ", ".join(sorted(requested - {row["id"] for row in sources}))
        raise SystemExit(f"Unknown work-log source IDs: {missing}")

    service = StructuredIngestionService(store=store)
    results = [service.backfill_work_log_source(row["id"]) for row in sources]
    print(json.dumps({
        "source_count": len(results),
        "row_count": sum(int(row.get("row_count", 0)) for row in results),
        "record_count": sum(int(row.get("work_record_count", 0)) for row in results),
        "results": results,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
