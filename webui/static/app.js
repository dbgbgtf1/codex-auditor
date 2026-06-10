const state = {
  sessions: [],
  selectedSessionId: null,
  messages: [],
  theme: document.body.dataset.theme || (document.body.classList.contains("dark") ? "dark" : "light"),
  polling: null,
  thinkingTimer: null,
};

const els = {
  themeToggle: document.getElementById("themeToggle"),
  newSessionButton: document.getElementById("newSessionButton"),
  sessionForm: document.getElementById("sessionForm"),
  cancelSessionButton: document.getElementById("cancelSessionButton"),
  sessionName: document.getElementById("sessionName"),
  sessionIdentifier: document.getElementById("sessionIdentifier"),
  sessionList: document.getElementById("sessionList"),
  vulnerabilityMeta: document.getElementById("vulnerabilityMeta"),
  vulnerabilityRows: document.getElementById("vulnerabilityRows"),
  refreshVulnerabilities: document.getElementById("refreshVulnerabilities"),
  messages: document.getElementById("messages"),
  messageForm: document.getElementById("messageForm"),
  messageInput: document.getElementById("messageInput"),
  sendButton: document.getElementById("sendButton"),
  stopButton: document.getElementById("stopButton"),
  toast: document.getElementById("toast"),
};

function api(path, options = {}) {
  const headers = options.headers || {};
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  return fetch(`api${path}`, {
    ...options,
    headers,
    body: options.body && !(options.body instanceof FormData) ? JSON.stringify(options.body) : options.body,
  }).then(async (response) => {
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `${response.status} ${response.statusText}`);
    }
    return payload;
  });
}

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.remove("hidden");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => els.toast.classList.add("hidden"), 4200);
}

function applyTheme(theme) {
  state.theme = theme === "dark" ? "dark" : "light";
  document.body.classList.toggle("dark", state.theme === "dark");
  els.themeToggle.title = state.theme === "dark" ? "切换到亮色" : "切换到暗色";
}

function currentSession() {
  return state.sessions.find((session) => Number(session.id) === Number(state.selectedSessionId)) || null;
}

function isBusySession(session) {
  return session?.status === "running" || session?.status === "judging";
}

function setSelectedSession(sessionId, persist = true) {
  state.selectedSessionId = sessionId ? Number(sessionId) : null;
  renderSessions();
  renderChatShell();
  loadMessages();
  resetVulnerabilities();
  loadVulnerabilities();
  if (persist) {
    api("/settings", {
      method: "PATCH",
      body: { selected_session_id: state.selectedSessionId || "" },
    }).catch((error) => showToast(error.message));
  }
}

function renderSessions() {
  els.sessionList.innerHTML = "";
  if (!state.sessions.length) {
    const empty = document.createElement("div");
    empty.className = "session-meta";
    empty.textContent = "暂无会话";
    els.sessionList.appendChild(empty);
    return;
  }

  state.sessions.forEach((session) => {
    const item = document.createElement("div");
    item.className = `session-item${Number(session.id) === Number(state.selectedSessionId) ? " active" : ""}`;

    const select = document.createElement("button");
    select.type = "button";
    select.className = "session-select";
    select.addEventListener("click", () => setSelectedSession(session.id));

    const title = document.createElement("div");
    title.className = "session-title";
    const dot = document.createElement("span");
    dot.className = `status-dot ${session.status === "running" ? "running" : session.status === "judging" ? "judging" : ""}`;
    const name = document.createElement("span");
    name.className = "session-name";
    name.textContent = session.name;
    title.append(dot, name);

    const meta = document.createElement("div");
    meta.className = "session-meta";
    const codexId = session.codex_session_id ? session.codex_session_id.slice(0, 8) : "未绑定";
    meta.textContent = `${session.identifier} · ${session.status} · ${codexId}`;

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "session-delete";
    deleteButton.title = isBusySession(session) ? "任务处理中不能删除" : "删除会话";
    deleteButton.setAttribute("aria-label", `删除会话 ${session.name}`);
    deleteButton.disabled = isBusySession(session);
    deleteButton.addEventListener("click", () => deleteSession(session));

    select.append(title, meta);
    item.append(select, deleteButton);
    els.sessionList.appendChild(item);
  });
}

function renderChatShell() {
  const session = currentSession();
  if (!session) {
    els.messageInput.disabled = true;
    els.sendButton.disabled = true;
    els.stopButton.disabled = true;
    els.messages.innerHTML = "";
    return;
  }
  const running = session.status === "running";
  const busy = isBusySession(session);
  els.messageInput.disabled = busy;
  els.sendButton.disabled = busy;
  els.stopButton.disabled = !running;
}

async function deleteSession(session) {
  if (!session || isBusySession(session)) return;
  if (!window.confirm(`删除会话 ${session.name}？`)) return;
  try {
    await api(`/sessions/${session.id}`, { method: "DELETE" });
    if (Number(state.selectedSessionId) === Number(session.id)) {
      state.selectedSessionId = null;
      state.messages = [];
    }
    await loadState();
    await loadMessages();
  } catch (error) {
    showToast(error.message);
  }
}

function renderMessages() {
  const nearBottom = els.messages.scrollTop + els.messages.clientHeight >= els.messages.scrollHeight - 80;
  els.messages.innerHTML = "";
  const visibleMessages = state.messages.filter((message) => {
    if (message.kind === "run") return false;
    if (message.kind === "vulnerability") return false;
    if (message.role === "system" && message.content.startsWith("Codex 主 agent 已启动")) return false;
    if (message.role === "system" && message.content.startsWith("已请求停止当前 Codex 运行")) return false;
    return true;
  });
  const session = currentSession();
  const running = session?.status === "running";
  if (!visibleMessages.length && !running) {
    const empty = document.createElement("div");
    empty.className = "message system";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = "等待消息";
    empty.appendChild(bubble);
    els.messages.appendChild(empty);
    return;
  }

  visibleMessages.forEach((message) => {
    const item = document.createElement("div");
    item.className = `message ${message.role}`;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    const content = document.createElement("div");
    content.textContent = message.content;
    const time = document.createElement("div");
    time.className = "message-time";
    time.textContent = formatTime(message.created_at);
    bubble.append(content, time);
    item.appendChild(bubble);
    els.messages.appendChild(item);
  });
  if (running) {
    appendThinkingMessage(session);
  }

  if (nearBottom) {
    els.messages.scrollTop = els.messages.scrollHeight;
  }
}

function appendThinkingMessage(session) {
  const item = document.createElement("div");
  item.className = "message system thinking";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  const content = document.createElement("div");
  content.className = "thinking-content";
  content.textContent = thinkingText(session);
  bubble.appendChild(content);
  item.appendChild(bubble);
  els.messages.appendChild(item);
}

function updateThinkingMessage() {
  const session = currentSession();
  const content = els.messages.querySelector(".thinking-content");
  if (!content || session?.status !== "running") return;
  content.textContent = thinkingText(session);
}

function thinkingText(session) {
  const startedAt = new Date(session.updated_at || session.created_at || Date.now()).getTime();
  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  return `已思考${formatDuration(elapsedSeconds)}`;
}

function formatDuration(totalSeconds) {
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h${minutes}m${seconds}s`;
  if (minutes > 0) return `${minutes}m${seconds}s`;
  return `${seconds}s`;
}

function severityFor(score) {
  const value = Number(score || 0);
  if (value >= 90) return { className: "critical", label: "严重" };
  if (value >= 70) return { className: "high", label: "高危" };
  if (value >= 40) return { className: "medium", label: "中危" };
  if (value > 0) return { className: "low", label: "低危" };
  return { className: "info", label: "信息" };
}

function renderVulnerabilities(payload) {
  els.vulnerabilityRows.innerHTML = "";
  if (!payload.ok) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 7;
    cell.className = "empty-cell";
    cell.textContent = `读取失败: ${payload.error}`;
    row.appendChild(cell);
    els.vulnerabilityRows.appendChild(row);
    els.vulnerabilityMeta.textContent = payload.path || "overall.json 不可用";
    return;
  }

  const findings = Array.isArray(payload.findings) ? payload.findings : [];
  if (payload.available === false) {
    els.vulnerabilityMeta.textContent = `${payload.identifier || ""} · overall.json 不可用`;
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 7;
    cell.className = "empty-cell";
    cell.textContent = payload.error || "overall.json 不可用";
    row.appendChild(cell);
    els.vulnerabilityRows.appendChild(row);
    return;
  } else {
    els.vulnerabilityMeta.textContent = `${payload.identifier || ""} · ${findings.length} 条 · ${payload.generated_at || "未标记时间"}`;
  }
  if (!findings.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 7;
    cell.className = "empty-cell";
    cell.textContent = "暂无漏洞";
    row.appendChild(cell);
    els.vulnerabilityRows.appendChild(row);
    return;
  }

  findings.forEach((finding) => {
    const row = document.createElement("tr");
    const severity = severityFor(finding.score);
    const cells = [
      finding.vulnerability_id || "",
      "",
      finding.vulnerability_type || "",
      finding.affected_module || "",
      finding.exploit_difficulty || "",
      finding.exp_exists_text || (finding.exp_exists ? "是" : "否"),
      finding.summary || "",
    ];
    cells.forEach((value, index) => {
      const cell = document.createElement("td");
      if (index === 1) {
        const pill = document.createElement("span");
        pill.className = `severity ${severity.className}`;
        pill.textContent = `${severity.label} ${finding.score ?? 0}`;
        cell.appendChild(pill);
      } else {
        cell.textContent = value;
      }
      row.appendChild(cell);
    });
    els.vulnerabilityRows.appendChild(row);
  });
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

async function loadState() {
  const payload = await api("/state");
  state.sessions = payload.sessions || [];
  applyTheme(payload.settings?.theme || "light");
  const selected = payload.settings?.selected_session_id;
  const selectedExists = state.sessions.some((session) => String(session.id) === String(selected));
  if (selectedExists) {
    state.selectedSessionId = Number(selected);
  } else if (!state.selectedSessionId && state.sessions.length) {
    state.selectedSessionId = Number(state.sessions[0].id);
  } else if (!state.sessions.length) {
    state.selectedSessionId = null;
  }
  renderSessions();
  renderChatShell();
  resetVulnerabilities();
  await loadVulnerabilities();
}

async function refreshSessionsOnly() {
  const payload = await api("/sessions");
  const previousSelected = state.selectedSessionId;
  state.sessions = payload.sessions || [];
  if (state.selectedSessionId && !state.sessions.some((session) => Number(session.id) === Number(state.selectedSessionId))) {
    state.selectedSessionId = state.sessions.length ? Number(state.sessions[0].id) : null;
  }
  renderSessions();
  renderChatShell();
  if (Number(previousSelected) !== Number(state.selectedSessionId)) {
    resetVulnerabilities();
    await loadVulnerabilities();
  }
}

async function loadMessages() {
  const session = currentSession();
  if (!session) {
    state.messages = [];
    renderMessages();
    return;
  }
  const payload = await api(`/sessions/${session.id}/messages`);
  state.messages = payload.messages || [];
  renderMessages();
}

async function loadVulnerabilities() {
  const session = currentSession();
  await loadVulnerabilitiesFor(session);
}

async function loadVulnerabilitiesFor(session) {
  if (!session) {
    els.vulnerabilityMeta.textContent = "未选择会话";
    els.vulnerabilityRows.innerHTML = '<tr><td colspan="7" class="empty-cell">暂无数据</td></tr>';
    return;
  }
  const sessionId = Number(session.id);
  try {
    const payload = await api(`/sessions/${sessionId}/vulnerabilities`);
    if (Number(state.selectedSessionId) !== sessionId) return;
    renderVulnerabilities(payload);
  } catch (error) {
    if (Number(state.selectedSessionId) !== sessionId) return;
    renderVulnerabilities({ ok: false, error: error.message });
  }
}

function resetVulnerabilities() {
  const session = currentSession();
  if (!session) {
    els.vulnerabilityMeta.textContent = "未选择会话";
    els.vulnerabilityRows.innerHTML = '<tr><td colspan="7" class="empty-cell">暂无数据</td></tr>';
    return;
  }
  els.vulnerabilityMeta.textContent = `${session.identifier} · 正在读取 overall.json`;
  els.vulnerabilityRows.innerHTML = '<tr><td colspan="7" class="empty-cell">正在读取漏洞列表</td></tr>';
}

async function poll() {
  try {
    await refreshSessionsOnly();
    await loadMessages();
  } catch (error) {
    showToast(error.message);
  }
}

els.themeToggle.addEventListener("click", async () => {
  const next = state.theme === "dark" ? "light" : "dark";
  applyTheme(next);
  try {
    await api("/settings", { method: "PATCH", body: { theme: next } });
  } catch (error) {
    showToast(error.message);
  }
});

els.newSessionButton.addEventListener("click", () => {
  els.sessionForm.classList.toggle("hidden");
  if (!els.sessionForm.classList.contains("hidden")) {
    els.sessionIdentifier.focus();
  }
});

els.cancelSessionButton.addEventListener("click", () => {
  els.sessionForm.reset();
  els.sessionForm.classList.add("hidden");
});

els.sessionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const payload = await api("/sessions", {
      method: "POST",
      body: {
        name: els.sessionName.value,
        identifier: els.sessionIdentifier.value,
      },
    });
    els.sessionForm.reset();
    els.sessionForm.classList.add("hidden");
    await loadState();
    setSelectedSession(payload.session.id);
  } catch (error) {
    showToast(error.message);
  }
});

els.messageForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const session = currentSession();
  const content = els.messageInput.value.trim();
  if (!session || isBusySession(session) || !content) return;
  els.messageInput.value = "";
  try {
    await api(`/sessions/${session.id}/messages`, {
      method: "POST",
      body: { content },
    });
    await poll();
  } catch (error) {
    showToast(error.message);
  }
});

els.stopButton.addEventListener("click", async () => {
  const session = currentSession();
  if (!session || session.status !== "running") return;
  try {
    await api(`/sessions/${session.id}/stop`, { method: "POST" });
    await poll();
  } catch (error) {
    showToast(error.message);
  }
});

els.refreshVulnerabilities.addEventListener("click", loadVulnerabilities);

window.addEventListener("load", async () => {
  try {
    await loadState();
    await loadMessages();
    state.polling = window.setInterval(poll, 2500);
    state.thinkingTimer = window.setInterval(updateThinkingMessage, 1000);
  } catch (error) {
    showToast(error.message);
  }
});
