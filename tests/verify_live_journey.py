#!/usr/bin/env python3
"""Milestone 7 live-journey verification (reproducible, cancel-safe).

Runs the operator's journey against the LIVE demo server (default
port 8712): discover -> confirm -> real planner preview -> submit ->
unified task + approval card -> CANCEL. The job is cancelled while it
is still blocked on approval, so the native pipeline is NEVER started
by this script; no skill or conversation state is mutated, and no
patient data is touched (fixtures are the synthetic m7_fixtures dirs).

Run:  python3 tests/verify_live_journey.py [port]
"""
import base64
import json
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = int(sys.argv[1] if len(sys.argv) > 1 else
           __import__("os").environ.get("NEOAG_DEMO_PORT", "8712"))
BASE = f"http://127.0.0.1:{PORT}"
sys.path.insert(0, str(ROOT / "tests"))
import m7_fixtures  # noqa: E402

_a = json.load(open(ROOT / "data" / "auth.json"))
H = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()
FAILS = []


def call(method, path, body=None, to=180):
    req = urllib.request.Request(
        BASE + path, data=(json.dumps(body).encode()
                           if body is not None else None),
        method=method, headers={"Content-Type": "application/json",
                                "Authorization": H})
    try:
        with urllib.request.urlopen(req, timeout=to) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else f"  [{str(extra)[:200]}]"), flush=True)
    if not cond:
        FAILS.append(name)


st, boot = call("GET", "/api/bootstrap", to=30)
check("L0 live demo answers with the unified task list",
      st == 200 and isinstance(boot.get("jobs"), list), st)
CLEAN, AMBIG = m7_fixtures.ensure()

# --- the ordinary journey: ONE directory -----------------------------
st, d = call("POST", "/api/native/discover", {"data_dir": str(CLEAN)})
check("L1 Discover inputs runs the native detector on the live server",
      st == 200 and d.get("engine", {}).get("detector", "").startswith(
          "neoag.open_neo.routing") and d.get("ready"),
      f"{st} {d.get('error', '')}")
check("L2 tumor/normal come from the VCF's genotype columns",
      (d.get("summary") or {}).get("tumor_sample_id") == "TEST001_T")
st, amb = call("POST", "/api/native/discover", {"data_dir": str(AMBIG)})
cands = [c for a in amb.get("ambiguous", [])
         for c in a.get("candidates", [])]
check("L3 a second run with a genuine ambiguity offers every real "
      "candidate as a choice", len(cands) == 2, cands)
st, m = call("POST", "/api/native/manifest",
             {"data_dir": str(AMBIG), "sample_id": "JOURNEY01",
              "picked": {"normal_dna_bam": cands[0]} if cands else {}})
check("L4 confirm writes the native open-neo-sample-v1 manifest",
      st == 201 and m.get("status") == "confirmed" and (
          m.get("proposal") or {}).get("manifest_path", "").endswith(
              ".json"), f"{st} {str(m)[:120]}")
st, plan = call("POST", "/api/jobs/native/preview",
                {"manifest_id": m.get("id", "")}, to=300)
check("L5 preview runs the REAL native planner over the confirmed "
      "manifest (executes nothing)",
      st == 200 and bool(plan.get("run_id")) and (
          "status" in plan), f"{st} {str(plan)[:140]}")
st, job = call("POST", "/api/jobs",
               {"engine": "native", "manifest_id": m.get("id", "")})
jid = job.get("job_id", "")
check("L6 submit through the unified endpoint returns ONE Job ID",
      st == 202 and jid, f"{st} {job}")
st, tk = call("GET", "/api/tasks")
check("L7 the job appears in the unified task system immediately",
      any(t["id"] == jid for t in tk.get("tasks", [])))
appr = (call("GET", "/api/bootstrap")[1].get("approvals") or [])
check("L8 execute-class work waits behind a human approval card",
      any(a.get("job_id") == jid and a.get("state") == "pending"
          for a in appr), [a.get("job_id") for a in appr][:6])
st, c = call("POST", f"/api/jobs/{jid}/cancel",
             {"reason": "live journey probe - cancelled while still "
                        "blocked on approval (nothing was executed)"})
check("L9 the task can be cancelled from the same unified surface",
      st == 200, f"{st} {c}")
st, det = call("GET", f"/api/tasks/{jid}")
check("L10 the cancelled outcome is recorded honestly (no run, no "
      "fabricated results)", det.get("state") == "cancelled",
      det.get("state"))
st, sk = call("GET", "/api/skills")
check("L11 skills list carries the §5 card fields (description, "
      "enabled, applies-to-new-chats)",
      st == 200 and all({"description", "enabled",
                         "active_in_new_sessions"} <= set(s)
                        for s in sk.get("skills", [])))

print()
print("LIVE JOURNEY:", "ALL PASS" if not FAILS else f"FAILED: {FAILS}")
print("NOTE: the submitted job was cancelled while still blocked on "
      "approval - the native pipeline was never started by this run.")
sys.exit(1 if FAILS else 0)
