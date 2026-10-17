(() => {
  "use strict";

  const ACTIVE_STATES = new Set([
    "queued",
    "resolving",
    "downloading",
    "awaiting_transcription",
    "transcribing",
  ]);
  const RETRY_STATES = new Set(["failed", "partial"]);
  const COMMENT_ACTIVE_STATES = new Set(["queued", "running"]);
  const COMMENT_SNAPSHOT_STATES = new Set(["completed", "partial"]);
  const COMMENT_DEFAULT_META = "还没有评论快照。只拉一级评论，按点赞排序。";
  const VIDEO_STATES = new Set([
    "awaiting_transcription",
    "transcribing",
    "partial",
    "completed",
  ]);
  const STATUS_LABELS = {
    queued: "排队中",
    resolving: "正在解析",
    downloading: "正在下载",
    awaiting_transcription: "等待转写",
    transcribing: "正在转写",
    completed: "已归档",
    partial: "部分完成",
    failed: "处理失败",
    cancelled: "已取消",
  };

  const state = {
    tasks: [],
    selectedId: null,
    selectedTask: null,
    pollController: null,
    pollTimer: null,
    toastTimer: null,
    commentTaskId: null,
    commentJobId: null,
    commentRenderedKey: null,
  };

  const elements = {
    submitForm: document.querySelector("#submit-form"),
    submitButton: document.querySelector("#submit-button"),
    submitMessage: document.querySelector("#submit-message"),
    videoUrl: document.querySelector("#video-url"),
    transcribe: document.querySelector("#transcribe"),
    taskList: document.querySelector("#task-list"),
    taskCount: document.querySelector("#task-count"),
    listEmpty: document.querySelector("#list-empty"),
    recordEmpty: document.querySelector("#record-empty"),
    recordDetail: document.querySelector("#record-detail"),
    statusRibbon: document.querySelector("#task-status"),
    statusMessage: document.querySelector("#status-message"),
    statusProgress: document.querySelector("#status-progress"),
    serviceIndicator: document.querySelector("#service-indicator"),
    serviceLabel: document.querySelector("#service-label"),
    recordNumber: document.querySelector("#record-number"),
    recordTitle: document.querySelector("#record-title"),
    recordAuthor: document.querySelector("#record-author"),
    description: document.querySelector("#description-text"),
    transcript: document.querySelector("#transcript-text"),
    videoPlayer: document.querySelector("#video-player"),
    videoPending: document.querySelector("#video-pending"),
    factId: document.querySelector("#fact-id"),
    factDuration: document.querySelector("#fact-duration"),
    factDate: document.querySelector("#fact-date"),
    retryButton: document.querySelector("#retry-button"),
    deleteButton: document.querySelector("#delete-button"),
    deleteDialog: document.querySelector("#delete-dialog"),
    confirmDelete: document.querySelector("#confirm-delete"),
    toast: document.querySelector("#toast"),
    commentsFetch: document.querySelector("#comments-fetch"),
    commentsSnapshot: document.querySelector("#comments-snapshot"),
    commentsDownload: document.querySelector("#comments-download"),
    commentsMeta: document.querySelector("#comments-meta"),
    commentsList: document.querySelector("#comments-list"),
  };

  function errorMessage(payload, fallback) {
    if (typeof payload?.detail === "string") return payload.detail;
    if (typeof payload?.detail?.message === "string") return payload.detail.message;
    return fallback;
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    const response = await fetch(path, { ...options, headers });
    if (!response.ok) {
      let payload = null;
      try {
        payload = await response.json();
      } catch (_error) {
        payload = null;
      }
      const error = new Error(errorMessage(payload, `请求失败（${response.status}）`));
      error.status = response.status;
      throw error;
    }
    if (response.status === 204) return null;
    return response.json();
  }

  function showToast(message) {
    window.clearTimeout(state.toastTimer);
    elements.toast.textContent = message;
    elements.toast.hidden = false;
    state.toastTimer = window.setTimeout(() => {
      elements.toast.hidden = true;
    }, 3200);
  }

  function formatDate(value, withTime = false) {
    if (!value) return "—";
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
    }).format(new Date(value));
  }

  function titleFor(task) {
    if (task.aweme_id) return `抖音视频 ${task.aweme_id}`;
    const parts = task.canonical_url.split("/").filter(Boolean);
    return `待解析视频 ${parts.at(-1) || ""}`;
  }

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

  function setServiceStatus(status) {
    elements.serviceIndicator.classList.add("is-online");
    elements.serviceLabel.textContent = status.worker?.connected
      ? "本地服务在线 · MLX Worker 就绪"
      : "本地服务在线 · 等待 MLX Worker";
  }

  function setDisconnected() {
    elements.serviceIndicator.classList.remove("is-online");
    elements.serviceLabel.textContent = "本地服务暂时不可用";
  }

  async function loadOverview(signal) {
    const [tasks, serviceStatus] = await Promise.all([
      api("/api/tasks", { signal }),
      api("/api/status", { signal }),
    ]);
    state.tasks = tasks;
    setServiceStatus(serviceStatus);
    if (!state.selectedId && tasks.length > 0) state.selectedId = tasks[0].id;
    if (state.selectedId && !tasks.some((task) => task.id === state.selectedId)) {
      state.selectedId = tasks[0]?.id || null;
    }
    renderTaskList();
    if (state.selectedId) {
      const selected = tasks.find((task) => task.id === state.selectedId);
      if (selected) await renderRecord(selected, signal);
    } else {
      renderEmptyRecord();
    }
  }

  function renderEmptyRecord() {
    state.selectedTask = null;
    elements.recordEmpty.hidden = false;
    elements.recordDetail.hidden = true;
    elements.statusRibbon.classList.remove("is-active");
    elements.statusMessage.textContent = "等待选择馆藏";
    elements.statusProgress.textContent = "—";
    elements.videoPlayer.removeAttribute("src");
    elements.videoPlayer.load();
  }

  async function safeArtifactJson(taskId, artifact, signal) {
    try {
      const response = await fetch(`/api/tasks/${taskId}/artifacts/${artifact}`, { signal });
      if (!response.ok) return null;
      return response.json();
    } catch (error) {
      if (error.name === "AbortError") throw error;
      return null;
    }
  }

  async function safeArtifactText(taskId, artifact, signal) {
    try {
      const response = await fetch(`/api/tasks/${taskId}/artifacts/${artifact}`, { signal });
      if (!response.ok) return null;
      return response.text();
    } catch (error) {
      if (error.name === "AbortError") throw error;
      return null;
    }
  }

  function updateArtifactLinks(task) {
    document.querySelectorAll("[data-artifact]").forEach((link) => {
      const artifact = link.dataset.artifact;
      const needsTranscript = ["transcript", "srt", "markdown"].includes(artifact);
      const available = task.aweme_id && (!needsTranscript || task.status === "completed");
      link.classList.toggle("is-unavailable", !available);
      link.setAttribute("aria-disabled", String(!available));
      link.href = available ? `/api/tasks/${task.id}/artifacts/${artifact}` : "#";
    });
  }

  function formatShortDate(value) {
    if (!value) return "—";
    return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit" }).format(
      new Date(value),
    );
  }

  function snapshotLabel(job) {
    const complete = job.status === "completed" && !job.has_more_remaining;
    return `${formatDate(job.finished_at, true)} · ${job.fetched_count} 条 · ${complete ? "已拉全" : "未拉全"}`;
  }

  function createCommentItem(comment) {
    const item = document.createElement("li");
    item.className = "comment-item";
    const meta = document.createElement("span");
    meta.className = "comment-item-meta";
    meta.textContent = [
      `👍${comment.digg_count}`,
      `💬${comment.reply_count}`,
      formatShortDate(comment.created_at),
      comment.ip_label || "—",
    ].join(" · ");
    const text = document.createElement("p");
    text.className = "comment-item-text";
    text.textContent = comment.text;
    item.append(meta, text);
    return item;
  }

  function resetComments(taskId) {
    state.commentTaskId = taskId;
    state.commentJobId = null;
    state.commentRenderedKey = null;
    elements.commentsSnapshot.replaceChildren();
    elements.commentsSnapshot.disabled = true;
    elements.commentsSnapshot.hidden = true;
    elements.commentsDownload.hidden = true;
    elements.commentsList.replaceChildren();
    elements.commentsMeta.textContent = COMMENT_DEFAULT_META;
  }

  async function loadComments(task, signal) {
    if (state.commentTaskId !== task.id) resetComments(task.id);
    let jobs;
    try {
      jobs = await api(`/api/tasks/${task.id}/comments`, { signal });
    } catch (error) {
      if (error.name === "AbortError") throw error;
      return;
    }
    if (state.selectedId !== task.id) return;

    const active = jobs.find((job) => COMMENT_ACTIVE_STATES.has(job.status));
    const snapshots = jobs.filter((job) => COMMENT_SNAPSHOT_STATES.has(job.status));
    const latest = jobs[0];
    elements.commentsFetch.disabled = Boolean(active) || task.status === "cancelled";
    elements.commentsFetch.textContent = active ? "拉取中…" : snapshots.length ? "刷新快照" : "拉取评论";

    if (!snapshots.some((job) => job.id === state.commentJobId)) {
      state.commentJobId = snapshots[0]?.id || null;
    }
    const options = snapshots.map((job) => {
      const option = document.createElement("option");
      option.value = job.id;
      option.textContent = snapshotLabel(job);
      option.selected = job.id === state.commentJobId;
      return option;
    });
    elements.commentsSnapshot.replaceChildren(...options);
    elements.commentsSnapshot.disabled = snapshots.length < 2;
    elements.commentsSnapshot.hidden = snapshots.length === 0;
    elements.commentsDownload.hidden = snapshots.length === 0;
    elements.commentsDownload.href = `/api/tasks/${task.id}/comments/latest.md`;

    let note = "";
    if (active) note = "正在后台拉取评论…";
    else if (latest?.status === "failed") note = `上次拉取失败：${latest.error_message || "请稍后重试"}`;

    if (!state.commentJobId) {
      elements.commentsList.replaceChildren();
      elements.commentsMeta.textContent = note || COMMENT_DEFAULT_META;
      return;
    }
    const renderKey = `${task.id}:${state.commentJobId}`;
    if (state.commentRenderedKey !== renderKey) {
      let view;
      try {
        view = await api(`/api/tasks/${task.id}/comments/${state.commentJobId}?limit=100`, {
          signal,
        });
      } catch (error) {
        if (error.name === "AbortError") throw error;
        elements.commentsMeta.textContent = error.message;
        return;
      }
      if (state.selectedId !== task.id || `${task.id}:${state.commentJobId}` !== renderKey) return;
      elements.commentsList.replaceChildren(...view.comments.map(createCommentItem));
      elements.commentsList.dataset.total = String(view.total_in_snapshot);
      state.commentRenderedKey = renderKey;
    }
    const total = Number(elements.commentsList.dataset.total || 0);
    const shown = elements.commentsList.children.length;
    const summary = total > shown ? `共 ${total} 条，显示点赞最高的 ${shown} 条` : `共 ${total} 条，按点赞排序`;
    elements.commentsMeta.textContent = note ? `${summary} · ${note}` : summary;
  }

  async function fetchComments() {
    const taskId = state.selectedId;
    if (!taskId) return;
    elements.commentsFetch.disabled = true;
    try {
      await api(`/api/tasks/${taskId}/comments`, { method: "POST", body: JSON.stringify({}) });
      showToast("已开始拉取评论");
      await refreshNow();
    } catch (error) {
      showToast(error.message);
      elements.commentsFetch.disabled = false;
    }
  }

  async function renderRecord(task, signal) {
    state.selectedTask = task;
    elements.recordEmpty.hidden = true;
    elements.recordDetail.hidden = false;
    const active = ACTIVE_STATES.has(task.status);
    elements.statusRibbon.classList.toggle("is-active", active);
    elements.statusMessage.textContent = task.error_message || task.message;
    elements.statusProgress.textContent = `${task.progress}%`;
    elements.recordNumber.textContent = `档案 · ${(task.aweme_id || task.id).slice(-8)}`;
    elements.recordTitle.textContent = titleFor(task);
    elements.recordAuthor.textContent = "等待解析作者";
    elements.factId.textContent = task.aweme_id || "解析中";
    elements.factDuration.textContent = "—";
    elements.factDate.textContent = formatDate(task.created_at, true);
    elements.retryButton.hidden = !RETRY_STATES.has(task.status);
    elements.deleteButton.disabled = ["resolving", "downloading", "awaiting_transcription", "transcribing"].includes(task.status);
    elements.description.textContent = "等待解析发布文案。";
    elements.transcript.textContent = task.transcribe
      ? "等待原生 MLX Worker 转写。"
      : "此任务未请求语音转写。";

    const hasVideo = VIDEO_STATES.has(task.status) && task.aweme_id;
    elements.videoPending.hidden = Boolean(hasVideo);
    if (hasVideo) {
      const videoUrl = `/api/tasks/${task.id}/artifacts/video`;
      if (elements.videoPlayer.getAttribute("src") !== videoUrl) {
        elements.videoPlayer.src = videoUrl;
      }
    } else {
      elements.videoPlayer.removeAttribute("src");
      elements.videoPlayer.load();
    }
    updateArtifactLinks(task);
    await loadComments(task, signal);

    if (!task.aweme_id) return;
    const [metadata, transcript] = await Promise.all([
      safeArtifactJson(task.id, "metadata", signal),
      safeArtifactText(task.id, "transcript", signal),
    ]);
    if (state.selectedId !== task.id) return;
    if (metadata) {
      elements.recordTitle.textContent = metadata.description || titleFor(task);
      elements.recordAuthor.textContent = metadata.author ? `发布者 · ${metadata.author}` : "发布者未知";
      elements.description.textContent = metadata.description || "这条视频没有发布文案。";
      elements.factDuration.textContent = metadata.duration_seconds
        ? `${Math.round(metadata.duration_seconds)} 秒`
        : "—";
    }
    if (transcript) elements.transcript.textContent = transcript.trim();
  }

  async function selectTask(taskId) {
    state.selectedId = taskId;
    renderTaskList();
    const task = state.tasks.find((item) => item.id === taskId);
    if (task) await renderRecord(task);
  }

  async function submitVideo(event) {
    event.preventDefault();
    elements.submitButton.disabled = true;
    elements.submitMessage.textContent = "正在建立本地归档…";
    try {
      const task = await api("/api/tasks", {
        method: "POST",
        body: JSON.stringify({
          url: elements.videoUrl.value.trim(),
          transcribe: elements.transcribe.checked,
        }),
      });
      state.selectedId = task.id;
      elements.videoUrl.value = "";
      elements.submitMessage.textContent = "已入队，后台会自动处理。";
      showToast("馆藏任务已建立");
      await refreshNow();
    } catch (error) {
      elements.submitMessage.textContent = `${error.message} 请检查链接后重试。`;
    } finally {
      elements.submitButton.disabled = false;
    }
  }

  async function retrySelected() {
    if (!state.selectedId) return;
    elements.retryButton.disabled = true;
    try {
      await api(`/api/tasks/${state.selectedId}/retry`, { method: "POST" });
      showToast("任务已重新排队");
      await refreshNow();
    } catch (error) {
      showToast(error.message);
    } finally {
      elements.retryButton.disabled = false;
    }
  }

  async function deleteSelected() {
    if (!state.selectedId) return;
    const deletingId = state.selectedId;
    try {
      await api(`/api/tasks/${deletingId}`, { method: "DELETE" });
      state.selectedId = null;
      showToast("本地馆藏已永久删除");
      await refreshNow();
    } catch (error) {
      showToast(error.message);
    }
  }

  async function copyText(targetId) {
    const target = document.querySelector(`#${targetId}`);
    const text = target?.textContent?.trim();
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      showToast("文案已复制");
    } catch (_error) {
      showToast("无法访问剪贴板，请手动选择文案复制");
    }
  }

  function schedulePoll() {
    window.clearTimeout(state.pollTimer);
    state.pollTimer = window.setTimeout(poll, 2200);
  }

  async function poll() {
    state.pollController?.abort();
    state.pollController = new AbortController();
    try {
      await loadOverview(state.pollController.signal);
    } catch (error) {
      if (error.name !== "AbortError") setDisconnected();
    } finally {
      schedulePoll();
    }
  }

  async function refreshNow() {
    state.pollController?.abort();
    window.clearTimeout(state.pollTimer);
    await poll();
  }

  function bindEvents() {
    elements.submitForm.addEventListener("submit", submitVideo);
    elements.retryButton.addEventListener("click", retrySelected);
    elements.commentsFetch.addEventListener("click", fetchComments);
    elements.commentsSnapshot.addEventListener("change", () => {
      state.commentJobId = elements.commentsSnapshot.value || null;
      const task = state.tasks.find((item) => item.id === state.selectedId);
      if (task) loadComments(task).catch(() => {});
    });
    elements.deleteButton.addEventListener("click", () => elements.deleteDialog.showModal());
    elements.deleteDialog.addEventListener("close", () => {
      if (elements.deleteDialog.returnValue === "delete") deleteSelected();
    });
    document.querySelectorAll("[data-copy-target]").forEach((button) => {
      button.addEventListener("click", () => copyText(button.dataset.copyTarget));
    });
    document.querySelectorAll("[data-focus-url]").forEach((button) => {
      button.addEventListener("click", () => {
        elements.videoUrl.focus();
        elements.videoUrl.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    });
    window.addEventListener("beforeunload", () => {
      state.pollController?.abort();
      window.clearTimeout(state.pollTimer);
    });
  }

  bindEvents();
  poll();
})();
