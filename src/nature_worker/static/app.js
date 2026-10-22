"use strict";

const $ = (sel) => document.querySelector(sel);
// drafts: unsent Communication text per work id. busyId: the work the agent is running for this page.
// nav: increases on every navigation so late responses for a previous view are ignored.
const state = {
  settings: null, work: [], current: null, filter: "all",
  busy: false, busyId: null, poll: null, drafts: {}, nav: 0,
  openTools: new Set(), listTimer: null,
};

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

// ---------------------------------------------------------------- Formatting

const STATUS_FILTERS = [
  ["all", "All"], ["needs_approval", "Needs your approval"], ["working", "Working"],
  ["finished", "Finished"], ["problem", "Failed, stopped or interrupted"],
];

function formatTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function formatSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function statusBadge(status, label) {
  return el("span", { class: "status s-" + status }, label);
}

// The first instruction in full; the stored title is cut at 60 characters.
function workTitle(w) {
  const text = (w.instruction || (w.work && w.work.instruction) || "").trim();
  if (text) return text.split("\n")[0];
  return w.title && w.title !== "New conversation" ? w.title : "Untitled work";
}

function formatClock(ts) {
  return new Date(ts * 1000).toLocaleTimeString(undefined, { timeStyle: "short" });
}

// ---------------------------------------------------------------- Routing

// "#/" My Work, "#/new" New Work, "#/work/<id>" one piece of work. "#<id>" links from v0.1.0 still work.
function parseRoute() {
  const h = location.hash.replace(/^#/, "");
  if (h === "/new") return { view: "new" };
  const m = h.match(/^\/work\/([0-9a-f]+)$/) || h.match(/^([0-9a-f]{32})$/);
  if (m) return { view: "work", id: m[1] };
  return { view: "list" };
}

function go(hash) {
  if (location.hash === hash) route(); else location.hash = hash;
}

async function route() {
  const r = parseRoute();
  const nav = ++state.nav;
  saveDraft();
  stopPolling();
  $("#sidebar").classList.remove("open");
  if (r.view === "work") {
    let conv = null;
    try { conv = await api("GET", `/api/conversations/${r.id}`); } catch (e) { /* not found */ }
    if (nav !== state.nav) return;  // the user navigated elsewhere meanwhile
    if (!conv) { go("#/"); return; }
    state.current = conv;
    $("#input").value = state.drafts[conv.id] || "";
    show("work");
    renderWork();
    if (conv.running) startPolling();
  } else {
    state.current = null;
    show(r.view);
    if (r.view === "new") renderNew(); else await loadWork();
  }
  loadWork(true);
}

function show(view) {
  for (const v of ["list", "new", "work"]) $("#view-" + v).hidden = v !== view;
  $("#nav-my-work").classList.toggle("active", view === "list");
  const heading = { list: "#list-heading", new: "#new-heading", work: "#work-title" }[view];
  $(heading).focus({ preventScroll: true });
  $("#main").scrollTop = 0;
}

function saveDraft() {
  if (!state.current) return;
  const text = $("#input").value;
  if (text) state.drafts[state.current.id] = text; else delete state.drafts[state.current.id];
}

// ---------------------------------------------------------------- My Work

async function loadWork(navOnly = false) {
  let items;
  try {
    items = await api("GET", "/api/work");
  } catch (e) {
    if (!navOnly) $("#work-list").replaceChildren(el("li", { class: "empty" }, e.message));
    return;
  }
  state.work = items;
  renderNav();
  if (!navOnly || !$("#view-list").hidden) renderList();
  clearTimeout(state.listTimer);
  if (!$("#view-list").hidden && items.some((w) => w.status === "working")) {
    state.listTimer = setTimeout(() => loadWork(true), 2000);
  }
}

function renderNav() {
  const nav = $("#work-nav");
  nav.replaceChildren();
  $("#my-work-count").textContent = state.work.length ? String(state.work.length) : "";
  for (const w of state.work.slice(0, 15)) {
    const a = el("a", { href: "#/work/" + w.id, title: workTitle(w) },
      el("span", { class: "dot s-" + w.status, "aria-hidden": "true" }),
      el("span", { class: "label" }, workTitle(w)),
      el("span", { class: "visually-hidden" }, ", " + w.status_label));
    if (state.current && state.current.id === w.id) a.classList.add("active");
    nav.append(a);
  }
}

function matchesFilter(w, key) {
  if (key === "all") return true;
  if (key === "problem") return ["failed", "stopped", "interrupted"].includes(w.status);
  return w.status === key;
}

function renderList() {
  const box = $("#status-filter");
  box.replaceChildren();
  for (const [key, label] of STATUS_FILTERS) {
    const n = state.work.filter((w) => matchesFilter(w, key)).length;
    const b = el("button", { type: "button", "aria-pressed": String(state.filter === key) }, `${label} (${n})`);
    b.addEventListener("click", () => { state.filter = key; renderList(); });
    box.append(b);
  }
  const list = $("#work-list");
  list.replaceChildren();
  if (state.work.length === 0) {
    list.append(el("li", { class: "empty" },
      el("h2", {}, "No work yet"),
      el("p", { class: "muted" }, "Assign a task in plain language. The agent can read, search and write files in your workspace folder, summarize data and produce reports."),
      el("button", { class: "primary", onclick: () => go("#/new") }, "+ New Work")));
    return;
  }
  const shown = state.work.filter((w) => matchesFilter(w, state.filter));
  if (shown.length === 0) list.append(el("li", { class: "empty muted" }, "No work with this status."));
  for (const w of shown) {
    const meta = [`Last activity ${formatTime(w.updated_at)}`];
    if (w.deliverables) meta.push(plural(w.deliverables, "result file"));
    list.append(el("li", {},
      el("a", { href: "#/work/" + w.id, class: "work-row" },
        el("span", { class: "row-title" }, workTitle(w)),
        statusBadge(w.status, w.status_label),
        el("span", { class: "row-meta muted small" }, meta.join(" · ")))));
  }
}

// ---------------------------------------------------------------- New Work

const EXAMPLES = [
  "List the files in my workspace and describe what they contain.",
  "Read the files in my workspace and write a summary report to report.md.",
  "Read sales.csv and write report.md with revenue totals per region.",
];

async function renderNew() {
  const ex = $("#new-examples");
  ex.replaceChildren();
  for (const text of EXAMPLES) {
    ex.append(el("button", { type: "button", class: "example", onclick: () => { $("#new-input").value = text; $("#new-input").focus(); } }, text));
  }
  try {
    const data = await api("GET", "/api/files");
    const files = data.files.filter((f) => f.type === "file").length;
    $("#new-files-hint").textContent = files
      ? `The agent can use the ${plural(files, "file")} in your workspace folder.`
      : "Your workspace folder is empty. Upload files the agent should work with, or ask it to create new ones.";
  } catch (e) { $("#new-files-hint").textContent = ""; }
}

function newWork() {
  $("#new-input").value = "";  // a fresh assignment every time
  go("#/new");
}

async function startWork(text) {
  if (!state.settings || !state.settings.configured) { openSettings(true); return; }
  const nav = state.nav;
  let conv;
  try {
    conv = await api("POST", "/api/conversations");
  } catch (e) { alert(e.message); return; }
  if (nav !== state.nav) return;
  $("#new-input").value = "";
  state.current = { ...conv, messages: [{ role: "user", content: text }], approvals: [], running: true, work: { instruction: text } };
  history.replaceState(null, "", "#/work/" + conv.id);
  state.nav++;
  show("work");
  renderWork();
  await runWithPolling(() => api("POST", `/api/conversations/${conv.id}/messages`, { text }));
}

// ---------------------------------------------------------------- Work detail

function argsSummary(args) {
  return Object.entries(args || {})
    .map(([k, v]) => `${k}=${typeof v === "string" ? JSON.stringify(v.length > 60 ? v.slice(0, 60) + "…" : v) : JSON.stringify(v)}`)
    .join(" ");
}

function toolCard(call, result, time) {
  let stateText = "running", cls = "";
  if (result) {
    const meta = result.meta || {};
    if (meta.denied) { stateText = "not approved"; cls = "err"; }
    else if (meta.error) { stateText = "failed"; cls = "err"; }
    else { stateText = "done"; cls = "ok"; }
  } else if (!state.current.running && !(state.busy && state.busyId === state.current.id)) {
    stateText = "no result"; cls = "err";
  }
  const details = el("details", { class: "tool" },
    el("summary", {},
      el("span", { class: "name" }, call.name),
      el("span", { class: "args" }, argsSummary(call.arguments)),
      time ? el("time", { class: "time muted small", datetime: new Date(time * 1000).toISOString(), title: formatTime(time) }, formatClock(time)) : null,
      el("span", { class: "state " + cls }, stateText)));
  const body = [];
  if (call.name === "write_file" && call.arguments && typeof call.arguments.content === "string") {
    body.push("Content written:\n" + call.arguments.content);
  }
  if (result) body.push("Result:\n" + result.content);
  if (body.length) details.append(el("pre", {}, body.join("\n\n")));
  details.open = state.openTools.has(call.id);
  details.addEventListener("toggle", () => {
    if (details.open) state.openTools.add(call.id); else state.openTools.delete(call.id);
  });
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
  return el("div", { class: "approval", role: "group", "aria-label": "Approval needed" },
    el("h3", {}, "Your action needed: " + a.reason),
    el("div", { class: "muted small" }, `The agent wants to run ${a.tool_name}. Nothing happens until you decide.`),
    pre,
    el("div", { class: "actions" }, approve, deny));
}

function renderWork() {
  const conv = state.current;
  if (!conv) return;
  const w = conv.work && conv.work.facts ? conv.work : null;
  const working = conv.running || (state.busy && state.busyId === conv.id);
  const status = working ? "working" : (w ? w.status : "working");
  const label = working ? "Working" : (w ? w.status_label : "Working");
  $("#work-title").textContent = workTitle(conv);
  $("#work-status").replaceWith(Object.assign(statusBadge(status, label), { id: "work-status" }));
  $("#delete-work").disabled = working;

  // Execution facts: only counts and times taken from the records.
  const facts = $("#work-facts");
  facts.replaceChildren();
  const f = w ? w.facts : null;
  const rows = [["Started", formatTime(conv.created_at)], ["Last activity", formatTime(conv.updated_at)]];
  if (f) {
    rows.push(["Agent steps", String(f.agent_steps)]);
    let tools = String(f.tool_calls);
    const extra = [];
    if (f.tool_failures) extra.push(`${f.tool_failures} failed`);
    if (f.denied) extra.push(`${f.denied} not approved`);
    if (extra.length) tools += ` (${extra.join(", ")})`;
    rows.push(["Tool calls", tools]);
    const appr = [];
    if (f.approvals_pending) appr.push(`${f.approvals_pending} waiting`);
    if (f.approvals_approved) appr.push(`${f.approvals_approved} approved`);
    if (f.approvals_denied) appr.push(`${f.approvals_denied} denied`);
    rows.push(["Approvals", appr.join(", ") || "None requested"]);
    rows.push(["Result files", String(w.deliverables.length)]);
  }
  for (const [k, v] of rows) facts.append(el("div", {}, el("dt", {}, k), el("dd", {}, v)));

  renderUpdate(conv, w, status);
  renderResults(w);
  renderActivity(conv);
  renderCommunication(conv);
  $("#send").disabled = state.busy;
  renderNav();
}

// Rebuilt on every poll, but only swapped in when it changed, so screen readers announce real updates only.
function renderUpdate(conv, w, status) {
  const target = $("#work-update");
  const box = el("div");
  buildUpdate(box, conv, w, status);
  if (box.innerHTML !== target.innerHTML) target.replaceChildren(...box.childNodes);
}

function buildUpdate(box, conv, w, status) {
  if (status === "working") {
    const calls = conv.messages.filter((m) => m.role === "assistant").flatMap((m) => m.tool_calls || []);
    const last = calls[calls.length - 1];
    box.append(el("div", { class: "update working-box" },
      el("span", { class: "working" }, "Working"),
      el("p", { class: "muted small" }, last ? `Latest step: ${last.name} ${argsSummary(last.arguments)}` : "Waiting for the model's first step.")));
    return;
  }
  if (status === "needs_approval") {
    for (const a of conv.approvals) box.append(approvalCard(a));
    return;
  }
  const text = w ? w.update : "";
  const cls = { failed: "update error", stopped: "update warn", interrupted: "update warn", finished: "update" }[status] || "update";
  const div = el("div", { class: cls });
  if (status === "finished") {
    div.append(el("p", { class: "muted small" }, "The agent ended its run with this reply. Check the results before relying on them."));
    const body = el("div", { class: "md" });
    body.innerHTML = renderMarkdown(text || "(no text)");
    div.append(body);
  } else if (status === "not_started") {
    div.append(el("p", {}, "No instructions yet. Describe the work in Communication below."));
  } else {
    div.append(el("p", {}, text));
    if (status === "failed") div.append(el("p", { class: "muted small" }, "Fix the cause (for example in Settings), then send a message such as \"try again\"."));
  }
  box.append(div);
}

function fileRow(f, kind) {
  const notes = [];
  if (kind === "deliverable") notes.push(f.action === "Overwrote" ? "Overwritten by this work" : "Created by this work");
  else notes.push("Read by this work");
  if (f.available && f.size !== undefined) notes.push(formatSize(f.size));
  if (f.changed_since) notes.push("changed since this work wrote it");
  if (!f.available) notes.push("no longer in the workspace");
  const actions = el("span", { class: "file-actions" });
  if (f.available) {
    actions.append(
      el("button", { type: "button", onclick: () => previewFile(f.path), "aria-label": "Preview " + f.path }, "Preview"),
      el("a", { class: "button", href: downloadUrl(f.path), "aria-label": "Download " + f.path }, "Download"));
  }
  return el("li", { class: f.available ? "" : "gone" },
    el("span", { class: "fname" }, f.path),
    el("span", { class: "muted small" }, notes.join(" · ")),
    actions);
}

function renderResults(w) {
  const box = $("#work-results");
  box.replaceChildren();
  if (!w) return;
  if (w.deliverables.length) {
    box.append(el("ul", { class: "files deliverables", "aria-label": "Result files" }, ...w.deliverables.map((f) => fileRow(f, "deliverable"))));
  } else {
    box.append(el("p", { class: "muted" }, "No files created by this work yet. Results appear here when the agent writes a file."));
  }
  if (w.inputs.length) {
    box.append(el("h3", { class: "sub-title" }, "Input files used"),
      el("ul", { class: "files inputs", "aria-label": "Input files used" }, ...w.inputs.map((f) => fileRow(f, "input"))));
  }
}

function renderActivity(conv) {
  const list = $("#work-activity");
  list.replaceChildren();
  const results = {};
  for (const m of conv.messages) if (m.role === "tool") results[m.tool_call_id] = m;
  const pending = new Set(conv.approvals.map((a) => a.tool_call_id));
  for (const m of conv.messages) {
    if (m.role === "assistant") {
      for (const call of m.tool_calls || []) {
        if (pending.has(call.id)) {
          list.append(el("li", { class: "event warn" }, `${call.name}: waiting for your approval (see Current work update).`));
        } else {
          list.append(el("li", {}, toolCard(call, results[call.id], (results[call.id] || m).created_at)));
        }
      }
    } else if (m.role === "notice") {
      const info = (m.meta || {}).error === "step_limit";
      list.append(el("li", { class: "event " + (info ? "warn" : "error") },
        el("span", { class: "muted small" }, formatTime(m.created_at) + " "), m.content));
    }
  }
  if (!list.children.length) list.append(el("li", { class: "muted" }, "No tool activity yet."));
}

function renderCommunication(conv) {
  const box = $("#messages");
  box.replaceChildren();
  for (const m of conv.messages) {
    if (m.role === "user") {
      box.append(el("div", { class: "msg user" }, el("div", { class: "who" }, "You"), el("div", { class: "text" }, m.content)));
    } else if (m.role === "assistant" && m.content) {
      const div = el("div", { class: "msg assistant" }, el("div", { class: "who" }, "Agent"));
      const body = el("div", { class: "md" });
      body.innerHTML = renderMarkdown(m.content);
      div.append(body);
      box.append(div);
    }
  }
  if (!box.children.length) box.append(el("p", { class: "muted" }, "No messages yet."));
}

async function refreshCurrent() {
  if (!state.current) return;
  const id = state.current.id;
  try {
    const conv = await api("GET", `/api/conversations/${id}`);
    if (!state.current || state.current.id !== id) return;  // the user moved to other work meanwhile
    state.current = conv;
    renderWork();
  } catch (e) { /* keep showing what we have */ }
}

function showError(message) {
  if ($("#view-work").hidden) { alert(message); return; }
  $("#work-update").prepend(el("div", { class: "update error", role: "alert" }, message));
}

function startPolling() {
  stopPolling();
  state.poll = setInterval(refreshCurrent, 1000);
}

function stopPolling() {
  if (state.poll) clearInterval(state.poll);
  state.poll = null;
}

async function runWithPolling(fn) {
  const id = state.current ? state.current.id : null;
  state.busy = true;
  state.busyId = id;
  renderWork();
  startPolling();
  let error = null;
  try { await fn(); } catch (e) { error = e; }
  stopPolling();
  state.busy = false;
  state.busyId = null;
  await refreshCurrent();
  if (error && state.current && state.current.id === id) showError(error.message);  // never in other work
  loadWork(true);
  if (!$("#files-panel").hidden) loadFiles();
}

async function send(text) {
  if (!state.settings || !state.settings.configured) { openSettings(true); return; }
  if (!state.current) return;
  state.current.messages.push({ role: "user", content: text });
  const id = state.current.id;
  await runWithPolling(() => api("POST", `/api/conversations/${id}/messages`, { text }));
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
      const name = el("button", { type: "button", class: "fname", title: "Preview " + f.path }, f.path);
      name.addEventListener("click", () => previewFile(f.path));
      list.append(el("li", {}, name,
        el("span", { class: "muted small" }, formatSize(f.size)),
        el("a", { href: downloadUrl(f.path), title: "Download " + f.path, "aria-label": "Download " + f.path }, "⬇")));
    }
  } catch (e) {
    list.replaceChildren(el("li", { class: "muted" }, e.message));
  }
}

function downloadUrl(path) {
  return "/api/files/download?path=" + encodeURIComponent(path);
}

// Read-only preview. The server decides what can be previewed (type and size) and explains why not.
async function previewFile(path) {
  const content = $("#preview-content"), message = $("#preview-message");
  $("#preview-name").textContent = path;
  $("#preview-download").href = downloadUrl(path);
  content.textContent = "";
  content.hidden = $("#preview-label").hidden = true;
  message.textContent = "Loading…";
  message.hidden = false;
  if (!$("#preview-dialog").open) $("#preview-dialog").showModal();
  try {
    const data = await api("GET", "/api/files/content?path=" + encodeURIComponent(path));
    if ($("#preview-name").textContent !== path) return;  // another file was opened meanwhile
    content.textContent = data.content || "(empty file)";
    content.hidden = $("#preview-label").hidden = false;
    message.hidden = true;
  } catch (e) {
    if ($("#preview-name").textContent !== path) return;
    message.textContent = e.message;
  }
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
  if (!$("#view-new").hidden) renderNew();
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

function submitOnEnter(textarea, form) {
  textarea.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
}

async function init() {
  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("#input").value.trim();
    if (!text || state.busy) return;
    $("#input").value = "";
    send(text);
  });
  submitOnEnter($("#input"), $("#composer"));
  $("#new-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("#new-input").value.trim();
    if (!text) { $("#new-input").focus(); return; }
    if (state.busy) { alert("The agent is still working on other work. Wait until it finishes."); return; }
    startWork(text);
  });
  $("#new-work").addEventListener("click", newWork);
  $("#list-new-work").addEventListener("click", newWork);
  $("#new-manage-files").addEventListener("click", () => { $("#files-panel").hidden = false; loadFiles(); });
  $("#toggle-sidebar").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
  $(".skip-link").addEventListener("click", (e) => { e.preventDefault(); $("#main").focus(); });
  $("#delete-work").addEventListener("click", async () => {
    if (!state.current || !confirm("Delete this work and its conversation? Workspace files are not affected.")) return;
    try {
      await api("DELETE", `/api/conversations/${state.current.id}`);
      delete state.drafts[state.current.id];
      state.current = null;
      go("#/");
    } catch (e) { showError(e.message); }
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
  $("#close-preview").addEventListener("click", () => $("#preview-dialog").close());
  $("#upload").addEventListener("change", (e) => { if (e.target.files[0]) uploadFile(e.target.files[0]); e.target.value = ""; });
  window.addEventListener("hashchange", route);

  try {
    state.settings = await api("GET", "/api/settings");
  } catch (e) {
    $("#view-list").hidden = false;
    $("#work-list").replaceChildren(el("li", { class: "empty" }, e.message));
    return;
  }
  updateModelLabel();
  await route();
  if (!state.settings.configured) openSettings(true);
}

init();
