from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from video_recover.config import Settings
from video_recover.crypto import CookieVault
from video_recover.main import create_app
from video_recover.parsers import ResolvedMedia
from video_recover.repository import Repository
from video_recover.service import VideoService


class StubParser:
    def resolve(self, url: str, *, cookie: str | None) -> ResolvedMedia:
        raise AssertionError("the page test must not parse network content")


def make_client(tmp_path: Path) -> TestClient:
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
    return TestClient(create_app(settings, service=service, start_runner=False))


def test_archive_page_has_compact_workspace_and_accessible_controls(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert "VideoRecover" in response.text
    assert 'class="capture-bar"' in response.text
    assert 'aria-label="最近任务"' in response.text
    assert 'class="record-menu"' in response.text
    assert 'for="video-url"' in response.text
    assert 'id="task-status"' in response.text
    assert 'aria-live="polite"' in response.text
    assert "解析第一条抖音视频" in response.text
    assert "下载文件" in response.text
    assert "NEW ARCHIVE" not in response.text
    assert "YOUR LOCAL COLLECTION" not in response.text
    assert "EXPORT / 导出产物" not in response.text


def test_frontend_assets_are_served_and_api_remains_available(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    stylesheet = client.get("/static/app.css")
    script = client.get("/static/app.js")

    assert stylesheet.status_code == 200
    assert "prefers-reduced-motion" in stylesheet.text
    assert "@container" in stylesheet.text
    assert ".capture-bar" in stylesheet.text
    assert ".archive-layout" in stylesheet.text
    assert ".record-menu" in stylesheet.text
    assert ".task-index" not in stylesheet.text
    assert script.status_code == 200
    assert "AbortController" in script.text
    assert client.get("/api/status").status_code == 200


def test_unknown_page_is_not_replaced_by_spa_shell(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    assert client.get("/does-not-exist").status_code == 404
