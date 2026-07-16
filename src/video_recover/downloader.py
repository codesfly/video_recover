from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from video_recover.errors import (
    DownloadFailed,
    DownloadTooLarge,
    InsufficientStorage,
    UnsafeMediaUrl,
    UserFacingError,
)
from video_recover.parsers import ResolvedMedia

TRUSTED_MEDIA_SUFFIXES = (
    "douyin.com",
    "douyinvod.com",
    "douyinpic.com",
    "zjcdn.com",
    "bytecdn.cn",
    "bytedance.com",
)


def _trusted_media_url(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".").lower()
    if parts.scheme != "https" or parts.username or parts.password or parts.fragment:
        return False
    if parts.port not in (None, 443):
        return False
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in TRUSTED_MEDIA_SUFFIXES)


def _client_context(
    client: httpx.Client | None,
) -> AbstractContextManager[httpx.Client]:
    if client is not None:
        return nullcontext(client)
    return httpx.Client(timeout=httpx.Timeout(60, connect=20))


def _iter_bytes(response: httpx.Response) -> Iterator[bytes]:
    yield from response.iter_bytes(chunk_size=1024 * 1024)


def download_file(
    media: ResolvedMedia,
    target: Path,
    *,
    client: httpx.Client | None = None,
    max_bytes: int = 2 * 1024 * 1024 * 1024,
    minimum_free_bytes: int = 512 * 1024 * 1024,
    progress: Callable[[int, int | None], None] | None = None,
) -> Path:
    if not _trusted_media_url(media.media_url):
        raise UnsafeMediaUrl()
    target.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(target.parent).free < minimum_free_bytes:
        raise InsufficientStorage()

    part_path = target.with_suffix(target.suffix + ".part")
    existing_size = part_path.stat().st_size if part_path.exists() else 0
    headers = dict(media.request_headers)
    if existing_size:
        headers["Range"] = f"bytes={existing_size}-"

    try:
        with (
            _client_context(client) as active_client,
            active_client.stream("GET", media.media_url, headers=headers) as response,
        ):
            response.raise_for_status()
            append = existing_size > 0 and response.status_code == 206
            written = existing_size if append else 0
            content_length = response.headers.get("content-length")
            remaining = int(content_length) if content_length and content_length.isdigit() else None
            expected_total = written + remaining if remaining is not None else None
            if expected_total is not None and expected_total > max_bytes:
                part_path.unlink(missing_ok=True)
                raise DownloadTooLarge()

            mode = "ab" if append else "wb"
            with part_path.open(mode) as output:
                for chunk in _iter_bytes(response):
                    written += len(chunk)
                    if written > max_bytes:
                        part_path.unlink(missing_ok=True)
                        raise DownloadTooLarge()
                    output.write(chunk)
                    if progress is not None:
                        progress(written, expected_total)
                output.flush()
                os.fsync(output.fileno())
    except UserFacingError:
        raise
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise DownloadFailed() from exc

    part_path.replace(target)
    return target
