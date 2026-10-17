"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { settings: null, conversations: [], current: null, busy: false, poll: null };

const MODEL_HINTS = {
  openai: "e.g. gpt-5-mini",
  anthropic: "e.g. claude-sonnet-5-5",
  gemini: "e.g. gemini-2.5-flash",
  deepseek: "e.g. deepseek-chat",
  ollama: "e.g. qwen3:8b (must support tools)",
  openai_compatible: "model name served by the endpoint",
};
const ENDPOINT_HINTS = {
  ollama: "http://localhost:11434",
  openai_compatible: "https://example.com/v1",
};

// ---------------------------------------------------------------- API helper

async function api(method, path, body, raw) {
  const opts = { method, headers: {} };
  if (raw !== undefined) {
    opts.body = raw;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (e) {
    throw new Error("Cannot reach the Nature AI Worker server. Is it still running?");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    let detail = data.detail;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
    throw new Error(detail || `Request failed (HTTP ${resp.status}).`);
  }
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

// ---------------------------------------------------------------- Markdown (minimal, escaped)

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function inline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
}

function renderMarkdown(text) {
  const out = [];
  const parts = escapeHtml(text).split(/```[^\n]*\n?/);
  parts.forEach((part, i) => {
    if (i % 2 === 1) { out.push(`<pre><code>${part}</code></pre>`); return; }
    let list = null;
    for (const line of part.split("\n")) {
      const h = line.match(/^(#{1,6})\s+(.*)/);
      const li = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/);
      if (li) {
        const type = /^\s*\d+\./.test(line) ? "ol" : "ul";
        if (list !== type) { if (list) out.push(`</${list}>`); out.push(`<${type}>`); list = type; }
        out.push(`<li>${inline(li[1])}</li>`);
        continue;
      }
      if (list) { out.push(`</${list}>`); list = null; }
      if (h) out.push(`<h3>${inline(h[2])}</h3>`);
      else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
    }
    if (list) out.push(`</${list}>`);
  });
  return out.join("");
}

// ---------------------------------------------------------------- Conversations

async function loadConversations() {
  state.conversations = await api("GET", "/api/conversations");
  const nav = $("#conversations");
  nav.replaceChildren();
  for (const c of state.conversations) {
    const a = el("a", { href: "#" + c.id, title: c.title }, c.title);
    if (state.current && state.current.id === c.id) a.classList.add("active");
    a.addEventListener("click", (e) => { e.preventDefault(); openConversation(c.id); $("#sidebar").classList.remove("open"); });
    nav.append(a);
  }
}

async function openConversation(id) {
  try {
    state.current = await api("GET", `/api/conversations/${id}`);
  } catch (e) {
    state.current = null;
  }
  history.replaceState(null, "", state.current ? "#" + state.current.id : "#");
  render();
  loadConversations();
}

function newChat() {
  state.current = null;
  history.replaceState(null, "", "#");
  render();
  loadConversations();
  $("#input").focus();
}

async function refreshCurrent() {
  if (!state.current) return;
  try {
    state.current = await api("GET", `/api/conversations/${state.current.id}`);
    render();
  } catch (e) { /* keep showing what we have */ }
}

// ---------------------------------------------------------------- Rendering

function argsSummary(args) {
  return Object.entries(args || {})
    .map(([k, v]) => `${k}=${typeof v === "string" ? JSON.stringify(v.length > 60 ? v.slice(0, 60) + "…" : v) : JSON.stringify(v)}`)
    .join(" ");
}

function toolCard(call, result) {
  let stateText = "running", cls = "";
  if (result) {
    const meta = result.meta || {};
    if (meta.denied) { stateText = "not approved"; cls = "err"; }
    else if (meta.error) { stateText = "failed"; cls = "err"; }
    else { stateText = "done"; cls = "ok"; }
  }
  const details = el("details", { class: "tool" },
    el("summary", {},
      el("span", { class: "name" }, call.name),
      el("span", { class: "args" }, argsSummary(call.arguments)),
      el("span", { class: "state " + cls }, stateText)));
  const body = [];
  if (call.name === "write_file" && call.arguments && typeof call.arguments.content === "string") {
    body.push("Content written:\n" + call.arguments.content);
  }
  if (result) body.push("Result:\n" + result.content);
  if (body.length) details.append(el("pre", {}, body.join("\n\n")));
  return details;
}

function approvalCard(a) {
  const args = { ...a.arguments };
  const content = typeof args.content === "string" ? args.content : null;
  delete args.content;
  const pre = el("pre", {}, JSON.stringify(args, null, 2) + (content !== null ? "\n\ncontent:\n" + content : ""));
  const approve = el("button", { class: "primary" }, "Approve");
  const deny = el("button", { class: "danger" }, "Deny");
  const decide = async (ok) => {
    approve.disabled = deny.disabled = true;
    await runWithPolling(() => api("POST", `/api/approvals/${a.id}`, { approve: ok }));
  };
  approve.addEventListener("click", () => decide(true));
  deny.addEventListener("click", () => decide(false));
  return el("div", { class: "approval" },
    el("h3", {}, "Approval needed: " + a.reason),
    el("div", { class: "muted small" }, `The agent wants to run ${a.tool_name}. Nothing happens until you decide.`),
    pre,
    el("div", { class: "actions" }, approve, deny));
}

function render() {
  const conv = state.current;
  const box = $("#messages");
  box.replaceChildren();
  $("#chat-title").textContent = conv ? conv.title : "New chat";
  $("#delete-chat").hidden = !conv;
  if (!conv || conv.messages.length === 0) {
    box.append(emptyState());
  } else {
    const results = {};
    for (const m of conv.messages) if (m.role === "tool") results[m.tool_call_id] = m;
    const approvals = {};
    for (const a of conv.approvals) approvals[a.tool_call_id] = a;
    for (const m of conv.messages) {
      if (m.role === "user") box.append(el("div", { class: "msg user" }, m.content));
      else if (m.role === "assistant") {
        if (m.content) {
          const div = el("div", { class: "msg assistant" });
          div.innerHTML = renderMarkdown(m.content);
          box.append(div);
        }
        for (const call of m.tool_calls || []) {
          if (approvals[call.id]) box.append(approvalCard(approvals[call.id]));
          else box.append(toolCard(call, results[call.id]));
        }
      } else if (m.role === "notice") {
        const info = (m.meta || {}).error === "step_limit";
        box.append(el("div", { class: "msg notice" + (info ? " info" : "") }, m.content));
      }
    }
  }
  if (state.busy || (conv && conv.running)) box.append(el("div", { class: "working" }, "Working"));
  box.scrollTop = box.scrollHeight;
  $("#send").disabled = state.busy;
}

function emptyState() {
  const tpl = el("div", { id: "empty-state" },
    el("h2", {}, "What should I work on?"),
    el("p", { class: "muted" }, "I can read, search and write files in your workspace, summarize data, and produce reports."));
  const ex = el("div", { class: "examples" });
  for (const text of [
    "List the files in my workspace and describe what they contain.",
    "Read the files in my workspace and write a summary report to report.md.",
  ]) {
    ex.append(el("button", { class: "example", onclick: () => { $("#input").value = text; $("#input").focus(); } }, text));
  }
  tpl.append(ex);
  return tpl;
}

function showError(message) {
  $("#messages").append(el("div", { class: "msg notice" }, message));
  $("#messages").scrollTop = $("#messages").scrollHeight;
}

// ---------------------------------------------------------------- Sending

async function runWithPolling(fn) {
  state.busy = true;
  render();
  state.poll = setInterval(refreshCurrent, 1000);
  try {
    await fn();
  } catch (e) {
    clearInterval(state.poll);
    state.busy = false;
    await refreshCurrent();
    showError(e.message);
    return;
  }
  clearInterval(state.poll);
  state.busy = false;
  await refreshCurrent();
  loadConversations();
  if ($("#files-panel").hidden === false) loadFiles();
}

async function send(text) {
  if (!state.settings || !state.settings.configured) { openSettings(true); return; }
  if (!state.current) {
    try {
      const conv = await api("POST", "/api/conversations");
      state.current = { ...conv, messages: [], approvals: [], running: false };
      history.replaceState(null, "", "#" + conv.id);
    } catch (e) { showError(e.message); return; }
  }
  state.current.messages.push({ role: "user", content: text });
  await runWithPolling(() => api("POST", `/api/conversations/${state.current.id}/messages`, { text }));
}

// ---------------------------------------------------------------- Files

async function loadFiles() {
  const list = $("#file-list");
  try {
    const data = await api("GET", "/api/files");
    $("#workspace-path").textContent = data.root;
    list.replaceChildren();
    if (data.files.length === 0) list.append(el("li", { class: "muted" }, "The workspace is empty. Upload a file or ask the agent to create one."));
    for (const f of data.files) {
      if (f.type === "dir") { list.append(el("li", { class: "dir" }, el("span", { class: "fname" }, f.path + "/"))); continue; }
      const name = el("span", { class: "fname", title: "Preview" }, f.path);
      name.addEventListener("click", () => previewFile(f.path));
      list.append(el("li", {}, name,
        el("span", { class: "muted small" }, formatSize(f.size)),
        el("a", { href: "/api/files/download?path=" + encodeURIComponent(f.path), title: "Download" }, "⬇")));
    }
  } catch (e) {
    list.replaceChildren(el("li", { class: "muted" }, e.message));
  }
}

function formatSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

async function previewFile(path) {
  $("#preview-name").textContent = path;
  try {
    const data = await api("GET", "/api/files/content?path=" + encodeURIComponent(path));
    $("#preview-content").textContent = data.content + (data.truncated ? "\n\n[Preview truncated]" : "");
  } catch (e) {
    $("#preview-content").textContent = e.message;
  }
  $("#file-preview").hidden = false;
}

async function uploadFile(file) {
  const url = (overwrite) => `/api/files?path=${encodeURIComponent(file.name)}&overwrite=${overwrite}`;
  try {
    await api("PUT", url(false), undefined, file);
  } catch (e) {
    if (!/already exists/.test(e.message) || !confirm(`${file.name} already exists. Replace it?`)) {
      if (!/already exists/.test(e.message)) alert(e.message);
      return;
    }
    try { await api("PUT", url(true), undefined, file); } catch (e2) { alert(e2.message); return; }
  }
  loadFiles();
}

// ---------------------------------------------------------------- Settings

function fillProviders() {
  const sel = $("#s-provider");
  sel.replaceChildren();
  for (const [id, spec] of Object.entries(state.settings.providers)) sel.append(el("option", { value: id }, spec.label));
}

function updateProviderFields() {
  const id = $("#s-provider").value;
  const spec = state.settings.providers[id];
  $("#s-model").placeholder = MODEL_HINTS[id] || "";
  $("#s-endpoint-row").hidden = !spec.needs_endpoint;
  $("#s-endpoint").placeholder = ENDPOINT_HINTS[id] || "";
  if (id === "ollama" && !$("#s-endpoint").value) $("#s-endpoint").value = spec.default_endpoint;
  $("#s-key-row").firstChild.textContent = spec.needs_key ? "API key" : "API key (optional)";
  updateKeyStatus();
  $("#settings-result").textContent = "";
}

function normEndpoint(url) {
  return (url || "").trim().replace(/\/+$/, "").toLowerCase();
}

function updateKeyStatus() {
  const id = $("#s-provider").value;
  const spec = state.settings.providers[id];
  const source = state.settings.key_sources[id];
  const bound = (state.settings.key_endpoints || {})[id];
  const current = normEndpoint($("#s-endpoint").value || spec.default_endpoint);
  const usable = source === "env" || (source === "stored" && (!spec.needs_endpoint || normEndpoint(bound) === current));
  $("#s-key").placeholder = usable ? "Leave empty to keep the current key" :
    (spec.needs_key ? "Paste your API key" : "Leave empty if not required");
  let text = "";
  if (source === "env") text = "Using the key from the server environment variable.";
  else if (source === "stored" && usable) text = "A key is stored (encrypted). Enter a new one to replace it.";
  else if (source === "stored") text = `A stored key exists for ${bound}. For safety it is never sent to a different endpoint; enter the key again to use it here.`;
  $("#s-key-status").textContent = text;
}

function openSettings(firstRun = false) {
  const s = state.settings;
  fillProviders();
  $("#s-provider").value = s.provider || "openai";
  $("#s-model").value = s.model || "";
  $("#s-endpoint").value = s.endpoint || "";
  $("#s-key").value = "";
  $("#s-web").checked = !!s.web_fetch_enabled;
  $("#s-steps").value = s.max_steps;
  $("#setup-intro").hidden = !firstRun;
  $("#settings-heading").textContent = firstRun ? "Set up your AI model" : "Settings";
  $("#s-cancel").hidden = firstRun && !s.configured;
  updateProviderFields();
  $("#settings-dialog").showModal();
}

function settingsBody() {
  return {
    provider: $("#s-provider").value,
    model: $("#s-model").value.trim(),
    endpoint: $("#s-endpoint-row").hidden ? null : ($("#s-endpoint").value.trim() || null),
    api_key: $("#s-key").value.trim() || null,
    web_fetch_enabled: $("#s-web").checked,
    max_steps: parseInt($("#s-steps").value, 10) || 20,
  };
}

function setResult(text, ok) {
  const r = $("#settings-result");
  r.textContent = text;
  r.className = ok === undefined ? "" : ok ? "ok" : "err";
}

async function testSettings() {
  const body = settingsBody();
  if (!body.model) { setResult("Enter a model name first.", false); return; }
  setResult("Testing… this makes a small real request to the model.");
  $("#s-test").disabled = true;
  try {
    const r = await api("POST", "/api/settings/test", body);
    setResult(r.message, r.ok && r.tools);
  } catch (e) {
    setResult(e.message, false);
  } finally {
    $("#s-test").disabled = false;
  }
}

async function saveSettings() {
  const body = settingsBody();
  if (!body.model) { setResult("Enter a model name.", false); return; }
  try {
    state.settings = await api("PUT", "/api/settings", body);
  } catch (e) { setResult(e.message, false); return; }
  if (!state.settings.configured) {
    setResult("Saved, but setup is incomplete: " + (state.settings.providers[body.provider].needs_key ? "an API key is required." : "an endpoint URL is required."), false);
    return;
  }
  $("#settings-dialog").close();
  updateModelLabel();
}

function updateModelLabel() {
  const s = state.settings;
  $("#model-label").textContent = s.configured ? `${s.providers[s.provider].label} · ${s.model}` : "No model configured";
}

// ---------------------------------------------------------------- Wiring

async function init() {
  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("#input").value.trim();
    if (!text || state.busy) return;
    $("#input").value = "";
    send(text);
  });
  $("#input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
  });
  $("#new-chat").addEventListener("click", newChat);
  $("#toggle-sidebar").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
  $("#delete-chat").addEventListener("click", async () => {
    if (!state.current || !confirm("Delete this conversation? Workspace files are not affected.")) return;
    try { await api("DELETE", `/api/conversations/${state.current.id}`); newChat(); } catch (e) { showError(e.message); }
  });
  $("#open-settings").addEventListener("click", () => openSettings(false));
  $("#s-provider").addEventListener("change", () => { $("#s-endpoint").value = ""; updateProviderFields(); });
  $("#s-endpoint").addEventListener("input", updateKeyStatus);
  $("#s-test").addEventListener("click", testSettings);
  $("#s-save").addEventListener("click", saveSettings);
  $("#s-cancel").addEventListener("click", () => $("#settings-dialog").close());
  $("#settings-dialog").addEventListener("cancel", (e) => { if (!state.settings.configured) e.preventDefault(); });
  $("#open-files").addEventListener("click", () => { $("#files-panel").hidden = false; loadFiles(); });
  $("#close-files").addEventListener("click", () => { $("#files-panel").hidden = true; });
  $("#refresh-files").addEventListener("click", loadFiles);
  $("#close-preview").addEventListener("click", () => { $("#file-preview").hidden = true; });
  $("#upload").addEventListener("change", (e) => { if (e.target.files[0]) uploadFile(e.target.files[0]); e.target.value = ""; });

  try {
    state.settings = await api("GET", "/api/settings");
  } catch (e) {
    showError(e.message);
    return;
  }
  updateModelLabel();
  await loadConversations();
  const id = location.hash.slice(1);
  if (id) await openConversation(id); else render();
  if (!state.settings.configured) openSettings(true);
}

init();
