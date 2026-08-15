from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from store.sqlite_store import ProjectSQLiteStore
from ingestion.source_manifest import build_default_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="把旧本地 JSON/JSONL 存储迁移到 SQLite project.db。")
    parser.add_argument(
        "--store-dir",
        default=str(PROJECT_ROOT / "data" / "store"),
        help="存储目录，默认 data/store。",
    )
    args = parser.parse_args()

    store = ProjectSQLiteStore(args.store_dir)
    for source in build_default_manifest():
        store.save_source(source, kind="minutes")
    counts = _table_counts(store.database_path)
    print(f"database={store.database_path}")
    print(f"archive_dir={store.archive_dir}")
    print(f"vault_dir={store.vault_dir}")
    for table, count in counts.items():
        print(f"{table}={count}")
    return 0


def _table_counts(database_path: Path) -> dict[str, int]:
    tables = ["sources", "items", "tasks", "events", "sessions", "messages", "runs", "people", "embeddings"]
    with sqlite3.connect(database_path) as conn:
        return {
            table: int(conn.execute(f"select count(*) from {table}").fetchone()[0])
            for table in tables
        }


if __name__ == "__main__":
    raise SystemExit(main())
