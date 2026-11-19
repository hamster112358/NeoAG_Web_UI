#!/usr/bin/env python3
"""M11 P0-1 battery: conversation vs execution separation.

The data model - not the rendering - separates the two:
  * messages.visibility 'chat'  = the conversation the user reads
    (user turns, substantive agent replies, important notices, final
     outcomes)
  * messages.visibility 'audit' = the execution record (per-tool events,
    turn kickoffs) - durably persisted, served only on ?include=audit
  * conversations carries the LIVE TURN (turn_state / turn_ts /
    activity) so the interface shows one compact honest indicator.

Covers: the one-time migration of a pre-M11 database (rows converted,
nothing deleted, idempotent), the storage contract of a live relay turn
(in-process, fake gateway), the startup sweep, and the HTTP contract of
an isolated real server (own DB on port 8793 - never the live demo).
Run: python3 tests/verify_m11_p01.py
"""
import atexit
import base64
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 8793
BASE = f"http://127.0.0.1:{PORT}"
PASS, FAIL = [], []

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

sys.path.insert(0, str(ROOT))
import types  # noqa: E402

_stub = types.ModuleType("backend.server")
_stub.publish = lambda ev: None
sys.modules.setdefault("backend.server", _stub)
from backend.store import Store  # noqa: E402


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS" if ok else "FAIL") + f"  {name}"
          + (f"  [{detail}]" if detail and not ok else ""))


# =============== 1. migration of a pre-M11 database ==================
udir = tempfile.mkdtemp(prefix="neoag-m11-p01-")
dbp = pathlib.Path(udir) / "p01unit.sqlite3"
c = sqlite3.connect(str(dbp))
c.executescript("""
CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
  role TEXT, text TEXT, meta_json TEXT, conv_id TEXT);
CREATE TABLE conversations(id TEXT PRIMARY KEY, created_at TEXT,
  updated_at TEXT, title TEXT, job_id TEXT, job_state TEXT,
  deleted INTEGER DEFAULT 0);
INSERT INTO conversations(id,updated_at,title)
  VALUES('c-old','t0','old chat');
""")
for i in range(200):
    c.execute("INSERT INTO messages(ts,role,text,meta_json,conv_id) "
              "VALUES('t','tool',?,'{\"kind\":\"tool\"}','c-old')",
              (f"[running] work {i}",))
c.execute("INSERT INTO messages(ts,role,text,meta_json,conv_id) "
          "VALUES('t','user','the question','{}','c-old')")
c.execute("INSERT INTO messages(ts,role,text,meta_json,conv_id) "
          "VALUES('t','agent','the answer','{}','c-old')")
c.execute("INSERT INTO messages(ts,role,text,meta_json,conv_id) "
          "VALUES('t2','agent','kickoff','"
          "{\"turn_state\":\"completed\"}','c-old')")
c.commit()
c.close()

os.environ["NEOAG_DEMO_DB"] = "p01unit.sqlite3"
s = Store(pathlib.Path(udir))
rows_all = s.msg_list(1000, conv_id="c-old", visibility=None)
rows_chat = s.msg_list(1000, conv_id="c-old")
check("1a migration converts tool events + kickoff to audit, keeps "
      "EVERY row (203 stored), user/agent stay chat",
      len(rows_all) == 203 and len(rows_chat) == 2
      and {m["role"] for m in rows_chat} == {"user", "agent"}
      and sum(1 for m in rows_all if m["visibility"] == "audit") == 201,
      f"all={len(rows_all)} chat={len(rows_chat)}")
conv = s.conv_get("c-old")
check("1b last persisted turn_state mirrored onto the conversation row",
      (conv or {}).get("turn_state") == "completed",
      str((conv or {}).get("turn_state")))
check("1c backfill recorded as done (idempotent marker)",
      s.setting("m11_p01_visibility", None) == 1)
s2 = Store(pathlib.Path(udir))
rows2 = s2.msg_list(1000, conv_id="c-old", visibility=None)
check("1d reopening the store never re-flags or duplicates",
      len(rows2) == 203)

# =============== 2. turn state lifecycle on the conversation ==========
s.conv_set_turn("c-old", "streaming", "Reading the manifest files")
conv = s.conv_get("c-old")
check("2a streaming turn: state + HUMAN-READABLE activity label stored",
      conv["turn_state"] == "streaming"
      and conv["activity"] == "Reading the manifest files"
      and conv.get("turn_ts"), str(conv)[:160])
s.msg_add("agent", "orphan kick", {"turn_state": "streaming"}, "c-old",
          visibility="audit")
n = s.msg_sweep_stale_streaming()
conv = s.conv_get("c-old")
check("2b startup sweep: orphaned streaming conversation -> interrupted,"
      " activity cleared", n >= 1
      and conv["turn_state"] == "interrupted by server restart"
      and conv["activity"] == "", str(conv["turn_state"]))

# =============== 3. live relay turn stores by class ===================
from backend import agent as amod  # noqa: E402
from backend.agent import DemoAgent  # noqa: E402


class TimeShim:
    sleep = staticmethod(lambda x: None)
    time = staticmethod(time.time)


class DoneGW:
    def job(self, j):
        return {"state": "done"}

    def transcript(self, j):
        return [
            {"sessionUpdate": "tool_call", "toolCallId": "t1",
             "status": "pending", "title": "`ls data/`"},
            {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
             "status": "completed",
             "rawOutput": {"type": "text", "text": "sample.bam"}},
            {"sessionUpdate": "agent_message_chunk", "messageId": "m1",
             "content": {"text": "Final answer text."}}]


ag = DemoAgent(s, None)
amod.time = TimeShim
try:
    conv = s.conv_create("turn class")
    cid = conv["id"]
    st = ag._cstate(cid)
    kick = s.msg_add("agent", "kick", {"turn_state": "streaming"}, cid,
                     visibility="audit")
    st.update({"job": "j1", "kick": kick["id"], "seen": {"pre-seeded"},
               "busy": True, "conv": cid})
    ag._client = DoneGW()
    ag._turn(cid, max_seconds=30)
finally:
    amod.time = time
rows_all = s.msg_list(200, conv_id=cid, visibility=None)
rows_chat = s.msg_list(200, conv_id=cid)
tools = [m for m in rows_all if m["role"] == "tool"]
check("3a per-tool events persisted as AUDIT rows (full record kept)",
      len(tools) >= 2 and all(m["visibility"] == "audit" for m in tools),
      f"{len(tools)} tool rows")
check("3b kickoff is an audit row, NOT conversation",
      any(m["id"] == kick["id"] and m["visibility"] == "audit"
          for m in rows_all))
check("3c the assistant reply is a CHAT row",
      any(m["text"] == "Final answer text."
          and m["visibility"] == "chat" for m in rows_all))
check("3d the CHAT view of this conversation is 1 row (the answer)",
      len(rows_chat) == 1, str([m["text"][:30] for m in rows_chat]))
conv = s.conv_get(cid)
check("3e finished turn: terminal state on the conversation row",
      conv["turn_state"] == "completed" and conv["activity"] == "",
      str(conv["turn_state"]))

# =============== 4. HTTP contract (isolated real server) ==============
try:
    (ROOT / "data" / "m11p01.sqlite3").unlink()
except OSError:
    pass
srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="m11p01.sqlite3",
             NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-m11p01-skills",
             NEOAG_TASKQ_DIR="/tmp/neoag-m11p01-taskq"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _cleanup():
    srv.terminate()
    try:
        srv.wait(timeout=8)
    except Exception:
        srv.kill()
    for p in (ROOT / "data" / "m11p01.sqlite3",):
        try:
            p.unlink()
        except OSError:
            pass
    import shutil
    for d in ("/tmp/neoag-m11p01-skills", "/tmp/neoag-m11p01-taskq"):
        shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(ROOT / "data" / "exports-m11p01", ignore_errors=True)


atexit.register(_cleanup)


def call(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = __import__("urllib.request", fromlist=["x"]).Request(
        BASE + path, data=data,
        method=method or ("POST" if data is not None else "GET"))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", AUTH_HEADER)
    import urllib.error
    try:
        with __import__("urllib.request", fromlist=["x"]).urlopen(
                req, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")
    except urllib.error.URLError:
        return 0, {}   # server not bound yet - the up-loop retries


up = False
for _ in range(80):
    st, _b = call("/api/bootstrap")
    if st == 200:
        up = True
        break
    time.sleep(0.25)
check("4a isolated server up", up)

if up:
    st, r = call("/api/chat", {"text": "P0-1 probe message",
                               "conv_id": ""})
    check("4b POST /api/chat returns ONLY chat rows (kickoff no longer"
          " appended as conversation noise)",
          st == 200 and r.get("messages")
          and all(m.get("visibility") == "chat"
                  for m in r["messages"]),
          str([(m.get("role"), m.get("visibility"))
               for m in r.get("messages", [])])[:150])
    cid = r["conv_id"]
    st, d = call(f"/api/conversations/{cid}")
    check("4c default GET: chat rows carry visibility; conversation "
          "carries turn fields",
          st == 200
          and all(m.get("visibility") == "chat" for m in d["messages"])
          and "turn_state" in d["conversation"])
    # inject an audit row straight into this isolated DB (test-owned)
    sc = sqlite3.connect(str(ROOT / "data" / "m11p01.sqlite3"))
    sc.execute("INSERT INTO messages(ts,role,text,meta_json,conv_id,"
               "visibility) VALUES('t','tool','[audit probe]','{}',?,"
               "'audit')", (cid,))
    sc.commit()
    sc.close()
    st, d1 = call(f"/api/conversations/{cid}")
    st, d2 = call(f"/api/conversations/{cid}?include=audit")
    check("4d audit rows hidden by default, present with include=audit "
          "(technical view), nothing deleted",
          all(m.get("visibility") == "chat" for m in d1["messages"])
          and any(m["text"] == "[audit probe]"
                  and m["visibility"] == "audit" for m in d2["messages"])
          and len(d2["messages"]) == len(d1["messages"]) + 1)
    st, bz = call("/api/bootstrap")
    check("4e bootstrap messages payload is chat-only too",
          all(m.get("visibility") == "chat" for m in bz.get("messages",
                                                            [])))

print()
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("failed: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
