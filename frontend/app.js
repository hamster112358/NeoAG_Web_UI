/* NeoAG Workspace - frontend logic (vanilla JS, no build step).
   Data comes from /api/bootstrap + per-conversation fetches + SSE.
   All writes go through the API; the browser only renders
   server-persisted state. Layout contract: nothing at page level
   scrolls - sidebar, chat log, work body and the drawer each own
   their scroll; the composer stays pinned at the bottom. */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

const state = {
  boot: null, params: null, view: "tasks", kindFilter: "",
  jobDetail: null, resultsJob: null,
  manifest: null, agentMode: "mock",
  convs: [], activeCid: null, convMsgs: [],
};

/* ---------------- API ---------------- */
async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!r.ok) throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status, data });
  return data;
}

function toast(msg, err) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("err", !!err);
  t.classList.remove("hidden");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.add("hidden"), 4200);
}

function ago(ts) {
  if (!ts) return "";
  const d = (Date.now() - new Date(ts).getTime()) / 1000;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  if (d < 7 * 86400) return `${Math.floor(d / 86400)}d ago`;
  return new Date(ts).toLocaleDateString();
}

/* ---------------- markdown (help docs) ---------------- */
function mdToHtml(md) {
  const out = [];
  let inCode = false, inList = false;
  for (const raw of md.split("\n")) {
    const line = raw;
    if (line.startsWith("```")) {
      if (inCode) { out.push("</pre>"); inCode = false; }
      else { if (inList) { out.push("</ul>"); inList = false; }
        out.push("<pre>"); inCode = true; }
      continue;
    }
    if (inCode) { out.push(esc(line)); continue; }
    const h = line.match(/^(#{1,4})\s+(.*)/);
    if (h) {
      if (inList) { out.push("</ul>"); inList = false; }
      const lvl = Math.min(3, h[1].length + 1);
      out.push(`<h${lvl}>${mdInline(h[2])}</h${lvl}>`); continue;
    }
    const li = line.match(/^\s*[-*]\s+(.*)/);
    if (li) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${mdInline(li[1])}</li>`); continue;
    }
    if (inList) { out.push("</ul>"); inList = false; }
    if (/^\s*\|/.test(line)) { out.push(`<p>${mdInline(line.replace(/\|/g, " "))}</p>`); continue; }
    if (line.trim() === "") { continue; }
    const bq = line.match(/^>\s?(.*)/);
    if (bq) { out.push(`<blockquote>${mdInline(bq[1])}</blockquote>`); continue; }
    out.push(`<p>${mdInline(line)}</p>`);
  }
  if (inList) out.push("</ul>");
  if (inCode) out.push("</pre>");
  return out.join("\n");
}
function mdInline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1 ($2)");
}

/* ---------------- boot ---------------- */
async function boot() {
  try {
    state.boot = await api("GET", "/api/bootstrap");
    state.agentMode = state.boot.agent_mode || "mock";
    state.convs = state.boot.conversations || [];
    state.activeCid = state.boot.active_conv || null;
    state.params = await api("GET", "/api/params");
    renderAgentBadge();
    renderSidebar();
    await openConv(state.activeCid, true);
    renderTasks();
    renderJobs();
    renderAttention();
    renderResults();
    renderDiscovery();
    renderHelp();
    initSplitter();
    connectSSE();
  } catch (e) {
    if (e.status === 401) return; // browser shows the login prompt
    toast("boot failed: " + e.message, true);
  }
}

let es = null;
function connectSSE() {
  if (es) es.close();
  es = new EventSource("/api/stream");
  // Reconnect catch-up: events published while the stream was down are
  // gone, so (re)opening always refetches the visible state. SSE is
  // best-effort - the streaming watchdog below is the guarantee.
  es.onopen = () => {
    $("conn-dot").className = "dot on"; $("conn-text").textContent = "live";
    refreshConv(); refreshSidebar(); refreshJobs(); renderAttention();
  };
  es.onerror = () => { $("conn-dot").className = "dot off"; $("conn-text").textContent = "reconnecting"; };
  es.onmessage = (m) => {
    let ev = {};
    try { ev = JSON.parse(m.data); } catch { return; }
    if (ev.type === "chat") {
      if (!ev.conv_id || ev.conv_id === state.activeCid) refreshConv();
      refreshSidebar();
    } else if (ev.type === "conversations") refreshSidebar();
    else if (ev.type === "job" || ev.type === "approval") {
      refreshJobs(); renderAttention(); refreshConv();
    } else if (ev.type === "research") refreshDiscovery();
    else if (ev.type === "manifest") refreshManifests();
    else if (ev.type === "agent_mode") renderAgentBadge();
    else if (ev.type === "records_changed") boot();
  };
}

/* ---------------- conversation sidebar ---------------- */
function renderSidebar() {
  const host = $("conv-list");
  host.innerHTML = (state.convs || []).map((c) => `
    <div class="conv-item ${c.id === state.activeCid ? "active" : ""}"
         data-conv="${esc(c.id)}">
      <div class="ct">${esc(c.title || "New chat")}</div>
      <div class="cm">
        ${(c.job_state === "working" || c.job_state === "blocked")
          ? '<span class="live" title="agent session live"></span>' : ""}
        <span>${ago(c.updated_at)}</span><span>-</span>
        <span>${c.n_messages || 0} msg</span>
      </div>
      <div class="cops">
        <button class="icon-btn" data-ren="${esc(c.id)}" title="Rename">ren</button>
        <button class="icon-btn danger" data-del="${esc(c.id)}" title="Delete (kept on server)">del</button>
      </div>
    </div>`).join("") || '<div class="empty">No chats yet.</div>';
  host.querySelectorAll("[data-conv]").forEach((el) =>
    el.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-ren],[data-del]")) return;
      openConv(el.dataset.conv);
    }));
  host.querySelectorAll("[data-ren]").forEach((el) =>
    el.addEventListener("click", () => startRename(el.dataset.ren)));
  host.querySelectorAll("[data-del]").forEach((el) =>
    el.addEventListener("click", async () => {
      const c = state.convs.find((x) => x.id === el.dataset.del);
      if (!confirm(`Delete chat "${(c && c.title) || "New chat"}"?\n\nIt disappears from this list; the saved messages stay on the server for audit.`)) return;
      try {
        await api("POST", `/api/conversations/${el.dataset.del}/delete`, {});
        if (state.activeCid === el.dataset.del) {
          state.activeCid = null;
          await refreshSidebar();
          await openNextConv();
        } else await refreshSidebar();
      } catch (e) { toast("delete failed: " + e.message, true); }
    }));
}

async function refreshSidebar() {
  try { state.convs = (await api("GET", "/api/conversations")).conversations; }
  catch { return; }
  renderSidebar();
}

async function openNextConv() {
  await openConv(state.convs.length ? state.convs[0].id : null);
}

async function openConv(cid, quiet) {
  state.activeCid = cid;
  renderSidebar();
  if (!cid) { state.convMsgs = []; renderChat(); return; }
  try {
    const d = await api("GET", `/api/conversations/${cid}`);
    state.convMsgs = d.messages;
    state.convObj = d.conversation;
  } catch (e) {
    if (!quiet) toast("cannot open chat: " + e.message, true);
    state.convMsgs = []; state.convObj = null;
  }
  renderChat();
}

async function refreshConv() {
  if (!state.activeCid) return;
  try {
    const d = await api("GET", `/api/conversations/${state.activeCid}`);
    state.convMsgs = d.messages;
    state.convObj = d.conversation;
    renderChat();
  } catch { /* transient */ }
}

/* Streaming watchdog: while the visible chat shows a streaming message,
   refetch every 3 s whether SSE is alive or not - reverse proxies can
   buffer or kill SSE, and missed events are never replayed. The server
   always stamps a terminal turn_state (completed / failed / cancelled /
   timed out / connection lost / interrupted), so this polling stops by
   itself once the turn honestly ends. */
setInterval(() => {
  if (!state.activeCid) return;
  const inflight = (state.convMsgs || []).slice(-8).some((m) => {
    const mt = m.meta || {};
    return mt.turn_state === "streaming" || (mt.badges || []).includes("streaming");
  });
  if (inflight) refreshConv();
}, 3000);

$("new-chat").addEventListener("click", async () => {
  try {
    const r = await api("POST", "/api/conversations", {});
    state.activeCid = r.conversation.id;
    await refreshSidebar();
    await openConv(r.conversation.id);
    $("chat-input").focus();
  } catch (e) { toast("new chat failed: " + e.message, true); }
});

function startRename(cid) {
  const item = document.querySelector(`[data-conv="${CSS.escape(cid)}"] .ct`);
  const conv = state.convs.find((c) => c.id === cid) || {};
  if (!item) return;
  const old = conv.title || "New chat";
  item.innerHTML = `<input class="rename" value="${esc(old)}">`;
  const inp = item.querySelector("input");
  inp.focus(); inp.select();
  const commit = async () => {
    const t = inp.value.trim();
    if (t && t !== old) {
      try { await api("POST", `/api/conversations/${cid}/rename`, { title: t }); }
      catch (e) { toast("rename failed: " + e.message, true); }
    }
    await refreshSidebar();
    if (cid === state.activeCid) renderChat();
  };
  inp.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") commit();
    if (ev.key === "Escape") { renderSidebar(); if (cid === state.activeCid) renderChat(); }
  });
  inp.addEventListener("blur", commit);
}

$("chat-rename").addEventListener("click", () => {
  if (state.activeCid) startRename(state.activeCid);
});
$("chat-title").addEventListener("dblclick", () => {
  if (state.activeCid) startRename(state.activeCid);
});

/* ---------------- chat pane ---------------- */
function msgHtml(m) {
  const meta = m.meta || {};
  const badges = (meta.badges || []).map((b) =>
    `<span class="badge">${esc(b)}</span>`).join("");
  return `<div class="msg ${esc(m.role)}">
    <div class="meta"><span>${esc(m.role)}</span>${badges}</div>
    <div class="body">${esc(m.text)}</div></div>`;
}

function gateCardHtml(a) {
  let info = {};
  try { info = JSON.parse(a.detail || "{}"); } catch { /* raw */ }
  return `<div class="inline-card confirm">
    <div class="q">Confirm destructive operation</div>
    <div class="sub">The execution gate blocked this before it ran. Approving
      unlocks exactly this one operation; nothing else. It is also recorded
      in the audit trail.</div>
    <div class="detail">${esc(info.note || a.detail || "")}</div>
    <div class="btnrow">
      <button class="btn primary sm" data-appr="${esc(a.id)}" data-ok="1">Approve once</button>
      <button class="btn danger sm" data-appr="${esc(a.id)}">Deny</button>
      <span class="hint">op ${esc((info.op || "").slice(0, 12))}</span>
    </div></div>`;
}

function renderChat() {
  const log = $("chat-log");
  const conv = state.convObj;
  $("chat-title").textContent =
    (conv && (conv.title || "New chat")) || "New chat";
  const st = conv && conv.job_state;
  const chip = $("chat-state");
  if (st === "working" || st === "blocked") {
    chip.classList.remove("hidden");
    chip.className = "pill accent";
    chip.textContent = st === "working" ? "agent working" : "awaiting confirmation";
  } else chip.classList.add("hidden");

  let html = (state.convMsgs || []).map(msgHtml).join("");
  const gateCards = (state.boot?.approvals || []).filter((a) => {
    if (a.state !== "pending" || a.action !== "gate_op") return false;
    try { return JSON.parse(a.detail || "{}").conv_id === state.activeCid; }
    catch { return false; }
  });
  for (const a of gateCards) html += gateCardHtml(a);
  if (!(state.convMsgs || []).length && !gateCards.length) {
    html += `<div class="emptychat">
      <div class="et">Start a conversation</div>
      <div class="es">Ask anything about NeoAG, or describe a sample and where its data live.</div>
      <button id="load-example">Load example - walk through a synthetic case</button>
    </div>`;
  }
  log.innerHTML = html;
  log.scrollTop = log.scrollHeight;
  const le = $("load-example");
  if (le) le.addEventListener("click", loadExample);
}

async function loadExample() {
  try {
    if (!state.activeCid) {
      const r = await api("POST", "/api/conversations", {});
      state.activeCid = r.conversation.id;
    }
    await api("POST", `/api/conversations/${state.activeCid}/example`, {});
    await refreshConv();
    await refreshSidebar();
    await refreshJobs();
  } catch (e) { toast("example failed: " + e.message, true); }
}

$("chat-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const text = $("chat-input").value.trim();
  if (!text) return;
  $("chat-input").value = "";
  try {
    const r = await api("POST", "/api/chat",
                        { text, conv_id: state.activeCid || "" });
    state.activeCid = r.conv_id;
    await refreshConv();
    await refreshSidebar();
    await refreshJobs();
  } catch (e) { toast("send failed: " + e.message, true); }
});
$("chat-input").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    $("chat-form").requestSubmit();
  }
});

async function refreshJobs() {
  try {
    const b = await api("GET", "/api/bootstrap");
    state.boot.jobs = b.jobs; state.boot.approvals = b.approvals;
    state.boot.decisions = b.decisions;
    renderJobs(); renderAttention();
  } catch (e) { /* transient */ }
}

/* ---------------- splitter ---------------- */
function initSplitter() {
  const shell = $("shell"), sp = $("splitter");
  const saved = localStorage.getItem("neoag.chatw");
  if (saved) shell.style.setProperty("--chatw", saved);
  let drag = null;
  sp.addEventListener("pointerdown", (ev) => {
    drag = { x: ev.clientX,
             w: parseInt(getComputedStyle(shell)
                  .getPropertyValue("--chatw")) || 400 };
    sp.classList.add("drag");
    sp.setPointerCapture(ev.pointerId);
  });
  sp.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const w = Math.min(760, Math.max(300, drag.w + ev.clientX - drag.x));
    shell.style.setProperty("--chatw", w + "px");
  });
  const end = () => {
    if (!drag) return;
    drag = null; sp.classList.remove("drag");
    localStorage.setItem("neoag.chatw",
      shell.style.getPropertyValue("--chatw") || "400px");
  };
  sp.addEventListener("pointerup", end);
  sp.addEventListener("pointercancel", end);
}

/* ---------------- tasks ---------------- */
function renderTasks() {
  const sel = $("nj-sample");
  sel.innerHTML = (state.boot.samples || []).map((s) =>
    `<option value="${esc(s.id)}">${esc(s.id)} - ${esc(s.label)}</option>`).join("");
  const stages = $("nj-stages");
  stages.innerHTML = (state.boot.stages || []).map((s) => {
    const dep = (s.depends_on || []).length
      ? `needs: ${s.depends_on.join(", ")}` : "independent entry";
    return `<label class="stage-row">
      <input type="checkbox" data-stage="${esc(s.id)}" ${s.required ? "checked" : ""} ${s.required ? "disabled" : ""}>
      <span>${esc(s.name)}</span>
      ${s.required ? '<span class="req">required</span>' : ""}
      <span class="dep">${esc(dep)}</span></label>`;
  }).join("");
  $("adv-count").textContent =
    `${state.params ? state.params.groups.length : 0} parameter groups from the real pipeline code`;
  renderAdvanced();
}

function renderAdvanced() {
  const host = $("adv-params");
  if (!state.params) { host.innerHTML = "<p class='hint'>inventory unavailable</p>"; return; }
  host.innerHTML = state.params.groups.map((g) => `
    <div class="pgroup ${g.expert ? "expert" : ""}">
      <h3>${esc(g.group)}</h3>
      ${g.items.map((p) => pitemHtml(p)).join("")}
    </div>`).join("");
  host.querySelectorAll("input,select").forEach((el) => {
    el.addEventListener("change", collectAdvanced);
  });
  collectAdvanced();
}

function pitemHtml(p) {
  let ctrl;
  if (p.type === "choice") {
    ctrl = `<select data-param="${esc(p.id)}">${(p.values || []).map((v) =>
      `<option>${esc(v)}</option>`).join("")}</select>`;
  } else if (p.type === "flag") {
    ctrl = `<input type="checkbox" data-param="${esc(p.id)}">`;
  } else {
    ctrl = `<input type="text" data-param="${esc(p.id)}" placeholder="${esc(p.default || "")}">`;
  }
  return `<div class="pitem">
    <div><div class="pname">${esc(p.label)}</div><div class="pdef">default: ${esc(p.default ?? "none")}</div></div>
    <div>${ctrl}</div>
    <div class="pdesc">${esc(p.desc || "")}</div>
    <div class="psrc">${esc(p.source || "")}</div>
  </div>`;
}

function collectAdvanced() {
  const cfg = {};
  document.querySelectorAll("[data-param]").forEach((el) => {
    if (el.type === "checkbox") {
      if (el.checked) cfg[el.dataset.param] = true;
    } else if (el.value !== "") {
      cfg[el.dataset.param] = el.value;
    }
  });
  state.requestedConfig = cfg;
  return cfg;
}

/* manifest */
$("man-propose").addEventListener("click", async () => {
  const desc = $("man-desc").value.trim();
  if (!desc) { toast("describe the data location first", true); return; }
  try {
    const m = await api("POST", "/api/manifests/propose", { description: desc });
    state.manifest = m;
    renderManifest();
    toast("manifest proposed - review below");
  } catch (e) { toast("propose failed: " + e.message, true); }
});
function renderManifest() {
  const m = state.manifest;
  const host = $("manifest-view");
  if (!m) { host.innerHTML = ""; return; }
  const rows = (m.proposal.inputs || []).map((i) => `
    <tr><td><b>${esc(i.role)}</b></td><td><code>${esc(i.path)}</code></td>
    <td>${esc(i.confidence)}</td><td>${esc(i.basis)}</td></tr>`).join("");
  const amb = (m.proposal.ambiguous || []).map((x) => `<li>${esc(x)}</li>`).join("");
  const mis = (m.proposal.missing || []).map((x) => `<li>${esc(x)}</li>`).join("");
  host.innerHTML = `<div class="manifest-card">
    <div class="mnote">${esc(m.proposal.note)}</div>
    <div class="tablewrap"><table class="data">
      <thead><tr><th>Role</th><th>Path</th><th>Confidence</th><th>Basis</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    ${mis ? `<p class="hint">Missing: <ul>${mis}</ul></p>` : ""}
    ${amb ? `<p class="hint">Ambiguous: <ul>${amb}</ul></p>` : ""}
    ${m.status === "proposed"
      ? `<div class="btnrow">
           <button class="btn primary sm" id="man-confirm">Confirm manifest</button>
           <span class="hint">or correct it in chat before confirming</span>
         </div>`
      : `<span class="pill ok">confirmed</span>`}
  </div>`;
  const cb = $("man-confirm");
  if (cb) cb.addEventListener("click", async () => {
    try {
      const m2 = await api("POST", `/api/manifests/${m.id}/confirm`, { edits: {} });
      state.manifest = m2; renderManifest();
    } catch (e) { toast("confirm failed: " + e.message, true); }
  });
}
async function refreshManifests() {
  state.manifest = (state.manifest && state.manifest.id)
    ? await api("GET", `/api/manifests/${state.manifest.id}`) : null;
  renderManifest();
}

/* submit */
$("nj-submit").addEventListener("click", async () => {
  const sample = $("nj-sample").value;
  const stage_ids = [...document.querySelectorAll("[data-stage]")]
    .filter((c) => c.checked).map((c) => c.dataset.stage);
  const execution_mode = document.querySelector(
    'input[name="execmode"]:checked').value;
  const simulate_failure = $("nj-fail").checked;
  const body = { sample_id: sample, stage_ids, mode: "auto",
                 execution_mode, simulate_failure,
                 kind: "analysis", requested_config: collectAdvanced() };
  if (state.manifest && state.manifest.status === "confirmed")
    body.manifest_id = state.manifest.id;
  try {
    const res = await api("POST", "/api/jobs", body);
    toast(`job ${res.job_id} waiting for your approval (Tasks panel above)`);
    await refreshJobs();
  } catch (e) { toast("submit failed: " + e.message, true); }
});

/* attention strip: anything blocked on a human click */
function renderAttention() {
  const host = $("attention");
  const rows = [];
  for (const a of (state.boot.approvals || [])) {
    if (a.state !== "pending" || a.action === "gate_op") continue;
    rows.push(`<div class="at-row">
      <span><b>Execute approval</b> - job ${esc(a.job_id)} (${esc(a.risk)} risk)</span>
      <span class="btnrow" style="margin:0">
        <button class="btn primary sm" data-appr="${esc(a.id)}" data-ok="1">Approve</button>
        <button class="btn danger sm" data-appr="${esc(a.id)}">Deny</button>
      </span></div>`);
  }
  for (const d of (state.boot.decisions || [])) {
    if (d.status !== "pending") continue;
    rows.push(`<div class="at-row">
      <span><b>Decision</b> - ${esc(d.question)}</span>
      <span class="btnrow" style="margin:0">${(d.options || []).map((o) =>
        `<button class="btn sm" data-dec="${esc(d.id)}" data-choice="${esc(o.id)}">${esc(o.label)}${o.validated ? " (validated)" : ""}</button>`).join("")}</span></div>`);
  }
  if (!rows.length) { host.classList.add("hidden"); host.innerHTML = ""; return; }
  host.classList.remove("hidden");
  host.innerHTML = `<b>${rows.length} item${rows.length > 1 ? "s" : ""} waiting for you</b>` +
    rows.join("");
}

/* ---------------- jobs ---------------- */
const ACTIVE_STATES = ["queued", "running", "paused", "blocked"];
function statusPill(st) {
  const cls = { completed: "ok", running: "accent", queued: "accent",
                blocked: "warn", paused: "warn", failed: "danger",
                cancelled: "muted" }[st] || "muted";
  return `<span class="pill ${cls}">${esc(st)}</span>`;
}
function jobRowHtml(j) {
  return `
    <div class="jr-top">
      <span class="jr-id">${esc(j.id)}</span>
      <span class="jr-sample">${esc(j.sample_id)}</span>
      <span class="pill muted">${esc(j.kind)}</span>
      ${statusPill(j.state)}
    </div>
    <div class="jr-meta">${esc(j.stage || "")} ${esc(j.error || "")}</div>
    <div class="progress"><i style="width:${j.progress || 0}%"></i></div>`;
}

function renderJobs() {
  const kind = state.kindFilter;
  const all = (state.boot.jobs || []).filter((j) => !kind || j.kind === kind);
  const active = all.filter((j) => ACTIVE_STATES.includes(j.state));
  const past = all.filter((j) => !ACTIVE_STATES.includes(j.state));
  const host = $("job-list");
  host.innerHTML = active.length ? active.map((j) =>
    `<div class="job-row" data-job="${esc(j.id)}">${jobRowHtml(j)}</div>`).join("")
    : `<div class="empty">Nothing running or queued. Prepare an analysis above, or ask the agent in chat.</div>`;
  $("history-count").textContent = past.length ? `(${past.length})` : "";
  $("history-list").innerHTML = past.length ? past.map((j) =>
    `<div class="job-row" data-job="${esc(j.id)}">${jobRowHtml(j)}</div>`).join("")
    : `<div class="empty">No finished jobs yet.</div>`;
  document.querySelectorAll("[data-job]").forEach((el) =>
    el.addEventListener("click", () => openJob(el.dataset.job)));
}

document.querySelectorAll("#kind-filter .chip").forEach((c) =>
  c.addEventListener("click", () => {
    document.querySelectorAll("#kind-filter .chip").forEach((x) =>
      x.classList.remove("active"));
    c.classList.add("active");
    state.kindFilter = c.dataset.kind;
    renderJobs();
  }));

async function openJob(id) {
  let detail;
  try { detail = await api("GET", `/api/jobs/${id}/events`); }
  catch (e) { toast("cannot open job: " + e.message, true); return; }
  const job = (state.boot.jobs || []).find((j) => j.id === id);
  let decisions = [];
  try { decisions = (await api("GET", `/api/decisions?job_id=${id}`)).decisions; }
  catch { /* optional */ }
  const jobEl = $("job-detail");
  const running = job && job.state === "running";
  jobEl.classList.remove("hidden");
  jobEl.innerHTML = `
    <button class="btn sm ghost close" id="jd-close">Close</button>
    <h2>${esc(job ? job.sample_id : id)}</h2>
    <div class="btnrow">${job ? statusPill(job.state) : ""}
      <span class="pill muted">${esc(job ? job.kind : "?")}</span></div>
    <section><h3>Controls</h3><div class="btnrow">
      <button class="btn sm" id="jd-pause" ${running ? "" : "disabled"}>Pause</button>
      <button class="btn sm" id="jd-resume" ${job && job.state === "paused" ? "" : "disabled"}>Resume</button>
      <button class="btn sm danger" id="jd-cancel"
        ${job && !["completed", "failed", "cancelled"].includes(job.state) ? "" : "disabled"}>Cancel</button>
    </div></section>
    <section><h3>Requested configuration</h3>
      <div class="kv">${Object.entries((job && job.spec &&
        job.spec.requested_config) || {})
        .map(([k, v]) => `<div class="k">${esc(k)}</div><div>${esc(v)}</div>`).join("") ||
        '<div class="hint">defaults only</div>'}</div></section>
    <section><h3>Decision history</h3><div class="timeline">${decisions.map((d) => `
      <div class="tl-row"><span class="tl-ts">${esc(d.ts || "")}</span>
        <span class="tl-kind">${esc(d.decision_type || "")}</span>
        <span>${esc(d.question || "")} -> <b>${esc(d.chosen || "pending")}</b></span>
      </div>`).join("") || '<div class="hint">no decisions recorded</div>'}</div></section>
    <section><h3>Live events</h3><div class="logview">${detail.slice(0, 120)
      .map((e) => `${esc(e.ts || "")} ${esc(e.kind || "")} ${esc(e.text || "")}`)
      .join("\n")}</div></section>`;
  $("jd-close").addEventListener("click", () => jobEl.classList.add("hidden"));
  const pc = $("jd-pause"), rc = $("jd-resume"), cc = $("jd-cancel");
  pc.addEventListener("click", () => api("POST", `/api/jobs/${id}/pause`, {}).then(refreshJobs));
  rc.addEventListener("click", () => api("POST", `/api/jobs/${id}/resume`, {}).then(refreshJobs));
  cc.addEventListener("click", () => api("POST", `/api/jobs/${id}/cancel`, {}).then(refreshJobs));
}

/* ---------------- results ---------------- */
function renderResults() {
  const done = (state.boot.jobs || []).filter((j) => j.state === "completed");
  $("res-job").innerHTML = done.map((j) =>
    `<option value="${esc(j.id)}">${esc(j.id)} - ${esc(j.sample_id)}</option>`).join("");
  if (!done.length) {
    $("res-meta").innerHTML = `<div class="empty">No completed runs yet.</div>`;
    $("res-table").innerHTML = "";
    return;
  }
  if (!state.resultsJob || !done.find((j) => j.id === state.resultsJob))
    state.resultsJob = done[0].id;
  $("res-job").value = state.resultsJob;
  api("GET", `/api/results/${state.resultsJob}`).then((res) => {
    const rel = res.production_release === false
      ? `<div class="mnote">production release BLOCKED: ${esc(res.release_note || "")}</div>`
      : "";
    $("res-meta").innerHTML = rel + `
      <div class="hint">${esc(res.disclaimer || "")}</div>
      <div class="kv">${Object.entries(res.provenance || {})
        .map(([k, v]) => `<div class="k">${esc(k)}</div><div>${esc(v)}</div>`).join("")}</div>`;
    const rows = (res.candidates || []).map((r) => `
      <tr><td>${esc(r.rank)}</td><td><code>${esc(r.peptide)}</code></td>
      <td>${esc(r.gene)}</td><td>${esc(r.hla)}</td><td>${esc(r.branch)}</td>
      <td>${esc(r.mock_combined_score)}</td>
      <td>${esc(r.safety_normal_hits)}</td></tr>`).join("");
    $("res-table").innerHTML = `<div class="tablewrap"><table class="data">
      <thead><tr><th>Rank</th><th>Peptide</th><th>Gene</th><th>HLA</th>
      <th>Branch</th><th>Score</th><th>Normal hits</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }).catch(() => { $("res-table").innerHTML = ""; });
}
$("res-job").addEventListener("change", () => {
  state.resultsJob = $("res-job").value;
  renderResults();
});

/* ---------------- discovery ---------------- */
function renderDiscovery() {
  const list = (state.boot.research || []);
  $("res-list").innerHTML = list.length ? list.map((r) => `
    <div class="job-row" data-res="${esc(r.id)}">
      <div class="jr-top"><span class="jr-id">${esc(r.id)}</span>
        <span class="pill muted">${esc(r.scope)}</span>
        <span class="pill accent">${esc(r.target)}</span>
        <span class="pill ${r.status === "completed" ? "ok" : "warn"}">${esc(r.status)}</span>
      </div></div>`).join("")
    : `<div class="empty">No digests yet.</div>`;
  document.querySelectorAll("[data-res]").forEach((el) =>
    el.addEventListener("click", () => showReport(el.dataset.res)));
  if (list.length) showReport(list[0].id);
}
async function showReport(id) {
  try {
    const r = await api("GET", `/api/research/${id}`);
    if (!r.report) { $("res-report").innerHTML = ""; return; }
    const rep = r.report;
    const rows = (rep.comparison || []).map((c) => `
      <tr><td><b>${esc(c.tool)}</b> ${esc(c.version || "")}</td>
      <td>${esc(c.availability_class)}</td><td>${esc(c.license)}</td>
      <td>${esc(c.evidence_quality)}</td>
      <td>${esc(c.accuracy_example || "")}</td>
      <td>${c.independently_benchmarked ? "YES" : "no"}</td>
      <td>${esc(c.notes || "")}</td></tr>`).join("");
    $("res-report").innerHTML = `<div class="card">
      <div class="mnote">${esc(rep.disclaimer)}</div>
      <div class="tablewrap"><table class="data">
        <thead><tr><th>Tool</th><th>Availability</th><th>License</th>
        <th>Evidence</th><th>Example figures</th><th>Independently benchmarked</th>
        <th>Notes</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="hint">${esc(rep.recommendation.verdict)}</p>
      <p class="hint">Pre-adoption experiments: ${esc(rep.recommendation.experiments_before_adoption.join(" | "))}</p>
      </div>`;
  } catch (e) { toast("report failed: " + e.message, true); }
}
$("res-run").addEventListener("click", async () => {
  const scope = $("res-scope").value, target = $("res-target").value;
  try {
    const r = await api("POST", "/api/research", { scope, target });
    toast(`digest ${r.research_id} started`);
    await refreshDiscovery();
  } catch (e) { toast("research failed: " + e.message, true); }
});
async function refreshDiscovery() {
  try {
    const b = await api("GET", "/api/bootstrap");
    state.boot.research = b.research;
    renderDiscovery();
  } catch (e) { /* transient */ }
}

/* ---------------- help ---------------- */
function renderHelp() {
  const docs = (state.boot.help || []);
  $("help-tabs").innerHTML = docs.map((d, i) =>
    `<button class="chip ${i === 0 ? "active" : ""}" data-doc="${esc(d.name)}">${esc(d.title)}</button>`).join("");
  document.querySelectorAll("[data-doc]").forEach((el) =>
    el.addEventListener("click", () => {
      document.querySelectorAll("[data-doc]").forEach((x) =>
        x.classList.remove("active"));
      el.classList.add("active");
      loadHelp(el.dataset.doc);
    }));
  if (docs.length) loadHelp(docs[0].name);
}
async function loadHelp(name) {
  try {
    const d = await api("GET", `/api/help/${name}`);
    $("help-doc").innerHTML = `<div class="doc-src">${esc(d.source)}</div>`
      + mdToHtml(d.markdown);
  } catch (e) { $("help-doc").innerHTML = `<p class="hint">unavailable</p>`; }
}

/* ---------------- workspace tabs ---------------- */
document.querySelectorAll(".wtab").forEach((t) =>
  t.addEventListener("click", () => {
    document.querySelectorAll(".wtab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    document.querySelectorAll(".wview").forEach((x) => x.classList.remove("active"));
    $(`view-${t.dataset.view}`).classList.add("active");
  }));

/* ---------------- agent mode badge ---------------- */
function renderAgentBadge() {
  const real = state.agentMode === "real";
  $("agent-mode").textContent = real
    ? "CodeBuddy (real)" : "Demo agent (scripted)";
  $("agent-mode").title = real
    ? "Connected to the real server-side CodeBuddy agent: ordinary work executes directly; destructive or out-of-workspace operations are blocked until you approve a confirmation card. Click to switch back to the scripted demo agent."
    : "Scripted demo agent - no LLM. Click to connect the real server-side CodeBuddy agent.";
}
$("agent-mode").addEventListener("click", async () => {
  const real = state.agentMode === "real";
  const next = real ? "mock" : "real";
  const ok = confirm(next === "real"
    ? "Switch to the REAL CodeBuddy agent?\n\nYour chat messages go to the server-side CodeBuddy gateway. Ordinary reads, diagnostics and workspace work execute directly (logged, not prompted); destructive or out-of-workspace operations are blocked by the execution gate until you approve a confirmation card."
    : "Switch back to the scripted demo agent?");
  if (!ok) return;
  try {
    await api("POST", "/api/agent/mode", { mode: next });
    state.agentMode = next;
    renderAgentBadge();
    toast(next === "real"
      ? "real CodeBuddy agent connected - gated execution, card-based confirmations"
      : "switched to the scripted demo agent");
  } catch (e) { toast("mode switch failed: " + e.message, true); }
});

/* ---------------- approvals + decisions ---------------- */
document.addEventListener("click", async (ev) => {
  const appr = ev.target.closest("[data-appr]");
  if (appr) {
    try {
      await api("POST", `/api/approvals/${appr.dataset.appr}`,
        { approve: appr.dataset.ok === "1" });
      await refreshJobs(); refreshConv();
    } catch (e) { toast("approval failed: " + e.message, true); }
    return;
  }
  const dec = ev.target.closest("[data-dec]");
  if (dec) {
    try {
      await api("POST", `/api/decisions/${dec.dataset.dec}/resolve`,
        { choice: dec.dataset.choice, note: "resolved in Tasks panel" });
      await refreshJobs();
    } catch (e) { toast("resolve failed: " + e.message, true); }
  }
});

boot();
