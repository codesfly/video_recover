from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from video_recover.errors import (
    CommentFetchStalled,
    CommentRateLimited,
    CommentSignMissing,
    CommentsUnavailable,
    UserFacingError,
)

PAGE_SIZE = 20
COMMENT_LIST_PATH = "/aweme/v1/web/comment/list/"
SIGNATURE_PARAMS = frozenset({"a_bogus", "msToken", "X-Bogus"})
COMMENT_BLOCKED_RESOURCES = frozenset({"font", "media"})

# Projects only safe fields inside the page so uid/sec_uid/avatar never reach Python.
# Promise.race is mandatory: the page SDK can hold requests forever and AbortController
# does not release them (see docs/superpowers/spikes/2026-10-18-anonymous-comments-spike.md).
FETCH_PAGE_JS = """
async ([url, timeoutMs]) => {
  const timeout = new Promise((resolve) => {
    setTimeout(() => resolve({ timed_out: true }), timeoutMs);
  });
  const request = (async () => {
    const response = await fetch(url, { credentials: "include" });
    const text = await response.text();
    let body;
    try {
      body = JSON.parse(text);
    } catch (error) {
      return { http: response.status, parse_error: true };
    }
    return {
      http: response.status,
      status_code: body.status_code ?? null,
      has_more: Boolean(body.has_more),
      cursor: body.cursor ?? null,
      total: body.total ?? null,
      comments: (body.comments || []).map((c) => ({
        cid: c.cid, text: c.text, digg_count: c.digg_count,
        reply_comment_total: c.reply_comment_total, create_time: c.create_time,
        ip_label: c.ip_label, nickname: c.user ? c.user.nickname : null,
      })),
    };
  })();
  try { return await Promise.race([request, timeout]); }
  catch (error) { return { fetch_error: String(error) }; }
}
"""


@dataclass(frozen=True, slots=True)
class Comment:
    cid: str
    text: str
    digg_count: int
    reply_count: int
    created_at: datetime | None
    ip_label: str | None
    nickname: str | None


@dataclass(frozen=True, slots=True)
class CommentPage:
    http_status: int
    status_code: int | None
    signed: bool
    has_more: bool
    cursor: int | None
    total: int | None
    comments: Sequence[Mapping[str, Any]]
    parse_error: bool = False

    @classmethod
    def from_js(cls, raw: Mapping[str, Any], *, signed: bool) -> CommentPage:
        return cls(
            http_status=int(raw.get("http", -1)),
            status_code=raw.get("status_code"),
            signed=signed,
            has_more=bool(raw.get("has_more")),
            cursor=raw.get("cursor"),
            total=raw.get("total"),
            comments=tuple(raw.get("comments") or ()),
            parse_error=bool(raw.get("parse_error")),
        )


class CommentPageSource(Protocol):
    def fetch_page(self, cursor: int, count: int) -> CommentPage: ...


@dataclass(frozen=True, slots=True)
class CommentHarvest:
    comments: tuple[Comment, ...]
    pages: int
    has_more_remaining: bool
    total_reported: int | None
    stop_error: UserFacingError | None


class CommentFetcher(Protocol):
    def fetch(
        self, canonical_url: str, *, max_comments: int, deadline_seconds: float
    ) -> CommentHarvest: ...


def map_comment(raw: Mapping[str, Any]) -> Comment | None:
    cid = raw.get("cid")
    if not cid:
        return None
    created = raw.get("create_time")
    return Comment(
        cid=str(cid),
        text=str(raw.get("text") or ""),
        digg_count=int(raw.get("digg_count") or 0),
        reply_count=int(raw.get("reply_comment_total") or 0),
        created_at=datetime.fromtimestamp(int(created), UTC) if created else None,
        ip_label=raw.get("ip_label") or None,
        nickname=raw.get("nickname") or None,
    )


def classify_page(page: CommentPage) -> UserFacingError | None:
    if page.parse_error or not page.signed:
        return CommentSignMissing()
    if page.http_status != 200 or page.status_code not in (0, None):
        return CommentRateLimited()
    return None


def harvest_comments(
    source: CommentPageSource,
    *,
    max_comments: int,
    deadline_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    pause: Callable[[], None] = lambda: None,
) -> CommentHarvest:
    started = clock()
    seen: dict[str, Comment] = {}
    cursor, pages, has_more, total = 0, 0, True, None
    error: UserFacingError | None = None
    while len(seen) < max_comments:
        if clock() - started > deadline_seconds:
            error = CommentFetchStalled("评论拉取超过单任务时长上限")
            break
        try:
            page = source.fetch_page(cursor, PAGE_SIZE)
        except CommentFetchStalled as exc:
            error = exc
            break
        pages += 1
        error = classify_page(page)
        if error is not None:
            break
        total = page.total if page.total is not None else total
        for raw in page.comments:
            comment = map_comment(raw)
            if comment is not None and comment.cid not in seen:
                seen[comment.cid] = comment
        has_more = page.has_more
        if not has_more:
            break
        if page.cursor is None or page.cursor <= cursor:
            error = CommentFetchStalled("评论翻页没有前进")
            break
        cursor = page.cursor
        pause()
    return CommentHarvest(
        comments=tuple(seen.values())[:max_comments],
        pages=pages,
        has_more_remaining=has_more,
        total_reported=total,
        stop_error=error,
    )


def _flat(text: str) -> str:
    return " ".join(text.split())


def render_markdown(*, aweme_id: str, fetched_at: datetime, harvest: CommentHarvest) -> str:
    state = "未拉全" if harvest.has_more_remaining or harvest.stop_error else "已拉全"
    lines = [
        f"# 评论快照 {aweme_id}",
        "",
        f"拉取时间：{fetched_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC · "
        f"共 {len(harvest.comments)} 条，{state}",
        "",
    ]
    ordered = sorted(
        harvest.comments,
        key=lambda c: (-c.digg_count, c.created_at or datetime.min.replace(tzinfo=UTC)),
    )
    for c in ordered:
        date = f"{c.created_at:%m-%d}" if c.created_at else "—"
        lines.append(
            f"- 👍{c.digg_count} 💬{c.reply_count} · {date} · {c.ip_label or '—'} · {_flat(c.text)}"
        )
    return "\n".join(lines) + "\n"


def comment_list_url(template_url: str, *, cursor: int, count: int) -> str:
    parts = urlsplit(template_url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in SIGNATURE_PARAMS and key not in {"cursor", "count"}
    ]
    query += [("cursor", str(cursor)), ("count", str(count))]
    return urlunsplit(parts._replace(query=urlencode(query)))


class BrowserCommentSource:
    def __init__(
        self,
        page: Any,
        *,
        page_timeout_seconds: float,
        first_request_timeout_seconds: float,
    ) -> None:
        self.page = page
        self.page_timeout_ms = int(page_timeout_seconds * 1000)
        self.first_request_timeout_seconds = first_request_timeout_seconds
        self.template_url: str | None = None
        self.last_signed = False
        page.on("request", self._observe)

    def _observe(self, request: Any) -> None:
        url = request.url
        if COMMENT_LIST_PATH in url and "/reply/" not in url:
            self.template_url = self.template_url or url
            self.last_signed = "a_bogus=" in url

    def open(self, canonical_url: str) -> None:
        self.page.goto(canonical_url, wait_until="domcontentloaded", timeout=45_000)
        deadline = time.monotonic() + self.first_request_timeout_seconds
        while self.template_url is None and time.monotonic() < deadline:
            self.page.wait_for_timeout(250)
        if self.template_url is None:
            raise CommentsUnavailable()

    def fetch_page(self, cursor: int, count: int) -> CommentPage:
        if self.template_url is None:
            raise CommentsUnavailable()
        self.last_signed = False
        url = comment_list_url(self.template_url, cursor=cursor, count=count)
        raw = self.page.evaluate(FETCH_PAGE_JS, [url, self.page_timeout_ms])
        if raw.get("timed_out") or raw.get("fetch_error"):
            raise CommentFetchStalled()
        return CommentPage.from_js(raw, signed=self.last_signed)


class AnonymousCommentFetcher:
    def __init__(
        self,
        *,
        page_timeout_seconds: float = 20,
        first_request_timeout_seconds: float = 15,
        page_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.page_timeout_seconds = page_timeout_seconds
        self.first_request_timeout_seconds = first_request_timeout_seconds
        if page_factory is None:
            from video_recover.browser_parser import anonymous_page

            page_factory = anonymous_page
        self.page_factory = page_factory

    def fetch(
        self, canonical_url: str, *, max_comments: int, deadline_seconds: float
    ) -> CommentHarvest:
        with self.page_factory(blocked_resource_types=COMMENT_BLOCKED_RESOURCES) as page:
            source = BrowserCommentSource(
                page,
                page_timeout_seconds=self.page_timeout_seconds,
                first_request_timeout_seconds=self.first_request_timeout_seconds,
            )
            source.open(canonical_url)
            return harvest_comments(
                source,
                max_comments=max_comments,
                deadline_seconds=deadline_seconds,
                pause=lambda: page.wait_for_timeout(random.randint(800, 1600)),
            )
