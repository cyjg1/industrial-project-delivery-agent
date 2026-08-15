from __future__ import annotations

import hashlib
import logging
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from agent.access_policy import User
from agent.conversation_agent import ConversationUpload, iter_conversation_agent_events
from agent.model_catalog import require_conversation_model
from store.runtime_repository import SQLiteRuntimeCheckpointSink


LOGGER = logging.getLogger(__name__)
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})


class ConversationRunManager:
    """Runs conversations independently from HTTP and persists replayable events."""

    def __init__(
        self,
        *,
        store_factory: Callable[[], Any],
        runtime_factory: Callable[[Any], Any],
        milestone_loader: Callable[[Any, str], Any],
        ingestion_wake: Callable[[], None] | None = None,
        inspection_wake: Callable[[], None] | None = None,
        final_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        evolution_service_factory: Callable[[Any], Any] | None = None,
        event_source: Callable[..., Iterator[dict[str, Any]]] = iter_conversation_agent_events,
        max_workers: int = 4,
    ) -> None:
        self.store_factory = store_factory
        self.runtime_factory = runtime_factory
        self.milestone_loader = milestone_loader
        self.ingestion_wake = ingestion_wake
        self.inspection_wake = inspection_wake
        self.final_transform = final_transform or (lambda payload: payload)
        self.evolution_service_factory = evolution_service_factory
        self.event_source = event_source
        self.max_workers = max(1, int(max_workers))
        self.executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="conversation-run",
        )
        self._lock = threading.RLock()
        self._futures: dict[str, Future[Any]] = {}

    def submit(
        self,
        *,
        actor: User,
        access_context: Any,
        project_id: str,
        view: str,
        message: str,
        session_id: str | None = None,
        upload: ConversationUpload | None = None,
        uploads: list[ConversationUpload] | None = None,
        model_id: str = "",
        client_request_id: str = "",
    ) -> dict[str, Any]:
        store = self.store_factory()
        selected_model = require_conversation_model(model_id)
        active_uploads = _normalize_uploads(upload, uploads)
        title = message or (active_uploads[0].filename if active_uploads else "") or "未命名会话"
        session = store.ensure_session(
            session_id=session_id,
            title=title,
            project_id=project_id,
            actor_id=actor.id,
        )
        proposed_run_id = f"conversation_{uuid4().hex}"
        turn_id = f"turn_{uuid4().hex}"
        persisted_uploads = self._persist_uploads(store.root_dir, proposed_run_id, active_uploads)
        input_payload = {
            "view": view,
            "message": message,
            "uploads": persisted_uploads,
            "model": selected_model,
        }
        run = store.runtime_runs.create_run(
            run_id=proposed_run_id,
            kind="conversation",
            project_id=project_id,
            actor_id=actor.id,
            session_id=session["session_id"],
            turn_id=turn_id,
            client_request_id=client_request_id,
            input_payload=input_payload,
            recoverable=True,
        )
        created = run["run_id"] == proposed_run_id
        if not created:
            shutil.rmtree(store.root_dir / "runtime_inputs" / proposed_run_id, ignore_errors=True)
        else:
            store.append_session_message(
                session["session_id"],
                "user",
                _user_content(message, active_uploads),
                metadata={"view": view, "model": selected_model},
                turn_id=turn_id,
                run_id=proposed_run_id,
                state="running",
            )
        if run["status"] not in TERMINAL_RUN_STATUSES:
            self._launch(run["run_id"], actor=actor, access_context=access_context)
        return self.public_run(run["run_id"])

    def public_run(self, run_id: str) -> dict[str, Any]:
        run = self.store_factory().runtime_runs.get_run(run_id)
        return {
            "run_id": run["run_id"],
            "session_id": run["session_id"],
            "turn_id": run["turn_id"],
            "status": run["status"],
            "stop_reason": run["stop_reason"],
            "error_id": run["error_id"],
        }

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self.store_factory().runtime_runs.get_run(run_id)

    def status(self) -> dict[str, Any]:
        rows = self.store_factory().runtime_runs.list_runs(
            kind="conversation",
            statuses=("queued", "running"),
            limit=1000,
        )
        return {
            "running": sum(1 for row in rows if row["status"] == "running"),
            "queued": sum(1 for row in rows if row["status"] == "queued"),
            "max_workers": self.max_workers,
        }

    def list_events(self, run_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
        return self.store_factory().runtime_runs.list_events(run_id, after_seq=after_seq)

    def wait(self, run_id: str, *, timeout: float = 300.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            run = self.get_run(run_id)
            if run["status"] in TERMINAL_RUN_STATUSES:
                self._wait_for_local_execution(run_id, deadline=deadline, timeout=timeout)
                return self.get_run(run_id)
            time.sleep(0.025)
        raise TimeoutError(f"Conversation run did not finish within {timeout:g} seconds")

    def _wait_for_local_execution(
        self,
        run_id: str,
        *,
        deadline: float,
        timeout: float,
    ) -> None:
        with self._lock:
            future = self._futures.get(run_id)
        if future is None:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Conversation run did not finish within {timeout:g} seconds")
        try:
            future.result(timeout=remaining)
        except FutureTimeoutError as exc:
            if future.done():
                raise
            raise TimeoutError(
                f"Conversation run did not finish within {timeout:g} seconds"
            ) from exc

    def recover(self) -> list[str]:
        store = self.store_factory()
        store.runtime_runs.recover_interrupted()
        queued = store.runtime_runs.list_runs(kind="conversation", statuses=("queued",))
        recovered: list[str] = []
        for run in queued:
            actor, access_context = self._resolve_actor(store, run["actor_id"])
            self._launch(run["run_id"], actor=actor, access_context=access_context)
            recovered.append(run["run_id"])
        return recovered

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True)

    def _launch(self, run_id: str, *, actor: User, access_context: Any) -> None:
        with self._lock:
            current = self._futures.get(run_id)
            if current is not None and not current.done():
                return
            future = self.executor.submit(
                self._execute,
                run_id,
                actor,
                access_context,
            )
            self._futures[run_id] = future
            future.add_done_callback(lambda _future, active_id=run_id: self._forget(active_id))

    def _forget(self, run_id: str) -> None:
        with self._lock:
            self._futures.pop(run_id, None)

    def _execute(self, run_id: str, actor: User, access_context: Any) -> None:
        store = self.store_factory()
        repository = store.runtime_runs
        run = repository.get_run(run_id)
        if run["status"] in TERMINAL_RUN_STATUSES:
            return
        prior_events = repository.list_events(run_id)
        repository.mark_running(run_id)
        if any(event["event_type"] == "delta" for event in prior_events):
            repository.append_event(
                run_id,
                "model_output_reset",
                {"reason": "process_recovery"},
            )
        repository.append_event(
            run_id,
            "run_started",
            {
                "run_id": run_id,
                "session_id": run["session_id"],
                "turn_id": run["turn_id"],
            },
        )
        final_payload: dict[str, Any] | None = None
        try:
            payload = run["input_payload"]
            selected_model = require_conversation_model(
                str(payload.get("model") or ""),
            )
            uploads = self._load_uploads(store.root_dir, payload.get("uploads") or [])
            milestone = self.milestone_loader(store, run["project_id"])
            checkpoint_sink = SQLiteRuntimeCheckpointSink(repository)
            for event in self.event_source(
                store=store,
                runtime=self.runtime_factory(store),
                model_id=selected_model,
                view=str(payload.get("view") or "overview"),
                message=str(payload.get("message") or ""),
                session_id=run["session_id"],
                uploads=uploads or None,
                milestone=milestone,
                actor=actor,
                access_context=access_context,
                ingestion_wake=self.ingestion_wake,
                inspection_wake=self.inspection_wake,
                runtime_run_id=run_id,
                turn_id=run["turn_id"],
                persist_user_message=False,
                event_sink=checkpoint_sink,
                runtime_checkpoint=run.get("checkpoint") or None,
            ):
                event_type = str(event.get("event") or "")
                event_payload = dict(event.get("data") or {})
                if event_type == "reset":
                    event_type = "model_output_reset"
                if event_type == "final":
                    final_payload = self.final_transform(event_payload)
                    continue
                repository.append_event(
                    run_id,
                    event_type,
                    event_payload,
                    round_index=int(event_payload.get("round_index") or 0),
                )
            if final_payload is None:
                raise RuntimeError("Conversation completed without a final payload")
            store.update_session_turn_state(run["turn_id"], "committed")
            repository.complete_run_with_event(
                run_id,
                stop_reason="completed",
                output_payload=final_payload,
                event_type="final",
                event_payload=final_payload,
            )
            self._close_memory_evolution(store, run_id)
        except Exception as exc:
            error_id = f"err_{uuid4().hex[:12]}"
            store.update_session_turn_state(run["turn_id"], "failed")
            public_error = {
                "detail": "对话执行失败，请使用错误编号联系管理员。",
                "error_id": error_id,
            }
            repository.fail_run_with_event(
                run_id,
                error_id=error_id,
                error_message=f"{type(exc).__name__}: {exc}",
                event_payload=public_error,
            )
            self._close_memory_evolution(store, run_id)
            LOGGER.exception("Conversation run %s failed (%s)", run_id, error_id)

    def _close_memory_evolution(self, store: Any, run_id: str) -> None:
        if self.evolution_service_factory is None:
            return
        try:
            result = self.evolution_service_factory(store).close_runtime_run(run_id)
            episode = result.get("episode") or {}
            store.runtime_runs.append_event(
                run_id,
                "memory_evolution",
                {
                    "episode_id": episode.get("episode_id") or "",
                    "relation": episode.get("relation") or "",
                    "reward_source": episode.get("reward_source") or "",
                    "trace_count": len(result.get("traces") or []),
                    "policy_candidate_count": (
                        result.get("evolution") or {}
                    ).get("policy_candidate_count", 0),
                    "cognition_candidate_count": (
                        result.get("evolution") or {}
                    ).get("cognition_candidate_count", 0),
                },
            )
        except Exception as exc:
            store.runtime_runs.append_event(
                run_id,
                "memory_evolution_error",
                {"error_type": type(exc).__name__},
            )
            LOGGER.exception("Memory-skill evolution failed for run %s", run_id)

    def _persist_uploads(
        self,
        root_dir: Path,
        run_id: str,
        uploads: list[ConversationUpload],
    ) -> list[dict[str, Any]]:
        if not uploads:
            return []
        target_dir = root_dir / "runtime_inputs" / run_id
        target_dir.mkdir(parents=True, exist_ok=True)
        rows: list[dict[str, Any]] = []
        for index, upload in enumerate(uploads, start=1):
            filename = Path(upload.filename or f"upload_{index}").name
            target = target_dir / f"{index:03d}_{filename}"
            target.write_bytes(upload.content)
            rows.append(
                {
                    "filename": filename,
                    "path": str(target.relative_to(root_dir)),
                    "content_type": upload.content_type,
                    "input_kind": upload.input_kind,
                    "sha256": hashlib.sha256(upload.content).hexdigest(),
                }
            )
        return rows

    def _load_uploads(
        self,
        root_dir: Path,
        descriptors: list[dict[str, Any]],
    ) -> list[ConversationUpload]:
        base = (root_dir / "runtime_inputs").resolve()
        uploads: list[ConversationUpload] = []
        for descriptor in descriptors:
            target = (root_dir / str(descriptor.get("path") or "")).resolve()
            if base not in target.parents:
                raise ValueError("Conversation upload path escaped the managed input directory")
            content = target.read_bytes()
            expected = str(descriptor.get("sha256") or "")
            if expected and hashlib.sha256(content).hexdigest() != expected:
                raise ValueError("Conversation upload content hash does not match persisted input")
            uploads.append(
                ConversationUpload(
                    filename=Path(str(descriptor.get("filename") or target.name)).name,
                    content=content,
                    content_type=str(descriptor.get("content_type") or ""),
                    input_kind=str(descriptor.get("input_kind") or "auto"),
                )
            )
        return uploads

    @staticmethod
    def _resolve_actor(store: Any, actor_id: str) -> tuple[User, Any]:
        row = store.get_access_user(actor_id)
        if row is None:
            raise ValueError(f"Unknown actor for recovered conversation run: {actor_id}")
        actor = User(
            id=str(row["id"]),
            org_id=str(row["org_id"]),
            name=str(row["name"]),
            feishu_id=str(row.get("feishu_id") or ""),
        )
        return actor, store.access_context_for_actor(actor.id)


def _normalize_uploads(
    upload: ConversationUpload | None,
    uploads: list[ConversationUpload] | None,
) -> list[ConversationUpload]:
    result: list[ConversationUpload] = []
    seen: set[tuple[str, str]] = set()
    for item in [*(uploads or []), *([upload] if upload is not None else [])]:
        key = (item.filename, hashlib.sha256(item.content).hexdigest())
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _user_content(message: str, uploads: list[ConversationUpload]) -> str:
    parts = [message.strip() or "请处理上传文件"]
    for upload in uploads:
        parts.extend((f"附件：{upload.filename}", f"附件用途：{upload.input_kind}"))
    return "\n".join(parts)
