from __future__ import annotations

import os
import threading
from datetime import date, datetime, timezone
from typing import Any, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from agent.access_policy import User
from agent.inspection_agent import InspectionTrigger


class InspectionDispatcher:
    """Converts deterministic project events into idempotent inspection jobs."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.jobs = store.inspection_jobs

    def dispatch_new_events(self, *, limit: int = 500) -> list[dict[str, Any]]:
        cursor = self.jobs.dispatch_cursor()
        created: list[dict[str, Any]] = []
        for event in self.jobs.event_records_after(cursor, limit=limit):
            job = self._job_for_event(event)
            if job is not None:
                created.append(job)
            self.jobs.set_dispatch_cursor(event["rowid"])
        return created

    def enqueue_manual(
        self,
        *,
        actor: User,
        project_id: str,
        source_ids: list[str] | None = None,
        reason: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        project = self._project(project_id)
        return self.jobs.enqueue(
            org_id=project["org_id"],
            project_id=project_id,
            actor_id=actor.id,
            author_id=actor.id,
            trigger_type="manual",
            entity_type="project",
            entity_id=project_id,
            source_ids=source_ids or [],
            payload={"reason": reason},
            idempotency_key=f"manual:{project_id}:{request_id or uuid4().hex}",
        )

    def enqueue_scheduled(
        self,
        *,
        project_id: str,
        inspection_date: date | None = None,
    ) -> dict[str, Any]:
        project = self._project(project_id)
        active_date = inspection_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
        return self.jobs.enqueue(
            org_id=project["org_id"],
            project_id=project_id,
            actor_id=project["owner_id"],
            author_id=project["owner_id"],
            trigger_type="scheduled_daily",
            entity_type="project",
            entity_id=project_id,
            payload={"inspection_date": active_date.isoformat()},
            idempotency_key=f"scheduled_daily:{project_id}:{active_date.isoformat()}",
        )

    def _job_for_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        action = event["action"]
        payload = dict(event.get("payload") or {})
        if action == "ingestion_job_completed":
            project_id = str(payload.get("project_id") or "")
            source_id = str(payload.get("source_id") or "")
            if not project_id or not source_id:
                return None
            return self._enqueue_event(
                event,
                project_id=project_id,
                trigger_type="ingestion_completed",
                source_ids=[source_id],
                topic_id=payload.get("topic_id"),
                author_id=str(payload.get("author_id") or payload.get("actor_id") or ""),
                sensitivity=str(payload.get("sensitivity") or "l3"),
            )
        if action == "work_item_updated":
            item = dict(payload.get("work_item") or {})
            if str(item.get("status") or "") != "done":
                return None
            source_ids = [
                str(ref.get("source_doc_id") or "")
                for ref in item.get("evidence_refs") or []
                if ref.get("source_doc_id")
            ]
            return self._enqueue_event(
                event,
                project_id=str(item.get("project_id") or ""),
                trigger_type="work_item_completed",
                source_ids=source_ids,
                topic_id=item.get("topic_id"),
                author_id=str(payload.get("editor") or item.get("author_id") or ""),
                sensitivity=str(item.get("sensitivity") or "l3"),
            )
        if action == "deliverable_updated":
            try:
                deliverable = self.store.get_deliverable(event["entity_id"])
            except KeyError:
                return None
            return self._enqueue_event(
                event,
                project_id=str(deliverable.get("project_id") or ""),
                trigger_type="deliverable_updated",
                source_ids=[],
                topic_id=deliverable.get("topic_id"),
                author_id=str(payload.get("actor_id") or deliverable.get("author_id") or ""),
                sensitivity=str(deliverable.get("sensitivity") or "l3"),
            )
        return None

    def _enqueue_event(
        self,
        event: dict[str, Any],
        *,
        project_id: str,
        trigger_type: str,
        source_ids: list[str],
        topic_id: str | None,
        author_id: str,
        sensitivity: str,
    ) -> dict[str, Any] | None:
        if not project_id:
            return None
        project = self._project(project_id)
        return self.jobs.enqueue(
            org_id=project["org_id"],
            project_id=project_id,
            topic_id=topic_id,
            actor_id=project["owner_id"],
            author_id=author_id or project["owner_id"],
            sensitivity=sensitivity if sensitivity in {"l1", "l2", "l3", "l4"} else "l3",
            trigger_type=trigger_type,
            entity_type=event["entity"],
            entity_id=event["entity_id"],
            event_id=event["event_id"],
            source_ids=source_ids,
            payload={
                "event_action": event["action"],
                "event_created_at": event["created_at"],
                "event_payload": event["payload"],
            },
            idempotency_key=f"event:{event['event_id']}",
        )

    def _project(self, project_id: str) -> dict[str, Any]:
        project = self.store.get_project(project_id)
        if project is None:
            raise KeyError(f"Unknown project: {project_id}")
        return project


class InspectionWorker:
    def __init__(
        self,
        *,
        store_factory: Callable[[], Any],
        runtime_factory: Callable[[Any], Any],
        milestone_loader: Callable[[Any, str], Any],
        poll_interval: float = 1.0,
        enabled: bool = True,
        worker_id: str = "",
    ) -> None:
        self.store_factory = store_factory
        self.runtime_factory = runtime_factory
        self.milestone_loader = milestone_loader
        self.poll_interval = max(0.05, float(poll_interval))
        self.enabled = bool(enabled)
        self.worker_id = worker_id or f"inspection-{os.getpid()}-{uuid4().hex[:8]}"
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._processed_jobs = 0
        self._last_error = ""
        self._started_at = ""

    def start(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
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
            thread.join(timeout=10)

    def process_once(self) -> dict[str, Any] | None:
        store = self.store_factory()
        try:
            InspectionDispatcher(store).dispatch_new_events()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._last_error = error
            return {"status": "dispatch_failed", "error": error}
        job = store.inspection_jobs.claim_next(worker_id=self.worker_id)
        if job is None:
            return None
        try:
            actor = _actor(store, job["actor_id"])
            context = store.access_context_for_actor(actor.id)
            trigger = InspectionTrigger(
                trigger_type=job["trigger_type"],
                entity_type=job["entity_type"],
                entity_id=job["entity_id"],
                event_id=job["event_id"],
                source_ids=tuple(job["source_ids"]),
                evidence=dict(job["payload"]),
            )
            run = self.runtime_factory(store).run_inspection(
                self.milestone_loader(store, job["project_id"]),
                actor=actor,
                access_context=context,
                project_id=job["project_id"],
                trigger=trigger,
            )
            if run.status != "completed":
                raise RuntimeError(run.error or f"inspection run stopped: {run.stop_reason}")
            candidate_ids = list(run.confirmed_item_ids)
            if run.final_report is not None:
                candidate_ids = [
                    item.item_id
                    for item in (
                        run.final_report.chain_gaps
                        + run.final_report.responsibility_gaps
                        + run.final_report.followup_drafts
                    )
                ]
            result = {
                "status": "no_change" if not candidate_ids else "candidates_created",
                "candidate_ids": candidate_ids,
                "candidate_count": len(candidate_ids),
                "no_change_reason": (
                    str(getattr(run, "no_change_reason", "") or "")
                    if not candidate_ids
                    else ""
                ),
                "verification_ok": bool(run.verification and run.verification.ok),
                "stop_reason": run.stop_reason,
            }
            completed = store.inspection_jobs.complete(
                job["job_id"],
                result_run_id=run.run_id,
                result=result,
            )
            with self._lock:
                self._processed_jobs += 1
                self._last_error = ""
            return completed
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            failed = store.inspection_jobs.fail(job["job_id"], error=error)
            with self._lock:
                self._last_error = error
            return failed

    def status(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            return {
                "enabled": self.enabled,
                "running": bool(thread is not None and thread.is_alive()),
                "worker_id": self.worker_id,
                "processed_jobs": self._processed_jobs,
                "last_error": self._last_error,
                "started_at": self._started_at,
            }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            result = self.process_once()
            if result is not None:
                if result["status"] in {"queued", "dispatch_failed"}:
                    self._wake_event.wait(timeout=self.poll_interval)
                    self._wake_event.clear()
                continue
            self._wake_event.wait(timeout=self.poll_interval)
            self._wake_event.clear()


def _actor(store: Any, actor_id: str) -> User:
    row = store.get_access_user(actor_id)
    if row is None:
        raise ValueError(f"Unknown inspection actor: {actor_id}")
    return User(
        id=str(row["id"]),
        org_id=str(row["org_id"]),
        name=str(row["name"]),
        feishu_id=str(row.get("feishu_id") or ""),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
