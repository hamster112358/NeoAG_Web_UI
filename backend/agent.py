"""Agent layer with two transports (see POST /api/agent/mode, audited):

 - mock (default): scripted demo agent - no LLM is called at all.
 - real: the genuine server-side CodeBuddy agent via the gateway Jobs
   API, one live job per conversation, executed inside the dedicated
   agent workspace /data1/neoag/agents/neoag_agent_ws. Ordinary reads,
   diagnostics and workspace writes run WITHOUT approval prompts; the
   workspace's PreToolUse gate (verified live) blocks destructive or
   out-of-workspace operations until a human approves the confirmation
   card, at which point this layer writes a single-use, hash-matched
   grant and tells the agent to retry.

Channel separation, per project requirements:
 - role="agent"      -> agent suggestion   (badge: "agent suggestion")
 - role="policy"     -> validated NeoAG policy text, quoted from policies.py
                        (badge: "validated policy", cites its source)
The scripted agent never invents policy, never claims real computation
happened, and never references anything but synthetic DEMO- data.
Honesty about simulated functionality lives in the header indicator and
artifact badges - NOT in repeated chat banners.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from . import mock_data
from .policies import POLICY_BY_ID
from .store import new_id

AGENT_WS = "/data1/neoag/agents/neoag_agent_ws"
APPROVALS_DIR = AGENT_WS + "/.neoag-approvals"
# NEOAG_TASKQ_DIR isolates the deployed task-query helper for
# verification batteries (fresh DB => fresh token; the live workspace
# token for the :8712 server must not be overwritten).
TASKQ_DIR = os.environ.get("NEOAG_TASKQ_DIR", AGENT_WS + "/.neoag")
CONFIRM_RE = re.compile(
    r"NEOAG_CONFIRM_REQUIRED op=([0-9a-f]{24}) :: (.+?) "
    r"This operation is classified", re.S)

# Official task-query helper (B1 + Milestone 5 routing): the agent
# queries the SAME backend records the UI shows, through the API,
# instead of ad-hoc SQL or chat memory; it is also the ONLY sanctioned
# way for the agent shell to register a formal run (job submit). A
# dedicated settings-backed token grants GET on the query routes plus
# POST /api/jobs only - everything else stays Basic-auth. The token is
# deployed into the workspace next to the helper.
TASKQUERY_SRC = '''#!/usr/bin/env python3
"""NeoAG Web official task-query helper.

Endpoints are the interface's OWN public query API; the token file
next to this script grants GET access to task/research routes plus
exactly ONE write: registering a FORMAL run via `job submit` (POST
/api/jobs only - never chat, never settings, never secrets). The
persisted backend records - not chat memory - are authoritative for
what has run.

Formal NeoAG executions (open-neo run --mode execute, neoag.cli run/
run-full/run-demo, gateway posts) are BLOCKED by the execution gate
in this shell; that is by design. Register them here instead - the
job then appears as the same task in the interface and starts only
after the human approval card.

usage:
  taskquery.py list [n]          recent formal tasks
  taskquery.py get <job_id>      full detail incl. artifact manifest,
                                 decision history, failure info
  taskquery.py snapshot <job_id> immutable per-run config snapshot
  taskquery.py active [conv_id]  tasks still active (optionally for a
                                 conversation - resolve 'the run I just
                                 did' through this, and ask ONE
                                 clarifying question if several match)
  taskquery.py research [n]      discovery/research report index
  taskquery.py research get <id> full report incl. provenance/coverage
  taskquery.py job submit '<json>'  register a formal run (blocked
                                 until the user approves the card).
                                 JSON body, e.g.
                                 {"sample_id": "DEMO-P001"} - mock engine;
                                 {"engine": "native",
                                  "native_profile": "demo",
                                  "sample_id": "NATIVE-1"} - safe
                                 synthetic native run;
                                 {"engine": "native",
                                  "native_profile": "gateway",
                                  "native_params": {"sample_id": "...",
                                    "somatic_vcf": "...",
                                    "hla_alleles": ["A*02:01"]}} - real
                                 native plan + execute via the gateway;
                                 {"kind": "benchmark", "benchmark": {
                                   "hypothesis": "...",
                                   "tools": {"candidate": "...",
                                             "baseline": "..."},
                                   "metrics": ["..."],
                                   "compute_estimate": "...",
                                   "software_installation": "none - ...",
                                   "modifies_neoag_installation": false,
                                   "dataset": {"source": "demo_fixture",
                                     "download_size": "0 B (...)",
                                     "destination": "..."}
                                   }} - independent benchmark consent
                                 request; EVERY field above is mandatory
                                 (external datasets also need
                                 dataset.id + consent_approval_id from an
                                 approved dataset consent card); the
                                 backend rejects incomplete briefs at
                                 submit and the job blocks until the
                                 user answers the card (approve / reject
                                 / limit scope / literature only).
  taskquery.py job status <id>   state/outcome of one registered job
  taskquery.py job events <id>   the job's event log
"""
import json, os, sys, urllib.error, urllib.request

BASE = os.environ.get("NEOAG_WEB_BASE", "http://127.0.0.1:8712")
HERE = os.path.dirname(os.path.abspath(__file__))


def _tok():
    return open(os.path.join(HERE, ".taskquery-token")).read().strip()


def _req(path, obj=None):
    req = urllib.request.Request(
        BASE + path,
        data=None if obj is None else json.dumps(obj).encode(),
        method="GET" if obj is None else "POST",
        headers={"X-NeoAG-Task-Token": _tok(),
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path):
    return _req(path)


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 1
    cmd = a[0]
    try:
        if cmd == "list":
            n = a[1] if len(a) > 1 else "20"
            out = _get(f"/api/tasks?limit={n}")
        elif cmd == "get":
            out = _get("/api/tasks/" + a[1])
        elif cmd == "snapshot":
            out = _get(f"/api/tasks/{a[1]}/snapshot")
        elif cmd == "active":
            out = _get("/api/tasks/active" +
                       (f"?conv_id={a[1]}" if len(a) > 1 else ""))
        elif cmd == "research":
            if len(a) > 1 and a[1] == "get":
                out = _get("/api/research/" + a[2])
            else:
                out = _get("/api/research")
        elif cmd == "job" and len(a) > 2:
            if a[1] == "submit":
                out = _req("/api/jobs", json.loads(a[2]))
            elif a[1] == "status":
                out = _get("/api/tasks/" + a[2])
            elif a[1] == "events":
                out = _get("/api/tasks/" + a[2]).get("events", [])
            else:
                print(__doc__)
                return 1
        else:
            print(__doc__)
            return 1
    except urllib.error.HTTPError as e:
        print(json.dumps({"error": e.code,
                          "detail": e.read().decode("utf-8", "replace")
                          [:400]}))
        return 1
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def ensure_task_gateway(store) -> bool:
    """Generate (once) the task-query token and deploy the helper +
    token into the agent workspace. The helper is refreshed whenever
    the embedded source changes (content compare), so routing upgrades
    actually reach existing workspaces."""
    import secrets as _secrets
    tok = store.setting("task_token", "")
    if not tok:
        tok = _secrets.token_urlsafe(24)
        store.setting_set("task_token", tok)
    os.makedirs(TASKQ_DIR, exist_ok=True)
    helper = os.path.join(TASKQ_DIR, "taskquery.py")
    try:
        cur = open(helper).read()
    except OSError:
        cur = ""
    if cur != TASKQUERY_SRC:
        with open(helper, "w") as f:
            f.write(TASKQUERY_SRC)
        os.chmod(helper, 0o755)
    tokfile = os.path.join(TASKQ_DIR, ".taskquery-token")
    if not os.path.exists(tokfile) or \
            open(tokfile).read().strip() != tok:
        with open(tokfile, "w") as f:
            f.write(tok)
        os.chmod(tokfile, 0o600)
    return True


class DemoAgent:
    AGENT_WS = AGENT_WS  # surfaced in /api/codebuddy/status

    def __init__(self, store, jobs):
        self.store = store
        self.jobs = jobs
        self._client = None            # lazy CodeBuddyClient (real mode)
        # per-conversation real-mode state:
        # {conv_id: {"job": str|None, "busy": bool, "seen": set,
        #            "ops": set, "replies": {mid: text}}}
        self._conv: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # helpers shared by both transports
    # ------------------------------------------------------------------
    def _say(self, text: str, role: str = "agent", meta: dict | None = None,
             conv_id: str = "") -> dict:
        meta = dict(meta or {})
        meta.setdefault("badges", ["agent suggestion"] if role == "agent"
                        else ["validated policy"])
        return self.store.msg_add(role, text, meta, conv_id)

    def _policy(self, pol_id: str, conv_id: str = "") -> dict:
        p = POLICY_BY_ID[pol_id]
        return self.store.msg_add(
            "policy", p["text"],
            {"policy_id": pol_id, "source": p["source"],
             "badges": ["validated policy"]}, conv_id)

    def _cstate(self, conv_id: str) -> dict:
        with self._lock:
            return self._conv.setdefault(
                conv_id, {"job": None, "busy": False, "seen": set(),
                          "ops": set(), "replies": {}, "delivered": set(),
                          "kick": None, "stop": False})

    def stop_turn(self, conv_id: str) -> dict:
        """User-initiated interrupt of the CURRENT agent turn: set the
        cooperative flag (the relay stamps an honest terminal state) and
        ask the gateway to stop the worker. The session ends - the next
        message starts a fresh session rebuilt from saved history."""
        st = self._cstate(conv_id)
        job_id = st.get("job")
        out = {"ok": True, "interrupted": False, "gateway_stopped": False}
        if not st.get("busy"):
            out["note"] = ("no agent turn is running in this chat right "
                           "now (nothing to stop)")
            return out
        st["stop"] = True
        out["interrupted"] = True
        if job_id and self._client:
            try:
                r = self._client.stop(job_id)
                out["gateway_stopped"] = r.get("http") == 200
            except Exception as e:
                out["gateway_error"] = str(e)[:160]
        self.store.audit("user", "agent.turn.stop", conv_id,
                         {"job": job_id, **out})
        return out

    def active_work(self) -> dict:
        """M6 PART F5: what is genuinely in flight right now. An
        agent-mode switch consults this so it can never yank the engine
        out from under a running turn or a running job."""
        with self._lock:
            busy = sum(1 for st in self._conv.values() if st.get("busy"))
        return {"busy_chats": busy,
                "running_jobs": self.store.n_jobs_in_states(
                    ("running", "paused"))}

    @staticmethod
    def title_from(text: str) -> str:
        line = re.sub(r"\s+", " ", (text or "").strip())
        return (line[:47] + "...") if len(line) > 50 else (line or "New chat")

    # ------------------------------------------------------------------
    # REAL-agent integration (CodeBuddy gateway Jobs API). OFF by
    # default; the operator switches modes in the UI (POST
    # /api/agent/mode), audited. backend/codebuddy_client.py documents
    # the verified API; the gate + grant flow is verified end-to-end.
    # ------------------------------------------------------------------
    def _ensure_client(self):
        if self._client is None:
            from .codebuddy_client import CodeBuddyClient
            self._client = CodeBuddyClient()
        return self._client

    CHAT_PREAMBLE = (
        "You are the NeoAG assistant inside the NeoAG Web chat "
        "(the workspace rules in CODEBUDDY.md apply). Keep answers "
        "grounded: never invent files, numbers or results. For any "
        "question about analyses/tasks/reports this interface has "
        "produced, query the authoritative records via "
        "`python3 .neoag/taskquery.py` (never from chat memory); if "
        "several tasks could match 'the run I just did', ask ONE "
        "focused clarifying question naming the candidates. When "
        "something fails for an already-clear reason (missing file "
        "or tool, absent license, recorded gate denial), answer "
        "briefly with the reason and the next step instead of "
        "starting open-ended tool exploration. The "
        "user's message follows.\n\n")

    def handle_real(self, text: str, conv_id: str) -> list[dict]:
        """Route one user turn to the conversation's real agent job
        (dispatch or multi-turn reply), then relay the real transcript
        asynchronously ('chat' SSE events)."""
        try:
            client = self._ensure_client()
        except Exception as e:
            return [self._say(
                f"Real-agent mode is ON but the CodeBuddy gateway is not "
                f"reachable from this server ({e}). Nothing was executed.",
                meta={"badges": ["agent suggestion", "integration error"]},
                conv_id=conv_id)]
        st = self._cstate(conv_id)
        if st["busy"]:
            return [self._say(
                "The agent is still working on the previous message in "
                "this chat (one exchange at a time). Wait for its reply.",
                meta={"badges": ["real agent", "integration note"]},
                conv_id=conv_id)]
        conv = self.store.conv_get(conv_id) or {}
        job_id = conv.get("job_id")
        alive = False
        if job_id:
            d = client.job(job_id)
            alive = bool(d) and not d.get("unknown") and \
                d.get("state") not in (None, "stopped", "failed")
        had_prior_session = bool(job_id)
        try:
            if alive:
                r = client.reply(job_id, text)
                if not r.get("delivered"):
                    raise RuntimeError(f"reply failed: "
                                       f"{r.get('error') or r}")
                started = "continued"
            else:
                # durable-history recovery: rebuild context from saved
                # messages, then dispatch a fresh job. The "previous
                # session ended" notice is only honest when a real
                # agent session actually existed for this chat before.
                hist = [ln for ln in self.store.conv_recent_text(conv_id, 14)]
                ctx = ""
                if hist:
                    ctx = ("Rolling transcript of this conversation "
                           "(fresh agent session - the previous one ended; "
                           "saved history rides along):\n"
                           + "\n".join(f"- {h}" for h in hist) + "\n\n")
                try:
                    ensure_task_gateway(self.store)
                except Exception:
                    pass  # helper absence degrades, never blocks chat
                d = client.dispatch(self.CHAT_PREAMBLE + ctx + text,
                                    cwd=AGENT_WS,
                                    name="neoag-web-chat")
                job_id = d.get("id")
                self.store.conv_set_job(conv_id, job_id, "working")
                self.store.audit("agent", "real.dispatch", conv_id,
                                 {"job": job_id})
                started = "started"
        except Exception as e:
            self.store.conv_set_job(conv_id, None, "error")
            return [self._say(
                f"Could not reach the real agent: {e} Nothing was "
                "executed. (The gateway rate-limits dispatch; try again "
                "shortly.)",
                meta={"badges": ["agent suggestion", "integration error"]},
                conv_id=conv_id)]
        st["job"] = job_id
        st["busy"] = True
        st["stop"] = False
        msgs = []
        if started == "started" and had_prior_session:
            # honest ONLY for a re-dispatch after a real session ended -
            # never on a continued live session, never on a fresh chat
            msgs.append(self.store.msg_add(
                "system",
                "The previous agent session for this chat had ended; a "
                "new one was started from the saved conversation history. "
                "Earlier context is available, but the agent process "
                "itself is new.",
                {"badges": ["real agent", "integration note"]}, conv_id))
        kick = self.store.msg_add(
            "agent",
            f"Real agent {started} (job {str(job_id)[:8]}...). Its tool "
            "work executes on the server; routine operations run "
            "directly, destructive ones will appear here as a "
            "confirmation card before anything changes.",
            {"badges": ["real agent", "streaming"],
             "source": "codebuddy", "job_id": job_id,
             "turn_state": "streaming"}, conv_id)
        st["kick"] = kick["id"]
        msgs.append(kick)
        threading.Thread(target=self._turn, args=(conv_id,),
                         daemon=True).start()
        return msgs

    TURN_MAX_SECONDS = 900   # module-level for testability

    def _finish_turn(self, st: dict, turn_state: str) -> None:
        """Take the turn's kickoff notice out of 'streaming' into an
        honest terminal state (persisted, so reloads show it too)."""
        kid = st.get("kick")
        if not kid:
            return
        for m in self.store.msg_list(500):
            if m["id"] == kid:
                meta = m["meta"]
                meta["turn_state"] = turn_state
                meta["badges"] = [b for b in meta.get("badges", [])
                                  if b != "streaming"] + [turn_state]
                self.store.msg_set_meta(kid, meta)
                break
        st["kick"] = None

    def _turn(self, conv_id: str, max_seconds: int | None = None) -> None:
        """Relay one agent turn: post NEW tool activity live, create
        confirmation cards for gate CONFIRM denials, post the assistant
        reply when the turn settles. Content only from the gateway -
        never fabricated. ANY exit path ends in an honest terminal
        state (the kickoff notice leaves 'streaming'). Tool output is
        stored RAW (the frontend escapes once at render time, so no
        HTML entities leak into the UI)."""
        from .server import publish
        st = self._cstate(conv_id)
        client = self._client
        job_id = st["job"]
        if not job_id:
            self.store.msg_add(
                "agent", "(This chat has no agent job to relay yet - "
                "send a message first.)",
                {"badges": ["real agent", "integration note"]}, conv_id)
            st["busy"] = False
            return
        max_seconds = max_seconds or self.TURN_MAX_SECONDS
        t0 = time.time()
        errors = 0
        try:
            if not st["seen"]:
                # First relay since (re)start: replayed history of earlier
                # turns must NOT be re-posted as fresh tool activity.
                for u in client.transcript(job_id):
                    kind = str(u.get("sessionUpdate") or "")
                    if kind in ("tool_call", "tool_call_update"):
                        st["seen"].add(
                            f"{kind}|{u.get('toolCallId')}|{u.get('status')}")
            while time.time() - t0 < max_seconds:
                time.sleep(2.5)
                try:
                    detail = client.job(job_id)
                    items = client.transcript(job_id)
                    errors = 0
                except Exception as e:
                    # Transient gateway blip: tolerate a few, then be
                    # honest about losing the job (never fabricate).
                    errors += 1
                    if errors < 4:
                        continue
                    self.store.audit("agent", "relay.connection_lost",
                                     conv_id, {"job": job_id, "err": str(e)})
                    self.store.msg_add(
                        "agent",
                        f"(The connection to the agent job was lost "
                        f"after repeated gateway errors ({e}). The job "
                        "may still be working on the gateway; this "
                        "interface will not guess its outcome.)",
                        {"badges": ["real agent", "connection lost"],
                         "job_id": job_id}, conv_id)
                    self._finish_turn(st, "connection lost")
                    publish({"type": "chat", "conv_id": conv_id})
                    return
                state = detail.get("state")
                self.store.conv_touch(conv_id, job_state=str(state))
                if st.get("stop"):
                    # user pressed Stop: honest terminal, no fabricated tail
                    self.store.audit("agent", "relay.user_interrupt",
                                     conv_id, {"job": job_id})
                    self.store.msg_add(
                        "agent",
                        "(This turn was interrupted at your request. "
                        "Anything recorded above really happened; "
                        "nothing after this point did. The agent "
                        "session ends here - your next message starts "
                        "a fresh session rebuilt from the saved "
                        "conversation.)",
                        {"badges": ["real agent",
                                    "cancelled (user interrupt)"],
                         "job_id": job_id}, conv_id)
                    st["stop"] = False
                    self._finish_turn(st, "cancelled (user interrupt)")
                    publish({"type": "chat", "conv_id": conv_id})
                    return
                for u in items:
                    kind = str(u.get("sessionUpdate") or "")
                    if kind in ("tool_call", "tool_call_update"):
                        key = f"{kind}|{u.get('toolCallId')}|{u.get('status')}"
                        if key in st["seen"]:
                            continue
                        st["seen"].add(key)
                        title = str(u.get("title") or "tool")
                        status = str(u.get("status") or "")
                        meta = {"badges": ["tool output", "real agent"],
                                "job_id": job_id, "kind": "tool",
                                "tool_call_id": str(u.get("toolCallId")
                                                    or ""),
                                "tool_status": status,
                                "tool_title": title}
                        if kind == "tool_call":
                            # body stays the short running line; the UI
                            # renders this collapsed under the title
                            body = f"[running] {title}"
                        else:
                            raw = u.get("rawOutput")
                            if isinstance(raw, dict):
                                raw = raw.get("text") or json.dumps(
                                    raw, ensure_ascii=False)
                            # stored RAW: frontend escapes once at render
                            out = str(raw or "")[:600]
                            body = f"[tool {status}] {out}"
                            meta["tool_output"] = out
                            self._scan_gate_confirm(u, conv_id, st)
                        self.store.msg_add("tool", body, meta, conv_id)
                        publish({"type": "chat", "conv_id": conv_id})
                    elif kind == "agent_message_chunk":
                        mid = str(u.get("messageId") or "")
                        txt = (u.get("content") or {}).get("text") or ""
                        if mid and txt.strip():
                            st["replies"][mid] = txt
                if state in ("done", "failed", "stopped"):
                    # newest assistant message wins (transcript order);
                    # a message already relayed is never re-sent
                    mid, body = (list(st["replies"].items())[-1]
                                 if st["replies"] else ("", ""))
                    if state == "done" and body and \
                            mid not in st["delivered"]:
                        st["delivered"].add(mid)
                        self.store.msg_add(
                            "agent", body,
                            {"badges": ["real agent"],
                             "source": "codebuddy", "job_id": job_id},
                            conv_id)
                        self._finish_turn(st, "completed")
                    else:
                        live = str(detail.get("detail") or "")[:200]
                        label = {"done": "no reply recorded",
                                 "failed": "failed",
                                 "stopped": "cancelled"}[state]
                        self.store.msg_add(
                            "agent",
                            f"(Agent turn ended: {state}."
                            + (f" Gateway detail: {live}" if live else "")
                            + " No assistant reply text was recorded - "
                              "this interface does not fabricate one.)",
                            {"badges": ["real agent", "integration note"],
                             "job_id": job_id}, conv_id)
                        self._finish_turn(st, label)
                    publish({"type": "chat", "conv_id": conv_id})
                    return
            self.store.msg_add(
                "agent",
                f"(No terminal job state seen within {max_seconds}s; the "
                "gateway still holds the job. Send another message to "
                "continue, or check the operator's CodeBuddy Web UI.)",
                {"badges": ["real agent", "timed out"],
                 "job_id": job_id}, conv_id)
            self._finish_turn(st, "timed out")
            publish({"type": "chat", "conv_id": conv_id})
        except Exception as e:  # never leave a turn stuck streaming
            self.store.audit("agent", "relay.crashed", conv_id,
                             {"job": job_id, "err": repr(e)})
            try:
                self.store.msg_add(
                    "agent",
                    f"(The message relay for this turn stopped "
                    f"unexpectedly: {e}. Nothing was fabricated; check "
                    "the job status or send another message.)",
                    {"badges": ["real agent", "relay error"],
                     "job_id": job_id}, conv_id)
                self._finish_turn(st, "relay error")
                publish({"type": "chat", "conv_id": conv_id})
            except Exception:
                pass
        finally:
            st["busy"] = False

    def _scan_gate_confirm(self, upd: dict, conv_id: str, st: dict) -> None:
        """A gate CONFIRM denial in tool output => create a confirmation
        card (approval record) so the human decides."""
        out = str(upd.get("rawOutput") or "")
        if upd.get("status") == "failed" and "NEOAG_FORMAL_JOB_REQUIRED" in out:
            # auditable classification (Milestone 5 routing): the gate
            # intercepted a formal-run launch attempt. No approval path
            # exists by design; the agent must register via
            # taskquery.py job submit. Logged once per distinct attempt.
            import hashlib
            h = hashlib.sha256(out[:600].encode()).hexdigest()[:24]
            if h not in st["ops"]:
                st["ops"].add(h)
                self.store.audit("agent", "gate.formal_job_blocked",
                                 conv_id,
                                 {"op": h,
                                  "note": out[:300].replace("\n", " ")})
        m = CONFIRM_RE.search(out)
        if not m or upd.get("status") != "failed":
            return
        op, note = m.group(1), m.group(2)[:300]
        if op in st["ops"]:
            return
        st["ops"].add(op)
        from .server import publish
        # structured consent card (Milestone 5 PART 3): the gate writes
        # a sidecar for dataset-download confirmations so the card can
        # state dataset / operation / resource scope / destination /
        # job instead of one prose line.
        structured = {}
        try:
            with open(os.path.join(APPROVALS_DIR, "pending",
                                   op + ".json")) as sf:
                structured = json.load(sf)
        except Exception:
            structured = {}
        risk = "dataset" if structured.get("class") == \
            "dataset_download" else "destructive"
        detail = {"op": op, "conv_id": conv_id, "note": note,
                  "job_id": st["job"]}
        if structured:
            detail["structured"] = {
                "kind": structured.get("class"),
                "dataset": structured.get("dataset", ""),
                "operation": structured.get("operation", "download"),
                "resource_scope": ("this source -> this destination "
                                   "only, 72 h (bounded; no blanket "
                                   "downloads)"),
                "destination": structured.get("destination", ""),
                "job_id": st["job"] or ""}
        aid = new_id("apm")
        self.store.approval_create(
            aid, "", "gate_op", risk,
            json.dumps(detail, ensure_ascii=False))
        self.store.audit("gate", "gate.confirm_required", aid,
                         {"op": op, "conv": conv_id})
        publish({"type": "approval", "id": aid})

    def on_gate_decision(self, appr: dict, approve: bool,
                         decision: str = "", scope_note: str = "") -> None:
        """Human resolved a confirmation card. Approve => write the
        single-use grant (the hook consumes it on an identical retry)
        and tell the agent; deny => tell it to stand down.
        M6 PART C: limit_scope / literature_only arrive as refusals of
        the request AS PROPOSED - no grant is written; the agent gets
        the user's actual answer verbatim so it re-asks within the
        allowed scope or switches to a literature-only assessment."""
        decision = decision or ("approve" if approve else "reject")
        from .server import publish
        info = json.loads(appr.get("detail") or "{}")
        conv_id, op = info.get("conv_id", ""), info.get("op", "")
        st = self._cstate(conv_id)
        job_id = st["job"] or (self.store.conv_get(conv_id) or {}).get("job_id")
        if approve and op:
            import os
            os.makedirs(APPROVALS_DIR, exist_ok=True)
            f = os.path.join(APPROVALS_DIR, f"{op}.json")
            with open(f, "w") as fh:
                json.dump({"op": op, "granted_at": time.time(),
                           "approval_id": appr["id"]}, fh)
            os.chmod(f, 0o600)
            strd = info.get("structured") or {}
            if strd.get("kind") == "dataset_download":
                # BOUNDED scope grant (Milestone 5 PART 3): same source
                # -> same destination, 72 h, no blanket download permit.
                # This is what keeps a multi-file download from becoming
                # per-file prompting while still being consented, scoped
                # and time-limited.
                try:
                    sf = os.path.join(APPROVALS_DIR, "dataset-scope.json")
                    scopes = []
                    try:
                        with open(sf) as fh:
                            scopes = json.load(fh)
                        if not isinstance(scopes, list):
                            scopes = []
                    except Exception:
                        scopes = []
                    scopes = [s for s in scopes
                              if float(s.get("expires", 0)) > time.time()]
                    src = str(strd.get("dataset") or "")
                    dst = str(strd.get("destination") or "")
                    # the SCOPE is a directory root, never a single
                    # file: sibling files of the same approved download
                    # keep flowing (no per-file prompting), anything
                    # outside the root still asks.
                    root = dst.rstrip("/")
                    last = root.rsplit("/", 1)[-1]
                    if "/" in root and "." in last:
                        root = root.rsplit("/", 1)[0] or "/"
                    if src and dst:
                        scopes.append({
                            "dataset": src, "destination": dst,
                            "destination_root": root,
                            "granted_at": time.time(),
                            "expires": time.time() + 72 * 3600,
                            "approval_id": appr["id"]})
                        with open(sf, "w") as fh:
                            json.dump(scopes, fh)
                        os.chmod(sf, 0o600)
                        self.store.audit(
                            "user", "dataset.scope_granted", appr["id"],
                            {"dataset": src[:160], "destination": dst,
                             "ttl_hours": 72})
                except OSError:
                    pass
                try:
                    os.unlink(os.path.join(APPROVALS_DIR, "pending",
                                           op + ".json"))
                except OSError:
                    pass
        try:
            client = self._ensure_client()
            if job_id:
                msg = (
                    "The user APPROVED this exact operation on the "
                    "confirmation card. Retry the identical tool call "
                    "now - the approval unlocks it exactly once - then "
                    "report the outcome."
                    if approve else
                    ("The user LIMITS THE SCOPE of this request: "
                     + (scope_note[:300] or "(no detail given)")
                     + ". Do NOT retry the original command. Redo the "
                       "request within the allowed scope only, and ask "
                       "again on a fresh card."
                     if decision == "limit_scope" else
                     "The user requested a LITERATURE-ONLY assessment. "
                     "Do not download data and do not run any tool for "
                     "this question; deliver the comparison from the "
                     "retrieved literature and state its limits."
                     if decision == "literature_only" else
                     "The user DECLINED the operation on the confirmation "
                     "card. Do not retry it; briefly explain the safest "
                     "alternative."))
                r = client.reply(job_id, msg)
                if not r.get("delivered"):
                    raise RuntimeError(str(r)[:160])
            if not st["busy"]:
                st["busy"] = True
                st["job"] = job_id
                kick = self.store.msg_add(
                    "agent",
                    f"Acting on your decision (job {str(job_id)[:8]}...) - "
                    "relaying the agent's response.",
                    {"badges": ["real agent", "streaming"],
                     "source": "codebuddy", "job_id": job_id,
                     "turn_state": "streaming"}, conv_id)
                st["kick"] = kick["id"]
                threading.Thread(target=self._turn, args=(conv_id,),
                                 daemon=True).start()
        except Exception as e:
            self.store.msg_add(
                "agent",
                f"(Approval recorded, but the agent session could not be "
                f"reached to act on it: {e}. The grant, if issued, stays "
                "valid ~10 minutes for a matching retry.)",
                {"badges": ["real agent", "integration note"]}, conv_id)
        publish({"type": "chat", "conv_id": conv_id})

    # ------------------------------------------------------------------
    # Load Example (explicit user action; nothing demo-related appears
    # automatically in a fresh workspace)
    # ------------------------------------------------------------------
    def load_example(self, conv_id: str) -> list[dict]:
        out = [self._say(
            "DEMO-P001 (synthetic): HER2+ breast cancer case with "
            "tumor/normal WGS and tumor RNA in /demo/DEMO-P001/. "
            "Analyze it end to end.",
            role="user", meta={"badges": ["example"]}, conv_id=conv_id)]
        out.append(self._say(
            "Loaded a synthetic example case so you can explore the "
            "workspace flow. Everything below it is DEMO data (seeded "
            "mock engine). Ask me to prepare an analysis - in DEMO "
            "mode the job runs the scripted engine; the real agent "
            "performs the equivalent steps server-side when Real mode "
            "is on.", conv_id=conv_id))
        res = self.jobs.submit("DEMO-P001",
                               [s["id"] for s in mock_data.STAGES
                                if s["required"]], "auto",
                               conv_id=conv_id)
        out.append(self._say(
            f"Prepared demo analysis {res['job_id']} for DEMO-P001 - "
            "blocked on your approval (execute-class work always "
            "needs a human click).", conv_id=conv_id))
        return out

    # ------------------------------------------------------------------
    # Scripted (mock) agent - unchanged intents
    # ------------------------------------------------------------------
    def propose_manifest(self, description: str) -> dict:
        t = description.lower()
        sample = next((s["id"] for s in mock_data.SAMPLES
                       if s["id"].lower() in t or s["label"].lower() in t),
                      "DEMO-P001")
        has_rna = "no rna" not in t
        manifest = {
            "mock": True,
            "note": ("MOCK proposal - not derived from a real directory "
                     "scan. With the real CodeBuddy agent connected, the "
                     "same manifest shape is produced from actual "
                     "server-side inspection of the named directory."),
            "source_description": description[:400],
            "sample_id": sample,
            "inputs": [
                {"role": "tumor_dna_bam",
                 "path": f"/demo/{sample}/tumor_WGS.bam",
                 "confidence": 0.92,
                 "basis": "filename pattern + read-group sample tag (mock)"},
                {"role": "normal_dna_bam",
                 "path": f"/demo/{sample}/normal_WGS.bam",
                 "confidence": 0.88,
                 "basis": "paired normal from sample sheet (mock)"},
            ],
            "missing": [],
            "ambiguous": [],
        }
        if has_rna:
            manifest["inputs"].append(
                {"role": "tumor_rna_bam",
                 "path": f"/demo/{sample}/tumor_RNA.bam",
                 "confidence": 0.74,
                 "basis": "RNA-seq naming convention (mock)"})
        else:
            manifest["missing"].append(
                "tumor RNA input - fusion/splice stages would be blocked")
        manifest["ambiguous"].append(
            "two candidate normal BAMs share the sample prefix - confirm "
            "which run is the matched normal")
        return manifest

    def handle(self, text: str, conv_id: str = "") -> list[dict]:
        t = text.lower().strip()
        out: list[dict] = []

        if re.search(r"^\s*(hi|hello|hey|help|what can you)\b", t):
            out.append(self._say(
                "I can explain NeoAG stages and settings, propose an "
                "input manifest from a directory description, prepare a "
                "formal analysis, and walk results. In DEMO mode every "
                "job runs the scripted mock engine on synthetic data; "
                "switch the mode pill to reach the real server-side "
                "agent. Try: \"analyze DEMO-P001\", \"status\", \"show "
                "top candidates\", \"what rules does NeoAG enforce\", "
                "\"research fusion tools\".", conv_id=conv_id))
            return out

        if any(k in t for k in ("research", "discover", "sota", "alternativ",
                                "compare tool", "benchmark tool")):
            from .research import submit_research
            # same backend workflow as the structured Discovery UI (E3) -
            # the LIVE retrieval engine, not the example digest
            target_map = {"fusion": "fusion_discovery", "splice":
                          "splice_discovery", "hla": "hla_typing",
                          "stability": "presentation", "presentation":
                          "presentation"}
            hit = next(((v, k) for k, v in target_map.items() if k in t),
                       None)
            scope = "full" if not hit or "pipeline" in t else "targeted"
            try:
                res = submit_research(
                    self.store, scope,
                    [hit[0]] if scope == "targeted" else None,
                    conv_id=conv_id, actor="agent", engine="live")
            except Exception as e:
                out.append(self._say(
                    f"Could not start the live research task: {e}",
                    conv_id=conv_id))
                return out
            out.append(self._policy("POL-002", conv_id))
            out.append(self._say(
                f"Started a LIVE SOTA discovery task ({scope} scope, "
                f"{res['research_id']}) through the same Discovery "
                "workflow as the UI. It performs real retrieval now "
                "(literature + registries) and persists every query; "
                "unreachable sources will be reported as gaps, not "
                "filled in. Open the Discovery view for progress and "
                "the provenance log.",
                conv_id=conv_id))
            return out

        if any(k in t for k in ("decision history", "decisions",
                                "what did you decide")):
            ds = self.store.decision_list(50)
            if not ds:
                out.append(self._say("No decisions recorded yet.",
                                     conv_id=conv_id))
                return out
            pend = [d for d in ds if d["status"] == "pending"]
            lines = "\n".join(
                f"- {d['ts'][:16]} [{d['decided_by'] or 'PENDING'}] "
                f"{d['stage']}: {d['decision_type']} -> "
                f"{d['chosen'] or 'waiting'}" for d in ds[:5])
            out.append(self._say(
                f"Decision history ({len(ds)} recorded, {len(pend)} pending): \n"
                + lines + "\n\nFull detail (options, evidence, policy version) "
                "is in each job's Decision History view.", conv_id=conv_id))
            return out

        if any(k in t for k in ("rule", "policy", "policies", "enforce", "validated")):
            out.append(self._policy("POL-002", conv_id))
            out.append(self._policy("POL-001", conv_id))
            out.append(self._policy("POL-003", conv_id))
            out.append(self._say(
                "Those three are quoted policy rules from the NeoAG "
                "repository. Anything I add is a suggestion only.",
                conv_id=conv_id))
            return out

        if any(k in t for k in ("manifest", "input", "directory", "where are")):
            lines = "\n".join(
                f"- {s['id']}: {s['label']} - {s['status']}"
                for s in mock_data.SAMPLES)
            out.append(self._say(
                "Describe where your sequencing data live and I will "
                "propose an input manifest for review. Demo cases: \n"
                + lines + "\n\nDEMO-P002 has no RNA inputs - the fusion and "
                "splice stages would be blocked in a real run.",
                conv_id=conv_id))
            return out

        m = re.search(r"demo-p00[0-9]", t)
        sample = m.group(0).upper() if m else None
        if sample and any(k in t for k in ("run", "start", "analyz", "process", "submit")):
            settings = self.store.setting("ui", {})
            mode = settings.get("mode", "auto")
            if mode == "advanced" and settings.get("stages"):
                stage_ids = settings["stages"]
            else:
                stage_ids = [s["id"] for s in mock_data.STAGES if s["required"]]
            res = self.jobs.submit(sample, stage_ids, mode,
                                   simulate_failure=bool(settings.get(
                                       "simulate_failure")),
                                   conv_id=conv_id)
            out.append(self._say(
                f"I prepared a formal analysis for {sample} with "
                f"{len(stage_ids)} stages ({mode} settings). It is blocked on "
                "your approval - the agent cannot queue execute-class work "
                "without a human click (POL-005).", conv_id=conv_id))
            out.append(self._policy("POL-005", conv_id))
            return out

        if sample and any(k in t for k in ("why", "explain", "candidate", "result", "top")):
            return self._results("latest", conv_id)
        if any(k in t for k in ("result", "candidate", "top ", "top-", "ranking")):
            return self._results("latest", conv_id)

        if any(k in t for k in ("status", "progress", "running", "job")):
            jobs = self.store.job_list(5)
            if not jobs:
                out.append(self._say("No jobs yet. Try \"analyze DEMO-P001\" "
                                     "or Load Example.", conv_id=conv_id))
                return out
            lines = "\n".join(
                f"- {j['id']}: {j['state']}"
                + (f", {j['progress']}%, stage {j['stage']}" if j["stage"] else "")
                + (f" ({j['error']})" if j["error"] else "") for j in jobs)
            out.append(self._say("Latest jobs (live in the Tasks view):\n" + lines,
                                 conv_id=conv_id))
            return out

        if any(k in t for k in ("pause", "stop", "cancel", "resume")):
            out.append(self._say(
                "Use the pause / stop / resume buttons on the job card in "
                "the Tasks view - controls act on the job worker, not just "
                "the chat. Cancellation is cooperative at stage/substep "
                "boundaries.", conv_id=conv_id))
            return out

        if any(k in t for k in ("real", "pipeline", "connect", "production")):
            out.append(self._policy("POL-007", conv_id))
            out.append(self._say(
                "Real mode (mode pill, top-right) connects the chat to the "
                "genuine server-side CodeBuddy agent, which inspects, "
                "computes and diagnoses directly on this server. Formal "
                "pipeline execution through the mock engine stays clearly "
                "badged until the NeoAG controlled-execution gateway is "
                "wired.", conv_id=conv_id))
            return out

        out.append(self._say(
            "Demo agent here (scripted, no model). Try: \"analyze "
            "DEMO-P001\", \"what rules does NeoAG enforce\", \"status\", "
            "\"show top candidates\", \"what data do we have\". Or Load "
            "Example for a guided demo.", conv_id=conv_id))
        return out

    def _results(self, _which: str, conv_id: str = "") -> list[dict]:
        """Answer from the persisted OUTPUT manifest, not from memory:
        artifact statuses decide what can be called delivered."""
        mine = ([j for j in self.store.job_list(20, conv_id=conv_id)
                 if j["state"] == "completed"] if conv_id else [])
        done = mine or [j for j in self.store.job_list(20)
                        if j["state"] == "completed"]
        if not done:
            return [self._say(
                "No completed demo run yet - run \"analyze DEMO-P001\" and "
                "approve it in the Approvals panel.", conv_id=conv_id)]
        if len(done) > 1 and conv_id and not mine:
            return [self._say(
                "Several completed runs match. Which one: "
                + ", ".join(j["id"] for j in done[:3]) + "?",
                conv_id=conv_id)]
        job = done[0]
        result = json.loads(job["result_json"] or "{}")
        arts = self.store.artifact_list(job["id"])
        art_lines = [f"- {a['name']}: {a['status']}"
                     + (f" ({a['rows']} rows, checksum {a['checksum']})"
                        if a["status"] == "available" else "")
                     for a in arts]
        outcome = result.get("outcome", "unknown (pre-manifest run)")
        rows = (result.get("candidates") or [])[:3]
        lines = "\n".join(
            f"- #{r['rank']} {r['peptide']} ({r['gene']}, {r['hla']}, "
            f"{r['branch']}): mock score {r['mock_combined_score']}"
            for r in rows)
        body = (f"Latest completed run {job['id']} ({job['sample_id']}) - "
                f"outcome: {outcome}.\nOutput manifest:\n"
                + ("\n".join(art_lines) or "- no artifacts registered")
                + ("\n\nTop rows from the primary table ("
                   + str(result.get("candidates_strategy"))
                   + f"):\n{lines}" if rows else
                   "\n\nNo candidate rows are available - empty results "
                   "are reported as empty."))
        return [
            self._policy("POL-002", conv_id),
            self._say(
                body + "\n\nOpen the Results view for full evidence "
                "columns. These are synthetic demo numbers, not "
                "predictions about anything real.", conv_id=conv_id),
        ]
