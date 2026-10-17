from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_recover.comments import CommentHarvest, map_comment
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
    "fetch_comments",
    "get_comments",
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


def make_service(tmp_path: Path, comment_fetcher=None) -> tuple[Settings, VideoService]:
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
        comment_fetcher=comment_fetcher,
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
            "/mcp",
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


class OneCommentFetcher:
    def fetch(self, canonical_url, *, max_comments, deadline_seconds):
        raw = {
            "cid": "1", "text": "第一行\n第二行", "digg_count": 7, "ip_label": "广东",
            "create_time": 1791029240,
        }
        return CommentHarvest((map_comment(raw),), 1, True, 99, None)


@pytest.mark.asyncio
async def test_comment_tools_queue_and_return_compact_text(tmp_path: Path) -> None:
    _settings, service = make_service(tmp_path, comment_fetcher=OneCommentFetcher())
    mcp = build_mcp(service)
    _c, submitted = await mcp.call_tool(
        "submit_video", {"url": TEST_URL, "fetch_comments": True}
    )
    assert submitted["comment_job_id"]

    _c, empty = await mcp.call_tool("get_comments", {"task_id": submitted["task_id"]})
    assert empty["available"] is False
    assert empty["latest_job"]["status"] == "queued"

    service.process_comment_job(service.repository.claim_next_comment_job())
    _c, result = await mcp.call_tool(
        "get_comments", {"task_id": submitted["task_id"], "limit": 500}
    )
    assert result["available"] is True and result["limit"] == 200
    assert result["text"].splitlines()[0].startswith("👍7")
    assert "第一行 第二行" in result["text"]
    assert result["snapshot"]["has_more_remaining"] is True

    _c, again = await mcp.call_tool("fetch_comments", {"task_id": submitted["task_id"]})
    assert again["ok"] is True and again["created"] is True
    _c, missing = await mcp.call_tool("fetch_comments", {"task_id": "missing"})
    assert missing["error_code"] == "not_found"


@pytest.mark.asyncio
async def test_comment_tools_declare_read_and_write_annotations(tmp_path: Path) -> None:
    _settings, service = make_service(tmp_path)
    tools = {tool.name: tool for tool in await build_mcp(service).list_tools()}
    assert tools["get_comments"].annotations.readOnlyHint is True
    assert tools["fetch_comments"].annotations.readOnlyHint is False
