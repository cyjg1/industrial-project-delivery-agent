from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent.project_files import ProjectFileError, ProjectFileService
from backend.access_scope import request_scope, require_role


class FolderCreateRequest(BaseModel):
    parent: str = ""
    name: str


@dataclass(frozen=True)
class ProjectFileRouterServices:
    store_factory: Callable[[], Any]
    ingestion_wake: Callable[[], Any]


def create_project_files_router(services: ProjectFileRouterServices) -> APIRouter:
    router = APIRouter(prefix="/api/project-files", tags=["project-files"])

    @router.get("")
    def list_project_files(request: Request) -> dict[str, Any]:
        scope = request_scope(request)
        require_role(scope, "exec")
        return ProjectFileService(services.store_factory()).workspace(project_id=scope.project_id)

    @router.post("/folders", status_code=201)
    def create_folder(request: Request, payload: FolderCreateRequest) -> dict[str, Any]:
        scope = request_scope(request)
        require_role(scope, "exec")
        service = ProjectFileService(services.store_factory())
        try:
            folder = service.create_folder(
                project_id=scope.project_id,
                parent=payload.parent,
                name=payload.name,
            )
        except ProjectFileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"folder": folder, "workspace": service.workspace(project_id=scope.project_id)}

    @router.post("/upload", status_code=202)
    async def upload_project_files(
        request: Request,
        files: list[UploadFile] = File(...),
        folder: str = Form(""),
        upload_type: str = Form("project_document"),
        title: str = Form(""),
        meeting_date: str = Form(""),
        topic: str = Form(""),
        sensitivity: str = Form("l1"),
    ) -> dict[str, Any]:
        scope = request_scope(request)
        require_role(scope, "exec")
        if not files:
            raise HTTPException(status_code=400, detail="至少需要上传一个文件。")
        if len(files) > 20:
            raise HTTPException(status_code=400, detail="一次最多上传 20 个文件。")
        service = ProjectFileService(services.store_factory())
        uploaded: list[dict[str, Any]] = []
        jobs: list[dict[str, Any]] = []
        try:
            for upload in files:
                content = await upload.read()
                row, job = service.upload(
                    project_id=scope.project_id,
                    actor=scope.actor,
                    folder=folder,
                    upload_type=upload_type,
                    filename=upload.filename or "project_file",
                    content=content,
                    title=title if len(files) == 1 else "",
                    meeting_date=meeting_date,
                    topic=topic,
                    sensitivity=sensitivity,
                )
                uploaded.append(row)
                if job:
                    jobs.append(job)
        except ProjectFileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if jobs:
            services.ingestion_wake()
        return {
            "uploaded": uploaded,
            "jobs": jobs,
            "workspace": service.workspace(project_id=scope.project_id),
        }

    @router.get("/download")
    def download_project_file(request: Request, path: str) -> FileResponse:
        scope = request_scope(request)
        require_role(scope, "exec")
        try:
            target = ProjectFileService(services.store_factory()).resolve_download(
                project_id=scope.project_id,
                relative_path=path,
            )
        except (ProjectFileError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="文件不存在或路径无效。") from exc
        return FileResponse(target, filename=target.name, media_type="application/octet-stream")

    return router
