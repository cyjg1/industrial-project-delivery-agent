from __future__ import annotations

from store.sqlite_store import ProjectSQLiteStore


# Backward-compatible import name for modules/tests that have not been renamed yet.
JsonAgentStore = ProjectSQLiteStore
