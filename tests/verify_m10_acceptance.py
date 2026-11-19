#!/usr/bin/env python3
"""Milestone 10 acceptance tests (Mandate section 17, A-N).

Runs against an ISOLATED server (fresh DB, own port 8794, throwaway
skills/task dirs) so nothing here touches live demo state or the other
suites' fixtures. What it proves, in the mandate's own order:

 A  product surface: nav order, branding, no Demo/CodeBuddy badges,
    no Load-example entry point, unified-dialog markup
 B  bilingual parity: en and zh dictionaries carry the SAME key set,
    zh-first branding/placeholder strings
 C  readiness endpoint: honest structure, real checks, unknown
    manifest refused (never invented)
 D  approval trail: a native submit WITHOUT the explicit authorization
    still blocks on its card (genuine gates preserved); denial cancels
 E  duplicate-submit prevention: a confirmed manifest is single-use
    (resubmit = 409, never a second queue entry); the honest retry is
    re-confirm -> new manifest -> new job
 F  scoped start authorization: the explicit Start click auto-approves
    its OWN approval (trail names the human authorization), the job
    queues, and if the run fails afterwards the chat carries a
    proactive structured failure notice
 G  controlled mode is REAL at the backend: invalid modes refused;
    when the planner clears a controlled run a plan-review decision
    (execute/abort) gates execution
 H  the mock engine and the old approval-card flow are untouched
 I  failure humanization: confirmed reason/next-step per real code
 J  gate cards carry derived quality fields (target/consequence)
 K  artifact download route: real bytes or 404, never 500 on traversal
 L  composer guard + registry/UI-contract static wiring
 N  results states: running -> 409, cancelled -> 409, failed -> 200
    structured failure view (no invented results); N3 drives a real
    native failure E2E via honest drift-after-confirm (inputs moved
    after submit) - planner-blocked, never gateway-submitted

Run: python3 tests/verify_m10_acceptance.py
"""
import atexit
import base64
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import m7_fixtures  # noqa: E402

PORT = 8794
BASE = f"http://127.0.0.1:{PORT}"
FIXDIR, _FIXAMB = m7_fixtures.ensure()
FAILS = []
NOTES = []

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

try:
    (ROOT / "data" / "m10accept.sqlite3").unlink()
except OSError:
    pass
_srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="m10accept.sqlite3",
             NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-m10acc-skills",
             NEOAG_TASKQ_DIR="/tmp/neoag-m10acc-taskq"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _cleanup():
    _srv.terminate()
    try:
        _srv.wait(timeout=8)
    except Exception:
        _srv.kill()
    try:
        (ROOT / "data" / "m10accept.sqlite3").unlink()
    except OSError:
        pass
    for d in ("/tmp/neoag-m10acc-skills", "/tmp/neoag-m10acc-taskq"):
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup)


def call(method, path, body=None, timeout=240):
    req = urllib.request.Request(
        BASE + path, data=(json.dumps(body).encode()
                           if body is not None else None),
        method=method,
        headers={"Content-Type": "application/json",
                 "Authorization": AUTH_HEADER})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except urllib.error.URLError:
        return 0, {}


def raw(path):
    req = urllib.request.Request(BASE + path, headers={
        "Authorization": AUTH_HEADER})
    return urllib.request.urlopen(req, timeout=30).read().decode(
        errors="replace")


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{str(extra)[:220]}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def note(msg):
    NOTES.append(msg)
    print("NOTE " + msg)


def wait_task(jid, states, deadline=300):
    t0 = time.time()
    last = {}
    while time.time() - t0 < deadline:
        st, j = call("GET", "/api/tasks/" + jid)
        if st == 200:
            last = j
            if j.get("state") in states:
                return j
        time.sleep(1.5)
    return last


# wait for boot
for _ in range(60):
    _st, _b = call("GET", "/api/bootstrap", timeout=5)
    if _st == 200:
        break
    time.sleep(1)
else:
    print("server did not start")
    sys.exit(1)

INDEX = raw("/")
APPJS = raw("/static/app.js")

# ================= TEST A: product surface ==============================
pos = {k: INDEX.find(k) for k in
       ('data-view="tasks"', 'data-view="discovery"',
        'data-view="results"', 'data-view="skills"', 'data-view="help"')}
check("A1 nav order Tasks -> Discovery -> Results -> Skills -> Help",
      all(v >= 0 for v in pos.values()) and
      pos['data-view="tasks"'] < pos['data-view="discovery"'] <
      pos['data-view="results"'] < pos['data-view="skills"'] <
      pos['data-view="help"'], pos)
check("A2 branding subtitle present, no Demo/CodeBuddy badges",
      "Agent-assisted neoantigen analysis" in INDEX and
      "CodeBuddy" not in INDEX and "codebuddy" not in INDEX.lower().replace(
          "/static", "") and 'id="load-example"' not in INDEX)
check("A3 unified dialog markup shipped (no reliance on native dialogs)",
      'id="dialog-root"' in INDEX and 'id="dlg-actions"' in INDEX and
      "uiDialog" in APPJS and "uiConfirm" in APPJS and "uiPrompt" in APPJS)
_code = re.sub(r"/\*[\s\S]*?\*/", "", APPJS)
_code = re.sub(r"^\s*//.*$", "", _code, flags=re.M)
_code = _code.replace("uiConfirm(", "_C(").replace("uiPrompt(", "_P(")
_native_dialogs = re.findall(
    r"(?<![\w.])(?:window\.)?(alert|confirm|prompt)\s*\(", _code)
check("A4 no window.alert/confirm/prompt call sites remain",
      not _native_dialogs, _native_dialogs[:5])
check("A5 execution-mode selector + options + readiness + registry "
      "controls in the markup",
      'id="exec-mode"' in INDEX and 'value="autonomous"' in INDEX and
      'value="controlled"' in INDEX and 'id="options-block"' in INDEX and
      'id="ready-block"' in INDEX and 'id="res-mock"' in INDEX and
      'id="res-findings"' in INDEX and 'id="attn-badge"' in INDEX)

# ================= TEST B: bilingual parity =============================
en_txt = APPJS.split("  en: {", 1)[1].split("  zh: {", 1)[0]
zh_txt = APPJS.split("  zh: {", 1)[1].split("};", 1)[0]
en_keys = set(re.findall(r'^\s*"([^"]+)":', en_txt, re.M))
zh_keys = set(re.findall(r'^\s*"([^"]+)":', zh_txt, re.M))
check("B1 en/zh dictionaries carry the identical key set",
      en_keys == zh_keys,
      f"en-only={sorted(en_keys - zh_keys)[:6]} "
      f"zh-only={sorted(zh_keys - en_keys)[:6]}")
check("B2 zh-first branding + placeholder strings",
      '"brand.tag": "智能体辅助新抗原分析平台"' in APPJS and
      '"chat.ph": "发送消息…"' in APPJS and
      '"opt.title": "分析项目"' in APPJS and
      '"rdy.title": "就绪检查"' in APPJS)
check("B3 identifiers stay untranslated (technical fold keys exist)",
      '"plan.summary": "规划详情（技术细节）"' in APPJS and
      "plan.runplan" in en_keys)

# ================= TEST C: readiness endpoint ===========================
st, e = call("POST", "/api/jobs/native/readiness",
             {"manifest_id": "man-does-not-exist"})
check("C1 readiness with an unknown manifest is a 400 (nothing invented)",
      st == 400, f"{st} {e}")
st, d = call("POST", "/api/native/discover", {"data_dir": str(FIXDIR)})
check("C2 fixture directory discovers", st == 200 and d.get("ok", True),
      f"{st} {str(d)[:120]}")
st, man = call("POST", "/api/native/manifest",
               {"data_dir": str(FIXDIR), "sample_id": "M10ACC",
                "picked": {}})
check("C3 confirmation produces a manifest",
      st == 201 and (man.get("id") or man.get("manifest", {}).get("id")),
      f"{st} {str(man)[:120]}")
mid = man.get("id") or man.get("manifest", {}).get("id") or ""
st, rdy = call("POST", "/api/jobs/native/readiness", {"manifest_id": mid})
checks = rdy.get("checks") or []
check("C4 readiness returns ONE structured verdict over the whole "
      "workflow (status + per-check rows + blockers + checked_at)",
      st == 200 and rdy.get("status") in ("READY", "BLOCKED") and
      len(checks) >= 8 and
      all({"id", "ok"} <= set(c) for c in checks) and
      isinstance(rdy.get("blockers"), list) and rdy.get("checked_at"),
      f"{st} {str(rdy)[:200]}")
ids = [c["id"] for c in checks]
check("C5 readiness covers runtime, inventories, inputs and the planner",
      "native_runtime" in ids and "inputs_present" in ids and
      "planner" in ids and "reference_manifest" in ids, ids)
if rdy.get("status") == "READY":
    check("C6 READY never reports a blocking row",
          not [c for c in checks if c["ok"] is False and c.get("blocking")])
else:
    check("C6 BLOCKED names every blocking reason in blockers[]",
          bool(rdy.get("blockers")), rdy)

# ================= TEST D: gates preserved without authorization ========
st, j1 = call("POST", "/api/jobs", {"engine": "native", "manifest_id": mid})
check("D1 native submit WITHOUT the explicit authorization still "
      "blocks on its approval card (genuine gate preserved)",
      st == 202 and j1.get("approval_id") and not j1.get("auto_started"),
      f"{st} {str(j1)[:150]}")
st, t1 = call("GET", "/api/tasks/" + j1["job_id"])
check("D2 the job sits in blocked state, nothing executed",
      t1.get("state") == "blocked", t1.get("state"))
st, r_ = call("GET", "/api/results/" + j1["job_id"])
check("N1 a not-finished run has NO results view yet (409, never "
      "an empty 200)", st == 409, f"{st} {str(r_)[:120]}")
st, _ = call("POST", "/api/approvals/" + j1["approval_id"],
             {"approve": False, "note": "acceptance denial"})
t1 = wait_task(j1["job_id"], ["cancelled"], 60)
check("D3 denial records a real cancellation (not a silent delete)",
      t1.get("state") == "cancelled", t1.get("state"))

# ================= TEST E: duplicate-submit prevention ==================
# The confirmed manifest is SINGLE-USE by design: once a run consumed
# it (confirmed -> linked), re-submitting the same input set is refused
# outright; a retry must re-discover and re-confirm, which re-makes
# the human's input confirmation for the new attempt. No second queue
# entry for one confirmed input set, ever.
st, dup = call("POST", "/api/jobs", {"engine": "native", "manifest_id": mid})
check("E1 a manifest that already produced a run cannot silently start "
      "a second one (409, no new job)",
      st == 409 and dup.get("job_id") is None, f"{st} {str(dup)[:160]}")
st, d2 = call("POST", "/api/native/discover", {"data_dir": str(FIXDIR)})
st, man2 = call("POST", "/api/native/manifest",
                {"data_dir": str(FIXDIR), "sample_id": "M10ACC",
                 "picked": {}})
mid2 = man2.get("id") or man2.get("manifest", {}).get("id") or ""
st, j2 = call("POST", "/api/jobs", {"engine": "native", "manifest_id": mid2})
check("E2 the honest retry path works: re-confirm a fresh manifest -> "
      "a new job is created (blocked on its card, nothing runs)",
      st == 202 and j2.get("job_id"), f"{st} {str(j2)[:120]}")
if j2.get("job_id"):
    call("POST", "/api/jobs/" + j2["job_id"] + "/cancel", {})
    wait_task(j2["job_id"], ["cancelled"], 60)

# ================= TEST F: scoped start authorization ===================
# SAFETY: an AUTONOMOUS gateway submit that the planner CLEARS would
# really execute on the shared gateway - the acceptance suite never
# starts real computation. It branches on the planner verdict measured
# in C: a blocking verdict makes an autonomous gateway submit fail
# deterministically BEFORE execution (which is exactly the failure
# path F must observe); a clear verdict is observed via the bundled
# demo profile instead (a supervised local subprocess, no gateway).
def new_manifest():
    call("POST", "/api/native/discover", {"data_dir": str(FIXDIR)})
    _st, mn = call("POST", "/api/native/manifest",
                   {"data_dir": str(FIXDIR), "sample_id": "M10ACC",
                    "picked": {}})
    return mn.get("id") or mn.get("manifest", {}).get("id") or ""


st, conv = call("POST", "/api/conversations", {})
cid = (conv.get("conversation") or {}).get("id") or ""
plan_status = (rdy.get("plan") or {}).get("status") or ""
plan_bad = (rdy.get("status") == "BLOCKED" or
            plan_status in ("BLOCKED", "FAILED", ""))
fb = {"engine": "native", "authorized": "start_analysis",
      "conv_id": cid, "execution_mode": "autonomous"}
if plan_bad:
    fb.update({"manifest_id": new_manifest()})
else:
    fb.update({"synthetic_demo": True})
st, j3 = call("POST", "/api/jobs", fb)
check("F1 the explicit Start click carries its authorization THROUGH: "
      "backend answers auto-started, no separate approval round-trip",
      st == 202 and j3.get("auto_started") is True and
      j3.get("authorization") == "user-explicit-start", f"{st} {str(j3)[:150]}")
st, boot = call("GET", "/api/bootstrap")
ap = next((a for a in boot.get("approvals", [])
           if a.get("job_id") == j3["job_id"]), {})
check("F2 the approval RECORD still exists and is approved BY THE "
      "HUMAN authorization (trail kept, not suppressed)",
      ap.get("state") == "approved" and "explicit Start Analysis "
      "authorization" in str(ap.get("decided_by", "")), str(ap)[:200])
t3 = wait_task(j3["job_id"],
               ["completed", "failed", "cancelled", "blocked"], 300)
state3 = t3.get("state")
if state3 == "failed":
    st, fv = call("GET", "/api/results/" + j3["job_id"])
    f = fv.get("failure") or {}
    check("N2 a failed run is a RESULTS subject: 200 structured "
          "failure view (reason, computation flag, next step)",
          st == 200 and bool(f.get("reason")) and
          isinstance(f.get("started_computation"), bool) and
          bool(f.get("next_step")), f"{st} {str(fv)[:200]}")
    st, cv = call("GET", "/api/conversations/" + cid)
    fm = [m for m in (cv.get("messages") or [])
          if (m.get("meta") or {}).get("kind") == "job_failure"]
    check("F3 the owning conversation was TOLD proactively with the "
          "structured failure record (never only in the task list)",
          len(fm) >= 1 and
          (fm[0]["meta"].get("failure") or {}).get("next_step"),
          str([m.get("meta", {}).get("kind")
               for m in (cv.get("messages") or [])[-4:]])[:160])
else:
    if state3 not in ("completed",):
        call("POST", "/api/jobs/" + j3["job_id"] + "/cancel", {})
    note(f"authorized run ended state={state3} ({'demo profile' if not plan_bad else 'gateway pre-verdict'}); "
         "failure-view E2E only observable on a failing run - the "
         "structured view itself is proven by G3b/N2 where reachable "
         "and unit-proven in I")

# ================= TEST G: controlled mode is real ======================
midg = new_manifest()
st, e = call("POST", "/api/jobs", {"engine": "native", "manifest_id": midg,
                                   "authorized": "start_analysis",
                                   "execution_mode": "sideways"})
check("G1 an unknown execution_mode is refused (400) - the choice is "
      "a real backend contract, not a label", st == 400, f"{st} {e}")
st, j4 = call("POST", "/api/jobs", {"engine": "native", "manifest_id": midg,
                                    "authorized": "start_analysis",
                                    "execution_mode": "controlled"})
check("G2 controlled submit is accepted", st == 202, f"{st} {str(j4)[:120]}")
t4 = wait_task(j4["job_id"],
               ["blocked", "completed", "failed", "cancelled"], 300)
dec = None
if t4.get("state") == "blocked":
    st, boot = call("GET", "/api/bootstrap")
    dec = next((x for x in boot.get("decisions", [])
                if x.get("job_id") == j4["job_id"] and
                x.get("status") == "pending"), {})
    opts = [o.get("id") for o in (dec or {}).get("options", [])]
    check("G3 controlled + planner PASS = execution stops at a REAL "
          "plan-review decision (execute/abort) - NOTHING ran while "
          "blocked",
          dec.get("key") == "native.plan_review" and
          set(opts) >= {"execute", "abort"}, str(dec)[:220])
    if dec:
        call("POST", f"/api/decisions/{dec['id']}/resolve",
             {"choice": "abort", "note": "acceptance abort"})
        t4b = wait_task(j4["job_id"], ["cancelled", "completed"], 120)
        check("G4 abort cancels the Web job WITHOUT any gateway "
              "submission", t4b.get("state") == "cancelled" and
              not (t4b.get("native") or {}).get("gateway_job_id"),
              str(t4b)[:180])
else:
    check("G3 controlled run reached a terminal/plan stage within the "
          "deadline (no stuck state either way)",
          t4.get("state") in ("failed", "completed", "cancelled"),
          str(t4)[:200])
    if t4.get("state") == "failed":
        st, fv = call("GET", "/api/results/" + j4["job_id"])
        f = fv.get("failure") or {}
        check("G3b the pre-execution planner verdict is HUMANIZED "
              "(started_computation False, reason, next step)",
              st == 200 and f.get("started_computation") is False and
              bool(f.get("reason")) and bool(f.get("next_step")),
              str(fv)[:200])

# ============ TEST N3: drift-after-submit fails HONESTLY (E2E) ==========
# A native failure driven end-to-end with ZERO gateway risk, on an
# input_dir-mode job (verified 2026-11-19: with a CONFIRMED MANIFEST the
# native planner trusts the manifest's recorded coordinates and never
# re-stats the live files - drift-immune by design; with input_dir-mode
# params the planner DOES scan the directory and blocks missing inputs
# with AMBIGUOUS_INPUT). Sequence: copy the fixtures into a private
# directory (shared workspace fixtures are never touched), submit an
# input_dir-mode job with the explicit Start authorization (controlled
# mode - even if the planner cleared it, execution would stop at the
# plan-review decision, never reaching the gateway), then move the
# private input directory away immediately. The planner subprocess
# needs seconds to import before it scans, so the scan sees the drift:
# a pre-execution PLAN verdict -> structured failure view + proactive
# chat notice, end-to-end. Every race outcome is handled honestly:
# the worker verdict (full N3b/N3c proof), the submit-time revalidation
# catching it first (N3b-alt), or the planner having scanned before the
# move (job stays blocked at plan review, aborted - nothing executed).
_drift = "/data1/neoag/agents/neoag_agent_ws/m10acc_drift_inputs"
_gone = _drift + ".m10gone"
shutil.rmtree(_drift, ignore_errors=True)
shutil.copytree(str(FIXDIR), _drift)
st, jd = call("POST", "/api/jobs", {
    "engine": "native",
    "native_params": {"sample_id": "M10DRIFT", "case_id": "M10-DRIFT",
                      "input_dir": _drift},
    "authorized": "start_analysis", "execution_mode": "controlled",
    "conv_id": cid})
check("N3a the input_dir-mode submit meets the honest server-side "
      "revalidation: accepted while inputs exist, or refused by that "
      "same revalidation (409) - never accepted-then-ignored",
      st == 202 or (st == 409 and jd.get("error") == "readiness_blocked"),
      f"{st} {str(jd)[:160]}")
try:
    os.rename(_drift, _gone)
    if st == 202:
        td = wait_task(jd["job_id"],
                       ["failed", "blocked", "completed", "cancelled"],
                       300)
        if td.get("state") == "failed":
            st, fv = call("GET", "/api/results/" + jd["job_id"])
            f = fv.get("failure") or {}
            check("N3b the drift is a RESULTS subject: 200 structured "
                  "failure view - confirmed reason, computation NOT "
                  "started, real next step",
                  st == 200 and f.get("started_computation") is False and
                  bool(f.get("reason")) and bool(f.get("next_step")),
                  f"{st} {str(fv)[:200]}")
            st, cv = call("GET", "/api/conversations/" + cid)
            fm = [m for m in (cv.get("messages") or [])
                  if (m.get("meta") or {}).get("kind") == "job_failure"
                  and (m.get("meta") or {}).get("job_id") == jd["job_id"]]
            check("N3c the owning conversation is TOLD proactively with "
                  "the same structured record (name/time/reason/next "
                  "step, never only the task list)",
                  len(fm) >= 1 and
                  (fm[0]["meta"].get("failure") or {}).get("next_step"),
                  str([m.get("meta", {}).get("kind")
                       for m in (cv.get("messages") or [])[-3:]])[:160])
        elif td.get("state") == "blocked":
            st, boot = call("GET", "/api/bootstrap")
            dec = next((x for x in boot.get("decisions", [])
                        if x.get("job_id") == jd["job_id"] and
                        x.get("status") == "pending"), {})
            if dec:
                call("POST", f"/api/decisions/{dec['id']}/resolve",
                     {"choice": "abort", "note": "acceptance drift test"})
            note("N3: planner scanned before the move could land - "
                 "failure view not exercised this run; job stayed safe "
                 "(blocked at plan review, aborted, nothing executed) - "
                 "verdict logic itself proven by G3/G4 and unit I")
        else:
            check("N3b drifted run reached an honest terminal state",
                  td.get("state") in ("failed", "cancelled"),
                  str(td)[:200])
    else:
        # server-side revalidation ALREADY caught the drift at submit
        # time (the race resolved the other way): still an honest refusal
        check("N3b-alt submit-time revalidation refused the drifted "
              "inputs (409 readiness_blocked, blockers named)",
              st == 409 and (jd.get("error") == "readiness_blocked"),
              f"{st} {str(jd)[:200]}")
finally:
    try:
        os.rename(_gone, _drift)
    except OSError:
        pass
    shutil.rmtree(_drift, ignore_errors=True)

# ================= TEST H: mock flow untouched ==========================
st, jm = call("POST", "/api/jobs",
              {"sample_id": "M10MOCK", "mode": "auto",
               "execution_mode": "controlled"})
check("H1 the mock engine keeps the classic approval-card flow",
      st == 202 and jm.get("approval_id") and not jm.get("auto_started"),
      f"{st} {str(jm)[:120]}")
st_ap, _r_ap = call("POST", "/api/approvals/" + jm["approval_id"],
                    {"approve": True})
check("H1b the approval POST itself is accepted", st_ap == 200,
      f"{st_ap} {str(_r_ap)[:120]}")
tm = wait_task(jm["job_id"], ["completed", "blocked"], 120)
i = 0
while tm.get("state") == "blocked" and i < 12:
    st, boot = call("GET", "/api/bootstrap")
    pd = [x for x in boot.get("decisions", [])
          if x.get("job_id") == jm["job_id"] and x.get("status")
          == "pending"]
    for x in pd:
        call("POST", f"/api/decisions/{x['id']}/resolve",
             {"choice": (x.get("options") or [{}])[0].get("id", ""),
              "note": "acceptance"})
    # the worker polls resolutions on a 0.25s beat and the row still
    # reads "blocked" for a moment after a resolve: give the handoff
    # time before re-checking, or the loop spins with nothing pending
    time.sleep(3)
    tm = wait_task(jm["job_id"], ["completed", "blocked"], 90)
    i += 1
st, resm = call("GET", "/api/results/" + jm["job_id"])
check("H2 the old mock journey still delivers through the same "
      "results registry", tm.get("state") == "completed" and
      st == 200 and resm.get("outcome"), f"{tm.get('state')} {st}")

# ================= TEST I: failure humanization unit ====================
from backend.jobs import JobManager  # noqa: E402
_h = JobManager._humanize_native_failure
f1 = _h("PLAN_TIMEOUT", "planner deadline exceeded",
        {"blocking_issues": ["reference missing"]}, "gateway", "planning")
f2 = _h("GATEWAY_MONITOR_TIMEOUT", "still running", None, "gateway",
        "execution")
f3 = _h("NATIVE_UNAVAILABLE", "import failed", None, "gateway",
        "preflight")
check("I1 a planner-blocked verdict is stated as NOTHING was executed "
      "(pre-execution), with the real blocker and a next step",
      f1["started_computation"] is False and "NOTHING was executed"
      in f1["reason"] and f1["blockers"] == ["reference missing"] and
      f1["next_step"], str(f1)[:200])
check("I2 a gateway monitor timeout admits the TRUE state is UNKNOWN "
      "(never guessed) and counts computation as started",
      f2["started_computation"] is True and "UNKNOWN" in f2["reason"],
      str(f2)[:180])
check("I3 the preflight refusal says no planning or computation ran",
      f3["started_computation"] is False and "BEFORE" in f3["reason"],
      str(f3)[:180])

# ================= TEST J: gate quality fields ==========================
from backend.agent import gate_consequence  # noqa: E402
g1 = gate_consequence("rm -rf /data1/neoag/work/jobs/job-abc/out")
g2 = gate_consequence("cp /tmp/a.json /data1/neoag/agents/x/auth.json")
check("J1 a destructive gate names its real target paths and effect "
      "kind (derived, not asserted)",
      g1["effect_kind"] == "delete" and
      any("job-abc" in p for p in g1["target_paths"]) and
      bool(g1["consequence"]), str(g1)[:220])
check("J2 an overwrite gate is distinguished from a delete",
      g2["effect_kind"] in ("overwrite", "write-or-run") and
      bool(g2["target_paths"]), str(g2)[:180])
check("J3 gate cards carry approve/deny effect fields in the code "
      "path that builds them",
      '"approve_effect"' in (ROOT / "backend" / "agent.py").read_text() and
      '"deny_effect"' in (ROOT / "backend" / "agent.py").read_text())

# ================= TEST K: artifact download route ======================
st, e = call("GET", "/api/results/" + jm["job_id"] + "/artifact/nope")
check("K1 an unknown artifact id is a 404 (never 500, never bytes of "
      "something else)", st == 404, f"{st} {e}")
st, e = call("GET", "/api/results/" + jm["job_id"] +
             "/artifact/..%2F..%2Fetc%2Fpasswd")
check("K2 a traversal-shaped artifact id cannot escape (404/403 only)",
      st in (403, 404), f"{st}")

# ================= TEST L: composer + registry wiring ===================
check("L1 composer placeholder is neutral bilingual text and the "
      "send-guard exists",
      'placeholder="Message…"' in INDEX and
      '"chat.ph": "发送消息…"' in APPJS and
      'state.turnWorking' in APPJS and 'chat.busySend' in APPJS)
check("L2 the results registry renders every outcome class + native "
      "verbatim tables + per-artifact downloads + findings first",
      all(k in APPJS for k in (
          "res-mock-wrap", "preview_tables", "/artifact/",
          "res-findings", "failcard", "res.inprogress",
          "res.cancelledNote", "res.openDiscovery")))
check("L3 status semantics keep Waiting-for-decision apart from "
      "Waiting-for-approval apart from Running",
      "st.waitdec" in en_keys and "st.blocked" in en_keys and
      "statusPill(st, j)" in APPJS and
      'd.status === "pending" && d.job_id === j.id' in APPJS)
check("L4 the central attention dialog + cross-tab badge reuse the "
      "real pending lists (approvals AND decisions AND gate cards)",
      'attentionRowsHtml(true)' in APPJS and
      'id="attn-badge"' in INDEX and
      "attn-badge" in APPJS and "attn.center" in en_keys)
check("L5 load-example has NO UI entry point any more (API may remain)",
      "load-example" not in APPJS and "loadExample" not in APPJS and
      "act == \"example\"" in (ROOT / "backend" / "server.py").read_text())

print("\n== M10 acceptance:", "FAILED: " + json.dumps(FAILS) if FAILS
      else "ALL PASS", f"({len(NOTES)} note(s))")
sys.exit(1 if FAILS else 0)
