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

# NEOAG_AGENT_WS relocates the agent workspace (and therefore the
# grant/scope-grant files the gate reads) for verification batteries;
# the live server keeps the default path untouched.
AGENT_WS = os.environ.get("NEOAG_AGENT_WS",
                          "/data1/neoag/agents/neoag_agent_ws")
APPROVALS_DIR = AGENT_WS + "/.neoag-approvals"
# "always allow within this scope" grants (M11 P0-2): narrow, dated,
# revocable; enforced by gate.py itself, not by the UI hiding cards.
SCOPE_GRANTS_FILE = "scope-grants.json"
SCOPE_TTL_SECONDS = 4 * 3600
# M11 P0-4: tool outputs are stored COMPLETE in the execution record
# (the old [:600] cap silently deleted evidence). This bound exists
# only so one pathological output cannot bloat the store unbounded;
# if it ever engages it leaves an explicit marker + the true size.
TOOL_OUTPUT_STORE_MAX = 200_000
# NEOAG_TASKQ_DIR isolates the deployed task-query helper for
# verification batteries (fresh DB => fresh token; the live workspace
# token for the :8712 server must not be overwritten).
TASKQ_DIR = os.environ.get("NEOAG_TASKQ_DIR", AGENT_WS + "/.neoag")
CONFIRM_RE = re.compile(
    r"NEOAG_CONFIRM_REQUIRED op=([0-9a-f]{24}) :: (.+?) "
    r"This operation is classified", re.S)

# M10 §10.1: an approval request must say WHICH operation, against
# WHICH target, and what its consequences are. These fields are
# DERIVED from the recorded command (a factual parse), never invented:
# where a claim cannot be made confidently the text says so.
_ABS_PATH_RE = re.compile(r"(?<![\w~.-])(/[A-Za-z0-9@._+-]+(?:/[A-Za-z0-9@._+-]*)+)")
_DESTRUCTIVE_RE = re.compile(r"(^|[\s;&|])(rm|rmdir|unlink|shred|truncate|dd|mkfs|wipefs|chown\s+-R|chmod\s+-R)(\s|$)")
_OVERWRITE_RE = re.compile(r"(^|[^0-9>])(>{1,2}\s*/|\btee\b\s+/|\bmv\b\s+/|\bdd\b[^|]*of=/)")


def gate_consequence(note: str) -> dict:
    """Conservative, derived consequence fields for a gate card."""
    targets = []
    for p in _ABS_PATH_RE.findall(note or ""):
        if p not in targets:
            targets.append(p)
    if _DESTRUCTIVE_RE.search(note or ""):
        kind = "delete"
        consequence = ("deletes or irreversibly overwrites its target(s) "
                       "- there is no undo from inside this interface")
    elif _OVERWRITE_RE.search(note or ""):
        kind = "overwrite"
        consequence = ("writes over existing file content at its "
                       "target(s); prior content may be lost")
    else:
        kind = "write-or-run"
        consequence = ("changes filesystem state or starts a process "
                       "outside the always-allowed workspace baseline - "
                       "reversibility depends on the exact command shown")
    return {"target_paths": targets[:8], "effect_kind": kind,
            "consequence": consequence}


_NETWORK_RE = re.compile(
    r"(^|[\s;&|])(curl|wget|scp|rsync|sftp|ftp|nc)(\s|$)")


def effect_classes(note: str, structured: dict) -> list[str]:
    """M11 P0-2: read/write/delete/network classification derived from
    the recorded command text. Conservative: anything not confidently
    parsed is reported as UNDETERMINED, never as safe."""
    if structured.get("class") == "dataset_download":
        return ["network-read"]
    out = []
    n = note or ""
    if _DESTRUCTIVE_RE.search(n):
        out.append("delete")
    if _OVERWRITE_RE.search(n):
        out.append("overwrite")
    if _NETWORK_RE.search(n):
        out.append("network")
    if not out:
        out = ["write-or-execute" if _ABS_PATH_RE.search(n)
               else "undetermined"]
    return out


def scope_roots_for(detail: dict) -> list[str]:
    """Narrow roots for an 'always allow within this scope' grant,
    DERIVED from the recorded target paths: the parent directory of
    each written target. Refuses anything broad (< 3 path segments,
    or more than 3 distinct roots) - a grant that would effectively
    cover the filesystem is not a scope."""
    roots = []
    for p in detail.get("target_paths") or []:
        parent = str(p).rsplit("/", 1)[0] or "/"
        if parent not in roots:
            roots.append(parent)
    if not roots or len(roots) > 3:
        return []
    for r in roots:
        if r.count("/") < 3:      # e.g. /tmp, /data1 - too broad
            return []
    return roots

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
                          "kick": None, "stop": False, "conv": conv_id,
                          "activity": "",
                          # M7 §6 delivery repair: what this turn must
                          # deliver, and whether it already got its one
                          # automatic session-restore
                          "text": "", "recovered": False})

    def reattach_turns(self) -> list[str]:
        """M11 P0-3: gateway jobs outlive this process. A turn that was
        streaming when the Web server stopped is not automatically
        lost: if the gateway still knows the job, re-attach a relay -
        events already recorded are not replayed (the seen-cursor is
        re-seeded from the transcript), and a final reply the job
        produced while this server was down is delivered on the first
        poll. Only genuinely gone jobs are left for the startup sweep
        to stamp interrupted."""
        reattached = []
        for conv in self.store.conv_list():
            if conv.get("turn_state") != "streaming":
                continue
            cid, job_id = conv["id"], conv.get("job_id") or ""
            if not job_id:
                continue
            st = self._cstate(cid)
            if st["busy"]:
                continue
            try:
                client = self._ensure_client()
                detail = client.job(job_id)
            except Exception:
                continue    # unreachable gateway: the sweep is honest
            if detail.get("unknown"):
                continue
            state = str(detail.get("state") or "")
            if state in ("done", "failed", "stopped"):
                # deliver-once rule: if the final reply is already in
                # the conversation, nothing remains to relay
                newest = ""
                try:
                    for u in client.transcript(job_id):
                        if str(u.get("sessionUpdate") or "") == \
                                "agent_message_chunk":
                            txt = (u.get("content") or {}).get("text") or ""
                            if txt.strip():
                                newest = txt
                except Exception:
                    newest = ""
                rows = self.store.msg_list(50, conv_id=cid)
                if newest and any(
                        m["role"] == "agent"
                        and m["text"].strip() == newest.strip()
                        for m in rows):
                    self.store.conv_set_turn(cid, "completed", "")
                    continue
                # else: the reply landed while we were down - the
                # re-attached _turn relays it as substantive output
            st["job"] = job_id
            st["busy"] = True
            st["stop"] = False
            st["text"] = ""
            st["recovered"] = False
            st["kick"] = None
            try:
                for u in client.transcript(job_id):
                    kind = str(u.get("sessionUpdate") or "")
                    if kind in ("tool_call", "tool_call_update"):
                        st["seen"].add(
                            f"{kind}|{u.get('toolCallId')}"
                            f"|{u.get('status')}")
            except Exception:
                pass
            self.store.audit("agent", "relay.reattached", cid,
                             {"job": job_id, "gw_state": state})
            threading.Thread(target=self._turn, args=(cid,),
                             daemon=True).start()
            reattached.append(cid)
        return reattached

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

    # M11 P0-4 context restoration budget: at most this many characters
    # of rolling transcript ride along with a recovery dispatch. Older
    # turns are not dropped or silently clipped - they are marked
    # ABBREVIATED with their message id, and the FULL text of every
    # message is written to a workspace file the fresh session can read.
    RECOVERY_CTX_BUDGET = 16_000
    CONV_CTX_SUBDIR = ".neoag-chat"

    def _recovery_context(self, conv_id: str, hist: list[dict]) -> str:
        """Build the recovery context for a fresh agent session.

        Three parts, per the message-integrity requirement: durable
        message references (every line cites its stored message id),
        reliable history retrieval (full transcript file inside the
        agent workspace, written atomically, survives restarts), and
        explicit context budgeting (a line that had to be shortened
        here announces it instead of masquerading as the message)."""
        if not hist:
            return ""
        rel = ""
        try:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", conv_id) or "conv"
            d = os.path.join(AGENT_WS, self.CONV_CTX_SUBDIR, safe)
            os.makedirs(d, exist_ok=True)
            lines = ["# Saved NeoAG Web conversation " + conv_id, "",
                     "Every stored chat message, oldest first, in full.",
                     ""]
            for m in hist:
                lines += [f"## message {m['id']} ({m['role']}, "
                          f"{len(m['text'])} chars)", "",
                          m["text"], ""]
            tmp = os.path.join(d, "transcript.md.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            os.replace(tmp, os.path.join(d, "transcript.md"))
            rel = f"{self.CONV_CTX_SUBDIR}/{safe}/transcript.md"
        except Exception:
            rel = ""  # best-effort file copy; window below still labels itself
        out, used, trimming = [], 0, False
        for m in reversed(hist):
            full = f"[{m['role']} #{m['id']}]: {m['text']}"
            if trimming or used + len(full) > self.RECOVERY_CTX_BUDGET:
                trimming = True
                head = m["text"].replace("\n", " ")[:180]
                where = (f"read message {m['id']} in '{rel}'" if rel
                         else "full text is in the interface's message store")
                out.append(f"[{m['role']} #{m['id']} ABBREVIATED here - "
                           f"{len(m['text'])} chars stored; for the full "
                           f"text, {where}] {head}...")
            else:
                used += len(full) + 1
                out.append(full)
        out.reverse()
        hdr = ("Saved transcript of this conversation from the NeoAG Web "
               "interface (fresh agent session - the previous one ended). "
               "Lines marked ABBREVIATED are truncated previews, NOT the "
               "full message"
               + (f"; the complete text of every stored message is at "
                  f"'{rel}' in your workspace (one '## message <id>' "
                  "section per message) - read it before relying on "
                  "anything a preview cuts off" if rel else "")
               + ".\n\n")
        return hdr + "\n".join(out) + \
            "\n\nThe user's current message follows verbatim.\n\n"

    def handle_real(self, text: str, conv_id: str,
                    recovering: bool = False) -> list[dict]:
        """Route one user turn to the conversation's real agent job
        (dispatch or multi-turn reply), then relay the real transcript
        asynchronously ('chat' SSE events). recovering=True marks the
        single automatic re-dispatch _turn performs after a delivery was
        lost - it must not re-arm the recovery flag (no loops)."""
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
        # M7 §6 (delivered:false repair): "alive" is what the GATEWAY
        # reports, and it is the precondition for a reply. A settled
        # ("done") worker that is STILL ALIVE may legally be continued
        # (verified multi-turn behaviour), so the gate is the gateway's
        # alive boolean - not the turn state. A dead (alive=false) or
        # stopped/failed record must never be replied to: the gateway
        # would accept the text, nothing would process it, and the user
        # message would silently disappear. Those take the session-
        # restore path below.
        alive = False
        if job_id:
            d = client.job(job_id)
            alive = bool(d) and not d.get("unknown") and \
                bool(d.get("alive")) and \
                d.get("state") not in ("stopped", "failed")
        had_prior_session = bool(job_id)
        started = None
        try:
            if alive:
                r = client.reply(job_id, text)
                if r.get("delivered"):
                    started = "continued"
                else:
                    # worker died between the liveness check and the
                    # reply (or refuses it): fall through to session
                    # restore instead of surfacing a dead-end error
                    self.store.audit("agent", "real.reply_refused",
                                     conv_id,
                                     {"job": job_id,
                                      "err": str(r.get("error")
                                                 or r)[:200]})
            if started is None:
                # durable-history recovery (M11 P0-4): rebuild context
                # from the stored message rows themselves - durable
                # message references + a file-backed full transcript +
                # an explicitly budgeted recent window. Nothing is ever
                # silently shortened here: every abbreviated line says
                # so and cites its message id. The user's current
                # message is appended VERBATIM (codebuddy_client raises
                # over transport limits, it never slices). The
                # "previous session ended" notice is only honest when a
                # real agent session actually existed for this chat.
                hist = self.store.conv_transcript(conv_id)
                ctx = self._recovery_context(conv_id, hist)
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
                                 {"job": job_id,
                                  "recovery": had_prior_session})
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
        st["text"] = text
        if not recovering:
            st["recovered"] = False
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
        # M11 P0-1: the kickoff is EXECUTION bookkeeping (audit row),
        # the conversation itself stays clean; the live turn (state,
        # time, meaningful activity label) rides on the conversation
        # row and renders as the one compact indicator.
        kick = self.store.msg_add(
            "agent",
            f"Real agent {started} (job {str(job_id)[:8]}...). Its tool "
            "work executes on the server; routine operations run "
            "directly, destructive ones will appear here as a "
            "confirmation card before anything changes.",
            {"badges": ["real agent", "streaming"],
             "source": "codebuddy", "job_id": job_id,
             "turn_state": "streaming"}, conv_id, visibility="audit")
        st["kick"] = kick["id"]
        st["conv"] = conv_id
        self.store.conv_set_turn(conv_id, "streaming", "")
        threading.Thread(target=self._turn, args=(conv_id,),
                         daemon=True).start()
        return msgs

    # M11 P0-3: live turns are NOT bounded by a wall-clock budget.
    # The gateway job outlives any single relay loop; ending a turn at
    # 900s while the job kept running is the exact regression this
    # milestone was filed for (POLYSOLVER research turns legitimately
    # run longer than the old cap). A turn now ends only on honest
    # events: terminal job state, user cancel, lost connection, or a
    # relay crash. Explicit max_seconds remains a TEST seam - nothing
    # production passes it. STALL_NOTICE_SECONDS: one honest notice
    # (not a terminal state) when a live job produces nothing for
    # half an hour.
    TURN_MAX_SECONDS = 900       # test seam only
    STALL_NOTICE_SECONDS = 1800  # notice threshold; test-overridable

    def _finish_turn(self, st: dict, turn_state: str) -> None:
        """End the turn honestly: the CONVERSATION row (the live
        indicator's source) leaves 'streaming' for the terminal state,
        and the kickoff audit row's meta mirrors it (so the technical
        view stays truthful after reloads). Both writes persist, so a
        reload shows the same state."""
        cid = st.get("conv")
        if cid:
            self.store.conv_set_turn(cid, turn_state, "")
        kid = st.get("kick")
        if kid:
            m = self.store.msg_get(kid)
            if m:
                meta = m["meta"]
                meta["turn_state"] = turn_state
                meta["badges"] = [b for b in meta.get("badges", [])
                                  if b != "streaming"] + [turn_state]
                self.store.msg_set_meta(kid, meta)
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
        st["conv"] = conv_id
        client = self._client
        job_id = st["job"]
        if not job_id:
            self.store.msg_add(
                "agent", "(This chat has no agent job to relay yet - "
                "send a message first.)",
                {"badges": ["real agent", "integration note"]}, conv_id)
            st["busy"] = False
            return
        # max_seconds is None for every production turn: the relay
        # runs until the job itself reaches an outcome (M11 P0-3)
        t0 = time.time()
        errors = 0
        dead_ticks = 0
        last_move = t0
        stall_noted = False

        def _recover(reason: str) -> bool:
            """M7 §6: the reply never came back because the session died
            (deploy / idle reclaim / gateway restart). Repair by session
            restore EXACTLY ONCE per message - the user's message is
            already saved, so re-dispatching it loses nothing - instead
            of parking the chat on the turn timeout."""
            if st.get("recovered") or not st.get("text"):
                return False
            st["recovered"] = True
            self.store.audit("agent", "relay.session_lost", conv_id,
                             {"job": job_id, "why": reason})
            self._finish_turn(st, "session lost - recovering")
            st["busy"] = False
            try:
                self.handle_real(st["text"], conv_id, recovering=True)
            except Exception as e:  # dispatch failed: honest, no loop
                self.store.msg_add(
                    "agent",
                    f"(The agent session was lost ({reason}) and the "
                    f"automatic recovery could not start: {e}. Resend "
                    "your message to try again.)",
                    {"badges": ["real agent", "integration error"],
                     "job_id": job_id}, conv_id)
            publish({"type": "chat", "conv_id": conv_id})
            return True
        try:
            if not st["seen"]:
                # First relay since (re)start: replayed history of earlier
                # turns must NOT be re-posted as fresh tool activity.
                for u in client.transcript(job_id):
                    kind = str(u.get("sessionUpdate") or "")
                    if kind in ("tool_call", "tool_call_update"):
                        st["seen"].add(
                            f"{kind}|{u.get('toolCallId')}|{u.get('status')}")
            last_state = None
            while max_seconds is None or time.time() - t0 < max_seconds:
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
                if state != last_state:
                    last_state = state
                    last_move = time.time()
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
                    # the session this card belonged to is ended by the
                    # user - the card can no longer unlock anything
                    # (M11 P0-2: cancel invalidates stale approvals)
                    self.store.approval_invalidate_conv(conv_id)
                    publish({"type": "chat", "conv_id": conv_id})
                    return
                # zombie guard (M7 §6): the gateway no longer has a live
                # worker, yet the record still claims the job runs
                # (deploy / reclaim left it behind). Do not wait out the
                # turn timeout for events that can never arrive.
                if not detail.get("unknown") and \
                        not detail.get("alive") and \
                        state not in ("done", "failed", "stopped"):
                    dead_ticks += 1
                    if dead_ticks >= 4 and _recover(
                            f"worker gone while job still recorded "
                            f"'{state}'"):
                        return
                    if dead_ticks >= 8:
                        # worker stays gone and recovery already had
                        # its shot (or never applied): end honestly.
                        # This replaces the old wall-clock cap for
                        # the zombie case - live jobs still wait on
                        # real events only (M11 P0-3).
                        self.store.msg_add(
                            "agent",
                            "(The gateway worker for this job is gone "
                            "and automatic recovery does not apply to "
                            "this turn - it is ended without a "
                            "fabricated outcome. The recorded events "
                            "above remain the full truth.)",
                            {"badges": ["real agent", "worker gone"],
                             "job_id": job_id}, conv_id)
                        self._finish_turn(st, "worker gone - no reply")
                        publish({"type": "chat", "conv_id": conv_id})
                        return
                else:
                    dead_ticks = 0
                dirty = False
                for u in items:
                    kind = str(u.get("sessionUpdate") or "")
                    if kind in ("tool_call", "tool_call_update"):
                        key = f"{kind}|{u.get('toolCallId')}|{u.get('status')}"
                        if key in st["seen"]:
                            continue
                        st["seen"].add(key)
                        last_move = time.time()
                        title = str(u.get("title") or "tool")
                        status = str(u.get("status") or "")
                        meta = {"badges": ["tool output", "real agent"],
                                "job_id": job_id, "kind": "tool",
                                "tool_call_id": str(u.get("toolCallId")
                                                    or ""),
                                "tool_status": status,
                                "tool_title": title}
                        if kind == "tool_call":
                            # body stays the short running line; the
                            # technical view renders this collapsed
                            # under the title
                            body = f"[running] {title}"
                            # one compact live indicator: the meaningful
                            # activity label rides on the conversation
                            # (never counts in the default chat)
                            if title != st.get("activity"):
                                st["activity"] = title
                                self.store.conv_set_turn(
                                    conv_id, "streaming", title)
                        else:
                            raw = u.get("rawOutput")
                            if isinstance(raw, dict):
                                raw = raw.get("text") or json.dumps(
                                    raw, ensure_ascii=False)
                            # M11 P0-4: the execution record keeps the
                            # FULL tool output - the old silent [:600]
                            # cap deleted the evidence it claimed to
                            # preserve. The one-line body is a preview,
                            # marked as such; meta carries the complete
                            # text (bounded storage, with an explicit
                            # marker + original size if the cap ever
                            # engages).
                            full = str(raw or "")
                            stored = full
                            if len(full) > TOOL_OUTPUT_STORE_MAX:
                                stored = (
                                    full[:TOOL_OUTPUT_STORE_MAX]
                                    + f"\n[END OF STORED RECORD at "
                                      f"{TOOL_OUTPUT_STORE_MAX} chars: "
                                      f"{len(full)} chars were produced, "
                                      f"the tail is not stored]")
                            meta["tool_output"] = stored
                            meta["tool_output_chars"] = len(full)
                            if len(stored) != len(full):
                                meta["tool_output_truncated"] = True
                            out = (full if len(full) <= 600 else
                                   full[:600] +
                                   f" ... [preview of {len(full)} chars; "
                                   "full output in the record]")
                            body = f"[tool {status}] {out}"
                            self._scan_gate_confirm(u, conv_id, st)
                        # M11 P0-1: the full per-call execution record
                        # is persisted as an audit row - durable,
                        # complete, but NOT conversation noise
                        self.store.msg_add("tool", body, meta, conv_id,
                                           visibility="audit")
                        dirty = True
                    elif kind == "agent_message_chunk":
                        mid = str(u.get("messageId") or "")
                        txt = (u.get("content") or {}).get("text") or ""
                        if mid and txt.strip():
                            st["replies"][mid] = txt
                # M11 P0-5: publish at most ONE chat event per poll cycle,
                # not one per tool event. A busy 60-call turn used to fire
                # 60 SSE refetches every poll, each a full conversation
                # reload on every connected client; the data is identical
                # regardless of how many rows this batch wrote.
                if dirty:
                    publish({"type": "chat", "conv_id": conv_id})
                if (not stall_noted and
                        time.time() - last_move > self.STALL_NOTICE_SECONDS):
                    # M11 P0-3: an honest WAITING notice is not a fake
                    # outcome - the job is alive on the gateway, we just
                    # hear nothing. Say exactly that, once, keep waiting.
                    stall_noted = True
                    self.store.msg_add(
                        "agent",
                        "(The agent job is still running on the server, "
                        "but it has produced no new activity for "
                        f"{int(time.time() - last_move) // 60} minutes. "
                        "This interface keeps waiting rather than guess "
                        "an outcome; you can stop the turn at any time.)",
                        {"badges": ["real agent", "stall notice"],
                         "job_id": job_id}, conv_id)
                    publish({"type": "chat", "conv_id": conv_id})
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
                    elif state == "done" and _recover(
                            "job reached 'done' with no assistant reply"):
                        # the delivered:false failure mode: the gateway
                        # took our text, then died before answering -
                        # restore the session and retry the message once
                        # before declaring the turn lost. 'failed' and
                        # 'stopped' do NOT recover: those are recorded
                        # outcomes (a crashed job, or deliberate cancel
                        # from Tasks/R7-style stop) - re-dispatching
                        # them would duplicate work someone ended on
                        # purpose; the honest terminal below stands.
                        return
                    else:
                        _det = str(detail.get("detail") or "")
                        # shown inside a user-visible message: if it
                        # must be shortened (M11 P0-4) say so, never
                        # clip silently
                        live = (_det if len(_det) <= 200 else
                                _det[:200] + f"... [gateway detail "
                                f"trimmed from {len(_det)} chars]")
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
        # durable dedup (M11 P0-2): the same operation already has a
        # live card - possibly presented before a server restart, which
        # wiped the old in-memory set and re-asked. Never two cards
        # for one operation; distinct operations still get their own.
        if self.store.approval_pending_op("gate_op", op):
            st["ops"].add(op)
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
        detail.update(gate_consequence(note))
        detail["effect_classes"] = effect_classes(note, structured)
        detail["reversibility"] = (
            "not reversible from this interface"
            if detail.get("effect_kind") == "delete" else
            "previous file content may be lost"
            if detail.get("effect_kind") == "overwrite" else
            "depends on the exact command shown - no automatic undo")
        # what each answer does - statements about this app's verified
        # grant mechanism, identical for every card:
        detail["approve_effect"] = (
            "writes a single-use, hash-matched grant: the EXACT same "
            "command unlocks once, nothing else")
        detail["deny_effect"] = (
            "the command never runs; the agent is told it was declined "
            "and must not retry it")
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
            json.dumps(detail, ensure_ascii=False),
            op=op, conv_id=conv_id)
        self.store.audit("gate", "gate.confirm_required", aid,
                         {"op": op, "conv": conv_id})
        publish({"type": "approval", "id": aid})

    def rewrite_scope_grants(self) -> None:
        """Materialise the live scoped grants for gate.py (atomic:
        tmp file + rename, 0600). Revocation / expiry re-runs this,
        so 'revoke' has an immediate effect on what the gate allows."""
        import tempfile
        grants = [{"id": g["id"], "roots": g["roots"],
                   "expires": g["expires"]}
                  for g in self.store.scope_grants_active()]
        try:
            os.makedirs(APPROVALS_DIR, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=APPROVALS_DIR)
            with os.fdopen(fd, "w") as fh:
                json.dump(grants, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, os.path.join(APPROVALS_DIR,
                                         SCOPE_GRANTS_FILE))
        except OSError:
            pass

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
        if decision == "approve_scope" and op:
            roots = scope_roots_for(info)
            if roots:
                gid = new_id("scg")
                self.store.scope_grant_add(
                    gid, time.time() + SCOPE_TTL_SECONDS, roots,
                    appr["id"], op, "writes under these roots only")
                self.rewrite_scope_grants()
                self.store.audit("user", "scope.granted", appr["id"],
                                 {"roots": roots, "op": op,
                                  "ttl_hours": SCOPE_TTL_SECONDS // 3600})
            else:
                # nothing NARROW can be derived from the recorded
                # targets - degrade honestly to approve-once instead
                # of inventing a broad grant
                decision = "approve"
        try:
            client = self._ensure_client()
            if job_id:
                msg = (
                    ("The user APPROVED this exact operation AND "
                     "auto-approves further writes under: "
                     + ", ".join(scope_roots_for(info))
                     + " (4 hours, revocable in the interface). Retry "
                       "the identical tool call now - then report the "
                       "outcome.")
                    if decision == "approve_scope" else
                    ("The user APPROVED this exact operation on the "
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
                     "alternative.")))
                r = client.reply(job_id, msg)
                if not r.get("delivered"):
                    raise RuntimeError(str(r)[:160])
            if not st["busy"]:
                st["busy"] = True
                st["job"] = job_id
                # this relay carries a decision outcome, not a chat
                # message - no automatic re-dispatch for it (the
                # single-use grant would be the thing duplicated)
                st["text"] = ""
                kick = self.store.msg_add(
                    "agent",
                    f"Acting on your decision (job {str(job_id)[:8]}...) - "
                    "relaying the agent's response.",
                    {"badges": ["real agent", "streaming"],
                     "source": "codebuddy", "job_id": job_id,
                     "turn_state": "streaming"}, conv_id,
                    visibility="audit")
                st["kick"] = kick["id"]
                st["conv"] = conv_id
                self.store.conv_set_turn(conv_id, "streaming", "")
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
