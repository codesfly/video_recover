from __future__ import annotations

import os
import platform
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from playwright.sync_api import Response, Route, sync_playwright

from video_recover.errors import ParserChanged
from video_recover.parsers import DouyinPageParser, ResolvedMedia
from video_recover.url_policy import normalize_douyin_url

DetailLoader = Callable[[str, str], Mapping[str, Any]]
BLOCKED_RESOURCE_TYPES = frozenset({"font", "image", "media"})


def should_block_resource(resource_type: str) -> bool:
    return resource_type in BLOCKED_RESOURCE_TYPES


def _browser_executable() -> str | None:
    configured = os.getenv("VIDEO_RECOVER_BROWSER_EXECUTABLE")
    if configured:
        return configured
    chromium = Path("/usr/bin/chromium")
    return str(chromium) if chromium.is_file() else None


def anonymous_user_agent(browser_version: str, *, system: str, machine: str) -> str:
    if system == "Darwin":
        platform_token = "Macintosh; Intel Mac OS X 10_15_7"
    elif system == "Windows":
        platform_token = "Windows NT 10.0; Win64; x64"
    else:
        platform_token = f"X11; Linux {machine or 'x86_64'}"
    return (
        f"Mozilla/5.0 ({platform_token}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{browser_version} Safari/537.36"
    )


def _is_detail_response(response: Response, aweme_id: str) -> bool:
    return (
        response.status == 200
        and "/aweme/v1/web/aweme/detail/" in response.url
        and f"aweme_id={aweme_id}" in response.url
    )


def parse_detail_response(response: Response, aweme_id: str) -> Mapping[str, Any] | None:
    if not _is_detail_response(response, aweme_id):
        return None
    try:
        payload = response.json()
    except Exception:
        return None
    detail = payload.get("aweme_detail") if isinstance(payload, dict) else None
    return detail if isinstance(detail, dict) else None


def load_detail_with_anonymous_browser(canonical_url: str, aweme_id: str) -> Mapping[str, Any]:
    with sync_playwright() as playwright:
        launch_options: dict[str, Any] = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        }
        executable = _browser_executable()
        if executable:
            launch_options["executable_path"] = executable
        else:
            launch_options["channel"] = "chrome"

        browser = playwright.chromium.launch(**launch_options)
        try:
            user_agent = anonymous_user_agent(
                browser.version,
                system=platform.system(),
                machine=platform.machine(),
            )
            context = browser.new_context(
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                user_agent=user_agent,
                viewport={"width": 1280, "height": 720},
            )
            page = context.new_page()

            def route_request(route: Route) -> None:
                if should_block_resource(route.request.resource_type):
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", route_request)
            detail: Mapping[str, Any] | None = None

            def capture_detail(response: Response) -> None:
                nonlocal detail
                detail = detail or parse_detail_response(response, aweme_id)

            page.on("response", capture_detail)
            page.goto(canonical_url, wait_until="domcontentloaded", timeout=45_000)
            deadline = time.monotonic() + 45
            while detail is None and time.monotonic() < deadline:
                page.wait_for_timeout(250)
            if detail is None:
                raise ParserChanged("匿名浏览器没有返回视频详情")
            return detail
        finally:
            browser.close()


class AnonymousBrowserParser:
    def __init__(self, detail_loader: DetailLoader = load_detail_with_anonymous_browser) -> None:
        self.detail_loader = detail_loader

    def resolve(self, url: str, *, cookie: str | None) -> ResolvedMedia:
        normalized = normalize_douyin_url(url)
        try:
            detail = self.detail_loader(normalized.canonical_url, normalized.aweme_id)
        except ParserChanged:
            raise
        except Exception:
            raise ParserChanged("匿名浏览器未能完成抖音页面校验") from None
        return DouyinPageParser._map_detail(detail, normalized, cookie=None)
