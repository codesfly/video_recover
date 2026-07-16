from __future__ import annotations

import os
import time

import httpx
import pytest

TEST_URL = "https://www.douyin.com/video/7662212894569811235"
TERMINAL_STATES = {"completed", "partial", "failed", "cancelled"}

pytestmark = pytest.mark.live


def require_live() -> None:
    if os.getenv("VIDEO_RECOVER_LIVE") != "1":
        pytest.skip("set VIDEO_RECOVER_LIVE=1 to run the real download acceptance test")


def test_requested_douyin_video_completes_with_all_artifacts() -> None:
    require_live()
    base_url = os.getenv("VIDEO_RECOVER_BASE_URL", "http://127.0.0.1:8787")
    timeout_seconds = int(os.getenv("VIDEO_RECOVER_LIVE_TIMEOUT", "1800"))
    client = httpx.Client(base_url=base_url, timeout=60)

    response = client.post("/api/tasks", json={"url": TEST_URL, "transcribe": True})
    response.raise_for_status()
    task = response.json()
    task_id = task["id"]
    if task["status"] in {"failed", "partial"}:
        retry = client.post(f"/api/tasks/{task_id}/retry")
        retry.raise_for_status()

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        task_response = client.get(f"/api/tasks/{task_id}")
        task_response.raise_for_status()
        task = task_response.json()
        if task["status"] in TERMINAL_STATES:
            break
        time.sleep(2)
    else:
        pytest.fail(f"task did not finish within {timeout_seconds} seconds")

    if task["status"] != "completed":
        pytest.fail(
            f"live task ended as {task['status']}: "
            f"{task.get('error_code')} {task.get('error_message')}"
        )

    video = client.get(f"/api/tasks/{task_id}/artifacts/video")
    video.raise_for_status()
    assert len(video.content) > 1024

    description = client.get(f"/api/tasks/{task_id}/artifacts/description")
    transcript = client.get(f"/api/tasks/{task_id}/artifacts/transcript")
    subtitles = client.get(f"/api/tasks/{task_id}/artifacts/srt")
    markdown = client.get(f"/api/tasks/{task_id}/artifacts/markdown")
    metadata = client.get(f"/api/tasks/{task_id}/artifacts/metadata")
    for artifact in (description, transcript, subtitles, markdown, metadata):
        artifact.raise_for_status()

    assert description.text.strip()
    assert transcript.text.strip()
    assert "-->" in subtitles.text
    assert "## 视频文案" in markdown.text
    assert metadata.json()["aweme_id"] == "7662212894569811235"
