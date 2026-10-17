from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from video_recover.comments import (
    CommentPage,
    harvest_comments,
    map_comment,
    render_markdown,
)
from video_recover.errors import (
    CommentFetchStalled,
    CommentRateLimited,
    CommentSignMissing,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "douyin_comment_page.json"


def fixture_page(**overrides) -> CommentPage:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw.update(overrides)
    return CommentPage.from_js(raw, signed=overrides.pop("signed", True))


class ScriptedSource:
    def __init__(self, pages):
        self.pages = list(pages)
        self.requests: list[tuple[int, int]] = []

    def fetch_page(self, cursor: int, count: int) -> CommentPage:
        self.requests.append((cursor, count))
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def page_of(cids, *, cursor, has_more=True, **overrides) -> CommentPage:
    fields = {
        "http_status": 200, "status_code": 0, "signed": True, "has_more": has_more,
        "cursor": cursor, "total": 999, "parse_error": False,
        "comments": tuple({"cid": c, "text": f"评论{c}", "digg_count": int(c)} for c in cids),
    }
    fields.update(overrides)
    return CommentPage(**fields)


def test_map_comment_projects_safe_fields_only() -> None:
    comment = map_comment(
        {"cid": "1", "text": "好", "digg_count": 3, "reply_comment_total": 2,
         "create_time": 1791029240, "ip_label": "广东", "nickname": "用户A"}
    )
    assert comment is not None
    assert comment.reply_count == 2
    assert comment.created_at == datetime.fromtimestamp(1791029240, UTC)
    assert map_comment({"text": "无 cid"}) is None


def test_harvest_dedups_by_cid_and_stops_on_has_more_false() -> None:
    source = ScriptedSource([
        page_of(["1", "2"], cursor=20),
        page_of(["2", "3"], cursor=40, has_more=False),
    ])
    harvest = harvest_comments(source, max_comments=200, deadline_seconds=60)
    assert [c.cid for c in harvest.comments] == ["1", "2", "3"]
    assert (harvest.pages, harvest.has_more_remaining, harvest.stop_error) == (2, False, None)
    assert source.requests == [(0, 20), (20, 20)]


def test_harvest_stops_at_max_and_reports_remaining() -> None:
    source = ScriptedSource([page_of(["1", "2", "3"], cursor=20)])
    harvest = harvest_comments(source, max_comments=2, deadline_seconds=60)
    assert [c.cid for c in harvest.comments] == ["1", "2"]
    assert harvest.has_more_remaining is True
    assert harvest.stop_error is None


@pytest.mark.parametrize(
    ("page", "error"),
    [
        (page_of(["9"], cursor=40, signed=False), CommentSignMissing),
        (page_of(["9"], cursor=40, parse_error=True), CommentSignMissing),
        (page_of(["9"], cursor=40, status_code=8), CommentRateLimited),
        (page_of(["9"], cursor=40, http_status=429), CommentRateLimited),
    ],
)
def test_bad_page_keeps_earlier_comments_and_records_error(page, error) -> None:
    source = ScriptedSource([page_of(["1"], cursor=20), page])
    harvest = harvest_comments(source, max_comments=200, deadline_seconds=60)
    assert [c.cid for c in harvest.comments] == ["1"]
    assert isinstance(harvest.stop_error, error)


def test_stalled_page_and_non_advancing_cursor_stop_the_loop() -> None:
    stalled = harvest_comments(
        ScriptedSource([page_of(["1"], cursor=20), CommentFetchStalled()]),
        max_comments=200, deadline_seconds=60,
    )
    assert isinstance(stalled.stop_error, CommentFetchStalled)

    stuck = harvest_comments(
        ScriptedSource([page_of(["1"], cursor=20), page_of(["2"], cursor=20)]),
        max_comments=200, deadline_seconds=60,
    )
    assert isinstance(stuck.stop_error, CommentFetchStalled)
    assert [c.cid for c in stuck.comments] == ["1", "2"]


def test_deadline_stops_before_next_request() -> None:
    ticks = iter([0.0, 0.0, 100.0])
    source = ScriptedSource([page_of(["1"], cursor=20), page_of(["2"], cursor=40)])
    harvest = harvest_comments(
        source, max_comments=200, deadline_seconds=60, clock=lambda: next(ticks),
    )
    assert [c.cid for c in harvest.comments] == ["1"]
    assert isinstance(harvest.stop_error, CommentFetchStalled)


def test_empty_video_is_a_clean_completion() -> None:
    harvest = harvest_comments(
        ScriptedSource([page_of([], cursor=0, has_more=False)]),
        max_comments=200, deadline_seconds=60,
    )
    assert (harvest.comments, harvest.stop_error) == ((), None)


def test_fixture_page_round_trip_and_markdown_sorted_by_likes() -> None:
    harvest = harvest_comments(
        ScriptedSource([fixture_page(has_more=False)]), max_comments=200, deadline_seconds=60,
    )
    assert len(harvest.comments) == 2
    markdown = render_markdown(
        aweme_id="7692767036498596273",
        fetched_at=datetime(2026, 10, 18, 3, 15, tzinfo=UTC),
        harvest=harvest,
    )
    lines = markdown.splitlines()
    assert lines[0] == "# 评论快照 7692767036498596273"
    assert "共 2 条，已拉全" in markdown
    first, second = [line for line in lines if line.startswith("- 👍")]
    assert first.startswith("- 👍617 💬136")
    assert "我今天生日 一个人在加拿大" in second  # newlines flattened
