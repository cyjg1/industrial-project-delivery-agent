from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from store.sqlite_store import ProjectSQLiteStore


StoreFactory = Callable[[], ProjectSQLiteStore]
Embedder = Callable[[list[str]], list[list[float]]]


class MemoryEmbeddingWorker:
    """Builds content-hashed item embeddings outside request and write paths."""

    def __init__(
        self,
        *,
        store_factory: StoreFactory,
        embedder: Embedder | None = None,
        poll_interval: float = 2.0,
        batch_size: int = 20,
        max_attempts: int = 3,
        retry_delay_seconds: int = 30,
        worker_id: str = "",
        enabled: bool = True,
        disabled_reason: str = "",
    ) -> None:
        self.store_factory = store_factory
        self.embedder = embedder
        self.poll_interval = max(0.05, float(poll_interval))
        self.batch_size = max(1, min(int(batch_size), 128))
        self.max_attempts = max(1, int(max_attempts))
        self.retry_delay_seconds = max(0, int(retry_delay_seconds))
        self.worker_id = (
            worker_id
            or f"memory-embedding-{os.getpid()}-{uuid4().hex[:8]}"
        )
        self.enabled = bool(enabled)
        self.disabled_reason = disabled_reason if not enabled else ""
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._processed_batches = 0
        self._processed_items = 0
        self._failed_items = 0
        self._recovered_items = 0
        self._last_error = ""
        self._started_at = ""

    def start(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            store = self.store_factory()
            self._recovered_items += store.recover_interrupted_embeddings()
            store.queue_memory_embeddings()
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
            thread.join(timeout=10.0)
        with self._lock:
            if self._thread is not None and not self._thread.is_alive():
                self._thread = None

    def process_once(self) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        store = self.store_factory()
        result = store.process_embedding_batch(
            worker_id=self.worker_id,
            batch_size=self.batch_size,
            embedder=self.embedder,
            max_attempts=self.max_attempts,
            retry_delay_seconds=self.retry_delay_seconds,
        )
        if result is None:
            return None
        with self._lock:
            self._processed_batches += 1
            self._processed_items += int(result.get("indexed") or 0)
            self._failed_items += int(result.get("failed") or 0)
            self._last_error = str(result.get("error") or "")
        return result

    def status(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            runtime = {
                "enabled": self.enabled,
                "running": bool(thread is not None and thread.is_alive()),
                "worker_id": self.worker_id,
                "disabled_reason": self.disabled_reason,
                "processed_batches": self._processed_batches,
                "processed_items": self._processed_items,
                "failed_items": self._failed_items,
                "recovered_items": self._recovered_items,
                "last_error": self._last_error,
                "started_at": self._started_at,
            }
        if self.enabled:
            try:
                runtime["index"] = self.store_factory().embedding_index_status()
            except Exception as exc:
                runtime["index"] = {}
                runtime["last_error"] = f"{type(exc).__name__}: {exc}"
        else:
            runtime["index"] = {
                "enabled": False,
                "complete": False,
            }
        return runtime

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                result = self.process_once()
            except Exception as exc:
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                result = None
            if result is not None and not result.get("error"):
                continue
            self._wake_event.wait(timeout=self.poll_interval)
            self._wake_event.clear()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
