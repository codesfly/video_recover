from __future__ import annotations

import json
import math
import os
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from video_recover.comments import Comment, CommentFetcher, render_markdown
from video_recover.config import Settings
from video_recover.crypto import CookieVault
from video_recover.domain import CommentJob, CommentJobStatus, Segment, Task, TaskStatus
from video_recover.errors import (
    CaptureConflict,
    DownloadFailed,
    DownloadTooLarge,
    InsufficientStorage,
    InvalidTransition,
    UnsafeCapture,
    UserFacingError,
)
from video_recover.parsers import Parser, ResolvedMedia
from video_recover.repository import Repository
from video_recover.transcript import write_artifacts
from video_recover.url_policy import normalize_douyin_url, resolve_douyin_url

SNAPSHOT_NAME = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")


@dataclass(frozen=True, slots=True)
class CommentView:
    job: CommentJob
    fetched_at: str
    total_in_snapshot: int
    comments: list[Comment]


class DownloadFunction(Protocol):
    def __call__(self, media: ResolvedMedia, target: Path, **options: object) -> Path: ...


def _atomic_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def _atomic_copy(
    source: Path,
    target: Path,
    *,
    max_bytes: int,
    minimum_free_bytes: int,
) -> None:
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        with source.open("rb") as input_file, temporary.open("wb") as output_file:
            written = 0
            while chunk := input_file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise DownloadTooLarge()
                if shutil.disk_usage(target.parent).free < (
                    len(chunk) + minimum_free_bytes
                ):
                    raise InsufficientStorage()
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


class VideoService:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: Repository,
        parser: Parser,
        cookie_vault: CookieVault,
        url_client: httpx.Client | None = None,
        comment_fetcher: CommentFetcher | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.parser = parser
        self.cookie_vault = cookie_vault
        self.url_client = url_client or httpx.Client(timeout=20)
        self.comment_fetcher = comment_fetcher

    def submit(
        self,
        url: str,
        *,
        source: str = "web",
        transcribe: bool = True,
        fetch_comments: bool = False,
    ) -> tuple[Task, bool]:
        try:
            normalized = normalize_douyin_url(url)
        except UserFacingError:
            normalized = resolve_douyin_url(url, client=self.url_client)
        task, created = self.repository.create_or_get_task(
            normalized.canonical_url,
            original_url=url,
            source=source,
            transcribe=transcribe,
        )
        if fetch_comments and task.status != TaskStatus.CANCELLED:
            self.request_comments(task.id)
        return task, created

    def save_cookie(self, cookie: str) -> None:
        encrypted = self.cookie_vault.encrypt(cookie).decode("ascii")
        self.repository.set_setting("douyin_cookie", encrypted)

    def get_cookie(self) -> str | None:
        encrypted = self.repository.get_setting("douyin_cookie")
        return None if encrypted is None else self.cookie_vault.decrypt(encrypted)

    def cookie_configured(self) -> bool:
        return self.repository.get_setting("douyin_cookie") is not None

    def import_local_capture(
        self,
        url: str,
        capture_path: Path,
        *,
        description: str,
        author: str,
        duration_seconds: float | None = None,
        transcribe: bool = True,
    ) -> Task:
        normalized = normalize_douyin_url(url)
        capture_root = self.settings.browser_capture_dir.resolve()
        try:
            source = capture_path.resolve()
            if not source.is_relative_to(capture_root) or not source.is_file():
                raise UnsafeCapture()
            size = source.stat().st_size
        except OSError as exc:
            raise UnsafeCapture() from exc
        if size < 1:
            raise UnsafeCapture()
        if size > self.settings.max_download_bytes:
            raise DownloadTooLarge()
        self.settings.download_dir.mkdir(parents=True, exist_ok=True)
        try:
            free_bytes = shutil.disk_usage(self.settings.download_dir).free
        except OSError as exc:
            raise InsufficientStorage() from exc
        if free_bytes < size + self.settings.minimum_free_bytes:
            raise InsufficientStorage()
        if duration_seconds is not None and (
            not math.isfinite(duration_seconds) or duration_seconds < 0
        ):
            raise UnsafeCapture()

        try:
            task = self.repository.prepare_capture_task(
                normalized.canonical_url,
                original_url=url,
                source="chrome",
                transcribe=transcribe,
            )
        except InvalidTransition as exc:
            raise CaptureConflict() from exc
        try:
            output_dir = self.settings.download_dir / normalized.aweme_id
            output_dir.mkdir(parents=True, exist_ok=True)
            metadata = {
                "aweme_id": normalized.aweme_id,
                "canonical_url": normalized.canonical_url,
                "description": description,
                "author": author,
                "duration_seconds": duration_seconds,
                "cover_url": None,
                "capture_source": "chrome",
            }
            _atomic_text(
                output_dir / "metadata.json",
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            )
            _atomic_text(output_dir / "description.txt", description.strip() + "\n")
            self.repository.set_media(
                task.id,
                aweme_id=normalized.aweme_id,
                output_dir=output_dir,
                metadata=metadata,
            )
            self.repository.transition(
                task.id,
                TaskStatus.DOWNLOADING,
                progress=20,
                message="正在导入本机视频",
            )
            _atomic_copy(
                source,
                output_dir / "video.mp4",
                max_bytes=self.settings.max_download_bytes,
                minimum_free_bytes=self.settings.minimum_free_bytes,
            )
            target = TaskStatus.AWAITING_TRANSCRIPTION if transcribe else TaskStatus.COMPLETED
            return self.repository.transition(
                task.id,
                target,
                progress=60 if transcribe else 100,
                message="等待语音转写" if transcribe else "导入完成",
            )
        except UserFacingError as exc:
            self.record_failure(task.id, exc)
            raise
        except OSError as exc:
            error = DownloadFailed("本机视频导入失败，请检查磁盘后重试")
            self.record_failure(task.id, error)
            raise error from exc

    def process_download(self, task_id: str, downloader: DownloadFunction) -> Task:
        task = self.repository.get_task(task_id)
        if task.status == TaskStatus.QUEUED:
            task = self.repository.transition(
                task.id,
                TaskStatus.RESOLVING,
                progress=5,
                message="正在解析视频",
            )
        elif task.status != TaskStatus.RESOLVING:
            raise InvalidTransition(
                f"cannot process a task in {task.status.value} state"
            )
        media = self.parser.resolve(task.canonical_url, cookie=self.get_cookie())
        output_dir = self.settings.download_dir / media.aweme_id
        output_dir.mkdir(parents=True, exist_ok=True)
        metadata = {
            "aweme_id": media.aweme_id,
            "canonical_url": media.canonical_url,
            "description": media.description,
            "author": media.author,
            "duration_seconds": media.duration_seconds,
            "cover_url": media.cover_url,
        }
        _atomic_text(
            output_dir / "metadata.json",
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        )
        _atomic_text(output_dir / "description.txt", media.description.strip() + "\n")
        self.repository.set_media(
            task.id,
            aweme_id=media.aweme_id,
            output_dir=output_dir,
            metadata=metadata,
        )
        self.repository.transition(
            task.id,
            TaskStatus.DOWNLOADING,
            progress=20,
            message="正在下载视频",
        )
        downloader(
            media,
            output_dir / "video.mp4",
            max_bytes=self.settings.max_download_bytes,
            minimum_free_bytes=self.settings.minimum_free_bytes,
        )
        target = TaskStatus.AWAITING_TRANSCRIPTION if task.transcribe else TaskStatus.COMPLETED
        message = "等待语音转写" if task.transcribe else "下载完成"
        progress = 60 if task.transcribe else 100
        return self.repository.transition(task.id, target, progress=progress, message=message)

    def complete_transcription(
        self,
        lease_id: str,
        task_id: str,
        segments: list[Segment],
    ) -> Task:
        task = self.repository.get_task(task_id)
        if task.output_dir is None:
            raise RuntimeError("transcription task has no output directory")
        description_path = task.output_dir / "description.txt"
        description = description_path.read_text(encoding="utf-8").strip()
        write_artifacts(task.output_dir, description, segments)
        return self.repository.complete_transcription(lease_id)

    def record_failure(
        self,
        task_id: str,
        error: UserFacingError,
        *,
        lease_id: str | None = None,
    ) -> Task:
        return self.repository.record_failure(task_id, error, lease_id=lease_id)

    def retry(self, task_id: str) -> Task:
        task = self.repository.get_task(task_id)
        target = (
            TaskStatus.AWAITING_TRANSCRIPTION
            if task.status == TaskStatus.PARTIAL
            else TaskStatus.QUEUED
        )
        progress = 60 if target == TaskStatus.AWAITING_TRANSCRIPTION else 0
        return self.repository.transition(task.id, target, progress=progress, message="已重新排队")

    def delete(self, task_id: str) -> None:
        task = self.repository.get_task(task_id)
        if task.status in {
            TaskStatus.RESOLVING,
            TaskStatus.DOWNLOADING,
            TaskStatus.AWAITING_TRANSCRIPTION,
            TaskStatus.TRANSCRIBING,
        }:
            raise InvalidTransition("cannot delete a task while it is processing")
        if self.repository.has_running_comment_job(task.id):
            raise InvalidTransition("cannot delete a task while comments are being fetched")
        root = self.settings.download_dir.resolve()
        # The aweme folder also holds comment snapshots taken before the video was downloaded.
        aweme_dir = self.settings.download_dir / normalize_douyin_url(task.canonical_url).aweme_id
        for directory in {task.output_dir, aweme_dir} - {None}:
            resolved = directory.resolve()
            if not resolved.is_relative_to(root) or resolved == root:
                raise RuntimeError("task output directory escapes download root")
            shutil.rmtree(resolved, ignore_errors=True)
        if not self.repository.delete_task(task_id):
            raise KeyError(task_id)

    def comments_dir(self, task: Task) -> Path:
        aweme_id = normalize_douyin_url(task.canonical_url).aweme_id
        return self.settings.download_dir / aweme_id / "comments"

    def request_comments(
        self, task_id: str, *, max_comments: int | None = None
    ) -> tuple[CommentJob, bool]:
        task = self.repository.get_task(task_id)
        if task.status == TaskStatus.CANCELLED:
            raise InvalidTransition("cannot fetch comments for a cancelled task")
        limit = max_comments if max_comments is not None else self.settings.comments_default_max
        if not 1 <= limit <= 1000:
            raise ValueError("max_comments must be between 1 and 1000")
        return self.repository.create_or_get_comment_job(task.id, max_comments=limit)

    def process_comment_job(self, job: CommentJob) -> CommentJob:
        if self.comment_fetcher is None:
            raise RuntimeError("comment fetcher is not configured")
        task = self.repository.get_task(job.task_id)
        try:
            harvest = self.comment_fetcher.fetch(
                task.canonical_url,
                max_comments=job.max_comments,
                deadline_seconds=self.settings.comments_job_timeout_seconds,
            )
        except UserFacingError as exc:
            return self._finish_comment_failure(job, exc)
        error = harvest.stop_error
        if error is not None and not harvest.comments:
            return self._finish_comment_failure(job, error, pages=harvest.pages)
        status = CommentJobStatus.COMPLETED if error is None else CommentJobStatus.PARTIAL
        fetched_at = self.repository.clock().astimezone(UTC)
        snapshot_name = f"{fetched_at:%Y%m%dT%H%M%SZ}-{job.id[:8]}"
        folder = self.comments_dir(task)
        folder.mkdir(parents=True, exist_ok=True)
        aweme_id = folder.parent.name
        payload = {
            "aweme_id": aweme_id,
            "fetched_at": fetched_at.isoformat(),
            "status": status.value,
            "pages": harvest.pages,
            "has_more_remaining": harvest.has_more_remaining,
            "total_reported": harvest.total_reported,
            "comments": [
                {**asdict(c), "created_at": c.created_at.isoformat() if c.created_at else None}
                for c in harvest.comments
            ],
        }
        _atomic_text(
            folder / f"{snapshot_name}.json",
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        _atomic_text(
            folder / "latest.md",
            render_markdown(aweme_id=aweme_id, fetched_at=fetched_at, harvest=harvest),
        )
        return self.repository.finish_comment_job(
            job.id,
            status=status,
            fetched_count=len(harvest.comments),
            pages=harvest.pages,
            has_more_remaining=harvest.has_more_remaining,
            total_reported=harvest.total_reported,
            snapshot_name=snapshot_name,
            error_code=None if error is None else error.code,
            error_message=None if error is None else error.message,
        )

    def _finish_comment_failure(
        self, job: CommentJob, error: UserFacingError, *, pages: int = 0
    ) -> CommentJob:
        return self.repository.finish_comment_job(
            job.id,
            status=CommentJobStatus.FAILED,
            fetched_count=0,
            pages=pages,
            has_more_remaining=False,
            total_reported=None,
            snapshot_name=None,
            error_code=error.code,
            error_message=error.message,
        )

    def read_comments(
        self,
        task_id: str,
        *,
        job_id: str | None = None,
        sort: str = "likes",
        limit: int = 50,
    ) -> CommentView | None:
        task = self.repository.get_task(task_id)
        if job_id is None:
            job = self.repository.latest_comment_snapshot_job(task.id)
        else:
            job = self.repository.get_comment_job(job_id)
            if job.task_id != task.id:
                raise KeyError(job_id)
        if job is None or job.snapshot_name is None:
            return None
        if not SNAPSHOT_NAME.fullmatch(job.snapshot_name):
            raise RuntimeError("invalid comment snapshot name")
        snapshot_path = self.comments_dir(task) / f"{job.snapshot_name}.json"
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        comments = [
            Comment(
                **{
                    **raw,
                    "created_at": datetime.fromisoformat(raw["created_at"])
                    if raw["created_at"]
                    else None,
                }
            )
            for raw in payload["comments"]
        ]
        if sort == "time":
            comments.sort(
                key=lambda c: c.created_at or datetime.min.replace(tzinfo=UTC), reverse=True
            )
        else:
            comments.sort(key=lambda c: -c.digg_count)
        return CommentView(
            job=job,
            fetched_at=payload["fetched_at"],
            total_in_snapshot=len(comments),
            comments=comments[: max(1, min(limit, 1000))],
        )
