#!/usr/bin/env python3
"""M11 P0-8 battery: ONE place per task.

Pinned behaviour (all measured, none asserted from memory):
  * every job created through the API carries a readable default name
    "Analysis - YYYY-MM-DD HH:MM" (server clock), set AT CREATION in
    the store - lists never show bare ids. Store-level unit check pins
    the kind prefixes (Discovery / Diagnostic / Analysis) directly.
  * rename is display-only: POST /api/jobs/<id>/title rewrites
    display_title and nothing else - id, spec_json, state and audit
    trail verifiably untouched in the DB; empty title 400, unknown
    job 404; the rename survives the whole run lifecycle.
  * the former side drawer is GONE from the frontend: no #job-detail
    container, no jd-* ids, no state.jobDetail; openJob is a
    synchronous select-and-jump into Results, and renderResults
    itself fetches the task head data (/api/jobs/<id>/events +
    /api/tasks/<id>) and renders identity, controls and the tech
    record for EVERY state, with a render-sequence guard so a late
    response for an abandoned selection never overwrites a newer one.
  * the full chain still works end to end: approve -> run ->
    completed -> /api/results 200, and the endpoints the Results
    task head calls answer for a blocked job (tolerantly) and for a
    completed one (fully).
Run: python3 tests/verify_m11_p08.py   (isolated :8787)
"""
import atexit
import base64
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 8787
TITLE_RE = re.compile(r"^(Analysis|Discovery|Diagnostic) - "
                      r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
PASS, FAIL = [], []

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

# ================= server ============================================
srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="m11p08.sqlite3",
             NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-m11p08-skills",
             NEOAG_TASKQ_DIR="/tmp/neoag-m11p08-taskq",
             NEOAG_GATE_TOKEN_DISABLE="1"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _cleanup():
    srv.terminate()
    try:
        srv.wait(timeout=8)
    except Exception:
        srv.kill()
    try:
        (ROOT / "data" / "m11p08.sqlite3").unlink()
    except OSError:
        pass
    shutil.rmtree(ROOT / "data" / "exports-m11p08", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p08-skills", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p08-taskq", ignore_errors=True)
    shutil.rmtree("/tmp/m11p08-store", ignore_errors=True)


atexit.register(_cleanup)


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS" if ok else "FAIL") + f"  {name}"
          + (f"  [{detail}]" if detail and not ok else ""))


def api(method, path, body=None, timeout=120):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method, headers={"Authorization": AUTH_HEADER})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def fetch(path):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        headers={"Authorization": AUTH_HEADER})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode()


up = False
for _ in range(80):
    try:
        _s, _b = api("GET", "/api/bootstrap")
        up = _s == 200
        if up:
            break
    except Exception:
        time.sleep(0.5)
if not up:
    print("FAIL  server never came up")
    sys.exit(1)


def jobs_list():
    st, b = api("GET", "/api/bootstrap")
    return {j["id"]: j for j in b.get("jobs", [])}


def wait_task(jid, states, deadline=180):
    t0 = time.time()
    last = {}
    while time.time() - t0 < deadline:
        st, j = api("GET", "/api/tasks/" + jid)
        if st == 200:
            last = j
            if j.get("state") in states:
                return j
        time.sleep(1.5)
    return last


# ================= 1: default name at creation ========================
st, j1 = api("POST", "/api/jobs",
             {"sample_id": "P08DEFAULT", "mode": "auto",
              "execution_mode": "controlled"})
jid = j1.get("job_id") or ""
check("1a a created job answers 202 with an approval card",
      st == 202 and jid and j1.get("approval_id"), f"{st} {str(j1)[:160]}")
row = jobs_list().get(jid) or {}
check("1b the job row carries a readable default name at creation "
      "(Analysis - YYYY-MM-DD HH:MM, never a bare id)",
      row.get("display_title") and bool(
          TITLE_RE.match(row["display_title"])),
      str(row.get("display_title")))

# store-level unit check of the kind prefixes (separate temp DB)
sys.path.insert(0, str(ROOT))
from backend.store import Store  # noqa: E402
_prev_db = os.environ.get("NEOAG_DEMO_DB")
os.environ["NEOAG_DEMO_DB"] = "m11p08-unit.sqlite3"
us = Store(pathlib.Path("/tmp/m11p08-store"))
for _jid, _kind in (("u-a", "discovery"), ("u-b", "diagnostic"),
                    ("u-c", "neoag-native-run"), ("u-d", "benchmark")):
    us.job_create(_jid, _kind, "U1", {}, "queued", None)
_titles = {r[0]: r[1] for r in us.db.execute(
    "SELECT id, display_title FROM jobs")}
if _prev_db is not None:
    os.environ["NEOAG_DEMO_DB"] = _prev_db
check("1c store prefixes the default name by kind (Discovery / "
      "Diagnostic / Analysis) and every default matches the clock form",
      _titles.get("u-a", "").startswith("Discovery - ")
      and _titles.get("u-b", "").startswith("Diagnostic - ")
      and _titles.get("u-c", "").startswith("Analysis - ")
      and _titles.get("u-d", "").startswith("Analysis - ")
      and all(TITLE_RE.match(v or "") for v in _titles.values()),
      str(_titles))

# ================= 2: rename is display-only ==========================
st, boot_before = api("GET", "/api/bootstrap")
spec_before = json.dumps(jobs_list()[jid].get("spec"), sort_keys=True)
st, rr = api("POST", f"/api/jobs/{jid}/title", {"title": "Renamed P08 Task"})
row = jobs_list().get(jid) or {}
check("2a rename sticks in the API row: same id, new display_title",
      st == 200 and row.get("display_title") == "Renamed P08 Task"
      and row.get("id") == jid, f"{st} {str(row)[:160]}")
check("2b rename leaves state and spec untouched",
      row.get("state") == "blocked"
      and json.dumps(row.get("spec"), sort_keys=True) == spec_before,
      str(row)[:200])

_dbp = ROOT / "data" / "m11p08.sqlite3"
con = sqlite3.connect(f"file:{_dbp}?mode=ro", uri=True)
_dbtitle, _dbspec = con.execute(
    "SELECT display_title, spec_json FROM jobs WHERE id=?",
    (jid,)).fetchone()
_has_audit = con.execute(
    "SELECT COUNT(*) FROM audit WHERE object=? AND action=?",
    (jid, "task.title.set")).fetchone()[0]
con.close()
check("2c DB truth: display_title rewritten, spec_json byte-identical, "
      "rename audited (audit trail extended, never rewritten)",
      _dbtitle == "Renamed P08 Task"
      and json.dumps(json.loads(_dbspec), sort_keys=True) == spec_before
      and _has_audit >= 1, f"{_dbtitle} audit={_has_audit}")

st, _e = api("POST", f"/api/jobs/{jid}/title", {"title": "   "})
check("2d an empty title is refused 400, name untouched",
      st == 400 and jobs_list()[jid]["display_title"]
      == "Renamed P08 Task", f"{st}")
st, _e = api("POST", "/api/jobs/job-does-not-exist-999/title",
             {"title": "ghost"})
check("2e renaming an unknown job is refused 404", st == 404, f"{st}")

# ================= 3: task-head endpoints for every state =============
st, evs = api("GET", f"/api/jobs/{jid}/events")
check("3a a blocked job answers its events list (what the Results "
      "tech record renders)", st == 200 and isinstance(evs, list),
      f"{st}")
st, _tk = api("GET", f"/api/tasks/{jid}")
check("3b the tasks row answers for a live job (client tolerates 404 "
      "on older rows)", st in (200, 404), f"{st}")
st, _sn = api("GET", f"/api/tasks/{jid}/snapshot")
check("3c the snapshot endpoint answers tolerantly for a job that "
      "never started", st in (200, 404), f"{st}")

# ================= 4: full lifecycle keeps the name ===================
st, _ap = api("POST", "/api/approvals/" + j1["approval_id"],
              {"approve": True})
check("4a approval accepted", st == 200, f"{st} {str(_ap)[:120]}")
t1 = wait_task(jid, ["completed", "blocked"], 180)
i = 0
while t1.get("state") == "blocked" and i < 12:
    st, boot = api("GET", "/api/bootstrap")
    pd = [x for x in boot.get("decisions", [])
          if x.get("job_id") == jid and x.get("status") == "pending"]
    for x in pd:
        api("POST", f"/api/decisions/{x['id']}/resolve",
            {"choice": (x.get("options") or [{}])[0].get("id", ""),
             "note": "p08"})
    time.sleep(3)
    t1 = wait_task(jid, ["completed", "blocked"], 90)
    i += 1
st, res4 = api("GET", f"/api/results/{jid}")
check("4b approved mock run reaches completed with a real results "
      "document (the branch Results renders for completed)",
      t1.get("state") == "completed" and st == 200
      and res4.get("outcome"), f"{t1.get('state')} {st}")
check("4c the renamed display title survives the whole lifecycle",
      jobs_list()[jid]["display_title"] == "Renamed P08 Task")
st, tsk = api("GET", f"/api/tasks/{jid}")
st2, snp = api("GET", f"/api/tasks/{jid}/snapshot")
check("4d for a completed job the task row and the start snapshot "
      "answer fully (200, with content the tech record shows)",
      st == 200 and st2 == 200 and (snp.get("snapshot") or {}),
      f"{st} {st2} {str(snp)[:120]}")

# ================= 5: the drawer is gone; Results is the one place ====
html = fetch("/")
js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
css = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")
check("5a served markup: no #job-detail drawer container, no "
      "drawer class, and the two new Results containers exist",
      "job-detail" not in html and 'class="drawer' not in html
      and 'id="res-task"' in html and 'id="res-tech"' in html,
      "")
check("5b no drawer code remains: zero jd-* ids, zero "
      "state.jobDetail references",
      "jd-" not in js and "jobDetail" not in js, "")
check("5c openJob is a synchronous select-and-jump: it sets "
      "state.resultsJob, persists the selection and switches to "
      "results (no async drawer fetch on click)",
      "function openJob(id) {" in js
      and "state.resultsJob = id;" in js
      and 'switchView("results");' in js
      and "async function openJob" not in js, "")
check("5d every task row (job or research) opens Results: the row "
      "click handler calls openJob unconditionally, no /^res-/ "
      "Discovery-jump branch remains in it",
      'el.addEventListener("click", () => openJob(el.dataset.job))'
      in js and "jump there instead of opening the job drawer"
      not in js, "")
check("5e renderResults itself loads the task head for EVERY state: "
      "fetches events + task row, renders taskHeadHtml + wireTaskHead "
      "+ taskTechHtml and fills the snapshot",
      re.search(r"async function renderResults\(\)", js) is not None
      and "/api/jobs/${jid}/events" in js
      and "/api/tasks/${jid}`" in js
      and "taskHeadHtml(job, task)" in js
      and "wireTaskHead(job)" in js
      and "taskTechHtml(job, task, detail)" in js
      and "fillTaskSnapshot(jid, s)" in js, "")
_seq = js.count("if (seq !== resultsSeq) return;")
check("5f render-sequence guard: a late response for an abandoned "
      "selection can never overwrite a newer one (4 continuations "
      "pinned)", _seq >= 4, f"n={_seq}")
check("5g clearResultsPanes wipes the task head and tech record too "
      "(no stale head from the previous selection)",
      '"res-task"' in js.split("function clearResultsPanes")[1]
      .split("}")[0] and '"res-tech"'
      in js.split("function clearResultsPanes")[1].split("}")[0], "")
check("5h styles: task-head and the wide progress bar are styled; "
      "the drawer rule is gone",
      ".task-head" in css and ".jr-bar.wide" in css
      and not re.search(r"\.drawer\s*\{", css), "")

# i18n: both dictionaries carry the new strings
src = js
i18n = src[src.index("const I18N = {"):]
langs = dict(re.findall(r'\n  (en|zh):\s*\{(.*?)\n  \},?', i18n, re.S))
_need = ["dr.rename", "dr.renameSave", "dr.renameCancel",
         "toast.renamed", "dr.tech", "dr.snapshot"]
for lang in ("en", "zh"):
    keys = set(re.findall(r'"([a-z0-9]+\.[A-Za-z0-9]+)"\s*:',
                          langs.get(lang, "")))
    lack = [k for k in _need if k not in keys]
    check(f"5i[{lang}] the {lang} dictionary carries the rename/"
          f"tech strings", not lack, str(lack))

# ================= 6: research rows keep the same naming contract ====
st, rr = api("POST", "/api/research",
             {"scope": "full", "target": "presentation",
              "engine": "example"})
rid = rr.get("research_id") or ""
check("6a research submitted via the ordinary pathway", st == 202 and rid,
      f"{st} {str(rr)[:120]}")
done = None
t0 = time.time()
while time.time() - t0 < 30:
    _s, rrw = api("GET", f"/api/research/{rid}")
    if rrw.get("status") == "completed":
        done = rrw
        break
    time.sleep(1.0)
st, boot6 = api("GET", "/api/bootstrap")
res_row = next((j for j in boot6.get("jobs", [])
                if j["id"] == rid), None) or {}
check("6b the research row gets the default Discovery name at "
      "creation (unified task list shows no bare id)",
      done is not None
      and res_row.get("kind") == "discovery"
      and res_row.get("display_title", "").startswith("Discovery - ")
      and bool(TITLE_RE.match(res_row.get("display_title") or "")),
      str(res_row)[:200])
st, rt = api("POST", f"/api/research/{rid}/title",
             {"title": "Renamed P08 Review"})
st, boot6b = api("GET", "/api/bootstrap")
res_row2 = next((j for j in boot6b.get("jobs", [])
                 if j["id"] == rid), None) or {}
check("6c research rename works through the same display-only rule "
      "(row id unchanged, new title shown)",
      st == 200 and res_row2.get("display_title")
      == "Renamed P08 Review" and res_row2.get("id") == rid,
      f"{st} {str(res_row2)[:160]}")

# ================= summary ============================================
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f_ in FAIL:
        print("FAILED:", f_)
    sys.exit(1)
