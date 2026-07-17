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
    cookieForm: document.querySelector("#cookie-form"),
    cookieInput: document.querySelector("#douyin-cookie"),
    cookieBadge: document.querySelector("#cookie-badge"),
    cookieMessage: document.querySelector("#cookie-message"),
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
    elements.cookieBadge.textContent = status.cookie.configured ? "已加密保存" : "未设置";
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
      elements.submitMessage.textContent = `${error.message} 请检查链接，或更新 Cookie 后重试。`;
    } finally {
      elements.submitButton.disabled = false;
    }
  }

  async function saveCookie(event) {
    event.preventDefault();
    const button = elements.cookieForm.querySelector("button");
    button.disabled = true;
    elements.cookieMessage.textContent = "正在加密保存…";
    try {
      await api("/api/settings/cookie", {
        method: "PUT",
        body: JSON.stringify({ cookie: elements.cookieInput.value }),
      });
      elements.cookieInput.value = "";
      elements.cookieBadge.textContent = "已加密保存";
      elements.cookieMessage.textContent = "访问凭据已保存在本机。";
      showToast("Cookie 已加密保存");
    } catch (error) {
      elements.cookieMessage.textContent = `${error.message} 请重新粘贴后保存。`;
    } finally {
      button.disabled = false;
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
    elements.cookieForm.addEventListener("submit", saveCookie);
    elements.retryButton.addEventListener("click", retrySelected);
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
