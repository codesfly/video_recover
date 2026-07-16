from __future__ import annotations

import json
import math
import os
import shutil
from pathlib import Path
from typing import Protocol

import httpx

from video_recover.config import Settings
from video_recover.crypto import CookieVault
from video_recover.domain import Segment, Task, TaskStatus
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
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.parser = parser
        self.cookie_vault = cookie_vault
        self.url_client = url_client or httpx.Client(timeout=20)

    def submit(
        self,
        url: str,
        *,
        source: str = "web",
        transcribe: bool = True,
    ) -> tuple[Task, bool]:
        try:
            normalized = normalize_douyin_url(url)
        except UserFacingError:
            normalized = resolve_douyin_url(url, client=self.url_client)
        return self.repository.create_or_get_task(
            normalized.canonical_url,
            original_url=url,
            source=source,
            transcribe=transcribe,
        )

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
        if task.output_dir is not None:
            root = self.settings.download_dir.resolve()
            output_dir = task.output_dir.resolve()
            if not output_dir.is_relative_to(root):
                raise RuntimeError("task output directory escapes download root")
            shutil.rmtree(output_dir, ignore_errors=True)
        if not self.repository.delete_task(task_id):
            raise KeyError(task_id)
