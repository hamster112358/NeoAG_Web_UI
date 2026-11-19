#!/usr/bin/env python3
"""M11 P1-9 battery: task management - pin / lock / trash / purge.

Pinned behaviour (all measured, none asserted from memory):
  * every job row exposes pinned/locked/trashed/trashed_at (0/0/0/""
    at creation).
  * ONE rule for single and bulk alike: trashing is refused while the
    task is ACTIVE (cancel first - validated, not implicit) and while
    LOCKED; bulk never gets a laxer path (per-id honest results).
  * trash is RECORD-level: the record leaves the normal listings and
    answers the trash view; restore returns it. Purge deletes the
    job's records (row, events, decisions, artifacts registry,
    snapshot) behind a typed-id confirmation - and NEVER touches
    files: a file referenced by the purged job and by another live
    job keeps its exact bytes and keeps its other reference working.
  * research rows are not job records: the flag endpoint refuses
    res-* ids.
  * every accepted change is audited (task.flags.set), every purge
    audited with its snapshot and files_touched=false; audit rows
    are never deleted by a purge.
  * trash_retention_days persists through the settings whitelist
    (a DISPLAY of the policy - removal is always manual).
Run: python3 tests/verify_m11_p09.py   (isolated :8785)
"""
import atexit
import base64
import hashlib
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
PORT = 8785
PASS, FAIL = [], []

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="m11p09.sqlite3",
             NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-m11p09-skills",
             NEOAG_TASKQ_DIR="/tmp/neoag-m11p09-taskq",
             NEOAG_GATE_TOKEN_DISABLE="1"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

FIX = pathlib.Path("/tmp/m11p09_files")


def _cleanup():
    srv.terminate()
    try:
        srv.wait(timeout=8)
    except Exception:
        srv.kill()
    try:
        (ROOT / "data" / "m11p09.sqlite3").unlink()
    except OSError:
        pass
    shutil.rmtree(ROOT / "data" / "exports-m11p09", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p09-skills", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p09-taskq", ignore_errors=True)
    shutil.rmtree(FIX, ignore_errors=True)


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


def new_job(sample):
    st, r = api("POST", "/api/jobs", {"sample_id": sample, "mode": "auto",
                                      "execution_mode": "controlled"})
    return st, r


def cancel_job(r):
    api("POST", "/api/approvals/" + r["approval_id"],
        {"approve": False, "note": "p09: reach a terminal state"})
    t0 = time.time()
    while time.time() - t0 < 60:
        st, j = api("GET", "/api/tasks/" + r["job_id"])
        if st == 200 and j.get("state") in ("cancelled", "failed"):
            return j["state"]
        time.sleep(1.0)
    return "?"


def db(sql, args=(), ro=True):
    uri = f"file:{ROOT / 'data' / 'm11p09.sqlite3'}?mode=ro" if ro else \
        str(ROOT / "data" / "m11p09.sqlite3")
    con = sqlite3.connect(uri, uri=ro)
    try:
        cur = con.execute(sql, args)
        rows = cur.fetchall()
        con.commit()
        return rows
    finally:
        con.close()


# ================= 1: flags exist from creation ========================
st, j1 = new_job("P09FLAGS")
jid = j1["job_id"]
row = jobs_list()[jid]
check("1a a fresh job row exposes the management flags at their "
      "honest defaults",
      row.get("pinned") == 0 and row.get("locked") == 0
      and row.get("trashed") == 0 and row.get("trashed_at") == "",
      str({k: row.get(k) for k in ("pinned", "locked", "trashed",
                                   "trashed_at")}))
st, fr = api("POST", f"/api/jobs/{jid}/flags", {"pinned": True})
ok_row = jobs_list()[jid]
n_audit = db("SELECT COUNT(*) FROM audit WHERE object=? AND "
             "action='task.flags.set'", (jid,))[0][0]
check("1b pin sticks, is exposed, and is audited",
      st == 200 and fr["flags"]["pinned"] == 1
      and ok_row.get("pinned") == 1 and n_audit >= 1,
      f"{st} {str(fr)[:120]} audit={n_audit}")

# ================= 2: trash rules (single path) ========================
st, r = api("POST", f"/api/jobs/{jid}/flags", {"trashed": True})
check("2a an ACTIVE (blocked) task cannot be trashed: the refusal "
      "names cancel-first (validated, not implicit)",
      st == 409 and "cancel" in r.get("error", ""), f"{st} {str(r)[:120]}")
cancel_job(j1)
st, r = api("POST", f"/api/jobs/{jid}/flags", {"trashed": True})
row = jobs_list().get(jid)
in_boot = row is not None
st2, tr = api("GET", "/api/jobs?trashed=1")
listed = [x for x in tr.get("jobs", []) if x["id"] == jid]
check("2b a cancelled task can be trashed; the record leaves the "
      "normal listing and answers the trash view with its stamp",
      st == 200 and r["flags"]["trashed"] == 1 and not in_boot
      and listed and listed[0]["trashed_at"],
      f"{st} in_boot={in_boot} {str(listed)[:160]}")
st, r = api("POST", f"/api/jobs/{jid}/flags", {"trashed": False})
row = jobs_list().get(jid) or {}
check("2c restore returns the record with its flags intact",
      st == 200 and row.get("trashed") == 0
      and row.get("trashed_at") == "" and row.get("pinned") == 1,
      f"{st} {str(row)[:160]}")

# ================= 3: the lock refuses trash (and purge) ===============
st, j2 = new_job("P09LOCK")
jid2 = j2["job_id"]
cancel_job(j2)
api("POST", f"/api/jobs/{jid2}/flags", {"locked": True})
st, r = api("POST", f"/api/jobs/{jid2}/flags", {"trashed": True})
check("3a a LOCKED task cannot be trashed - the refusal says unlock "
      "first", st == 409 and "unlock" in r.get("error", ""),
      f"{st} {str(r)[:120]}")
st, r = api("POST", f"/api/jobs/{jid2}/flags", {"locked": False})
st, r = api("POST", f"/api/jobs/{jid2}/flags", {"trashed": True})
check("3b after unlock the trash works normally",
      st == 200 and r["flags"]["trashed"] == 1, f"{st} {str(r)[:120]}")

# ================= 4: bulk is the SAME rule per id =====================
st, j3 = new_job("P09BULK")
jid3 = j3["job_id"]
cancel_job(j3)
api("POST", f"/api/jobs/{jid3}/flags", {"locked": True})
st, br = api("POST", "/api/jobs/flags/bulk",
             {"ids": [jid, jid3, "job-nope-000"], "trashed": True})
res = {x["id"]: x for x in br.get("results", [])}
check("4a bulk returns ONE honest line per id: the unlocked task is "
      "trashed, the LOCKED one is refused (lock blocks bulk exactly "
      "as it blocks individual), an unknown id says 404",
      st == 200 and res.get(jid, {}).get("ok") is True
      and res.get(jid3, {}).get("ok") is False
      and "unlock" in res.get(jid3, {}).get("error", "")
      and res.get("job-nope-000", {}).get("status") == 404,
      f"{st} {str(br)[:220]}")
st_a, e_a = api("POST", "/api/jobs/flags/bulk", {"ids": [jid3]})
st_b, e_b = api("POST", "/api/jobs/flags/bulk",
                {"ids": [f"x{i}" for i in range(101)], "pinned": True})
check("4b bulk refuses nonsense loudly: no flags -> 400, "
      ">100 ids -> 400",
      st_a == 400 and st_b == 400, f"{st_a} {st_b}")

# ================= 5: purge = records only, files never ================
FIX.mkdir(parents=True, exist_ok=True)
shared = FIX / "shared_artifact.tsv"
shared.write_text("peptide\thla\nQAHQLVTVI\tA*02:01\n", encoding="utf-8")
digest0 = hashlib.sha256(shared.read_bytes()).hexdigest()
# jid (purged here) and jid3 (kept alive) BOTH reference the file:
# one direct artifacts registry row each, like a real run would make.
for _jid in (jid, jid3):
    db("INSERT INTO artifacts(id,job_id,created_at,type,name,strategy,"
       "path,status,rows) VALUES(?,?,?,?,?,?,?,?,?)",
       (f"art-{_jid}", _jid, "2026-11-19T00:00:00+08:00", "table",
        "shared_artifact.tsv", "s0", str(shared), "available", 1),
       ro=False)
st, r = api("POST", f"/api/jobs/{jid}/purge", {"confirm": True})
check("5a purge without the typed id is refused 400 (a bare confirm "
      "flag is not confirmation)",
      st == 400 and "confirm" in json.dumps(r), f"{st} {str(r)[:120]}")
st, r = api("POST", f"/api/jobs/{jid}/purge", {"confirm": "wrong-id"})
check("5b a mismatched confirmation is refused", st == 400, f"{st}")
st, r = api("POST", f"/api/jobs/{jid}/purge", {"confirm": jid})
gone_job = db("SELECT COUNT(*) FROM jobs WHERE id=?", (jid,))[0][0]
gone_ev = db("SELECT COUNT(*) FROM job_events WHERE job_id=?",
             (jid,))[0][0]
gone_art = db("SELECT COUNT(*) FROM artifacts WHERE job_id=?",
              (jid,))[0][0]
audit_p = db("SELECT detail_json FROM audit WHERE object=? AND "
             "action='task.purged'", (jid,))
digest1 = hashlib.sha256(shared.read_bytes()).hexdigest()
art_other = db("SELECT COUNT(*) FROM artifacts WHERE job_id=?",
               (jid3,))[0][0]
check("5c purge deletes the RECORDS (job row + events + its "
      "output-manifest rows) - audit says what was removed and that "
      "no files were touched",
      st == 200 and gone_job == 0 and gone_ev == 0 and gone_art == 0
      and audit_p and '"files_touched": false' in audit_p[0][0],
      f"{st} job={gone_job} ev={gone_ev} art={gone_art} "
      f"audit={str(audit_p)[:120]}")
check("5d files are NEVER part of any of this: the shared artifact's "
      "bytes are identical after the purge and the OTHER job's "
      "reference keeps working",
      digest1 == digest0 and art_other == 1
      and shared.exists(),
      f"{digest1[:12]} vs {digest0[:12]} other={art_other}")
st, r = api("GET", f"/api/results/{jid}")
check("5e the purged record is honestly gone from results (404, not "
      "an empty 200)", st == 404, f"{st}")
api("POST", f"/api/jobs/{jid3}/flags", {"locked": True})
st, r = api("POST", f"/api/jobs/{jid3}/purge", {"confirm": jid3})
check("5f a LOCKED task refuses purge as well",
      st == 409 and "lock" in r.get("error", "").lower(),
      f"{st} {str(r)[:120]}")
st, j5 = new_job("P09ACTIVE")
st, r = api("POST", f"/api/jobs/{j5['job_id']}/purge",
            {"confirm": j5["job_id"]})
check("5g an ACTIVE task refuses purge (cancel first)",
      st == 409 and "cancel" in r.get("error", ""), f"{st} {str(r)[:120]}")

# ================= 6: research rows are not job records ================
st, rr = api("POST", "/api/research", {"scope": "full",
                                       "target": "presentation",
                                       "engine": "example"})
rid = rr.get("research_id") or ""
st2, r2 = api("POST", f"/api/jobs/{rid}/flags", {"trashed": True})
check("6a the flag endpoint refuses a research id (404): research is "
      "not a job record and cannot be trashed through this door",
      st == 202 and rid and st2 == 404, f"{st} {st2} {rid}")

# ================= 7: retention is a display, persisted ================
st, cur = api("POST", "/api/settings", {"trash_retention_days": 45})
check("7a trash_retention_days persists through the settings "
      "whitelist", st == 200 and cur.get("trash_retention_days") == 45,
      f"{st} {str(cur)[:120]}")

# ================= 8: the UI contract =================================
html = fetch("/")
js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
css = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")
check("8a the Tasks card carries the management toolbar (search, "
      "sort, trash view, bulk bar)",
      'id="tm-search"' in html and 'id="tm-sort"' in html
      and 'id="tm-trash-toggle"' in html and 'id="tm-bulk"' in html, "")
check("8b rows carry checkbox + flag chips; research rows are "
      "excluded from the checkbox by id",
      "function jrCheck" in js
      and 'class="jr-check"' in js and "function jrFlags" in js
      and 'if (/^res-/.test(j.id)) return "";' in js, "")
check("8c trash view is honest copy: record-level note, restore and "
      "the typed-id purge confirmation, and bulk goes through the "
      "SAME /flags/bulk rule",
      "tm.trashNote" in js and 'data-restore=' in js
      and "mountPurgeConfirm" in js and "tm.purgeMismatch" in js
      and "/api/jobs/flags/bulk" in js, "")
check("8d the head shows the record controls with the WHY in the "
      "title (cancel-first / locked) instead of silent graying",
      "function taskFlagsRow" in js and "tm.lockedFirst" in js
      and "tm.cancelFirst" in js, "")
check("8e retention appears as a DISPLAY with the honest statement "
      "that nothing is ever removed automatically",
      "tm.retentionHint" in js and "tm.trashAge" in js, "")
i18n = js[js.index("const I18N = {"):]
langs = dict(re.findall(r'\n  (en|zh):\s*\{(.*?)\n  \},?', i18n,
                        re.S))
for name in ("en", "zh"):
    keys = set(re.findall(r'"([a-z0-9]+\.[A-Za-z0-9]+)"\s*:',
                          langs.get(name, "")))
    lack = [k for k in ("tm.search", "tm.trashNote", "tm.retentionHint",
                        "tm.purgeNote", "tm.bulkPart", "tm.trashAge",
                        "tm.lockedFirst", "tm.cancelFirst")
            if k not in keys]
    check(f"8f[{name}] the {name} dictionary carries the task-"
          f"management copy", not lack, str(lack))
check("8g styles for the management controls exist",
      ".tm-bar" in css and ".jr-flag" in css and ".tm-bulk" in css, "")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f_ in FAIL:
        print("FAILED:", f_)
    sys.exit(1)
