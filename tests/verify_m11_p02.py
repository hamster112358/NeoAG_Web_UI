#!/usr/bin/env python3
"""M11 P0-2 battery: approval correctness.

Pins the semantics the confirmation cards must have:
  * one live card per operation, enforced in the DATABASE (survives
    server restarts - the old in-memory dedup set did not)
  * cancel / expiry take a card out of the actionable set
    (state='stale' / 'expired'), decided cards reject replays (409)
  * 'always allow within this scope' (approve_scope): narrow roots
    derived from the recorded targets, dated, listed via
    /api/scopegrants, revocable (revocation rewrites the file the
    execution gate reads), NEVER available on run/job cards
  * derived card fields: effect classes + reversibility, with an
    honest 'undetermined' when the command text cannot say

Isolated server: own DB on port 8792, NEOAG_AGENT_WS pointed at a
temp directory so grant / scope files never touch the live workspace.
Run: python3 tests/verify_m11_p02.py
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
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 8792
BASE = f"http://127.0.0.1:{PORT}"
PASS, FAIL = [], []

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

TMPWS = tempfile.mkdtemp(prefix="neoag-m11-p02-ws-")
APPROVALS = os.path.join(TMPWS, ".neoag-approvals")
os.environ["NEOAG_AGENT_WS"] = TMPWS

sys.path.insert(0, str(ROOT))
_stub = types.ModuleType("backend.server")
_stub.publish = lambda ev: None
sys.modules.setdefault("backend.server", _stub)
from backend.store import Store  # noqa: E402
from backend import agent as AG  # noqa: E402


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS" if ok else "FAIL") + f"  {name}"
          + (f"  [{detail}]" if detail and not ok else ""))


# ================= 1. schema / migration ==============================
udir = tempfile.mkdtemp(prefix="neoag-m11-p02-")
s = Store(pathlib.Path(udir))
cols = {r[1] for r in s.db.execute("PRAGMA table_info(approvals)")}
tables = {r[0] for r in s.db.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}
check("1a fresh DB: approvals.op/conv_id + scope_grants table",
      {"op", "conv_id"} <= cols and "scope_grants" in tables,
      str(cols))

olddir = tempfile.mkdtemp(prefix="neoag-m11-p02-old-")
dbp = pathlib.Path(olddir) / "p02old.sqlite3"
c = sqlite3.connect(str(dbp))
c.executescript("""
CREATE TABLE approvals(id TEXT PRIMARY KEY, created_at TEXT,
  decided_at TEXT, job_id TEXT, action TEXT, risk TEXT, detail TEXT,
  state TEXT, decided_by TEXT);
INSERT INTO approvals(id,created_at,action,risk,detail,state)
  VALUES('apm-old','2026-01-01T00:00:00','gate_op','destructive',
         '{\"op\":\"aa\"}','approved');
""")
c.commit()
c.close()
os.environ["NEOAG_DEMO_DB"] = "p02old.sqlite3"
s2 = Store(pathlib.Path(olddir))  # noqa: F841 - migrates in place
del os.environ["NEOAG_DEMO_DB"]
c = sqlite3.connect(str(dbp))
cols = {r[1] for r in c.execute("PRAGMA table_info(approvals)")}
n_old = c.execute("SELECT count(*) FROM approvals WHERE id='apm-old'"
                  ).fetchone()[0]
c.close()
check("1b pre-P0-2 DB migrates: op/conv_id columns added, old card kept",
      {"op", "conv_id"} <= cols and n_old == 1)

# ================= 2. dedup / decide / invalidate / expiry ============
a1 = s.approval_create("apm-1", "", "gate_op", "destructive",
                       '{"op":"ab" * 12}', op="ab" * 12, conv_id="c1")
dup = s.approval_pending_op("gate_op", "ab" * 12)
check("2a pending card for an op found by hash (restart-survivable dedup)",
      dup and dup["id"] == "apm-1")
check("2b unknown op has no pending card",
      s.approval_pending_op("gate_op", "ff" * 12) is None)
s.approval_decide("apm-1", True, "user")
check("2c decided card leaves the pending set (a NEW identical attempt "
      "may card again)", s.approval_pending_op("gate_op", "ab" * 12) is None)
check("2d double-decide returns None (HTTP maps this to 409)",
      s.approval_decide("apm-1", True, "user") is None)

s.approval_create("apm-2", "", "gate_op", "destructive", "{}",
                  op="cc" * 12, conv_id="c1")
s.approval_create("apm-3", "", "gate_op", "destructive", "{}",
                  op="dd" * 12, conv_id="c1")
s.approval_create("apm-4", "", "gate_op", "destructive", "{}",
                  op="ee" * 12, conv_id="c2")
n = s.approval_invalidate_conv("c1")
states = {row[0]: row[1] for row in s.db.execute(
    "SELECT id,state FROM approvals").fetchall()}
check("2e cancel invalidates exactly that conversation's pending cards "
      "(2 moved to stale, other conversation untouched)",
      n == 2 and states["apm-2"] == "stale"
      and states["apm-3"] == "stale" and states["apm-4"] == "pending",
      str(states))
s.db.execute("UPDATE approvals SET created_at=? WHERE id='apm-4'",
             (time.strftime("%Y-%m-%dT%H:%M:%S",
                            time.gmtime(time.time() - 7200)),))
s.db.commit()
s.approval_create("apm-5", "", "gate_op", "destructive", "{}",
                  op="11" * 12, conv_id="c3")
ne = s.approval_expire_stale()
states = {row[0]: row[1] for row in s.db.execute(
    "SELECT id,state FROM approvals").fetchall()}
check("2f expiry sweep retires only the stale-older card (30 min grace)",
      ne == 1 and states["apm-4"] == "expired"
      and states["apm-5"] == "pending", str(states))

# ================= 3. scoped grants store =============================
g = s.scope_grant_add("scg-1", time.time() + 3600,
                      ["/data1/neoag/agents/neoag_agent_ws/jobs/j1"],
                      "apm-2", "ab" * 12, "writes only")
act = s.scope_grant_list()
check("3a grant stored with roots round-trip",
      len(act) == 1 and act[0]["roots"] == g["roots"]
      and not act[0]["revoked"])
check("3b revoke marks + returns; second revoke is a no-op",
      s.scope_grant_revoke("scg-1") is not None
      and s.scope_grant_revoke("scg-1") is None
      and not s.scope_grants_active())
check("3c expired grants leave the active set",
      (s.scope_grant_add("scg-2", time.time() - 1, ["/x/y/z"], "", "",
                         ""),
       not any(x["id"] == "scg-2"
               for x in s.scope_grants_active()))[1])

# ================= 4. derived card fields =============================
ec = AG.effect_classes
check("4a effect classes derived: rm -> delete",
      ec("rm -rf /data1/neoag/x/y", {}) == ["delete"])
check("4b overwrite detected", "overwrite" in ec(
    "echo x > /data1/neoag/x/y", {}))
check("4c network verb detected", "network" in ec(
    "curl -s https://x/y -o /data1/neoag/a", {}))
check("4d no path -> UNDETERMINED, never 'safe'",
      ec("echo hi", {}) == ["undetermined"])
check("4e dataset download card -> network-read",
      ec("", {"class": "dataset_download"}) == ["network-read"])
sr = AG.scope_roots_for
check("4f scope roots derived from targets (parent dirs)",
      sr({"target_paths": ["/data1/neoag/agents/neoag_agent_ws/"
                           "jobs/j1/out/f.tsv"]})
      == ["/data1/neoag/agents/neoag_agent_ws/jobs/j1/out"])
check("4g BROAD targets refuse a scope (no broad grants exist)",
      sr({"target_paths": ["/tmp/x"]}) == []
      and sr({"target_paths": []}) == [])
check("4h more than 3 distinct roots refuses (not a narrow scope)",
      sr({"target_paths": [f"/data1/a{i}/b{i}/c/f.txt"
                           for i in range(4)]}) == [])

# ================= 5. live HTTP server ================================
srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="m11p02.sqlite3",
             NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-m11p02-skills",
             NEOAG_TASKQ_DIR="/tmp/neoag-m11p02-taskq"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _cleanup():
    srv.terminate()
    try:
        srv.wait(timeout=8)
    except Exception:
        srv.kill()
    try:
        (ROOT / "data" / "m11p02.sqlite3").unlink()
    except OSError:
        pass
    shutil.rmtree(ROOT / "data" / "exports-m11p02", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p02-skills", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p02-taskq", ignore_errors=True)
    shutil.rmtree(TMPWS, ignore_errors=True)


atexit.register(_cleanup)


def call(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        method=method or ("POST" if data is not None else "GET"))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", AUTH_HEADER)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


for _ in range(40):
    try:
        urllib.request.urlopen(BASE + "/api/health", timeout=2)
        break
    except Exception:
        time.sleep(0.5)

OP = "ab" * 12
seed = sqlite3.connect(str(ROOT / "data" / "m11p02.sqlite3"))
seed.execute(
    "INSERT INTO approvals(id,created_at,action,risk,detail,state,op,"
    "conv_id) VALUES('apm-h1',?,'gate_op','destructive',?,'pending',"
    "?, 'c-h')",
    (time.strftime("%Y-%m-%dT%H:%M:%S"), json.dumps({
        "op": OP, "conv_id": "c-h", "note": "writes outside workspace",
        "target_paths": ["/data1/neoag/agents/neoag_agent_ws/jobs/"
                         "j1/out/f.tsv"]}), OP))
seed.execute(
    "INSERT INTO approvals(id,created_at,job_id,action,risk,detail,"
    "state) VALUES('apm-h2',?,'job-x','run-summary','execute','{}',"
    "'pending')", (time.strftime("%Y-%m-%dT%H:%M:%S"),))
seed.execute(
    "INSERT INTO approvals(id,created_at,action,risk,detail,state,op,"
    "conv_id) VALUES('apm-h3',?,'gate_op','destructive','{}','pending'"
    ", 'ee12', 'c-h')",
    (time.strftime("%Y-%m-%dT%H:%M:%S"),))
seed.commit()
seed.close()

st, b = call("/api/bootstrap")
check("5a bootstrap carries scope_grants list",
      st == 200 and isinstance(b.get("scope_grants"), list))

st, r = call("/api/approvals/apm-h2", {"decision": "approve_scope"})
check("5b run/job card CANNOT be scope-approved (400, card untouched)",
      st == 400)

st, r = call("/api/approvals/apm-h3", {"decision": "banana"})
check("5c unknown decision rejected (400)", st == 400)

st, r = call("/api/approvals/apm-h1", {"decision": "approve_scope"})
gf = os.path.join(APPROVALS, f"{OP}.json")
sf = os.path.join(APPROVALS, "scope-grants.json")
ok_file = os.path.isfile(gf)
roots = []
if os.path.isfile(sf):
    with open(sf) as fh:
        roots = [g["roots"] for g in json.load(fh)]
check("5d approve_scope: single-use grant written AND dated scoped "
      "grant file materialised for the gate",
      st == 200 and ok_file
      and roots == [["/data1/neoag/agents/neoag_agent_ws/jobs/j1/out"]],
      f"st={st} gf={ok_file} roots={roots}")

st2, _ = call("/api/approvals/apm-h1", {"decision": "approve"})
check("5e replaying any decision on a decided card -> 409",
      st2 == 409)

st, r = call("/api/scopegrants")
gid = (r.get("grants") or [{}])[0].get("id", "") if r.get("grants") \
    else ""
check("5f /api/scopegrants lists the live grant",
      st == 200 and gid.startswith("scg"))
st, r = call(f"/api/scopegrants/{gid}/revoke", {})
with open(sf) as fh:
    after = json.load(fh)
st3, r3 = call("/api/scopegrants")
check("5g revoke: immediate - the file the gate reads no longer lists "
      "it, list endpoint empty",
      st == 200 and after == [] and st3 == 200
      and r3.get("grants") == [])

# stale card -> 409 through the real endpoint
seed = sqlite3.connect(str(ROOT / "data" / "m11p02.sqlite3"))
seed.execute("UPDATE approvals SET state='stale' WHERE id='apm-h3'")
seed.commit()
seed.close()
st, _ = call("/api/approvals/apm-h3", {"decision": "approve"})
check("5h invalidated card refuses decisions (409 = 'no longer "
      "pending', UI maps it to the plain-words message)", st == 409)

# ================= 6. frontend contract ================================
appjs = (ROOT / "frontend" / "app.js").read_text()
check("6a one actionable card at a time (oldest first, rest queued)",
      "gateCards.forEach((a, i) => { html += gateCardHtml(a, i === 0); })"
      in appjs and "gateSeen === 0" in appjs)
check("6b scope button on write cards, sent as approve_scope",
      'data-dec="approve_scope"' in appjs
      and "gate.scopeBtn" in appjs)
check("6c stale-card decision shows the plain-words message",
      'e.status === 409' in appjs and "gate.staleGone" in appjs)
check("6d scoped grants are listed and revocable from the UI",
      "data-revoke" in appjs and "attn.scopesH" in appjs
      and "b.scope_grants" in appjs)
missing = [k for k in ("gate.scopeBtn", "gate.scopeTitle", "gate.queued",
                       "gate.staleGone", "gate.effect",
                       "gate.reversibility", "gate.ec.write",
                       "gate.ec.overwrite", "gate.ec.delete",
                       "gate.ec.network", "gate.ec.network-read",
                       "gate.ec.write-or-execute",
                       "gate.ec.undetermined", "attn.scopesH",
                       "attn.scopeUntil", "attn.revoke",
                       "toast.revokeOk", "toast.revokeFail")
           if appjs.count(f'"{k}": "') != 2]
check("6e every new key exists in BOTH the en and zh dictionaries",
      not missing, str(missing))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
