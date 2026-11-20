#!/usr/bin/env python3
"""M11 P0-4 battery: message integrity and context recovery.

The filed regressions this pins:
  * an old backend slice [:4000] and client slice [:16000] silently cut
    the user's OWN message tail while reporting success
    (fixed reject-not-truncate; re-pinned here)
  * session recovery rebuilt context from transcript lines silently
    shortened to ~200 chars, masquerading as the conversation
    (now: durable message ids + file-backed full transcript +
    explicitly-budgeted window whose shortened lines label themselves)
  * the execution record capped tool output at [:600] silently
    (now: full output stored; any bound leaves an explicit marker)
Run: python3 tests/verify_m11_p04.py
"""
import atexit
import base64
import json
import re
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import types
import urllib.request
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 8791
LIM = 100_000                  # server.MAX_CHAT_CHARS (re-asserted below)
PASS, FAIL = [], []

# --- isolation MUST be set before backend.agent import (AGENT_WS,
# --- TASKQ_DIR are read at import time) ------------------------------
udir = tempfile.mkdtemp(prefix="neoag-m11-p04-")
os.environ["NEOAG_AGENT_WS"] = udir + "/ws"
os.environ["NEOAG_TASKQ_DIR"] = udir + "/taskq"
os.environ["NEOAG_GATE_APPROVALS"] = udir + "/approvals"
os.environ["NEOAG_DEMO_DB"] = "p04unit.sqlite3"

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

sys.path.insert(0, str(ROOT))
_stub = types.ModuleType("backend.server")
_stub.publish = lambda ev: None
sys.modules.setdefault("backend.server", _stub)
from backend.store import Store  # noqa: E402
from backend import agent as _amod  # noqa: E402
from backend.agent import DemoAgent  # noqa: E402


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS" if ok else "FAIL") + f"  {name}"
          + (f"  [{detail}]" if detail and not ok else ""))


class _TimeShim:                 # relay polling becomes a spin
    sleep = staticmethod(lambda s: None)
    time = staticmethod(time.time)


class _RecGW:
    """Records dispatch/reply payloads. job() is alive only for the
    ids the test declares - everything else reads as gone, which is
    what drives the recovery path."""

    def __init__(self, alive_ids=()):
        self.dispatched = []
        self.replied = []
        self.alive_ids = set(alive_ids)

    def job(self, j):
        if j in self.alive_ids:
            return {"state": "running", "alive": True}
        return {"unknown": True}

    def transcript(self, j):
        return []

    def dispatch(self, prompt, cwd, name, **kw):
        self.dispatched.append({"prompt": prompt, "cwd": cwd, "name": name})
        return {"id": "job-rec"}

    def reply(self, j, text):
        self.replied.append(text)
        return {"delivered": True}


SPEC = ("SPECIMEN LONG SPEC - section 1: alpha.\n\n"
        + ("section-2 filler line with unicode \u2713\u6f22\u5b57\U0001f9ec\n"
           * 400)
        + "\nsection 9 (TAIL, must survive everywhere): "
          "final requirement \u0394 = 0.001 - END OF SPEC")

try:
    _amod.time = _TimeShim
    store = Store(pathlib.Path(udir))
    ag = DemoAgent(store, None)

    # ============ 1. durable full-text retrieval ======================
    c1 = store.conv_create("p04 transcript")
    cid1 = c1["id"]
    store.msg_add("user", SPEC, {}, cid1)
    store.msg_add("agent", "ack \u2713", {"badges": ["real agent"]}, cid1)
    store.msg_add("tool", "[tool completed] x", {}, cid1,
                  visibility="audit")
    tr = store.conv_transcript(cid1)
    check("1a stored message survives intact (exact text + tail, "
          "durable ids, chat-only, oldest first)",
          len(tr) == 2 and tr[0]["role"] == "user"
          and tr[0]["text"] == SPEC
          and tr[1]["text"] == "ack \u2713"
          and isinstance(tr[0]["id"], int) and tr[0]["id"] < tr[1]["id"],
          f"rows={len(tr)}")

    # ============ 2. recovery dispatch: verbatim + honest context =====
    gw = _RecGW()                        # knows NO live job -> recovery
    ag._client = gw
    ag._ensure_client = lambda: gw
    c2 = store.conv_create("p04 recovery")
    cid2 = c2["id"]
    big = "OLD-TURN " + "z" * 3000
    mid_first = None
    for i in range(30):                  # ~90k chars of history
        row = store.msg_add("user" if i % 2 else "agent",
                            big + str(i), {}, cid2)
        if mid_first is None:
            mid_first = row["id"]
    store.conv_set_job(cid2, "job-dead")   # a real session existed
    store.msg_add("user", SPEC, {}, cid2)
    ag.handle_real(SPEC, cid2)
    prompt = gw.dispatched[-1]["prompt"]
    n_abbrev = prompt.count("ABBREVIATED")
    check("2a recovery dispatch delivers the CURRENT message verbatim "
          "(exact tail, after preamble+transcript)",
          prompt.endswith(SPEC)
          and prompt.startswith(ag.CHAT_PREAMBLE))
    check("2b recovery context is BUDGETED and HONEST, not a silent "
          "~200-char digest: shortened lines label themselves and "
          "cite their message ids",
          n_abbrev > 0 and "chars stored;" in prompt
          and "Saved transcript" in prompt
          and len(prompt) < len(SPEC) + 25_000,
          f"abbrev={n_abbrev} len={len(prompt)} spec={len(SPEC)}")
    fpath = (pathlib.Path(udir) / "ws" / ".neoag-chat" / cid2
             / "transcript.md")
    ftxt = fpath.read_text(encoding="utf-8") if fpath.exists() else ""
    check("2c the full transcript FILE (durable, workspace-readable, "
          "restart-proof) carries the exact tail of an abbreviated "
          "message under its message id - retrieval, not guessing",
          ftxt.startswith("# Saved NeoAG Web conversation")
          and f"## message {mid_first}" in ftxt
          and "z" * 3000 + "0" in ftxt and "END OF SPEC" in ftxt,
          "missing/wrong file")
    check("2d abbreviated lines point the fresh session AT the file",
          "transcript.md" in prompt and "read message" in prompt)

    # ============ 3. continuation and lost-delivery paths =============
    gw2 = _RecGW(alive_ids={"job-alive"})
    ag2 = DemoAgent(store, None)
    ag2._client = gw2
    ag2._ensure_client = lambda: gw2
    c3 = store.conv_create("p04 continue")
    cid3 = c3["id"]
    store.conv_set_job(cid3, "job-alive")
    store.msg_add("user", SPEC, {}, cid3)
    ag2.handle_real(SPEC, cid3)
    continued_ok = gw2.replied == [SPEC] and not gw2.dispatched
    gw2.alive_ids = set()                # let the relay thread retire
    check("3a live continuation hands over the text EXACTLY "
          "(no preamble, no transcript, no slicing)", continued_ok)

    class _Refuse(_RecGW):
        def reply(self, j, text):
            self.replied.append(text)
            return {"delivered": False, "error": "worker gone"}

    gw3 = _Refuse(alive_ids={"job-dying"})
    ag3 = DemoAgent(store, None)
    ag3._client = gw3
    ag3._ensure_client = lambda: gw3
    c4 = store.conv_create("p04 refused")
    cid4 = c4["id"]
    store.conv_set_job(cid4, "job-dying")
    store.msg_add("user", SPEC, {}, cid4)
    ag3.handle_real(SPEC, cid4)
    fell_ok = (gw3.replied == [SPEC] and len(gw3.dispatched) == 1
               and gw3.dispatched[0]["prompt"].endswith(SPEC))
    gw3.alive_ids = set()
    check("3b lost-delivery (gateway accepted, never processed) falls "
          "through to recovery WITHOUT losing the message text", fell_ok)

    # ============ 4. execution record keeps full tool output ==========
    class _ToolGW:
        def __init__(self, out):
            self.out = out
            self.n = 0

        def job(self, j):
            self.n += 1
            return ({"state": "running", "alive": True} if self.n <= 1
                    else {"state": "done", "alive": True})

        def transcript(self, j):
            return [{"sessionUpdate": "tool_call_update",
                     "toolCallId": "t1", "status": "completed",
                     "title": "`run`", "rawOutput": self.out},
                    {"sessionUpdate": "agent_message_chunk",
                     "messageId": "m1",
                     "content": {"text": "/unit/done"}}]

    out5k = "line \u2713\u6f22\n" * 800           # 6400 chars > old cap
    c5 = store.conv_create("p04 toolout")
    cid5 = c5["id"]
    store.conv_set_job(cid5, "job-to")
    st5 = ag._cstate(cid5)
    st5.update({"job": "job-to", "busy": True, "seen": {"x"},
                "stop": False, "conv": cid5})
    ag._client = _ToolGW(out5k)
    ag._turn(cid5)
    audit_rows = [m for m in store.msg_list(200, conv_id=cid5,
                                            visibility=None)
                  if m["meta"].get("kind") == "tool"]
    rec = audit_rows[0]["meta"]["tool_output"] if audit_rows else ""
    body = audit_rows[0]["text"] if audit_rows else ""
    check("4a tool output stored COMPLETE in the execution record "
          "(old silent [:600] cap is gone); the one-liner is a marked "
          "preview",
          audit_rows and rec == out5k
          and audit_rows[0]["meta"].get("tool_output_chars")
          == len(out5k) > 600
          and "full output in the record" in body
          and "tool_output_truncated" not in audit_rows[0]["meta"],
          f"rec_len={len(rec)}")

    old_max = _amod.TOOL_OUTPUT_STORE_MAX
    _amod.TOOL_OUTPUT_STORE_MAX = 1000
    try:
        c6 = store.conv_create("p04 cap")
        cid6 = c6["id"]
        store.conv_set_job(cid6, "job-cap")
        st6 = ag._cstate(cid6)
        st6.update({"job": "job-cap", "busy": True, "seen": {"x"},
                    "stop": False, "conv": cid6})
        ag._client = _ToolGW("K" * 2000)
        ag._turn(cid6)
        ar6 = [m for m in store.msg_list(200, conv_id=cid6,
                                         visibility=None)
               if m["meta"].get("kind") == "tool"]
        m6 = ar6[0]["meta"] if ar6 else {}
        check("4b even when the storage bound engages it is EXPLICIT: "
              "marker + true size + truncated flag, never a silent clip",
              m6.get("tool_output", "").startswith("K" * 1000)
              and "END OF STORED RECORD" in m6.get("tool_output", "")
              and m6.get("tool_output_truncated") is True
              and m6.get("tool_output_chars") == 2000)
    finally:
        _amod.TOOL_OUTPUT_STORE_MAX = old_max

    # ============ 5. honest gateway-detail inside a message ===========
    _raw5 = "boom " * 200

    class _FailGW:
        def job(self, j):
            return {"state": "failed", "alive": False,
                    "detail": _raw5}

        def transcript(self, j):
            return []

    c7 = store.conv_create("p04 detail")
    cid7 = c7["id"]
    store.conv_set_job(cid7, "job-fail")
    st7 = ag._cstate(cid7)
    st7.update({"job": "job-fail", "busy": True, "seen": set(),
                "stop": False, "conv": cid7})
    ag._client = _FailGW()
    ag._turn(cid7)
    # P1-12 moved the terminal line into phrasing.turn_ended_text; its
    # honesty tail is stable across states. The trimmed-from count is
    # measured on the WHITESPACE-COLLAPSED string the display works
    # with, so derive it from the same input instead of hardcoding.
    _norm5 = " ".join(_raw5.split())
    tx7 = [m["text"] for m in store.msg_list(200, conv_id=cid7)
           if "No assistant reply text was recorded" in m["text"]]
    _trim5 = re.search(r"\[text trimmed from (\d+) chars\]",
                       tx7[0]) if tx7 else None
    check("5a a gateway detail shortened for display SAYS so "
          "(trimmed-from marker + the size it collapsed to)",
          bool(_trim5) and int(_trim5.group(1)) == len(_norm5),
          tx7[0][:120] if tx7 else "no terminal message")
finally:
    _amod.time = time
    try:
        del os.environ["NEOAG_DEMO_DB"]
    except KeyError:
        pass

# ============ 6. live server: exact delivery, limits, drafts ==========
srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="m11p04.sqlite3",
             NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-m11p04-skills",
             NEOAG_GATE_TOKEN_DISABLE="1"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _cleanup():
    srv.terminate()
    try:
        srv.wait(timeout=8)
    except Exception:
        srv.kill()
    try:
        (ROOT / "data" / "m11p04.sqlite3").unlink()
    except OSError:
        pass
    shutil.rmtree(ROOT / "data" / "exports-m11p04", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p04-skills", ignore_errors=True)
    shutil.rmtree(udir, ignore_errors=True)


atexit.register(_cleanup)


def api(method, path, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method, headers={"Authorization": AUTH_HEADER})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


up = False
for _ in range(40):
    try:
        stx, _ = api("GET", "/api/bootstrap")
        up = stx == 200
        if up:
            break
    except Exception:
        time.sleep(0.5)
check("6a server boots (isolated :8791)", up)

if up:
    stx, r = api("POST", "/api/chat", {"text": SPEC, "conv_id": ""})
    cid = r.get("conv_id", "")
    got = [m for m in r.get("messages", []) if m["role"] == "user"]
    stx2, d = api("GET", "/api/conversations/" + cid)
    stored = [m for m in d.get("messages", []) if m["role"] == "user"]
    check("6b a long multi-section unicode spec persists and reloads "
          "byte-exactly (no silent truncation anywhere on the chat "
          "path)",
          stx == 200 and got and got[0]["text"] == SPEC
          and stored and stored[0]["text"] == SPEC)

    stx3, r3 = api("POST", "/api/chat",
                   {"text": "x" * (LIM + 1), "conv_id": cid})
    stx4, d4 = api("GET", "/api/conversations/" + cid)
    sizes = [len(m["text"]) for m in d4.get("messages", [])]
    check("6c over-limit message: explicit 413 naming the limit "
          f"({LIM}), NOTHING persisted",
          stx3 == 413 and r3.get("error") == "message_too_long"
          and r3.get("limit") == LIM and all(s <= LIM for s in sizes),
          f"st={stx3} body={str(r3)[:80]}")
    stx5, r5 = api("POST", "/api/chat", {"text": "y" * LIM,
                                         "conv_id": ""})
    u5 = [m for m in r5.get("messages", []) if m["role"] == "user"]
    check("6d the limit itself is usable: a message of exactly the "
          "limit is accepted and stored at full length",
          stx5 == 200 and u5 and len(u5[0]["text"]) == LIM)

appjs = (ROOT / "frontend" / "app.js").read_text()
check("6e a failed send puts the draft back in the composer and shows "
      "the server's own reason (no lost drafts, no bare HTTP 413)",
      '$("chat-input").value = text' in appjs
      and "(e.data.detail || e.data.error)" in appjs)
check("6g drafts are per-conversation: switching chats parks the "
      "un-sent text keyed by conversation and restores the target's "
      "own draft",
      'const DRAFT_KEY = "nj_drafts"' in appjs
      and "draftSave(state.activeCid)" in appjs
      and 'draftSave(state.activeCid);\n  state.activeCid = cid;'
      in appjs and "draftLoad(cid)" in appjs)
serverpy = (ROOT / "backend" / "server.py").read_text()
check("6f the 413 rejection precedes ANY persistence of an oversized "
      "message, and the audit row names its preview honestly",
      serverpy.index("chat.rejected_too_long")
      < serverpy.index('msg_add("user", text')
      and '"text_head"' in serverpy)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
