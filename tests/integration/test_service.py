from __future__ import annotations

from video_recover.config import Settings
from video_recover.crypto import CookieVault
from video_recover.parsers import ResolvedMedia
from video_recover.repository import Repository
from video_recover.service import VideoService

TEST_URL = "https://www.douyin.com/video/7662212894569811235"
MEDIA = ResolvedMedia(
    aweme_id="7662212894569811235",
    canonical_url=TEST_URL,
    media_url="https://v3-dy-o-abtest.zjcdn.com/video.mp4",
    description="发布描述",
    author="作者",
    duration_seconds=52,
    cover_url=None,
    request_headers={},
)


class StubParser:
    def resolve(self, url, *, cookie):
        assert url == TEST_URL
        return MEDIA


def make_service(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_directories()
    repository = Repository(settings.database_path)
    return VideoService(
        settings=settings,
        repository=repository,
        parser=StubParser(),
        cookie_vault=CookieVault(settings.secret_key_path),
    )


def test_submit_deduplicates_same_video(tmp_path):
    service = make_service(tmp_path)

    first, first_created = service.submit(TEST_URL, source="web")
    second, second_created = service.submit(TEST_URL, source="mcp")

    assert first_created is True
    assert second_created is False
    assert first.id == second.id


def test_cookie_round_trip_never_stores_plain_text(tmp_path):
    service = make_service(tmp_path)
    service.save_cookie("sessionid=top-secret")

    stored = service.repository.get_setting("douyin_cookie")
    assert stored is not None
    assert "top-secret" not in stored
    assert service.get_cookie() == "sessionid=top-secret"

