from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import unquote

import httpx
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from video_recover.errors import CookieRequired, ParserChanged, UserFacingError
from video_recover.url_policy import NormalizedDouyinUrl, normalize_douyin_url

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
SCRIPT_PATTERN = re.compile(
    r'<script[^>]+id=["\'](?P<id>RENDER_DATA|__UNIVERSAL_DATA_FOR_REHYDRATION__)["\'][^>]*>'
    r"(?P<body>.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class ResolvedMedia:
    aweme_id: str
    canonical_url: str
    media_url: str
    description: str
    author: str
    duration_seconds: float | None
    cover_url: str | None
    request_headers: Mapping[str, str]


class Parser(Protocol):
    def resolve(self, url: str, *, cookie: str | None) -> ResolvedMedia: ...


class _QuietLogger:
    def debug(self, _message: str) -> None:
        pass

    def warning(self, _message: str) -> None:
        pass

    def error(self, _message: str) -> None:
        pass


class ParserChain:
    def __init__(self, parsers: Sequence[Parser]) -> None:
        if not parsers:
            raise ValueError("parser chain cannot be empty")
        self.parsers = tuple(parsers)

    def resolve(self, url: str, *, cookie: str | None) -> ResolvedMedia:
        errors: list[UserFacingError] = []
        for parser in self.parsers:
            try:
                return parser.resolve(url, cookie=cookie)
            except UserFacingError as exc:
                errors.append(exc)
            except Exception:
                errors.append(ParserChanged())
        parser_error = next(
            (error for error in reversed(errors) if not isinstance(error, CookieRequired)),
            None,
        )
        if parser_error is not None:
            raise parser_error
        raise errors[-1] if errors else ParserChanged()


class YtDlpParser:
    def resolve(self, url: str, *, cookie: str | None) -> ResolvedMedia:
        normalized = normalize_douyin_url(url)
        headers = {"User-Agent": USER_AGENT, "Referer": "https://www.douyin.com/"}
        if cookie:
            headers["Cookie"] = cookie
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "best[ext=mp4]/best",
            "http_headers": headers,
            "logger": _QuietLogger(),
        }
        try:
            with YoutubeDL(options) as downloader:
                info = downloader.extract_info(normalized.canonical_url, download=False)
        except DownloadError as exc:
            message = str(exc).lower()
            if any(marker in message for marker in ("cookie", "login", "403", "forbidden")):
                raise CookieRequired() from None
            raise ParserChanged() from None
        if not isinstance(info, dict):
            raise ParserChanged()
        return self.map_info(info, normalized, cookie=cookie)

    @staticmethod
    def map_info(
        info: Mapping[str, Any],
        normalized: NormalizedDouyinUrl,
        *,
        cookie: str | None,
    ) -> ResolvedMedia:
        formats = [
            item
            for item in info.get("formats") or []
            if isinstance(item, dict) and item.get("url") and item.get("ext") in {None, "mp4"}
        ]
        selected = max(
            formats,
            key=lambda item: (
                int(item.get("height") or 0),
                float(item.get("tbr") or 0),
            ),
            default=info if info.get("url") else None,
        )
        if not selected or not selected.get("url"):
            raise ParserChanged("解析结果中没有可下载的视频地址")
        request_headers = {
            "User-Agent": USER_AGENT,
            "Referer": normalized.canonical_url,
        }
        if cookie:
            request_headers["Cookie"] = cookie
        duration = info.get("duration")
        return ResolvedMedia(
            aweme_id=str(info.get("id") or normalized.aweme_id),
            canonical_url=normalized.canonical_url,
            media_url=str(selected["url"]),
            description=str(info.get("description") or info.get("title") or ""),
            author=str(info.get("uploader") or info.get("creator") or "未知作者"),
            duration_seconds=float(duration) if duration is not None else None,
            cover_url=str(info["thumbnail"]) if info.get("thumbnail") else None,
            request_headers=request_headers,
        )


class DouyinPageParser:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=30)

    def resolve(self, url: str, *, cookie: str | None) -> ResolvedMedia:
        normalized = normalize_douyin_url(url)
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": USER_AGENT,
            "Referer": "https://www.douyin.com/",
        }
        if cookie:
            headers["Cookie"] = cookie
        try:
            response = self.client.get(
                normalized.canonical_url,
                headers=headers,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise ParserChanged("无法读取抖音视频页面") from None
        if response.status_code in {401, 403, 412, 429}:
            raise CookieRequired()
        if response.status_code != 200:
            raise ParserChanged("抖音视频页面返回异常状态")
        return self.map_page(response.text, normalized, cookie=cookie)

    @classmethod
    def map_page(
        cls,
        page: str,
        normalized: NormalizedDouyinUrl,
        *,
        cookie: str | None,
    ) -> ResolvedMedia:
        for match in SCRIPT_PATTERN.finditer(page):
            body = html.unescape(match.group("body").strip())
            if match.group("id").upper() == "RENDER_DATA":
                body = unquote(body)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                continue
            detail = cls._find_aweme(data, normalized.aweme_id)
            if detail is not None:
                return cls._map_detail(detail, normalized, cookie=cookie)
        raise ParserChanged("抖音页面中没有找到视频数据")

    @classmethod
    def _find_aweme(cls, value: object, aweme_id: str) -> Mapping[str, Any] | None:
        if isinstance(value, dict):
            identifier = value.get("aweme_id") or value.get("awemeId")
            if str(identifier) == aweme_id and isinstance(value.get("video"), dict):
                return value
            for child in value.values():
                found = cls._find_aweme(child, aweme_id)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._find_aweme(child, aweme_id)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _url_list(value: object) -> list[str]:
        if not isinstance(value, dict):
            return []
        urls = value.get("url_list") or value.get("urlList") or []
        return [str(url) for url in urls if isinstance(url, str) and url.startswith("https://")]

    @classmethod
    def _map_detail(
        cls,
        detail: Mapping[str, Any],
        normalized: NormalizedDouyinUrl,
        *,
        cookie: str | None,
    ) -> ResolvedMedia:
        video = detail.get("video")
        if not isinstance(video, dict):
            raise ParserChanged()
        candidates: list[tuple[int, str]] = []
        for bitrate in video.get("bit_rate") or video.get("bitRate") or []:
            if not isinstance(bitrate, dict):
                continue
            urls = cls._url_list(bitrate.get("play_addr") or bitrate.get("playAddr"))
            if urls:
                score = int(bitrate.get("bit_rate") or bitrate.get("bitRate") or 0)
                candidates.append((score, urls[0]))
        root_urls = cls._url_list(video.get("play_addr") or video.get("playAddr"))
        candidates.extend((0, media_url) for media_url in root_urls)
        if not candidates:
            raise ParserChanged("页面视频数据中没有可下载地址")
        media_url = max(candidates, key=lambda item: item[0])[1]
        author = detail.get("author") if isinstance(detail.get("author"), dict) else {}
        cover_urls = cls._url_list(video.get("cover"))
        duration = detail.get("duration") or video.get("duration")
        duration_seconds = float(duration) if duration is not None else None
        if duration_seconds and duration_seconds > 1000:
            duration_seconds /= 1000
        request_headers = {"User-Agent": USER_AGENT, "Referer": normalized.canonical_url}
        if cookie:
            request_headers["Cookie"] = cookie
        return ResolvedMedia(
            aweme_id=normalized.aweme_id,
            canonical_url=normalized.canonical_url,
            media_url=media_url,
            description=str(detail.get("desc") or detail.get("description") or ""),
            author=str(author.get("nickname") or author.get("name") or "未知作者"),
            duration_seconds=duration_seconds,
            cover_url=cover_urls[0] if cover_urls else None,
            request_headers=request_headers,
        )
