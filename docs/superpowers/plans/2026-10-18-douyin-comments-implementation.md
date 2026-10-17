# Douyin Comment Snapshots Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users and agents capture timestamped snapshots of a video's top-level Douyin comments anonymously, store them next to the archived video, and read them through REST, MCP, and the Web workspace.

**Architecture:** A new `comments.py` module splits comment capture into a pure harvesting loop (dedup, stop conditions, error classification) and a thin Playwright page source that calls `comment/list` from inside the video page so the page's own security SDK signs each request. Comment jobs live in their own `comment_jobs` table, are independent of the video `TaskStatus` machine, and run in `JobRunner` after the download pipeline. Snapshots are JSON files under `downloads/<aweme_id>/comments/` plus a regenerated `latest.md`.

**Tech Stack:** Python 3.12, FastAPI, FastMCP, SQLite, Playwright (system Chromium in Docker), vanilla JavaScript, pytest, ruff.

**Spec:** [`docs/superpowers/specs/2026-10-18-douyin-comments-design.md`](../specs/2026-10-18-douyin-comments-design.md)
**Evidence:** [`docs/superpowers/spikes/2026-10-18-anonymous-comments-spike.md`](../spikes/2026-10-18-anonymous-comments-spike.md)

---

## Baseline (2026-10-18)

- `.venv/bin/python -m pytest -m 'not live and not mac_live' -q` → 101 passed, **1 failed**: `tests/e2e/test_install_scripts.py::test_compose_binds_only_loopback_and_persists_data`. The failure comes from the owner's uncommitted `compose.yaml` edit (limits 4 CPU / 8G → 2 CPU / 4G). It is unrelated to this plan; do not touch `compose.yaml` or that test. Treat "only this one failure" as green.
- `.venv/bin/ruff check .` → clean (spike scripts carry `# ruff: noqa`).

Conventions for every task: when a step says "append" to a test or source file, merge the new imports into that file's top import block (ruff `E402` / `I001`). Run `.venv/bin/ruff check src tests` before each commit.

## File map

- `src/video_recover/domain.py`: `CommentJobStatus`, `CommentJob`.
- `src/video_recover/errors.py`: four comment error classes.
- `src/video_recover/comments.py` (new): `Comment`, `CommentPage`, `CommentHarvest`, `harvest_comments`, `render_markdown`, `comment_list_url`, `BrowserCommentSource`, `AnonymousCommentFetcher`.
- `src/video_recover/browser_parser.py`: extract `anonymous_page()` context manager shared by video parsing and comments.
- `src/video_recover/repository.py`: `comment_jobs` schema and job lifecycle methods.
- `src/video_recover/service.py`: request/process/read comment jobs, `submit(fetch_comments=)`, delete changes.
- `src/video_recover/runner.py`: comment jobs after the pipeline; startup recovery.
- `src/video_recover/config.py`: three `comments_*` settings.
- `src/video_recover/main.py`: wire `AnonymousCommentFetcher` into `build_service`.
- `src/video_recover/api.py`: four comment endpoints.
- `src/video_recover/mcp_server.py`: `fetch_comments`, `get_comments`, `submit_video(fetch_comments=)`.
- `src/video_recover/templates/index.html`, `static/app.js`, `static/app.css`: comment section.
- Tests: `tests/unit/test_comments.py` (new), `tests/integration/test_comment_jobs.py` (new), `tests/integration/test_runner.py`, `test_api.py`, `test_mcp.py`, `test_web.py`, `tests/e2e/test_live_comments.py` (new), `tests/fixtures/douyin_comment_page.json` (new).
- `README.md`: MCP tool list, REST endpoints, comment limitations.

---

### Task 1: Domain types and comment errors

**Files:**
- Modify: `src/video_recover/domain.py`
- Modify: `src/video_recover/errors.py`
- Test: `tests/unit/test_domain.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_domain.py`:

```python
from video_recover.domain import ACTIVE_COMMENT_STATUSES, CommentJobStatus
from video_recover.errors import (
    CommentFetchStalled,
    CommentRateLimited,
    CommentSignMissing,
    CommentsUnavailable,
)


def test_comment_job_statuses_and_active_set() -> None:
    assert {status.value for status in CommentJobStatus} == {
        "queued", "running", "completed", "partial", "failed",
    }
    assert ACTIVE_COMMENT_STATUSES == {CommentJobStatus.QUEUED, CommentJobStatus.RUNNING}


def test_comment_errors_have_stable_codes_and_retry_flags() -> None:
    assert (CommentsUnavailable().code, CommentsUnavailable().retryable) == (
        "comments_unavailable", False,
    )
    assert CommentSignMissing().code == "comment_sign_missing"
    assert CommentFetchStalled().code == "comment_fetch_stalled"
    assert CommentRateLimited().code == "comment_rate_limited"
    assert all(e().retryable for e in (CommentSignMissing, CommentFetchStalled, CommentRateLimited))
```

- [ ] **Step 2: Run it and confirm the import fails**

Run: `.venv/bin/python -m pytest tests/unit/test_domain.py -q`
Expected: `ImportError: cannot import name 'ACTIVE_COMMENT_STATUSES'`.

- [ ] **Step 3: Add the domain types**

Append to `src/video_recover/domain.py`:

```python
class CommentJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


ACTIVE_COMMENT_STATUSES = frozenset({CommentJobStatus.QUEUED, CommentJobStatus.RUNNING})


@dataclass(frozen=True, slots=True)
class CommentJob:
    id: str
    task_id: str
    status: CommentJobStatus
    max_comments: int
    fetched_count: int
    pages: int
    has_more_remaining: bool
    total_reported: int | None
    snapshot_name: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
```

- [ ] **Step 4: Add the errors**

Append to `src/video_recover/errors.py`:

```python
class CommentsUnavailable(UserFacingError):
    def __init__(self) -> None:
        super().__init__("comments_unavailable", "该视频没有可读取的评论区（可能已关闭评论或不可见）")


class CommentSignMissing(UserFacingError):
    def __init__(self) -> None:
        super().__init__(
            "comment_sign_missing",
            "抖音页面没有为评论请求签名，评论接口可能已变化",
            retryable=True,
        )


class CommentFetchStalled(UserFacingError):
    def __init__(self, message: str = "评论加载超时，请稍后重试") -> None:
        super().__init__("comment_fetch_stalled", message, retryable=True)


class CommentRateLimited(UserFacingError):
    def __init__(self) -> None:
        super().__init__("comment_rate_limited", "抖音暂时限制了评论读取，请稍后重试", retryable=True)
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_domain.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/video_recover/domain.py src/video_recover/errors.py tests/unit/test_domain.py
git commit -m "feat(comments): add comment job domain types and errors"
```

---

### Task 2: Pure harvesting loop and Markdown rendering

**Files:**
- Create: `src/video_recover/comments.py`
- Create: `tests/fixtures/douyin_comment_page.json`
- Create: `tests/unit/test_comments.py`

- [ ] **Step 1: Create the fixture**

`tests/fixtures/douyin_comment_page.json` holds the JS-projected shape (what `FETCH_PAGE_JS` in Task 3 returns), with nicknames replaced:

```json
{
  "http": 200,
  "status_code": 0,
  "has_more": true,
  "cursor": 20,
  "total": 1131,
  "comments": [
    {"cid": "7694475988264682277", "text": "这个是真的想学，求出教程[色]", "digg_count": 617,
     "reply_comment_total": 136, "create_time": 1791029240, "ip_label": "广东", "nickname": "用户A"},
    {"cid": "7694471438568620838", "text": "我今天生日\n一个人在加拿大", "digg_count": 376,
     "reply_comment_total": 6, "create_time": 1791030000, "ip_label": "海外", "nickname": "用户B"},
    {"cid": "7694475988264682277", "text": "重复条目", "digg_count": 1,
     "reply_comment_total": 0, "create_time": 1791029240, "ip_label": "广东", "nickname": "用户A"},
    {"text": "没有 cid 的脏数据"}
  ]
}
```

- [ ] **Step 2: Write the failing tests**

Create `tests/unit/test_comments.py`:

```python
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
```

- [ ] **Step 3: Run and confirm the module is missing**

Run: `.venv/bin/python -m pytest tests/unit/test_comments.py -q`
Expected: `ModuleNotFoundError: No module named 'video_recover.comments'`.

- [ ] **Step 4: Implement the pure part of `comments.py`**

```python
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
```

Notes for the implementer:
- `fixture_page` in the test pops `signed` before `CommentPage.from_js`; keep `from_js` ignoring unknown keys.
- Comment order in the harvest is first-seen API order; Markdown and readers sort on demand.

- [ ] **Step 5: Run the tests and ruff**

Run: `.venv/bin/python -m pytest tests/unit/test_comments.py -q && .venv/bin/ruff check src tests`
Expected: all pass, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add src/video_recover/comments.py tests/unit/test_comments.py tests/fixtures/douyin_comment_page.json
git commit -m "feat(comments): add pure comment harvesting loop and markdown rendering"
```

---

### Task 3: Shared anonymous page and the browser page source

**Files:**
- Modify: `src/video_recover/browser_parser.py`
- Modify: `src/video_recover/comments.py`
- Test: `tests/unit/test_comments.py`, existing `tests/integration/test_parsers.py`

- [ ] **Step 1: Write the failing URL test**

Append to `tests/unit/test_comments.py`:

```python
from urllib.parse import parse_qs, urlsplit

from video_recover.comments import comment_list_url


def test_comment_list_url_strips_signatures_and_sets_paging() -> None:
    template = (
        "https://www.douyin.com/aweme/v1/web/comment/list/?device_platform=webapp&aid=6383"
        "&aweme_id=7692767036498596273&cursor=0&count=5&msToken=abc&a_bogus=xyz&X-Bogus=q"
    )
    query = parse_qs(urlsplit(comment_list_url(template, cursor=40, count=20)).query)
    assert query["cursor"] == ["40"] and query["count"] == ["20"]
    assert query["aweme_id"] == ["7692767036498596273"]
    assert not {"a_bogus", "msToken", "X-Bogus"} & query.keys()
```

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/python -m pytest tests/unit/test_comments.py -q -k comment_list_url`
Expected: `ImportError`.

- [ ] **Step 3: Extract `anonymous_page()` in `browser_parser.py`**

Replace the body of `load_detail_with_anonymous_browser` with a shared context manager. Behaviour of video parsing must stay byte-for-byte the same (same launch args, UA, locale, timezone, viewport, and `BLOCKED_RESOURCE_TYPES`).

```python
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

from playwright.sync_api import Page, Response, Route, sync_playwright


@contextmanager
def anonymous_page(
    *, blocked_resource_types: frozenset[str] = BLOCKED_RESOURCE_TYPES
) -> Iterator[Page]:
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
            context = browser.new_context(
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                user_agent=anonymous_user_agent(
                    browser.version, system=platform.system(), machine=platform.machine()
                ),
                viewport={"width": 1280, "height": 720},
            )
            page = context.new_page()

            def route_request(route: Route) -> None:
                if route.request.resource_type in blocked_resource_types:
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", route_request)
            yield page
        finally:
            browser.close()


def load_detail_with_anonymous_browser(canonical_url: str, aweme_id: str) -> Mapping[str, Any]:
    with anonymous_page() as page:
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
```

Keep `should_block_resource` for its existing tests.

- [ ] **Step 4: Add the browser source and fetcher to `comments.py`**

```python
import random
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from video_recover.errors import CommentsUnavailable

COMMENT_LIST_PATH = "/aweme/v1/web/comment/list/"
SIGNATURE_PARAMS = frozenset({"a_bogus", "msToken", "X-Bogus"})
COMMENT_BLOCKED_RESOURCES = frozenset({"font", "media"})

# Projects only safe fields inside the page so uid/sec_uid/avatar never reach Python.
# Promise.race is mandatory: the page SDK can hold requests forever and AbortController
# does not release them (see spike).
FETCH_PAGE_JS = """
async ([url, timeoutMs]) => {
  const timeout = new Promise((resolve) => setTimeout(() => resolve({ timed_out: true }), timeoutMs));
  const request = (async () => {
    const response = await fetch(url, { credentials: "include" });
    const text = await response.text();
    let body;
    try { body = JSON.parse(text); } catch (error) { return { http: response.status, parse_error: true }; }
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
    def __init__(self, page: Any, *, page_timeout_seconds: float, first_request_timeout_seconds: float):
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
        assert self.template_url is not None
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
```

The lazy import keeps `comments.py` importable in unit tests without touching Playwright at import time. Place the new imports at the top of the module (ruff `I` rule); the snippet above lists them separately only for readability.

- [ ] **Step 5: Run unit and parser tests**

Run: `.venv/bin/python -m pytest tests/unit tests/integration/test_parsers.py -q && .venv/bin/ruff check src tests`
Expected: all pass. `test_parsers.py` proves the `anonymous_page` refactor did not change video parsing.

- [ ] **Step 6: Commit**

```bash
git add src/video_recover/browser_parser.py src/video_recover/comments.py tests/unit/test_comments.py
git commit -m "feat(comments): fetch comment pages through the anonymous page SDK"
```

---

### Task 4: Repository for comment jobs

**Files:**
- Modify: `src/video_recover/repository.py`
- Create: `tests/integration/test_comment_jobs.py`

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

from video_recover.domain import CommentJobStatus
from video_recover.repository import Repository

TEST_URL = "https://www.douyin.com/video/7662212894569811235"


def make_repository(tmp_path):
    repository = Repository(tmp_path / "db.sqlite3")
    task, _ = repository.create_or_get_task(TEST_URL)
    return repository, task


def test_one_active_comment_job_per_task(tmp_path) -> None:
    repository, task = make_repository(tmp_path)
    first, created = repository.create_or_get_comment_job(task.id, max_comments=200)
    again, created_again = repository.create_or_get_comment_job(task.id, max_comments=50)
    assert created and not created_again
    assert again.id == first.id and again.max_comments == 200


def test_claim_finish_and_latest_snapshot(tmp_path) -> None:
    repository, task = make_repository(tmp_path)
    job, _ = repository.create_or_get_comment_job(task.id, max_comments=200)
    claimed = repository.claim_next_comment_job()
    assert claimed is not None and claimed.status == CommentJobStatus.RUNNING
    assert repository.claim_next_comment_job() is None
    assert repository.has_running_comment_job(task.id)

    done = repository.finish_comment_job(
        job.id, status=CommentJobStatus.PARTIAL, fetched_count=41, pages=3,
        has_more_remaining=True, total_reported=1131,
        snapshot_name="20261018T031500Z-1a2b3c4d",
        error_code="comment_rate_limited", error_message="限流",
    )
    assert done.finished_at is not None and done.fetched_count == 41
    assert repository.latest_comment_snapshot_job(task.id).id == job.id
    second, created = repository.create_or_get_comment_job(task.id, max_comments=200)
    assert created and second.id != job.id
    assert [j.id for j in repository.list_comment_jobs(task.id)] == [second.id, job.id]


def test_failed_jobs_are_not_snapshots_and_restart_requeues_running(tmp_path) -> None:
    repository, task = make_repository(tmp_path)
    job, _ = repository.create_or_get_comment_job(task.id, max_comments=200)
    repository.claim_next_comment_job()
    assert repository.recover_interrupted_comment_jobs() == 1
    requeued = repository.get_comment_job(job.id)
    assert requeued.status == CommentJobStatus.QUEUED and requeued.started_at is None

    repository.claim_next_comment_job()
    repository.finish_comment_job(
        job.id, status=CommentJobStatus.FAILED, fetched_count=0, pages=0,
        has_more_remaining=False, total_reported=None, snapshot_name=None,
        error_code="comments_unavailable", error_message="无评论区",
    )
    assert repository.latest_comment_snapshot_job(task.id) is None


def test_comment_jobs_cascade_with_task(tmp_path) -> None:
    repository, task = make_repository(tmp_path)
    job, _ = repository.create_or_get_comment_job(task.id, max_comments=200)
    repository.delete_task(task.id)
    assert repository.list_comment_jobs(task.id) == []
```

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/python -m pytest tests/integration/test_comment_jobs.py -q`
Expected: `AttributeError: 'Repository' object has no attribute 'create_or_get_comment_job'`.

- [ ] **Step 3: Extend the schema**

Append inside `SCHEMA`, after the existing index statements:

```sql
CREATE TABLE IF NOT EXISTS comment_jobs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    max_comments INTEGER NOT NULL CHECK(max_comments BETWEEN 1 AND 1000),
    fetched_count INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    has_more_remaining INTEGER NOT NULL DEFAULT 0 CHECK(has_more_remaining IN (0, 1)),
    total_reported INTEGER,
    snapshot_name TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_comment_jobs_one_active
    ON comment_jobs(task_id) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS idx_comment_jobs_status_created ON comment_jobs(status, created_at);
```

Existing databases pick this up on the next `_initialize()`; no migration is needed.

- [ ] **Step 4: Implement the methods**

Add to `Repository` (import `CommentJob`, `CommentJobStatus` from `domain`):

```python
    def create_or_get_comment_job(
        self, task_id: str, *, max_comments: int
    ) -> tuple[CommentJob, bool]:
        now = _iso(self.clock())
        job_id = str(uuid4())
        with self.transaction(immediate=True) as connection:
            active = connection.execute(
                "SELECT * FROM comment_jobs WHERE task_id=? AND status IN (?, ?)",
                (task_id, CommentJobStatus.QUEUED.value, CommentJobStatus.RUNNING.value),
            ).fetchone()
            if active is not None:
                return self._comment_job(active), False
            connection.execute(
                """
                INSERT INTO comment_jobs(id, task_id, status, max_comments, created_at)
                VALUES(?,?,?,?,?)
                """,
                (job_id, task_id, CommentJobStatus.QUEUED.value, max_comments, now),
            )
        return self.get_comment_job(job_id), True

    def get_comment_job(self, job_id: str) -> CommentJob:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM comment_jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._comment_job(row)

    def list_comment_jobs(self, task_id: str) -> list[CommentJob]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM comment_jobs WHERE task_id=? ORDER BY created_at DESC, rowid DESC",
                (task_id,),
            ).fetchall()
        return [self._comment_job(row) for row in rows]

    def latest_comment_snapshot_job(self, task_id: str) -> CommentJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM comment_jobs
                WHERE task_id=? AND snapshot_name IS NOT NULL AND status IN (?, ?)
                ORDER BY finished_at DESC LIMIT 1
                """,
                (task_id, CommentJobStatus.COMPLETED.value, CommentJobStatus.PARTIAL.value),
            ).fetchone()
        return None if row is None else self._comment_job(row)

    def has_running_comment_job(self, task_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM comment_jobs WHERE task_id=? AND status=?",
                (task_id, CommentJobStatus.RUNNING.value),
            ).fetchone()
        return row is not None

    def claim_next_comment_job(self) -> CommentJob | None:
        now = _iso(self.clock())
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT id FROM comment_jobs WHERE status=? ORDER BY created_at LIMIT 1",
                (CommentJobStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE comment_jobs SET status=?, started_at=? WHERE id=?",
                (CommentJobStatus.RUNNING.value, now, row["id"]),
            )
            job_id = str(row["id"])
        return self.get_comment_job(job_id)

    def finish_comment_job(
        self,
        job_id: str,
        *,
        status: CommentJobStatus,
        fetched_count: int,
        pages: int,
        has_more_remaining: bool,
        total_reported: int | None,
        snapshot_name: str | None,
        error_code: str | None,
        error_message: str | None,
    ) -> CommentJob:
        if status not in {
            CommentJobStatus.COMPLETED, CommentJobStatus.PARTIAL, CommentJobStatus.FAILED,
        }:
            raise ValueError("comment jobs can only finish in a terminal state")
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE comment_jobs
                SET status=?, fetched_count=?, pages=?, has_more_remaining=?,
                    total_reported=?, snapshot_name=?, error_code=?, error_message=?,
                    finished_at=?
                WHERE id=? AND status=?
                """,
                (
                    status.value, fetched_count, pages, int(has_more_remaining),
                    total_reported, snapshot_name, error_code, error_message,
                    _iso(self.clock()), job_id, CommentJobStatus.RUNNING.value,
                ),
            )
        return self.get_comment_job(job_id)

    def recover_interrupted_comment_jobs(self) -> int:
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE comment_jobs SET status=?, started_at=NULL WHERE status=?",
                (CommentJobStatus.QUEUED.value, CommentJobStatus.RUNNING.value),
            )
            return cursor.rowcount

    @staticmethod
    def _comment_job(row: sqlite3.Row) -> CommentJob:
        return CommentJob(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            status=CommentJobStatus(row["status"]),
            max_comments=int(row["max_comments"]),
            fetched_count=int(row["fetched_count"]),
            pages=int(row["pages"]),
            has_more_remaining=bool(row["has_more_remaining"]),
            total_reported=row["total_reported"],
            snapshot_name=row["snapshot_name"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=_datetime(row["created_at"]),
            started_at=_datetime(row["started_at"]) if row["started_at"] else None,
            finished_at=_datetime(row["finished_at"]) if row["finished_at"] else None,
        )
```

`finish_comment_job` only updates a `running` row, so a job requeued by a restart cannot be overwritten by a stale worker.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/integration/test_comment_jobs.py tests/integration/test_repository.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/video_recover/repository.py tests/integration/test_comment_jobs.py
git commit -m "feat(comments): persist comment jobs with one active job per video"
```

---

### Task 5: Service — request, process, read, submit, delete

**Files:**
- Modify: `src/video_recover/config.py`
- Modify: `src/video_recover/service.py`
- Test: `tests/integration/test_comment_jobs.py`

- [ ] **Step 1: Add settings**

In `Settings`:

```python
    comments_default_max: int = Field(default=200, ge=1, le=1000)
    comments_job_timeout_seconds: int = Field(default=180, ge=30)
    comments_page_timeout_seconds: int = Field(default=20, ge=5)
```

- [ ] **Step 2: Write the failing service tests**

Append to `tests/integration/test_comment_jobs.py`:

```python
import json

import pytest

from video_recover.comments import CommentHarvest, map_comment
from video_recover.config import Settings
from video_recover.crypto import CookieVault
from video_recover.domain import TaskStatus
from video_recover.errors import CommentRateLimited, CommentsUnavailable, InvalidTransition
from video_recover.service import VideoService

AWEME_ID = "7662212894569811235"


class StubFetcher:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def fetch(self, canonical_url, *, max_comments, deadline_seconds):
        self.calls.append((canonical_url, max_comments, deadline_seconds))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def harvest(*cids, error=None, has_more=False):
    return CommentHarvest(
        comments=tuple(
            map_comment({"cid": c, "text": f"评论{c}", "digg_count": int(c), "nickname": "用户"})
            for c in cids
        ),
        pages=1, has_more_remaining=has_more, total_reported=10, stop_error=error,
    )


def make_service(tmp_path, fetcher=None):
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_directories()
    service = VideoService(
        settings=settings,
        repository=Repository(settings.database_path),
        parser=object(),
        cookie_vault=CookieVault(settings.secret_key_path),
        comment_fetcher=fetcher,
    )
    return settings, service


def run_job(service, task_id, **kwargs):
    service.request_comments(task_id, **kwargs)
    return service.process_comment_job(service.repository.claim_next_comment_job())


def test_completed_job_writes_snapshot_and_latest_markdown(tmp_path) -> None:
    fetcher = StubFetcher(harvest("1", "5"))
    settings, service = make_service(tmp_path, fetcher)
    task, _ = service.submit(TEST_URL)

    job = run_job(service, task.id)

    assert job.status.value == "completed" and job.fetched_count == 2
    assert fetcher.calls == [(TEST_URL, 200, 180)]
    folder = settings.download_dir / AWEME_ID / "comments"
    snapshot = json.loads((folder / f"{job.snapshot_name}.json").read_text(encoding="utf-8"))
    assert snapshot["status"] == "completed"
    assert set(snapshot["comments"][0]) == {
        "cid", "text", "digg_count", "reply_count", "created_at", "ip_label", "nickname",
    }
    assert (folder / "latest.md").read_text(encoding="utf-8").startswith("# 评论快照")


def test_partial_and_failed_outcomes(tmp_path) -> None:
    _settings, service = make_service(tmp_path, StubFetcher(harvest("1", error=CommentRateLimited())))
    task, _ = service.submit(TEST_URL)
    partial = run_job(service, task.id)
    assert (partial.status.value, partial.error_code) == ("partial", "comment_rate_limited")
    assert partial.snapshot_name is not None

    service.comment_fetcher = StubFetcher(CommentsUnavailable())
    failed = run_job(service, task.id)
    assert (failed.status.value, failed.snapshot_name) == ("failed", None)
    assert service.repository.latest_comment_snapshot_job(task.id).id == partial.id


def test_read_comments_sorts_and_limits(tmp_path) -> None:
    _settings, service = make_service(tmp_path, StubFetcher(harvest("1", "9", "5")))
    task, _ = service.submit(TEST_URL)
    run_job(service, task.id)

    view = service.read_comments(task.id, sort="likes", limit=2)
    assert [c.cid for c in view.comments] == ["9", "5"]
    assert view.total_in_snapshot == 3
    assert service.read_comments(task.id, sort="time", limit=10) is not None


def test_request_rejects_cancelled_and_validates_limit(tmp_path) -> None:
    _settings, service = make_service(tmp_path, StubFetcher(harvest()))
    task, _ = service.submit(TEST_URL)
    with pytest.raises(ValueError):
        service.request_comments(task.id, max_comments=1001)
    service.repository.transition(task.id, TaskStatus.CANCELLED, progress=0, message="取消")
    with pytest.raises(InvalidTransition):
        service.request_comments(task.id)


def test_submit_can_queue_comments_and_delete_cleans_folder(tmp_path) -> None:
    settings, service = make_service(tmp_path, StubFetcher(harvest("1")))
    task, _ = service.submit(TEST_URL, fetch_comments=True)
    assert len(service.repository.list_comment_jobs(task.id)) == 1

    claimed = service.repository.claim_next_comment_job()
    with pytest.raises(InvalidTransition):
        service.delete(task.id)  # comment job is running
    service.process_comment_job(claimed)

    service.delete(task.id)
    assert not (settings.download_dir / AWEME_ID).exists()
```

- [ ] **Step 3: Run to confirm failure**

Run: `.venv/bin/python -m pytest tests/integration/test_comment_jobs.py -q`
Expected: `TypeError: VideoService.__init__() got an unexpected keyword argument 'comment_fetcher'`.

- [ ] **Step 4: Implement in `service.py`**

Constructor and helpers:

```python
import re
from dataclasses import asdict, dataclass

from video_recover.comments import Comment, CommentFetcher, render_markdown
from video_recover.domain import CommentJob, CommentJobStatus

SNAPSHOT_NAME = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")


@dataclass(frozen=True, slots=True)
class CommentView:
    job: CommentJob
    fetched_at: str
    total_in_snapshot: int
    comments: list[Comment]
```

`VideoService.__init__` gains `comment_fetcher: CommentFetcher | None = None` and stores it as `self.comment_fetcher`.

Methods:

```python
    def comments_dir(self, task: Task) -> Path:
        aweme_id = normalize_douyin_url(task.canonical_url).aweme_id
        return self.settings.download_dir / aweme_id / "comments"

    def request_comments(
        self, task_id: str, *, max_comments: int | None = None
    ) -> tuple[CommentJob, bool]:
        task = self.repository.get_task(task_id)
        if task.status == TaskStatus.CANCELLED:
            raise InvalidTransition("cannot fetch comments for a cancelled task")
        limit = max_comments if max_comments is not None else self.settings.comments_default_max
        if not 1 <= limit <= 1000:
            raise ValueError("max_comments must be between 1 and 1000")
        return self.repository.create_or_get_comment_job(task.id, max_comments=limit)

    def process_comment_job(self, job: CommentJob) -> CommentJob:
        if self.comment_fetcher is None:
            raise RuntimeError("comment fetcher is not configured")
        task = self.repository.get_task(job.task_id)
        try:
            harvest = self.comment_fetcher.fetch(
                task.canonical_url,
                max_comments=job.max_comments,
                deadline_seconds=self.settings.comments_job_timeout_seconds,
            )
        except UserFacingError as exc:
            return self._finish_failed(job, exc)
        error = harvest.stop_error
        if error is not None and not harvest.comments:
            return self._finish_failed(job, error, pages=harvest.pages)
        status = CommentJobStatus.COMPLETED if error is None else CommentJobStatus.PARTIAL
        fetched_at = self.repository.clock()
        snapshot_name = f"{fetched_at:%Y%m%dT%H%M%SZ}-{job.id[:8]}"
        folder = self.comments_dir(task)
        folder.mkdir(parents=True, exist_ok=True)
        payload = {
            "aweme_id": folder.parent.name,
            "fetched_at": fetched_at.isoformat(),
            "status": status.value,
            "pages": harvest.pages,
            "has_more_remaining": harvest.has_more_remaining,
            "total_reported": harvest.total_reported,
            "comments": [
                {**asdict(c), "created_at": c.created_at.isoformat() if c.created_at else None}
                for c in harvest.comments
            ],
        }
        _atomic_text(
            folder / f"{snapshot_name}.json",
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        _atomic_text(
            folder / "latest.md",
            render_markdown(aweme_id=folder.parent.name, fetched_at=fetched_at, harvest=harvest),
        )
        return self.repository.finish_comment_job(
            job.id,
            status=status,
            fetched_count=len(harvest.comments),
            pages=harvest.pages,
            has_more_remaining=harvest.has_more_remaining,
            total_reported=harvest.total_reported,
            snapshot_name=snapshot_name,
            error_code=None if error is None else error.code,
            error_message=None if error is None else error.message,
        )

    def _finish_failed(self, job: CommentJob, error: UserFacingError, *, pages: int = 0) -> CommentJob:
        return self.repository.finish_comment_job(
            job.id, status=CommentJobStatus.FAILED, fetched_count=0, pages=pages,
            has_more_remaining=False, total_reported=None, snapshot_name=None,
            error_code=error.code, error_message=error.message,
        )

    def read_comments(
        self,
        task_id: str,
        *,
        job_id: str | None = None,
        sort: str = "likes",
        limit: int = 50,
    ) -> CommentView | None:
        task = self.repository.get_task(task_id)
        if job_id is None:
            job = self.repository.latest_comment_snapshot_job(task.id)
        else:
            job = self.repository.get_comment_job(job_id)
            if job.task_id != task.id:
                raise KeyError(job_id)
        if job is None or job.snapshot_name is None:
            return None
        if not SNAPSHOT_NAME.fullmatch(job.snapshot_name):
            raise RuntimeError("invalid comment snapshot name")
        payload = json.loads(
            (self.comments_dir(task) / f"{job.snapshot_name}.json").read_text(encoding="utf-8")
        )
        comments = [
            Comment(
                **{
                    **raw,
                    "created_at": datetime.fromisoformat(raw["created_at"])
                    if raw["created_at"] else None,
                }
            )
            for raw in payload["comments"]
        ]
        if sort == "time":
            comments.sort(key=lambda c: c.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        else:
            comments.sort(key=lambda c: -c.digg_count)
        return CommentView(
            job=job,
            fetched_at=payload["fetched_at"],
            total_in_snapshot=len(comments),
            comments=comments[: max(1, min(limit, 1000))],
        )
```

`submit` gains `fetch_comments: bool = False`; after `create_or_get_task`:

```python
        if fetch_comments and task.status != TaskStatus.CANCELLED:
            self.request_comments(task.id)
        return task, created
```

`delete` changes, before removing files:

```python
        if self.repository.has_running_comment_job(task.id):
            raise InvalidTransition("cannot delete a task while comments are being fetched")
        aweme_dir = (self.settings.download_dir / normalize_douyin_url(task.canonical_url).aweme_id)
```

Then remove `task.output_dir` as today, and additionally `shutil.rmtree(aweme_dir, ignore_errors=True)` after the same `is_relative_to(root)` check. They are normally the same directory; the second call covers comments fetched before the video was downloaded.

Import `datetime`, `UTC` and `normalize_douyin_url` in `service.py` if missing.

- [ ] **Step 5: Run service and existing tests**

Run: `.venv/bin/python -m pytest tests/integration -q`
Expected: all pass (existing `VideoService(...)` call sites keep working because `comment_fetcher` defaults to `None`).

- [ ] **Step 6: Commit**

```bash
git add src/video_recover/config.py src/video_recover/service.py tests/integration/test_comment_jobs.py
git commit -m "feat(comments): store comment snapshots and read them back sorted"
```

---

### Task 6: Runner scheduling and app wiring

**Files:**
- Modify: `src/video_recover/runner.py`
- Modify: `src/video_recover/main.py`
- Test: `tests/integration/test_runner.py`

- [ ] **Step 1: Write the failing runner tests**

Extend `make_context` in `tests/integration/test_runner.py` with a `comment_fetcher=None` keyword passed to `VideoService`, then add:

```python
from video_recover.comments import CommentHarvest


class EmptyFetcher:
    calls = 0

    def fetch(self, canonical_url, *, max_comments, deadline_seconds):
        EmptyFetcher.calls += 1
        return CommentHarvest((), 1, False, 0, None)


def test_pipeline_runs_before_comment_jobs(tmp_path) -> None:
    service, _repository, runner = make_context(tmp_path, comment_fetcher=EmptyFetcher())
    task, _ = service.submit(TEST_URL, fetch_comments=True)

    assert runner.run_once() is True  # download pipeline first
    assert service.repository.list_comment_jobs(task.id)[0].status.value == "queued"
    assert runner.run_once() is True  # then the comment job
    assert service.repository.list_comment_jobs(task.id)[0].status.value == "completed"


def test_comment_jobs_are_skipped_without_fetcher_and_requeued_on_startup(tmp_path) -> None:
    service, _repository, runner = make_context(tmp_path)
    task, _ = service.submit(TEST_URL, transcribe=False)  # no CPU-fallback work afterwards
    runner.run_once()
    service.request_comments(task.id)
    assert runner.run_once() is False  # no fetcher configured → job stays queued

    service.repository.claim_next_comment_job()
    runner.recover_startup()
    assert service.repository.list_comment_jobs(task.id)[0].status.value == "queued"
```

`make_context` returns `(service, repository, runner)`; only `comment_fetcher` is new.

- [ ] **Step 2: Run to confirm failure**

Run: `.venv/bin/python -m pytest tests/integration/test_runner.py -q`

- [ ] **Step 3: Schedule comment jobs in `run_once`**

Between the pipeline block and the CPU fallback block:

```python
        if self.service.comment_fetcher is not None:
            comment_job = self.repository.claim_next_comment_job()
            if comment_job is not None:
                try:
                    self.service.process_comment_job(comment_job)
                except Exception:
                    self.logger.exception(
                        "comment job failed", extra={"comment_job_id": comment_job.id}
                    )
                    failure = InternalFailure()
                    self.repository.finish_comment_job(
                        comment_job.id, status=CommentJobStatus.FAILED, fetched_count=0,
                        pages=0, has_more_remaining=False, total_reported=None,
                        snapshot_name=None, error_code=failure.code,
                        error_message=failure.message,
                    )
                return True
```

In `recover_startup`, also call `self.repository.recover_interrupted_comment_jobs()` and include it in the returned count.

- [ ] **Step 4: Wire the fetcher in `main.py`**

```python
from video_recover.comments import AnonymousCommentFetcher


def build_service(settings: Settings) -> VideoService:
    return VideoService(
        settings=settings,
        repository=Repository(settings.database_path),
        parser=ParserChain([YtDlpParser(), DouyinPageParser(), AnonymousBrowserParser()]),
        cookie_vault=CookieVault(settings.secret_key_path),
        comment_fetcher=AnonymousCommentFetcher(
            page_timeout_seconds=settings.comments_page_timeout_seconds,
        ),
    )
```

- [ ] **Step 5: Run the suite**

Run: `.venv/bin/python -m pytest -m 'not live and not mac_live' -q`
Expected: only the baseline `compose.yaml` failure.

- [ ] **Step 6: Commit**

```bash
git add src/video_recover/runner.py src/video_recover/main.py tests/integration/test_runner.py
git commit -m "feat(comments): run comment jobs after the download pipeline"
```

---

### Task 7: REST endpoints

**Files:**
- Modify: `src/video_recover/api.py`
- Test: `tests/integration/test_api.py`

- [ ] **Step 1: Write the failing API test**

`make_client` in `test_api.py` builds its own `VideoService`; add a `comment_fetcher` parameter (default `None`) and pass it through. Then:

```python
def test_comment_endpoints_queue_list_read_and_download(tmp_path: Path) -> None:
    from video_recover.comments import CommentHarvest, map_comment

    class Fetcher:
        def fetch(self, canonical_url, *, max_comments, deadline_seconds):
            comments = tuple(map_comment({"cid": c, "text": c, "digg_count": int(c)}) for c in "193")
            return CommentHarvest(comments, 1, False, 3, None)

    client, service = make_client(tmp_path, comment_fetcher=Fetcher())
    task_id = client.post("/api/tasks", json={"url": TEST_URL}).json()["id"]

    created = client.post(f"/api/tasks/{task_id}/comments", json={"max_comments": 50})
    assert created.status_code == 202 and created.json()["status"] == "queued"
    assert client.post(f"/api/tasks/{task_id}/comments", json={"max_comments": 5000}).status_code == 422
    assert client.get(f"/api/tasks/{task_id}/comments/latest.md").status_code == 404

    service.process_comment_job(service.repository.claim_next_comment_job())

    jobs = client.get(f"/api/tasks/{task_id}/comments").json()
    assert jobs[0]["status"] == "completed" and jobs[0]["fetched_count"] == 3
    view = client.get(f"/api/tasks/{task_id}/comments/{jobs[0]['id']}?limit=2").json()
    assert [c["digg_count"] for c in view["comments"]] == [9, 3]
    markdown = client.get(f"/api/tasks/{task_id}/comments/latest.md")
    assert markdown.status_code == 200 and markdown.text.startswith("# 评论快照")
    assert client.get(f"/api/tasks/{task_id}/comments/not-a-job").status_code == 404
```

- [ ] **Step 2: Implement**

Models:

```python
class CommentJobCreate(BaseModel):
    max_comments: int | None = Field(default=None, ge=1, le=1000)


class CommentJobResponse(BaseModel):
    id: str
    task_id: str
    status: str
    max_comments: int
    fetched_count: int
    pages: int
    has_more_remaining: bool
    error_code: str | None
    error_message: str | None
    created_at: datetime
    finished_at: datetime | None


class CommentResponse(BaseModel):
    cid: str
    text: str
    digg_count: int
    reply_count: int
    created_at: datetime | None
    ip_label: str | None
    nickname: str | None


class CommentSnapshotResponse(BaseModel):
    job: CommentJobResponse
    fetched_at: datetime
    total_in_snapshot: int
    comments: list[CommentResponse]
```

Routes inside `build_router` — register `latest.md` **before** `{job_id}`:

```python
    @router.post(
        "/api/tasks/{task_id}/comments", response_model=CommentJobResponse, status_code=202
    )
    def request_comments(task_id: str, body: CommentJobCreate) -> CommentJobResponse:
        get_task_or_404(task_id)
        try:
            job, _created = service.request_comments(task_id, max_comments=body.max_comments)
        except InvalidTransition:
            raise HTTPException(status_code=409, detail="已取消的任务不能拉取评论") from None
        return comment_job_response(job)

    @router.get("/api/tasks/{task_id}/comments", response_model=list[CommentJobResponse])
    def list_comment_jobs(task_id: str) -> list[CommentJobResponse]:
        get_task_or_404(task_id)
        return [comment_job_response(j) for j in service.repository.list_comment_jobs(task_id)]

    @router.get("/api/tasks/{task_id}/comments/latest.md")
    def latest_comments_markdown(task_id: str) -> FileResponse:
        task = get_task_or_404(task_id)
        path = service.comments_dir(task) / "latest.md"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="还没有评论快照")
        return FileResponse(path, media_type="text/markdown; charset=utf-8", filename="comments.md")

    @router.get(
        "/api/tasks/{task_id}/comments/{job_id}", response_model=CommentSnapshotResponse
    )
    def read_comments(
        task_id: str,
        job_id: str,
        sort: Literal["likes", "time"] = "likes",
        limit: int = 100,
    ) -> CommentSnapshotResponse:
        get_task_or_404(task_id)
        try:
            view = service.read_comments(task_id, job_id=job_id, sort=sort, limit=limit)
        except KeyError:
            raise HTTPException(status_code=404, detail="评论快照不存在") from None
        if view is None:
            raise HTTPException(status_code=404, detail="评论快照不存在")
        return CommentSnapshotResponse(
            job=comment_job_response(view.job),
            fetched_at=view.fetched_at,
            total_in_snapshot=view.total_in_snapshot,
            comments=[CommentResponse(**asdict(c)) for c in view.comments],
        )
```

Add a module-level `comment_job_response(job: CommentJob) -> CommentJobResponse` next to `task_response`, and update the delete route's 409 detail to cover comments (`"任务处理中或正在拉取评论，暂时不能删除"`).

- [ ] **Step 3: Run**

Run: `.venv/bin/python -m pytest tests/integration/test_api.py -q`
Expected: pass, including `test_openapi_includes_public_and_internal_contracts`; extend that test's expected paths with the three comment paths if it asserts an exact set.

- [ ] **Step 4: Commit**

```bash
git add src/video_recover/api.py tests/integration/test_api.py
git commit -m "feat(comments): expose comment jobs and snapshots over REST"
```

---

### Task 8: MCP tools

**Files:**
- Modify: `src/video_recover/mcp_server.py`
- Test: `tests/integration/test_mcp.py`

- [ ] **Step 1: Write the failing tests**

Add `"fetch_comments"` and `"get_comments"` to `EXPECTED_TOOLS`, let `make_service` accept `comment_fetcher`, then:

```python
@pytest.mark.asyncio
async def test_comment_tools_queue_and_return_compact_text(tmp_path: Path) -> None:
    from video_recover.comments import CommentHarvest, map_comment

    class Fetcher:
        def fetch(self, canonical_url, *, max_comments, deadline_seconds):
            raw = [{"cid": "1", "text": "第一行\n第二行", "digg_count": 7, "ip_label": "广东",
                    "create_time": 1791029240}]
            return CommentHarvest(tuple(map(map_comment, raw)), 1, True, 99, None)

    _settings, service = make_service(tmp_path, comment_fetcher=Fetcher())
    mcp = build_mcp(service)
    _c, submitted = await mcp.call_tool("submit_video", {"url": TEST_URL, "fetch_comments": True})
    assert submitted["comment_job_id"]

    _c, empty = await mcp.call_tool("get_comments", {"task_id": submitted["task_id"]})
    assert empty["available"] is False

    service.process_comment_job(service.repository.claim_next_comment_job())
    _c, result = await mcp.call_tool("get_comments", {"task_id": submitted["task_id"], "limit": 500})
    assert result["available"] is True and result["limit"] == 200
    assert result["text"].splitlines()[0].startswith("👍7")
    assert "第一行 第二行" in result["text"]
    assert result["snapshot"]["has_more_remaining"] is True

    _c, again = await mcp.call_tool("fetch_comments", {"task_id": submitted["task_id"]})
    assert again["ok"] is True and again["created"] is True
```

- [ ] **Step 2: Implement**

`submit_video` gains `fetch_comments: bool = False`, passes it to `service.submit`, and adds `comment_job_id` (latest active job id or `None`) to the payload.

```python
    @mcp.tool()
    def fetch_comments(task_id: str, max_comments: int = 200) -> dict[str, Any]:
        """Queue an anonymous snapshot of a video's top-level comments (no replies).
        Asynchronous: poll get_comments until available."""
        try:
            job, created = service.request_comments(task_id, max_comments=max_comments)
        except KeyError:
            return {"ok": False, "error_code": "not_found", "message": "任务不存在"}
        except InvalidTransition:
            return {"ok": False, "error_code": "invalid_state", "message": "已取消的任务不能拉取评论"}
        except ValueError:
            return {"ok": False, "error_code": "invalid_limit", "message": "max_comments 需在 1–1000"}
        return {"ok": True, "created": created, **_comment_job_payload(job)}

    @mcp.tool()
    def get_comments(
        task_id: str, job_id: str | None = None, sort: str = "likes", limit: int = 50
    ) -> dict[str, Any]:
        """Read a comment snapshot as compact text, one comment per line."""
        safe_limit = max(1, min(limit, 200))
        try:
            view = service.read_comments(
                task_id, job_id=job_id, sort="time" if sort == "time" else "likes", limit=safe_limit
            )
        except KeyError:
            return {"ok": False, "error_code": "not_found", "message": "任务或快照不存在"}
        jobs = service.repository.list_comment_jobs(task_id)
        if view is None:
            latest = jobs[0] if jobs else None
            return {
                "ok": True,
                "available": False,
                "latest_job": None if latest is None else _comment_job_payload(latest),
                "message": "还没有评论快照，可先调用 fetch_comments",
            }
        lines = [
            " · ".join([
                f"👍{c.digg_count} 💬{c.reply_count}",
                f"{c.created_at:%Y-%m-%d}" if c.created_at else "—",
                c.ip_label or "—",
                " ".join(c.text.split()),
            ])
            for c in view.comments
        ]
        return {
            "ok": True,
            "available": True,
            "limit": safe_limit,
            "snapshot": {
                **_comment_job_payload(view.job),
                "fetched_at": view.fetched_at,
                "total_in_snapshot": view.total_in_snapshot,
            },
            "text": "\n".join(lines),
        }
```

Add `_comment_job_payload(job)` next to `_task_payload` with `job_id`, `status`, `max_comments`, `fetched_count`, `has_more_remaining`, `error_code`, `error_message`, `created_at`, `finished_at`.

Extend the server `instructions`: "Comment snapshots are also asynchronous: call fetch_comments, then poll get_comments. Only top-level comments are available; replies are not collected."

- [ ] **Step 3: Run**

Run: `.venv/bin/python -m pytest tests/integration/test_mcp.py -q`

- [ ] **Step 4: Commit**

```bash
git add src/video_recover/mcp_server.py tests/integration/test_mcp.py
git commit -m "feat(comments): add fetch_comments and get_comments MCP tools"
```

---

### Task 9: Web comment section

**Files:**
- Modify: `src/video_recover/templates/index.html`
- Modify: `src/video_recover/static/app.js`
- Modify: `src/video_recover/static/app.css`
- Test: `tests/integration/test_web.py`

- [ ] **Step 1: Lock the DOM contract**

In `test_archive_page_has_compact_workspace_and_accessible_controls` add:

```python
    assert 'id="comments-section"' in response.text
    assert 'id="comments-fetch"' in response.text
    assert 'id="comments-snapshot"' in response.text
    assert 'id="comments-list"' in response.text
    assert 'id="comments-download"' in response.text
```

and in the asset test: `assert ".comment-list" in stylesheet.text`.

- [ ] **Step 2: Template**

After the transcript `copy-section` (inside the same column), add:

```html
<section class="copy-section comments-section" id="comments-section" aria-labelledby="comments-title">
  <div class="section-heading">
    <h3 id="comments-title">评论</h3>
    <button class="text-action" id="comments-fetch" type="button">拉取评论</button>
  </div>
  <div class="comments-toolbar">
    <label class="visually-hidden" for="comments-snapshot">选择评论快照</label>
    <select id="comments-snapshot"></select>
    <a id="comments-download" class="text-action" href="#" hidden>下载 MD</a>
  </div>
  <p id="comments-meta" class="copy-meta" aria-live="polite">还没有评论快照。仅一级评论，按点赞排序。</p>
  <ol id="comments-list" class="comment-list"></ol>
</section>
```

Reuse an existing visually-hidden utility class if the stylesheet already has one; otherwise add it in Step 4.

- [ ] **Step 3: JavaScript**

Inside the existing IIFE, following the `api()` / `renderRecord()` style:

- `loadComments(task, signal)`: `GET /api/tasks/{id}/comments`; fill `#comments-snapshot` with jobs that have `fetched_count > 0` or status `completed` (label: `MM-DD HH:mm · N 条 · 已拉全/未拉全`); when a job is `queued`/`running`, set the button to `拉取中…`, disable it, and let the existing `schedulePoll()` refresh; render the selected snapshot via `GET /api/tasks/{id}/comments/{job}?limit=100` into `<li>` items (`👍N`, date, IP, text via `textContent` only — never `innerHTML`); show `#comments-download` pointing at `comments/latest.md` when any snapshot exists; show the latest failed job's `error_message` in `#comments-meta`.
- `fetchComments()`: `POST /api/tasks/{id}/comments` with `{}`, toast `已开始拉取评论`, then `refreshNow()`.
- Call `loadComments` from `renderRecord`, bind `#comments-fetch` click and `#comments-snapshot` change in `bindEvents`.
- Abort with the same `AbortController` signal `renderRecord` already receives so switching tasks cancels stale requests.

- [ ] **Step 4: CSS**

Add `.comments-section`, `.comments-toolbar`, `.comment-list` (no bullets, 0.5rem gap, `max-height: 28rem; overflow-y: auto`), `.comment-meta` (muted tabular numbers), using the existing paper/vermilion tokens. Keep the mobile layout free of horizontal overflow (the e2e test checks this).

- [ ] **Step 5: Run Web tests**

Run: `.venv/bin/python -m pytest tests/integration/test_web.py tests/e2e/test_web_ui.py -q`
Expected: pass (`test_web_ui.py` drives system Chrome; it must still report no console errors).

- [ ] **Step 6: Commit**

```bash
git add src/video_recover/templates/index.html src/video_recover/static tests/integration/test_web.py
git commit -m "feat(comments): show comment snapshots in the web workspace"
```

---

### Task 10: Live acceptance, docs, and full verification

**Files:**
- Create: `tests/e2e/test_live_comments.py`
- Modify: `README.md`

- [ ] **Step 1: Live test (skipped by default)**

```python
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
    assert len({c.cid for c in harvest.comments}) == len(harvest.comments)
```

- [ ] **Step 2: README**

- Add `fetch_comments` / `get_comments` to the MCP tool list and `submit_video(fetch_comments)`.
- Add the four REST endpoints.
- Add a short "评论快照" section: anonymous only, top-level only (replies need login), default 200 / max 1000, snapshots in `downloads/<aweme_id>/comments/`, the SDK-signing dependency and what `comment_sign_missing` means.

- [ ] **Step 3: Full local verification**

```bash
.venv/bin/python -m pytest -m 'not live and not mac_live' -q
.venv/bin/ruff check .
VIDEO_RECOVER_LIVE=1 .venv/bin/python -m pytest tests/e2e/test_live_comments.py -m live -q -s
```

Expected: only the baseline `compose.yaml` failure; ruff clean; live test passes on the host Chrome.

- [ ] **Step 4: Container verification**

Rebuild and run the live comment test inside the image, using a throwaway container so the resident app is untouched:

```bash
docker build -t video-recover:comments .
docker run --rm -e VIDEO_RECOVER_LIVE=1 --entrypoint python video-recover:comments -c "from video_recover.comments import AnonymousCommentFetcher as F; h=F().fetch('https://www.douyin.com/video/7692767036498596273', max_comments=60, deadline_seconds=120); print(h.pages, len(h.comments), h.stop_error)"
```

Expected: `>=3 >=40 None`. Do **not** run `./scripts/dev-up.sh` or replace the resident `video-recover-app-1` container without asking the owner first.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/test_live_comments.py README.md
git commit -m "docs(comments): document comment snapshots and add live acceptance test"
```

---

## Out of scope (from the spec)

Replies, Cookie mode, scheduled refresh, comment detail table and cross-video search, sentiment analysis, live/image posts.
