from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from store.sqlite_store import ProjectSQLiteStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把存量 confirmed 条目各自迁移为独立记忆线程；不调用模型、不重跑来源。"
    )
    parser.add_argument("--store-dir", default=str(PROJECT_ROOT / "data" / "store"))
    parser.add_argument("--project-id", default="", help="只迁移指定项目；默认迁移全部项目。")
    parser.add_argument("--apply", action="store_true", help="实际写入；不传时只做 dry-run。")
    parser.add_argument(
        "--backup-dir",
        default="",
        help="apply 前的 SQLite 备份目录；默认使用 <store-dir>/_archived。",
    )
    args = parser.parse_args()

    store = ProjectSQLiteStore(Path(args.store_dir))
    project_id = args.project_id or None
    preview = store.backfill_confirmed_items_as_threads(project_id=project_id, apply=False)
    if not args.apply or preview["planned"] == 0:
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0

    backup_dir = Path(args.backup_dir) if args.backup_dir else store.archive_dir
    backup_path = backup_database(store.database_path, backup_dir)
    result = store.backfill_confirmed_items_as_threads(project_id=project_id, apply=True)
    print(json.dumps({**result, "backup": str(backup_path)}, ensure_ascii=False, indent=2))
    return 0


def backup_database(database_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"project-before-memory-thread-backfill-{stamp}.db"
    with sqlite3.connect(database_path) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    return target


if __name__ == "__main__":
    raise SystemExit(main())
