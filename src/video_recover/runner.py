from __future__ import annotations

import logging
import threading

from video_recover.domain import TaskStatus
from video_recover.downloader import download_file
from video_recover.errors import InternalFailure, UserFacingError
from video_recover.service import DownloadFunction, VideoService
from video_recover.transcribers import Transcriber


class JobRunner:
    def __init__(
        self,
        service: VideoService,
        *,
        downloader: DownloadFunction = download_file,
        cpu_transcriber: Transcriber | None = None,
        allow_cpu_fallback: bool = False,
        poll_seconds: float = 1.0,
    ) -> None:
        self.service = service
        self.repository = service.repository
        self.downloader = downloader
        self.cpu_transcriber = cpu_transcriber
        self.allow_cpu_fallback = allow_cpu_fallback
        self.poll_seconds = poll_seconds
        self.logger = logging.getLogger(__name__)
        self._stop = threading.Event()

    def run_once(self) -> bool:
        self.repository.recover_expired_leases()
        task = self.repository.next_task(TaskStatus.QUEUED)
        if task is not None:
            try:
                self.service.process_download(task.id, self.downloader)
            except UserFacingError as exc:
                self.service.record_failure(task.id, exc)
            except Exception:
                self.logger.exception("download pipeline failed", extra={"task_id": task.id})
                self.service.record_failure(task.id, InternalFailure())
            return True

        if not self.allow_cpu_fallback or self.cpu_transcriber is None:
            return False
        lease = self.repository.acquire_transcription_lease(
            "container-cpu",
            ttl_seconds=3600,
        )
        if lease is None:
            return False
        task = self.repository.get_task(lease.task_id)
        try:
            if task.output_dir is None:
                raise InternalFailure()
            segments = self.cpu_transcriber.transcribe(task.output_dir / "video.mp4")
            self.service.complete_transcription(lease.id, task.id, segments)
        except UserFacingError as exc:
            self.service.record_failure(task.id, exc, lease_id=lease.id)
        except Exception:
            self.logger.exception("transcription pipeline failed", extra={"task_id": task.id})
            self.service.record_failure(task.id, InternalFailure(), lease_id=lease.id)
        return True

    def run_forever(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(self.poll_seconds)

    def start_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, name="video-recover-runner", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()
