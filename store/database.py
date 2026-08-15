from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class SQLiteDatabase:
    def __init__(self, path: str | Path, *, lock: threading.RLock | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = lock or threading.RLock()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma foreign_keys = on")
        connection.execute("pragma secure_delete = on")
        connection.execute("pragma busy_timeout = 30000")
        connection.execute("pragma journal_mode = wal")
        try:
            yield connection
        finally:
            connection.close()
