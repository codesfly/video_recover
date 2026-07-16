from __future__ import annotations

import httpx
import pytest

from video_recover.downloader import download_file
from video_recover.errors import DownloadTooLarge, UnsafeMediaUrl
from video_recover.parsers import ResolvedMedia


def media(url: str = "https://v3-dy-o-abtest.zjcdn.com/video.mp4") -> ResolvedMedia:
    return ResolvedMedia(
        aweme_id="7662212894569811235",
        canonical_url="https://www.douyin.com/video/7662212894569811235",
        media_url=url,
        description="描述",
        author="作者",
        duration_seconds=1,
        cover_url=None,
        request_headers={"Referer": "https://www.douyin.com/"},
    )


def test_resumes_part_file_with_range_header(tmp_path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(206, headers={"content-length": "5"}, content=b"-rest")

    target = tmp_path / "video.mp4"
    target.with_suffix(".mp4.part").write_bytes(b"first")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        download_file(media(), target, client=client, minimum_free_bytes=0)

    assert requests[0].headers["Range"] == "bytes=5-"
    assert target.read_bytes() == b"first-rest"
    assert not target.with_suffix(".mp4.part").exists()


def test_restarts_when_server_ignores_range(tmp_path):
    target = tmp_path / "video.mp4"
    target.with_suffix(".mp4.part").write_bytes(b"stale")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Range"] == "bytes=5-"
        return httpx.Response(200, headers={"content-length": "5"}, content=b"fresh")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        download_file(media(), target, client=client, minimum_free_bytes=0)

    assert target.read_bytes() == b"fresh"


def test_rejects_media_larger_than_limit(tmp_path):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "20"}, content=b"x" * 20)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(DownloadTooLarge),
    ):
        download_file(media(), tmp_path / "video.mp4", client=client, max_bytes=10)

    assert not (tmp_path / "video.mp4").exists()


def test_rejects_untrusted_media_host_before_request(tmp_path):
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"bad")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(UnsafeMediaUrl),
    ):
        download_file(
            media("https://evil.example/video.mp4"),
            tmp_path / "video.mp4",
            client=client,
        )

    assert calls == 0
