# Balanced Web Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the visually dense VideoRecover archive page with the approved compact balanced workspace while preserving every API-backed Web feature.

**Architecture:** Keep the FastAPI/Jinja delivery model and the existing polling/API functions. Recompose the template into a compact header, capture bar, task rail, and two-column detail stage; make only the small JavaScript change needed to remove decorative task numbering. Replace the page-specific CSS with a smaller responsive system that retains the paper/vermilion identity and all accessibility behavior.

**Tech Stack:** FastAPI, Jinja2, vanilla JavaScript, modern CSS, pytest, Playwright with installed Google Chrome.

---

## File map

- `tests/integration/test_web.py`: server-rendered structure and static asset contracts.
- `tests/e2e/test_web_ui.py`: desktop submission, compact layout, settings disclosure, and mobile overflow behavior.
- `src/video_recover/templates/index.html`: semantic workspace structure and all stable DOM IDs used by JavaScript.
- `src/video_recover/static/app.js`: task item rendering and existing API/polling interactions.
- `src/video_recover/static/app.css`: compact paper-archive visual system and responsive layout.

### Task 1: Lock the approved UI contract with failing tests

**Files:**
- Modify: `tests/integration/test_web.py`
- Modify: `tests/e2e/test_web_ui.py`

- [x] **Step 1: Replace the page structure assertions**

Replace `test_archive_page_has_accessible_controls_and_empty_state` with:

```python
def test_archive_page_has_compact_workspace_and_accessible_controls(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert "VideoRecover" in response.text
    assert 'class="capture-bar"' in response.text
    assert 'aria-label="最近任务"' in response.text
    assert 'class="record-menu"' in response.text
    assert 'for="video-url"' in response.text
    assert 'id="task-status"' in response.text
    assert 'aria-live="polite"' in response.text
    assert "解析第一条抖音视频" in response.text
    assert "下载文件" in response.text
    assert "NEW ARCHIVE" not in response.text
    assert "YOUR LOCAL COLLECTION" not in response.text
    assert "EXPORT / 导出产物" not in response.text
```

- [x] **Step 2: Extend the asset contract for the balanced layout**

Add these assertions after the existing reduced-motion/container checks:

```python
    assert ".capture-bar" in stylesheet.text
    assert ".archive-layout" in stylesheet.text
    assert ".record-menu" in stylesheet.text
    assert ".task-index" not in stylesheet.text
```

- [x] **Step 3: Update the browser flow to the new hierarchy**

In `test_desktop_submit_and_mobile_layout_have_no_browser_errors`, replace the old heading and numbered-task assertions and add compactness/settings checks:

```python
                page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
                assert page.get_by_role(
                    "heading",
                    name="解析一条抖音视频",
                ).is_visible()
                capture_height = page.locator(".capture-bar").bounding_box()["height"]
                assert capture_height < 190
                page.get_by_label("抖音视频链接").fill(TEST_URL)
                page.get_by_role("button", name="开始归档").click()
                page.get_by_role(
                    "button",
                    name="待解析视频 7662212894569811235",
                ).wait_for()
                assert page.get_by_text("已入队，后台会自动处理。").is_visible()

                page.get_by_text("设置", exact=True).click()
                cookie_input = page.get_by_label("Cookie")
                assert cookie_input.get_attribute("type") == "password"
                assert cookie_input.input_value() == ""

                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate(
                    "document.documentElement.scrollWidth "
                    "=== document.documentElement.clientWidth"
                )
                assert console_errors == []
```

- [x] **Step 4: Run the focused tests and confirm the new contract fails**

Run:

```bash
.venv/bin/pytest tests/integration/test_web.py tests/e2e/test_web_ui.py -q
```

Expected: integration and browser assertions fail because the page still contains `capture-strip`, the old large heading, task indices, and the old export label.

### Task 2: Recompose the template and task renderer

**Files:**
- Modify: `src/video_recover/templates/index.html`
- Modify: `src/video_recover/static/app.js`

- [x] **Step 1: Replace the masthead and capture hero**

Use a compact `.masthead` containing the existing wordmark IDs, `#service-indicator`, and a native `.settings-sheet`. Move the existing `#cookie-form`, `#douyin-cookie`, `#cookie-badge`, and `#cookie-message` into the settings disclosure. Replace `.capture-strip` with:

```html
<section class="capture-bar" aria-labelledby="capture-title">
  <div class="capture-heading">
    <p>新建归档</p>
    <h1 id="capture-title">解析一条抖音视频</h1>
  </div>
  <form id="submit-form" class="capture-form">
    <label class="sr-only" for="video-url">抖音视频链接</label>
    <input id="video-url" name="url" type="url" inputmode="url" autocomplete="url"
      spellcheck="false" placeholder="粘贴抖音视频链接" required>
    <label class="check-control">
      <input id="transcribe" name="transcribe" type="checkbox" checked>
      <span>提取语音</span>
    </label>
    <button class="button button-primary" id="submit-button" type="submit">开始归档</button>
    <p id="submit-message" class="form-message" role="status" aria-live="polite"></p>
  </form>
</section>
```

- [x] **Step 2: Replace the archive rail headings and empty state**

Keep `#task-list`, `#task-count`, and `#list-empty`, but use `aria-label="最近任务"`, the visible heading `最近任务`, and one compact empty action labeled `解析第一条抖音视频`.

- [x] **Step 3: Integrate status and secondary actions into the detail header**

Move `#task-status`, `#status-message`, and `#status-progress` into `.record-header`. Put `#retry-button` and `#delete-button` inside:

```html
<details class="record-menu">
  <summary class="button button-quiet">更多</summary>
  <div class="record-menu-panel">
    <button class="text-action" id="retry-button" type="button" hidden>重新处理</button>
    <button class="text-action text-danger" id="delete-button" type="button">删除馆藏</button>
  </div>
</details>
```

Move `.artifact-section` below `.record-grid`, rename its visible heading to `下载文件`, and retain all six `data-artifact` links.

- [x] **Step 4: Simplify the empty state and footer**

Use a single heading `还没有归档视频`, one explanatory sentence, and the `解析第一条抖音视频` focus action. Combine the footer content into one line without removing the version or local/MCP message.

- [x] **Step 5: Remove decorative task numbering from JavaScript**

Replace `createTaskItem` and its caller with:

```javascript
  function createTaskItem(task) {
    const item = document.createElement("li");
    item.className = "task-item";

    const button = document.createElement("button");
    button.className = "task-button";
    button.type = "button";
    button.dataset.taskId = task.id;
    button.dataset.complete = String(task.status === "completed");
    button.setAttribute("aria-current", String(task.id === state.selectedId));

    const copy = document.createElement("span");
    copy.className = "task-copy";
    const title = document.createElement("span");
    title.className = "task-title";
    title.textContent = titleFor(task);
    const meta = document.createElement("span");
    meta.className = "task-meta";
    meta.textContent = `${STATUS_LABELS[task.status] || task.status} · ${formatDate(task.created_at)}`;
    copy.append(title, meta);

    const mark = document.createElement("span");
    mark.className = "task-status-mark";
    mark.setAttribute("aria-hidden", "true");

    button.append(copy, mark);
    button.addEventListener("click", () => selectTask(task.id));
    item.append(button);
    return item;
  }

  function renderTaskList() {
    const fragment = document.createDocumentFragment();
    state.tasks.forEach((task) => fragment.append(createTaskItem(task)));
    elements.taskList.replaceChildren(fragment);
    elements.taskCount.textContent = String(state.tasks.length);
    elements.listEmpty.hidden = state.tasks.length > 0;
  }
```

### Task 3: Implement the compact responsive visual system

**Files:**
- Modify: `src/video_recover/static/app.css`

- [x] **Step 1: Reduce the design tokens and global chrome**

Keep the existing paper, ink, vermilion, success, display-font, and body-font tokens. Remove `--text-display`, large hero spacing, the background grid, decorative stamp styles, and task-index rules. Keep visible focus styles and `[hidden]` behavior.

- [x] **Step 2: Implement the desktop workspace**

Use these governing layout rules:

```css
.capture-bar {
  display: grid;
  grid-template-columns: minmax(10rem, 0.28fr) minmax(0, 1fr);
  align-items: center;
  gap: 1.5rem;
  padding: 1.25rem clamp(1rem, 3vw, 2.5rem);
  border-bottom: 1px solid var(--rule);
}

.archive-layout {
  display: grid;
  grid-template-columns: minmax(14rem, 17rem) minmax(0, 1fr);
  min-height: calc(100vh - 11rem);
}

@container (min-width: 48rem) {
  .record-grid {
    grid-template-columns: minmax(14rem, 30%) minmax(0, 1fr);
  }
}
```

Style the selected task with a vermilion left edge plus a subtle surface fill. Keep the record title below `clamp(1.5rem, 3vw, 2.5rem)`. Limit the video to a practical desktop height and reduce gaps between copy sections.

- [x] **Step 3: Make exports and settings compact**

Render `.artifact-links` as a wrapping row of six text links separated by subtle rules. Position `.settings-sheet` as a desktop dropdown anchored to the masthead, and make `.record-menu-panel` a small anchored action surface.

- [x] **Step 4: Add mobile reflow and motion safeguards**

Below `48rem`, stack the capture bar, archive rail, and record content; keep all controls at least 44px high and ensure long URLs/titles wrap or truncate without widening the document. Preserve the existing `@media (prefers-reduced-motion: reduce)` rule.

- [x] **Step 5: Run focused tests and lint until green**

Run:

```bash
.venv/bin/pytest tests/integration/test_web.py tests/e2e/test_web_ui.py -q
.venv/bin/ruff check src tests
```

Expected: all focused tests pass and Ruff reports `All checks passed!`.

- [x] **Step 6: Commit the tested implementation**

```bash
git add src/video_recover/templates/index.html src/video_recover/static/app.css \
  src/video_recover/static/app.js tests/integration/test_web.py tests/e2e/test_web_ui.py
git commit -m "feat: simplify web archive workspace"
```

### Task 4: Full verification and live browser acceptance

**Files:**
- Verify only; fix the files above if evidence reveals a defect.

- [x] **Step 1: Run the complete non-live suite**

Run:

```bash
.venv/bin/pytest -m 'not live and not mac_live' -q
```

Expected: 92 tests pass and only the explicitly marked live test is deselected.

- [x] **Step 2: Rebuild and restart the local Docker service from the completed branch**

After the tested branch has been fast-forwarded into the primary workspace, run from the primary workspace so its existing `.env` and data volume remain authoritative:

```bash
bash scripts/dev-up.sh
```

Expected: the script reports `健康检查、Web、MCP 与容器状态均正常。`.

- [x] **Step 3: Inspect the real completed task at desktop width**

Verify the capture bar is compact; recent tasks and detail share the screen; video, original copy, transcript, and all six download links are visible; settings and delete are secondary; console error count is zero.

- [x] **Step 4: Inspect at 390×844**

Verify document `scrollWidth === clientWidth`, controls reflow, and every retained content section remains reachable.

- [x] **Step 5: Review the final diff and requirements**

Run:

```bash
git diff main...HEAD --check
git status --short
```

Expected: no whitespace errors and a clean worktree.
