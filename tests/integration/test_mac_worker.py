from pathlib import Path

import pytest

from video_recover.domain import Segment
from video_recover_mac.client import LeasePayload
from video_recover_mac.main import NativeWorker, resolve_media_path


class FakeClient:
    def __init__(self):
        self.heartbeat_calls = 0
        self.completed_segments = None

    def lease(self, _worker_id):
        return LeasePayload("lease-1", "task-1", "downloads/123/video.mp4")

    def heartbeat(self, _lease_id):
        self.heartbeat_calls += 1
        return True

    def complete(self, _lease_id, segments):
        self.completed_segments = segments

    def fail(self, _lease_id, _message):
        raise AssertionError("worker should not fail")


class FakeTranscriber:
    def transcribe(self, path):
        assert path.name == "video.mp4"
        return [Segment(0, 1, "识别完成。")]

    def unload_if_idle(self):
        return False


def test_worker_heartbeats_and_completes_lease(tmp_path):
    video = tmp_path / "downloads" / "123" / "video.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    client = FakeClient()
    worker = NativeWorker(
        client=client,
        transcriber=FakeTranscriber(),
        data_dir=tmp_path,
        worker_id="mac-test",
        heartbeat_seconds=60,
    )

    assert worker.run_once() is True
    assert client.heartbeat_calls >= 1
    assert client.completed_segments == [Segment(0, 1, "识别完成。")]


def test_media_path_cannot_escape_data_root(tmp_path):
    with pytest.raises(ValueError):
        resolve_media_path(tmp_path, Path("../../etc/passwd"))
