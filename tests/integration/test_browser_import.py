from __future__ import annotations

import json

import video_recover.browser_import as browser_import
from video_recover.browser_import import main

TEST_URL = "https://www.douyin.com/video/7662212894569811235"


def test_browser_import_cli_stages_capture_and_prints_safe_result(
    tmp_path,
    monkeypatch,
    capsys,
):
    data_dir = tmp_path / "data"
    capture_dir = data_dir / "browser-capture"
    capture_dir.mkdir(parents=True)
    capture = capture_dir / "7662212894569811235.mp4"
    capture.write_bytes(b"browser-video")
    monkeypatch.setenv("VIDEO_RECOVER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VIDEO_RECOVER_MINIMUM_FREE_BYTES", "1")

    exit_code = main(
        [
            "--url",
            TEST_URL,
            "--file",
            str(capture),
            "--description",
            "发布描述",
            "--author",
            "作者",
            "--duration",
            "19.9",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["status"] == "awaiting_transcription"
    assert result["aweme_id"] == "7662212894569811235"
    assert "cookie" not in result


def test_browser_import_cli_reports_duplicate_without_traceback(
    tmp_path,
    monkeypatch,
    capsys,
):
    data_dir = tmp_path / "data"
    capture_dir = data_dir / "browser-capture"
    capture_dir.mkdir(parents=True)
    capture = capture_dir / "7662212894569811235.mp4"
    capture.write_bytes(b"browser-video")
    monkeypatch.setenv("VIDEO_RECOVER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VIDEO_RECOVER_MINIMUM_FREE_BYTES", "1")
    args = [
        "--url",
        TEST_URL,
        "--file",
        str(capture),
        "--description",
        "发布描述",
        "--author",
        "作者",
        "--no-transcribe",
    ]

    assert main(args) == 0
    capsys.readouterr()
    exit_code = main(args)

    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert exit_code == 2
    assert payload["error_code"] == "capture_conflict"
    assert "Traceback" not in captured.err
    assert str(tmp_path) not in captured.err


def test_browser_import_cli_masks_unexpected_internal_error(tmp_path, monkeypatch, capsys):
    capture = tmp_path / "private-location.mp4"
    monkeypatch.setattr(
        browser_import,
        "build_service",
        lambda _settings: (_ for _ in ()).throw(RuntimeError(str(capture))),
    )

    exit_code = browser_import.main(
        [
            "--url",
            TEST_URL,
            "--file",
            str(capture),
            "--description",
            "发布描述",
            "--author",
            "作者",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert exit_code == 2
    assert payload["error_code"] == "internal_failure"
    assert "Traceback" not in captured.err
    assert str(capture) not in captured.err
