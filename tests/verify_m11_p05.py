#!/usr/bin/env python3
"""M11 P0-5 battery: performance - measured, not claimed.

The audit (see commit 5fb7dbb + this run's printed numbers):
  * ZERO secondary indexes in a DB that only grows: every conversation
    read was a full scan, and conv_list ran one message full scan PER
    conversation on EVERY sidebar refresh (the hottest UI call).
  * the relay published one SSE 'chat' event per tool update; every
    event triggered a full conversation refetch + full innerHTML
    rebuild on every client (rebuild ran even when the new HTML was
    identical to the old one).
Fixes: five IF NOT EXISTS indexes; one coalesced publish per poll;
chatRefreshSoon() SSE coalescing; renderChat skips the DOM write when
the conversation markup is unchanged.

This battery PRINTS before/after numbers (no-index vs index, same
6k-message workload) and keeps loose guard thresholds so a regression
still fails. Run: python3 tests/verify_m11_p05.py
"""
import atexit
import base64
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 8789
PASS, FAIL = [], []

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

udir = tempfile.mkdtemp(prefix="neoag-m11-p05-")
os.environ["NEOAG_DEMO_DB"] = "p05unit.sqlite3"
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


class _TimeShim:
    sleep = staticmethod(lambda s: None)
    time = staticmethod(time.time)


def bench(fn, n):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000.0)
    ts.sort()
    return ts[len(ts) // 2], ts[-1]      # median, worst


try:
    _amod.time = _TimeShim
    # ---- seed a realistic-growth workload through the real schema ----
    store = Store(pathlib.Path(udir))
    db = store.db
    cids = []
    for i in range(60):
        cids.append(store.conv_create(f"p05 c{i}")["id"])
    rows = []
    for cid in cids:
        for k in range(100):             # 6000 stored messages
            rows.append(("2026-11-19T00:00:00+08:00",
                         "user" if k % 2 else "agent",
                         f"message {k} payload " + "x" * 200,
                         "{}", cid, "chat"))
        for k in range(50):              # 3000 tool-event audit rows
            rows.append(("2026-11-19T00:00:00+08:00", "tool",
                         "[tool completed] out",
                         json.dumps({"kind": "tool", "tool_call_id": f"t{k}"}),
                         cid, "audit"))
    db.executemany("INSERT INTO messages(ts,role,text,meta_json,conv_id,"
                   "visibility) VALUES(?,?,?,?,?,?)", rows)
    db.commit()
    total = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    print(f"[workload] conversations={len(cids)} messages={total}")

    # ---- BEFORE: drop the M11 indexes from the same DB --------------
    for idx in ("idx_messages_conv", "idx_conversations_open",
                "idx_decisions_job", "idx_researchevents"):
        db.execute(f"DROP INDEX IF EXISTS {idx}")
    db.commit()
    big = cids[0]
    b_conv, b_convw = bench(lambda: store.conv_list(200), 7)
    b_msg, b_msgw = bench(lambda: store.msg_list(300, conv_id=big), 7)
    print(f"[before-index] conv_list median={b_conv:.2f}ms "
          f"worst={b_convw:.2f}ms | msg_list(300) median={b_msg:.2f}ms "
          f"worst={b_msgw:.2f}ms")

    # ---- AFTER: the schema's own (idempotent) index statements ------
    for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_messages_conv "
            "ON messages(conv_id, visibility)",
            "CREATE INDEX IF NOT EXISTS idx_conversations_open "
            "ON conversations(deleted, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_decisions_job "
            "ON decisions(job_id)",
            "CREATE INDEX IF NOT EXISTS idx_researchevents "
            "ON research_events(research_id, id)"):
        db.execute(stmt)
    db.commit()
    a_conv, a_convw = bench(lambda: store.conv_list(200), 7)
    a_msg, a_msgw = bench(lambda: store.msg_list(300, conv_id=big), 7)
    print(f"[after-index ] conv_list median={a_conv:.2f}ms "
          f"worst={a_convw:.2f}ms | msg_list(300) median={a_msg:.2f}ms "
          f"worst={a_msgw:.2f}ms")
    check("1a indexes make the two hot reads measurably faster on a "
          "6k-message DB (median)",
          a_conv < b_conv and a_msg < b_msg,
          f"conv {b_conv:.2f}->{a_conv:.2f} msg {b_msg:.2f}->{a_msg:.2f}")
    plan = db.execute("EXPLAIN QUERY PLAN SELECT * FROM messages WHERE "
                      "conv_id=? AND visibility=?",
                      (big, "chat")).fetchall()
    check("1b the conversation read really uses the index now "
          "(EXPLAIN QUERY PLAN, not a scan)",
          any("idx_messages_conv" in r[-1] for r in plan),
          " | ".join(r[-1] for r in plan))

    # ---- 2. relay: ONE coalesced chat event per poll, not per row ---
    events = []
    _stub.publish = lambda ev: events.append(ev)
    try:
        ag = DemoAgent(store, None)

        class _BusyGW:
            def __init__(self):
                self.n = 0

            def job(self, j):
                self.n += 1
                return ({"state": "running", "alive": True}
                        if self.n <= 1 else
                        {"state": "done", "alive": True})

            def transcript(self, j):
                ups = []
                for k in range(6):       # 6 tool events in ONE poll
                    ups.append(
                        {"sessionUpdate": "tool_call", "toolCallId": f"c{k}",
                         "status": "pending", "title": f"`cmd{k}`"})
                    ups.append(
                        {"sessionUpdate": "tool_call_update",
                         "toolCallId": f"c{k}", "status": "completed",
                         "title": f"`cmd{k}`", "rawOutput": "ok"})
                ups.append({"sessionUpdate": "agent_message_chunk",
                            "messageId": "m1",
                            "content": {"text": "/unit/p05"}})
                return ups

        cid = store.conv_create("p05 fanout")["id"]
        store.conv_set_job(cid, "job-busy")
        st = ag._cstate(cid)
        st.update({"job": "job-busy", "busy": True, "seen": {"seed"},
                   "stop": False, "conv": cid})
        ag._client = _BusyGW()
        ag._turn(cid)
    finally:
        _stub.publish = lambda ev: None
    chat_ev = [e for e in events if e.get("type") == "chat"]
    n_rows = len([m for m in store.msg_list(200, conv_id=cid,
                                            visibility=None)
                  if m["meta"].get("kind") == "tool"])
    # 12 stored tool rows in one poll -> exactly ONE batch event,
    # plus the ONE terminal event (which must stay - it ends the turn)
    check("2a 12 tool events in one poll publish EXACTLY ONE batch "
          "chat event (+1 honest terminal), while ALL 12 rows persist",
          len(chat_ev) == 2 and n_rows == 12,
          f"events={len(chat_ev)} rows={n_rows}")

    # ---- 3. static contracts the fix depends on ----------------------
    storepy = (ROOT / "backend" / "store.py").read_text()
    check("3a the index DDL is idempotent (IF NOT EXISTS) so every "
          "existing DB upgrades on open - no manual migration",
          storepy.count("CREATE INDEX IF NOT EXISTS") >= 4)
    agentpy = (ROOT / "backend" / "agent.py").read_text()
    check("3b relay coalescing present (dirty-flag publish, not "
          "per-update)",
          "dirty = True" in agentpy and "if dirty:" in agentpy
          and agentpy.count("visibility=\"audit\")\n                        "
                            "publish(") == 0)
    appjs = (ROOT / "frontend" / "app.js").read_text()
    check("3c renderChat skips the DOM write when markup is unchanged "
          "(no scroll yank, no node churn)",
          "renderChat._sig" in appjs
          and "if (sig !== renderChat._sig)" in appjs)
    check("3d SSE chat events are coalesced (chatRefreshSoon), the "
          "watchdog stays the delivery guarantee",
          "function chatRefreshSoon" in appjs
          and 'if (ev.type === "chat") chatRefreshSoon(ev.conv_id)'
          in appjs
          and "Streaming watchdog" in appjs)
finally:
    _amod.time = time
    try:
        del os.environ["NEOAG_DEMO_DB"]
    except KeyError:
        pass

# ---- 4. end-to-end endpoint latencies on a served, indexed DB -------
shutil.copyfile(pathlib.Path(udir) / "p05unit.sqlite3",
                ROOT / "data" / "m11p05.sqlite3")
srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="m11p05.sqlite3",
             NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-m11p05-skills",
             NEOAG_GATE_TOKEN_DISABLE="1"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _cleanup():
    srv.terminate()
    try:
        srv.wait(timeout=8)
    except Exception:
        srv.kill()
    try:
        (ROOT / "data" / "m11p05.sqlite3").unlink()
    except OSError:
        pass
    shutil.rmtree(ROOT / "data" / "exports-m11p05", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p05-skills", ignore_errors=True)
    shutil.rmtree(udir, ignore_errors=True)


atexit.register(_cleanup)


def api(method, path, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method, headers={"Authorization": AUTH_HEADER})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()
    return (time.perf_counter() - t0) * 1000.0


up = False
for _ in range(60):
    try:
        api("GET", "/api/bootstrap")
        up = True
        break
    except Exception:
        time.sleep(0.5)
check("4a server serves the 6k-message DB", up)

if up:
    # find the seeded 150-row conversation through the API itself
    ts = []
    for _ in range(9):
        ts.append(api("GET", "/api/bootstrap"))
    t_bootstrap = sorted(ts)
    ts = []
    for _ in range(9):
        ts.append(api("GET", "/api/conversations"))
    t_convs = sorted(ts)
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/conversations",
        headers={"Authorization": AUTH_HEADER})
    with urllib.request.urlopen(req, timeout=30) as r:
        crows = json.loads(r.read().decode())["conversations"]
    big_row = max(crows, key=lambda c: c.get("n_messages", 0))
    ts = []
    for _ in range(9):
        ts.append(api("GET", "/api/conversations/" + big_row["id"]))
    t_conv = sorted(ts)
    ts = []
    for _ in range(9):
        ts.append(api("POST", "/api/chat", {"text": "perf probe " * 40,
                                            "conv_id": big_row["id"]}))
    t_chat = sorted(ts)
    print(f"[served] bootstrap med={t_bootstrap[4]:.1f}ms "
          f"max={t_bootstrap[-1]:.1f}ms | conv-list "
          f"med={t_convs[4]:.1f}ms max={t_convs[-1]:.1f}ms | conv-open "
          f"({big_row.get('n_messages')} msgs) med={t_conv[4]:.1f}ms "
          f"max={t_conv[-1]:.1f}ms | chat POST med={t_chat[4]:.1f}ms "
          f"max={t_chat[-1]:.1f}ms")
    check("4b served endpoint latency on a 6k-message DB stays inside "
          "the P0-5 budget (median < 300ms each, including chat POST)",
          t_bootstrap[4] < 300 and t_convs[4] < 300
          and t_conv[4] < 300 and t_chat[4] < 300,
          f"boot={t_bootstrap[4]:.0f} convs={t_convs[4]:.0f} "
          f"conv={t_conv[4]:.0f} chat={t_chat[4]:.0f}")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
