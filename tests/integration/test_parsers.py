from __future__ import annotations

import importlib
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


def test_chain_reports_parser_change_when_anonymous_fallback_cannot_resolve():
    chain = ParserChain(
        [
            StubParser(error=CookieRequired()),
            StubParser(error=ParserChanged("匿名解析失败")),
        ]
    )

    with pytest.raises(ParserChanged, match="匿名解析失败"):
        chain.resolve(TEST_URL, cookie=None)


def test_anonymous_browser_parser_maps_detail_to_direct_cdn_without_cookie():
    browser_parser = importlib.import_module("video_recover.browser_parser")
    calls = []

    def load_detail(canonical_url: str, aweme_id: str):
        calls.append((canonical_url, aweme_id))
        return {
            "aweme_id": aweme_id,
            "desc": "匿名解析文案",
            "author": {"nickname": "匿名作者"},
            "duration": 52400,
            "video": {
                "cover": {"url_list": ["https://p3-sign.douyinpic.com/cover.jpeg"]},
                "bit_rate": [
                    {
                        "bit_rate": 800,
                        "play_addr": {
                            "url_list": ["https://v3-dy-o-abtest.zjcdn.com/low.mp4"]
                        },
                    },
                    {
                        "bit_rate": 1800,
                        "play_addr": {
                            "url_list": ["https://v26-web.douyinvod.com/high.mp4"]
                        },
                    },
                ],
            },
        }

    parser = browser_parser.AnonymousBrowserParser(detail_loader=load_detail)

    media = parser.resolve(TEST_URL, cookie="sessionid=must-not-be-forwarded")

    assert calls == [(TEST_URL, "7662212894569811235")]
    assert media.media_url == "https://v26-web.douyinvod.com/high.mp4"
    assert media.description == "匿名解析文案"
    assert media.author == "匿名作者"
    assert media.duration_seconds == 52.4
    assert "Cookie" not in media.request_headers


def test_browser_loader_blocks_heavy_assets_but_keeps_scripts_and_api_requests():
    browser_parser = importlib.import_module("video_recover.browser_parser")

    assert browser_parser.should_block_resource("image") is True
    assert browser_parser.should_block_resource("media") is True
    assert browser_parser.should_block_resource("font") is True
    assert browser_parser.should_block_resource("script") is False
    assert browser_parser.should_block_resource("xhr") is False


def test_browser_response_parser_skips_empty_challenge_response_then_accepts_detail():
    browser_parser = importlib.import_module("video_recover.browser_parser")

    class FakeResponse:
        status = 200

        def __init__(self, payload):
            self.url = (
                "https://www.douyin.com/aweme/v1/web/aweme/detail/"
                "?aweme_id=7662212894569811235"
            )
            self.payload = payload

        def json(self):
            if self.payload is None:
                raise json.JSONDecodeError("empty", "", 0)
            return self.payload

    empty = FakeResponse(None)
    valid = FakeResponse({"aweme_detail": {"aweme_id": "7662212894569811235"}})

    assert browser_parser.parse_detail_response(empty, "7662212894569811235") is None
    assert browser_parser.parse_detail_response(valid, "7662212894569811235") == {
        "aweme_id": "7662212894569811235"
    }


def test_anonymous_user_agent_matches_runtime_browser_without_headless_marker():
    browser_parser = importlib.import_module("video_recover.browser_parser")

    user_agent = browser_parser.anonymous_user_agent(
        "150.0.7871.124",
        system="Linux",
        machine="aarch64",
    )

    assert "X11; Linux aarch64" in user_agent
    assert "Chrome/150.0.7871.124" in user_agent
    assert "HeadlessChrome" not in user_agent


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
