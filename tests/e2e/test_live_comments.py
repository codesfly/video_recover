from __future__ import annotations

import os

import pytest

from video_recover.comments import AnonymousCommentFetcher

pytestmark = pytest.mark.live

HOT_VIDEO = os.getenv(
    "VIDEO_RECOVER_COMMENT_URL", "https://www.douyin.com/video/7692767036498596273"
)


def test_anonymous_fetcher_reads_three_signed_pages() -> None:
    if os.getenv("VIDEO_RECOVER_LIVE") != "1":
        pytest.skip("set VIDEO_RECOVER_LIVE=1 to run the real comment acceptance test")

    harvest = AnonymousCommentFetcher().fetch(HOT_VIDEO, max_comments=60, deadline_seconds=120)

    assert harvest.stop_error is None, harvest.stop_error
    assert harvest.pages >= 3
    assert len(harvest.comments) >= 40
    assert len({comment.cid for comment in harvest.comments}) == len(harvest.comments)
