/* NeoAG web demo frontend - vanilla JS, no build step. */
"use strict";
const S = { boot: null, openLogs: new Set(), lastAudit: [], resCache: {} };

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "html") e.innerHTML = v;
    else e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null) continue;
    e.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return e;
};
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.status + " " + r.statusText);
  return j;
};
const post = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body || {}) });
const t = (iso) => (iso || "").replace("T", " ").replace(/\+.*$/, "");

function toast(msg, err) {
  const el = h("div", { class: "disclaimer", style:
    `position:fixed;right:16px;bottom:16px;z-index:9;${err ? "border-color:#e88;background:#fde3e0;color:#b42318" : ""}` }, msg);
  document.body.append(el);
  setTimeout(() => el.remove(), 4200);
}

/* ---------- boot / SSE ---------- */
async function boot() {
  S.boot = await api("/api/bootstrap");
  $("#conn-dot").classList.add("live");
  $("#conn-text").textContent = "local demo server";
  renderChat(); renderForm(); renderJobs(); renderApprovals();
  renderDecisions(); renderPolicies(); renderData(); refreshResults();
  refreshAudit(); renderResearch();
}
function connectSSE() {
  const es = new EventSource("/api/stream");
  let pending = null;
  const refresh = () => {
    clearTimeout(pending);
    pending = setTimeout(async () => {
      const b = await api("/api/bootstrap").catch(() => null);
      if (!b) return;
      S.boot = b;
      renderChat(); renderJobs(); renderApprovals(); renderDecisions();
      refreshResults(); refreshAudit(); renderResearch();
    }, 250);
  };
  es.onopen = () => { $("#conn-dot").classList.add("live"); $("#conn-text").textContent = "local demo server"; };
  es.onerror = () => { $("#conn-dot").classList.remove("live"); $("#conn-text").textContent = "reconnecting…"; };
  es.onmessage = (m) => { const ev = JSON.parse(m.data); if (ev.type !== "hello") refresh(); };
}

/* ---------- chat ---------- */
function badges(meta, role) {
  const list = (meta && meta.badges) || (role === "user" ? [] : ["agent suggestion"]);
  const cls = { "agent suggestion": "agent", "validated policy": "policy",
                "mock prototype": "sys" };
  const out = list.map(b => h("span", { class: "badge " + (cls[b] || "sys") }, b));
  if (meta && meta.policy_id) out.push(h("span", { class: "badge src" },
    `${meta.policy_id} - ${meta.source}`));
  return out;
}
function renderChat() {
  const box = $("#chat-box");
  box.innerHTML = "";
  for (const m of S.boot.messages) {
    const role = m.role === "user" ? "user" : m.role;
    box.append(h("div", { class: "msg " + role },
      m.role === "user" ? null : h("div", { class: "meta" }, badges(m.meta, m.role)),
      m.text));
  }
  box.scrollTop = box.scrollHeight;
}
$("#chat-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const inp = $("#chat-input"), text = inp.value.trim();
  if (!text) return;
  inp.value = "";
  try {
    const r = await post("/api/chat", { text });
    S.boot.messages.push(...r.messages);
    renderChat(); renderJobs(); renderApprovals();
  } catch (e) { toast("chat failed: " + e.message, true); }
});
$("#chips").addEventListener("click", (ev) => {
  const b = ev.target.closest(".chip");
  if (!b) return;
  $("#chat-input").value = b.dataset.say;
  $("#chat-form").dispatchEvent(new Event("submit"));
});

/* ---------- tasks ---------- */
function renderForm() {
  const sel = $("#nj-sample");
  sel.innerHTML = "";
  for (const s of S.boot.samples)
    sel.append(h("option", { value: s.id }, `${s.id} - ${s.assay}`));
  const box = $("#nj-stages");
  if (!box.childElementCount) {
    for (const st of S.boot.stages) {
      box.append(h("label", {},
        h("input", { type: "checkbox", value: st.id }),
        st.name,
        st.required ? null : h("span", { class: "opt" }, " (optional)")));
    }
    box.addEventListener("change", syncStageDefaults);
  }
  $("#nj-mode").value = S.boot.settings.mode || "auto";
  syncStageDefaults();
}
function syncStageDefaults() {
  const adv = $("#nj-mode").value === "advanced";
  $("#nj-stages").classList.toggle("hidden", !adv);
  if (!adv) for (const cb of $$("#nj-stages input"))
    cb.checked = S.boot.stages.find(s => s.id === cb.value).required;
}
$("#nj-mode").addEventListener("change", () => {
  syncStageDefaults();
  post("/api/settings", { mode: $("#nj-mode").value });
});
$("#nj-submit").addEventListener("click", async () => {
  const stage_ids = $$("#nj-stages input").filter(c => c.checked).map(c => c.value);
  try {
    await post("/api/jobs", {
      sample_id: $("#nj-sample").value, stage_ids,
      mode: $("#nj-mode").value,
      execution_mode: $("#nj-exec").value,
      simulate_failure: $("#nj-fail").checked,
    });
    toast("Mock analysis requested - it is now waiting in the Approvals panel.");
  } catch (e) { toast("submit failed: " + e.message, true); }
});

const CTR = (j) => {
  const btns = [];
  if (j.state === "running") {
    btns.push(h("button", { class: "small", onclick: () => act(`/api/jobs/${j.id}/pause`) }, "Pause"));
    btns.push(h("button", { class: "small danger", onclick: () => act(`/api/jobs/${j.id}/cancel`) }, "Stop"));
  } else if (j.state === "paused") {
    btns.push(h("button", { class: "small", onclick: () => act(`/api/jobs/${j.id}/resume`) }, "Resume"));
    btns.push(h("button", { class: "small danger", onclick: () => act(`/api/jobs/${j.id}/cancel`) }, "Stop"));
  } else if (["queued", "blocked"].includes(j.state)) {
    btns.push(h("button", { class: "small danger", onclick: () => act(`/api/jobs/${j.id}/cancel`) }, "Cancel request"));
  }
  btns.push(h("button", { class: "small", onclick: () => toggleLog(j.id) }, "Log"));
  return btns;
};
async function act(path) {
  try { await post(path, {}); } catch (e) { toast(e.message, true); }
}
async function toggleLog(id) {
  if (S.openLogs.has(id)) S.openLogs.delete(id); else S.openLogs.add(id);
  renderJobs();
}
function modeCtl(j) {
  const cur = j.spec.execution_mode || "controlled";
  if (["completed", "failed", "cancelled"].includes(j.state))
    return h("span", { class: "sub" }, `exec: ${cur}`);
  return h("select", { value: cur, title:
      "Switch execution mode - applies at the next stage boundary and is " +
      "recorded in the decision history",
    onchange: async (ev) => {
      try { await post(`/api/jobs/${j.id}/mode`, { mode: ev.target.value }); }
      catch (e) { toast(e.message, true); ev.target.value = cur; }
    } },
    h("option", { value: "controlled" }, "exec: controlled"),
    h("option", { value: "autonomous" }, "exec: autonomous"));
}
async function renderJobs() {
  const list = $("#job-list");
  list.innerHTML = "";
  for (const j of S.boot.jobs) {
    const card = h("div", { class: "card job" },
      h("div", { class: "head" },
        h("h3", {}, j.id),
        h("span", { class: "pill " + j.state }, j.state),
        h("span", { class: "sub" }, `${j.sample_id} - ${j.spec.mode || "?"} settings - ` +
          `${(j.spec.stage_ids || []).length} stages`)),
      h("div", { class: "bar" }, h("i", { style: `width:${j.progress || 0}%` })),
      h("div", { class: "sub" },
        j.stage ? `stage: ${j.stage} - ${j.progress}%` :
        j.error ? j.error : `created ${t(j.created_at)}${j.finished_at ? " - finished " + t(j.finished_at) : ""}`),
      h("div", { class: "ctl" }, CTR(j), modeCtl(j)));
    if (S.openLogs.has(j.id)) {
      const log = h("div", { class: "log", id: "log-" + j.id }, "loading…");
      card.append(log);
      const evs = await api(`/api/jobs/${j.id}/events`).catch(() => []);
      log.textContent = evs.map(e => `[${t(e.ts).slice(11)}] ${e.text}`).join("\n") || "(no events)";
      log.scrollTop = log.scrollHeight;
    }
    list.append(card);
  }
}

/* ---------- results ---------- */
async function refreshResults() {
  const sel = $("#res-job");
  const done = S.boot.jobs.filter(j => j.state === "completed");
  const cur = sel.value;
  sel.innerHTML = "";
  if (!done.length) {
    sel.append(h("option", { value: "" }, "(no completed mock runs yet)"));
    $("#res-meta").innerHTML = ""; $("#res-table").innerHTML = "";
    return;
  }
  for (const j of done)
    sel.append(h("option", { value: j.id }, `${j.id} - ${j.sample_id}`));
  sel.value = done.some(j => j.id === cur) ? cur : done[0].id;
  await showResults(sel.value);
}
$("#res-job") && $("#res-job").addEventListener("change", e => showResults(e.target.value));
async function showResults(id) {
  if (!id) return;
  let r = S.resCache[id];
  if (!r) { r = await api("/api/results/" + id).catch(() => null); if (r) S.resCache[id] = r; }
  if (!r) return;
  $("#res-meta").innerHTML = "";
  $("#res-meta").append(
    h("div", { class: "disclaimer" }, r.disclaimer),
    r.production_release === false ? h("div", { class: "disclaimer",
        style: "border-color:#e88;background:#fde3e0;color:#b42318" },
      "production_release: false - " + r.release_note) : null,
    r.production_release === true ? h("div", { class: "sub" },
      "production_release: true - " + r.release_note) : null,
    h("div", { class: "prov" },
      `provenance: job=${r.provenance.job_id}  sample=${r.provenance.sample}  ` +
      `engine=${r.provenance.engine}  exec=${r.provenance.execution_mode}  ` +
      `dataset=${r.dataset_version}  seed=${r.provenance.generator_seed}`));
  const cols = [["rank", "#"], ["peptide", "Peptide"], ["gene", "Gene (synthetic)"],
    ["branch", "Branch"], ["hla", "HLA"], ["pred_el_rank_pct", "pred EL %rank"],
    ["pred_stability_rank", "pred stability"], ["pred_cleavage_score", "pred cleavage"],
    ["pred_immunogenicity", "pred immuno"], ["safety_normal_hits", "safety hits"],
    ["rna_expr_tpm_mock", "expr TPM (mock)"], ["pred_ccf_pct", "pred CCF %"],
    ["mock_combined_score", "mock score"]];
  const table = h("table", {},
    h("thead", {}, h("tr", {}, cols.map(([, l]) => h("th", {}, l)), h("th", {}, "Evidence summary (mock)"))),
    h("tbody", {}, r.candidates.map(c => h("tr", {},
      cols.map(([k, l]) => h("td", { class: typeof c[k] === "number" ? "num" : "" }, String(c[k]))),
      h("td", { class: "sub" }, c.evidence_summary)))));
  $("#res-table").innerHTML = "";
  $("#res-table").append(table);
}

/* ---------- approvals ---------- */
function renderApprovals() {
  const list = $("#appr-list");
  list.innerHTML = "";
  const pend = S.boot.approvals.filter(a => a.state === "pending");
  $("#appr-count").textContent = pend.length || "";
  $("#appr-count").style.display = pend.length ? "" : "none";
  if (!S.boot.approvals.length) {
    list.append(h("div", { class: "panel" }, "No approvals yet. Request an analysis in the Tasks panel or ask the assistant to \"analyze DEMO-P001\"."));
    return;
  }
  for (const a of S.boot.approvals) {
    const card = h("div", { class: "card appr" + (a.state === "pending" ? " pending" : "") },
      h("div", { class: "head" },
        h("h3", {}, a.id),
        h("span", { class: "pill " + (a.state === "pending" ? "blocked" : a.state === "approved" ? "completed" : "failed") }, a.state)),
      h("p", {}, a.detail),
      h("p", { class: "sub" }, `action: ${a.action} - risk: ${a.risk} - job: ${a.job_id} - created ${t(a.created_at)}`));
    if (a.state === "pending") card.append(h("div", { class: "actions" },
      h("button", { class: "primary small", onclick: () => decide(a.id, true) }, "Approve"),
      h("button", { class: "danger small", onclick: () => decide(a.id, false) }, "Deny")));
    else card.append(h("p", { class: "sub" }, `decided ${t(a.decided_at)} by ${a.decided_by}`));
    list.append(card);
  }
}
async function decide(id, approve) {
  try { await post("/api/approvals/" + id, { approve }); }
  catch (e) { toast(e.message, true); }
}

/* ---------- decisions (persistent decision history) ---------- */
function decisionCard(d) {
  return h("div", { class: "card appr pending" },
    h("div", { class: "head" },
      h("h3", {}, d.id),
      h("span", { class: "pill blocked" }, d.status)),
    h("p", {}, d.question),
    h("p", { class: "sub" },
      `job: ${d.job_id} - stage: ${d.stage} - type: ${d.decision_type}` +
      (d.policy_id ? ` - policy: ${d.policy_id} (${d.policy_version})` : "")),
    (d.evidence || []).length ? h("p", { class: "sub" },
      "evidence: " + d.evidence.join("; ")) : null,
    h("div", { class: "actions" },
      (d.options || []).map(o => h("button",
        { class: "small " + (o.validated ? "primary" : ""),
          title: o.validated ? "validated default" :
                 "alternative (recorded as an override)",
          onclick: () => resolveDec(d.id, o.id) },
        (o.validated ? "[default] " : "[alt] ") + o.id))));
}
async function resolveDec(id, choice) {
  try { await post(`/api/decisions/${id}/resolve`, { choice }); }
  catch (e) { toast(e.message, true); }
}
function renderDecisions() {
  const ds = S.boot.decisions || [];
  const pend = ds.filter(d => d.status === "pending");
  const pendBox = $("#dec-pending");
  pendBox.innerHTML = "";
  if (!pend.length) pendBox.append(h("div", { class: "hint" },
    "No open decisions. In Controlled mode the run pauses at each decision " +
    "point (input mapping, gate parameters, stage failures) and the options " +
    "appear here; in Autonomous mode the engine resolves them to validated " +
    "defaults and each choice is recorded in the history below."));
  for (const d of pend) pendBox.append(decisionCard(d));
  const hist = $("#dec-history");
  hist.innerHTML = "";
  if (!ds.length) {
    hist.append(h("div", { class: "hint" }, "Decision history is empty."));
    return;
  }
  hist.append(h("table", {},
    h("thead", {}, h("tr", {},
      ["time", "job", "stage", "type", "options considered", "chosen",
       "rationale", "evidence", "policy", "decided by", "status"]
      .map(x => h("th", {}, x)))),
    h("tbody", {}, ds.map(d => h("tr", {},
      h("td", {}, t(d.decided_at || d.ts)),
      h("td", { class: "sub" }, d.job_id),
      h("td", {}, d.stage),
      h("td", {}, d.decision_type),
      h("td", { class: "sub" }, (d.options || []).map(o =>
        (o.validated ? "[default] " : "[alt] ") + o.id + ": " + o.label)
        .join(" | ")),
      h("td", {}, d.chosen || "-"),
      h("td", { class: "sub" }, d.rationale || "-"),
      h("td", { class: "sub" }, (d.evidence || []).join("; ")),
      h("td", { class: "sub" }, (d.policy_id || "-") +
        (d.policy_version ? ` (${d.policy_version})` : "")),
      h("td", {}, d.decided_by || "-"),
      h("td", {}, h("span", { class: "pill " + d.status }, d.status)))))));
}

/* ---------- discovery (mock research digests) ---------- */
function renderResearch() {
  const list = $("#res-list");
  list.innerHTML = "";
  const rs = S.boot.research || [];
  if (!rs.length) {
    list.append(h("div", { class: "hint" },
      "No mock research digests yet. Pick a scope and target above, then run - " +
      "the digest is static example content, no network access."));
    return;
  }
  for (const r of rs)
    list.append(h("div", { class: "card" },
      h("div", { class: "head" },
        h("h3", {}, r.id),
        h("span", { class: "pill " + r.status }, r.status)),
      h("p", { class: "sub" },
        `scope: ${r.scope} - target: ${r.target} - started ${t(r.created_at)}` +
        (r.finished_at ? ` - finished ${t(r.finished_at)}` : "")),
      h("button", { class: "small", onclick: () => showResearch(r.id) },
        "Open report")));
}
$("#res-run") && $("#res-run").addEventListener("click", async () => {
  try {
    const r = await post("/api/research", {
      scope: $("#res-scope").value, target: $("#res-target").value });
    toast("Mock research digest queued: " + r.research_id +
          " - static example content, no live research is connected.");
    renderResearch();
  } catch (e) { toast(e.message, true); }
});
async function showResearch(id) {
  const r = await api("/api/research/" + id).catch(e => {
    toast(e.message, true); return null; });
  if (!r) return;
  const rep = r.report;
  const box = $("#res-report");
  box.innerHTML = "";
  if (!rep) {
    box.append(h("p", { class: "sub" },
      "report not ready yet (status: " + r.status + ")"));
    return;
  }
  box.append(h("div", { class: "disclaimer" }, rep.disclaimer));
  box.append(h("p", { class: "sub" },
    `scope: ${rep.scope} - target: ${rep.target}`));
  box.append(h("p", { class: "sub" },
    "availability classes: " + rep.availability_classes_legend));
  const cols = ["tool", "version", "availability_class", "license",
    "source_citation", "publication_year", "evidence_quality",
    "accuracy_example", "perf_example", "io_compat", "maintenance",
    "notes"];
  box.append(h("table", {},
    h("thead", {}, h("tr", {}, cols.map(c => h("th", {}, c)),
      h("th", {}, "current in NeoAG"), h("th", {}, "basis"),
      h("th", {}, "independently benchmarked"))),
    h("tbody", {}, rep.comparison.map(row => h("tr", {},
      cols.map(c => h("td", { class: c === "publication_year" ? "num" : "sub" },
        String(row[c]))),
      h("td", {}, row.current_in_neoag ? "yes" : "no"),
      h("td", { class: "sub" }, row.basis),
      h("td", {}, row.independently_benchmarked ? "yes" :
        "NO - no independent benchmark exists yet"))))));
  box.append(h("div", { class: "card" },
    h("p", {}, h("strong", {}, "recommendation: "),
      rep.recommendation.verdict),
    h("p", { class: "sub" },
      "required experiments before any adoption:"),
    h("ol", {}, rep.recommendation.experiments_before_adoption
      .map(x => h("li", {}, x)))));
  box.append(h("p", { class: "sub" },
    `provenance: engine=${rep.provenance.engine}  ` +
    `dataset=${rep.provenance.dataset_version}  ` +
    `live_sources_connected=${rep.provenance.live_sources_connected}`));
}

/* ---------- policies / data / audit ---------- */
function renderPolicies() {
  const box = $("#pol-list");
  box.innerHTML = "";
  box.append(h("div", { class: "hint", style: "margin-bottom:10px" },
    "These rules are quoted from the NeoAG repository (read-only inspection). " +
    "They are displayed separately from agent suggestions and cannot be edited in the UI."));
  for (const p of S.boot.policies)
    box.append(h("div", { class: "card pcard" },
      h("span", { class: "pid" }, p.id), " ", p.text,
      h("div", { class: "src" }, "source: " + p.source)));
}
function renderData() {
  const box = $("#data-list");
  box.innerHTML = "";
  for (const s of S.boot.samples) {
    box.append(h("div", { class: "card" },
      h("div", { class: "head" }, h("h3", {}, s.id + " - " + s.label)),
      h("p", { class: "sub" }, `${s.assay} - ${s.genome_build} - ${s.status}`),
      h("ul", { class: "flist" }, s.files.map(f =>
        h("li", {}, f.path, h("span", { class: "kind" }, f.kind),
          f.size, h("span", { class: "mock" }, "MOCK")))),
      h("p", { class: "ni" }, "Preview / download of files: not implemented - these entries are names only, no data exists behind them.")));
  }
}
async function refreshAudit() {
  const rows = await api("/api/audit").catch(() => []);
  if (JSON.stringify(rows) === JSON.stringify(S.lastAudit)) return;
  S.lastAudit = rows;
  const box = $("#audit-list");
  box.innerHTML = "";
  box.append(h("div", { class: "hint", style: "margin-bottom:10px" },
    "Decision audit trail - every user/agent/engine action is recorded durably."));
  box.append(h("div", {}, h("table", {},
    h("thead", {}, h("tr", {}, ["time", "actor", "action", "object", "detail"].map(x => h("th", {}, x)))),
    h("tbody", {}, rows.map(a => h("tr", {},
      h("td", {}, t(a.ts)), h("td", {}, a.actor), h("td", {}, a.action),
      h("td", { class: "sub" }, a.object),
      h("td", { class: "sub" }, a.detail_json === "{}" ? "" : a.detail_json)))))));
}

/* ---------- nav ---------- */
$$(".tab").forEach(b => b.addEventListener("click", () => {
  $$(".tab").forEach(x => x.classList.toggle("active", x === b));
  $$(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + b.dataset.view));
  if (b.dataset.view === "audit") refreshAudit();
}));

boot().then(connectSSE).catch(e => toast("cannot reach demo server: " + e.message, true));
