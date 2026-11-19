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

# ---------- isolated server (fresh db, own port, throwaway deploy dirs
# so the live agent workspace's skills + task token are never touched) ----------
# a previous run that was SIGKILLed can leave its DB behind: remove it
# BEFORE spawning so "fresh workspace" is genuinely fresh
try:
    (ROOT / "data" / "batterytest.sqlite3").unlink()
except OSError:
    pass
_srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="batterytest.sqlite3",
             NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-battery-skills",
             NEOAG_TASKQ_DIR="/tmp/neoag-battery-taskq"),
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
    import shutil as _sh
    for _d in ("/tmp/neoag-battery-skills", "/tmp/neoag-battery-taskq"):
        _sh.rmtree(_d, ignore_errors=True)
    _sh.rmtree(ROOT / "data" / "exports-batterytest", ignore_errors=True)


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
       "showReport", "/api/research", "agent_mode",
       # M7: the unified preparation flow replaces the old manifests UI
       "/api/native/discover", "/api/native/manifest"]))
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
# deterministically reach the first blocked decision BEFORE requesting the
# switch (requesting earlier raced with stage startup and let the engine
# auto-resolve everything); the switch then applies at the NEXT boundary
j3, b3 = wait_state(jid3, ["blocked"])
call(f"/api/jobs/{jid3}/mode", {"mode": "autonomous"})
# the job is blocked at the input_qc decision created before the switch
# request - the user still answers it; the switch then applies at the next
# stage boundary and every later decision is engine-resolved
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

# ---------- 8 research digests (EXPLICIT example engine: the testing
# fixture; live retrieval has its own real-network verification script) ----
st, r = call("/api/research", {"scope": "full", "target": "presentation",
                               "engine": "example"})
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
st, r = call("/api/research", {"scope": "targeted", "target": "stability",
                               "engine": "example"})
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

# M7 §1.2/§9: research runs are FORMAL background tasks - they must be
# in the official task query, not only in the research panel.
_, tks28 = call("/api/tasks?kind=research")
check("28b research runs appear in the unified task query",
      any(t["id"].startswith("res-") and t["kind"] == "discovery"
          for t in tks28.get("tasks", [])), str(tks28)[:160])

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
# M6 F5 added backend protection for mode switches (409 while work runs):
# wait for a quiet moment so THIS check (persisting a switch) is not
# racing a demo job that started moments ago. The protected path itself
# is check 137.
for _ in range(80):
    _bq = call("/api/bootstrap")[1]
    if not [j for j in _bq["jobs"]
            if j["state"] in ("running", "paused")]:
        break
    time.sleep(0.5)
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
                         "turn_state": "streaming"}, cid,
                        visibility="audit")
        st = _ag._cstate(cid)
        st.update({"job": "unitjob", "kick": k["id"], "seen": set(seen),
                   "busy": True, "conv": cid})
        _ag._client = client
        _ag._turn(cid, max_seconds=60)
        # the unit asserts read the RAW store (both visibility classes)
        msgs = _ts.msg_list(500, conv_id=cid, visibility=None)
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
                  "turn_state": "streaming"}, "u-sweep",
                 visibility="audit")
_n = _ts.msg_sweep_stale_streaming()
_kr = [m for m in _ts.msg_list(500, conv_id="u-sweep", visibility=None)
       if m["id"] == _k["id"]][0]
check("67 startup sweep: orphaned streaming -> interrupted",
      _n >= 1 and _kr["meta"].get("turn_state")
      == "interrupted by server restart")
_appjs = (ROOT / "frontend" / "app.js").read_text()
check("68 frontend reaches terminal WITHOUT SSE: watchdog + reopen catch-up",
      "Streaming watchdog" in _appjs and
      'turn_state === "streaming"' in _appjs and
      "Reconnect catch-up" in _appjs and
      "refreshConv(); refreshSidebar(); refreshJobs()" in _appjs)
del os.environ["NEOAG_DEMO_DB"]
shutil.rmtree(_udb, ignore_errors=True)

# ---------- 69-78 Milestone-4 workflow reliability contract (F2) -------
# A completed run is IMMEDIATELY queryable through the official task API
# with its output-artifact manifest - the bug fixed here was Results
# staying empty until a full page reload (frontend) and results being a
# stored claim rather than the live artifact table (backend).
jid_f, aid_f = submit("DEMO-P004", "autonomous")
approve(aid_f)
jf, _ = wait_state(jid_f, ["completed"])
_, res_f = call("/api/results/" + jid_f)
_, t_f = call("/api/tasks/" + jid_f)
check("69 auto-results: completed job carries outcome + artifact manifest",
      jf.get("outcome") in ("delivered", "zero_candidates", "partial") and
      len(res_f.get("artifacts", [])) == 2,
      str(jf.get("outcome")) + " arts=" + str(len(res_f.get("artifacts", []))))
check("70 both-ranking choice really produces BOTH tables",
      set(res_f.get("candidates_by_strategy", {})) ==
      {"weighted", "evidence-consensus"})
_man = res_f.get("manifest", {})
check("71 requested+effective config, versions and snapshot ref recorded",
      _man.get("effective_config", {}).get("stability_max_rank") == 1.4 and
      _man.get("versions", {}).get("engine", "").startswith("mock-job-engine")
      and "run_snapshots" in str(_man.get("snapshot")))
# A2 E2E: choosing "weighted" in a controlled run changes the artifacts
jid_w, aid_w = submit("DEMO-P004", "controlled")
approve(aid_w)
for _choice, _pick in (("matched", lambda d: d["key"] == "input.rna_pair"),
                       ("gate14", lambda d: d["key"] == "presentation.stability_gate"),
                       ("weighted", lambda d: d["key"] == "ranking.outputs")):
    jj, bb = wait_state(jid_w, ["blocked"])
    pd = [d for d in bb["decisions"] if d["job_id"] == jid_w and
          d["status"] == "pending" and _pick(d)]
    if pd:
        resolve(pd[0]["id"], _choice)
    if jj["state"] != "blocked":
        break
    wait_unblocked(jid_w)
_, res_w = wait_state(jid_w, ["completed"])
_, res_wres = call("/api/results/" + jid_w)
check("72 ranking decision genuinely changes outputs (weighted-only = 1 artifact)",
      len(res_wres.get("artifacts", [])) == 1 and
      set(res_wres.get("candidates_by_strategy", {})) == {"weighted"},
      str([(a["strategy"], a["status"])
           for a in res_wres.get("artifacts", [])]))
# A2 E2B: the section-2 run overrode the gate to 2.0 - the EFFECT must
# be visible in the effective config and the warnings (not silently ignored)
_, res_g = call("/api/results/" + jid)
check("73 gate override visible: effective config 2.0 + recorded warning",
      res_g["manifest"]["effective_config"]["stability_max_rank"] == 2.0 and
      any("relaxed" in w for w in res_g.get("warnings", [])),
      str(res_g["manifest"]["effective_config"]))
check("74 applied decision effects stored per resolved decision",
      all((d.get("downstream") or {}).get("applied_effect")
          for d in t_f["decisions"]
          if d["status"] == "resolved" and d.get("effect_class")),
      str([(d["key"], d.get("effect_class")) for d in t_f["decisions"]]))
check("75 official task API lists formal runs",
      jid_f in [t["id"] for t in call("/api/tasks?limit=100")[1]["tasks"]])
# B2: conversation linkage - a linked job is findable by its conversation;
# an unknown conversation gets NOTHING (never the most-recent job guessed)
_, cvl = call("/api/conversations", {"title": "link probe"})
cid_link = cvl["conversation"]["id"]
_, jl = call("/api/jobs", {"sample_id": "DEMO-P004", "conv_id": cid_link})
_, act_l = call("/api/tasks/active?conv_id=" + cid_link)
_, act_n = call("/api/tasks/active?conv_id=conv-does-not-exist")
check("76 conv-linked run queryable; unknown conv -> empty (no guessing)",
      [t["id"] for t in act_l.get("tasks", [])] == [jl["job_id"]] and
      act_n.get("tasks") == [], str(act_l)[:120])
call("/api/jobs/" + jl["job_id"] + "/cancel", {})
# A1 taxonomy as a pure function, incl. zero-candidates vs failure
from backend.jobs import classify_outcome as _co  # noqa: E402
check("77 outcome taxonomy: five distinct honest states",
      _co([{"status": "available"}]) == "delivered" and
      _co([{"status": "empty"}, {"status": "empty"}]) == "zero_candidates" and
      _co([{"status": "available"}, {"status": "empty"}]) == "partial" and
      _co([{"status": "failed_generation"}]) == "generation_failed" and
      _co([]) == "no_artifacts")
# never over-claim: if an artifact disappears, results must DOWNGRADE and
# say so. The mock consensus arm may legitimately produce an empty table
# on some seeds (the run then already stores 'partial'), so the scenario
# is made DETERMINISTIC: the persisted claim is first pinned to a
# delivered run whose artifacts are all available (a row-count/
# status update - no re-run), then one REQUESTED artifact row is removed.
import sqlite3 as _s3
_db = ROOT / "data" / "batterytest.sqlite3"
_c = _s3.connect(str(_db))
_rows78 = _c.execute("SELECT strategy, status FROM artifacts WHERE job_id=?",
                     (jid_f,)).fetchall()
_with = len(_rows78)
_req78 = (((res_f.get("manifest") or {}).get("effective_config") or {})
          .get("ranking_artifacts") or [r[0] for r in _rows78 if r[0]])
_del78 = next((s for s in _req78
               if any(r[0] == s and r[1] == "available" for r in _rows78)),
              "evidence-consensus")
_res78 = json.loads(_c.execute(
    "SELECT result_json FROM jobs WHERE id=?", (jid_f,)).fetchone()[0])
_res78["outcome"] = "delivered"
_c.execute("UPDATE jobs SET result_json=?, outcome='delivered' WHERE id=?",
           (json.dumps(_res78, ensure_ascii=False), jid_f))
_c.execute("UPDATE artifacts SET status='available', rows=5 "
           "WHERE job_id=? AND strategy IS NOT NULL", (jid_f,))
_c.execute("DELETE FROM artifacts WHERE job_id=? AND strategy=?",
           (jid_f, _del78))
_c.commit()
_c.close()
_, res_drift = call("/api/results/" + jid_f)
check("78 a run that CLAIMED delivered but lost a requested artifact "
      "row (here %s) is served as 'partial' with the drift disclosed "
      "(was %d artifacts; stored result never re-runs the pipeline)"
      % (_del78, _with),
      res_drift["outcome"] == "partial" and "outcome_drift" in res_drift
      and _del78 in res_drift.get("outcome_drift", "")
      and _del78 in (res_drift.get("missing_artifacts") or []),
      str(res_drift["outcome"]) + " " + str(res_drift.get("outcome_drift"))[:80])
# read-only task token: opens GET task routes ONLY
_tok = json.loads(_s3.connect(str(_db)).execute(
    "SELECT value_json FROM settings WHERE key='task_token'").fetchone()[0])


def _tokget(path, token=None, auth=False):
    req = urllib.request.Request(BASE + path)
    if token:
        req.add_header("X-NeoAG-Task-Token", token)
    if auth:
        req.add_header("Authorization", AUTH_HEADER)
    try:
        with urllib.request.urlopen(req, timeout=10) as _r:
            return _r.status
    except urllib.error.HTTPError as _e:
        return _e.code


check("79 read-only task token: GET task routes ok, everything else 401",
      _tokget("/api/tasks", token=_tok) == 200 and
      _tokget("/api/tasks/" + jid_f + "/snapshot", token=_tok) == 200 and
      _tokget("/api/bootstrap", token=_tok) == 401 and
      _tokget("/api/conversations", token=_tok) == 401)

# ---------- 80-86 Milestone-4 Skills workspace (F3) --------------------
_, skl = call("/api/skills")
check("80 skills seeded: three categories, honest activation flags",
      len(skl["skills"]) == 3 and
      {s["category"] for s in skl["skills"]} == set(skl["categories"]) and
      all("active_in_new_sessions" in s for s in skl["skills"]),
      str([(s["slug"], s["active_in_new_sessions"]) for s in skl["skills"]]))
def _status(path, body=None):
    try:
        return call(path, body)[0]
    except urllib.error.HTTPError as e:
        return e.code


# M7 §5: the author supplies NAME + INSTRUCTIONS only; slug, category
# and frontmatter are internal. ONE Save validates, versions and
# applies the file for new chats; Disable really removes it again.
st_bad1 = _status("/api/skills", {"name": "gate",
                                  "instructions": "collision attempt"})
st_bad2 = _status("/api/skills", {"name": "", "instructions": "x"})
st_bad3 = _status("/api/skills", {"name": "Battery Probe NoFM",
                                  "instructions": "plain text, no fm"})
check("81 protected name + empty name refused; missing frontmatter "
      "auto-completed",
      st_bad1 == 400 and st_bad2 == 400 and st_bad3 == 201,
      f"{st_bad1}/{st_bad2}/{st_bad3}")
st_mk, mk82 = call("/api/skills", {"name": "Battery Probe",
                                   "instructions": "body one"})
check("82 ONE Save creates (name-derived slug) and applies it for "
      "new chats",
      st_mk == 201 and mk82.get("applied") and
      mk82["skill"]["slug"] == "battery-probe" and
      os.path.isfile("/tmp/neoag-battery-skills/battery-probe/SKILL.md"),
      str(mk82)[:160])
st_up, up83 = call("/api/skills/battery-probe/update",
                   {"name": "Battery Probe", "instructions": "body two"})
check("83 update bumps the version and re-applies in the same Save",
      st_up == 200 and up83["skill"]["version"] == 2 and
      up83.get("applied") and
      "body two" in open("/tmp/neoag-battery-skills/battery-probe/"
                         "SKILL.md").read(), str(up83)[:120])
_, sv = call("/api/skills/battery-probe/versions")
check("84 full version history is kept behind the single Save",
      len(sv.get("versions", [])) == 2, str(sv)[:120])
st_dis, _d85 = call("/api/skills/battery-probe/disable", {})
check("85 disable REMOVES the deployed file (new chats stop loading it)",
      st_dis == 200 and not os.path.exists(
          "/tmp/neoag-battery-skills/battery-probe/SKILL.md"),
      str(_d85)[:120])
st_en, _e85 = call("/api/skills/battery-probe/enable", {})
check("85b enable re-applies the current version",
      st_en == 200 and os.path.isfile(
          "/tmp/neoag-battery-skills/battery-probe/SKILL.md"))
st_tt = _status("/api/skills/battery-probe/test", {})
check("85c the inert Test route is gone (§5: removed, not hidden)",
      st_tt == 404, str(st_tt))
st_rs, rs = call("/api/skills/battery-probe/restore", {"version": 1})
_disk86 = open("/tmp/neoag-battery-skills/battery-probe/SKILL.md").read()
check("86 restore re-applies v1 content AS A NEW version (file == "
      "record)",
      st_rs == 200 and rs["skill"]["version"] == 3 and
      "body one" in _disk86 and "body two" not in _disk86,
      str(rs.get("skill", {}).get("version")))

# ---------- 87-95 Milestone-5 formal-job routing (PART 1, P0) ---------
def _tokpost(path, obj, token=None, auth=False):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 **({"X-NeoAG-Task-Token": token} if token else {}),
                 **({"Authorization": AUTH_HEADER} if auth else {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as _r:
            return _r.status, json.loads(_r.read().decode() or "{}")
    except urllib.error.HTTPError as _e:
        try:
            return _e.code, json.loads(_e.read().decode() or "{}")
        except Exception:
            return _e.code, {}


st87, r87 = _tokpost("/api/jobs", {"sample_id": "BATTERY-AGENT-1"},
                     token=_tok)
_jid87 = r87.get("job_id")
check("87 task token POST /api/jobs registers a job (202, blocked+approval)",
      st87 == 202 and bool(_jid87) and bool(r87.get("approval_id")),
      f"{st87} {str(r87)[:120]}")
st87b, t87 = call("/api/tasks/" + (_jid87 or "x"))
_ap87 = t87.get("approval_id")
check("88 token-created job starts BLOCKED behind the same approval card",
      t87.get("state") == "blocked" and bool(_ap87),
      str(t87)[:140])
st87c, aud87 = call("/api/audit")
_rows87 = aud87 if isinstance(aud87, list) else aud87.get("entries", [])
check("89 scoped-token creation is auditable (agent + scoped_token row)",
      any(a.get("actor") == "agent"
          and a.get("action") == "job.create.scoped_token"
          and a.get("object") == _jid87
          for a in _rows87),
      str(_rows87)[:140])
_st_chat = _tokpost("/api/chat", {"text": "x"}, token=_tok)[0]
_st_appr = _tokpost("/api/approvals/" + (_ap87 or "x"),
                    {"approve": True}, token=_tok)[0]
_st_mode = _tokpost("/api/agent/mode", {"mode": "real"}, token=_tok)[0]
check("90 token grants POST on /api/jobs ONLY (chat/approvals/mode 401)",
      _st_chat == 401 and _st_appr == 401 and _st_mode == 401,
      f"{_st_chat} {_st_appr} {_st_mode}")
st87d, _ = call(f"/api/approvals/{_ap87}", {"approve": True})
_fin87 = {}
for _ in range(180):
    _, t87f = call("/api/tasks/" + _jid87)
    if t87f.get("state") in ("completed", "failed", "cancelled"):
        _fin87 = t87f
        break
    for d in t87f.get("decisions") or []:
        # same decision-point ceremony a GUI job goes through
        if d.get("status") == "pending":
            opts = d.get("options") or []
            ch = next((o["id"] for o in opts if o.get("policy_default")),
                      opts[0]["id"] if opts and isinstance(opts[0], dict)
                      else None)
            if ch:
                call(f"/api/decisions/{d['id']}/resolve", {"choice": ch})
    time.sleep(0.5)
check("91 chat/GUI parity: agent-registered job completes through the "
      "same approval->runner path",
      st87d == 200 and _fin87.get("state") == "completed",
      str(_fin87)[:120])

_helper = "/tmp/neoag-battery-taskq/taskquery.py"
_hsrc = open(_helper).read() if os.path.exists(_helper) else ""
check("92 deployed helper refreshed with job submit/status/events "
      "(single sanctioned registration path)",
      "job submit" in _hsrc and "job events" in _hsrc
      and "_req(\"/api/jobs\"" in _hsrc, str(len(_hsrc)))
_e87 = dict(os.environ, NEOAG_WEB_BASE=BASE)
_r87a = subprocess.run([sys.executable, _helper, "job", "submit",
                        '{"sample_id": "BATTERY-HELPER-1"}'],
                       capture_output=True, text=True, env=_e87,
                       timeout=30)
_r87j = json.loads(_r87a.stdout or "{}")
_r87b = subprocess.run(
    [sys.executable, _helper, "job", "status", _r87j.get("job_id", "x")],
    capture_output=True, text=True, env=_e87, timeout=30)
check("93 helper end-to-end: job submit -> id, job status -> that task",
      _r87a.returncode == 0 and _r87j.get("job_id") and
      _r87b.returncode == 0 and
      json.loads(_r87b.stdout or "{}").get("id") == _r87j.get("job_id"),
      f"{_r87a.returncode} {_r87a.stdout[:100]}{_r87a.stderr[:100]}")

import importlib.util as _ilu
_gspec = _ilu.spec_from_file_location(
    "gate_m5", str(ROOT / "agent_workspace_assets" / ".codebuddy" /
                   "gate.py"))
_gate = _ilu.module_from_spec(_gspec)
_gspec.loader.exec_module(_gate)
_gcases = [
    ("open-neo run --mode execute --case-id C1", "deny"),
    ("open-neo run --mode plan --case-id C1 --outdir /tmp/p", None),
    ("bash -c \"open-neo run --mode execute\"", "deny"),
    ("grep -r 'open-neo run' docs/", None),
    ("python3 -m neoag.cli run-demo --outdir /tmp/d", "deny"),
    ("python3 -m neoag.open_neo.cli run --mode execute --outdir /tmp",
     "deny"),
    ("curl -s -X POST -d '{}' http://127.0.0.1:8760/open/run", "deny"),
    ("python3 -m neoag.controlled_execution.gateway --port 8760", "deny"),
    ("python3 .neoag/taskquery.py job submit '{\"sample_id\":\"S\"}'", None),
    ("open-neo doctor --json", None),
]
_gbad = [c for c, want in _gcases
         if (("deny" if (lambda d: d and d[0] == "deny")(
             _gate.decide({"tool_name": "Bash",
                           "tool_input": {"command": c}})) else None)
             != want)]
_gtok = _gate.decide({"tool_name": "Bash", "tool_input": {
    "command": "open-neo run --mode execute"}})
check("94 gate classifies formal launches DENY-FORMAL (no approval path, "
      "plan-mode and docs-greps stay allowed)",
      not _gbad and _gtok and "NEOAG_FORMAL_JOB_REQUIRED" in _gtok[1],
      str(_gbad)[:160])
import io as _io
import contextlib as _cl
import tempfile
_tmpa = tempfile.mkdtemp(prefix="neoag-routing-audit-")
_gate.APPROVALS = _tmpa
_ev = {"tool_name": "Bash", "tool_input": {
    "command": "open-neo run --mode execute --case-id X"},
    "session_id": "batt"}
import sys as _sys
_old_in, _old_out = _sys.stdin, _sys.stdout
_sys.stdin, _sys.stdout = _io.StringIO(json.dumps(_ev)), _io.StringIO()
try:
    _gate.main()
finally:
    _sys.stdin, _sys.stdout = _old_in, _old_out
_routing = os.path.join(_tmpa, "routing.jsonl")
check("95 every routing interception is audit-logged (class FORMAL_JOB)",
      os.path.isfile(_routing) and
      json.loads(open(_routing).readline())["class"] == "FORMAL_JOB",
      str(open(_routing).readline())[:140] if
      os.path.isfile(_routing) else "no routing.jsonl")
import shutil as _sh87
_sh87.rmtree(_tmpa, ignore_errors=True)

# ---------- 96-98 Milestone-5 scientific config redesign (PART 2) ----
st96, pv96 = call("/api/params")
_conc96 = {i.get("concern") for g in pv96["groups"] for i in g["items"]}
_mods96 = [g["module"] for g in pv96["groups"] if g.get("module")]
check("96 the SAME code-cited options are split into the four concerns "
      "(inputs/science/execution/managed) with per-module views",
      st96 == 200 and {"inputs", "science", "execution",
                       "managed"} <= _conc96 and
      len(_mods96) >= 3 and
      all(m.get("purpose") and m.get("tools") and
          m.get("tools_provenance") for m in _mods96),
      f"{sorted(str(c) for c in _conc96)} mods={len(_mods96)}")
check("97 module tools are DERIVED from the inventory capture (never "
      "asserted in the params module), capture timestamp disclosed",
      pv96.get("inventory_captured_at") and
      all(any(t for t in m["tools"]) for m in _mods96) and
      all(i.get("source") for g in pv96["groups"] for i in g["items"]),
      str(pv96.get("inventory_captured_at")))
_ncli = sum(1 for g in pv96["groups"] for i in g["items"] if i.get("cli"))
check("98 CLI mapping shown per option (only flags the code actually "
      "exposes)", _ncli >= 15, f"cli rows={_ncli}")

# ---------- 99-102 M7 §5 skills usability + dataset consent -----------
st99a, sk99 = call("/api/skills", {
    "name": "Battery Auto Slug Probe",
    "instructions": "Just the knowledge text - no frontmatter written."})
check("99 skills usability: slug generated from the name, frontmatter "
      "auto-filled, and the SAME Save applies it for new chats",
      st99a == 201 and
      sk99["skill"]["slug"] == "battery-auto-slug-probe" and
      sk99.get("applied") and
      sk99["skill"]["content"].startswith("---") and
      "name: battery-auto-slug-probe" in sk99["skill"]["content"],
      f"{st99a} {str(sk99)[:120]}")
check("100 the applied file on disk == the saved record (byte-for-byte)",
      open("/tmp/neoag-battery-skills/battery-auto-slug-probe/"
           "SKILL.md").read() == sk99["skill"]["content"])
_ds_cmd = ("wget -O /tmp/neoag-dl-consent-test/expr.h5ad "
           "https://figshare.com/ndownloader/files/12345")
_gd = _gate.decide({"tool_name": "Bash",
                    "tool_input": {"command": _ds_cmd}})
_gpaper = _gate.decide({"tool_name": "Bash", "tool_input": {
    "command": "curl -sL https://arxiv.org/pdf/2101.00001 "
               "-o /tmp/paper.pdf"}})
check("101 dataset download -> structured consent card (dataset, "
      "operation, scope, destination) while literature reads stay free",
      _gd and _gd[0] == "confirm" and
      _gd[2].get("class") == "dataset_download" and
      _gd[2].get("dataset") and _gd[2].get("destination") and
      all(w in _gd[1] for w in ("dataset=", "operation=", "destination=",
                                "scope")) and
      _gpaper is None,
      str(_gd)[:160])
_tmpa2 = tempfile.mkdtemp(prefix="neoag-scope-")
_gate.APPROVALS = _tmpa2
json.dump([{"dataset": "https://figshare.com/ndownloader/files/12345",
            "destination_root": "/tmp/neoag-dl-consent-test",
            "expires": time.time() + 60, "approval_id": "appr-x"}],
          open(os.path.join(_tmpa2, "dataset-scope.json"), "w"))
_in_scope = _gate.decide({"tool_name": "Bash", "tool_input": {
    "command": "wget -O /tmp/neoag-dl-consent-test/next-file.h5ad "
               "https://figshare.com/ndownloader/files/67890"}})
_out_scope = _gate.decide({"tool_name": "Bash", "tool_input": {
    "command": "wget -O /tmp/somewhere-else/x.h5ad "
               "https://figshare.com/ndownloader/files/67890"}})
_gate.APPROVALS = os.path.join(WS if (WS := "/data1/neoag/agents/neoag_agent_ws") else "", ".neoag-approvals")
check("102 consent is BOUNDED: sibling files inside the approved "
      "destination need no re-prompt; anything outside it still asks",
      _in_scope is None and _out_scope
      and _out_scope[0] == "confirm",
      f"in={_in_scope} out={bool(_out_scope)}")
_sh87.rmtree(_tmpa2, ignore_errors=True)

# ---------- 103-105 M5 PART 4: version chain + evidence context -------
from backend import evidence as _ev4                                  # noqa: E402
from backend import research as _res4                                  # noqa: E402
from backend import versions as _ver4                                  # noqa: E402
_none = lambda n: ([], "not-on-pypi", {})                              # noqa: E731
_bc = lambda n: ([], "not-on-bioconda", {})                            # noqa: E731
_vf = lambda r: {}                                                     # noqa: E731
_hd = lambda r: {"sha": "beef1234567890", "date": "2026-11-01",
                 "url": "h"}                                           # noqa: E731
_c_tags = _ver4.resolve("ToolX", repo="a/x", probes={
    "releases_tags": lambda r: {"releases": [],
                                "tags": ["v3.2.1", "3.1.0", "nightly-9"],
                                "error": None},
    "pypi": _none, "bioconda": _bc, "version_file": _vf,
    "head_commit": _hd}, budget={"core": 10})
_c_pre = _ver4.resolve("ToolY", repo="a/y", probes={
    "releases_tags": lambda r: {"releases": [
        {"tag": "2.0rc1", "prerelease": True,
         "published": "2026-09-01T00:00:00Z"},
        {"tag": "1.9", "prerelease": False,
         "published": "2026-01-01T00:00:00Z"}],
        "tags": ["2.0rc1", "1.9"], "error": None},
    "pypi": _none, "bioconda": _bc, "version_file": _vf,
    "head_commit": _hd}, budget={"core": 5})
_c_dev = _ver4.resolve("ToolZ", repo="a/z", probes={
    "releases_tags": lambda r: {"releases": [], "tags": [], "error": None},
    "pypi": _none, "bioconda": _bc, "version_file": _vf,
    "head_commit": _hd}, budget={"core": 9})
_c_bud = _ver4.resolve("ToolW", repo="a/w", probes={
    "releases_tags": lambda r: (_ for _ in ()).throw(
        AssertionError("budget-0 must not call the adapter")),
    "pypi": _none, "bioconda": _bc, "version_file": _vf,
    "head_commit": _hd}, budget={"core": 0})
check("103 version chain: tags fallback (no false unknown despite empty "
      "releases), stable-vs-prerelease split, commit fallback labelled "
      "DEVELOPMENT, budget-skipped steps keep their reason",
      _c_tags["display"].startswith("v3.2.1 (stable; git tag")
      and _c_pre["values"]["latest_stable"]["value"] == "1.9"
      and _c_pre["values"]["latest_release"]["value"] == "2.0rc1"
      and "DEVELOPMENT" in _c_dev["display"]
      and any("budget" in c["outcome"] for c in _c_bud["chain"]),
      _c_dev["display"][:60])

_r01 = _ev4.metric_rows(
    "MHCflurry",
    "On the ICBC cohort MHCflurry v1.4 achieved an AUROC of 0.91 against "
    "mass spectrometry eluted ligands over 12,000 peptides. Earlier work "
    "reports accuracy of 87%.",
    citation={"id": "PMID:1", "title": "MHCflurry improvements",
              "year": "2021", "url": "u", "source": "pubmed"},
    level="abstract")
check("104 performance evidence is CONTEXT-GATED: the contextual row "
      "keeps metric+dataset+version, the contextless number is quoted "
      "but excluded, and the cross-version caveat blocks credit transfer",
      len(_r01) >= 2 and _r01[0]["context_established"]
      and _r01[0]["dataset"] == "ICBC" and _r01[0]["value"] == "0.91"
      and not _r01[1]["context_established"]
      and "must NOT be credited" in _ev4.cross_version_caveat(_r01[0],
                                                              "1.9")
      and "1 of 2" in _ev4.comparability_note(_r01))

def _cand4(nm, role="named in retrieved title (pubmed)",
           cat="software (registry-verified)", ev=(), baseline=False,
           eligibility=None):
    # eligibility is set by the scientific screen (science.screen_
    # candidate) in the real pipeline; fixtures state it explicitly
    elig = eligibility or (
        "current baseline (not an alternative)" if baseline
        else "direct alternative")
    rel = ("direct-alternative" if elig == "direct alternative"
           else "wrapper" if elig.startswith("wrapper") else "unresolved")
    return {"name": nm, "task": "Presentation & APPM Scoring",
            "role_in_neoag": role, "category": cat,
            "eligibility": elig,
            "relationship": rel,
            "identity_status": "resolved",
            "relevance_basis": [],
            "eligibility_reasons": ["battery fixture"],
            "is_baseline": baseline,
            "comparable_for_replacement":
                elig == "direct alternative",
            "artifact_class": "algorithm (method implementation)",
            "verified_version": "1.0", "license": "MIT",
            "maintenance": "active (last push 2026-06-01)",
            "availability": ("found on PyPI, GitHub"
                             if cat == "software (registry-verified)"
                             else "Not verified"),
            "evidence_gaps": ["exact version",
                              "input/output format"],
            "installed_version": "n/a",
            "published_benchmark_evidence": list(ev)}
_cur4 = _cand4("mhcflurry", role="configured NeoAG workflow TOML",
               baseline=True)
_row4 = {"metric": "AUROC", "value": "0.9", "dataset": "ICBC",
         "verified": "confirm", "doc_cid": "DOI:fixture4",
         "context": {}, "context_established": False,
         "tested_version": "v1.0",
         "evidence_level": "abstract: author-reported"}
_t4 = {"id": "presentation", "label": "Presentation & APPM Scoring"}
_st4 = {"baseline": {"identified": True, "consensus": False,
                     "tools": [{"name": "mhcflurry",
                                "role": "current baseline",
                                "evidence": ["planner code"],
                                "pinned_version": None}],
                     "note": ""}}
_concl4 = [_res4._module_decision(_t4, a, _st4) for a in (
    [], [_cur4], [_cur4, _cand4("A")], [_cur4, _cand4("B", ev=[_row4])],
    [_cand4("C", cat="mentioned in literature only")])]
_tc4 = _res4._task_comparisons(
    [_t4], [_cur4, _cand4("A"), _cand4("B", ev=[_row4]),
            _cand4("W", eligibility="wrapper/integration (not an "
                                    "independent algorithm)")],
    {"presentation": _st4})
def _cnd155(nm, task, role, ev=(), cat="software (registry-verified)",
            baseline=False):
    elig = ("current baseline (not an alternative)" if baseline
            else "direct alternative")
    return {"name": nm, "task": task, "role_in_neoag": role,
            "category": cat,
            "eligibility": elig,
            "relationship": "direct-alternative" if not baseline
            else "unrelated",
            "identity_status": "resolved", "relevance_basis": [],
            "version_claimed": None, "license_claimed": None,
            "followups_open": False,
            "eligibility_reasons": ["battery fixture"],
            "is_baseline": baseline,
            "comparable_for_replacement":
                elig == "direct alternative",
            "artifact_class": "algorithm (method implementation)",
            "verified_version": "1.0 (stable; PyPI)", "license": "MIT",
            "maintenance": "active (last push 2026-06-01)",
            "availability": "found on PyPI", "installed_version": "n/a",
            "compatibility": "Not verified",
            "version_chain": {"values": {}},
            "published_benchmark_evidence": list(ev),
            "performance_summary": "x", "evidence_gaps": ["exact version"],
            "independent_validation": bool(ev)}
_inv155 = {"stages": [{"id": "presentation", "name": "P",
                       "current_tools": ["mhcflurry"],
                       "baseline": {"identified": True,
                                    "consensus": False,
                                    "tools": [{"name": "mhcflurry",
                                               "role": "current baseline",
                                               "evidence": ["planner"],
                                               "pinned_version": None}],
                                    "note": ""}},
                      {"id": "sv", "name": "S", "current_tools": []},
                      {"id": "reports", "name": "R",
                       "current_tools": []}],
           "limitations": [], "repo": "r", "code_verified": True,
           "git_commit": "x", "captured_at": "t"}
_rt155 = [{"id": "presentation", "label": "Presentation & APPM Scoring"},
          {"id": "sv", "label": "Sv"}]
_CTX155 = {"modality": "eluted-ligand benchmark",
           "sample_design": "held-out peptides",
           "ground_truth": "mass-spectrometry",
           "coverage": "117k peptides"}
_row155 = {"metric": "AUROC", "value": "0.9", "dataset": "ICBC",
           "verified": "confirm", "doc_cid": "DOI:alt155",
           "context": dict(_CTX155), "context_established": True,
           "tested_version": "v1.0",
           "evidence_level": "abstract: independent claim, confirmed"}
_rowb155 = {"metric": "AUROC", "value": "0.85", "dataset": "ICBC",
            "verified": "confirm", "doc_cid": "DOI:base155",
            "context": dict(_CTX155), "context_established": True,
            "tested_version": "v1.0",
            "evidence_level": "abstract: independent claim, confirmed"}
_rep155 = _res4._live_report(
    "res_t", {"targets": _rt155}, _inv155, {}, "completed",
    candidates=[_cnd155("mhcflurry", "Presentation & APPM Scoring",
                        "configured NeoAG workflow TOML",
                        ev=[_rowb155], baseline=True),
                _cnd155("ALT", "Presentation & APPM Scoring",
                        "named in title", ev=[_row155])],
    citations=[], resolved_targets=_rt155)
_cov155 = _rep155["coverage_accounting"]
_mc155 = _rep155["module_conclusions"]["presentation"]
check("105 evidence COVERAGE is accounted per inventory module "
      "(identified/researched/sufficient/thin/missing+reason); "
      "completion means coverage, not 'queries returned'; a TARGETED "
      "run is complete FOR ITS SCOPE and out-of-scope modules are "
      "labelled as such (never 'stopped early')",
      _cov155["identified_modules"] == 3 and
      _cov155["researched_modules"] == 2 and
      _cov155["completeness"] == "complete" and
      "presentation" in _cov155["sufficient_evidence"] and
      {m["id"] for m in _cov155["not_researched"]} == {"reports"}
      and all("scope" in m["reason"]
              for m in _cov155["not_researched"])
      and _rep155["status"] == "completed",
      str(_cov155)[:150])
check("106 every researched module gets a recommendation + an "
      "evidence-derived rationale + a concrete action, computed FROM "
      "stated evidence facts (M8 S9: the label is a pure function of "
      "the recorded facts - a verified direct pair whose alternative "
      "wins licenses 'Promising', and the label cannot contradict the "
      "numbers next to it); a module whose baseline was never "
      "established is honestly 'Review incomplete'; Open Questions are "
      "gone",
      _mc155["recommendation"] == "Promising candidate for further "
      "evaluation"
      and _mc155["conclusion"] == _mc155["recommendation"]
      and _mc155["evidence_facts"]["direct_alternatives"] == 1
      and _mc155["evidence_facts"]["verified_measurement_pairs"] == 1
      and _mc155["evidence_facts"]["direct_comparable_pairs"] == 1
      and any(x.get("winner") == "alternative" and x.get("class")
              == "direct" for x in _mc155["comparisons"])
      and _mc155["rationale"] and "validate" in _mc155["action"].lower()
      and _rep155["module_conclusions"]["sv"]["recommendation"]
      == "Review incomplete"
      and "open_questions" not in _rep155
      and _rep155["recommended_next_actions"]
      and _rep155["recommended_next_actions"][0].startswith("[presentation]"),
      str(_mc155)[:300])
_rep155b = _res4._live_report(
    "res_t2", {"targets": _rt155}, _inv155, {}, "cancelled",
    candidates=[], resolved_targets=_rt155, reached_ids=set())
check("107 a run stopped mid-way publishes NO premature conclusions: "
      "scheduled-but-unreached modules carry the stop reason + resume "
      "note (making the run honestly PARTIAL), while modules that were "
      "never in scope keep their scope label",
      _rep155b["module_conclusions"] == {}
      and all(("run stopped" in m["reason"] and "resume" in m["reason"])
              for m in _rep155b["coverage_accounting"]["not_researched"]
              if m["id"] in ("presentation", "sv"))
      and any(m["id"] == "reports" and "scope" in m["reason"]
              for m in _rep155b["coverage_accounting"]["not_researched"])
      and _rep155b["coverage_accounting"]["completeness"] == "partial")

_rowB4 = [r for r in _tc4[0]["rows"] if r["tool"] == "B"][0]
_rowA4 = [r for r in _tc4[0]["rows"] if r["tool"] == "A"][0]
check("108 recommendations use ONLY the fixed M8 five-label vocabulary "
      "and never invent a winner from one-sided evidence (a candidate "
      "number with no matched baseline number yields 'Recommend "
      "targeted benchmark', never 'Promising'); task comparisons put "
      "ONLY direct alternatives in rows, name wrappers separately, and "
      "flag every row whose measurement conditions are not stated",
      all(c["recommendation"] in _res4.CONCLUSION_VOCABULARY
          for c in _concl4)
      and [c["recommendation"] for c in _concl4] == [
          "No justified change identified",
          "No justified change identified",
          "Recommend targeted benchmark",
          "Recommend targeted benchmark",
          "Recommend targeted benchmark"]
      and [r["tool"] for r in _tc4[0]["rows"]] == ["A", "B"]
      and "AUROC=0.9" in _rowB4["evidence"]
      and "[conditions not stated]" in _rowB4["evidence"]
      and "[DOI:fixture4]" in _rowB4["evidence"]
      and "no measurement claim survived" in _rowA4["evidence"]
      and _tc4[0]["wrappers_integrations"] == ["W"]
      and _tc4[0]["baseline"]["identified"],
      str([(c["recommendation"],
            r["evidence"][:60]) for c, r in zip(_concl4, _tc4[0]["rows"])])[:220])

# ---------- 109-115 M5 PART 7: downloadable exports -----------------------
import io as _io4                                                # noqa: E402
import zipfile as _zip4                                          # noqa: E402
import tempfile as _tf4                                          # noqa: E402
from backend import exports as _exp4                             # noqa: E402


def _raw(path, token=None, auth=True):
    req = urllib.request.Request(BASE + path)
    if auth:
        req.add_header("Authorization", AUTH_HEADER)
    if token:
        req.add_header("X-NeoAG-Task-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


_expdir = ROOT / "data" / "exports-batterytest"
_st1, _hd1, _hb1 = _raw(f"/api/research/{rid}/export/html")
_st1b, _hd1b, _hb1b = _raw(f"/api/research/{rid}/export/html")
check("109 research HTML export: first download renders once + persists, "
      "re-download serves byte-identical pinned bytes from disk (never "
      "re-generated)",
      _st1 == 200 and _hd1.get("X-Export-Version") == "1"
      and "attachment" in _hd1.get("Content-Disposition", "")
      and _hd1.get("X-Export-Sha256") == hashlib.sha256(_hb1).hexdigest()
      and _st1b == 200 and _hb1b == _hb1
      and _hd1b.get("X-Export-Version") == "1"
      and (b"NeoAG scientific tool review" in _hb1
           or b"Example (mock) research digest" in _hb1)
      and any(p.is_file() for p in _expdir.rglob("*.html")),
      str(_st1) + " " + str(_hd1.get("X-Export-Version")))
_sj, _hdj, _bj = _raw(f"/api/research/{rid}/export/json")
_sx, _hdx, _bx = _raw(f"/api/research/{rid}/export/tsv")
_sp, _hdp, _bp = _raw(f"/api/research/{rid}/export/pdf")
_rj = json.loads(_bj or b"{}")
_rt = _bx.decode("utf-8", "replace")
check("110 JSON export carries the stored provenance + export envelope; "
      "TSV sections parse; PDF is a valid stdlib PDF that declares itself "
      "text-only",
      _sj == 200 and _rj["provenance"]["engine"] == "example-mock"
      and _rj["export"]["report_id"] == rid and _rj["comparison"]
      and _sx == 200 and "# ===== section: comparison" in _rt
      and all("\t" in ln for ln in _rt.splitlines()
              if ln and not ln.startswith("#"))
      and _sp == 200 and _bp.startswith(b"%PDF-1.")
      and _bp.rstrip().endswith(b"%%EOF") and b"text-only" in _bp,
      f"j{_sj} x{_sx} p{_sp}")
_c2 = _s3.connect(str(_db))
_rrow = _c2.execute("SELECT report_json FROM research WHERE id=?",
                    (rid,)).fetchone()
_rv = json.loads(_rrow[0])
_rv["disclaimer"] = _rv["disclaimer"] + " [amended for export versioning]"
_c2.execute("UPDATE research SET report_json=? WHERE id=?",
            (json.dumps(_rv, ensure_ascii=False), rid))
_c2.commit()
_c2.close()
_st1c, _hd1c, _hb1c = _raw(f"/api/research/{rid}/export/html")
_lv = call(f"/api/research/{rid}/exports")[1]["exports"]
check("111 a changed stored report yields export VERSION 2 pinned to the "
      "new content while version 1 stays listed and on disk (versioned, "
      "not overwritten)",
      _st1c == 200 and _hd1c.get("X-Export-Version") == "2"
      and _hb1c != _hb1 and b"amended for export versioning" in _hb1c
      and sorted(e["version"] for e in _lv if e["format"] == "html")
      == [1, 2]
      and len(list(_expdir.rglob("research-*.html"))) >= 2)
_szz, _hzz, _z1 = _raw(f"/api/results/{jid_f}/bundle")
_zf = _zip4.ZipFile(_io4.BytesIO(_z1))
_znames = _zf.namelist()
_zman = json.loads(_zf.read("manifest.json"))
_szz2, _hzz2, _z1b = _raw(f"/api/results/{jid_f}/bundle")
check("112 results bundle ZIP is built from persisted job state (result, "
      "decision history, immutable snapshot, artifacts, ranking tables, "
      "provenance), re-downloads byte-identically, and records its scope "
      "rules",
      _szz == 200 and _hzz.get("X-Export-Version") == "1"
      and _szz2 == 200 and _z1b == _z1
      and {"README.txt", "job.json", "result.json", "snapshot.json",
           "decisions.json", "artifacts.json", "manifest.json",
           "events.json"} <= set(_znames)
      and any(n.startswith("tables/") and n.endswith(".tsv")
              for n in _znames)
      and "fastq" in json.dumps(_zman["scope_rules"]).lower()
      and all(f.get("sha256") for f in _zman["files"])
      and b"raw sequencing/alignment" in _zf.read("README.txt"),
      str(_szz) + " " + str(sorted(_znames))[:160])
_c3 = _s3.connect(str(_db))
_c3.execute("INSERT INTO artifacts(id,job_id,created_at,type,name,"
            "strategy,path,status,rows,checksum,meta_json) "
            "VALUES('art-export-late',?, '2026-11-16T00:00:00','table',"
            "'late_artifact.tsv',NULL,'/tmp/x','available',1,'deadbeef',"
            "'{}')", (jid_f,))
_c3.commit()
_c3.close()
_szz3, _hzz3, _z3 = _raw(f"/api/results/{jid_f}/bundle")
_ljb = call(f"/api/results/{jid_f}/exports")[1]["exports"]
check("113 bundle re-versions ONLY when persisted job state changes "
      "(added artifact row -> version 2; old version still listed; the "
      "pipeline was never re-run to serve a download)",
      _szz3 == 200 and _hzz3.get("X-Export-Version") == "2"
      and _z3 != _z1 and
      sorted(e["version"] for e in _ljb) == [1, 2])
_sna, _, _ = _raw(f"/api/research/{rid}/export/json", auth=False)
_stk, _, _ = _raw(f"/api/research/{rid}/export/tsv", token=_tok)
_skb, _, _ = _raw(f"/api/results/{jid_f}/bundle", token=_tok)
_snc, _, _eb = _raw(f"/api/results/{jid6}/bundle")
check("114 exports are access-controlled (no credentials -> 401; the "
      "read-only task token may download what it can already read) and a "
      "non-completed run is refused with 409 rather than generated",
      _sna == 401 and _stk == 200 and _skb == 200 and _snc == 409
      and b"cancelled" in _eb, str(_sna) + str(_stk) + str(_snc))
_nd = _tf4.mkdtemp()
open(os.path.join(_nd, "keep.tsv"), "w").write("a\tb\n")
os.makedirs(os.path.join(_nd, "sub"))
open(os.path.join(_nd, "sub", "reads_R1.fastq.gz"), "wb").write(b"x" * 100)
open(os.path.join(_nd, "huge.tsv"), "wb").write(
    b"y" * (2 * 1024 * 1024 + 10))
_keep, _exc = _exp4.plan_native_files(_nd)
shutil.rmtree(_nd, ignore_errors=True)
check("115 bundle scope planner: raw FASTQ/BAM-class inputs and oversized "
      "files are EXCLUDED with recorded reasons - a .fastq.gz can never "
      "enter a results bundle regardless of size",
      [f["name"] for f in _keep] == ["keep.tsv"]
      and {e["name"] for e in _exc} == {"sub/reads_R1.fastq.gz",
                                        "huge.tsv"}
      and any("raw sequencing" in e["reason"] for e in _exc)
      and any("per-file cap" in e["reason"] for e in _exc),
      str(_keep) + " " + str(_exc)[:200])

# ---------- 116-120 M5 PART 6: benchmark jobs + discovery actions --------
def _post(path, body):
    """POST that also returns structured 4xx bodies (a refused benchmark
    brief is a result, not a transport error)."""
    try:
        return call(path, body)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


_BENCH_BRIEF = {
    "hypothesis": "a looser stability gate (5.8 vs 3.0) keeps borderline "
                  "peptides inside the weighted top-12 on the synthetic "
                  "pool",
    "tools": {"candidate": "NetMHCpan-4.1", "baseline": "netMHCcons"},
    "candidate_strategy": "weighted",
    "config": {"baseline_gate": 3.0, "candidate_gate": 5.8},
    "metrics": ["top_k_overlap", "rank_correlation_spearman",
                "candidate_set_fraction"],
    "compute_estimate": "seconds, in-process, same pool",
    "software_installation": "none - both arms already installed",
    "modifies_neoag_installation": False,
    "risks": ["synthetic pool only - not a biological claim"],
    "dataset": {"source": "demo_fixture"},
    "top_n": 12}
_s116a, _p116a = _post("/api/jobs", {"kind": "benchmark",
                                     "benchmark": {"hypothesis": "h"}})
_s116, _p116 = _post("/api/jobs", {"kind": "benchmark",
                                   "benchmark": _BENCH_BRIEF})
_jid116 = _p116.get("job_id", "")
_aid116 = _p116.get("approval_id", "")
_b116 = call("/api/bootstrap")[1]
_j116 = next((j for j in _b116["jobs"] if j["id"] == _jid116), {})
_a116 = next((a for a in _b116["approvals"] if a["id"] == _aid116), {})
check("116 a benchmark is a FORMAL job: brief validated at submit, the "
      "job starts blocked and its approval card carries the full brief "
      "(hypothesis, both tools, every metric, no-download statement) - "
      "nothing runs before approval",
      _s116a == 400 and "hypothesis" in str(_p116a) and \
      "invalid kind" not in str(_p116a)
      and _s116 == 202 and _j116.get("kind") == "benchmark"
      and _j116.get("state") == "blocked"
      and "looser stability gate" in _a116.get("action", "")
      and "NetMHCpan-4.1" in _a116.get("action", "")
      and "top_k_overlap" in _a116.get("action", "")
      and "No dataset downloads" in _a116.get("detail", ""),
      f"{_s116a}/{_s116} {_j116.get('state')}")
try:
    call(f"/api/approvals/{_aid116}", {"approve": True})
except urllib.error.HTTPError as e:
    check("117a approval accepted (prerequisite)", False, str(e.code))
_res117 = None
_t117 = time.time()
while time.time() - _t117 < 90:
    _s117, _, _b117 = _raw(f"/api/results/{_jid116}")
    if _s117 == 200:
        _res117 = json.loads(_b117)
        break
    time.sleep(0.5)
_m117 = (_res117 or {}).get("metrics") or {}
_c117 = _m117.get("computed") or {}
_r117 = (_res117 or {}).get("reproducibility") or {}
check("117 after approval the harness runs BOTH arms for real on the "
      "identical seeded pool: metrics are COMPUTED from the produced "
      "tables (never asserted), with a reproducibility record (seed, pool "
      "sha, environment, honest ground-truth statement) and files on disk",
      _res117 and _res117.get("engine") == "benchmark"
      and all(k in _c117 for k in ("top_k_overlap",
                                   "rank_correlation_spearman",
                                   "candidate_set_fraction"))
      and "COMPUTED" in _m117.get("note", "")
      and _r117.get("seed") and _r117.get("input_pool_sha256")
      and _r117.get("environment", {}).get("python")
      and "identical-input property" in _r117.get("ground_truth", "")
      and "MOCK FIXTURE" in _res117.get("disclaimer", "")
      and os.path.isfile(os.path.join(_res117["outdir"],
                                      "arm_baseline.tsv")),
      json.dumps({k: _c117.get(k, {}).get("value") for k in _c117})[:120])
_s118a, _p118a = _post("/api/jobs", {"kind": "benchmark", "benchmark": {
    **_BENCH_BRIEF, "dataset": {"source": "public_repo",
                                "id": "SOMETHING-ELSECAT"}}})
_s118b, _p118b = _post("/api/jobs", {"kind": "benchmark", "benchmark": {
    **_BENCH_BRIEF,
    "dataset": {"source": "public_repo", "id": "SOMETHING-ELSECAT",
                "download_size": "~80 MB (2x fastq.gz)",
                "destination": "/tmp/bench-data/SOMETHING-ELSECAT",
                "consent_approval_id": _aid116}}})
_cid118 = "appr-consent-batt-" + str(int(time.time()))
_c118 = _s3.connect(str(_db))
_c118.execute(
    "INSERT INTO approvals(id,created_at,job_id,action,risk,detail,"
    "state,decided_by) VALUES(?,?,?,?,?,?,'approved','operator')",
    (_cid118, "2026-11-16T00:00:00", "", "gate_op", "dataset",
     json.dumps({"structured": {"kind": "dataset_download",
                                "dataset": "SEED-BENCH-1"}})))
_c118.commit()
_c118.close()
_s118c, _p118c = _post("/api/jobs", {"kind": "benchmark", "benchmark": {
    **_BENCH_BRIEF,
    "dataset": {"source": "public_repo", "id": "SEED-BENCH-2",
                "download_size": "~40 MB (1x bam)",
                "destination": "/tmp/bench-data/SEED-BENCH-2",
                "consent_approval_id": _cid118}}})
_s118d, _p118d = _post("/api/jobs", {"kind": "benchmark", "benchmark": {
    **_BENCH_BRIEF,
    "dataset": {"source": "public_repo", "id": "SEED-BENCH-1",
                "download_size": "~40 MB (1x bam)",
                "destination": "/tmp/bench-data/SEED-BENCH-1",
                "consent_approval_id": _cid118}}})
check("118 dataset consent is enforced the PART 3 way: an external "
      "dataset needs an APPROVED dataset-download consent card naming "
      "that dataset; free prose mentioning 'dataset' (even an approved "
      "benchmark card) is NOT consent; the synthetic fixture stays free",
      _s118a == 400 and "consent" in str(_p118a)
      and _s118b == 400 and "consent" in str(_p118b)
      and _s118c == 400 and "consent" in str(_p118c)
      and _s118d == 202,
      f"{_s118a}/{_s118b}/{_s118c}/{_s118d}")
_c119 = _s3.connect(str(_db))
_rep119 = json.loads(_c119.execute(
    "SELECT report_json FROM research WHERE id=?", (rid2,)).fetchone()[0])
_c119.close()
_rep119["candidates"] = [{
    "name": "BatteryProbeTool", "license": "MIT",
    "verified_version": "9.9", "maintenance": "active",
    "artifact_class": "algorithm (method implementation)"}]
_rep119["inventory"] = {"stages": [{
    "id": "stability", "current_tools": ["battery-probe-cli"],
    "provenance": "battery fixture"}]}
_rep119["module_conclusions"] = {"stability": {
    "conclusion": "keep current tools"}}
_c119b = _s3.connect(str(_db))
_c119b.execute("UPDATE research SET report_json=? WHERE id=?",
               (json.dumps(_rep119, ensure_ascii=False), rid2))
_c119b.commit()
_c119b.close()
_s119a, _, _b119a = _raw(f"/api/research/{rid2}/compatibility"
                         "?candidate=BatteryProbeTool&target=stability")
_p119a = json.loads(_b119a)
_s119b, _, _b119b = _raw(f"/api/research/{rid2}/compatibility"
                         "?candidate=TotallyInventedTool&target=stability")
_s119c, _, _b119c = _raw(f"/api/research/{rid2}/adoption-plan"
                         "?candidate=batteryprobetool&target=stability")
_p119c = json.loads(_b119c)
_s119d, _, _ = _raw(f"/api/research/{rid2}/adoption-plan"
                    "?candidate=TotallyInventedTool")
check("119 discovery follow-up actions derive ONLY from the stored "
      "report: compatibility of a reported candidate reports presence/"
      "version/license without claiming interface verification; a "
      "candidate the report never retrieved is a 404 (never an invented "
      "row); the adoption plan APPLIES nothing and gates on fresh numbers",
      _s119a == 200 and "Not verified" in _p119a.get("interface", "")
      and "installed_version_probe" in _p119a.get("on_this_host", {})
      and _p119a.get("license_caution") and _p119a.get("current_stage",
                                                       {}).get(
                                                           "configured_tools")
      == ["battery-probe-cli"]
      and _s119b == 404 and b"never retrieved" in _b119b
      and _s119c == 200 and _p119c.get("applied") is False
      and "HUMAN" in _p119c.get("statement", "")
      and len(_p119c.get("steps") or []) >= 5
      and _p119c.get("verification_gate")
      and _s119d == 404,
      f"{_s119a}/{_s119b}/{_s119c}/{_s119d}")
_s120, _h120, _z120 = _raw(f"/api/results/{_jid116}/bundle")
_zf120 = _zip4.ZipFile(_io4.BytesIO(_z120))
_n120 = _zf120.namelist()
_s120b, _h120b, _z120b = _raw(f"/api/results/{_jid116}/bundle")
check("120 the results bundle ships the benchmark's reproducibility files "
      "under native/ (arm tables, reproducibility record, input pool) "
      "with the README stating benchmark runs are in scope, and "
      "re-downloads byte-identically",
      _s120 == 200
      and {"native/arm_baseline.tsv", "native/arm_candidate.tsv",
           "native/reproducibility.json",
           "native/input_pool.json"} <= set(_n120)
      and b"BENCHMARK" in _zf120.read("README.txt")
      and _h120.get("X-Export-Sha256") == _h120b.get("X-Export-Sha256")
      and _z120b == _z120
      and not any(n.lower().endswith((".fastq", ".fq", ".bam", ".sam",
                                      ".cram")) for n in _n120),
      f"{_s120} {str(sorted(_n120))[:150]}")
shutil.rmtree(os.path.join(str(ROOT / "data"), "benchmarks", _jid116),
              ignore_errors=True)
_s121, _p121 = _post("/api/jobs", {"kind": "benchmark",
                                   "benchmark": _BENCH_BRIEF})
_jid121 = _p121.get("job_id", "")
try:
    _sd121, _ = call("/api/approvals/" + _p121.get("approval_id", ""),
                     {"approve": False})
except urllib.error.HTTPError as e:
    _sd121 = e.code
_b121 = next((j for j in call("/api/bootstrap")[1]["jobs"]
              if j["id"] == _jid121), {})
_s121r, _, _b121r = _raw(f"/api/results/{_jid121}")
check("121 a DENIED benchmark dies there: the job is cancelled with the "
      "reason recorded, no result exists, no harness directory was ever "
      "created and the pipeline was never started",
      _sd121 == 200 and _b121.get("state") == "cancelled"
      and "denied" in str(_b121.get("error", ""))
      and _s121r == 409
      and not os.path.isdir(os.path.join(str(ROOT / "data"),
                                         "benchmarks", _jid121)),
      f"decide={_sd121} state={_b121.get('state')} results={_s121r}")

# ---- M6 PART C: consent card completeness + four-way answers -------
_s128a, _p128a = _post("/api/jobs", {"kind": "benchmark", "benchmark": {
    **{k: v for k, v in _BENCH_BRIEF.items()},
    "software_installation": "", "modifies_neoag_installation": False,
    "dataset": {"source": "public_repo", "id": "X-1"}}})
_s128b, _p128b = _post("/api/jobs", {"kind": "benchmark", "benchmark": {
    **_BENCH_BRIEF, "software_installation": ""}})
_s128c, _p128c = _post("/api/jobs", {"kind": "benchmark", "benchmark": {
    **_BENCH_BRIEF, "modifies_neoag_installation": True}})
_s128, _p128 = _post("/api/jobs", {"kind": "benchmark",
                                   "benchmark": _BENCH_BRIEF})
_a128 = next((a for a in call("/api/bootstrap")[1]["approvals"]
              if a["id"] == _p128.get("approval_id", "")), {})
_f128 = {}
try:
    _f128 = {f["label"]: f["value"]
             for f in json.loads(_a128.get("detail", "{}")).get(
                 "fields", [])}
except Exception:
    pass
check("128 M6 C the backend REFUSES consent-incomplete briefs (no "
      "download size / no installation statement / production "
      "modification claim) and a complete brief yields a consent card "
      "stating all nine fields: dataset+source, question, tools, size, "
      "destination, compute, metrics, installation, production impact",
      _s128a == 400 and "download_size" in str(_p128a)
      and _s128b == 400 and "software installation" in str(_p128b)
      and _s128c == 400 and "production" in str(_p128c)
      and _s128 == 202
      and all(lbl in _f128 for lbl in (
          "Dataset and source", "Scientific question", "Comparison tools",
          "Download size", "Storage destination", "Expected computing",
          "Evaluation metrics", "Software installation necessary",
          "Modifies current NeoAG installation")),
      f"{_s128a}/{_s128b}/{_s128c}/{_s128} fields={sorted(_f128)[:2]}")
_s129a, _p129a = _post("/api/jobs", {"kind": "benchmark",
                                     "benchmark": _BENCH_BRIEF})
try:
    _sd129, _pd129 = call("/api/approvals/" + _p129a.get("approval_id", ""),
                          {"decision": "limit_scope",
                           "scope": "CPU only, 5 samples max"})
except urllib.error.HTTPError as e:
    _sd129, _pd129 = e.code, {}
_j129 = next((j for j in call("/api/bootstrap")[1]["jobs"]
              if j["id"] == _p129a.get("job_id", "")), {})
_s129b, _p129b = _post("/api/jobs", {"kind": "benchmark",
                                     "benchmark": _BENCH_BRIEF})
try:
    _sd129b, _ = call("/api/approvals/" + _p129b.get("approval_id", ""),
                      {"decision": "literature_only"})
except urllib.error.HTTPError as e:
    _sd129b = e.code
_j129b = next((j for j in call("/api/bootstrap")[1]["jobs"]
               if j["id"] == _p129b.get("job_id", "")), {})
try:
    _sd129c, _ = call("/api/approvals/" + _p129b.get("approval_id", ""),
                      {"decision": "banana"})
except urllib.error.HTTPError as e:
    _sd129c = e.code
check("129 M6 C the four answers are DISTINCT recorded outcomes: "
      "limit_scope cancels the job quoting the allowed scope, "
      "literature_only cancels it naming the literature-only request, "
      "an unknown decision is a 400",
      _sd129 == 200 and _j129.get("state") == "cancelled"
      and "CPU only, 5 samples max" in str(_j129.get("error", ""))
      and "re-scoped" in str(_j129.get("error", ""))
      and _sd129b == 200 and _j129b.get("state") == "cancelled"
      and "literature-only" in str(_j129b.get("error", ""))
      and _sd129c == 400,
      f"{_sd129}/{_j129.get('state')}/{_sd129b}/{_sd129c}")
# limit_scope without a scope note is rejected (nothing runs, card
# stays pending) - the backend enforces the contract, not prose
_s129d, _p129d = _post("/api/jobs", {"kind": "benchmark",
                                     "benchmark": _BENCH_BRIEF})
try:
    _sd129d, _bd129d = call(
        "/api/approvals/" + _p129d.get("approval_id", ""),
        {"decision": "limit_scope"})
except urllib.error.HTTPError as e:
    _sd129d = e.code
    try:
        _bd129d = json.loads(e.read().decode() or "{}")
    except Exception:
        _bd129d = {}
_a129d = next((a for a in call("/api/bootstrap")[1]["approvals"]
               if a["id"] == _p129d.get("approval_id", "")), {})
check("130 M6 C limit_scope demands the scope text ('what will you "
      "allow') - without it the answer is a 400 whose error names the "
      "missing scope note, and the card is still pending; approved "
      "benchmark work stays a FORMAL task, never a silent production "
      "change",
      _sd129d == 400 and "scope" in str(_bd129d)
      and _a129d.get("state") == "pending",
      f"{_sd129d} state={_a129d.get('state')} body={str(_bd129d)[:80]}")

# ---- M7 PART 2: ONE data directory -> NATIVE discovery -> confirmed
# manifest -> unified submit, over synthetic-but-parseable fixtures
# under an authorized input root (m7_fixtures, no patient data).
sys.path.insert(0, str(ROOT / "tests"))
import m7_fixtures as _m7fx
_case_clean, _case_amb = _m7fx.ensure()
_s131, _p131 = call("/api/native/discover", {"data_dir": str(_case_clean)})
_flags131 = {i["flag"]: i["path"] for i in _p131.get("inputs", [])}
_sum131 = _p131.get("summary") or {}
check("131 M7 discovery over one data directory is the NATIVE scanner's "
      "own answer: every recognized file carries a role + category, the "
      "tumor/normal pair is read from the somatic VCF's genotype columns "
      "(not filenames), routes come from native routing, and a clean "
      "directory is READY with no invented ambiguity",
      _s131 == 200 and _p131.get("ok")
      and _p131["engine"]["detector"].startswith("neoag.open_neo.routing")
      and {"tumor_dna_bam", "normal_dna_bam", "somatic_vcf", "hla_file"}
      <= set(_flags131)
      and all(i.get("role") and i.get("category")
              for i in _p131.get("inputs", []))
      and _sum131.get("tumor_sample_id") == "TEST001_T"
      and _sum131.get("normal_sample_id") == "TEST001_N"
      and _p131.get("ready") and _p131.get("routes")
      and not _p131.get("ambiguous"),
      f"{_s131} {sorted(_flags131)}")
_s131b, _p131b = call("/api/native/discover",
                      {"data_dir": str(_case_amb)})
_amb131 = [a for a in _p131b.get("ambiguous", [])
           if a.get("field") == "normal_dna_bam"]
check("131b a genuine ambiguity (two candidate normal BAMs) comes back "
      "as ONE structured choice listing EVERY real candidate - never a "
      "silent pick, and never an alternative dropped by the parser",
      _s131b == 200 and not _p131b.get("ready") and len(_amb131) == 1
      and sorted(os.path.basename(c) for c in _amb131[0]["candidates"])
      == ["test002_normal_dna_run1.bam", "test002_normal_dna_run2.bam"],
      str([(a["field"], a["candidates"])
           for a in _p131b.get("ambiguous", [])])[:160])
_s131c = _status("/api/native/discover", {"data_dir": "/etc"})
_s131d = _status("/api/native/discover", {"data_dir": "some/relative"})
check("131c a directory outside the authorized roots and a relative "
      "path fail with a clear 400 - nothing is scanned and nothing is "
      "fabricated from an unreadable location",
      _s131c == 400 and _s131d == 400, f"{_s131c}/{_s131d}")
_pick132 = _amb131[0]["candidates"][0]
_s132a, _p132m = _post("/api/native/manifest",
                       {"data_dir": str(_case_amb),
                        "sample_id": "TEST002",
                        "picked": {"normal_dna_bam": _pick132}})
_mid132 = _p132m.get("id", "")
_mpath132 = (_p132m.get("proposal") or {}).get("manifest_path") or ""
_mfile132 = (json.loads(pathlib.Path(_mpath132).read_text())
             if _mpath132 and pathlib.Path(_mpath132).exists() else {})
_s132b, _p132b = _post("/api/jobs", {"engine": "native",
                                     "manifest_id": _mid132,
                                     "native_params":
                                         {"event_top_n": 7}})
_jid132 = _p132b.get("job_id", "")
_c132 = _s3.connect(str(_db))
_spec132 = json.loads(_c132.execute(
    "SELECT spec_json FROM jobs WHERE id=?", (_jid132,)).fetchone()[0])
_mstat132 = _c132.execute("SELECT status FROM manifests WHERE id=?",
                          (_mid132,)).fetchone()[0]
_c132.close()
# the confirmation gate stays real: an UNCONFIRMED manifest (the
# agent-side proposal path) still cannot submit.
_s132e, _p132e = _post("/api/manifests/propose",
                       {"description": "battery probe proposal"})
_s132f = _status("/api/jobs", {"engine": "native",
                               "manifest_id": _p132e.get("id", "")})
_np132 = _spec132.get("native_params") or {}
check("132 M7 the confirmation step IS the single source of input "
      "truth: /api/native/manifest writes the human-confirmed manifest "
      "in the NATIVE open-neo-sample-v1 schema honoring the pick, "
      "submission forwards THAT FILE (never re-derived inputs) plus the "
      "recorded override, the manifest links to the formal job, and an "
      "unconfirmed proposal is still refused (409)",
      _s132a == 201 and _p132m.get("status") == "confirmed"
      and _mfile132.get("schema_version") == "open-neo-sample-v1"
      and _mfile132.get("sample_id") == "TEST002"
      and _mfile132.get("inputs", {}).get("normal_dna_bam") == _pick132
      and _s132b == 202
      and _np132.get("sample_manifest") == _mpath132
      and _np132.get("event_top_n") == 7
      and _np132.get("sample_id") == "TEST002"
      and _mstat132 == "linked"
      and _s132e == 201 and _s132f == 409,
      f"{_s132a}/{_s132b}/{_s132f} mstat={_mstat132} "
      f"params={sorted(_np132)}")
_s133a, _p133a = _post("/api/jobs/native/preview",
                       {"manifest_id": "man-does-not-exist"})
_s133b, _p133b = _post("/api/jobs", {"engine": "native",
                                     "synthetic_demo": True})
_c133 = _s3.connect(str(_db))
_spec133 = json.loads(_c133.execute(
    "SELECT spec_json FROM jobs WHERE id=?",
    (_p133b.get("job_id", ""),)).fetchone()[0])
_c133.close()
check("133 M6 D honesty edges: a preview with a nonexistent manifest is "
      "a 400 (no invented plan), and the bundled synthetic demo is an "
      "EXPLICIT data choice that still lands in the same formal job "
      "pipeline (blocked until approval, native demo profile)",
      _s133a == 400 and "manifest" in str(_p133a)
      and _s133b == 202
      and _spec133.get("native_profile") == "demo",
      f"{_s133a}/{_s133b}")

# ---- M6 PART E: structured science controls + server-side validation --
_s134, _p134 = call("/api/params")
_sci134 = _p134.get("science") or []
_info134 = _p134.get("science_info") or []
_ids134 = {c["id"] for c in _sci134}
_good134 = (_s134 == 200 and len(_sci134) >= 5
    and all(c["control"] in ("radio", "number", "checkbox")
            for c in _sci134)
    and all(c.get("options") for c in _sci134 if c["control"] == "radio")
    and all(isinstance(c.get("min"), int) and isinstance(c.get("max"), int)
            and c["min"] < c["max"]
            for c in _sci134 if c["control"] == "number")
    and all(c.get("evidence") and c.get("affects") and "native_default" in c
            for c in _sci134)
    and {"event_top_n", "candidate_top_n", "genome_build",
         "rna_quant_method", "automatic_tool_policy",
         "skip_dna_sv", "skip_bam_matcher"} <= _ids134
    and _info134 and all("py:" in i["evidence"] for i in _info134)
    # no control is a free-text field: every one is a fixed choice, a
    # bounded number or a checkbox (brief E: no unvalidated free text)
    and not any(c["control"] not in ("radio", "number", "checkbox")
                for c in _sci134))
check("134 M6 E the science panel is a curated STRUCTURED set: every "
      "control is a fixed-choice radio, a bounded number or a checkbox "
      "(never free text), each cites the deployed code and its native "
      "default, the code-decided list carries real file:line citations, "
      "and the set matches the keys the Web layer forwards", _good134,
      f"{_s134} ids={sorted(_ids134)} info={len(_info134)}")
_s135a, _p135a = _post("/api/jobs", {"engine": "native",
                                     "synthetic_demo": True,
                                     "native_params":
                                         {"assay_type": "banana"}})
_s135b, _p135b = _post("/api/jobs", {"engine": "native",
                                     "synthetic_demo": True,
                                     "native_params":
                                         {"candidate_top_n": "abc"}})
_s135c, _p135c = _post("/api/jobs", {"engine": "native",
                                     "synthetic_demo": True,
                                     "native_params":
                                         {"event_top_n": 99999}})
_s135d, _p135d = _post("/api/jobs/native/preview",
                       {"native_params": {"sample_id": "X",
                                          "genome_build": "GRCh99"}})
_s135, _p135 = _post("/api/jobs", {"engine": "native",
                                   "synthetic_demo": True,
                                   "native_params":
                                       {"rna_quant_method": "salmon",
                                        "candidate_top_n": 50,
                                        "skip_dna_sv": True}})
_spec135 = {}
if _p135.get("job_id"):
    _c135 = _s3.connect(str(_db))
    _spec135 = json.loads(_c135.execute(
        "SELECT spec_json FROM jobs WHERE id=?",
        (_p135["job_id"],)).fetchone()[0])
    _c135.close()
check("135 M6 E the BACKEND (not the form) validates every scientific "
      "override: a fake choice, a non-integer and an out-of-range top-N "
      "are all refused with the offending key named, the preview path "
      "validates too, and a fully valid override set is accepted and "
      "reaches the run spec",
      _s135a == 400 and "assay_type" in str(_p135a)
      and _s135b == 400 and "candidate_top_n" in str(_p135b)
      and _s135c == 400 and "event_top_n" in str(_p135c)
      and _s135d == 400 and "genome_build" in str(_p135d)
      and _s135 == 202
      and _spec135.get("native_params", {}).get("rna_quant_method")
      == "salmon"
      and int(_spec135.get("native_params", {}).get("candidate_top_n",
                                                    0)) == 50
      and _spec135.get("native_params", {}).get("skip_dna_sv") is True,
      f"{_s135a}/{_s135b}/{_s135c}/{_s135d}/{_s135}")

# ---- M6 PART F: conversation menu + protected mode switch -------------
_s136, _p136 = call("/api/conversations", {"title": "MENU-TEST"})
_cid136 = _p136["conversation"]["id"]
_s136a, _p136a = call(f"/api/conversations/{_cid136}/flag",
                      {"pinned": True})
_s136b, _p136b = call("/api/conversations")
_order136 = [c["id"] for c in _p136b["conversations"]]
_s136c, _p136c = call(f"/api/conversations/{_cid136}/flag",
                      {"archived": True})
_s136d, _p136d = call("/api/conversations")
_arched136 = [c for c in _p136d["conversations"]
              if c["id"] == _cid136]
try:
    _s136e, _p136e = call(f"/api/conversations/{_cid136}/flag",
                          {"pinned": "yes"})
except urllib.error.HTTPError as _e136:
    _s136e = _e136.code
    try:
        _p136e = json.loads(_e136.read().decode() or "{}")
    except Exception:
        _p136e = {}
try:
    _s136f, _p136f = call(f"/api/conversations/{_cid136}/flag", {})
except urllib.error.HTTPError as _e136f:
    _s136f = _e136f.code
    try:
        _p136f = json.loads(_e136f.read().decode() or "{}")
    except Exception:
        _p136f = {}
check("136 M6 F the conversation menu backend: flag endpoint pins a chat "
      "to the TOP of the list, archiving keeps the row RETURNED with its "
      "archived marker (so Unarchive stays possible), a non-boolean flag "
      "and an empty flag request are both refused",
      _s136 == 201
      and _s136a == 200 and _p136a["conversation"]["pinned"] == 1
      and _order136 and _order136[0] == _cid136
      and _s136c == 200 and _s136d == 200
      and _arched136 and _arched136[0]["archived"] == 1
      and _s136e == 400 and _s136f == 400,
      f"{_s136a}/{_order136[:1]}/{_s136c}/{_s136e}/{_s136f}")
_s137a, _p137a = call("/api/agent/mode", {"mode": "real"})  # idle: allowed
_c137 = _s3.connect(str(_db))
_c137.execute("UPDATE jobs SET state='running' WHERE id="
              "(SELECT id FROM jobs ORDER BY rowid DESC LIMIT 1)")
_c137.commit()
_c137.close()
try:
    _s137b, _p137b = call("/api/agent/mode", {"mode": "mock"})
except urllib.error.HTTPError as _e137b:
    _s137b = _e137b.code
    try:
        _p137b = json.loads(_e137b.read().decode() or "{}")
    except Exception:
        _p137b = {}
_mode_during137 = call("/api/bootstrap")[1].get("agent_mode")
_s137c, _p137c = call("/api/agent/mode", {"mode": "real"})  # same = no-op
_c137 = _s3.connect(str(_db))
_c137.execute("UPDATE jobs SET state='cancelled' WHERE state='running'")
_c137.commit()
_c137.close()
_s137d, _p137d = call("/api/agent/mode", {"mode": "mock"})
check("137 M6 F switching agent mode is PROTECTED AT THE BACKEND: a "
      "switch away from the current mode is refused (409, reason names "
      "the in-flight work) while any job runs, a no-op switch stays "
      "allowed, and after the run ends the switch goes through; the "
      "battery keeps finishing in mock mode",
      _s137a == 200 and _s137b == 409 and "flight" in str(_p137b)
      and _mode_during137 == "real"
      and _s137c == 200 and _s137d == 200,
      f"{_s137a}/{_s137b}/{_s137c}/{_s137d}")

# ---- M6 A1/A2 candidate-identity contract (offline units) ----
# Negative fixture, 2026-11-17 LIVE study: an abstract that introduced
# the tool "PepGen" also yielded the allele string "DQA101" and the
# virus "CoV-2" through the shape path; both inherited the
# introduced-as-software privilege and biology-flavoured wording then
# promoted them to 'direct alternative'. The fixes under test: (1) the
# intro cue is attributed to the ONE name it introduces, (2)
# gene/allele-shaped names cannot be minted as tools at extraction,
# (3) the screen promotes on a SOFTWARE CLAIM only, not on domain +
# operation + biology vocabulary alone.
from backend import science as _sci138        # noqa: E402
_prof138 = _sci138.profile_for("presentation", "presentation")
_rec138 = {
    "title": "PepGen: conditional generation of peptides for MHC binding",
    "abstract": ("Here we present PepGen, a tool for prioritizing "
                 "candidate peptides. We evaluated binding to "
                 "HLA-DQA1*01 alleles and validated SARS-CoV-2 "
                 "specific epitopes with peptide binding assays."),
    "source": "europepmc"}
_m138 = {x["name"]: x for x in _sci138.extract_tool_mentions([_rec138],
                                                             _prof138)}
_shape_junk = [k for k in _m138
               if _sci138.GENE_SHAPE.match(k)
               or _sci138.ALLELE_SHAPE.match(
                   k.replace("-", "").split("*")[0]) and "*" in k]
_cov138 = _sci138.screen_candidate(
    "CoV-2", "PepGen: conditional generation of peptides for MHC "
    "binding", profile=_prof138, has_naming_source=False)
_mm138 = _sci138.screen_candidate(
    "MultiMHC", "MultiMHC is a deep learning model for peptide-MHC "
    "binding prediction", profile=_prof138)
_nm138 = _sci138.screen_candidate(
    "PepGen", "conditional generation of peptides for MHC binding",
    profile=_prof138, has_naming_source=True)
check("138 M6 A1/A2 candidate identity (regression: allele/virus names "
      "from a foreign intro cue): gene/allele-shaped names are never "
      "minted as tools; the intro-cue privilege belongs to the ONE "
      "name introduced (PepGen), co-occurring mentions get none; a "
      "domain+operation description WITHOUT a software claim is 'not a "
      "direct alternative', while a method signal or a genuine naming "
      "publication still promotes (no over-rejection)",
      "PepGen" in _m138 and _m138["PepGen"]["method_context"]
      and not _shape_junk
      and not any(_sci138.GENE_SHAPE.match(k) for k in _m138)
      and all(not v["method_context"] for k, v in _m138.items()
              if k != "PepGen")
      and _cov138["eligibility"] == "insufficient information to classify"
      and _cov138["comparable_for_replacement"] is False
      and _mm138["eligibility"] == "direct alternative"
      and _nm138["eligibility"] == "direct alternative",
      f"mentions={list(_m138)} cov={_cov138['eligibility']} "
      f"mm={_mm138['eligibility']} nm={_nm138['eligibility']}")

_s127, _p127 = call("/api/settings", {"top_n": 7,
                                      "task_token": "evil-token",
                                      "gate_grants": {"anything": True}})
_c127 = _s3.connect(str(_db))
_tok_after = json.loads(_c127.execute(
    "SELECT value_json FROM settings WHERE key='task_token'").fetchone()[0])
_c127.close()
check("127 the demo control panel writes ONLY whitelisted UI keys: a "
      "settings POST cannot reach the task token or any other stored "
      "setting (auth material is not user-editable), and the read-only "
      "token keeps working untouched",
      _s127 == 200 and _p127.get("top_n") == 7
      and "task_token" not in _p127 and "gate_grants" not in _p127
      and _tok_after == _tok
      and _tokget("/api/tasks", token="evil-token") == 401
      and _tokget("/api/tasks", token=_tok) == 200,
      f"{_s127} tok_changed={_tok_after != _tok}")
call("/api/settings", {"top_n": 12})

print()
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", ", ".join(FAIL))
    raise SystemExit(1)
print("ALL_PHASE_B_CHECKS_PASSED")
