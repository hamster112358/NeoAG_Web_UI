#!/usr/bin/env python3
"""Milestone 7 acceptance tests A-G (Mandate section 10).

Runs against an ISOLATED server (fresh DB, own port 8796, throwaway
skills/task dirs) so nothing here touches live demo state. The input
fixtures (m7_fixtures.ensure) are synthetic-but-parseable directories
under the agent workspace, inside the authorized input roots.

 A  input discovery on a real directory (native detector, evidence)
 B  invalid / unauthorized / empty directories fail honestly
 C  scientific configuration shape + server-side re-validation
 D  native submit journey: preview (real planner) -> submit -> job id
    -> unified task -> approval card -> cancel
 E  Skills journey (two-field Save/apply/disable) + UI contract
 F  agent delivery recovery (delegates to verify_turn_recovery.py)
 G  research run = unified task + conclusion-first report

Run: python3 tests/verify_m7_acceptance.py
"""
import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import m7_fixtures  # noqa: E402  (inline fixture generator)
PORT = 8796
BASE = f"http://127.0.0.1:{PORT}"
FIXDIR, FIXAMB = m7_fixtures.ensure()
FAILS = []

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

try:
    (ROOT / "data" / "m7acceptance.sqlite3").unlink()
except OSError:
    pass
_srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="m7acceptance.sqlite3",
             NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-m7acc-skills",
             NEOAG_TASKQ_DIR="/tmp/neoag-m7acc-taskq"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _cleanup():
    _srv.terminate()
    try:
        _srv.wait(timeout=8)
    except Exception:
        _srv.kill()
    try:
        (ROOT / "data" / "m7acceptance.sqlite3").unlink()
    except OSError:
        pass
    for d in ("/tmp/neoag-m7acc-skills", "/tmp/neoag-m7acc-taskq",
              str(m7_fixtures.AGENT_WS / "m7acc-empty")):
        shutil.rmtree(d, ignore_errors=True)


import atexit  # noqa: E402
atexit.register(_cleanup)


def call(method, path, body=None):
    req = urllib.request.Request(
        BASE + path, data=(json.dumps(body).encode()
                           if body is not None else None),
        method=method,
        headers={"Content-Type": "application/json",
                 "Authorization": AUTH_HEADER})
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except urllib.error.URLError:
        # server still booting (or just restarted): a retry-able
        # "not up yet", never a crash of the whole suite
        return 0, {}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{str(extra)[:180]}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# wait for boot
for _ in range(60):
    st, _b = call("GET", "/api/bootstrap")
    if st == 200:
        break
    time.sleep(1)
else:
    print("server did not start")
    sys.exit(1)

# ================= TEST A: discovery on a real directory ================
st, d = call("POST", "/api/native/discover", {"data_dir": str(FIXDIR)})
check("A1 discovery accepted on the real directory", st == 200,
      f"{st} {str(d)[:150]}")
check("A2 detector IS NeoAG's native one",
      (d.get("engine") or {}).get("detector", "").startswith(
          "neoag.open_neo.routing"), d.get("engine"))
flags = {i.get("flag") for i in d.get("inputs", [])}
check("A3 per-file classification from native routing (role + "
      "category on every row)",
      {"tumor_dna_bam", "normal_dna_bam", "tumor_rna_bam",
       "somatic_vcf", "hla_file"} <= flags and all(
          i.get("role") and i.get("category")
          for i in d.get("inputs", [])), sorted(flags))
check("A4 tumor/normal read from VCF evidence (not name guesses)",
      d["summary"].get("tumor_sample_id") == "TEST001_T" and
      d["summary"].get("normal_sample_id") == "TEST001_N",
      d.get("summary"))
check("A5 clean directory is READY (no ambiguity, routes present)",
      d.get("ready") and d.get("routes") and not d.get("ambiguous"),
      str({k: d.get(k) for k in ("ready", "missing")})[:120])
st, amb = call("POST", "/api/native/discover",
               {"data_dir": str(FIXAMB)})
amb_fields = {a["field"] for a in amb.get("ambiguous", [])}
check("A6 genuinely ambiguous dir -> STRUCTURED choices, not ready",
      st == 200 and not amb.get("ready") and amb_fields and all(
          a.get("candidates") for a in amb.get("ambiguous", [])),
      str(sorted(amb_fields)))
picked = {}
for a in amb.get("ambiguous", []):
    c0 = a["candidates"][0]
    # candidates are plain file paths (structured choice = pick one)
    picked[a["field"]] = c0 if isinstance(c0, str) else c0["path"]
st, m = call("POST", "/api/native/manifest",
             {"data_dir": str(FIXAMB), "sample_id": "TEST002",
              "picked": picked})
man_ok = (st == 201 and pathlib.Path(
    m["proposal"]["manifest_path"]).exists()
    if st == 201 else False)
body = json.loads(pathlib.Path(
    m["proposal"]["manifest_path"]).read_text()) if man_ok else {}
check("A7 confirmed manifest written in the NATIVE schema, honoring "
      "the human pick", man_ok and
      body.get("schema_version") == "open-neo-sample-v1" and
      body.get("sample_id") == "TEST002", str(m)[:150])

# ================= TEST B: invalid inputs fail honestly =================
st, e1 = call("POST", "/api/native/discover", {"data_dir": "some/relative"})
check("B1 relative path refused", st == 400 and "absolute" in
      str(e1), f"{st} {e1}")
st, e2 = call("POST", "/api/native/discover",
              {"data_dir": "/data1/neoag/no/such/dir"})
check("B2 missing directory refused", st == 400, f"{st} {e2}")
st, e3 = call("POST", "/api/native/discover", {"data_dir": "/etc"})
check("B3 directory outside the authorized roots refused",
      st == 400 and "authorized" in str(e3), f"{st} {e3}")
_empty = m7_fixtures.AGENT_WS / "m7acc-empty"
_empty.mkdir(exist_ok=True)
st, d4 = call("POST", "/api/native/discover",
              {"data_dir": str(_empty)})
st2, e4 = call("POST", "/api/native/manifest",
               {"data_dir": str(_empty), "sample_id": "X"})
check("B4 empty directory: discovery honest, manifest refused",
      st == 200 and not d4.get("ready") and st2 == 400,
      f"{st}/{st2} {str(e4)[:100]}")
bad_pick = {a["field"]: "/etc/passwd"
            for a in amb.get("ambiguous", [])} or {"x": "/etc/passwd"}
st, e5 = call("POST", "/api/native/manifest",
              {"data_dir": str(FIXAMB), "sample_id": "TEST002",
               "picked": bad_pick})
check("B5 a 'pick' pointing outside the scanned dir refused",
      st == 400 and "outside" in str(e5), f"{st} {e5}")

# ============== TEST C: scientific configuration shape ==================
st, pr = call("GET", "/api/params")
ctrl = pr.get("science", [])
check("C1 only verified scientific controls offered (small set)",
      st == 200 and 0 < len(ctrl) <= 8 and all(
          c["control"] in ("radio", "number", "checkbox")
          for c in ctrl), f"n={len(ctrl)}")
check("C2 every control cites deployed code + native default",
      all(c.get("evidence") and "native_default" in c for c in ctrl))
check("C3 no free-text/path knobs in the science set",
      not any(c["control"] not in ("radio", "number", "checkbox")
              or "/" in str(c.get("id")) for c in ctrl))
raw_html = urllib.request.urlopen(
    urllib.request.Request(BASE + "/", headers={
        "Authorization": AUTH_HEADER}), timeout=30).read().decode()
check("C4 config panel shows 'Scientific configuration: NeoAG "
      "defaults' by default (overrides are the exception)",
      "Scientific configuration" in raw_html and "NeoAG defaults"
      in raw_html)
st, e6 = call("POST", "/api/jobs",
              {"engine": "native", "manifest_id": m["manifest"]["id"]
               if "manifest" in m else m.get("id"),
               "native_params": {"event_top_n": 999999}})
check("C5 out-of-range override refused SERVER-SIDE before any job "
      "exists", st == 400, f"{st} {e6}")

# ============== TEST D: native submit journey ===========================
mid = m.get("manifest", {}).get("id") or m.get("id")
st, plan = call("POST", "/api/jobs/native/preview", {"manifest_id": mid})
check("D1 preview runs the REAL native planner on the confirmed "
      "manifest", st == 200 and (plan.get("steps") or
                                 plan.get("run_id") or
                                 plan.get("status")), f"{st} {plan}")
check("D2 planner answers carry the run identity + honest gaps",
      bool(plan.get("run_id")) and isinstance(
          plan.get("missing_evidence", []), list), str(plan)[:150])
st, job = call("POST", "/api/jobs",
               {"engine": "native", "manifest_id": mid,
                "native_params": {"event_top_n": 5}})
jid = job.get("job_id", "")
check("D3 submit through the unified endpoint returns ONE Job ID",
      st in (200, 201, 202) and jid, f"{st} {job}")
st, tk = call("GET", "/api/tasks")
check("D4 the job is registered in the unified task system",
      any(t["id"] == jid for t in tk.get("tasks", [])))
st, jt = call("GET", f"/api/tasks/{jid}")
snap = jt.get("spec") or {}
check("D5 the run records the manifest + the recorded override",
      "event_top_n" in json.dumps(jt) and "manifest" in json.dumps(jt)
      .lower(), str(jt)[:150])
appr = (call("GET", "/api/bootstrap")[1].get("approvals") or [])
check("D6 execute-class work stops at a human approval card",
      any(a.get("job_id") == jid and a.get("state") == "pending"
          for a in appr) or jt.get("state") in ("pending_approval",
                                                "queued", "blocked"),
      str([a.get("job_id") for a in appr])[:120])
st, _c = call("POST", f"/api/jobs/{jid}/cancel", {})
check("D7 the task can be cancelled from the same unified surface",
      st == 200, f"{st} {_c}")

# ============== TEST E: Skills journey + UI contract ====================
st, sk = call("POST", "/api/skills", {
    "name": "Acceptance report style",
    "instructions": "Lead every report with the conclusion."})
check("E1 New Skill form fields only (name + instructions) apply "
      "immediately", st == 201 and sk.get("applied") and
      sk["skill"]["slug"] == "acceptance-report-style", f"{st} {sk}")
rows = call("GET", "/api/skills")[1]["skills"]
row = next((r for r in rows if r["slug"] == "acceptance-report-style"),
           {})
check("E2 card data available: description + enabled + applied state",
      bool(row.get("description")) and row.get("enabled") and
      row.get("active_in_new_sessions"), str(row)[:150])
call("POST", "/api/skills/acceptance-report-style/disable", {})
gone = not pathlib.Path("/tmp/neoag-m7acc-skills/"
                        "acceptance-report-style/SKILL.md").exists()
check("E3 Disable really removes it from new sessions", gone)
appjs = urllib.request.urlopen(urllib.request.Request(
    BASE + "/static/app.js", headers={
        "Authorization": AUTH_HEADER}), timeout=30).read().decode()
check("E4 normal UI has no Slug/Category fields, no separate Deploy "
      "or Test control",
      "sk-slug" not in appjs and "sk-cat" not in appjs
      and "skill-deploy" not in appjs and "data-stest" not in appjs
      and "data-sdep" not in appjs)
check("E5 the editor states 'Changes apply to new chats.'",
      "Changes apply to new chats." in appjs)
check("E6 advanced fold keeps version history + raw text",
      "Version history" in appjs and "Raw SKILL.md" in appjs)

# ============== TEST F: agent delivery recovery =========================
r = subprocess.run([sys.executable, "-u", str(
    ROOT / "tests" / "verify_turn_recovery.py")],
    capture_output=True, text=True, timeout=400)
check("F1 real-agent delivery recovery suite passes (settled-session, "
      "reply-refused, silent-settle, zombie, no-loop)",
      r.returncode == 0 and "ALL PASS" in r.stdout,
      r.stdout[-200:] + r.stderr[-200:])

# ============== TEST G: research = task, report leads with conclusions ==
st, sub = call("POST", "/api/research",
               {"scope": "targeted", "targets": ["presentation"],
                "engine": "example"})
rid = sub.get("research_id", "")
t0 = time.time()
while time.time() - t0 < 90:
    rrow = call("GET", f"/api/research/{rid}")[1]
    if rrow.get("status") not in ("queued", "running"):
        break
    time.sleep(2)
rep = (rrow.get("report") or {})
check("G1 research run completes as a background task",
      st == 202 and rid and rrow.get("status") == "completed",
      f"{st} {rrow.get('status')}")
check("G2 the run is listed in the unified task query",
      any(t["id"] == rid and t["kind"] == "discovery"
          for t in call("GET", "/api/tasks?kind=research")[1].get(
              "tasks", [])))
check("G3 report leads with the assessment (conclusion-first)",
      bool(rep.get("assessment", {}).get("headline")) and
      rep.get("review_class"), str(rep)[:150])
check("G4 honesty labels present (fixture marked as such)",
      "MOCK" in json.dumps(rep).upper())

print()
print("ACCEPTANCE RESULT:", "ALL PASS" if not FAILS
      else f"FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
