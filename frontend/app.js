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
  sseDown: true, // until the live stream says otherwise (§6 status)
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
    renderDiscoveryControls();
    renderDiscovery();
    renderSkills();
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
    state.sseDown = false;
    $("conn-dot").className = "dot on"; $("conn-text").textContent = "live";
    refreshConv(); refreshSidebar(); refreshJobs(); renderAttention();
    renderResults();
  };
  es.onerror = () => {
    state.sseDown = true;
    $("conn-dot").className = "dot off";
    $("conn-text").textContent = "reconnecting";
    if (state.activeCid) renderChat(); // status line must say "Reconnecting"
  };
  es.onmessage = (m) => {
    let ev = {};
    try { ev = JSON.parse(m.data); } catch { return; }
    if (ev.type === "chat") {
      if (!ev.conv_id || ev.conv_id === state.activeCid) refreshConv();
      refreshSidebar();
    } else if (ev.type === "conversations") refreshSidebar();
    else if (ev.type === "job" || ev.type === "approval") {
      refreshJobs(); renderAttention(); refreshConv();
    } else if (ev.type === "research" || ev.type === "research_changed") {
      refreshDiscovery();
      refreshJobs(); // research rows are unified task rows too (§1.2)
    } else if (ev.type === "skills_changed") refreshSkills();
    else if (ev.type === "manifest") { /* the prep flow keeps its own state */ }
    else if (ev.type === "agent_mode") renderAgentBadge();
    else if (ev.type === "records_changed") boot();
  };
}

/* ---------------- conversation sidebar ---------------- */
/* M6 PART F4: one ellipsis menu per conversation (Rename / Pin /
   Archive / Delete) - the cluttered inline ren/del buttons and the
   header rename are gone. Archived chats move to a collapsed group so
   the action stays reversible. */
function renderSidebar() {
  const host = $("conv-list");
  const convs = state.convs || [];
  const live = convs.filter((c) => !c.archived);
  const arch = convs.filter((c) => c.archived);
  const row = (c) => `
    <div class="conv-item ${c.id === state.activeCid ? "active" : ""}"
         data-conv="${esc(c.id)}">
      <div class="ct">${c.pinned ? '<span class="cpin" title="pinned">&#9733;</span>' : ""}${esc(c.title || "New chat")}</div>
      <div class="cm">
        ${(c.job_state === "working" || c.job_state === "blocked")
          ? '<span class="live" title="agent session live"></span>' : ""}
        <span>${ago(c.updated_at)}</span><span>-</span>
        <span>${c.n_messages || 0} msg</span>
      </div>
      <div class="cops">
        <button class="icon-btn" data-menu="${esc(c.id)}"
          title="Rename / pin / archive / delete">&#8943;</button>
      </div>
    </div>`;
  host.innerHTML =
    (live.map(row).join("") || '<div class="empty">No chats yet.</div>')
    + (arch.length
       ? `<details class="conv-arch"><summary>Archived (${arch.length})</summary>`
         + arch.map(row).join("") + "</details>" : "");
  host.querySelectorAll("[data-conv]").forEach((el) =>
    el.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-menu]")) return;
      openConv(el.dataset.conv);
    }));
  host.querySelectorAll("[data-menu]").forEach((el) =>
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      showConvMenu(el.dataset.menu, el);
    }));
}

let convMenuCloser = null;
function closeConvMenu() {
  document.querySelectorAll(".conv-menu").forEach((x) => x.remove());
  if (convMenuCloser) {
    document.removeEventListener("click", convMenuCloser);
    convMenuCloser = null;
  }
}
function showConvMenu(cid, anchor) {
  closeConvMenu();
  const c = (state.convs || []).find((x) => x.id === cid) || {};
  const items = c.archived
    ? [["unarchive", "Unarchive"], ["delete", "Delete"]]
    : [["rename", "Rename"],
       [c.pinned ? "unpin" : "pin", c.pinned ? "Unpin" : "Pin to top"],
       ["archive", "Archive"], ["delete", "Delete"]];
  const m = document.createElement("div");
  m.className = "conv-menu";
  m.innerHTML = items.map(([k, label]) =>
    `<button data-act="${k}"${k === "delete" ? ' class="danger"' : ""}>` +
    `${label}</button>`).join("");
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect();
  m.style.top = (r.bottom + 4) + "px";
  m.style.left = Math.max(6, Math.min(r.left, window.innerWidth - 160)) + "px";
  m.addEventListener("click", async (ev) => {
    const act = (ev.target.closest("[data-act]") || {}).dataset
      && ev.target.closest("[data-act]").dataset.act;
    closeConvMenu();
    if (!act) return;
    try {
      if (act === "rename") { startRename(cid); return; }
      if (act === "pin" || act === "unpin") {
        await api("POST", `/api/conversations/${cid}/flag`,
                  { pinned: act === "pin" });
      } else if (act === "archive" || act === "unarchive") {
        await api("POST", `/api/conversations/${cid}/flag`,
                  { archived: act === "archive" });
      } else if (act === "delete") {
        if (!confirm(`Delete chat "${c.title || "New chat"}"?\n\n` +
          "It disappears from this list; the saved messages stay on " +
          "the server for audit.")) return;
        await api("POST", `/api/conversations/${cid}/delete`, {});
      }
      if (state.activeCid === cid && ["archive", "delete"].includes(act)) {
        state.activeCid = null;
        await refreshSidebar();
        await openNextConv();
      } else await refreshSidebar();
    } catch (e) { toast("chat action failed: " + e.message, true); }
  });
  convMenuCloser = (ev2) => {
    if (!ev2.target.closest(".conv-menu")
        && !ev2.target.closest("[data-menu]")) closeConvMenu();
  };
  setTimeout(() => document.addEventListener("click", convMenuCloser), 0);
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

/* M6 PART F4: renaming now lives ONLY in the conversation's ⋯ menu -
   the header rename button and the double-click shortcut are removed
   (the header just displays the title). */

/* ---------------- chat pane ---------------- */
function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  return isNaN(d) ? "" : d.toLocaleTimeString([],
    { hour: "2-digit", minute: "2-digit" });
}
const dayKey = (ts) => new Date(ts).toDateString();

/* Tool output arrives as two persisted messages (call + update) with the
   same tool_call_id; they merge into ONE collapsed entry, and the stored
   text is RAW (escaped here once) - no more leaked HTML entities. */
function toolHtml(m) {
  const meta = m.meta || {};
  const status = meta.tool_status || "running";
  const title = meta.tool_title || "tool";
  const out = meta.tool_output ?? m.text;
  return `<details class="msg tool toolmsg">
    <summary><span class="tool-status ${esc(status)}">${
      status === "completed" ? "done"
      : status === "failed" ? "failed" : "..."}</span>
      <b>${esc(title)}</b>
      <span class="ts">${esc(fmtTime(m.ts))}</span></summary>
    <pre class="tool-out">${esc(out)}</pre></details>`;
}

function msgHtml(m) {
  const meta = m.meta || {};
  if (meta.kind === "tool") return toolHtml(m);
  const badges = (meta.badges || []).map((b) =>
    `<span class="badge">${esc(b)}</span>`).join("");
  return `<div class="msg ${esc(m.role)}">
    <div class="meta"><span>${esc(m.role)}</span>${badges}<span class="ts">${
      esc(fmtTime(m.ts))}</span></div>
    <div class="body">${esc(m.text)}</div></div>`;
}

/* M7 §6: the tool chatter of ONE turn collapses into a single expandable
   activity summary once it is done - while tools are still running the
   summary stays open so the work is visible. Nothing is hidden from the
   audit trail: every call remains its own stored message, rendered
   inside the fold. */
function activityHtml(rows) {
  if (rows.length === 1) return msgHtml(rows[0]);
  const done = rows.filter((r) =>
    (r.meta || {}).tool_status === "completed").length;
  const failed = rows.filter((r) =>
    (r.meta || {}).tool_status === "failed").length;
  const live = rows.length - done - failed;
  const bits = [];
  if (done) bits.push(`${done} done`);
  if (failed) bits.push(`${failed} failed`);
  if (live) bits.push(`${live} running`);
  return `<details class="msg activity"${live ? " open" : ""}>
    <summary>Agent activity - ${rows.length} tool calls
      (${bits.join(", ")})
      <span class="ts">${esc(fmtTime(rows[rows.length - 1].ts))}</span>
    </summary>
    <div class="act-rows">${rows.map((r) => toolHtml(r)).join("")}
    </div></details>`;
}

/* final answers deserve more visual weight than tool chatter */
function mergeToolMsgs(msgs) {
  const merged = [], idxById = {};
  for (const m of msgs) {
    const meta = m.meta || {};
    if (meta.kind === "tool" && meta.tool_call_id) {
      if (idxById[meta.tool_call_id] !== undefined) {
        merged[idxById[meta.tool_call_id]] = m;  // update wins, stays in place
        continue;
      }
      idxById[meta.tool_call_id] = merged.length;
    }
    merged.push(m);
  }
  return merged;
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

  const merged = mergeToolMsgs(state.convMsgs || []);
  // §6: consecutive tool messages (one turn's work) become one block
  const blocks = [];
  for (const m of merged) {
    const last = blocks[blocks.length - 1];
    if ((m.meta || {}).kind === "tool" && last && last.tools) {
      last.tools.push(m);
    } else if ((m.meta || {}).kind === "tool") {
      blocks.push({ tools: [m] });
    } else {
      blocks.push({ msg: m });
    }
  }
  let lastDay = "";
  let html = blocks.map((b) => {
    const first = b.tools ? b.tools[0] : b.msg;
    let sep = "";
    let dk = "";
    try { dk = dayKey(first.ts); } catch { /* unparseable ts: no separator */ }
    if (dk && dk !== lastDay) {
      sep = `<div class="date-sep">${esc(
        new Date(first.ts).toLocaleDateString([],
          { year: "numeric", month: "short", day: "numeric" }))}</div>`;
      lastDay = dk;
    }
    return sep + (b.tools ? activityHtml(b.tools) : msgHtml(b.msg));
  }).join("");
  const gateCards = (state.boot?.approvals || []).filter((a) => {
    if (a.state !== "pending" || a.action !== "gate_op") return false;
    try { return JSON.parse(a.detail || "{}").conv_id === state.activeCid; }
    catch { return false; }
  });
  for (const a of gateCards) html += gateCardHtml(a);
  updateActivity(gateCards.length > 0);
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

/* B4: activity line right above the composer - what the agent is doing
   right now, for how long, and a Stop control for the live turn. */
function currentTurn() {
  const msgs = state.convMsgs || [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    const mt = msgs[i].meta || {};
    if (mt.turn_state) return { state: mt.turn_state, ts: msgs[i].ts };
  }
  return null;
}

function updateActivity(gatePending) {
  const bar = $("chat-status"), txt = $("act-text"),
        el = $("act-elapsed"), stop = $("chat-stop");
  if (!bar) return;
  const turn = currentTurn();
  const working = !!turn && turn.state === "streaming";
  state.turnWorking = working;
  state.turnSince = turn && working ? new Date(turn.ts).getTime() : null;
  // M7 §6 status semantics - honest at a glance: waiting for approval is
  // never shown as "thinking", a lost connection is never shown as work
  // in progress, and "thinking" only shows while no tool is executing.
  const blocked = state.convObj && state.convObj.job_state === "blocked";
  let label = "";
  if (working && (gatePending || blocked)) {
    label = "Waiting for your approval";
  } else if (working && state.sseDown) {
    label = "Reconnecting - the last recorded state is shown below";
  } else if (working) {
    const tools = (state.convMsgs || []).slice(-4).some(
      (m) => (m.meta || {}).kind === "tool" &&
             !["completed", "failed"].includes((m.meta || {})
               .tool_status || ""));
    label = tools ? "Running tools" : "Thinking";
  } else if (gatePending || blocked) {
    label = "Waiting for your approval";
  } else if (turn) {
    label = "Last turn: " + turn.state;
  }
  if (!label) {
    bar.classList.add("hidden"); stop.classList.add("hidden");
    return;
  }
  bar.classList.remove("hidden");
  bar.classList.toggle("working", working);
  txt.textContent = label;
  stop.classList.toggle("hidden", !working);
  tickElapsed();
}

function tickElapsed() {
  const el = $("act-elapsed");
  if (!el || !state.turnSince) { if (el) el.textContent = ""; return; }
  const s = Math.max(0, Math.floor((Date.now() - state.turnSince) / 1000));
  el.textContent = s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}
setInterval(() => { if (state.turnWorking) tickElapsed(); }, 1000);

$("chat-stop").addEventListener("click", async () => {
  $("chat-stop").disabled = true;
  try {
    const r = await api("POST", "/api/chat/stop",
                        { conv_id: state.activeCid || "" });
    toast(r.note || "stop requested");
    await refreshConv();
  } catch (e) { toast("stop failed: " + e.message, true); }
  $("chat-stop").disabled = false;
});

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
    // A1: finished runs must appear in Results WITHOUT a page reload -
    // this was the bug: the Results view was never re-rendered here.
    renderResults();
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
  renderScience();
}

function renderScience() {
  const host = $("cfg-science");
  if (!state.params) {
    host.innerHTML = "<p class='hint'>parameter inventory unavailable - " +
      "showing nothing rather than guessing options</p>";
    return;
  }
  const modules = state.params.groups.filter((g) => g.concern === "science");
  host.innerHTML = sciencePanelHtml(modules);
  host.querySelectorAll("input,select").forEach((el) =>
    el.addEventListener("change", updateSciState));
  updateSciState();
}

function updateSciState() {
  const el = $("sci-state");
  if (!el) return;
  const n = Object.keys(collectAdvanced()).length;
  el.textContent = n ? `${n} customization${n > 1 ? "s" : ""} - recorded in the run`
                     : "NeoAG defaults";
}

function collectAdvanced() {
  const cfg = {};
  document.querySelectorAll("[data-param]").forEach((el) => {
    if (el.type === "checkbox") {
      if (el.checked) cfg[el.dataset.param] = true;
    } else if (el.type === "radio") {
      // M6 PART E: the default radio carries value "" = "change nothing,
      // native default stays"; only a ticked non-default option is sent.
      if (el.checked && el.value !== "") cfg[el.dataset.param] = el.value;
    } else if (el.value !== "") {
      cfg[el.dataset.param] = el.value;
    }
  });
  state.requestedConfig = cfg;
  return cfg;
}

/* M6 PART E: the science panel renders ONLY the curated structured set
   served by /api/params (radio / bounded number / checkbox - never free
   text). Each row cites the deployed code and states what it changes;
   leaving it untouched forwards nothing, so native defaults apply. */
function sciencePanelHtml(modules) {
  const sc = state.params || {};
  const secs = sc.science_sections || {};
  const ctrl = sc.science || [];
  const mods = {};
  (modules || []).forEach((g) => { if (g.module) mods[g.module.id] = g.module; });
  if (!ctrl.length) {
    return "<h3>Scientific configuration</h3><p class='hint'>science control " +
      "inventory unavailable - showing nothing rather than guessing options</p>";
  }
  let html = `<h3>Scientific configuration</h3>
    <p class="hint">Every control below was verified against the deployed NeoAG
    code (cited per row) and is forwarded to native execution - the value
    is also re-validated on the server, so nothing unparseable can reach a
    run. Leaving a control at its default sends nothing: the native default
    shown applies. Scientific decisions the code makes on its own are listed
    below the controls instead of given fake knobs.</p>`;
  Object.keys(secs).forEach((sid) => {
    const items = ctrl.filter((c) => c.section === sid);
    if (!items.length) return;
    const mod = mods[sid];
    html += `<div class="pgroup"><h3>${esc(secs[sid])}</h3>`;
    if (mod) html += `<p class="hint">${esc(mod.purpose || "")}
      <br>Tools (from the configured repo's workflow TOML):
      <b>${esc((mod.tools || []).join(", "))}</b>
      <br><span class="psrc">${esc((mod.tools_provenance || []).join("; "))}</span></p>`;
    html += items.map((c) => scCtrlHtml(c)).join("") + "</div>";
  });
  const info = sc.science_info || [];
  if (info.length) {
    html += `<div class="pgroup"><h3>Decided by the deployed code - no knob offered</h3>
      ${info.map((i) => `<div class="pitem">
        <div><div class="pname">${esc(i.title)}</div></div><div></div>
        <div class="pdesc">${esc(i.decided)}</div>
        <div class="psrc">${esc(i.evidence)}</div></div>`).join("")}</div>`;
  }
  return html;
}

function scCtrlHtml(c) {
  const id = esc(c.id);
  let ctrl;
  if (c.control === "radio") {
    ctrl = `<div class="radios">
      <label class="radio"><input type="radio" name="sci-${id}" data-param="${id}"
        value="" checked><span>native default (${esc(String(c.native_default))})</span></label>
      ${(c.options || []).map((o) => `<label class="radio">
        <input type="radio" name="sci-${id}" data-param="${id}" value="${esc(o)}">
        <span>${esc(o)}</span></label>`).join("")}</div>`;
  } else if (c.control === "number") {
    ctrl = `<input type="number" data-param="${id}" min="${c.min}" max="${c.max}"
      step="${c.step || 1}" placeholder="native default: ${esc(String(c.native_default))}">`;
  } else {
    ctrl = `<label class="chk"><input type="checkbox" data-param="${id}">
      enabled for this run</label>`;
  }
  return `<div class="pitem">
    <div><div class="pname">${esc(c.label)} <code>--${esc(c.id.replace(/_/g, "-"))}</code></div>
    <div class="pdef">native default: ${esc(String(c.native_default))}</div></div>
    <div>${ctrl}</div>
    <div class="pdesc">${esc(c.affects || "")}</div>
    <div class="psrc">${esc(c.evidence || "")}</div></div>`;
}

/* M7 PART 2: the unified preparation flow. ONE data location in; the
   NATIVE NeoAG detector's answer reviewed; the human confirms (picking
   among real candidates where genuinely ambiguous); the confirmed
   manifest file is the single input source for planning AND execution.
   No per-file fields, no engine choice, no mock substitution in this
   flow - the simulation engine lives under Demo & developer controls. */
const prep = { disc: null, manifest: null };

function prepSetState(txt, cls) {
  const el = $("prep-state");
  el.textContent = txt;
  el.className = "pill " + (cls || "muted");
}
function prepSetActions(hint, canPreview, canSubmit) {
  $("prep-hint").textContent = hint;
  $("nj-native-preview").disabled = !canPreview;
  $("nj-submit").disabled = !canSubmit;
}

$("prep-discover").addEventListener("click", async () => {
  const data_dir = $("prep-dir").value.trim();
  if (!data_dir) { toast("enter the sequencing data directory first", true); return; }
  $("prep-flow").innerHTML =
    '<div class="hint">inspecting the directory with the NeoAG detector ' +
    "(read-only; VCFs and tables are read as the detector requires)...</div>";
  prepSetState("discovering...", "warn");
  prepSetActions("scanning", false, false);
  loadNativeCap();
  try {
    const d = await api("POST", "/api/native/discover", { data_dir });
    prep.disc = d; prep.manifest = null;
    renderDiscovery(d);
  } catch (e) {
    $("prep-flow").innerHTML = `<div class="mnote warn"><b>Discovery ` +
      `failed:</b> ${esc(e.message)}<br><span class="hint">nothing was ` +
      `proposed from this directory, and no demo data was substituted.</span></div>`;
    prepSetState("discovery failed", "danger");
    prepSetActions("fix the path above and try again", false, false);
  }
});

function renderDiscovery(d) {
  const s = d.summary || {};
  const amb = d.ambiguous || [];
  const blocked = (d.missing || []).length > 0;
  const pill = blocked ? '<span class="pill danger">blocked - see details</span>'
    : amb.length ? '<span class="pill warn">needs one confirmation</span>'
    : '<span class="pill ok">ready for native analysis</span>';
  const list = (a) => (a && a.length) ? a.map(esc).join(" &middot; ") : "none found";
  const ambHtml = amb.map((a) => (a.candidates || []).length ? `
    <div class="pitem"><div><div class="pname">${esc(a.label || a.field)}</div>
    <div class="pdesc">several candidates were found - pick one:</div></div>
    <div>${(a.candidates || []).map((c) =>
      `<label class="chk"><input type="radio" name="amb-${esc(a.field)}" ` +
      `value="${esc(c)}"> <code>${esc(String(c).split("/").pop())}</code></label>`
    ).join("")}</div></div>` : `
    <div class="pitem"><div><div class="pname">${esc(a.label || a.field)}</div>
    <div class="pdesc">${esc(a.reason || "the native scanner flagged this")}` +
    ` - add or correct the file, then scan again</div></div></div>`).join("");
  $("prep-flow").innerHTML = `<div class="manifest-card">
    <div class="mnote"><b>Case ${esc(s.case_guess || "")}</b> ${pill}</div>
    <div class="kv">
      <div class="k">Sample identity</div><div>${esc(s.relationship ||
        (s.tumor_sample_id ? "tumor sample " + s.tumor_sample_id +
         " read from the data" :
         "no paired-sample evidence yet - the directory name is used, " +
         "rename it below if you like"))}</div>
      <div class="k">DNA sequencing</div><div>${list(s.dna)}</div>
      <div class="k">RNA sequencing</div><div>${list(s.rna)}</div>
      <div class="k">Existing evidence</div><div>${list(s.evidence)}</div>
      <div class="k">Ambiguous assignments</div><div>${amb.length ? amb.length + " - choose below" : "none"}</div>
      <div class="k">Missing required data</div><div>${blocked ? (d.missing || []).length + " - blocking" : "none"}</div>
      <div class="k">HLA</div><div>${esc(d.native_hla_note || "")}</div>
    </div>
    ${amb.length ? `<h3>Confirm which file to use</h3>${ambHtml}` : ""}
    <details class="adv"><summary>Input details - the ${s.n_recognized || 0} recognized file(s) of ${s.n_files || 0} seen</summary>
      <div class="tablewrap"><table class="data"><thead><tr><th>Role</th><th>File</th><th>State</th></tr></thead><tbody>
      ${(d.inputs || []).map((i) => `<tr><td>${esc(i.role)}</td><td><code>${esc(i.path)}</code></td><td>${esc([i.status, i.bam_index && ("bam index " + i.bam_index)].filter(Boolean).join(", "))}</td></tr>`).join("")}
      </tbody></table></div>
      ${(d.routes || []).length ? `<p class="hint">Native routing from these inputs: ${(d.routes || []).map((r) => `<b>${esc(r.route)}</b> ${esc(r.status)}`).join(" &middot; ")}</p>` : ""}
      ${blocked ? `<p class="hint">Blocking: <ul>${(d.missing || []).map((m) => `<li>${esc(m.field || "")}${m.field ? ": " : ""}${esc(m.reason || m)}</li>`).join("")}</ul></p>` : ""}
      ${(d.warnings || []).length ? `<p class="hint">${(d.warnings || []).map(esc).join("<br>")}</p>` : ""}
      ${(d.unmatched || []).length ? `<p class="hint">${d.unmatched.length} file(s) were not recognized as inputs and stay untouched: ${(d.unmatched || []).slice(0, 12).map((f) => `<code>${esc(f)}</code>`).join(" ")}${d.unmatched.length > 12 ? " ..." : ""}</p>` : ""}
    </details>
    <label class="lbl">Sample / case id for this run</label>
    <input type="text" id="prep-sample" value="${esc(((s.tumor_sample_id || s.case_guess || "").replace(/_T$/i, "")).toUpperCase())}">
    <div class="btnrow">
      <button id="prep-confirm" class="btn primary" ${blocked ? "disabled" : ""}>Confirm these inputs</button>
      <span class="hint">${blocked ? "resolve the blocking items first - a plan would fail honestly the same way" : "the confirmed manifest is reused by planning and execution - nothing will be asked again"}</span>
    </div>
  </div>`;
  prepSetState(blocked ? "blocked" : (amb.length ? "review needed" : "inputs detected"),
               blocked ? "danger" : (amb.length ? "warn" : "ok"));
  prepSetActions(blocked ? "the detector reported what is missing - add it to the directory and re-scan"
                         : "review the detected inputs, then confirm", false, false);
  const cb = $("prep-confirm");
  if (cb) cb.addEventListener("click", confirmInputs);
}

async function confirmInputs() {
  const picked = {};
  document.querySelectorAll("#prep-flow input[type=radio]:checked").forEach((r) => {
    picked[r.name.replace(/^amb-/, "")] = r.value;
  });
  const unresolved = (prep.disc.ambiguous || [])
    // only entries that actually offer candidate files can be picked;
    // the rest are notes resolved by fixing the directory + re-scan
    .filter((a) => (a.candidates || []).length && !picked[a.field])
    .map((a) => a.label || a.field);
  if (unresolved.length) {
    toast("choose among the candidates for: " + unresolved.join(", "), true);
    return;
  }
  try {
    const m = await api("POST", "/api/native/manifest", {
      data_dir: prep.disc.data_dir,
      sample_id: $("prep-sample").value.trim(),
      picked });
    prep.manifest = m;
    renderConfirmed(m);
    toast("manifest written - the planner and the runner both read this run's inputs from that single file");
  } catch (e) { toast("could not write the manifest: " + e.message, true); }
}

function renderConfirmed(m) {
  const p = m.proposal || {};
  const old = $("confirmed-card");
  if (old) old.remove();
  const div = document.createElement("div");
  div.id = "confirmed-card";
  div.innerHTML = `<div class="manifest-card">
    <div class="mnote"><span class="pill ok">confirmed</span> input manifest
      <b>${esc(m.id)}</b> &middot; sample <b>${esc(p.sample_id || "")}</b>
      &middot; ${(p.inputs || []).length} inputs &middot; native schema
      open-neo-sample-v1</div>
    <p class="hint">${esc(p.note || "")}</p>
    <details class="adv"><summary>Confirmed input list</summary>
      ${(p.inputs || []).map((i) => `<div><b>${esc(i.role)}</b>: <code>${esc(i.path)}</code></div>`).join("")}</details>
  </div>`;
  $("prep-flow").appendChild(div);
  const c = $("prep-confirm");
  if (c) { c.disabled = true; c.textContent = "Confirmed"; }
  prepSetState("manifest confirmed", "ok");
  prepSetActions("preview the native plan, or submit for approval", true, true);
}

/* native capability line (honest, cached) */
let nativeCap = null;
function renderNativeCap() {
  const host = $("native-cap");
  if (!nativeCap || !host) return;
  const t = nativeCap.toolchain || {};
  const y = (b) => b ? "present" : "<b>MISSING</b>";
  host.innerHTML = nativeCap.available
    ? `Native source imports <b>OK</b> &middot; demo fixtures ${y(t.demo_fixtures && t.demo_fixtures.present)}` +
      ` &middot; reference FASTA ${y(t.reference_fasta && t.reference_fasta.present)}` +
      ` &middot; VEP ${y(t.vep_bin && t.vep_bin.present)}` +
      ` &middot; native gateway ${nativeCap.gateway && nativeCap.gateway.running
        ? "running (" + esc(String(nativeCap.gateway.url)) + ")"
        : "not running - started on demand at submission"}` +
      ` &middot; gateway cancel: ${nativeCap.cancel_supported_by_native_gateway ? "supported" : "NOT supported (verified limitation)"}`
    : `<b>Native execution UNAVAILABLE on this host</b> - discovery and ` +
      `native jobs report the failure honestly; nothing is silently ` +
      `substituted by the simulation engine`;
}
async function loadNativeCap() {
  if (nativeCap) { renderNativeCap(); return; }
  const host = $("native-cap");
  if (!host) return;
  host.textContent = "checking native capability...";
  try { nativeCap = await api("GET", "/api/native/capability"); renderNativeCap(); }
  catch (e) { host.innerHTML = "capability check failed: " + esc(e.message); }
}

/* Demo/Developer mode gate (Milestone 5 PART 2 concern D): the
   simulation engine and developer controls are hidden until explicitly
   enabled, and stay off by default in every fresh session. */
function devModeOn() {
  try { return localStorage.getItem("nj_devmode") === "1"; }
  catch (_) { return false; }
}
function applyDevMode() {
  const on = devModeOn();
  const cb = $("devmode-toggle");
  if (cb) cb.checked = on;
  $("dev-controls").classList.toggle("hidden", !on);
  if (!on) $("nj-fail").checked = false;
  const stubWrap = $("nj-stub-wrap");
  if (stubWrap) {
    const showStub = on && !$("nj-n-demo").checked;
    stubWrap.style.display = showStub ? "" : "none";
    if (!showStub) $("nj-n-stub").checked = false;
  }
}
$("devmode-toggle").addEventListener("change", (e) => {
  try { localStorage.setItem("nj_devmode", e.target.checked ? "1" : "0"); }
  catch (_) { /* storage unavailable: session-only toggle */ }
  applyDevMode();
});
$("nj-n-demo").addEventListener("change", () => applyDevMode());
applyDevMode();

function renderNativePlan(plan) {
  let host = $("native-plan-view");
  if (!host) {
    host = document.createElement("div");
    host.id = "native-plan-view";
    $("prep-flow").appendChild(host);
  }
  if (!plan) return;
  const steps = (plan.steps || []).map((st) =>
    `<li><b>${esc(st.step_id || "")} ${esc(st.name || "")}</b> ` +
    `<span class="pill ${st.status === "PASS" ? "ok"
      : st.status === "SKIPPED" || st.status === "DRY_RUN" ? "muted"
      : st.status === "FAILED" || st.status === "BLOCKED" ? "danger"
      : "warn"}">${esc(st.status || "")}</span>` +
    (st.detail ? ` <span class="hint">${esc(String(st.detail).slice(0, 140))}</span>` : "") +
    `</li>`).join("");
  host.innerHTML = `<div class="manifest-card">
    <div class="mnote ${plan.ok ? "" : "warn"}">native plan: <b>${esc(plan.status || plan.status_code || "?")}</b>${plan.run_id ? " &middot; run " + esc(plan.run_id) : ""}</div>
    ${steps ? `<ul>${steps}</ul>` : ""}
    ${plan.blocking_issues && plan.blocking_issues.length
      ? `<p class="hint">Blocking: ${plan.blocking_issues.map(esc).join(", ")}</p>` : ""}
    ${plan.missing_evidence && plan.missing_evidence.length
      ? `<p class="hint">Toolchain not installed for: ${plan.missing_evidence.map(esc).join(", ")} - the plan marks those steps BLOCKED honestly</p>` : ""}
    ${plan.detail ? `<p class="hint">${esc(plan.detail)}</p>` : ""}
    ${plan.run_plan_md ? `<details><summary class="hint">generated run plan (native markdown)</summary><pre class="tool-out">${esc(plan.run_plan_md)}</pre></details>` : ""}
  </div>`;
}

$("nj-native-preview").addEventListener("click", async () => {
  const m = prep.manifest;
  if (!(m && m.status === "confirmed")) {
    toast("confirm the detected inputs first - the preview plans exactly those inputs, nothing invented", true);
    return;
  }
  let host = $("native-plan-view");
  if (!host) {
    host = document.createElement("div");
    host.id = "native-plan-view";
    $("prep-flow").appendChild(host);
  }
  host.innerHTML = '<div class="hint">running the REAL NeoAG planner on the confirmed manifest (read-only, executes nothing)... this can take a minute</div>';
  try {
    const plan = await api("POST", "/api/jobs/native/preview", { manifest_id: m.id });
    renderNativePlan(plan);
  } catch (e) {
    host.innerHTML = `<div class="mnote warn">preview failed: ${esc(e.message)}</div>`;
  }
});

/* submit: the GUI and the chat path post to the SAME /api/jobs endpoint
   - one Job ID either way, registered in the unified task system. */
$("nj-submit").addEventListener("click", async () => {
  if (devModeOn() && $("nj-n-demo").checked) {
    const body = { engine: "native", synthetic_demo: true,
                   stub: $("nj-n-stub").checked };
    const sid = ($("prep-sample") || {}).value;
    if (sid) body.sample_id = sid;
    try {
      const res = await api("POST", "/api/jobs", body);
      toast(`native synthetic-demo job ${res.job_id} waiting for your approval (Tasks panel above)`);
      await refreshJobs();
    } catch (e) { toast("native demo submit failed: " + e.message, true); }
    return;
  }
  const m = prep.manifest;
  if (!(m && m.status === "confirmed")) {
    toast("first discover the inputs and confirm them - the confirmed manifest is the run's input list", true);
    return;
  }
  const body = { engine: "native", manifest_id: m.id,
                 stub: $("nj-n-stub").checked };
  const sci = collectAdvanced();
  const fwd = {};
  ["event_top_n", "candidate_top_n", "genome_build",
   "rna_quant_method", "automatic_tool_policy",
   "skip_dna_sv", "skip_bam_matcher"].forEach((k) => {
    if (sci[k] !== undefined && sci[k] !== "") fwd[k] = sci[k];
  });
  if (Object.keys(fwd).length) body.native_params = fwd;
  try {
    const res = await api("POST", "/api/jobs", body);
    toast(`native job ${res.job_id} waiting for your approval ` +
      "(Tasks panel above) - this page stays usable while it runs");
    await refreshJobs();
  } catch (e) { toast("native submit failed: " + e.message, true); }
});

/* simulation engine (demo) submission - dev controls only */
$("nj-submit-demo").addEventListener("click", async () => {
  const sample = $("nj-sample").value;
  const stage_ids = [...document.querySelectorAll("[data-stage]")]
    .filter((c) => c.checked).map((c) => c.dataset.stage);
  const execution_mode = document.querySelector(
    'input[name="execmode"]:checked').value;
  const body = { sample_id: sample, stage_ids, mode: "auto",
                 execution_mode, simulate_failure: $("nj-fail").checked,
                 kind: "analysis", requested_config: collectAdvanced(),
                 requested_by: "user (simulation engine)" };
  try {
    const res = await api("POST", "/api/jobs", body);
    toast(`simulated job ${res.job_id} waiting for your approval (demo engine)`);
    await refreshJobs();
  } catch (e) { toast("submit failed: " + e.message, true); }
});

/* attention strip: anything blocked on a human click */
function renderAttention() {
  const host = $("attention");
  const rows = [];
  for (const a of (state.boot.approvals || [])) {
    if (a.state !== "pending" || a.action === "gate_op") continue;
    let info = {};
    try { info = JSON.parse(a.detail || "{}"); } catch { /* plain note */ }
    if (info.kind === "benchmark_consent") {
      // M6 C: the consent card states the whole experiment - dataset
      // and source, question, tools, download size, destination,
      // compute, metrics, installation, production impact - and offers
      // the four answers the user is entitled to give.
      const fields = (info.fields || [])
        .map((f) => `<b>${esc(f.label)}</b><span>${esc(f.value)}</span>`)
        .join("");
      rows.push(`<div class="at-col">
        <span><b>Benchmark consent</b> - job ${esc(a.job_id)} (${esc(a.risk)} risk)</span>
        <div class="at-fields">${fields}</div>
        <span class="hint">${esc(info.note || "")}</span>
        <span class="btnrow" style="margin:0">
          <button class="btn primary sm" data-appr="${esc(a.id)}" data-dec="approve">Approve</button>
          <button class="btn sm" data-appr="${esc(a.id)}" data-dec="limit_scope">Limit scope…</button>
          <button class="btn sm" data-appr="${esc(a.id)}" data-dec="literature_only">Literature only</button>
          <button class="btn danger sm" data-appr="${esc(a.id)}" data-dec="reject">Reject</button>
        </span></div>`);
      continue;
    }
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
        `<button class="btn sm" data-dec="${esc(d.id)}" data-choice="${esc(o.id)}"
          title="${esc(o.validation_status || "")}">${esc(o.label)}${o.policy_default ? " (policy default)" : ""}</button>`).join("")}</span></div>`);
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
    el.addEventListener("click", () => {
      const id = el.dataset.job;
      if (/^res-/.test(id)) {
        // §1.2: research task row -> its report lives in the Discovery
        // workspace; jump there instead of opening the job modal
        const tab = document.querySelector(
          '.wtab[data-view="discovery"]');
        if (tab) tab.click();
        showReport(id);
        return;
      }
      openJob(id);
    }));
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
  let detail = [], task = null;
  try { detail = await api("GET", `/api/jobs/${id}/events`); }
  catch (e) { toast("cannot open job: " + e.message, true); return; }
  try { task = await api("GET", `/api/tasks/${id}`); } catch { /* older row */ }
  const job = (state.boot.jobs || []).find((j) => j.id === id);
  const decisions = (task && task.decisions) || [];
  const jobEl = $("job-detail");
  const running = job && job.state === "running";
  jobEl.classList.remove("hidden");
  const arts = (task && task.artifacts) || [];
  let snapHtml = `<div class="hint">loading snapshot...</div>`;
  jobEl.innerHTML = `
    <button class="btn sm ghost close" id="jd-close">Close</button>
    <h2>${esc(job ? job.sample_id : id)}</h2>
    <div class="btnrow">${job ? statusPill(job.state) : ""}
      ${task && task.outcome ? `<span class="pill ${task.outcome === "delivered"
        ? "ok" : task.outcome ? "warn" : "muted"}">${esc(task.outcome)}</span>` : ""}
      <span class="pill muted">${esc(job ? job.kind : "?")}</span>
      ${task && task.native ? `<span class="pill accent">native ${esc(task.native.profile || "")}</span>`
        : `<span class="pill warn">mock engine</span>`}</div>
    <section><h3>Controls</h3><div class="btnrow">
      <button class="btn sm" id="jd-pause" ${running && !(task && task.native) ? "" : "disabled"}
        title="${task && task.native ? "native runs have no safe pause point (real pipeline process)" : ""}">Pause</button>
      <button class="btn sm" id="jd-resume" ${job && job.state === "paused" ? "" : "disabled"}>Resume</button>
      <button class="btn sm danger" id="jd-cancel"
        ${job && !["completed", "failed", "cancelled"].includes(job.state) ? "" : "disabled"}
        title="${task && task.native && task.native.profile === "gateway" ? "cancelling stops the Web job only - the native gateway job has no cancel route (verified) and runs on" : ""}">Cancel</button>
      ${task && task.native && task.native.profile === "gateway"
        ? `<span class="hint">native gateway job ${esc(task.native.gateway_job_id || "")}${task.native.gateway_job_id ? "" : " (not yet submitted)"} - cancel caveats in the event log</span>` : ""}
    </div></section>
    ${task && task.native ? `<section><h3>Native execution</h3>
      <div class="kv">
        <div class="k">engine</div><div>${esc(task.native.engine || "native")} ${esc(task.native.engine_version || "")} (${esc(task.native.profile || "")})</div>
        <div class="k">inputs</div><div><code>${esc(JSON.stringify(task.native.native_params || {}))}</code></div>
        <div class="k">output directory</div><div><code>${esc(task.native.outdir || "-")}</code></div>
        ${task.native.failure_code ? `<div class="k">failure</div><div>${esc(task.native.failure_code)}</div>` : ""}
      </div>
      ${task.native.plan && (task.native.plan.steps || []).length
        ? `<details open><summary class="hint">native plan (produced by NeoAG's own planner, not a hardcoded stage list)</summary><ul>${
            (task.native.plan.steps || []).map((s) => `<li><b>${esc(s.step_id || "")} ${esc(s.name || "")}</b>: ${esc(s.status || "")}${s.detail ? " - " + esc(String(s.detail).slice(0, 140)) : ""}</li>`).join("")
          }</ul></details>` : ""}
      ${task.native.plan && task.native.plan.run_plan_md
        ? `<details><summary class="hint">generated run plan (markdown)</summary><pre class="tool-out">${esc(task.native.plan.run_plan_md)}</pre></details>` : ""}
    </section>` : ""}
    <section><h3>Requested configuration</h3>
      <div class="kv">${Object.entries((job && job.spec &&
        job.spec.requested_config) || {})
        .map(([k, v]) => `<div class="k">${esc(k)}</div><div>${esc(v)}</div>`).join("") ||
        '<div class="hint">defaults only</div>'}</div></section>
    <section><h3>Output artifacts</h3>
      ${arts.length ? `<div class="tablewrap"><table class="data">
        <thead><tr><th>Artifact</th><th>Status</th><th>Rows</th><th>Checksum</th></tr></thead>
        <tbody>${arts.map((a) => `<tr><td>${esc(a.name)}</td>
          <td><span class="pill ${a.status === "available" ? "ok"
            : a.status === "empty" ? "warn" : "danger"}">${esc(a.status)}</span></td>
          <td>${esc(a.rows ?? "")}</td><td><code>${esc(a.checksum || "")}</code></td></tr>`).join("")}</tbody></table></div>`
      : '<div class="hint">no artifacts registered (pre-artifact run or nothing generated)</div>'}</section>
    <section><h3>Decision history (with applied effects)</h3><div class="timeline">${decisions.map((d) => `
      <div class="tl-row"><span class="tl-ts">${esc(d.ts || "")}</span>
        <span class="tl-kind">${esc(d.key || d.decision_type || "")}</span>
        <span>${esc(d.question || "")} -> <b>${esc(d.chosen || "pending")}</b>
        <span class="hint">(${esc(d.decided_by || "")}, ${esc(d.policy_id || "")}${
          d.policy_version ? " v" + esc(d.policy_version) : ""})</span>
        ${d.downstream && d.downstream.applied_effect
          ? `<div class="hint">effect applied: ${esc(
              JSON.stringify(d.downstream.applied_effect))}</div>` : ""}</span>
      </div>`).join("") || '<div class="hint">no decisions recorded</div>'}</div></section>
    <section><h3>Immutable run snapshot</h3>
      <div id="jd-snapshot">${snapHtml}</div></section>
    <section><h3>Live events</h3><div class="logview">${detail.slice(0, 120)
      .map((e) => `${esc(e.ts || "")} ${esc(e.kind || "")} ${esc(e.text || "")}`)
      .join("\n")}</div></section>`;
  $("jd-close").addEventListener("click", () => jobEl.classList.add("hidden"));
  const pc = $("jd-pause"), rc = $("jd-resume"), cc = $("jd-cancel");
  pc.addEventListener("click", () => api("POST", `/api/jobs/${id}/pause`, {}).then(refreshJobs));
  rc.addEventListener("click", () => api("POST", `/api/jobs/${id}/resume`, {}).then(refreshJobs));
  cc.addEventListener("click", () => api("POST", `/api/jobs/${id}/cancel`, {}).then(refreshJobs));
  api("GET", `/api/tasks/${id}/snapshot`).then((snap) => {
    const s = snap.snapshot || {};
    $("jd-snapshot").innerHTML = `
      <div class="kv">${Object.entries({ job: s.job_id, engine: s.engine_version,
        policy: s.policy_version, started: snap.created_at,
        execution_mode_at_start: s.execution_mode_at_start,
        dataset: s.dataset_version,
        seed: s.seed,
        stage_plan: (s.stage_plan || []).join(", ") })
        .map(([k, v]) => `<div class="k">${esc(k)}</div><div>${esc(
          typeof v === "object" ? JSON.stringify(v) : v)}</div>`).join("")}</div>
      <details class="adv"><summary>full recorded snapshot</summary>
        <pre class="tool-out">${esc(JSON.stringify(s, null, 1))}</pre></details>
      <div class="hint">written once when the run started; never updated -
        later setting/policy/skill changes cannot rewrite this record.</div>`;
  }).catch(() => {
    $("jd-snapshot").innerHTML = `<div class="hint">no snapshot recorded for
      this run (created before the snapshot contract existed)</div>`;
  });
}

/* ---------------- results (rendered from the OUTPUT MANIFEST) --------- */
const OUTCOME_CLS = { delivered: "ok", zero_candidates: "warn",
  partial: "warn", generation_failed: "danger", no_artifacts: "danger" };

function candTable(rows) {
  if (!rows || !rows.length) {
    return `<div class="mnote"><b>0 rows.</b> An empty candidate table is
      a legitimate result, not a hidden failure - the artifact above shows
      status <b>empty</b> and the run outcome reflects it.</div>`;
  }
  return `<div class="tablewrap"><table class="data">
      <thead><tr><th>Rank</th><th>Peptide</th><th>Gene</th><th>HLA</th>
      <th>Branch</th><th>Score</th><th>Normal hits</th></tr></thead>
      <tbody>${rows.map((r) => `
      <tr><td>${esc(r.rank)}</td><td><code>${esc(r.peptide)}</code></td>
      <td>${esc(r.gene)}</td><td>${esc(r.hla)}</td><td>${esc(r.branch)}</td>
      <td>${esc(r.mock_combined_score ?? r.combined_score ?? "")}</td>
      <td>${esc(r.safety_normal_hits ?? "")}</td></tr>`).join("")}</tbody>
    </table></div>`;
}

function renderResults() {
  // research rows share the unified task list but have no run results -
  // their report lives in the Discovery workspace
  const done = (state.boot.jobs || []).filter((j) => j.state === "completed"
                                              && !/^res-/.test(j.id));
  $("res-job").innerHTML = done.map((j) =>
    `<option value="${esc(j.id)}">${esc(j.id)} - ${esc(j.sample_id)}` +
    `${j.outcome ? " - " + esc(j.outcome) : ""}</option>`).join("");
  if (!done.length) {
    $("res-outcome").innerHTML = `<div class="empty">No completed runs yet.</div>`;
    $("res-meta").innerHTML = ""; $("res-table").innerHTML = "";
    $("res-warnings").innerHTML = ""; $("res-artifacts").innerHTML = "";
    $("res-strategy").innerHTML = ""; $("res-manifest").innerHTML = "";
    return;
  }
  if (!state.resultsJob || !done.find((j) => j.id === state.resultsJob))
    state.resultsJob = done[0].id;
  $("res-job").value = state.resultsJob;
  api("GET", `/api/results/${state.resultsJob}`).then((res) => {
    const outcome = res.outcome || "no_artifacts";
    $("res-outcome").innerHTML = `
      <div class="outcome ${esc(OUTCOME_CLS[outcome] || "muted")}">
        <b>${esc(outcome.replace(/_/g, " "))}</b>
        <div>${esc(res.outcome_note || "")}</div>
        <div class="hint">${esc(res.delivery_claim || "")}</div>
      </div>
      ${res.outcome_drift ? `<div class="mnote warn">${esc(res.outcome_drift)}</div>` : ""}
      ${res.production_release === false
        ? `<div class="mnote">production release BLOCKED: ${esc(res.release_note || "")}</div>` : ""}
      <div class="btnrow"><a class="btn sm"
        href="/api/results/${esc(state.resultsJob)}/bundle"
        download>Download results bundle (.zip)</a>
        <span class="hint">built once from the persisted job state (result,
        ranking tables, decision history, immutable snapshot, artifacts,
        provenance); raw FASTQ/BAM-class inputs and oversized files are
        NEVER included and every exclusion is listed with its reason in
        the bundle manifest; re-download serves the same pinned bytes</span>
      </div>`;
    $("res-meta").innerHTML = `
      <div class="hint">${esc(res.disclaimer || "")}</div>
      <div class="kv">${Object.entries(res.provenance || {})
        .map(([k, v]) => `<div class="k">${esc(k)}</div><div>${esc(v)}</div>`).join("")}</div>`;
    $("res-warnings").innerHTML = (res.warnings || []).length
      ? `<div class="warnlist">${res.warnings.map((w) =>
          `<div class="mnote warn">warning: ${esc(w)}</div>`).join("")}</div>` : "";
    if (res.engine === "benchmark") {
      // M5 PART 6: the stated metrics are shown COMPUTED or honestly
      // marked unanswered - never silently omitted
      const m = res.metrics || {};
      const ck = (res.arms_summary || {}).candidate || {};
      $("res-meta").insertAdjacentHTML("afterbegin",
        `<div class="card"><b>Benchmark metrics</b>` +
        `<div class="hint">candidate arm: ${esc(ck.kind || "")}` +
        `${ck.tool ? " (" + esc(ck.tool) + ")" : ""}</div>` +
        (m.computed ? `<pre class="json">${esc(JSON.stringify(m.computed, null, 1))}</pre>` : "") +
        `<div class="mnote${m.computed ? "" : " warn"}">${esc(m.note || "")}</div>` +
        `<div class="hint">seed ${esc((res.reproducibility || {}).seed || "")}` +
        ` - the arm files under this run's directory are the measured` +
        ` artefacts (see the results bundle)</div></div>`);
    }
    const arts = res.artifacts || (res.manifest || {}).artifacts || [];
    $("res-artifacts").innerHTML = `<h3 class="sec">Output artifacts (${arts.length})</h3>` +
      (arts.length ? `<div class="tablewrap"><table class="data">
        <thead><tr><th>Artifact</th><th>Strategy</th><th>Status</th>
        <th>Rows</th><th>Checksum</th></tr></thead><tbody>${arts.map((a) => `
          <tr><td>${esc(a.name)}</td><td>${esc(a.strategy || "")}</td>
          <td><span class="pill ${a.status === "available" ? "ok"
            : a.status === "empty" ? "warn" : "danger"}">${esc(a.status)}</span></td>
          <td>${esc(a.rows ?? "")}</td>
          <td><code>${esc(a.checksum || "")}</code></td></tr>`).join("")}</tbody></table></div>`
      : `<div class="mnote warn">No artifacts are registered for this run -
          nothing from it can be described as delivered.</div>`);
    // A2: every strategy the run produced gets its own table, not one
    const byS = res.candidates_by_strategy || {};
    const strategies = Object.keys(byS).length ? Object.keys(byS)
      : (res.candidates_strategy ? [res.candidates_strategy] : []);
    if (!state.resultsStrategy || !strategies.includes(state.resultsStrategy))
      state.resultsStrategy = strategies[0] || null;
    if (strategies.length) {
      $("res-strategy").innerHTML = `<div class="row"><label class="lbl">Ranking
        <select id="res-strategy-sel">${strategies.map((s) => {
          const a = arts.find((x) => x.strategy === s);
          return `<option value="${esc(s)}">${esc(s)}${
            a ? ` (${a.rows} rows, ${a.status})` : ""}</option>`;
        }).join("")}</select></label></div>`;
      $("res-strategy-sel").value = state.resultsStrategy;
      $("res-strategy-sel").addEventListener("change", () => {
        state.resultsStrategy = $("res-strategy-sel").value;
        renderResults();
      });
      $("res-table").innerHTML = candTable(
        byS[state.resultsStrategy] || res.candidates || []);
    } else {
      $("res-strategy").innerHTML = "";
      $("res-table").innerHTML = arts.length
        ? `<div class="mnote">no candidate rows to display</div>` : "";
    }
    const man = res.manifest || {};
    const kv = (o) => Object.entries(o || {}).map(([k, v]) =>
      `<div class="k">${esc(k)}</div><div>${esc(
        typeof v === "object" ? JSON.stringify(v) : v)}</div>`).join("");
    $("res-manifest").innerHTML = `
      <div class="kv">${kv({ manifest_ref: man.manifest_ref,
        execution_mode: man.execution_mode,
        requested_vs_effective_note: man.requested_vs_effective_note,
        generated_at: man.generated_at, snapshot: man.snapshot })}</div>
      <h3 class="sec">Requested configuration</h3>
      <div class="kv">${kv(man.requested_config) || "<div class='hint'>defaults only</div>"}</div>
      <h3 class="sec">Effective configuration (what this run really applied)</h3>
      <div class="kv">${kv(man.effective_config)}</div>
      <h3 class="sec">Versions</h3>
      <div class="kv">${kv(man.versions)}</div>
      <h3 class="sec">Decisions this run applied</h3>
      <div class="timeline">${(man.decisions || []).map((d) => `
        <div class="tl-row"><span class="tl-kind">${esc(d.key || d.decision_type)}</span>
        <span>${esc(d.question)} -> <b>${esc(d.chosen || "pending")}</b> (${esc(d.decided_by)}, ${esc(d.policy_id || "")})</span></div>`).join("")
        || '<div class="hint">none</div>'}</div>`;
  }).catch((e) => {
    $("res-outcome").innerHTML = `<div class="mnote warn">results unavailable: ${esc(e.message)}</div>`;
    $("res-table").innerHTML = "";
  });
}
$("res-job").addEventListener("change", () => {
  state.resultsJob = $("res-job").value;
  state.resultsStrategy = null;
  renderResults();
});

/* ---------------- discovery (real retrieval + example fixture) -------- */
function discoveryTargets() {
  const d = state.boot?.discovery || {};
  return ($("res-engine")?.value || "live") === "example"
    ? (d.example_targets || []) : (d.live_targets || []);
}

function renderDiscoveryControls() {
  const scope = $("res-scope").value;
  const targeted = scope === "targeted";
  $("res-target-hint").textContent = targeted
    ? "(one or more; from the configured repository's real stage inventory)"
    : "- full pipeline review covers every inventoried stage";
  const box = $("res-targets");
  box.classList.toggle("off", !targeted);
  const sel = state.discSel || {};
  box.innerHTML = discoveryTargets().map((t) => `
    <label class="stage-row"><input type="checkbox" data-dtarget="${esc(t.id)}"
      ${sel[t.id] ? "checked" : ""} ${targeted ? "" : "disabled"}>
      <span>${esc(t.label)}</span>
      ${t.code_verified ? "" : '<span class="dep">docs-level stage</span>'}
      ${(t.tools || []).length
        ? `<span class="dep">uses: ${esc(t.tools.join(", "))}</span>` : ""}
    </label>`).join("");
  box.querySelectorAll("[data-dtarget]").forEach((c) =>
    c.addEventListener("change", () => {
      state.discSel = state.discSel || {};
      state.discSel[c.dataset.dtarget] = c.checked;
    }));
  $("res-custom").disabled = !targeted;
}

function renderDiscovery() {
  const list = (state.boot.research || []);
  const ACTIVE = ["running", "queued"];
  $("res-list").innerHTML = list.length ? list.map((r) => `
    <div class="job-row" data-res="${esc(r.id)}">
      <div class="jr-top"><span class="jr-id">${esc(r.id)}</span>
        <span class="pill ${r.engine === "live" ? "accent" : "muted"}">${esc(r.engine || "example")}</span>
        <span class="pill muted">${esc(r.scope)}</span>
        <span class="pill ${r.status === "completed" ? "ok"
          : ACTIVE.includes(r.status) ? "accent" : "warn"}">${esc(r.status)}</span>
      </div>
      <div class="jr-meta">${esc(r.target || "")}</div>
      ${(r.progress && r.progress.note)
        ? `<div class="jr-meta hint">${esc(r.progress.note)}</div>` : ""}
    </div>`).join("")
    : `<div class="empty">No research runs yet.</div>`;
  document.querySelectorAll("[data-res]").forEach((el) =>
    el.addEventListener("click", () => showReport(el.dataset.res)));
  if (list.length && !list.some((r) => r.id === state.discOpen))
    showReport(list[0].id);
  // keep the view honest while something runs (SSE may be buffered)
  clearTimeout(renderDiscovery._t);
  if (list.some((r) => ACTIVE.includes(r.status)))
    renderDiscovery._t = setTimeout(refreshDiscovery, 2500);
}

function exportBar(rid) {
  // M5 PART 7: files are rendered ONCE server-side, pinned to the stored
  // report content, and re-downloads serve those exact bytes (the view
  // never re-runs retrieval to serve a download).
  const b = (fmt, label) =>
    `<a class="btn sm" href="/api/research/${encodeURIComponent(rid)}` +
    `/export/${fmt}" download>${label}</a>`;
  return `<div class="btnrow">${b("html", "Download HTML")}` +
    `${b("json", "Download JSON")}${b("tsv", "Download TSV tables")}` +
    `${b("pdf", "Download PDF")}<span class="hint">persisted per report ` +
    `version - re-download never re-runs retrieval</span></div>`;
}

async function discoveryAction(btn) {
  const d = btn.dataset;
  const out = document.getElementById("dact-" + d.tid);
  const show = (html) => { if (out) out.innerHTML = html; };
  const qs = `candidate=${encodeURIComponent(d.tool)}` +
    `&target=${encodeURIComponent(d.tid)}`;
  try {
    if (d.dact === "bench") {
      const r = await api("POST", "/api/jobs", {
        kind: "benchmark",
        benchmark: {
          hypothesis: `${d.tool} versus ${d.base || "the configured tool"} ` +
            "changes the ranking metrics on this deployment's own " +
            "validation inputs",
          tools: { candidate: d.tool,
            baseline: d.base || "configured tool" },
          candidate_strategy: "evidence-consensus",
          metrics: ["top_k_overlap", "rank_correlation_spearman",
            "candidate_set_fraction"],
          dataset: { source: "demo_fixture" },
          compute_estimate: "single host, CPU minutes (fixture harness)",
          risks: ["fixture inputs are synthetic - results do not " +
            "establish real-world performance"] } });
      show(`<div class="mnote">Benchmark PROPOSED: job ${esc(r.job_id)} ` +
        `is blocked on approval ${esc(r.approval_id)} - it runs only if ` +
        `you approve the card (Approvals panel).</div>`);
      refresh();
    } else if (d.dact === "compat") {
      const j = await api("GET", `/api/research/${encodeURIComponent(d.rid)}` +
        `/compatibility?${qs}`);
      show(`<div class="card"><b>Compatibility check (read-only): ` +
        `${esc(j.candidate)}</b>` +
        `<div class="hint">${esc(j.interface)}</div><pre class="json">${
          esc(JSON.stringify(j, null, 1))}</pre></div>`);
    } else {
      const j = await api("GET", `/api/research/${encodeURIComponent(d.rid)}` +
        `/adoption-plan?${qs}`);
      show(`<div class="card"><b>Adoption plan - ${esc(j.candidate)} ` +
        `(${esc(j.target || "")})</b><span class="pill danger">not ` +
        `applied</span><div class="mnote">${esc(j.statement)}</div>` +
        `<ol>${(j.steps || []).map((s) => `<li>${esc(s)}</li>`).join("")}` +
        `</ol><div class="hint">${esc(j.verification_gate)}</div></div>`);
    }
  } catch (e) { toast("action failed: " + e.message, true); }
}

function reportHtmlLive(rep, rid) {
  // M6 B: conclusion-first view mirroring the HTML export - assessment,
  // module table, scientific comparison, actions; all technical detail
  // behind collapsed <details>.
  const kv = (o) => `<div class="kv">${Object.entries(o || {}).map(([k, v]) =>
    `<div class="k">${esc(k)}</div><div>${esc(
      typeof v === "object" ? JSON.stringify(v) : v)}</div>`).join("")}</div>`;
  const ul = (xs) => `<ul>${(xs || []).map((x) =>
    `<li>${esc(typeof x === "object" ? JSON.stringify(x) : x)}</li>`).join("")}</ul>`;
  const pill = (t, kind) => `<span class="pill ${kind || ""}">${esc(t)}</span>`;
  const a = rep.assessment || {};
  const ready = !!rep.decision_ready;
  const mtRows = (rep.module_table || []).map((m) => `<tr>
    <td><b>${esc(m.module)}</b></td>
    <td>${esc(m["current NeoAG method"])}</td>
    <td>${esc(m["best relevant alternative"])}</td>
    <td class="hint">${esc(m.evidence)}</td>
    <td>${esc(m.recommendation)}</td></tr>`).join("");
  const scards = (rep.scientific_comparison || []).map((t) => {
    const firstAlt = (t.rows || [])[0];
    const bl = t.baseline || {};
    const curName = (bl.tools || []).map((x) => x.name).join(", ") ||
      bl.note || "NOT identified";
    const dec = t.decision || {};
    const dKind = dec.recommendation === "Retain current tool" ? "ok"
      : ["Research incomplete", "Insufficient evidence"].includes(dec.recommendation) ? "danger" : "warn";
    const actions = firstAlt ? `
      <div class="btnrow">
        <button class="btn sm" data-dact="compat" data-rid="${esc(rid || "")}"
          data-tid="${esc(t.target_id)}" data-tool="${esc(firstAlt.tool)}">Check compatibility</button>
        <button class="btn sm" data-dact="plan" data-rid="${esc(rid || "")}"
          data-tid="${esc(t.target_id)}" data-tool="${esc(firstAlt.tool)}">Adoption plan</button>
        <button class="btn sm" data-dact="bench" data-tid="${esc(t.target_id)}"
          data-tool="${esc(firstAlt.tool)}" data-base="${esc((bl.tools || [])[0]?.name || "")}">Propose benchmark</button>
        <span class="hint">read-only checks; a proposed benchmark is a
        FORMAL job that waits for your approval before anything runs</span></div>
      <div id="dact-${esc(t.target_id)}"></div>` : "";
    const groupLine = (label, names) => (names || []).length
      ? `<div class="hint"><b>${esc(label)}:</b> ${esc((names || []).join(", "))}</div>` : "";
    return `
    <div class="card"><b>${esc(t.target)}</b>
    ${t.question ? `<div class="mnote"><b>Question:</b> ${esc(t.question)}</div>` : ""}
    <div class="hint"><b>Current NeoAG method:</b> ${esc(curName)}
      ${bl.consensus ? pill("multi-tool consensus", "ok") : ""}</div>
    ${(t.rows || []).length ? `<div class="tablewrap"><table class="data">
      <thead><tr><th>Alternative (direct)</th><th>Version</th>
      <th>Published performance (context-established)</th>
      <th>License · maintenance</th></tr></thead>
      <tbody>${(t.rows || []).map((r) => `<tr>
        <td><b>${esc(r.tool)}</b><div class="hint">${esc(r.why_comparable || "")}</div></td>
        <td>${esc(r.version)}</td>
        <td>${esc(r.evidence)}</td>
        <td>${esc(r.license)} · ${esc(r.maintenance)}</td></tr>`).join("")}</tbody>
    </table></div>` : `<div class="hint">No retrieved candidate passed the
      scientific relevance screen as a direct alternative for this
      module.</div>`}
    ${groupLine("Complementary methods", t.complementary)}
    ${groupLine("Wrappers / integrations", t.wrappers_integrations)}
    ${groupLine("Related but not comparable", t.related_not_comparable)}
    ${(t.excluded_irrelevant || []).length ? `<div class="hint"><b>Screened out as irrelevant:</b> ${esc((t.excluded_irrelevant || []).slice(0, 6).join(", "))} - reasons in the appendix</div>` : ""}
    ${dec.recommendation ? `<div>${pill(dec.recommendation, dKind)}
      <span class="hint">${esc(dec.rationale || "")}</span></div>` : ""}
    ${dec.action ? `<div class="mnote"><b>Action:</b> ${esc(dec.action)}</div>` : ""}
    <div class="hint">${esc(t.note || "")}</div>${actions}</div>`;
  }).join("");
  const cov = rep.coverage_accounting || {};
  const prov = rep.provenance || {};
  const rows = (rep.comparison || []).map((c) => `
    <tr><td><b>${esc(c.tool)}</b></td><td>${esc(c.target || "")}</td>
    <td>${esc(c.eligibility || "")}</td>
    <td>${esc(c.artifact_class || "unclassified")}</td>
    <td>${esc(c.verified_version || "Not verified")}</td>
    <td>${esc(c.license || "Not verified")}</td>
    <td>${esc(c.maintenance || "Not verified")}</td>
    <td class="hint">${esc(c.performance_summary || "")}</td></tr>`).join("");
  const exRows = (rep.excluded_candidates || []).map((c) => `<tr>
    <td><b>${esc(c.name)}</b></td><td>${esc(c.module || "")}</td>
    <td>${esc(c.eligibility || "")}</td>
    <td class="hint">${esc((c.reasons || []).join("; "))}</td>
    <td class="hint">${esc((c.origin || "").slice(0, 90))}</td></tr>`).join("");
  return `<div class="card">
    <div>${pill(ready ? "decision-ready review" : "PRELIMINARY RESEARCH",
                ready ? "ok" : "warn")}</div>
    <h3 class="sec">1. Executive assessment</h3>
    <p class="mnote"><b>${esc(a.headline || "")}</b></p>
    <div class="hint">${esc(a.scope || "")}</div>
    ${(a.top_findings || []).length ? `<b>Most important findings</b>${ul(a.top_findings)}` : ""}
    ${(a.evidence_gaps || []).length ? `<b>Evidence gaps</b>${ul(a.evidence_gaps)}` : ""}
    ${a.next_action ? `<div class="mnote"><b>Next recommended action:</b> ${esc(a.next_action)}</div>` : ""}
    <details class="adv"><summary>What this report is (and is not)</summary>
      <div class="mnote">${esc(rep.disclaimer)}</div></details>
    ${(rep.module_table || []).length ? `<h3 class="sec">2. Module-level
      recommendations</h3><div class="tablewrap"><table class="data">
      <thead><tr><th>Module</th><th>Current NeoAG method</th>
      <th>Best relevant alternative</th><th>Evidence</th>
      <th>Recommendation</th></tr></thead>
      <tbody>${mtRows}</tbody></table></div>` : ""}
    ${(rep.scientific_comparison || []).length ? `<h3 class="sec">3.
      Scientific comparison</h3>${scards}` : ""}
    ${(rep.recommended_next_actions || []).length ? `<h3 class="sec">4.
      Recommended actions (scientific priority)</h3>${ul(rep.recommended_next_actions)}` : ""}
    <details class="adv"><summary>5. Technical appendix (exclusions,
      full candidates, version chains, evidence records, coverage,
      gates, citations, provenance)</summary>
      <b class="hint">Excluded candidates - every retrieval that failed
      the scientific eligibility screen, with its reason</b>
      <div class="tablewrap"><table class="data"><thead><tr><th>Name</th>
      <th>Module</th><th>Class</th><th>Why excluded</th>
      <th>Origin</th></tr></thead><tbody>${exRows ||
        '<tr><td colspan="5" class="hint">none</td></tr>'}</tbody></table></div>
      <b class="hint">Full candidate comparison table</b>
      <div class="tablewrap"><table class="data"><thead><tr><th>Tool</th>
      <th>Module</th><th>Eligibility</th><th>Kind</th><th>Version</th>
      <th>License</th><th>Maintenance</th><th>Performance (context gate)</th>
      </tr></thead><tbody>${rows}</tbody></table></div>
      <b class="hint">Version verification chains</b>
      ${(rep.candidates || []).map((c) => `<div class="hint"><b>${esc(c.name)}</b>
        → ${esc(c.verified_version)} · installed: ${esc(c.installed_version || "n/a")}
        <ul>${((c.version_chain || {}).chain || []).map((s) =>
          `<li>${esc(s.step)}: ${esc(s.outcome)}${s.value ? " = " + esc(JSON.stringify(s.value)) : ""}${s.reason ? " (" + esc(s.reason) + ")" : ""} [${esc(String(s.checked_at || "").slice(0, 10))}]</li>`).join("")}</ul>
      </div>`).join("")}
      <b class="hint">Coverage accounting</b>
      ${kv({ completeness: cov.completeness,
        identified_modules: cov.identified_modules,
        researched_modules: cov.researched_modules,
        sufficient_evidence: cov.sufficient_evidence,
        insufficient_evidence: cov.insufficient_evidence,
        not_researched: cov.not_researched, note: cov.note })}
      <b class="hint">Research quality gates</b>
      <ul>${(rep.quality_gates || []).map((g) => `<li>${g.passed ? "PASS" : "FAIL"} ${esc(g.check)} - ${esc(g.detail)}</li>`).join("")}</ul>
      <b class="hint">Sources and citations</b>
      <ul>${(rep.literature_citations || []).slice(0, 40).map((c) => `
        <li><b>${esc(c.title || "")}</b> ${esc(c.year || "")} -
        ${esc(c.source || "")} ${esc(c.url || c.id || "")}</li>`).join("")}</ul>
      <b class="hint">Methodology / limitations / provenance</b>
      <p class="hint">${esc(rep.methodology || "")}</p>
      ${ul(rep.limitations)}
      ${kv({ sources_consulted: prov.sources_consulted,
        query_counts: prov.query_counts, dedup_removed: prov.dedup_removed,
        github_budget: prov.github_budget_at_start,
        failures: prov.failures })}
      <p class="hint">queries persisted: ${(prov.queries || []).length}</p>
    </details>
  </div>`;
}

function reportHtmlExample(rep) {
  const rows = (rep.comparison || []).map((c) => `
    <tr><td><b>${esc(c.tool)}</b> ${esc(c.version || "")}</td>
    <td>${esc(c.availability_class)}</td><td>${esc(c.license)}</td>
    <td>${esc(c.evidence_quality)}</td>
    <td>${esc(c.accuracy_example || "")}</td>
    <td>${c.independently_benchmarked ? "YES" : "no"}</td>
    <td>${esc(c.notes || "")}</td></tr>`).join("");
  return `<div class="card">
    <div class="mnote warn">${esc(rep.disclaimer)}</div>
    <div class="tablewrap"><table class="data">
      <thead><tr><th>Tool</th><th>Availability</th><th>License</th>
      <th>Evidence</th><th>Example figures</th><th>Independently benchmarked</th>
      <th>Notes</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="hint">${esc(rep.recommendation.verdict)}</p>
    <p class="hint">Pre-adoption experiments: ${esc(rep.recommendation.experiments_before_adoption.join(" | "))}</p>
    </div>`;
}

async function showReport(id) {
  state.discOpen = id;
  try {
    const r = await api("GET", `/api/research/${id}`);
    if (!r.report) {
      const prog = r.progress || {};
      $("res-report").innerHTML = `<div class="card">
        <div class="jr-top"><span class="jr-id">${esc(r.id)}</span>
        <span class="pill accent">${esc(r.status)}</span></div>
        ${r.progress?.steps?.length ? `<ul>${r.progress.steps.map((s) =>
          `<li class="hint">${esc(s)}</li>`).join("")}</ul>`
          : `<p class="hint">no persisted progress yet</p>`}
        ${["running", "queued"].includes(r.status)
          ? `<div class="btnrow"><button class="btn danger sm" id="rep-cancel">Cancel run</button>
             <span class="hint">stops after the current query; progress is kept</span></div>` : ""}
        ${["incomplete", "failed", "cancelled"].includes(r.status)
          ? `<div class="btnrow"><button class="btn sm" id="rep-resume">Resume (persisted queries skipped)</button></div>` : ""}
        ${["incomplete", "failed"].includes(r.status)
          ? `<div class="mnote warn">This run did not finish; nothing here
             is a complete review. Resume or accept the partial result.</div>` : ""}
      </div>`;
      const cb = $("rep-cancel");
      if (cb) cb.addEventListener("click", async () => {
        try { await api("POST", `/api/research/${id}/cancel`, {}); refreshDiscovery(); }
        catch (e) { toast("cancel failed: " + e.message, true); } });
      const rb = $("rep-resume");
      if (rb) rb.addEventListener("click", async () => {
        try { await api("POST", `/api/research/${id}/resume`, {}); refreshDiscovery(); }
        catch (e) { toast("resume failed: " + e.message, true); } });
      return;
    }
    const rep = r.report;
    const isLive = (rep.engine || "").includes("live") ||
      !!rep.inventory;
    $("res-report").innerHTML = exportBar(r.id) +
      (isLive ? reportHtmlLive(rep, r.id) : reportHtmlExample(rep));
    document.querySelectorAll("[data-dact]").forEach((b) =>
      b.addEventListener("click", () => discoveryAction(b)));
  } catch (e) { toast("report failed: " + e.message, true); }
}

$("res-run").addEventListener("click", async () => {
  const scope = $("res-scope").value;
  const engine = $("res-engine").value;
  const targets = [...document.querySelectorAll("[data-dtarget]:checked")]
    .map((c) => c.dataset.dtarget);
  const custom = $("res-custom").value.trim()
    ? $("res-custom").value.trim().split(/\s*,\s*/) : [];
  if (scope === "targeted" && !targets.length && !custom.length) {
    toast("pick at least one target (or a custom one) for a targeted review", true);
    return;
  }
  try {
    const r = await api("POST", "/api/research",
                        { scope, engine, targets, custom });
    toast(`${engine} research ${r.research_id} started` +
      (r.unknown_targets?.length
        ? ` - ignored unknown targets: ${r.unknown_targets.join(", ")}` : ""));
    state.discOpen = r.research_id;
    $("res-custom").value = "";
    await refreshDiscovery();
  } catch (e) {
    toast("research failed: " + (e.data?.error || e.message), true);
  }
});
$("res-scope").addEventListener("change", renderDiscoveryControls);
$("res-engine").addEventListener("change", () => {
  state.discSel = {};
  renderDiscoveryControls();
});

async function refreshDiscovery() {
  try {
    const b = await api("GET", "/api/bootstrap");
    state.boot.research = b.research;
    renderDiscovery();
    if (state.discOpen) showReport(state.discOpen);
  } catch (e) { /* transient */ }
}

/* ---------------- skills (advisory instructions, M7 §5) ---------------
 * Normal UI = name + instructions only. One Save validates, records a
 * version and applies the skill for new chats; slug/category/frontmatter
 * and deploy are generated or pinned by the backend. Version history and
 * raw text live in a collapsed "Advanced" fold; rollback is there too. */
function stripFrontmatter(t) {
  const m = /^---\r?\n[\s\S]*?\r?\n---\r?\n?/.exec(t || "");
  return m ? t.slice(m[0].length).replace(/^\s+/, "") : (t || "");
}

function renderSkills() {
  const skills = state.boot.skills || [];
  $("skill-list").innerHTML = skills.map((s) => `
    <div class="job-row skill-row" data-skill="${esc(s.slug)}">
      <div class="jr-top">
        <span class="jr-id">${esc(s.title || s.slug)}</span>
        <span class="pill ${s.enabled ? "ok" : "muted"}">${s.enabled ? "enabled" : "disabled"}</span>
        <span class="pill ${s.active_in_new_sessions ? "accent" : "warn"}">${
          !s.enabled ? "not loaded"
          : s.active_in_new_sessions ? "applies to new chats"
          : "saved - not applied yet"}</span>
      </div>
      <div class="jr-meta hint">${esc(s.description || "")}</div>
      <div class="btnrow">
        <button class="btn sm" data-sedit="${esc(s.slug)}">Edit</button>
        <button class="btn sm ${s.enabled ? "danger" : ""}" data-stog="${esc(s.slug)}"
          data-on="${s.enabled ? "0" : "1"}">${s.enabled ? "Disable" : "Enable"}</button>
      </div>
      <details class="skill-adv">
        <summary>Advanced (version history, raw text)</summary>
        <div class="btnrow">
          <button class="btn sm ghost" data-svers="${esc(s.slug)}">Version history</button>
          <button class="btn sm ghost" data-sraw="${esc(s.slug)}">Raw SKILL.md</button>
        </div>
        <div class="skill-versions hidden" id="sv-${esc(s.slug)}"></div>
        <pre class="skill-raw hidden" id="raw-${esc(s.slug)}"></pre>
      </details>
    </div>`).join("") ||
    `<div class="empty">No skills yet.</div>`;
  const on = (attr, fn) => document.querySelectorAll(`[data-${attr}]`)
    .forEach((el) => el.addEventListener("click", (ev) => {
      ev.stopPropagation(); fn(el); }));
  on("sedit", (el) => openSkillEditor(el.dataset.sedit));
  on("stog", async (el) => {
    try {
      const r = await api("POST", `/api/skills/${el.dataset.stog}/` +
        (el.dataset.on === "1" ? "enable" : "disable"), {});
      const st = (r.deploy || {}).state || "";
      toast(el.dataset.on === "1"
        ? `${el.dataset.stog}: enabled - ${st || "applies to new chats"}`
        : `${el.dataset.stog}: disabled - ${st || "will not load"}`);
      await refreshSkills();
    } catch (e) { toast("toggle failed: " + e.message, true); }
  });
  on("svers", async (el) => {
    const host = $(`sv-${el.dataset.svers}`);
    if (!host.classList.contains("hidden")) { host.classList.add("hidden"); return; }
    try {
      const d = await api("GET", `/api/skills/${el.dataset.svers}/versions`);
      host.innerHTML = (d.versions || []).map((v) => `
        <div class="tl-row"><span class="tl-ts">${esc(v.created_at || "")}</span>
        <span>v${esc(v.version)} - ${esc(v.source || "")}</span>
        <button class="btn sm ghost" data-srestore="${esc(el.dataset.svers)}"
          data-ver="${esc(v.version)}">restore</button></div>`).join("");
      host.classList.remove("hidden");
      host.querySelectorAll("[data-srestore]").forEach((b) =>
        b.addEventListener("click", async () => {
          try {
            await api("POST", `/api/skills/${b.dataset.srestore}/restore`,
                      { version: parseInt(b.dataset.ver, 10) });
            toast(`restored v${b.dataset.ver} as a new version - applies to new chats`);
            await refreshSkills();
          } catch (e) { toast("restore failed: " + e.message, true); }
        }));
    } catch (e) { toast("versions failed: " + e.message, true); }
  });
  on("sraw", async (el) => {
    const host = $(`raw-${el.dataset.sraw}`);
    if (!host.classList.contains("hidden")) { host.classList.add("hidden"); return; }
    try {
      const d = await api("GET", `/api/skills/${el.dataset.sraw}`);
      host.textContent = d.content;
      host.classList.remove("hidden");
    } catch (e) { toast("cannot load raw text: " + e.message, true); }
  });
}

async function refreshSkills() {
  try {
    const d = await api("GET", "/api/skills");
    state.boot.skills = d.skills;
    renderSkills();
  } catch (e) { /* transient */ }
}

function openSkillEditor(slug) {
  const host = $("skill-editor");
  const fill = (s) => {
    host.innerHTML = `<div class="card">
      <div class="card-head"><h2>${s ? "Edit skill" : "New skill"}</h2></div>
      <label class="lbl">Name</label>
      <input id="sk-name" value="${esc(s ? s.title : "")}"
        placeholder="What is this skill called?">
      <label class="lbl">Instructions</label>
      <textarea id="sk-body" rows="14" spellcheck="false"
        placeholder="What should the agent know or do differently? Write it in plain language - the agent reads this at the start of each new chat.">${esc(s ? stripFrontmatter(s.content) : "")}</textarea>
      <div class="btnrow">
        <button class="btn primary sm" id="sk-save">Save</button>
        <button class="btn ghost sm" id="sk-cancel">Cancel</button>
        <span class="hint">Changes apply to new chats.</span>
      </div>
      <div id="sk-err" class="hint"></div></div>`;
    $("sk-cancel").addEventListener("click", () => { host.innerHTML = ""; });
    $("sk-save").addEventListener("click", async () => {
      const body = { name: $("sk-name").value.trim(),
                     instructions: $("sk-body").value };
      try {
        const r = await api("POST",
                            s ? `/api/skills/${s.slug}/update` : "/api/skills",
                            body);
        const st = (r.deploy || {}).state || "";
        if (r.applied) {
          toast(`saved v${r.skill.version} - changes apply to new chats`);
        } else if (st.startsWith("disabled")) {
          toast("saved - but the skill is disabled, so new chats will not " +
                "load it. Press Enable to apply.", true);
        } else {
          toast("saved, but applying failed: " + st + " " +
                ((r.deploy || {}).error || ""), true);
        }
        host.innerHTML = "";
        await refreshSkills();
      } catch (e) {
        $("sk-err").textContent =
          (e.data?.errors || [e.data?.error || e.message]).join("; ");
      }
    });
  };
  if (!slug) return fill(null);
  api("GET", `/api/skills/${slug}`).then(fill)
    .catch((e) => toast("cannot load skill: " + e.message, true));
}
$("skill-new").addEventListener("click", () => openSkillEditor(null));

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
/* M6 PART F5: a small in-page dropdown replaces the native confirm().
   It states what each mode means BEFORE choosing, marks the current
   one, and refuses (server-enforced, not just advisory) while a chat
   turn or a job is in flight. */
$("agent-mode").addEventListener("click", (ev) => {
  ev.stopPropagation();
  const open = document.querySelector(".mode-menu");
  if (open) { open.remove(); return; }
  const m = document.createElement("div");
  m.className = "conv-menu mode-menu";
  const cur = state.agentMode;
  m.innerHTML = `
    <button data-mode="mock"${cur === "mock" ? ' class="cur"' : ""}>
      Demo agent (scripted)</button>
    <button data-mode="real"${cur === "real" ? ' class="cur"' : ""}>
      Real CodeBuddy agent</button>
    <div class="mm-note">Real mode sends your chat to the server-side
      CodeBuddy gateway: ordinary reads and workspace work execute
      directly (logged); destructive or out-of-workspace operations
      pause until you approve a confirmation card.</div>
    <div class="mm-note">Switching is blocked while a chat turn or a
      job is in flight.</div>`;
  document.body.appendChild(m);
  const r = $("agent-mode").getBoundingClientRect();
  m.style.top = (r.bottom + 4) + "px";
  m.style.left = Math.max(6, Math.min(r.left, window.innerWidth - 280)) + "px";
  m.addEventListener("click", async (ev2) => {
    const mode = (ev2.target.closest("[data-mode]") || {}).dataset
      && ev2.target.closest("[data-mode]").dataset.mode;
    if (!mode || mode === cur) { m.remove(); return; }
    try {
      await api("POST", "/api/agent/mode", { mode });
      m.remove();
      state.agentMode = mode;
      renderAgentBadge();
      toast(mode === "real"
        ? "real CodeBuddy agent connected - gated execution, card-based confirmations"
        : "switched to the scripted demo agent");
    } catch (e) { m.remove(); toast("mode switch failed: " + e.message, true); }
  });
  setTimeout(() => document.addEventListener("click", function h(ev3) {
    if (!ev3.target.closest(".mode-menu")
        && !ev3.target.closest("#agent-mode")) {
      m.remove();
      document.removeEventListener("click", h);
    }
  }), 0);
});

/* ---------------- approvals + decisions ---------------- */
document.addEventListener("click", async (ev) => {
  const appr = ev.target.closest("[data-appr]");
  if (appr) {
    try {
      const dec = appr.dataset.dec;
      const body = dec
        ? { decision: dec }
        : { approve: appr.dataset.ok === "1" };
      if (dec === "limit_scope") {
        const s = window.prompt(
          "What scope will you allow? (e.g. 'subsample 5 of 200 " +
          "samples; CPU only; no GPU')");
        if (!s) return;           // user backed out - card stays pending
        body.scope = s;
      }
      await api("POST", `/api/approvals/${appr.dataset.appr}`, body);
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
