"use strict";
// Platform Client UI. All server text is rendered with textContent only.

const $ = (id) => document.getElementById(id);
let me = null;
let pollTimer = null;
let currentConversation = null;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c instanceof Node ? c : String(c));
  return node;
}

function banner(text, kind = "error") {
  const b = $("banner");
  b.className = kind === "info" ? "info" : "";
  b.textContent = text || "";
  b.hidden = !text;
}

async function api(path, options = {}) {
  const init = { credentials: "same-origin", headers: {}, ...options };
  if (options.json !== undefined) {
    init.method = init.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.json);
  }
  const res = await fetch(path, init);
  if (res.status === 401 && path !== "/api/login") {
    me = null;
    showView("login");
    banner("Your Platform session ended. Sign in again.", "info");
    throw new Error("signed-out");
  }
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const ref = body.correlation_id ? ` (reference ${body.correlation_id})` : "";
    throw new Error((body.detail || `Request failed (${res.status})`) + ref);
  }
  return body;
}

function fail(err) { if (err.message !== "signed-out") banner(err.message); }

function elapsed(seconds) {
  if (seconds == null) return "";
  const m = Math.floor(seconds / 60), s = seconds % 60;
  return m ? `${m} min ${s}s` : `${s}s`;
}

// Polling never stops on its own: long Agent work is normal.
function pollDelay(seconds) { return seconds < 60 ? 3000 : seconds < 900 ? 10000 : 30000; }

function stopPolling() { if (pollTimer) clearTimeout(pollTimer); pollTimer = null; }

function showView(name) {
  for (const v of document.querySelectorAll(".view")) v.hidden = v.id !== `view-${name}`;
  $("nav").hidden = !me;
  $("logout").hidden = !me;
}

async function refreshStatus() {
  const s = await fetch("/api/status", { credentials: "same-origin" }).then((r) => r.json());
  me = s.signed_in ? s.user : null;
  $("platform").textContent = `Platform: ${s.platform} · Client Interface ${s.client_interface}`;
  $("who").textContent = me ? `${me.display_name} (${me.email})` : "Not signed in";
  $("expiry").textContent = me && s.session_expires_at
    ? `Session valid until ${new Date(s.session_expires_at * 1000).toLocaleTimeString()}` : "";
  if (s.revocation_pending) banner("The previous Platform session has not been confirmed ended yet.", "info");
  return s;
}

// -- Sign in / out ------------------------------------------------------------

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pw = $("login-password");
  try {
    await api("/api/login", { json: { email: $("login-email").value, password: pw.value } });
    pw.value = "";
    banner("");
    await refreshStatus();
    location.hash = "#/tasks";
    route();
  } catch (err) { pw.value = ""; fail(err); }
});

$("logout").addEventListener("click", async () => {
  stopPolling();
  try {
    await api("/api/logout", { method: "POST" });
    banner("Signed out. The Platform confirmed the session ended.", "info");
  } catch (err) { fail(err); }
  me = null;
  await refreshStatus();
  showView("login");
});

// -- New Task (Platform templates) --------------------------------------------

let templates = [];
let chosen = null;

async function loadTemplates() {
  templates = await api("/api/templates");
  const list = $("template-list");
  list.replaceChildren();
  $("template-form").hidden = true;
  if (!templates.length) list.append(el("p", { class: "muted" }, "The Platform has no enabled templates for you."));
  for (const t of templates) {
    list.append(el("div", { class: "card", tabindex: "0", onclick: () => chooseTemplate(t) },
      el("strong", {}, t.name), el("span", { class: "tag" }, t.kind === "task" ? "Task" : "Chat"),
      el("p", { class: "muted small" }, t.description)));
  }
}

function areaInput(area) {
  const id = `area-${area.slot}`;
  let input;
  if (area.type === "textarea") input = el("textarea", { id, rows: "3" });
  else if (area.type === "select" || area.type === "multiselect") {
    input = el("select", { id, ...(area.type === "multiselect" ? { multiple: "" } : {}) },
      ...(area.type === "select" ? [el("option", { value: "" }, "")] : []),
      ...area.options.map((o) => el("option", { value: o }, o)));
  } else input = el("input", { id, type: { number: "number", date: "date" }[area.type] || "text" });
  if (area.required) input.required = true;
  return el("label", {}, `${area.label}${area.required ? " *" : ""}`,
    area.description ? el("span", { class: "small muted" }, ` - ${area.description}`) : null, input);
}

function chooseTemplate(t) {
  chosen = t;
  $("template-name").textContent = t.name;
  $("template-desc").textContent = t.description;
  const areas = $("template-areas");
  areas.replaceChildren();
  let section = null;
  for (const a of t.areas) {
    if (a.section && a.section !== section) { section = a.section; areas.append(el("h3", {}, section)); }
    areas.append(areaInput(a));
  }
  $("additional-label").textContent = t.additional_instructions.label + (t.additional_instructions.required ? " *" : "");
  $("template-additional").placeholder = t.additional_instructions.placeholder || "";
  $("template-additional").value = "";
  $("template-form").hidden = false;
}

$("template-cancel").addEventListener("click", () => { $("template-form").hidden = true; });

$("template-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const values = {};
  for (const a of chosen.areas) {
    const input = $(`area-${a.slot}`);
    values[String(a.slot)] = a.type === "multiselect"
      ? [...input.selectedOptions].map((o) => o.value) : input.value;
  }
  try {
    const sub = await api("/api/submit", { json: {
      template_id: chosen.id, values, additional: $("template-additional").value } });
    trackSubmission(sub);
  } catch (err) { fail(err); }
});

// -- Submissions: the Platform may take many minutes to answer -----------------

function trackSubmission(sub) {
  stopPolling();
  location.hash = `#/chat/${sub.conversation_id}`;
  const tick = async () => {
    try {
      const s = await api(`/api/submissions/${sub.submission_id}`);
      const conv = await api(`/api/conversations/${sub.conversation_id}`);
      const n = s.task_number ?? conv.task?.task_number;
      renderConversation(conv);
      if (s.state === "running") {
        banner(`Submitted to the Platform - waiting for the Agent (${elapsed(s.elapsed_seconds)}).` +
          (n ? ` Task #${n}.` : ""), "info");
        if (n) { location.hash = `#/task/${n}`; return; }
        pollTimer = setTimeout(tick, pollDelay(s.elapsed_seconds));
        return;
      }
      if (s.state === "error" || s.state === "unknown") banner(s.error);
      else banner(n ? "" : `The Platform answered (${s.execution_status}).`, "info");
      if (n) location.hash = `#/task/${n}`;
    } catch (err) { fail(err); }
  };
  tick();
}

// -- Chat ------------------------------------------------------------------------

function renderMessages(container, messages) {
  container.replaceChildren(...messages.map((m) => el("div", { class: `msg ${m.role}` },
    el("span", { class: "who" }, m.role === "user" ? "You" : (m.executing_agent_name || m.agent_name || "Agent"),
      " · ", new Date(m.created_at).toLocaleString()),
    m.content)));
  container.scrollTop = container.scrollHeight;
}

function renderConversation(conv) {
  currentConversation = conv.conversation.id;
  $("chat-task").textContent = conv.task?.task_number
    ? `Task #${conv.task.task_number}: ${conv.task.title}` : "No Task yet - the Platform decides whether this becomes one.";
  renderMessages($("messages"), conv.messages);
}

async function loadChat(cid) {
  const convs = await api("/api/conversations");
  $("conversation-list").replaceChildren(...convs.map((c) => el("button", {
    class: c.id === cid ? "active" : "", onclick: () => { location.hash = `#/chat/${c.id}`; } },
    c.title || "Conversation", el("div", { class: "small muted" }, new Date(c.updated_at).toLocaleString()))));
  if (cid) renderConversation(await api(`/api/conversations/${cid}`));
  else { currentConversation = null; $("messages").replaceChildren(); $("chat-task").textContent = ""; }
}

$("chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const content = $("chat-input").value.trim();
  if (!content) return;
  try {
    const sub = await api("/api/submit", { json: { content, conversation_id: currentConversation } });
    $("chat-input").value = "";
    trackSubmission(sub);
  } catch (err) { fail(err); }
});

$("chat-new").addEventListener("click", () => { location.hash = "#/chat"; });

// -- Tasks --------------------------------------------------------------------------

async function loadTasks(attention, search = "") {
  $("tasks-heading").textContent = attention ? "Needs My Attention" : "My Tasks";
  const page = await api(`/api/tasks?${new URLSearchParams({
    ...(attention ? { queue: "attention" } : {}), ...(search ? { search } : {}) })}`);
  $("task-rows").replaceChildren(...page.items.map((t) => el("tr", { onclick: () => { location.hash = `#/task/${t.task_number}`; } },
    el("td", {}, `#${t.task_number}`), el("td", {}, t.title),
    el("td", {}, t.status, t.needs_attention ? el("span", { class: "tag" }, "needs you") : null),
    el("td", {}, t.work_pending ? "Work pending" : (t.work_status || "")),
    el("td", {}, new Date(t.last_activity_at).toLocaleString()))));
  if (!page.items.length) $("task-rows").append(el("tr", {}, el("td", { colspan: "5", class: "muted" }, "No Tasks.")));
}

$("task-search").addEventListener("submit", (e) => { e.preventDefault(); loadTasks(false, $("task-query").value); });

function when(ts) { return ts ? new Date(ts).toLocaleString() : ""; }

function since(ts) { return ts ? Math.max(0, Math.round((Date.now() - new Date(ts).getTime()) / 1000)) : null; }

// Lifecycle, Agent execution and your decisions are separate facts: never merged.
function renderProgress(p) {
  const box = $("task-progress");
  const ex = p.execution, dec = p.decision;
  box.className = `card progress exec-${ex.state}${dec.yours_to_decide ? " needs-you" : ""}`;
  const row = (label, ...value) => el("div", { class: "fact" }, el("dt", {}, label), el("dd", {}, ...value));

  let execution;
  if (ex.state === "running") {
    execution = [el("span", { class: "running" }, `${ex.agent_name || "Agent"} is working now`),
      ` - run started ${when(ex.started_at)} (${elapsed(since(ex.started_at))} ago). `,
      el("span", { class: "muted" }, "Agent work often takes 4-11 minutes; this page keeps checking.")];
  } else if (ex.state === "queued") {
    execution = ["Work accepted by the Platform; no run has started yet."];
  } else if (ex.state === "stopped") {
    const last = ex.last_period;
    execution = [`Not running. Last run ended ${when(last.ended_at)}`,
      last.duration_seconds != null ? ` after ${elapsed(last.duration_seconds)}` : "",
      ` - recorded by the Platform as `, el("strong", {}, last.status),
      last.failure_reason ? ` (reason: "${last.failure_reason}")` : "", "."];
  } else execution = ["No work has run yet."];

  const mine = dec.pending_approvals.filter((a) => a.can_decide);
  const others = dec.pending_approvals.length - mine.length;
  const decision = mine.length
    ? [el("strong", {}, `${mine.length} approval request${mine.length > 1 ? "s" : ""} waiting for your decision: `),
       mine.map((a) => `${a.reference} - ${a.title}${a.expires_at ? ` (expires ${when(a.expires_at)})` : ""}`).join("; "),
       " - see Approvals below."]
    : [others ? `${others} approval request(s) pending, not yours to decide.` : "No approval request is waiting for you."];

  const items = [row("Task", p.lifecycle.closed ? "Closed" : "Open"),
    row("Agent execution", ...execution), row("Your decision", ...decision)];
  const u = p.agent_report;
  if (u) {
    items.push(el("div", { class: "small muted report-heading" },
      `Agent's own Task Update, saved ${when(u.saved_at)}`,
      u.stale ? el("span", { class: "tag stale" }, "saved before the latest run - may be out of date") : null));
    items.push(el("dl", { class: "update" },
      ...[["Status", u.status], ["Completed", u.completed], ["Work product", u.important_work_product],
          ["Remaining", u.remaining], ["Dependency", u.dependency], ["Next", u.next_action],
          ["Asks you", u.user_action]].flatMap(([k, v]) => v ? [el("dt", {}, k), el("dd", {}, v)] : [])));
  }
  box.replaceChildren(el("dl", { class: "facts" }, ...items.slice(0, 3)), ...items.slice(3));
}

function renderApprovals(n, approvals) {
  const box = $("task-approvals");
  if (!approvals.length) { box.replaceChildren(el("p", { class: "muted" }, "None.")); return; }
  box.replaceChildren(...approvals.map((a) => {
    const comment = el("input", { placeholder: "Comment (optional)", maxlength: "2000" });
    const decide = (approve) => async () => {
      try { await api(`/api/tasks/${n}/approvals/${a.approval_id}`, { json: { approve, comment: comment.value || null } }); loadTask(n); }
      catch (err) { fail(err); }
    };
    const actions = a.actions.map((x) => el("li", {}, `${x.tool} (${x.binding}, used ${x.uses}/${x.max_uses})`,
      x.email ? el("pre", { class: "content" }, `To: ${x.email.recipient}\nSubject: ${x.email.subject}\n\n${x.email.body}`) : null));
    return el("div", { class: "card" },
      el("strong", {}, `${a.reference}: ${a.title}`), el("span", { class: "tag" }, a.status),
      el("p", {}, a.proposal), a.details ? el("p", { class: "muted" }, a.details) : null,
      a.conditions ? el("p", { class: "small" }, `Conditions: ${a.conditions}`) : null,
      actions.length ? el("ul", {}, ...actions) : null,
      a.status === "pending" && a.can_decide ? el("div", { class: "row" }, comment,
        el("button", { class: "primary", onclick: decide(true) }, "Approve"),
        el("button", { class: "danger", onclick: decide(false) }, "Reject")) : null);
  }));
}

async function renderResults(n) {
  const results = await api(`/api/tasks/${n}/results`);
  $("task-results").replaceChildren(...(results.length ? results.map((r) => {
    const body = el("pre", { class: "content" });
    body.hidden = true;
    return el("div", { class: "card" },
      el("strong", {}, r.title || (r.kind === "reply" ? "Agent reply" : r.kind)), el("span", { class: "tag" }, r.kind),
      el("div", { class: "small muted" }, `${r.agent_name || ""} · ${new Date(r.created_at).toLocaleString()}`),
      el("p", {}, r.summary),
      el("button", { onclick: async () => {
        if (body.hidden && !body.textContent) body.textContent = (await api(`/api/tasks/${n}/results/${r.result_id}`)).content;
        body.hidden = !body.hidden;
      } }, "Show full result"), body);
  }) : [el("p", { class: "muted" }, "No results yet.")]));
  const files = await api(`/api/tasks/${n}/files`);
  $("task-files").replaceChildren(...(files.length ? files.map((f) => el("div", {},
    el("a", { href: `/api/tasks/${n}/files/${encodeURIComponent(f.file_id)}` }, f.file_name),
    el("span", { class: "small muted" }, ` ${f.content_type}, ${f.size_bytes} bytes, v${f.version}, sha256 ${f.sha256.slice(0, 12)}…`)))
    : [el("p", { class: "muted" }, "No files.")]));
}

function renderHistory(task, history) {
  $("task-history").replaceChildren(...(history.periods.length ? history.periods.map((p) => el("div", { class: "small" },
    `${new Date(p.started_at).toLocaleString()} - ${p.ended_at ? new Date(p.ended_at).toLocaleString() : "running"}: `,
    `${p.status} (${p.trigger})${p.agent_name ? ` by ${p.agent_name}` : ""}${p.failure_reason ? ` - ${p.failure_reason}` : ""}`))
    : [el("p", { class: "muted" }, "No recorded work yet.")]));
  renderMessages($("task-conversation"), task.history);
}

let lastResultsKey = null;

async function loadTask(n) {
  stopPolling();
  const view = await api(`/api/tasks/${n}`);
  const t = view.task, p = view.progress;
  $("task-title").textContent = `Task #${t.task_number}: ${t.title}`;
  renderProgress(p);
  renderApprovals(n, view.approvals);
  renderHistory(t, view.work_history);
  const key = `${n}:${t.last_activity_at}:${p.execution.state}`;
  if (key !== lastResultsKey) { lastResultsKey = key; await renderResults(n); }
  $("task-close").hidden = t.status === "completed";
  $("task-continue").hidden = !t.can_continue_in_chat;
  $("task-close").onclick = async () => {
    try { await api(`/api/tasks/${n}/complete`, { method: "POST" }); loadTask(n); } catch (err) { fail(err); }
  };
  $("task-continue").onclick = async () => {
    try { const c = await api(`/api/tasks/${n}/continue`, { method: "POST" }); location.hash = `#/chat/${c.id}`; }
    catch (err) { fail(err); }
  };
  if (p.active) banner("");
  if (!p.terminal) {
    const delay = p.active ? pollDelay(since(p.execution.started_at) || 0) : 30000;
    pollTimer = setTimeout(() => { if (location.hash === `#/task/${n}`) loadTask(n).catch(fail); }, delay);
  }
}

// -- Routing --------------------------------------------------------------------------

async function route() {
  stopPolling();
  if (!me) { showView("login"); return; }
  const [, page, arg] = location.hash.split("/");
  for (const a of document.querySelectorAll("#nav a")) a.classList.toggle("active", a.getAttribute("href") === location.hash);
  try {
    if (page === "new") { showView("new"); await loadTemplates(); }
    else if (page === "chat") { showView("chat"); await loadChat(arg); }
    else if (page === "task" && /^\d+$/.test(arg || "")) { showView("task"); await loadTask(Number(arg)); }
    else { showView("tasks"); await loadTasks(arg === "attention"); }
  } catch (err) { fail(err); }
}

window.addEventListener("hashchange", route);
refreshStatus().then(route).catch(() => banner("The Platform Client is not reachable."));
