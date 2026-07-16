from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from video_recover.config import Settings
from video_recover.crypto import CookieVault
from video_recover.domain import TaskStatus
from video_recover.errors import (
    DownloadFailed,
    DownloadTooLarge,
    InsufficientStorage,
    ParserChanged,
    TranscriptionFailed,
    UnsafeCapture,
)
from video_recover.parsers import ResolvedMedia
from video_recover.repository import Repository
from video_recover.service import VideoService, _atomic_copy

TEST_URL = "https://www.douyin.com/video/7662212894569811235"
MEDIA = ResolvedMedia(
    aweme_id="7662212894569811235",
    canonical_url=TEST_URL,
    media_url="https://v3-dy-o-abtest.zjcdn.com/video.mp4",
    description="发布描述",
    author="作者",
    duration_seconds=52,
    cover_url=None,
    request_headers={},
)


class StubParser:
    def resolve(self, url, *, cookie):
        assert url == TEST_URL
        return MEDIA


def make_service(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_directories()
    repository = Repository(settings.database_path)
    return VideoService(
        settings=settings,
        repository=repository,
        parser=StubParser(),
        cookie_vault=CookieVault(settings.secret_key_path),
    )


def test_submit_deduplicates_same_video(tmp_path):
    service = make_service(tmp_path)

    first, first_created = service.submit(TEST_URL, source="web")
    second, second_created = service.submit(TEST_URL, source="mcp")

    assert first_created is True
    assert second_created is False
    assert first.id == second.id


def test_cookie_round_trip_never_stores_plain_text(tmp_path):
    service = make_service(tmp_path)
    service.save_cookie("sessionid=top-secret")

    stored = service.repository.get_setting("douyin_cookie")
    assert stored is not None
    assert "top-secret" not in stored
    assert service.get_cookie() == "sessionid=top-secret"


def test_import_local_capture_stages_video_and_description_for_transcription(tmp_path):
    service = make_service(tmp_path)
    capture_dir = service.settings.data_dir / "browser-capture"
    capture_dir.mkdir(parents=True, exist_ok=True)
    capture = capture_dir / "7662212894569811235.mp4"
    capture.write_bytes(b"real-browser-video")

    task = service.import_local_capture(
        TEST_URL,
        capture,
        description="口播账号的三个核心数据,分别由什么决定 #郑经说 #短视频口播",
        author="郑经说",
        duration_seconds=19.916667,
    )

    assert task.status == TaskStatus.AWAITING_TRANSCRIPTION
    assert task.source == "chrome"
    assert task.output_dir is not None
    assert (task.output_dir / "video.mp4").read_bytes() == b"real-browser-video"
    assert "三个核心数据" in (task.output_dir / "description.txt").read_text(
        encoding="utf-8"
    )
    metadata = json.loads((task.output_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["aweme_id"] == "7662212894569811235"
    assert metadata["capture_source"] == "chrome"


def test_import_local_capture_rejects_files_outside_capture_root(tmp_path):
    service = make_service(tmp_path)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"do-not-import")

    with pytest.raises(UnsafeCapture):
        service.import_local_capture(TEST_URL, outside, description="描述", author="作者")

    assert service.repository.list_tasks() == []


def test_import_local_capture_atomically_replaces_failed_parser_task(tmp_path):
    service = make_service(tmp_path)
    task, _ = service.submit(TEST_URL)
    service.repository.transition(
        task.id,
        TaskStatus.RESOLVING,
        progress=5,
        message="正在解析视频",
    )
    service.record_failure(task.id, ParserChanged())
    capture_dir = service.settings.data_dir / "browser-capture"
    capture_dir.mkdir(parents=True, exist_ok=True)
    capture = capture_dir / "7662212894569811235.mp4"
    capture.write_bytes(b"browser-fallback-video")

    imported = service.import_local_capture(
        TEST_URL,
        capture,
        description="发布描述",
        author="作者",
    )

    assert imported.id == task.id
    assert imported.status == TaskStatus.AWAITING_TRANSCRIPTION
    assert imported.error_code is None


def test_import_local_capture_can_replace_partial_transcription_task(tmp_path):
    service = make_service(tmp_path)
    task, _ = service.submit(TEST_URL)
    service.repository.transition(task.id, TaskStatus.RESOLVING, progress=5, message="解析")
    service.repository.transition(task.id, TaskStatus.DOWNLOADING, progress=20, message="下载")
    service.repository.transition(
        task.id,
        TaskStatus.AWAITING_TRANSCRIPTION,
        progress=60,
        message="等待转写",
    )
    service.record_failure(task.id, TranscriptionFailed())
    capture_dir = service.settings.data_dir / "browser-capture"
    capture_dir.mkdir(parents=True, exist_ok=True)
    capture = capture_dir / "7662212894569811235.mp4"
    capture.write_bytes(b"replacement-video")

    imported = service.import_local_capture(
        TEST_URL,
        capture,
        description="发布描述",
        author="作者",
    )

    assert imported.id == task.id
    assert imported.status == TaskStatus.AWAITING_TRANSCRIPTION
    assert imported.output_dir is not None
    assert (imported.output_dir / "video.mp4").read_bytes() == b"replacement-video"


def test_import_local_capture_failure_marks_task_failed(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    capture_dir = service.settings.data_dir / "browser-capture"
    capture_dir.mkdir(parents=True, exist_ok=True)
    capture = capture_dir / "7662212894569811235.mp4"
    capture.write_bytes(b"browser-video")

    def fail_copy(_source, _target, **_options):
        raise OSError("disk write failed")

    monkeypatch.setattr("video_recover.service._atomic_copy", fail_copy)

    with pytest.raises(DownloadFailed):
        service.import_local_capture(
            TEST_URL,
            capture,
            description="发布描述",
            author="作者",
        )

    task = service.repository.list_tasks()[0]
    assert task.status == TaskStatus.FAILED
    assert task.error_code == "download_failed"


def test_settings_create_browser_capture_directory(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")

    settings.ensure_directories()

    assert settings.browser_capture_dir.is_dir()


def test_import_requires_space_for_capture_plus_reserve(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    capture = service.settings.browser_capture_dir / "7662212894569811235.mp4"
    capture.write_bytes(b"browser-video")
    available = service.settings.minimum_free_bytes + capture.stat().st_size - 1
    monkeypatch.setattr(
        "video_recover.service.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=available),
    )

    with pytest.raises(InsufficientStorage):
        service.import_local_capture(TEST_URL, capture, description="描述", author="作者")

    assert service.repository.list_tasks() == []


def test_atomic_copy_enforces_maximum_while_streaming(tmp_path):
    source = tmp_path / "growing.mp4"
    source.write_bytes(b"12345")
    target = tmp_path / "video.mp4"

    with pytest.raises(DownloadTooLarge):
        _atomic_copy(source, target, max_bytes=4, minimum_free_bytes=0)

    assert not target.exists()
    assert not target.with_suffix(".mp4.tmp").exists()


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), float("-inf"), -1.0])
def test_import_rejects_non_finite_or_negative_duration(tmp_path, duration):
    service = make_service(tmp_path)
    capture = service.settings.browser_capture_dir / "7662212894569811235.mp4"
    capture.write_bytes(b"browser-video")

    with pytest.raises(UnsafeCapture):
        service.import_local_capture(
            TEST_URL,
            capture,
            description="描述",
            author="作者",
            duration_seconds=duration,
        )

    assert service.repository.list_tasks() == []
