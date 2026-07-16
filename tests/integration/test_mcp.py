from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_recover.config import Settings
from video_recover.crypto import CookieVault
from video_recover.main import create_app
from video_recover.mcp_server import build_mcp
from video_recover.parsers import ResolvedMedia
from video_recover.repository import Repository
from video_recover.service import VideoService

TEST_URL = "https://www.douyin.com/video/7662212894569811235"
EXPECTED_TOOLS = {
    "submit_video",
    "get_task",
    "list_videos",
    "get_metadata",
    "get_transcript",
    "retry_task",
    "get_service_status",
}


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


def make_service(tmp_path: Path) -> tuple[Settings, VideoService]:
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
    return settings, service


@pytest.mark.asyncio
async def test_mcp_exposes_only_safe_tool_set(tmp_path: Path) -> None:
    _settings, service = make_service(tmp_path)
    mcp = build_mcp(service)

    tools = {tool.name for tool in await mcp.list_tools()}

    assert tools == EXPECTED_TOOLS
    assert "delete_video" not in tools
    assert "set_cookie" not in tools


@pytest.mark.asyncio
async def test_submit_tool_uses_same_persisted_service(tmp_path: Path) -> None:
    _settings, service = make_service(tmp_path)
    mcp = build_mcp(service)

    _content, result = await mcp.call_tool("submit_video", {"url": TEST_URL})

    assert isinstance(result, dict)
    task_id = result["task_id"]
    assert service.repository.get_task(task_id).source == "mcp"
    assert result["status"] == "queued"


@pytest.mark.asyncio
async def test_mcp_reads_metadata_and_transcript_without_path_access(tmp_path: Path) -> None:
    _settings, service = make_service(tmp_path)
    task, _ = service.submit(TEST_URL, transcribe=False)

    def fake_download(media, target: Path, **options):
        target.write_bytes(b"video")
        return target

    service.process_download(task.id, fake_download)
    mcp = build_mcp(service)

    _metadata_content, metadata = await mcp.call_tool(
        "get_metadata",
        {"task_id": task.id},
    )
    _transcript_content, transcript = await mcp.call_tool(
        "get_transcript",
        {"task_id": task.id},
    )

    assert metadata["description"] == "发布描述"
    assert transcript["available"] is False
    assert "output_dir" not in str(metadata)


def test_streamable_http_mcp_is_mounted_on_app(tmp_path: Path) -> None:
    settings, service = make_service(tmp_path)
    app = create_app(settings, service=service, start_runner=False)

    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "video-recover"
