from __future__ import annotations

import json

from video_recover.config import Settings
from video_recover.crypto import CookieVault
from video_recover.domain import Segment, TaskStatus
from video_recover.errors import ParserChanged, TranscriptionFailed
from video_recover.parsers import ResolvedMedia
from video_recover.repository import Repository
from video_recover.runner import JobRunner
from video_recover.service import VideoService

TEST_URL = "https://www.douyin.com/video/7662212894569811235"
MEDIA = ResolvedMedia(
    aweme_id="7662212894569811235",
    canonical_url=TEST_URL,
    media_url="https://v3-dy-o-abtest.zjcdn.com/video.mp4",
    description="发布描述",
    author="作者",
    duration_seconds=52,
    cover_url="https://p3-sign.douyinpic.com/cover.jpeg",
    request_headers={},
)


class StubParser:
    def __init__(self, error=None):
        self.error = error

    def resolve(self, _url, *, cookie):
        if self.error:
            raise self.error
        return MEDIA


class StubTranscriber:
    def __init__(self, error=None):
        self.error = error

    def transcribe(self, video_path):
        assert video_path.read_bytes() == b"video-bytes"
        if self.error:
            raise self.error
        return [Segment(0, 1.2, "识别出的文案。")]


def fake_downloader(_media, target, **_options):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"video-bytes")
    return target


def make_context(tmp_path, *, parser=None, transcriber=None, native_grace_seconds=0):
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_directories()
    repository = Repository(settings.database_path)
    service = VideoService(
        settings=settings,
        repository=repository,
        parser=parser or StubParser(),
        cookie_vault=CookieVault(settings.secret_key_path),
    )
    runner = JobRunner(
        service,
        downloader=fake_downloader,
        cpu_transcriber=transcriber,
        allow_cpu_fallback=transcriber is not None,
        native_worker_grace_seconds=native_grace_seconds,
    )
    return service, repository, runner


def test_runner_persists_video_then_waits_for_native_transcription(tmp_path):
    service, repository, runner = make_context(tmp_path)
    task, _ = service.submit(TEST_URL)

    assert runner.run_once() is True

    saved = repository.get_task(task.id)
    assert saved.status == TaskStatus.AWAITING_TRANSCRIPTION
    assert saved.output_dir is not None
    assert (saved.output_dir / "video.mp4").read_bytes() == b"video-bytes"
    assert (saved.output_dir / "description.txt").read_text(encoding="utf-8") == "发布描述\n"
    metadata = json.loads((saved.output_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["aweme_id"] == MEDIA.aweme_id


def test_cpu_transcription_completes_artifacts(tmp_path):
    service, repository, runner = make_context(tmp_path, transcriber=StubTranscriber())
    task, _ = service.submit(TEST_URL)

    runner.run_once()
    runner.run_once()

    saved = repository.get_task(task.id)
    assert saved.status == TaskStatus.COMPLETED
    assert saved.output_dir is not None
    assert "识别出的文案" in (saved.output_dir / "transcript.txt").read_text(encoding="utf-8")


def test_cpu_fallback_waits_for_native_worker_grace_period(tmp_path):
    service, repository, runner = make_context(
        tmp_path,
        transcriber=StubTranscriber(),
        native_grace_seconds=300,
    )
    task, _ = service.submit(TEST_URL)

    assert runner.run_once() is True
    assert runner.run_once() is False

    assert repository.get_task(task.id).status == TaskStatus.AWAITING_TRANSCRIPTION


def test_transcription_failure_preserves_video_and_marks_partial(tmp_path):
    transcriber = StubTranscriber(error=TranscriptionFailed("decoder failed"))
    service, repository, runner = make_context(tmp_path, transcriber=transcriber)
    task, _ = service.submit(TEST_URL)

    runner.run_once()
    runner.run_once()

    saved = repository.get_task(task.id)
    assert saved.status == TaskStatus.PARTIAL
    assert saved.output_dir is not None
    assert (saved.output_dir / "video.mp4").exists()


def test_parser_failure_is_categorized_without_internal_message(tmp_path):
    service, repository, runner = make_context(tmp_path, parser=StubParser(ParserChanged()))
    task, _ = service.submit(TEST_URL)

    runner.run_once()

    saved = repository.get_task(task.id)
    assert saved.status == TaskStatus.FAILED
    assert saved.error_code == "parser_changed"
