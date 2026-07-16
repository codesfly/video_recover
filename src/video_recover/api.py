from __future__ import annotations

import secrets
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from video_recover.config import Settings
from video_recover.domain import Segment, Task
from video_recover.errors import InvalidTransition, TranscriptionFailed, UserFacingError
from video_recover.service import VideoService


class TaskCreate(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    transcribe: bool = True


class TaskResponse(BaseModel):
    id: str
    canonical_url: str
    original_url: str
    status: str
    progress: int
    message: str
    source: str
    transcribe: bool
    aweme_id: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class CookieUpdate(BaseModel):
    cookie: str = Field(min_length=1, max_length=32768)


class CookieStatus(BaseModel):
    configured: bool


class NativeWorkerStatus(BaseModel):
    connected: bool
    worker_id: str | None
    last_seen: datetime | None


class ServiceStatus(BaseModel):
    status: Literal["ok"]
    cookie: CookieStatus
    worker: NativeWorkerStatus
    task_count: int


class WorkerLeaseRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)


class WorkerLeaseResponse(BaseModel):
    lease_id: str
    task_id: str
    media_path: str


class SegmentPayload(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str = Field(min_length=1)


class WorkerCompleteRequest(BaseModel):
    segments: list[SegmentPayload]


class WorkerFailureRequest(BaseModel):
    message: str = Field(default="Mac 原生转写失败", min_length=1, max_length=500)


ARTIFACTS: dict[str, tuple[str, str]] = {
    "video": ("video.mp4", "video/mp4"),
    "description": ("description.txt", "text/plain; charset=utf-8"),
    "transcript": ("transcript.txt", "text/plain; charset=utf-8"),
    "srt": ("transcript.srt", "application/x-subrip; charset=utf-8"),
    "markdown": ("transcript.md", "text/markdown; charset=utf-8"),
    "metadata": ("metadata.json", "application/json"),
}


def task_response(task: Task) -> TaskResponse:
    return TaskResponse(
        id=task.id,
        canonical_url=task.canonical_url,
        original_url=task.original_url,
        status=task.status.value,
        progress=task.progress,
        message=task.message,
        source=task.source,
        transcribe=task.transcribe,
        aweme_id=task.aweme_id,
        error_code=task.error_code,
        error_message=task.error_message,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def build_router(service: VideoService, settings: Settings) -> APIRouter:
    router = APIRouter()

    def get_task_or_404(task_id: str) -> Task:
        try:
            return service.repository.get_task(task_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="任务不存在") from None

    def require_worker_token(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = f"Bearer {settings.worker_token}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Worker 凭据无效",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @router.post("/api/tasks", response_model=TaskResponse, status_code=202)
    def submit_task(body: TaskCreate) -> TaskResponse:
        try:
            task, _created = service.submit(body.url, transcribe=body.transcribe)
        except UserFacingError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": exc.code, "message": exc.message},
            ) from None
        return task_response(task)

    @router.get("/api/tasks", response_model=list[TaskResponse])
    def list_tasks(limit: int = 100) -> list[TaskResponse]:
        safe_limit = min(max(limit, 1), 500)
        return [task_response(task) for task in service.repository.list_tasks(limit=safe_limit)]

    @router.get("/api/tasks/{task_id}", response_model=TaskResponse)
    def get_task(task_id: str) -> TaskResponse:
        return task_response(get_task_or_404(task_id))

    @router.post("/api/tasks/{task_id}/retry", response_model=TaskResponse)
    def retry_task(task_id: str) -> TaskResponse:
        get_task_or_404(task_id)
        try:
            return task_response(service.retry(task_id))
        except InvalidTransition:
            raise HTTPException(status_code=409, detail="当前任务状态不能重试") from None

    @router.delete("/api/tasks/{task_id}", status_code=204)
    def delete_task(task_id: str) -> Response:
        get_task_or_404(task_id)
        try:
            service.delete(task_id)
        except InvalidTransition:
            raise HTTPException(status_code=409, detail="任务处理中，暂时不能删除") from None
        return Response(status_code=204)

    @router.get("/api/tasks/{task_id}/artifacts/{artifact}")
    def get_artifact(task_id: str, artifact: str) -> FileResponse:
        task = get_task_or_404(task_id)
        selected = ARTIFACTS.get(artifact)
        if selected is None or task.output_dir is None:
            raise HTTPException(status_code=404, detail="产物不存在")
        filename, media_type = selected
        path = (task.output_dir / filename).resolve()
        output_dir = task.output_dir.resolve()
        if not path.is_relative_to(output_dir) or not path.is_file():
            raise HTTPException(status_code=404, detail="产物不存在")
        return FileResponse(path, media_type=media_type, filename=filename)

    @router.get("/api/status", response_model=ServiceStatus)
    def get_status() -> ServiceStatus:
        worker_seen = service.repository.get_native_worker_seen()
        worker_id = worker_seen[0] if worker_seen else None
        last_seen = worker_seen[1] if worker_seen else None
        connected = bool(
            last_seen
            and (service.repository.clock() - last_seen).total_seconds()
            <= settings.native_worker_timeout_seconds
        )
        return ServiceStatus(
            status="ok",
            cookie=CookieStatus(configured=service.cookie_configured()),
            worker=NativeWorkerStatus(
                connected=connected,
                worker_id=worker_id,
                last_seen=last_seen,
            ),
            task_count=len(service.repository.list_tasks(limit=500)),
        )

    @router.put("/api/settings/cookie", status_code=204)
    def update_cookie(body: CookieUpdate) -> Response:
        try:
            service.save_cookie(body.cookie)
        except ValueError:
            raise HTTPException(status_code=422, detail="Cookie 不能为空") from None
        return Response(status_code=204)

    @router.post(
        "/internal/worker/lease",
        response_model=WorkerLeaseResponse,
        responses={204: {"description": "没有等待中的转写任务"}},
    )
    def lease_task(
        body: WorkerLeaseRequest,
        _auth: None = Depends(require_worker_token),
    ) -> WorkerLeaseResponse | Response:
        service.repository.record_native_worker_seen(body.worker_id)
        lease = service.repository.acquire_transcription_lease(
            body.worker_id,
            ttl_seconds=settings.native_worker_timeout_seconds,
        )
        if lease is None:
            return Response(status_code=204)
        task = service.repository.get_task(lease.task_id)
        if task.output_dir is None:
            raise HTTPException(status_code=409, detail="任务缺少视频文件")
        media = (task.output_dir / "video.mp4").resolve()
        root = settings.data_dir.resolve()
        if not media.is_relative_to(root):
            raise HTTPException(status_code=409, detail="任务文件路径无效")
        return WorkerLeaseResponse(
            lease_id=lease.id,
            task_id=lease.task_id,
            media_path=str(media.relative_to(root)),
        )

    @router.post("/internal/worker/{lease_id}/heartbeat", status_code=204)
    def heartbeat(
        lease_id: str,
        _auth: None = Depends(require_worker_token),
    ) -> Response:
        if not service.repository.heartbeat_lease(
            lease_id,
            ttl_seconds=settings.native_worker_timeout_seconds,
        ):
            raise HTTPException(status_code=404, detail="租约不存在")
        return Response(status_code=204)

    @router.post(
        "/internal/worker/{lease_id}/complete",
        response_model=TaskResponse,
    )
    def complete(
        lease_id: str,
        body: WorkerCompleteRequest,
        _auth: None = Depends(require_worker_token),
    ) -> TaskResponse:
        try:
            lease = service.repository.get_transcription_lease(lease_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="租约不存在") from None
        segments = [Segment(item.start, item.end, item.text) for item in body.segments]
        return task_response(service.complete_transcription(lease.id, lease.task_id, segments))

    @router.post("/internal/worker/{lease_id}/fail", response_model=TaskResponse)
    def fail(
        lease_id: str,
        body: WorkerFailureRequest,
        _auth: None = Depends(require_worker_token),
    ) -> TaskResponse:
        try:
            lease = service.repository.get_transcription_lease(lease_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="租约不存在") from None
        task = service.record_failure(
            lease.task_id,
            TranscriptionFailed(body.message),
            lease_id=lease.id,
        )
        return task_response(task)

    return router
