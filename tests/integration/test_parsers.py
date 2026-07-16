from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from video_recover.errors import CookieRequired, ParserChanged
from video_recover.parsers import (
    DouyinPageParser,
    ParserChain,
    ResolvedMedia,
    YtDlpParser,
)
from video_recover.url_policy import normalize_douyin_url

TEST_URL = "https://www.douyin.com/video/7662212894569811235"
FIXTURES = Path(__file__).parents[1] / "fixtures"
MEDIA = ResolvedMedia(
    aweme_id="7662212894569811235",
    canonical_url=TEST_URL,
    media_url="https://v3-dy-o-abtest.zjcdn.com/video.mp4",
    description="描述",
    author="作者",
    duration_seconds=52.0,
    cover_url=None,
    request_headers={},
)


class StubParser:
    def __init__(self, *, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def resolve(self, url, *, cookie):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def test_chain_falls_back_after_recoverable_parser_error():
    first = StubParser(error=ParserChanged("yt-dlp changed"))
    second = StubParser(result=MEDIA)

    assert ParserChain([first, second]).resolve(TEST_URL, cookie=None) == MEDIA
    assert first.calls == 1
    assert second.calls == 1


def test_auth_error_is_reported_when_all_parsers_require_cookie():
    chain = ParserChain(
        [
            StubParser(error=CookieRequired()),
            StubParser(error=CookieRequired()),
        ]
    )

    with pytest.raises(CookieRequired):
        chain.resolve(TEST_URL, cookie=None)


def test_yt_dlp_mapping_selects_highest_quality_mp4():
    info = json.loads((FIXTURES / "yt_dlp_info.json").read_text(encoding="utf-8"))
    media = YtDlpParser.map_info(info, normalize_douyin_url(TEST_URL), cookie=None)

    assert media.media_url.endswith("high.mp4")
    assert media.description == "发布描述"
    assert media.author == "测试作者"
    assert media.duration_seconds == 52.4


def test_page_parser_reads_embedded_json_and_highest_bitrate():
    payload = (FIXTURES / "douyin_page.json").read_text(encoding="utf-8")
    page = f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{payload}</script>'

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    parser = DouyinPageParser(httpx.Client(transport=httpx.MockTransport(handler)))
    media = parser.resolve(TEST_URL, cookie=None)

    assert media.media_url.endswith("page-high.mp4")
    assert media.description == "页面内发布描述"
    assert media.duration_seconds == 52.4


def test_page_parser_maps_forbidden_response_to_cookie_required():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    parser = DouyinPageParser(httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(CookieRequired):
        parser.resolve(TEST_URL, cookie="sessionid=top-secret")
