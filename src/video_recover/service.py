from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Protocol

import httpx

from video_recover.config import Settings
from video_recover.crypto import CookieVault
from video_recover.domain import Segment, Task, TaskStatus
from video_recover.errors import InvalidTransition, UserFacingError
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

    def process_download(self, task_id: str, downloader: DownloadFunction) -> Task:
        task = self.repository.get_task(task_id)
        self.repository.transition(
            task.id,
            TaskStatus.RESOLVING,
            progress=5,
            message="正在解析视频",
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
