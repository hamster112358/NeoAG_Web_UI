/* NeoAG Workspace - frontend logic (vanilla JS, no build step).
   Data comes from /api/bootstrap + SSE. All writes go through the API;
   the browser only renders server-persisted state. */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

const state = {
  boot: null, params: null, view: "tasks", kindFilter: "",
  jobDetail: null, helpDoc: null, resultsJob: null,
  manifest: null, agentMode: "mock",
};

const LEGACY_NOTICE_RE = /^MOCK PROTOTYPE/;

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
    state.params = await api("GET", "/api/params");
    renderAgentBadge();
    renderChat();
    renderTasks();
    renderJobs();
    renderResults();
    renderDiscovery();
    renderHelp();
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
  es.onopen = () => { $("conn-dot").className = "dot on"; $("conn-text").textContent = "live"; };
  es.onerror = () => { $("conn-dot").className = "dot off"; $("conn-text").textContent = "reconnecting"; };
  es.onmessage = (m) => {
    let ev = {};
    try { ev = JSON.parse(m.data); } catch { return; }
    if (ev.type === "chat") refreshChat();
    else if (ev.type === "job" || ev.type === "approval") { refreshJobs(); refreshChat(); }
    else if (ev.type === "research") refreshDiscovery();
    else if (ev.type === "manifest") refreshManifests();
    else if (ev.type === "agent_mode") renderAgentBadge();
    else if (ev.type === "settings") refreshTasks();
  };
}

/* ---------------- chat ---------------- */
function msgHtml(m) {
  const meta = m.meta || {};
  const badges = (meta.badges || []).map((b) =>
    `<span class="badge">${esc(b)}</span>`).join("");
  return `<div class="msg ${esc(m.role)}">
    <div class="meta"><span>${esc(m.role)}</span>${badges}</div>
    <div class="body">${esc(m.text)}</div></div>`;
}

function renderChat() {
  const log = $("chat-log");
  const msgs = (state.boot.messages || []).filter((m) =>
    !(m.meta && Array.isArray(m.meta.badges) &&
      m.meta.badges.includes("mock prototype")) &&
    !LEGACY_NOTICE_RE.test(m.text || ""));
  const start = state.boot.messages.length < 3;
  let html = msgs.map(msgHtml).join("");
  if (start) {
    html += `<div class="quickstart">
      <div class="qs-title">Quick starts</div>
      <button data-say="analyze DEMO-P001">Run a demo analysis of sample DEMO-P001</button>
      <button data-say="what rules does NeoAG enforce">Explain the scientific policy rules</button>
      <button data-say="show top candidates">Show top-ranked candidates</button>
    </div>`;
  }
  const pend = (state.boot.approvals || []).filter((a) => a.state === "pending");
  for (const a of pend) {
    html += `<div class="inline-card">
      <div class="q">Approval required</div>
      <div class="body" style="font-size:12.5px;color:var(--text-2)">${esc(a.detail || a.action)}</div>
      <div class="btnrow">
        <button class="btn primary sm" data-appr="${esc(a.id)}" data-ok="1">Approve</button>
        <button class="btn danger sm" data-appr="${esc(a.id)}">Deny</button>
      </div></div>`;
  }
  const pdec = (state.boot.decisions || []).filter((d) => d.status === "pending");
  for (const d of pdec.slice(0, 4)) {
    html += `<div class="inline-card">
      <div class="q">${esc(d.question)}</div>
      <div class="opts">${(d.options || []).map((o) =>
        `<button class="opt" data-dec="${esc(d.id)}" data-choice="${esc(o.id)}">${esc(o.label)}${o.validated ? '<span class="validated">validated default</span>' : ""}</button>`).join("")}</div>
      <div class="why">${esc(d.evidence ? d.evidence.join(" - ") : "")}</div></div>`;
  }
  log.innerHTML = html;
  log.scrollTop = log.scrollHeight;
}

$("chat-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const text = $("chat-input").value.trim();
  if (!text) return;
  $("chat-input").value = "";
  try {
    await api("POST", "/api/chat", { text });
    await refreshChat();
  } catch (e) { toast("send failed: " + e.message, true); }
});
async function refreshChat() {
  try {
    state.boot.messages = (await api("GET", "/api/bootstrap")).messages;
    renderChat();
  } catch (e) { /* transient */ }
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
    toast(`job ${res.job_id} blocked on approval - approve it in the chat pane`);
    await refreshJobs(); refreshChat();
  } catch (e) { toast("submit failed: " + e.message, true); }
});

/* ---------------- jobs ---------------- */
function statusPill(st) {
  const cls = { completed: "ok", running: "accent", queued: "accent",
                blocked: "warn", paused: "warn", failed: "danger",
                cancelled: "muted" }[st] || "muted";
  return `<span class="pill ${cls}">${esc(st)}</span>`;
}

function renderJobs() {
  const host = $("job-list");
  const kind = state.kindFilter;
  const jobs = (state.boot.jobs || []).filter((j) => !kind || j.kind === kind);
  if (!jobs.length) {
    host.innerHTML = `<div class="empty">No jobs in this category yet.</div>`;
    return;
  }
  host.innerHTML = jobs.map((j) => `
    <div class="job-row" data-job="${esc(j.id)}">
      <div class="jr-top">
        <span class="jr-id">${esc(j.id)}</span>
        <span class="jr-sample">${esc(j.sample_id)}</span>
        <span class="pill muted">${esc(j.kind)}</span>
        ${statusPill(j.state)}
      </div>
      <div class="jr-meta">${esc(j.stage || "")} ${esc(j.error || "")}</div>
      <div class="progress"><i style="width:${j.progress || 0}%"></i></div>
    </div>`).join("");
  host.querySelectorAll("[data-job]").forEach((el) =>
    el.addEventListener("click", () => openJob(el.dataset.job)));
}

async function refreshJobs() {
  try {
    const b = await api("GET", "/api/bootstrap");
    state.boot.jobs = b.jobs; state.boot.approvals = b.approvals;
    state.boot.decisions = b.decisions;
    renderJobs(); renderChat();
  } catch (e) { /* transient */ }
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
  }).catch((e) => { $("res-table").innerHTML = ""; });
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
    ? "Connected to the real server-side CodeBuddy agent. Click to switch back to the scripted demo agent."
    : "Scripted demo agent - no LLM. Click to switch to the real server-side CodeBuddy agent.";
}
$("agent-mode").addEventListener("click", async () => {
  const real = state.agentMode === "real";
  const next = real ? "mock" : "real";
  const ok = confirm(next === "real"
    ? "Switch to the REAL CodeBuddy agent?\n\nMessages will be sent to the server-side CodeBuddy gateway (one active run; approvals must be given in the operator CodeBuddy Web UI; the gateway rate-limits run creation)."
    : "Switch back to the scripted demo agent?");
  if (!ok) return;
  try {
    await api("POST", "/api/agent/mode", { mode: next });
    state.agentMode = next;
    renderAgentBadge();
    toast(next === "real"
      ? "real CodeBuddy agent connected - be aware this drives the operator's live gateway"
      : "switched to the scripted demo agent");
  } catch (e) { toast("mode switch failed: " + e.message, true); }
});

/* ---------------- approvals + decisions (chat cards) ---------------- */
document.addEventListener("click", async (ev) => {
  const appr = ev.target.closest("[data-appr]");
  if (appr) {
    try {
      await api("POST", `/api/approvals/${appr.dataset.appr}`,
        { approve: appr.dataset.ok === "1" });
      await refreshJobs(); refreshChat();
    } catch (e) { toast("approval failed: " + e.message, true); }
    return;
  }
  const dec = ev.target.closest("[data-dec]");
  if (dec) {
    try {
      await api("POST", `/api/decisions/${dec.dataset.dec}/resolve`,
        { choice: dec.dataset.choice, note: "resolved in chat" });
      await refreshJobs(); refreshChat();
    } catch (e) { toast("resolve failed: " + e.message, true); }
  }
});

document.querySelectorAll("[data-say]").forEach((b) =>
  b.addEventListener("click", () => {
    $("chat-input").value = b.dataset.say;
    $("chat-form").requestSubmit();
  }));

boot();
