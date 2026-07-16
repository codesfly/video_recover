from __future__ import annotations

from datetime import UTC, datetime, timedelta

from video_recover_mac.transcriber import LazyMlxTranscriber


class Clock:
    def __init__(self):
        self.value = datetime(2026, 7, 17, 9, 0, tzinfo=UTC)

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class FakeEngine:
    def __init__(self):
        self.closed = False

    def transcribe(self, _path):
        return {
            "segments": [
                {"start": 0.0, "end": 1.5, "text": " 原生识别文案。 "},
            ]
        }

    def close(self):
        self.closed = True


def test_model_loads_on_first_job_and_is_reused(tmp_path):
    calls = 0

    def loader(_model_name):
        nonlocal calls
        calls += 1
        return FakeEngine()

    transcriber = LazyMlxTranscriber(loader=loader)
    first = transcriber.transcribe(tmp_path / "sample.mp4")
    second = transcriber.transcribe(tmp_path / "sample.mp4")

    assert calls == 1
    assert first == second
    assert first[0].text == "原生识别文案。"


def test_model_unloads_after_idle_timeout(tmp_path):
    clock = Clock()
    engine = FakeEngine()
    transcriber = LazyMlxTranscriber(
        loader=lambda _name: engine,
        idle_seconds=600,
        clock=clock.now,
    )
    transcriber.transcribe(tmp_path / "sample.mp4")

    clock.advance(601)

    assert transcriber.unload_if_idle() is True
    assert engine.closed is True
    assert transcriber.loaded is False

