from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from agent.source_ingestion import SourceIngestionService
from store.sqlite_store import ProjectSQLiteStore


StoreFactory = Callable[[], ProjectSQLiteStore]
ServiceFactory = Callable[[ProjectSQLiteStore], Any]


class IngestionWorker:
    def __init__(
        self,
        *,
        store_factory: StoreFactory,
        service_factory: ServiceFactory | None = None,
        poll_interval: float = 1.0,
        worker_id: str = "",
        enabled: bool = True,
        disabled_reason: str = "",
    ) -> None:
        self.store_factory = store_factory
        self.service_factory = service_factory or (
            lambda store: SourceIngestionService(store=store)
        )
        self.poll_interval = max(0.01, float(poll_interval))
        self.worker_id = worker_id or f"ingestion-{os.getpid()}-{uuid4().hex[:8]}"
        self.enabled = bool(enabled)
        self.disabled_reason = disabled_reason if not enabled else ""
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._recovered_jobs = 0
        self._processed_jobs = 0
        self._last_error = ""
        self._started_at = ""

    def start(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            store = self.store_factory()
            self._recovered_jobs += store.requeue_interrupted_ingestion_jobs()
            self._stop_event.clear()
            self._wake_event.clear()
            self._started_at = _now()
            self._thread = threading.Thread(
                target=self._run,
                name=self.worker_id,
                daemon=True,
            )
            self._thread.start()
            return True

    def wake(self) -> bool:
        if not self.enabled:
            return False
        self._wake_event.set()
        return True

    def shutdown(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
        with self._lock:
            if self._thread is not None and not self._thread.is_alive():
                self._thread = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            return {
                "enabled": self.enabled,
                "running": bool(thread is not None and thread.is_alive()),
                "worker_id": self.worker_id,
                "disabled_reason": self.disabled_reason,
                "recovered_jobs": self._recovered_jobs,
                "processed_jobs": self._processed_jobs,
                "last_error": self._last_error,
                "started_at": self._started_at,
            }

    def _run(self) -> None:
        store: ProjectSQLiteStore | None = None
        service: Any | None = None
        while not self._stop_event.is_set():
            try:
                if store is None:
                    store = self.store_factory()
                if service is None:
                    service = self.service_factory(store)
                result = service.process_next(worker_id=self.worker_id)
                if result is not None:
                    with self._lock:
                        self._processed_jobs += 1
                        self._last_error = ""
                    continue
            except Exception as exc:
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                service = None
            self._wake_event.wait(timeout=self.poll_interval)
            self._wake_event.clear()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
