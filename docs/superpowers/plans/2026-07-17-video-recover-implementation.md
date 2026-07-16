# VideoRecover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and publish a Docker-managed Douyin video downloader with a polished local Web UI, persistent jobs, Codex/Claude Desktop MCP access, and Metal-accelerated macOS transcription.

**Architecture:** A FastAPI control plane owns SQLite state, parsing, downloads, Web, REST, and MCP. A user LaunchAgent runs an MLX Whisper worker that long-polls the control plane for leased transcription jobs; a container CPU provider is the fallback. All interfaces call one application service and persist artifacts beneath one host-mounted data root.

## Execution status (2026-07-17)

- Tasks 1–12 are implemented and committed on `feature/video-recover`.
- 70 non-live tests, including the real Chrome desktop/mobile E2E, pass.
- The ARM64 Docker container is healthy after restart; Web, health, HTTP MCP, stdio MCP, Codex configuration, Claude Desktop configuration, persistence, and the macOS LaunchAgent have been verified locally.
- The requested live URL correctly reaches the stable `cookie_required` state because Douyin now requires a browser-generated `s_v_web_id`. Final MP4/transcript acceptance and GitHub publication remain pending until a Cookie is saved through the local Web UI; no browser Cookie is read automatically.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Settings, SQLite, HTTPX, yt-dlp, cryptography/Fernet, official MCP Python SDK, Jinja2/vanilla JavaScript/CSS, faster-whisper CPU fallback, mlx-whisper on macOS, pytest, Ruff, Docker Compose.

---

## File map

```text
pyproject.toml                         # package metadata, runtime/dev/mac extras, test config
Dockerfile                            # native ARM64-compatible runtime image
compose.yaml                          # localhost-only service, volumes, health and resources
.env.example                          # supported operational settings
src/video_recover/config.py           # validated environment and data paths
src/video_recover/domain.py           # task, segment and media value objects + state rules
src/video_recover/errors.py           # stable user-facing error taxonomy
src/video_recover/url_policy.py       # Douyin URL extraction, redirect and SSRF controls
src/video_recover/crypto.py            # persistent Fernet key and cookie vault
src/video_recover/repository.py       # SQLite schema, tasks, settings, leases and recovery
src/video_recover/transcript.py       # TXT, SRT and Markdown formatters
src/video_recover/parsers.py          # yt-dlp and page-data parser adapters + fallback
src/video_recover/downloader.py       # resumable streaming download + atomic completion
src/video_recover/transcribers.py     # native-lease and CPU transcription providers
src/video_recover/service.py          # single application-service boundary
src/video_recover/runner.py           # persistent single-concurrency job loop
src/video_recover/api.py              # REST, worker lease API and health endpoints
src/video_recover/mcp_server.py       # shared MCP tool registrations and HTTP mount
src/video_recover/mcp_stdio.py        # Claude Desktop process transport
src/video_recover/main.py             # app factory and lifespan
src/video_recover/templates/index.html
src/video_recover/static/app.css
src/video_recover/static/app.js
src/video_recover_mac/config.py        # native worker settings
src/video_recover_mac/client.py        # lease/heartbeat/result control-plane client
src/video_recover_mac/transcriber.py   # lazy MLX model lifecycle
src/video_recover_mac/main.py          # long-running worker entry point
scripts/dev-up.sh                      # create secrets/data and start Compose
scripts/dev-down.sh                    # stop services without deleting data
scripts/dev-check.sh                   # deterministic deployment verification
scripts/install-mac-worker.sh          # venv + LaunchAgent idempotent install
scripts/uninstall-mac-worker.sh        # unload agent, retain user data/models
scripts/install-mcp.sh                 # Codex command + Claude config guidance
deploy/com.codesfly.video-recover.worker.plist
tests/unit/                            # pure domain, URL, crypto, transcript tests
tests/integration/                     # SQLite, parser, download, runner, API, MCP tests
tests/e2e/                             # Compose, stdio and opt-in live-link checks
README.md                              # setup, Cookie, MCP, operation and troubleshooting
LICENSE                               # MIT license for this repository
NOTICE                                # third-party parser references and licenses
```

## Task 1: Reproducible package and application skeleton

**Files:**
- Create: `pyproject.toml`
- Create: `src/video_recover/__init__.py`
- Create: `src/video_recover/config.py`
- Create: `src/video_recover/main.py`
- Create: `tests/conftest.py`
- Create: `tests/integration/test_health.py`

- [ ] **Step 1: Write the failing health test**

```python
from fastapi.testclient import TestClient
from video_recover.main import create_app


def test_health_reports_storage_and_service(tmp_settings):
    with TestClient(create_app(tmp_settings, start_runner=False)) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "storage": "ok"}
```

- [ ] **Step 2: Run it and verify RED**

Run: `python3 -m pytest tests/integration/test_health.py -q`  
Expected: FAIL because `video_recover.main` does not exist.

- [ ] **Step 3: Add package metadata and a minimal app factory**

`pyproject.toml` must declare Python 3.12, package discovery under `src`, runtime dependencies for FastAPI/HTTPX/yt-dlp/cryptography/Jinja2/MCP, `dev` extras for pytest/Ruff, and `mac` extras for mlx-whisper. `Settings` must resolve `data_dir`, `database_path`, `download_dir`, `secret_key_path`, localhost port, native-worker timeout and CPU fallback without reading global state at import time.

```python
def create_app(settings: Settings | None = None, *, start_runner: bool = True) -> FastAPI:
    cfg = settings or Settings()
    cfg.ensure_directories()
    app = FastAPI(title="VideoRecover", lifespan=build_lifespan(cfg, start_runner))

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        probe_sqlite(cfg.database_path)
        return {"status": "ok", "storage": "ok"}

    return app
```

- [ ] **Step 4: Run the focused test and the style check**

Run: `python3 -m pytest tests/integration/test_health.py -q && python3 -m ruff check src tests`  
Expected: one passing test and no Ruff violations.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/video_recover tests/conftest.py tests/integration/test_health.py
git commit -m "feat: scaffold application control plane"
```

## Task 2: Domain model and Douyin URL security policy

**Files:**
- Create: `src/video_recover/domain.py`
- Create: `src/video_recover/errors.py`
- Create: `src/video_recover/url_policy.py`
- Create: `tests/unit/test_domain.py`
- Create: `tests/unit/test_url_policy.py`

- [ ] **Step 1: Write state-machine and URL tests**

```python
def test_completed_task_cannot_return_to_downloading():
    with pytest.raises(InvalidTransition):
        require_transition(TaskStatus.COMPLETED, TaskStatus.DOWNLOADING)


@pytest.mark.parametrize("url", [
    "https://www.douyin.com/video/7662212894569811235",
    "https://douyin.com/video/7662212894569811235?previous_page=web_code_link",
])
def test_normalizes_video_urls(url):
    assert normalize_douyin_url(url).canonical_url == (
        "https://www.douyin.com/video/7662212894569811235"
    )


@pytest.mark.parametrize("url", [
    "http://www.douyin.com/video/7662212894569811235",
    "https://douyin.com.evil.example/video/7662212894569811235",
    "file:///etc/passwd",
])
def test_rejects_unsafe_urls(url):
    with pytest.raises(UnsafeUrl):
        normalize_douyin_url(url)
```

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest tests/unit/test_domain.py tests/unit/test_url_policy.py -q`  
Expected: FAIL on missing domain and URL policy modules.

- [ ] **Step 3: Implement explicit states, value objects and redirect checks**

```python
class TaskStatus(StrEnum):
    QUEUED = "queued"
    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    AWAITING_TRANSCRIPTION = "awaiting_transcription"
    TRANSCRIBING = "transcribing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


ALLOWED_HOSTS = frozenset({"douyin.com", "www.douyin.com", "v.douyin.com", "iesdouyin.com", "v.iesdouyin.com"})


def allowed_host(host: str | None) -> bool:
    return bool(host) and host.rstrip(".").lower() in ALLOWED_HOSTS
```

For short links, use an injected `httpx.Client` with redirects disabled, follow at most five `Location` headers manually, require HTTPS at every hop, reject credentials/fragments, and validate every destination before requesting the next hop.

- [ ] **Step 4: Verify GREEN and full state coverage**

Run: `python3 -m pytest tests/unit/test_domain.py tests/unit/test_url_policy.py -q`  
Expected: all parametrized security cases pass.

- [ ] **Step 5: Commit**

```bash
git add src/video_recover/domain.py src/video_recover/errors.py src/video_recover/url_policy.py tests/unit
git commit -m "feat: add secure douyin task domain"
```

## Task 3: Encrypted settings and durable SQLite repository

**Files:**
- Create: `src/video_recover/crypto.py`
- Create: `src/video_recover/repository.py`
- Create: `tests/unit/test_crypto.py`
- Create: `tests/integration/test_repository.py`

- [ ] **Step 1: Write failing cookie, dedupe, transition and lease tests**

```python
def test_cookie_is_encrypted_at_rest(tmp_path):
    vault = CookieVault(tmp_path / "app.key")
    token = vault.encrypt("sessionid=top-secret")
    assert b"top-secret" not in token
    assert vault.decrypt(token) == "sessionid=top-secret"


def test_create_task_deduplicates_canonical_url(repository):
    first, created = repository.create_or_get_task(TEST_URL)
    second, created_again = repository.create_or_get_task(TEST_URL)
    assert created is True
    assert created_again is False
    assert first.id == second.id


def test_expired_transcription_lease_returns_to_queue(repository, clock):
    task = repository.seed_awaiting_transcription(TEST_URL)
    lease = repository.acquire_transcription_lease("worker-a", ttl_seconds=30)
    clock.advance(seconds=31)
    repository.recover_expired_leases()
    assert repository.get_task(task.id).status == TaskStatus.AWAITING_TRANSCRIPTION
```

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest tests/unit/test_crypto.py tests/integration/test_repository.py -q`  
Expected: FAIL because vault and repository are missing.

- [ ] **Step 3: Implement the repository with transactions and migrations**

Create `tasks`, `events`, `settings`, and `transcription_leases` tables. Set `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, and `busy_timeout=5000`. Every transition must use `BEGIN IMMEDIATE`, compare the expected current status, insert an event, and commit atomically.

```python
def transition(self, task_id: str, target: TaskStatus, *, progress: int, message: str) -> Task:
    with self.transaction(immediate=True) as db:
        row = self._get_task_row(db, task_id)
        require_transition(TaskStatus(row["status"]), target)
        db.execute(
            "UPDATE tasks SET status=?, progress=?, message=?, updated_at=? WHERE id=?",
            (target.value, progress, message, self.clock.now_iso(), task_id),
        )
        db.execute(
            "INSERT INTO events(task_id, status, message, created_at) VALUES(?,?,?,?)",
            (task_id, target.value, message, self.clock.now_iso()),
        )
    return self.get_task(task_id)
```

The generated Fernet key must be created with mode `0600`; stored Cookie values are encrypted bytes encoded as URL-safe text. No repository log call may include the encrypted or plain value.

- [ ] **Step 4: Verify GREEN, reopen the database, and check persistence**

Run: `python3 -m pytest tests/unit/test_crypto.py tests/integration/test_repository.py -q`  
Expected: all tests pass, including a test that constructs a second repository instance over the same file.

- [ ] **Step 5: Commit**

```bash
git add src/video_recover/crypto.py src/video_recover/repository.py tests/unit/test_crypto.py tests/integration/test_repository.py
git commit -m "feat: persist encrypted recovery jobs"
```

## Task 4: Deterministic transcript artifact generation

**Files:**
- Create: `src/video_recover/transcript.py`
- Create: `tests/unit/test_transcript.py`

- [ ] **Step 1: Write exact TXT, SRT and Markdown assertions**

```python
SEGMENTS = [
    Segment(0.0, 1.24, "很多时候，我们不是缺少工具。"),
    Segment(1.24, 3.2, "而是缺少一条可以重复执行的路径。"),
]


def test_srt_has_stable_indices_and_millisecond_timestamps():
    assert render_srt(SEGMENTS) == (
        "1\n00:00:00,000 --> 00:00:01,240\n很多时候，我们不是缺少工具。\n\n"
        "2\n00:00:01,240 --> 00:00:03,200\n而是缺少一条可以重复执行的路径。\n"
    )


def test_markdown_keeps_original_words_and_groups_paragraphs():
    rendered = render_markdown("发布描述", SEGMENTS)
    assert "## 发布描述" in rendered
    assert "很多时候，我们不是缺少工具。" in rendered
    assert "而是缺少一条可以重复执行的路径。" in rendered
```

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest tests/unit/test_transcript.py -q`  
Expected: FAIL on missing formatter functions.

- [ ] **Step 3: Implement pure formatters and atomic artifact writing**

`render_txt` joins trimmed segment text with newline. `render_srt` uses half-up millisecond conversion. `render_markdown` creates a description section and paragraph groups at sentence punctuation, a silence gap of at least 1.2 seconds, or 180 Chinese characters. `write_artifacts` writes `.tmp` siblings, fsyncs, then uses `Path.replace`.

- [ ] **Step 4: Verify GREEN**

Run: `python3 -m pytest tests/unit/test_transcript.py -q`  
Expected: exact snapshots pass without locale dependence.

- [ ] **Step 5: Commit**

```bash
git add src/video_recover/transcript.py tests/unit/test_transcript.py
git commit -m "feat: generate transcript artifacts"
```

## Task 5: Resilient parser adapter chain

**Files:**
- Create: `src/video_recover/parsers.py`
- Create: `tests/fixtures/yt_dlp_info.json`
- Create: `tests/fixtures/douyin_page.json`
- Create: `tests/integration/test_parsers.py`
- Create: `NOTICE`

- [ ] **Step 1: Write parser mapping and fallback tests**

```python
def test_chain_falls_back_after_recoverable_parser_error():
    first = StubParser(error=ParserChanged("yt-dlp changed"))
    second = StubParser(result=MEDIA)
    assert ParserChain([first, second]).resolve(TEST_URL, cookie=None) == MEDIA
    assert first.calls == 1
    assert second.calls == 1


def test_auth_error_is_reported_when_all_parsers_require_cookie():
    chain = ParserChain([StubParser(error=CookieRequired()), StubParser(error=CookieRequired())])
    with pytest.raises(CookieRequired):
        chain.resolve(TEST_URL, cookie=None)
```

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest tests/integration/test_parsers.py -q`  
Expected: FAIL because parser adapters are missing.

- [ ] **Step 3: Implement `YtDlpParser`, `DouyinPageParser` and error mapping**

`YtDlpParser` calls `YoutubeDL.extract_info(download=False)` with a mobile-compatible user agent, no playlist, quiet logging, a sanitized logger, optional Cookie header, and format preference `best[ext=mp4]/best`.

`DouyinPageParser` fetches the canonical page with HTTPX and extracts embedded JSON only from known script elements (`RENDER_DATA` and `__UNIVERSAL_DATA_FOR_REHYDRATION__`). It recursively locates the matching aweme ID, chooses the highest bitrate playable MP4 URL, and never executes remote JavaScript. Fixture JSON is synthetic and contains no real Cookie.

```python
@dataclass(frozen=True)
class ResolvedMedia:
    aweme_id: str
    canonical_url: str
    media_url: str
    description: str
    author: str
    duration_seconds: float | None
    cover_url: str | None
    request_headers: Mapping[str, str]
```

Attribute the inspected MIT parser reference in `NOTICE`; do not copy its unrelated batch, comment, account, or browser code.

- [ ] **Step 4: Verify GREEN and prove secrets are absent from errors**

Run: `python3 -m pytest tests/integration/test_parsers.py -q`  
Expected: fallback, fixture mapping, Cookie-required mapping, and sanitization tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/video_recover/parsers.py tests/fixtures tests/integration/test_parsers.py NOTICE
git commit -m "feat: resolve douyin media with parser fallback"
```

## Task 6: Resumable safe downloader

**Files:**
- Create: `src/video_recover/downloader.py`
- Create: `tests/integration/test_downloader.py`

- [ ] **Step 1: Write HTTP range, size and atomic-file tests**

```python
def test_resumes_part_file_with_range_header(tmp_path, recording_transport):
    target = tmp_path / "video.mp4"
    target.with_suffix(".mp4.part").write_bytes(b"first")
    download_file(MEDIA, target, client=httpx.Client(transport=recording_transport))
    assert recording_transport.requests[0].headers["Range"] == "bytes=5-"
    assert target.read_bytes() == b"first-rest"
    assert not target.with_suffix(".mp4.part").exists()


def test_rejects_media_larger_than_limit(tmp_path, oversized_transport):
    with pytest.raises(DownloadTooLarge):
        download_file(MEDIA, tmp_path / "video.mp4", client=httpx.Client(transport=oversized_transport), max_bytes=10)
```

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest tests/integration/test_downloader.py -q`  
Expected: FAIL on missing downloader.

- [ ] **Step 3: Implement streaming download and disk guard**

Reject non-HTTPS media URLs unless the host exactly matches an allowlisted Douyin CDN suffix resolved from parser output. Check `Content-Length`, free disk threshold, chunked bytes and configured maximum. If the server ignores `Range`, truncate the part file and restart once. Fsync before replacing the final target.

- [ ] **Step 4: Verify GREEN**

Run: `python3 -m pytest tests/integration/test_downloader.py -q`  
Expected: resume, restart, oversize, timeout, disk guard and atomic completion pass.

- [ ] **Step 5: Commit**

```bash
git add src/video_recover/downloader.py tests/integration/test_downloader.py
git commit -m "feat: download video artifacts safely"
```

## Task 7: Application service and persistent job runner

**Files:**
- Create: `src/video_recover/service.py`
- Create: `src/video_recover/runner.py`
- Create: `src/video_recover/transcribers.py`
- Create: `tests/integration/test_service.py`
- Create: `tests/integration/test_runner.py`

- [ ] **Step 1: Write end-to-end mocked pipeline tests**

```python
def test_runner_persists_video_then_waits_for_native_transcription(app_context):
    task = app_context.service.submit(TEST_URL)
    app_context.runner.run_once()
    saved = app_context.repository.get_task(task.id)
    assert saved.status == TaskStatus.AWAITING_TRANSCRIPTION
    assert (saved.output_dir / "video.mp4").exists()
    assert (saved.output_dir / "metadata.json").exists()
    assert (saved.output_dir / "description.txt").exists()


def test_transcription_failure_preserves_video_and_marks_partial(app_context):
    task = app_context.seed_downloaded_task()
    app_context.transcriber.fail_with(TranscriptionFailed("decoder failed"))
    app_context.runner.run_once()
    saved = app_context.repository.get_task(task.id)
    assert saved.status == TaskStatus.PARTIAL
    assert (saved.output_dir / "video.mp4").exists()
```

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest tests/integration/test_service.py tests/integration/test_runner.py -q`  
Expected: FAIL on missing application service and runner.

- [ ] **Step 3: Implement one orchestration boundary and one-concurrency loop**

`VideoService` owns `submit`, `get`, `list`, `retry`, `delete`, `save_cookie`, `cookie_status`, `lease_transcription`, `heartbeat_lease`, and `complete_transcription`. `JobRunner` only asks the repository for the next runnable task, executes one stage, and records categorized errors.

```python
def run_once(self) -> bool:
    task = self.repository.claim_next_pipeline_task()
    if task is None:
        self.repository.recover_expired_leases()
        return False
    try:
        self._run_current_stage(task)
    except UserFacingError as exc:
        self.service.record_failure(task.id, exc)
    except Exception:
        self.logger.exception("pipeline stage failed", extra={"task_id": task.id})
        self.service.record_failure(task.id, InternalFailure())
    return True
```

Startup recovery must be idempotent. CPU fallback becomes eligible only when no native heartbeat has been seen for the configured timeout.

- [ ] **Step 4: Verify GREEN and runner restart behavior**

Run: `python3 -m pytest tests/integration/test_service.py tests/integration/test_runner.py -q`  
Expected: mocked full pipeline, partial output, dedupe, retry, recovery and offline fallback pass.

- [ ] **Step 5: Commit**

```bash
git add src/video_recover/service.py src/video_recover/runner.py src/video_recover/transcribers.py tests/integration/test_service.py tests/integration/test_runner.py
git commit -m "feat: orchestrate persistent recovery jobs"
```

## Task 8: Native MLX worker protocol and model lifecycle

**Files:**
- Create: `src/video_recover_mac/__init__.py`
- Create: `src/video_recover_mac/config.py`
- Create: `src/video_recover_mac/client.py`
- Create: `src/video_recover_mac/transcriber.py`
- Create: `src/video_recover_mac/main.py`
- Create: `tests/unit/test_mac_transcriber.py`
- Create: `tests/integration/test_mac_worker.py`

- [ ] **Step 1: Write lazy-load, unload and heartbeat tests**

```python
def test_model_loads_on_first_job_and_unloads_after_idle(fake_mlx, clock):
    transcriber = LazyMlxTranscriber(fake_mlx.load, idle_seconds=600, clock=clock)
    transcriber.transcribe(Path("sample.wav"))
    assert fake_mlx.load_calls == 1
    clock.advance(seconds=601)
    assert transcriber.unload_if_idle() is True
    assert fake_mlx.model_closed is True


def test_worker_heartbeats_while_transcribing(worker_harness):
    worker_harness.run_one_lease()
    assert worker_harness.client.heartbeat_calls >= 1
    assert worker_harness.client.completed_segments == SEGMENTS
```

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest tests/unit/test_mac_transcriber.py tests/integration/test_mac_worker.py -q`  
Expected: FAIL on missing native worker package.

- [ ] **Step 3: Implement the host-polling worker**

The client calls `/internal/worker/lease`, `/internal/worker/{lease_id}/heartbeat`, and `/internal/worker/{lease_id}/complete` with a Bearer token. A relative media path from the server is resolved under a configured data root and rejected if `Path.resolve()` escapes it. Heartbeats run in a helper thread and stop in `finally`.

`LazyMlxTranscriber` imports `mlx_whisper` only inside its loader, uses a quantized `mlx-community/whisper-large-v3-turbo` model, requests Chinese transcription with word timestamps, and maps output to shared `Segment` objects.

- [ ] **Step 4: Verify GREEN without requiring MLX in CI**

Run: `python3 -m pytest tests/unit/test_mac_transcriber.py tests/integration/test_mac_worker.py -q`  
Expected: fake-model tests pass on Linux and macOS; the real MLX smoke test is marked `mac_live`.

- [ ] **Step 5: Commit**

```bash
git add src/video_recover_mac tests/unit/test_mac_transcriber.py tests/integration/test_mac_worker.py
git commit -m "feat: add native mlx transcription worker"
```

## Task 9: REST and internal worker APIs

**Files:**
- Create: `src/video_recover/api.py`
- Create: `tests/integration/test_api.py`
- Modify: `src/video_recover/main.py`

- [ ] **Step 1: Write request/response, Cookie masking and auth tests**

```python
def test_submit_is_async_and_returns_task(client):
    response = client.post("/api/tasks", json={"url": TEST_URL, "transcribe": True})
    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_cookie_value_never_returns_from_api(client):
    client.put("/api/settings/cookie", json={"cookie": "sessionid=top-secret"})
    payload = client.get("/api/status").json()
    assert payload["cookie"]["configured"] is True
    assert "top-secret" not in str(payload)


def test_worker_endpoint_requires_token(client):
    assert client.post("/internal/worker/lease").status_code == 401
```

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest tests/integration/test_api.py -q`  
Expected: FAIL because REST routes are not mounted.

- [ ] **Step 3: Implement typed endpoints and consistent errors**

Create Pydantic request/response models. REST endpoints are `/api/tasks`, `/api/tasks/{id}`, `/api/tasks/{id}/retry`, `/api/tasks/{id}` DELETE, `/api/tasks/{id}/artifacts/{format}`, `/api/status`, and `/api/settings/cookie`. Internal routes compare the token with `secrets.compare_digest` and return no stack traces.

- [ ] **Step 4: Verify GREEN and OpenAPI generation**

Run: `python3 -m pytest tests/integration/test_api.py -q`  
Expected: API contract, 404/409 mappings, Cookie masking and worker auth tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/video_recover/api.py src/video_recover/main.py tests/integration/test_api.py
git commit -m "feat: expose recovery and worker APIs"
```

## Task 10: Shared MCP tools over HTTP and stdio

**Files:**
- Create: `src/video_recover/mcp_server.py`
- Create: `src/video_recover/mcp_stdio.py`
- Create: `tests/integration/test_mcp.py`
- Modify: `src/video_recover/main.py`

- [ ] **Step 1: Write MCP tool inventory and parity tests**

```python
EXPECTED_TOOLS = {
    "submit_video", "get_task", "list_videos", "get_metadata",
    "get_transcript", "retry_task", "get_service_status",
}


async def test_mcp_exposes_safe_tool_set(mcp_client):
    tools = {tool.name for tool in await mcp_client.list_tools()}
    assert tools == EXPECTED_TOOLS
    assert "delete_video" not in tools
    assert "set_cookie" not in tools


async def test_submit_tool_returns_same_task_as_rest(mcp_client, repository):
    result = await mcp_client.call_tool("submit_video", {"url": TEST_URL})
    assert repository.get_task(result.structuredContent["task_id"]).source == "mcp"
```

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest tests/integration/test_mcp.py -q`  
Expected: FAIL because MCP server is missing.

- [ ] **Step 3: Register tools once and connect both transports**

```python
def build_mcp(service: VideoService) -> FastMCP:
    mcp = FastMCP(
        "video-recover",
        instructions=(
            "Video downloads and transcription are asynchronous. Call submit_video, "
            "then poll get_task until completed or partial before reading artifacts."
        ),
    )
    register_tools(mcp, service)
    return mcp
```

Mount Streamable HTTP under `/mcp`. The stdio module builds the same MCP instance from `Settings`, writes protocol data only to stdout, and writes logs only to stderr. Mark query tools read-only; mark submit/retry as non-destructive writes.

- [ ] **Step 4: Verify HTTP and stdio GREEN**

Run: `python3 -m pytest tests/integration/test_mcp.py -q`  
Expected: tool inventory, annotations, structured results, asynchronous behavior and stdio framing pass.

- [ ] **Step 5: Commit**

```bash
git add src/video_recover/mcp_server.py src/video_recover/mcp_stdio.py src/video_recover/main.py tests/integration/test_mcp.py
git commit -m "feat: expose video recovery through mcp"
```

## Task 11: Production Web management interface

**Files:**
- Create: `src/video_recover/templates/index.html`
- Create: `src/video_recover/static/app.css`
- Create: `src/video_recover/static/app.js`
- Create: `tests/integration/test_web.py`
- Create: `tests/e2e/test_web_ui.py`
- Modify: `src/video_recover/main.py`

- [ ] **Step 1: Write page semantics and browser-flow tests**

```python
def test_home_contains_primary_workflow(client):
    html = client.get("/").text
    assert "抖存" in html
    assert 'id="video-url"' in html
    assert 'id="task-list"' in html
    assert 'id="transcript-tabs"' in html


def test_submit_and_view_transcript(page, running_app, completed_fixture):
    page.goto(running_app.url)
    page.get_by_label("抖音视频链接").fill(TEST_URL)
    page.get_by_role("button", name="开始解析").click()
    page.get_by_text("排队中").wait_for()
```

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest tests/integration/test_web.py -q`  
Expected: FAIL because template and static assets do not exist.

- [ ] **Step 3: Implement the approved archive-desk design**

Use semantic HTML, keyboard-visible focus, status text in addition to color, `aria-live` for progress, and a mobile single-column breakpoint. JavaScript uses the REST API, abortable polling, `navigator.clipboard`, native `<video>`, and explicit delete confirmation. Cookie input is `type=password`, never re-populated, and cleared after save.

Visual tokens must match the approved mockup: warm paper, ink, vermilion state accent, acid-yellow primary action, narrow archive rail, asymmetric list/detail workspace, no generic card grid and no decorative glass effects.

- [ ] **Step 4: Run page and browser tests**

Run: `python3 -m pytest tests/integration/test_web.py -q && python3 -m pytest tests/e2e/test_web_ui.py -q`  
Expected: semantic contract and desktop/mobile workflows pass with no console errors.

- [ ] **Step 5: Commit**

```bash
git add src/video_recover/templates src/video_recover/static src/video_recover/main.py tests/integration/test_web.py tests/e2e/test_web_ui.py
git commit -m "feat: add local media archive web desk"
```

## Task 12: Docker, launchd and client installation scripts

**Files:**
- Create: `Dockerfile`
- Create: `compose.yaml`
- Create: `.env.example`
- Create: `scripts/dev-up.sh`
- Create: `scripts/dev-down.sh`
- Create: `scripts/dev-check.sh`
- Create: `scripts/install-mac-worker.sh`
- Create: `scripts/uninstall-mac-worker.sh`
- Create: `scripts/install-mcp.sh`
- Create: `deploy/com.codesfly.video-recover.worker.plist`
- Create: `tests/e2e/test_install_scripts.py`

- [ ] **Step 1: Write shell contract tests before scripts**

```python
def test_compose_binds_only_loopback(compose_config):
    ports = compose_config["services"]["app"]["ports"]
    assert ports == ["127.0.0.1:8787:8787"]
    assert compose_config["services"]["app"]["restart"] == "unless-stopped"


def test_launch_agent_runs_as_background_user_process(plist):
    assert plist["Label"] == "com.codesfly.video-recover.worker"
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["ProcessType"] == "Background"
```

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest tests/e2e/test_install_scripts.py -q`  
Expected: FAIL because deployment artifacts are absent.

- [ ] **Step 3: Implement idempotent operational artifacts**

Use a multi-stage Python 3.12 slim image, install FFmpeg and health tooling, create an unprivileged app user, and declare the health check. Compose mounts `${VIDEO_RECOVER_DATA_DIR}` to `/data`, loads `.env`, and applies conservative CPU/memory limits.

`dev-up.sh` creates a random Worker token with `openssl rand -hex 32` only when absent, resolves an absolute data path, builds before replacing the running container, starts detached, and calls `dev-check.sh`.

`install-mac-worker.sh` creates a venv beneath `~/Library/Application Support/VideoRecover`, installs `.[mac]`, substitutes absolute executable/data paths into the plist, uses `launchctl bootstrap gui/$(id -u)`, and is safe to rerun. Uninstall removes the LaunchAgent and venv only after confirmation while retaining models and downloaded data.

`install-mcp.sh` prints and optionally runs:

```bash
codex mcp add video-recover --url http://127.0.0.1:8787/mcp
```

It also emits the exact Claude Desktop stdio JSON using the absolute Docker and Compose paths; it backs up any existing config before merging.

- [ ] **Step 4: Verify script contracts and shell syntax**

Run: `python3 -m pytest tests/e2e/test_install_scripts.py -q && bash -n scripts/*.sh && docker compose config -q`  
Expected: all configuration and idempotency contract tests pass.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile compose.yaml .env.example scripts deploy tests/e2e/test_install_scripts.py
git commit -m "build: package docker and mac services"
```

## Task 13: Full verification, documentation and GitHub publication

**Files:**
- Create: `README.md`
- Create: `LICENSE`
- Create: `tests/e2e/test_live_douyin.py`
- Modify: `.gitignore`
- Modify: `docs/superpowers/plans/2026-07-17-video-recover-implementation.md`

- [ ] **Step 1: Add an opt-in live acceptance test**

```python
@pytest.mark.live
def test_requested_douyin_video_completes(live_client):
    task = live_client.submit("https://www.douyin.com/video/7662212894569811235")
    completed = live_client.wait(task.id, timeout=1800)
    assert completed.status == "completed"
    assert completed.artifact("video").stat().st_size > 0
    assert completed.artifact("description").read_text(encoding="utf-8").strip()
    assert completed.artifact("txt").read_text(encoding="utf-8").strip()
    assert "-->" in completed.artifact("srt").read_text(encoding="utf-8")
    assert "## 视频文案" in completed.artifact("markdown").read_text(encoding="utf-8")
```

- [ ] **Step 2: Write operator documentation**

README must cover prerequisites, `./scripts/dev-up.sh`, Cookie extraction without screenshots that expose secrets, Web usage, output paths, MLX installation, CPU fallback, Codex setup, Claude Desktop setup, start/stop/update, backup, logs, health checks, safe deletion, limitations and authorized-use notice. Include concrete troubleshooting for Cookie required, parser changed, Worker offline, model download, low disk and Docker resource pressure.

- [ ] **Step 3: Run the complete automated suite**

Run: `python3 -m pytest -m "not live and not mac_live" -q && python3 -m ruff check src tests`  
Expected: all non-live tests pass and Ruff reports no violations.

- [ ] **Step 4: Build and verify Docker without interrupting an existing service**

Run: `docker compose build && docker compose up -d && ./scripts/dev-check.sh`  
Expected: image builds natively for ARM64, container becomes healthy, Web returns 200, `/healthz` returns `{"status":"ok","storage":"ok"}`, MCP initializes, and logs contain no Cookie or token.

- [ ] **Step 5: Verify persistence and both MCP transports**

Run: `docker compose restart && ./scripts/dev-check.sh`  
Expected: task history and settings survive restart.

Run: `codex mcp add video-recover --url http://127.0.0.1:8787/mcp && codex mcp get video-recover`  
Expected: Codex reports an enabled Streamable HTTP server.

Run: `docker compose exec -T app python -m video_recover.mcp_stdio` through the MCP Inspector smoke client.  
Expected: Claude-compatible stdio initialization and `tools/list` return the same seven tools.

- [ ] **Step 6: Install and verify the native MLX Worker**

Run: `./scripts/install-mac-worker.sh && launchctl print gui/$(id -u)/com.codesfly.video-recover.worker`  
Expected: LaunchAgent state is running, the Web status reports a recent native Worker heartbeat, and the model is not loaded before a task exists.

- [ ] **Step 7: Run the requested live URL acceptance test**

Run: `VIDEO_RECOVER_LIVE=1 python3 -m pytest tests/e2e/test_live_douyin.py -m live -q -s`  
Expected: the requested video and all four text artifacts exist and pass content checks. If Douyin returns a Cookie-required classification, save a valid Cookie through Web, retry the same task, and rerun this exact test. The test must never print the Cookie.

- [ ] **Step 8: Inspect the final diff and secrets**

Run: `git diff --check && git status --short && rg -n "sessionid=|__ac_signature=|WORKER_TOKEN=.*[A-Fa-f0-9]{32}" --glob '!tests/**' --glob '!.env.example' .`  
Expected: no whitespace errors, only intended files are changed, and no real secret matches are found.

- [ ] **Step 9: Commit final documentation and verification assets**

```bash
git add README.md LICENSE .gitignore tests/e2e/test_live_douyin.py docs/superpowers/plans/2026-07-17-video-recover-implementation.md
git commit -m "docs: add operations and acceptance guide"
```

- [ ] **Step 10: Publish the verified main branch**

The target repository was confirmed to exist with no refs. Use SSH because the local GitHub CLI token is invalid:

```bash
git remote add origin git@github.com:codesfly/video_recover.git
git push -u origin main
git ls-remote --heads origin main
```

Expected: push succeeds and `refs/heads/main` points to the local `git rev-parse HEAD` commit.

## Final completion audit

Before reporting completion, inspect evidence for every spec section:

- [ ] Web, REST, HTTP MCP and stdio MCP all use one persisted task record.
- [ ] Public parsing works or produces a correct Cookie-required action; Cookie retry works.
- [ ] MP4, description, metadata, TXT, SRT and Markdown are present for the requested URL.
- [ ] MLX Worker is persistent, lazy, single-concurrency and reports heartbeat.
- [ ] Container CPU fallback is tested without loading during normal native operation.
- [ ] Restart retains database, Cookie status, models and artifacts.
- [ ] Web binds only to localhost and secrets are absent from response bodies and logs.
- [ ] Codex and Claude Desktop transports expose exactly the approved safe tools.
- [ ] All tests and deployment checks passed from fresh commands, not earlier cached claims.
- [ ] Remote `codesfly/video_recover` main matches the verified local commit.
