from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from video_recover.errors import UnsafeUrl

ALLOWED_HOSTS = frozenset(
    {
        "douyin.com",
        "www.douyin.com",
        "v.douyin.com",
        "iesdouyin.com",
        "v.iesdouyin.com",
    }
)
SHORT_HOSTS = frozenset({"v.douyin.com", "v.iesdouyin.com"})
URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
VIDEO_PATH_PATTERN = re.compile(r"^/video/(?P<aweme_id>\d{10,30})/?$")
TRAILING_PUNCTUATION = "，。！？；：,.!?;:）)]}〉》\"'"


@dataclass(frozen=True, slots=True)
class NormalizedDouyinUrl:
    canonical_url: str
    aweme_id: str


def _extract_candidate(value: str) -> str:
    match = URL_PATTERN.search(value.strip())
    if not match:
        raise UnsafeUrl("未找到抖音 HTTPS 视频链接")
    return match.group(0).rstrip(TRAILING_PUNCTUATION)


def _validated_parts(value: str):
    parts = urlsplit(value)
    host = (parts.hostname or "").rstrip(".").lower()
    if parts.scheme.lower() != "https":
        raise UnsafeUrl("仅支持 HTTPS 抖音链接")
    if parts.username or parts.password or parts.fragment:
        raise UnsafeUrl()
    if parts.port not in (None, 443):
        raise UnsafeUrl()
    if host not in ALLOWED_HOSTS:
        raise UnsafeUrl("链接域名不在抖音白名单中")
    return parts, host


def normalize_douyin_url(value: str) -> NormalizedDouyinUrl:
    candidate = _extract_candidate(value)
    parts, host = _validated_parts(candidate)
    if host not in {"douyin.com", "www.douyin.com"}:
        raise UnsafeUrl("短链接需要先安全解析")
    match = VIDEO_PATH_PATTERN.fullmatch(parts.path)
    if not match:
        raise UnsafeUrl("链接不是有效的抖音视频地址")
    aweme_id = match.group("aweme_id")
    return NormalizedDouyinUrl(
        canonical_url=f"https://www.douyin.com/video/{aweme_id}",
        aweme_id=aweme_id,
    )


def resolve_douyin_url(
    value: str,
    *,
    client: httpx.Client,
    max_redirects: int = 5,
) -> NormalizedDouyinUrl:
    current = _extract_candidate(value)
    parts, host = _validated_parts(current)
    if host not in SHORT_HOSTS:
        return normalize_douyin_url(current)

    current = urlunsplit(("https", host, parts.path or "/", parts.query, ""))
    for _ in range(max_redirects):
        response = client.get(current, follow_redirects=False)
        if response.status_code not in {301, 302, 303, 307, 308}:
            break
        location = response.headers.get("location")
        if not location:
            raise UnsafeUrl("抖音短链接缺少跳转目标")
        target = urljoin(current, location)
        _validated_parts(target)
        try:
            return normalize_douyin_url(target)
        except UnsafeUrl:
            current = target

    raise UnsafeUrl("抖音短链接未跳转到有效视频")
