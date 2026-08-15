from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from agent.conversation.run_manager import ConversationRunManager, TERMINAL_RUN_STATUSES
from agent.conversation_agent import ConversationUpload
from agent.deliverables import (
    DeliverableValidationError,
    MAX_DELIVERABLE_FILES,
    deliverable_file_size_limit,
    deliverable_total_size_limit,
)
from agent.model_catalog import (
    conversation_model_catalog,
    require_conversation_model,
)
from backend.access_scope import request_scope, require_session


class ConversationRequest(BaseModel):
    view: str = "overview"
    message: str = ""
    model: str = ""
    session_id: str | None = None
    client_request_id: str = ""


class ConversationRenameRequest(BaseModel):
    title: str


class ConversationArchiveRequest(BaseModel):
    archived: bool = True


@dataclass(frozen=True)
class ConversationRouterServices:
    store_factory: Callable[[], Any]
    run_manager: ConversationRunManager


def create_conversation_router(services: ConversationRouterServices) -> APIRouter:
    router = APIRouter(prefix="/api/conversation", tags=["conversation"])

    @router.get("/models")
    def list_conversation_models() -> dict[str, Any]:
        return conversation_model_catalog()

    @router.get("/sessions")
    def list_sessions(request: Request) -> dict[str, Any]:
        scope = request_scope(request)
        return {
            "sessions": services.store_factory().list_sessions(
                project_id=scope.project_id,
                actor_id=scope.actor.id,
            )
        }

    @router.get("/sessions/{session_id}/messages")
    def session_messages(request: Request, session_id: str) -> dict[str, Any]:
        store = services.store_factory()
        session = _required_session(store, request, session_id)
        return {
            "session_id": session["session_id"],
            "messages": store.list_session_messages(session_id),
        }

    @router.post("/sessions/{session_id}/rename")
    def rename_session(
        request: Request,
        session_id: str,
        payload: ConversationRenameRequest,
    ) -> dict[str, Any]:
        store = services.store_factory()
        _required_session(store, request, session_id)
        session = store.rename_session(session_id, payload.title)
        scope = request_scope(request)
        return {
            "session": session,
            "sessions": store.list_sessions(
                project_id=scope.project_id,
                actor_id=scope.actor.id,
            ),
        }

    @router.post("/sessions/{session_id}/archive")
    def archive_session(
        request: Request,
        session_id: str,
        payload: ConversationArchiveRequest | None = None,
    ) -> dict[str, Any]:
        store = services.store_factory()
        _required_session(store, request, session_id)
        session = store.archive_session(
            session_id,
            archived=True if payload is None else payload.archived,
        )
        scope = request_scope(request)
        return {
            "session": session,
            "sessions": store.list_sessions(
                project_id=scope.project_id,
                actor_id=scope.actor.id,
            ),
        }

    @router.delete("/sessions/{session_id}")
    def delete_session(request: Request, session_id: str) -> dict[str, Any]:
        store = services.store_factory()
        _required_session(store, request, session_id)
        store.delete_session(session_id)
        scope = request_scope(request)
        return {
            "deleted": True,
            "session_id": session_id,
            "sessions": store.list_sessions(
                project_id=scope.project_id,
                actor_id=scope.actor.id,
            ),
        }

    @router.post("")
    async def conversation(request: Request) -> dict[str, Any]:
        payload = await read_conversation_payload(request)
        started = _start_run(services, request, payload)
        run = await run_in_threadpool(
            lambda: services.run_manager.wait(started["run_id"], timeout=300)
        )
        if run["status"] != "completed":
            error_id = run.get("error_id") or "unknown"
            raise HTTPException(
                status_code=502,
                detail=f"对话执行失败，错误编号：{error_id}",
            )
        return dict(run["output_payload"])

    @router.post("/stream")
    async def conversation_stream(request: Request) -> StreamingResponse:
        payload = await read_conversation_payload(request)
        started = _start_run(services, request, payload)
        return _event_response(
            services.run_manager,
            started["run_id"],
            after_seq=0,
        )

    @router.get("/runs/{run_id}/events")
    def replay_events(
        request: Request,
        run_id: str,
        after: int = 0,
    ) -> StreamingResponse:
        _authorize_run(services.run_manager, request, run_id)
        return _event_response(
            services.run_manager,
            run_id,
            after_seq=max(0, int(after)),
        )

    return router


def _start_run(
    services: ConversationRouterServices,
    request: Request,
    payload: dict[str, Any],
) -> dict[str, Any]:
    store = services.store_factory()
    scope = request_scope(request)
    try:
        model_id = require_conversation_model(payload.get("model"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload["session_id"]:
        _required_session(store, request, payload["session_id"])
    return services.run_manager.submit(
        actor=scope.actor,
        access_context=scope.access_context,
        project_id=scope.project_id,
        view=payload["view"],
        message=payload["message"],
        session_id=payload["session_id"],
        upload=payload["upload"],
        uploads=payload["uploads"],
        model_id=model_id,
        client_request_id=payload["client_request_id"],
    )


def _required_session(store: Any, request: Request, session_id: str) -> dict[str, Any]:
    try:
        session = store.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    require_session(request_scope(request), session)
    return session


def _authorize_run(manager: ConversationRunManager, request: Request, run_id: str) -> None:
    try:
        run = manager.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown conversation run: {run_id}") from exc
    scope = request_scope(request)
    if run["project_id"] != scope.project_id or run["actor_id"] != scope.actor.id:
        raise HTTPException(status_code=404, detail=f"Unknown conversation run: {run_id}")


def _event_response(
    manager: ConversationRunManager,
    run_id: str,
    *,
    after_seq: int,
) -> StreamingResponse:
    return StreamingResponse(
        _persisted_events(manager, run_id, after_seq=after_seq),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def _persisted_events(
    manager: ConversationRunManager,
    run_id: str,
    *,
    after_seq: int,
    poll_seconds: float = 0.05,
    heartbeat_seconds: float = 10.0,
    terminal_grace_seconds: float = 1.0,
) -> Iterator[str]:
    cursor = max(0, int(after_seq))
    next_heartbeat = time.monotonic() + heartbeat_seconds
    terminal_event_types = {"final", "error", "cancelled"}
    terminal_without_event_since: float | None = None
    while True:
        events = manager.list_events(run_id, after_seq=cursor)
        for event in events:
            cursor = event["seq"]
            yield sse_frame(
                event["event_type"],
                event["payload"],
                event_id=cursor,
            )
        run = manager.get_run(run_id)
        now = time.monotonic()
        if run["status"] in TERMINAL_RUN_STATUSES and not events:
            terminal_events = [
                event
                for event in manager.list_events(run_id, after_seq=0)
                if event["event_type"] in terminal_event_types
            ]
            if terminal_events and cursor >= terminal_events[-1]["seq"]:
                try:
                    manager.wait(run_id, timeout=30)
                except TimeoutError:
                    return
                if manager.list_events(run_id, after_seq=cursor):
                    continue
                return
            if not terminal_events:
                if terminal_without_event_since is None:
                    terminal_without_event_since = now
                elif now - terminal_without_event_since >= max(
                    0.0,
                    terminal_grace_seconds,
                ):
                    yield sse_frame(
                        "error",
                        {
                            "detail": "运行已结束，但终态事件缺失。",
                            "error_id": str(
                                run.get("error_id")
                                or "terminal_event_missing"
                            ),
                            "status": run["status"],
                        },
                        event_id=cursor + 1,
                    )
                    return
        else:
            terminal_without_event_since = None
        if not events and now >= next_heartbeat:
            yield ": heartbeat\n\n"
            next_heartbeat = now + heartbeat_seconds
        time.sleep(max(0.01, poll_seconds))


async def read_conversation_payload(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        form_uploads = [
            upload
            for field in ("files", "file")
            for upload in form.getlist(field)
            if upload is not None and hasattr(upload, "read")
        ]
        if len(form_uploads) > MAX_DELIVERABLE_FILES:
            raise HTTPException(
                status_code=400,
                detail=f"一次最多上传 {MAX_DELIVERABLE_FILES} 个文件。",
            )
        try:
            file_size_limit = deliverable_file_size_limit()
            total_size_limit = deliverable_total_size_limit()
        except DeliverableValidationError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        conversation_uploads: list[ConversationUpload] = []
        total_size = 0
        for upload in form_uploads:
            content = await _read_upload_with_limit(
                upload,
                file_size_limit,
                total_remaining=total_size_limit - total_size,
            )
            total_size += len(content)
            if not content:
                raise HTTPException(status_code=400, detail="上传文件为空。")
            conversation_uploads.append(
                ConversationUpload(
                    filename=Path(getattr(upload, "filename", "") or "conversation_upload.md").name,
                    content=content,
                    content_type=getattr(upload, "content_type", "") or "",
                    input_kind=str(form.get("input_kind") or "auto"),
                )
            )
        return {
            "view": str(form.get("view") or "overview"),
            "message": str(form.get("message") or ""),
            "model": str(form.get("model") or ""),
            "session_id": str(form.get("session_id") or "") or None,
            "client_request_id": str(form.get("client_request_id") or "") or uuid4().hex,
            "upload": conversation_uploads[0] if len(conversation_uploads) == 1 else None,
            "uploads": conversation_uploads if len(conversation_uploads) > 1 else None,
        }
    payload = ConversationRequest.model_validate(await request.json())
    return {
        "view": payload.view,
        "message": payload.message,
        "model": payload.model,
        "session_id": payload.session_id,
        "client_request_id": payload.client_request_id.strip() or uuid4().hex,
        "upload": None,
        "uploads": None,
    }


async def _read_upload_with_limit(
    upload: UploadFile,
    limit: int,
    *,
    total_remaining: int | None = None,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        read_limit = min(1024 * 1024, limit - total + 1)
        if total_remaining is not None:
            read_limit = min(read_limit, total_remaining - total + 1)
        chunk = await upload.read(max(1, read_limit))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Upload file {Path(upload.filename or 'uploaded_file').name} "
                    f"exceeds configured limit of {limit} bytes"
                ),
            )
        if total_remaining is not None and total > total_remaining:
            raise HTTPException(
                status_code=413,
                detail=(
                    "Upload request exceeds configured total limit while reading "
                    f"{Path(upload.filename or 'uploaded_file').name}"
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)


def sse_frame(event: str, data: dict[str, Any], *, event_id: int) -> str:
    return (
        f"id: {int(event_id)}\n"
        f"event: {event}\n"
        f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    )
