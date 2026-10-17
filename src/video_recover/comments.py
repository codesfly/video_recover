from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from video_recover.errors import (
    CommentFetchStalled,
    CommentRateLimited,
    CommentSignMissing,
    UserFacingError,
)

PAGE_SIZE = 20


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
