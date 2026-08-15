from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


PRESERVED_TABLES = {
    "orgs",
    "projects",
    "project_configs",
    "users",
    "project_members",
    "topics",
    "topic_members",
    "people",
    "person_assignments",
    "person_entities",
}

FTS_TABLES = ("items_fts", "source_chunks_fts")
FTS_SHADOW_PREFIXES = tuple(f"{name}_" for name in FTS_TABLES)


def table_count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clear runtime/test data while preserving people organization and access configuration.",
    )
    parser.add_argument("--database", default="data/store/project.db")
    parser.add_argument("--backup-dir", required=True)
    args = parser.parse_args()

    database = Path(args.database).resolve()
    backup_dir = Path(args.backup_dir).resolve()
    if not database.is_file():
        raise SystemExit(f"Database not found: {database}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"project-before-git-clean-{datetime.now():%Y%m%d-%H%M%S}.db"
    shutil.copy2(database, backup)

    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        people_before = {
            table: table_count(connection, table)
            for table in ("people", "person_assignments", "person_entities")
        }
        table_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
            )
        ]

        with connection:
            for table in FTS_TABLES:
                if table in table_names:
                    connection.execute(f'DELETE FROM "{table}"')
            for table in table_names:
                if (
                    table in PRESERVED_TABLES
                    or table in FTS_TABLES
                    or table.startswith(FTS_SHADOW_PREFIXES)
                    or table == "sources"
                ):
                    continue
                connection.execute(f'DELETE FROM "{table}"')
            if "sources" in table_names:
                connection.execute("DELETE FROM sources WHERE kind <> 'people_structure'")

        people_after = {
            table: table_count(connection, table)
            for table in ("people", "person_assignments", "person_entities")
        }
        if people_after != people_before:
            raise RuntimeError(
                f"People organization changed during cleanup: before={people_before}, after={people_after}",
            )
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {integrity}")
        connection.execute("VACUUM")
    finally:
        connection.close()

    print(f"Backup: {backup}")
    print(f"Preserved people organization: {people_after}")
    print("Runtime/test data cleanup completed.")


if __name__ == "__main__":
    main()
