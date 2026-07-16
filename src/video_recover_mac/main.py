from __future__ import annotations

import logging
import threading
from pathlib import Path

from video_recover_mac.client import WorkerClient
from video_recover_mac.config import MacWorkerSettings
from video_recover_mac.transcriber import LazyMlxTranscriber


def resolve_media_path(data_dir: Path, relative_path: Path) -> Path:
    root = data_dir.expanduser().resolve()
    resolved = (root / relative_path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("worker media path escapes data root")
    return resolved


class NativeWorker:
    def __init__(
        self,
        *,
        client,
        transcriber,
        data_dir: Path,
        worker_id: str,
        heartbeat_seconds: float,
        poll_seconds: float = 2.0,
    ) -> None:
        self.client = client
        self.transcriber = transcriber
        self.data_dir = data_dir
        self.worker_id = worker_id
        self.heartbeat_seconds = heartbeat_seconds
        self.poll_seconds = poll_seconds
        self.stop_event = threading.Event()
        self.logger = logging.getLogger(__name__)

    def _heartbeat_loop(self, lease_id: str, stopped: threading.Event) -> None:
        while not stopped.wait(self.heartbeat_seconds):
            if not self.client.heartbeat(lease_id):
                return

    def run_once(self) -> bool:
        lease = self.client.lease(self.worker_id)
        if lease is None:
            self.transcriber.unload_if_idle()
            return False
        heartbeat_stop = threading.Event()
        self.client.heartbeat(lease.lease_id)
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(lease.lease_id, heartbeat_stop),
            name="video-recover-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            media_path = resolve_media_path(self.data_dir, Path(lease.media_path))
            segments = self.transcriber.transcribe(media_path)
            self.client.complete(lease.lease_id, segments)
        except Exception:
            self.logger.exception("native transcription failed", extra={"task_id": lease.task_id})
            self.client.fail(lease.lease_id, "Mac 原生转写失败")
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=self.heartbeat_seconds + 1)
        return True

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            if not self.run_once():
                self.stop_event.wait(self.poll_seconds)

    def stop(self) -> None:
        self.stop_event.set()


def main() -> None:
    settings = MacWorkerSettings()
    logging.basicConfig(level=logging.INFO)
    worker = NativeWorker(
        client=WorkerClient(settings.control_url, settings.worker_token),
        transcriber=LazyMlxTranscriber(
            model_name=settings.mlx_model,
            idle_seconds=settings.model_idle_seconds,
        ),
        data_dir=settings.data_dir,
        worker_id=settings.worker_id,
        heartbeat_seconds=settings.heartbeat_seconds,
        poll_seconds=settings.poll_seconds,
    )
    worker.run_forever()


if __name__ == "__main__":
    main()
