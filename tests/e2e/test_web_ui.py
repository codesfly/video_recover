from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import uvicorn

from video_recover.config import Settings
from video_recover.crypto import CookieVault
from video_recover.main import create_app
from video_recover.repository import Repository
from video_recover.service import VideoService

TEST_URL = "https://www.douyin.com/video/7662212894569811235"


class UnusedParser:
    def resolve(self, url: str, *, cookie: str | None):
        raise AssertionError("the UI test does not run background parsing")


def serve_test_app(tmp_path: Path) -> tuple[uvicorn.Server, threading.Thread]:
    settings = Settings(
        data_dir=tmp_path / "data",
        worker_token="test-worker-token-long-enough",
    )
    settings.ensure_directories()
    service = VideoService(
        settings=settings,
        repository=Repository(settings.database_path),
        parser=UnusedParser(),
        cookie_vault=CookieVault(settings.secret_key_path),
    )
    app = create_app(settings, service=service, start_runner=False)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started or not server.servers:
        server.should_exit = True
        thread.join(timeout=5)
        pytest.fail("test Web server did not start")
    return server, thread


def test_desktop_submit_and_mobile_layout_have_no_browser_errors(tmp_path: Path) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    server, thread = serve_test_app(tmp_path)
    sockets = next(iter(server.servers)).sockets
    port = sockets[0].getsockname()[1]
    browser = None
    try:
        with sync_api.sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(channel="chrome", headless=True)
            except sync_api.Error as exc:
                pytest.skip(f"Google Chrome is unavailable: {exc}")
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                console_errors: list[str] = []
                page.on(
                    "console",
                    lambda message: console_errors.append(message.text)
                    if message.type == "error"
                    else None,
                )
                page.on("pageerror", lambda error: console_errors.append(str(error)))

                page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
                assert page.get_by_role(
                    "heading",
                    name="收下一条视频，留住它的声音。",
                ).is_visible()
                page.get_by_label("抖音视频链接").fill(TEST_URL)
                page.get_by_role("button", name="开始归档").click()
                page.get_by_role(
                    "button",
                    name="01 待解析视频 7662212894569811235",
                ).wait_for()
                assert page.get_by_text("已入队，后台会自动处理。").is_visible()

                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate(
                    "document.documentElement.scrollWidth "
                    "=== document.documentElement.clientWidth"
                )
                cookie_input = page.get_by_label("Cookie")
                assert cookie_input.get_attribute("type") == "password"
                assert cookie_input.input_value() == ""
                assert console_errors == []
            finally:
                browser.close()
                browser = None
    finally:
        if browser is not None:
            browser.close()
        server.should_exit = True
        thread.join(timeout=10)
