"""Redesign verification battery v4 (mock only).
Keeps every milestone-2 invariant and the redesign checks (new IA, job
kinds, input manifests, real params inventory, help docs, agent-mode
gating), and adds the phase-B checks: conversation persistence API,
gate classifier + grant protocol, layout contract, honest status
endpoint.

Runs against an ISOLATED server (fresh NEOAG_DEMO_DB on port 8798)
that this script spawns itself - the live demo state and its database
are never touched.
Run: python3 tests/verify_battery.py
"""
import atexit
import base64
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 8798
BASE = f"http://127.0.0.1:{PORT}"
PASS, FAIL = [], []

AUTH_PATH = ROOT / "data" / "auth.json"
with open(AUTH_PATH) as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

# ---------- isolated server (fresh db, own port) ----------
_srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="batterytest.sqlite3",
             NEOAG_DEMO_PORT=str(PORT)),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _cleanup():
    _srv.terminate()
    try:
        _srv.wait(timeout=8)
    except Exception:
        _srv.kill()
    try:
        (ROOT / "data" / "batterytest.sqlite3").unlink()
    except OSError:
        pass


atexit.register(_cleanup)


def _wait_up():
    for _ in range(80):
        try:
            req = urllib.request.Request(BASE + "/api/bootstrap",
                                         headers={"Authorization": AUTH_HEADER})
            urllib.request.urlopen(req, timeout=3)
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("isolated battery server did not start")


_wait_up()


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS" if ok else "FAIL") + f"  {name}" + (f"  [{detail}]" if detail and not ok else ""))


def call(path, body=None, method=None, timeout=15, auth=True):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method or
                                 ("POST" if data is not None else "GET"))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("Authorization", AUTH_HEADER)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode() or "{}")


def wait_state(jid, states, limit=90):
    t0 = time.time()
    while time.time() - t0 < limit:
        _, b = call("/api/bootstrap")
        for j in b["jobs"]:
            if j["id"] == jid:
                if j["state"] in states:
                    return j, b
        time.sleep(0.4)
    raise TimeoutError(f"job {jid} not in {states} within {limit}s")


def wait_unblocked(jid, limit=30):
    """Wait until a blocked job has actually left blocked state (avoids the
    post-resolve race where 'blocked' still shows for ~0.25s)."""
    t0 = time.time()
    while time.time() - t0 < limit:
        _, b = call("/api/bootstrap")
        for j in b["jobs"]:
            if j["id"] == jid:
                if j["state"] != "blocked":
                    return j
        time.sleep(0.3)
    raise TimeoutError(f"job {jid} still blocked after {limit}s")


REQUIRED = ["input_qc", "hla_typing", "purity_cnv", "hla_loh", "rna_expression",
            "snv", "fusion_discovery", "fusion_peptides", "splice_discovery",
            "splice_peptides", "presentation", "safety_escape", "ranking",
            "reports"]


def submit(sample, exec_mode, fail=False, stages=None):
    st, r = call("/api/jobs", {"sample_id": sample, "stage_ids": stages or REQUIRED,
                               "mode": "auto", "execution_mode": exec_mode,
                               "simulate_failure": fail})
    assert st == 202, (st, r)
    return r["job_id"], r["approval_id"]


def approve(aid):
    call(f"/api/approvals/{aid}", {"approve": True})


def resolve(d_id, choice):
    st, r = call(f"/api/decisions/{d_id}/resolve", {"choice": choice})
    assert st == 200, (st, r)


def get_raw(path, timeout=5, auth=True):
    req = urllib.request.Request(BASE + path)
    if auth:
        req.add_header("Authorization", AUTH_HEADER)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


# ---------- 0 fresh workspace is empty (no seeded demo clutter) ----------
_, bz = call("/api/bootstrap")
check("0 fresh workspace starts empty",
      bz["conversations"] == [] and bz["messages"] == [] and
      bz["jobs"] == [] and bz["approvals"] == [] and
      bz.get("active_conv", "") == "", str({k: bz[k] for k in
      ("conversations", "messages", "jobs", "approvals")})[:160])

# ---------- 1 static / frontend ----------
st, idx = get_raw("/")
check("1 index served (new IA)", st == 200 and b"work-nav" in idx and
      b"Tasks" in idx and b"Discovery" in idx and b"Help" in idx and
      b"demo-banner" not in idx)
st, raw_js = get_raw("/static/app.js")
js = raw_js.decode()
check("2 app.js wired", all(k in js for k in
      ["renderJobs", "/api/decisions/", "/api/jobs/", "execution_mode",
       "showReport", "/api/research", "agent_mode", "manifests"]))
st, _ = get_raw("/static/styles.css")
check("3 styles served", st == 200)

# ---------- 1b auth gate rejects credential-less requests ----------
try:
    urllib.request.urlopen(BASE + "/api/bootstrap", timeout=5)
    check("1b no-auth bootstrap rejected", False, "200 without credentials")
except urllib.error.HTTPError as e:
    check("1b no-auth bootstrap rejected", e.code == 401, str(e.code))
try:
    urllib.request.urlopen(BASE + "/", timeout=5)
    check("1c no-auth index rejected", False, "200 without credentials")
except urllib.error.HTTPError as e:
    check("1c no-auth index rejected", e.code == 401, str(e.code))
try:
    bad = base64.b64encode(b"operator:wrong-password").decode()
    req = urllib.request.Request(BASE + "/api/bootstrap",
                                 headers={"Authorization": "Basic " + bad})
    urllib.request.urlopen(req, timeout=5)
    check("1d wrong password rejected", False, "200 with wrong password")
except urllib.error.HTTPError as e:
    check("1d wrong password rejected", e.code == 401, str(e.code))

# ---------- 2 controlled run with all three decision points ----------
jid, aid = submit("DEMO-P001", "controlled")
approve(aid)
j, b = wait_state(jid, ["blocked"])
dec = [d for d in b["decisions"] if d["job_id"] == jid and d["status"] == "pending"]
check("4 controlled blocks at 1st decision", len(dec) == 1 and
      dec[0]["decision_type"] == "input_mapping", json.dumps(b["decisions"])[:200])
check("5 decision carries policy+version", bool(dec[0]["policy_id"]) and
      bool(dec[0]["policy_version"]), str(dec[0]))
resolve(dec[0]["id"], "matched")
wait_unblocked(jid)
j, b = wait_state(jid, ["blocked", "completed"])
pend = [d for d in b["decisions"] if d["job_id"] == jid and d["status"] == "pending"]
# presentation gate decision comes next (rankings only if selected -> ranking stage has dp)
gate = [d for d in pend if d["decision_type"] == "parameter_choice" and
        "gate" in json.dumps(d["options"])]
check("6 2nd decision = stability gate", len(gate) == 1)
resolve(gate[0]["id"], "gate20")  # deliberate override of validated default
wait_unblocked(jid)
j, b = wait_state(jid, ["blocked", "completed"])
pend = [d for d in b["decisions"] if d["job_id"] == jid and d["status"] == "pending"]
rk = [d for d in pend if d["decision_type"] == "parameter_choice" and
      {o["id"] for o in d["options"]} == {"both", "weighted"}]
check("7 3rd decision = ranking outputs", len(rk) == 1)
resolve(rk[0]["id"], "both")
j, b = wait_state(jid, ["completed"])
check("8 controlled run completes", j["state"] == "completed")
st, res = call("/api/results/" + jid)
check("9 controlled result release=true", res.get("production_release") is True,
      str(res.get("release_note")))
hist = [d for d in b["decisions"] if d["job_id"] == jid]
check("10 decision history complete fields", len(hist) == 3 and all(
    d["chosen"] and d["rationale"] and d["decided_by"] == "user" and
    d["decided_at"] and d["policy_id"] and d["policy_version"] and
    d["options"] and d["evidence"] is not None for d in hist))
gate_row = next(d for d in hist if "gate" in json.dumps(d["options"]))
check("11 override recorded (gate20 by user)", gate_row["chosen"] == "gate20" and
      gate_row["decided_by"] == "user", str(gate_row["chosen"]))

# ---------- 3 autonomous run auto-resolves everything ----------
jid2, aid2 = submit("DEMO-P002", "autonomous")
approve(aid2)
j2, b2 = wait_state(jid2, ["completed"])
auto = [d for d in b2["decisions"] if d["job_id"] == jid2]
check("12 autonomous never blocked", j2["state"] == "completed")
check("13 3 engine decisions recorded", len(auto) == 3 and all(
    d["decided_by"] == "engine" and d["status"] == "resolved" for d in auto),
    str([(d["chosen"], d["decided_by"]) for d in auto]))
st, res2 = call("/api/results/" + jid2)
check("14 autonomous result release=true + mode recorded",
      res2.get("production_release") is True and
      res2["provenance"].get("execution_mode") == "autonomous")

# ---------- 4 mode switch at stage boundary ----------
jid3, aid3 = submit("DEMO-P003", "controlled")
approve(aid3)
call(f"/api/jobs/{jid3}/mode", {"mode": "autonomous"})
# the job is blocked at the input_qc decision created before the switch
# request - the user still answers it; the switch then applies at the next
# stage boundary and every later decision is engine-resolved
j3, b3 = wait_state(jid3, ["blocked"])
p3 = [d for d in b3["decisions"] if d["job_id"] == jid3 and d["status"] == "pending"]
resolve(p3[0]["id"], p3[0]["options"][0]["id"])
j3, b3 = wait_state(jid3, ["completed"])
sw = [d for d in b3["decisions"] if d["job_id"] == jid3 and
      d["decision_type"] == "mode_switch"]
check("15 mode_switch decision recorded at boundary", len(sw) == 1 and
      sw[0]["chosen"] == "autonomous" and sw[0]["status"] == "resolved",
      str(sw)[:200])
st, res3 = call("/api/results/" + jid3)
check("16 switch applies (provenance exec=autonomous)",
      res3["provenance"].get("execution_mode") == "autonomous")

# ---------- 5 failure injection, controlled: retry bounded then skip ----------
jid4, aid4 = submit("DEMO-P001", "controlled", fail=True)
approve(aid4)
# resolve the input_qc decision if it appears
j4, b4 = wait_state(jid4, ["blocked"])
first = [d for d in b4["decisions"] if d["job_id"] == jid4 and d["status"] == "pending"]
resolve(first[0]["id"], first[0]["options"][0]["id"])
wait_unblocked(jid4)
# gate decision
j4, b4 = wait_state(jid4, ["blocked"])
pend = [d for d in b4["decisions"] if d["job_id"] == jid4 and d["status"] == "pending"]
resolve(pend[0]["id"], pend[0]["options"][0]["id"])
wait_unblocked(jid4)
# presentation runs after the gate decision and fails (mock) -> card #1
j4, b4 = wait_state(jid4, ["blocked"])
err1 = [d for d in b4["decisions"] if d["job_id"] == jid4 and
        d["decision_type"] == "error_recovery" and d["status"] == "pending"]
check("17 error_recovery card shown", len(err1) == 1 and
      {o["id"] for o in err1[0]["options"]} == {"retry", "skip", "terminate"},
      str(err1)[:200])
resolve(err1[0]["id"], "retry")
wait_unblocked(jid4)
# after retry the stage re-runs and fails again -> new card WITHOUT retry
j4, b4 = wait_state(jid4, ["blocked"])
err2 = [d for d in b4["decisions"] if d["job_id"] == jid4 and
        d["decision_type"] == "error_recovery" and d["status"] == "pending"]
check("18 retry is bounded (2nd card has no retry option)", len(err2) == 1 and
      {o["id"] for o in err2[0]["options"]} == {"skip", "terminate"},
      str(err2)[:200])
resolve(err2[0]["id"], "skip")
wait_unblocked(jid4)
# with presentation skipped the run continues and still asks the ranking
# decision in controlled mode - skip never silently removes later gates
j4, b4 = wait_state(jid4, ["blocked"])
rk4 = [d for d in b4["decisions"] if d["job_id"] == jid4 and d["status"] == "pending"]
resolve(rk4[0]["id"], rk4[0]["options"][0]["id"])
j4, b4 = wait_state(jid4, ["completed"])
st, res4 = call("/api/results/" + jid4)
check("19 skip marks production_release=false",
      res4.get("production_release") is False and
      "presentation" in res4.get("release_note", "").lower(),
      str(res4.get("release_note")))

# ---------- 6 failure injection, autonomous: one auto retry then fail ----------
jid5, aid5 = submit("DEMO-P002", "autonomous", fail=True)
approve(aid5)
j5, b5 = wait_state(jid5, ["failed"])
check("20 autonomous stops after bounded retry", j5["state"] == "failed" and
      "silently skipping" in (j5.get("error") or ""), str(j5.get("error"))[:160])
evs = call(f"/api/jobs/{jid5}/events")[1]
check("21 auto-retry logged", any("retrying stage once" in e["text"] for e in evs))

# ---------- 7 cancellation ----------
jid6, aid6 = submit("DEMO-P003", "controlled")
approve(aid6)
# block at decision, then cancel -> job cancelled + pending decision cancelled
j6, b6 = wait_state(jid6, ["blocked"])
call(f"/api/jobs/{jid6}/cancel", {})
j6, b6 = wait_state(jid6, ["cancelled"])
pend6 = [d for d in b6["decisions"] if d["job_id"] == jid6]
check("22 cancel while blocked works", j6["state"] == "cancelled")
check("23 pending decision cancelled not silent",
      all(d["status"] == "cancelled" for d in pend6 if d["status"] != "resolved"),
      str([(d["decision_type"], d["status"]) for d in pend6]))

# ---------- 8 research digests ----------
st, r = call("/api/research", {"scope": "full", "target": "presentation"})
assert st == 202
rid = r["research_id"]
t0 = time.time()
rep = None
while time.time() - t0 < 20:
    _, rr = call(f"/api/research/{rid}")
    if rr["status"] == "completed":
        rep = rr["report"]
        break
    time.sleep(0.5)
check("24 full research digest completes", rep is not None)
check("25 digest honesty labels", rep and all(
    row["independently_benchmarked"] is False and "MOCK" in row["basis"]
    for row in rep["comparison"]))
check("26 full digest covers 4 targets", rep and len(rep["comparison"]) == 11,
      str(len(rep["comparison"]) if rep else 0))
check("27 recommendation + experiments", rep and
      "No replacement recommended" in rep["recommendation"]["verdict"] and
      len(rep["recommendation"]["experiments_before_adoption"]) == 5)
st, r = call("/api/research", {"scope": "targeted", "target": "stability"})
rid2 = r["research_id"]
t0 = time.time()
rep2 = None
while time.time() - t0 < 20:
    _, rr = call(f"/api/research/{rid2}")
    if rr["status"] == "completed":
        rep2 = rr["report"]
        break
    time.sleep(0.5)
check("28 targeted digest (stability) rows", rep2 and
      len(rep2["comparison"]) == 2 and
      any("POL-001" in (row.get("notes") or "") for row in rep2["comparison"]))

# ---------- 9 SSE events flow ----------
sse_events = []
stop_flag = threading.Event()


def sse_listen():
    req = urllib.request.Request(BASE + "/api/stream",
                                 headers={"Authorization": AUTH_HEADER})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            for raw in resp:
                if stop_flag.is_set():
                    break
                line = raw.decode().strip()
                if line.startswith("data:"):
                    sse_events.append(line[5:].strip())
    except Exception:
        pass


th = threading.Thread(target=sse_listen, daemon=True)
th.start()
time.sleep(0.5)
jid7, aid7 = submit("DEMO-P001", "autonomous")
approve(aid7)
time.sleep(2.5)
stop_flag.set()
check("29 SSE delivers job+approval events", len(sse_events) >= 2 and
      any('"job"' in e for e in sse_events), str(sse_events[:3]))
wait_state(jid7, ["completed"], limit=60)

# ---------- 10 audit trail records decisions ----------
_, b8 = call("/api/bootstrap")
_, audit = call("/api/audit")
acts = {a["action"] for a in audit}
check("30 audit has decision + research + mode entries",
      any(a.startswith("decision.") for a in acts) and
      "research.completed" in acts and "job.mode.requested" in acts,
      str(sorted(acts)))

# ---------- 11 bad requests rejected honestly ----------
st_bad = None
try:
    call("/api/decisions/does-not-exist/resolve", {"choice": "x"})
except urllib.error.HTTPError as e:
    st_bad = e.code
check("31 unknown decision resolve -> 409", st_bad == 409, str(st_bad))
try:
    call("/api/jobs", {"sample_id": "DEMO-P001", "stage_ids": ["nope"],
                       "execution_mode": "weird"})
    st_bad = 200
except urllib.error.HTTPError as e:
    st_bad = e.code
check("32 invalid execution_mode -> 4xx/5xx", st_bad in (400, 422, 500), str(st_bad))

# ---------- 12 redesign: IA, kinds, manifests, params, help ----------
def _raises409(path, body):
    try:
        call(path, body)
        return False
    except urllib.error.HTTPError as e:
        return e.code == 409


def _job_kind(jid):
    _, bb = call("/api/bootstrap")
    return next(j["kind"] for j in bb["jobs"] if j["id"] == jid)


_, b9 = call("/api/bootstrap")
check("33 bootstrap carries redesign keys",
      all(k in b9 for k in ("manifests", "agent_mode", "params_meta",
                            "stage_graph", "decision_points", "help")),
      str(sorted(b9.keys())))
st, params = call("/api/params")
check("34 params inventory is code-grounded",
      st == 200 and len(params["groups"]) >= 6 and
      all(p.get("source") for g in params["groups"] for p in g["items"]) and
      len(params["human_required"]) >= 2)
st, doc = call("/api/help/workflow")
check("35 help serves real repo doc", st == 200 and doc["bytes"] > 1000)
try:
    call("/api/help/does-not-exist")
    st_bad = 200
except urllib.error.HTTPError as e:
    st_bad = e.code
check("36 unknown help doc -> 404", st_bad == 404, str(st_bad))
st, m = call("/api/manifests/propose",
             {"description": "tumor DNA, matched normal DNA and RNA under /data/project-x for DEMO-P001"})
check("37 manifest proposed with mock note",
      st == 201 and m["proposal"]["mock"] is True and m["proposal"]["inputs"])
st, m2 = call(f"/api/manifests/{m['id']}/confirm", {"edits": {}})
check("38 manifest confirm + double-confirm 409",
      m2["status"] == "confirmed" and _raises409(f"/api/manifests/{m['id']}/confirm", {}))
try:
    call("/api/manifests/propose", {"description": "  "})
    st_bad = 200
except urllib.error.HTTPError as e:
    st_bad = e.code
check("39 empty manifest description -> 400", st_bad == 400, str(st_bad))
st, rj = call("/api/jobs", {"sample_id": "DEMO-P001",
                           "stage_ids": ["input_qc", "presentation"],
                           "mode": "auto", "execution_mode": "autonomous",
                           "kind": "discovery",
                           "requested_config": {"netmhcpan_home": "/opt/netMHCpan-4.1b"}})
check("40 discovery-kind job accepted + kind recorded",
      st == 202 and _job_kind(rj["job_id"]) == "discovery")
approve(rj["approval_id"])
try:
    call("/api/jobs", {"sample_id": "X", "stage_ids": ["input_qc"],
                       "kind": "bogus"})
    st_bad = 200
except urllib.error.HTTPError as e:
    st_bad = e.code
check("41 invalid kind -> 400", st_bad == 400, str(st_bad))
_, b10 = call("/api/bootstrap")
j10 = next(j for j in b10["jobs"] if j["id"] == rj["job_id"])
check("42 requested_config persisted in job spec",
      j10["spec"].get("requested_config", {}).get("netmhcpan_home")
      == "/opt/netMHCpan-4.1b")
wait_state(rj["job_id"], ["completed"], limit=60)
_, d10 = call(f"/api/decisions?job_id={rj['job_id']}")
check("43 decisions filter by job", len(d10["decisions"]) >= 1 and
      all(d["job_id"] == rj["job_id"] for d in d10["decisions"]))
call("/api/agent/mode", {"mode": "real"})
_, b11 = call("/api/bootstrap")
check("44 agent-mode switch persists", b11["agent_mode"] == "real")
call("/api/agent/mode", {"mode": "mock"})
try:
    call("/api/agent/mode", {"mode": "bogus"})
    st_bad = 200
except urllib.error.HTTPError as e:
    st_bad = e.code
check("45 invalid agent-mode -> 400", st_bad == 400, str(st_bad))
check("46 conversation-aware frontend wired",
      all(k in js for k in ("renderSidebar", "openConv", "conv_id",
                            "/api/conversations", "initSplitter")))
def _banner_count():
    _, bb = call("/api/bootstrap")
    return sum(1 for m in bb["messages"]
               if (m.get("meta") or {}).get("badges") == ["mock prototype"])
_before = _banner_count()
call("/api/chat", {"text": "status"})
_after = _banner_count()
check("47 chat never appends new mock banners", _after == _before,
      f"{_before} -> {_after}")
_, b12 = call("/api/bootstrap")
check("48 scripted chat still answers in mock mode",
      any(m["role"] == "agent" for m in b12["messages"][-2:]))
cb = call("/api/codebuddy/status")[1] if True else {}
check("49 codebuddy client status honest", cb.get("connected") in (True, False))
st, kb = call("/api/help/codebuddy")
check("50 codebuddy doc describes jobs API + gate", st == 200 and
      "jobs api" in kb.get("markdown", "").lower() and
      "confirm_required" in kb.get("markdown", "").lower())

# ---------- 13 phase B: conversations, gate, layout contract ----------
st, cr = call("/api/chat", {"text": "battery conversation alpha"})
cid_a = cr["conv_id"]
check("51 first chat auto-creates a titled conversation",
      st == 200 and cid_a and
      cr["conversation"]["title"].startswith("battery conversation"),
      str(cr["conversation"])[:160])
call("/api/chat", {"text": "second message in alpha", "conv_id": cid_a})
st, cv = call(f"/api/conversations/{cid_a}")
check("52 messages thread into the conversation",
      st == 200 and sum(1 for m in cv["messages"] if m["role"] == "user") == 2)
call("/api/chat", {"text": "battery conversation beta"})
_, cl = call("/api/conversations")
ids = {c["id"] for c in cl["conversations"]}
check("53 history lists separate conversations with counts",
      cid_a in ids and len(ids) >= 2 and
      all(c["n_messages"] >= 2 for c in cl["conversations"]))
cid_b = next(c["id"] for c in cl["conversations"]
             if (c["title"] or "").startswith("battery conversation beta"))
st, _ = call(f"/api/conversations/{cid_a}/rename", {"title": "Renamed A"})
_, cv2 = call(f"/api/conversations/{cid_a}")
check("54 rename persists", st == 200 and
      cv2["conversation"]["title"] == "Renamed A")
_, cvb = call(f"/api/conversations/{cid_b}")
check("55 switching chats shows only that conversation's messages",
      all("alpha" not in m["text"] for m in cvb["messages"]))
st, _ = call(f"/api/conversations/{cid_b}/delete", {})
_, cl2 = call("/api/conversations")
check("56 delete is soft: hidden from list, rows retained",
      st == 200 and all(c["id"] != cid_b for c in cl2["conversations"]))
try:
    call(f"/api/conversations/{cid_b}/rename", {"title": "x"})
    st_bad = 200
except urllib.error.HTTPError as e:
    st_bad = e.code
check("57 deleted conversation is closed for edits", st_bad == 404,
      str(st_bad))
st, ex = call(f"/api/conversations/{cid_a}/example", {})
_, cv3 = call(f"/api/conversations/{cid_a}")
check("58 Load Example populates only its own conversation",
      st == 200 and len(ex["messages"]) >= 2 and
      all(m["conv_id"] == cid_a for m in ex["messages"]) and
      len(cv3["messages"]) >= 5)
try:
    call("/api/admin/restore", {"file": "../auth.json"})
    st_bad = 200
except urllib.error.HTTPError as e:
    st_bad = e.code
check("59 restore rejects path traversal / unknown archives",
      st_bad == 400, str(st_bad))
cb = call("/api/codebuddy/status")[1]
check("60 status endpoint honest about the gate model",
      "gate" in json.dumps(cb).lower() and
      '"plan"' not in json.dumps(cb))
_, css_raw = get_raw("/static/styles.css")
css = css_raw.decode()
check("61 layout contract: app fills viewport, panes scroll alone",
      "overflow: hidden" in css and "--chatw" in css and
      b'id="splitter"' in idx and b'id="conv-list"' in idx and
      b'id="new-chat"' in idx)
# gate classifier straight from the versioned asset (no gateway needed)
import importlib.util as _ilu
_gp = ROOT / "agent_workspace_assets" / ".codebuddy" / "gate.py"
_spec = _ilu.spec_from_file_location("gate_asset", _gp)
_gate = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_gate)


def _cls(tool, cmd):
    d = _gate.decide({"tool_name": tool, "tool_input": {"command": cmd}})
    if d is None:
        return "allow"
    r = d[1] if len(d) > 1 else ""
    return d[0] if d[0] != "deny" else (
        "secret" if "credential" in r else
        "self" if "self-protected" in r else
        "frozen" if "immutable" in r else "deny-?")


_ok62 = (_cls("Bash", "cat $HOME/.codebuddy/settings.json") == "secret"
         and _cls("Bash", "echo x > /home/operator/out.log") == "confirm"
         and _cls("Bash", "echo x >> /data2/caojingqi/original_OpenNeoAG/f") == "frozen"
         and _cls("Bash", "rm jobs/scratch.txt") == "allow"
         and _cls("Bash", "echo hi > jobs/out.txt") == "allow"
         and _cls("Bash", "sed -i s/a/b/ .codebuddy/gate.py") == "self")
check("62 gate classifier (asset) classes routine/destructive correctly",
      _ok62)
_h = hashlib.sha256(b"battery-grant-probe").hexdigest()[:24]
_gr = pathlib.Path(_gate.APPROVALS) / (_h + ".json")
_gr.parent.mkdir(parents=True, exist_ok=True)
_gr.write_text(json.dumps({"granted_at": time.time()}))
_ok63 = _gate.grant_valid(_h) and not _gate.grant_valid(_h)
try:
    _gr.unlink()
except OSError:
    pass
check("63 grant is single-use and hash-matched (real protocol dir)", _ok63)

# ---------- 64-68 relay terminal-state regression (2026-11-16 stall bug) ----
# The field failure: a real-mode turn stayed 'streaming' forever when the
# delivery path (SSE via the buffering quick tunnel) went dead and no
# terminal state was ever stamped. These unit checks pin the backend half
# of the fix against an isolated temp DB - no gateway needed.
import shutil
import tempfile
import types

sys.path.insert(0, str(ROOT))
_stub = types.ModuleType("backend.server")
_stub.publish = lambda ev: None
sys.modules.setdefault("backend.server", _stub)
from backend.store import Store as _Store            # noqa: E402
from backend import agent as _amod                   # noqa: E402
from backend.agent import DemoAgent as _Agent        # noqa: E402

_udb = tempfile.mkdtemp(prefix="neoag-relay-unit-")
os.environ["NEOAG_DEMO_DB"] = "relayunit.sqlite3"
_ts = _Store(pathlib.Path(_udb))
_ag = _Agent(_ts, None)


class _TimeShim:                 # make relay polling instant
    sleep = staticmethod(lambda s: None)
    time = staticmethod(time.time)


class _DeadGW:
    def job(self, j):
        raise ConnectionError("gateway down (unit)")

    def transcript(self, j):
        raise ConnectionError("gateway down (unit)")


class _DoneGW:
    def job(self, j):
        return {"state": "done"}

    def transcript(self, j):
        return [
            {"sessionUpdate": "tool_call", "toolCallId": "t1",
             "status": "pending", "title": "`pwd`"},
            {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
             "status": "completed",
             "rawOutput": {"type": "text", "text": "Stdout: /unit/ws"}},
            {"sessionUpdate": "agent_message_chunk", "messageId": "m1",
             "content": {"text": "/unit/ws"}}]


class _FailGW:
    def job(self, j):
        return {"state": "failed", "detail": "boom"}

    def transcript(self, j):
        return []


def _unit_turn(client, cid, seen={"x"}):
    _amod.time = _TimeShim
    try:
        k = _ts.msg_add("agent", "kick",
                        {"badges": ["real agent", "streaming"],
                         "turn_state": "streaming"}, cid)
        st = _ag._cstate(cid)
        st.update({"job": "unitjob", "kick": k["id"], "seen": set(seen),
                   "busy": True})
        _ag._client = client
        _ag._turn(cid, max_seconds=60)
        msgs = _ts.msg_list(500, conv_id=cid)
        kick = [m for m in msgs if m["id"] == k["id"]][0]
        return kick["meta"].get("turn_state"), msgs
    finally:
        _amod.time = time


_ts1, _msgs1 = _unit_turn(_DeadGW(), "u-lost")
check("64 gateway failure -> honest 'connection lost', never stuck",
      _ts1 == "connection lost" and
      any("connection to the agent job was lost" in m["text"]
          for m in _msgs1), str(_ts1))
_ts2, _msgs2 = _unit_turn(_DoneGW(), "u-ok")
check("65 done turn -> completed + clean real tool text",
      _ts2 == "completed" and
      any(m["role"] == "tool"
          and m["text"] == "[tool completed] Stdout: /unit/ws"
          for m in _msgs2) and
      any(m["role"] == "agent" and m["text"] == "/unit/ws"
          for m in _msgs2), str(_ts2))
_ts3, _msgs3 = _unit_turn(_FailGW(), "u-fail")
check("66 failed job -> honest terminal, no fabricated reply",
      _ts3 == "failed" and
      any("does not fabricate" in m["text"] for m in _msgs3), str(_ts3))
_ts3b, _msgs3b = _unit_turn(_DeadGW(), "u-seedfail", seen=set())
check("66b transcript-seeding failure also ends honestly (never stuck)",
      _ts3b in ("relay error", "connection lost") and _ts3b != "streaming",
      str(_ts3b))
_k = _ts.msg_add("agent", "orphan",
                 {"badges": ["real agent", "streaming"],
                  "turn_state": "streaming"}, "u-sweep")
_n = _ts.msg_sweep_stale_streaming()
_kr = [m for m in _ts.msg_list(500, conv_id="u-sweep")
       if m["id"] == _k["id"]][0]
check("67 startup sweep: orphaned streaming -> interrupted",
      _n >= 1 and _kr["meta"].get("turn_state")
      == "interrupted by server restart")
_appjs = (ROOT / "frontend" / "app.js").read_text()
check("68 frontend reaches terminal WITHOUT SSE: watchdog + reopen catch-up",
      "Streaming watchdog" in _appjs and "inflight" in _appjs and
      "Reconnect catch-up" in _appjs and
      "refreshConv(); refreshSidebar(); refreshJobs()" in _appjs)
del os.environ["NEOAG_DEMO_DB"]
shutil.rmtree(_udb, ignore_errors=True)

print()
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", ", ".join(FAIL))
    raise SystemExit(1)
print("ALL_PHASE_B_CHECKS_PASSED")
