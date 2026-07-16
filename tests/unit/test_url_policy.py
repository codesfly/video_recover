import httpx
import pytest

from video_recover.errors import UnsafeUrl
from video_recover.url_policy import normalize_douyin_url, resolve_douyin_url

VIDEO_ID = "7662212894569811235"
CANONICAL = f"https://www.douyin.com/video/{VIDEO_ID}"


@pytest.mark.parametrize(
    "url",
    [
        CANONICAL,
        f"https://douyin.com/video/{VIDEO_ID}?previous_page=web_code_link",
        f"复制这条链接 {CANONICAL} 打开抖音观看",
    ],
)
def test_normalizes_video_urls(url):
    normalized = normalize_douyin_url(url)
    assert normalized.canonical_url == CANONICAL
    assert normalized.aweme_id == VIDEO_ID


@pytest.mark.parametrize(
    "url",
    [
        f"http://www.douyin.com/video/{VIDEO_ID}",
        f"https://douyin.com.evil.example/video/{VIDEO_ID}",
        "file:///etc/passwd",
        "https://www.douyin.com/video/not-a-number",
    ],
)
def test_rejects_unsafe_urls(url):
    with pytest.raises(UnsafeUrl):
        normalize_douyin_url(url)


def test_resolves_short_link_and_validates_each_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "v.douyin.com"
        return httpx.Response(302, headers={"location": CANONICAL})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        normalized = resolve_douyin_url("https://v.douyin.com/abc123/", client=client)

    assert normalized.canonical_url == CANONICAL


def test_rejects_short_link_redirect_to_untrusted_host_before_requesting_it():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/video/1"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(UnsafeUrl),
    ):
        resolve_douyin_url("https://v.douyin.com/abc123/", client=client)

    assert requests == ["https://v.douyin.com/abc123/"]
