from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from video_recover.config import Settings
from video_recover.crypto import CookieVault
from video_recover.domain import TaskStatus
from video_recover.main import create_app
from video_recover.parsers import ResolvedMedia
from video_recover.repository import Repository
from video_recover.service import VideoService

TEST_URL = "https://www.douyin.com/video/7662212894569811235"


class StubParser:
    def resolve(self, url: str, *, cookie: str | None) -> ResolvedMedia:
        return ResolvedMedia(
            aweme_id="7662212894569811235",
            canonical_url=url,
            media_url="https://v3-dy-o-abtest.zjcdn.com/video.mp4",
            description="发布描述",
            author="作者",
            duration_seconds=52,
            cover_url=None,
            request_headers={},
        )


def make_client(tmp_path: Path) -> tuple[TestClient, VideoService]:
    settings = Settings(
        data_dir=tmp_path / "data",
        worker_token="test-worker-token-long-enough",
    )
    settings.ensure_directories()
    service = VideoService(
        settings=settings,
        repository=Repository(settings.database_path),
        parser=StubParser(),
        cookie_vault=CookieVault(settings.secret_key_path),
    )
    app = create_app(settings, service=service, start_runner=False)
    return TestClient(app), service


def test_submit_is_async_and_returns_task(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)

    response = client.post("/api/tasks", json={"url": TEST_URL, "transcribe": True})

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["canonical_url"] == TEST_URL


def test_cookie_value_never_returns_from_api(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)

    response = client.put(
        "/api/settings/cookie",
        json={"cookie": "sessionid=top-secret"},
    )

    assert response.status_code == 204
    payload = client.get("/api/status").json()
    assert payload["cookie"]["configured"] is True
    assert "top-secret" not in str(payload)


def test_worker_endpoint_requires_exact_bearer_token(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)

    assert client.post("/internal/worker/lease").status_code == 401
    assert (
        client.post(
            "/internal/worker/lease",
            headers={"Authorization": "Bearer wrong-worker-token"},
            json={"worker_id": "mac"},
        ).status_code
        == 401
    )


def test_idle_worker_poll_is_visible_in_service_status(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    headers = {"Authorization": "Bearer test-worker-token-long-enough"}

    lease = client.post(
        "/internal/worker/lease",
        headers=headers,
        json={"worker_id": "macbook-m5"},
    )
    status_payload = client.get("/api/status").json()

    assert lease.status_code == 204
    assert status_payload["worker"]["connected"] is True
    assert status_payload["worker"]["worker_id"] == "macbook-m5"


def test_native_worker_lease_heartbeat_and_complete(tmp_path: Path) -> None:
    client, service = make_client(tmp_path)
    task, _ = service.submit(TEST_URL)

    def fake_download(media, target: Path, **options):
        target.write_bytes(b"video")
        return target

    service.process_download(task.id, fake_download)
    headers = {"Authorization": "Bearer test-worker-token-long-enough"}

    response = client.post(
        "/internal/worker/lease",
        headers=headers,
        json={"worker_id": "macbook"},
    )
    assert response.status_code == 200
    lease = response.json()
    assert lease["media_path"] == "downloads/7662212894569811235/video.mp4"
    assert client.post(
        f"/internal/worker/{lease['lease_id']}/heartbeat",
        headers=headers,
    ).status_code == 204

    complete = client.post(
        f"/internal/worker/{lease['lease_id']}/complete",
        headers=headers,
        json={"segments": [{"start": 0, "end": 1.5, "text": "语音文案"}]},
    )
    assert complete.status_code == 200
    assert complete.json()["status"] == "completed"
    assert (
        service.settings.download_dir / "7662212894569811235" / "transcript.srt"
    ).exists()


def test_api_lists_retries_downloads_and_deletes(tmp_path: Path) -> None:
    client, service = make_client(tmp_path)
    created = client.post("/api/tasks", json={"url": TEST_URL}).json()
    task_id = created["id"]

    listed = client.get("/api/tasks").json()
    assert [task["id"] for task in listed] == [task_id]
    assert client.get(f"/api/tasks/{task_id}").status_code == 200

    service.repository.transition(
        task_id,
        TaskStatus.RESOLVING,
        progress=5,
        message="解析",
    )
    assert client.post(f"/api/tasks/{task_id}/retry").status_code == 409
    assert client.delete(f"/api/tasks/{task_id}").status_code == 409


def test_artifacts_are_allowlisted_and_missing_tasks_are_404(tmp_path: Path) -> None:
    client, service = make_client(tmp_path)
    task, _ = service.submit(TEST_URL, transcribe=False)

    def fake_download(media, target: Path, **options):
        target.write_bytes(b"video")
        return target

    service.process_download(task.id, fake_download)

    metadata = client.get(f"/api/tasks/{task.id}/artifacts/metadata")
    assert metadata.status_code == 200
    assert metadata.json()["description"] == "发布描述"
    assert client.get(f"/api/tasks/{task.id}/artifacts/secrets").status_code == 404
    assert client.get("/api/tasks/missing").status_code == 404
    assert client.delete(f"/api/tasks/{task.id}").status_code == 204
    assert not (service.settings.download_dir / "7662212894569811235").exists()


def test_openapi_includes_public_and_internal_contracts(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)

    schema = client.get("/openapi.json").json()

    assert "/api/tasks" in schema["paths"]
    assert "/internal/worker/lease" in schema["paths"]
