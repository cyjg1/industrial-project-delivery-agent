from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.meeting_ingestion import MeetingIngestionService
from agent.structured_ingestion import StructuredIngestionService
from ingestion.structured_workbook import STRUCTURED_INPUT_KINDS
from store.sqlite_store import ProjectSQLiteStore


class SourceIngestionService:
    """Dispatches claimed jobs to deterministic workbook or meeting-memory ingestion."""

    def __init__(self, *, store: ProjectSQLiteStore):
        self.store = store
        self._meeting_service: MeetingIngestionService | None = None
        self._structured_service = StructuredIngestionService(store=store)

    def process_next(self, *, worker_id: str) -> dict[str, Any] | None:
        job = self.store.claim_next_ingestion_job(worker_id=worker_id)
        if job is None:
            return None
        try:
            return self.process_job(job["id"])
        except Exception as exc:
            self.store.fail_ingestion_job(job["id"], f"{type(exc).__name__}: {exc}")
            raise

    def process_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_ingestion_job(job_id)
        input_kind = str(job["input_kind"])
        input_suffix = Path(str(job["input_path"])).suffix.lower()
        if input_kind in STRUCTURED_INPUT_KINDS or (input_kind == "auto" and input_suffix in {".xlsx", ".xlsm"}):
            return self._structured_service.process_job(job_id)
        if self._meeting_service is None:
            self._meeting_service = MeetingIngestionService(store=self.store)
        return self._meeting_service.process_job(job_id)
