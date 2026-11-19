"""INPUT-discovery regression suite (incident 2026-11-19).

NOTE: distinct from verify_discovery.py, which covers the M4 RESEARCH
discovery engine. This suite covers the Tasks-view "Discover inputs"
route (/api/native/discover) and its frontend contract.

A dataset root on the scratch volume was entered in the Web UI, every
click was answered by the backend with HTTP 400 + a precise error, and
the operator saw NOTHING: renderPrepFlow() cleared #prep-error right
after the catch block wrote it, so the failure card self-erased in 0 ms.
Eight identical audit rows (ok:false, found:0) proved the requests were
submitted and rejected, never stalled.

What this suite pins down:
 D1 an unauthorized-but-existing directory gets an EXPLICIT 400 naming
    the authorized roots (never a silent 200, never a fabricated scan)
 D2 a missing directory and a relative path are refused explicitly
 D3 nested input folders are recognized: scanning a CASE ROOT whose
    files live under inputs/ detects them (root.rglob in the native
    detector) - no individual file paths required from the user
 D4 the answer is the GENUINE native detector's (engine field), fully
    structured (inputs/ambiguous/missing/warnings/summary), and never
    cites a path that does not exist
 D5 frontend contract (static): renderPrepFlow must not touch
    #prep-error; the catch renders the card AFTER the rebuild; the busy
    state, duplicate-generation guard and timeout ceiling stay wired

Run: python3 tests/verify_input_discovery.py  (port 8795, throwaway DB)
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
PORT = 8795
BASE = f"http://127.0.0.1:{PORT}"
DB = "disc95.sqlite3"
TMPROOT = pathlib.Path("/tmp/neoag-disc95-root")
FIXDIR = ROOT.parent / "neoag_agent_ws" / "test_data"
APPJS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
PASS = FAIL = 0
NOTES = []


def check(name, cond, extra=""):
    global PASS, FAIL
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{extra}]"))
    PASS, FAIL = PASS + (1 if cond else 0), FAIL + (0 if cond else 1)


def note(msg):
    NOTES.append(msg)
    print("NOTE " + msg)


if not FIXDIR.is_dir():
    sys.exit(f"fixture directory missing: {FIXDIR}")

# nested dataset: CASE ROOT -> inputs/ (real fixtures) -> decoy folder
shutil.rmtree(TMPROOT, ignore_errors=True)
case = TMPROOT / "CASEA"
(case / "inputs").mkdir(parents=True)
for fn in ("test001_tumor_dna.bam", "test001_tumor_dna.bam.bai",
           "test001_normal_dna.bam", "test001_normal_dna.bam.bai",
           "test001_rna.bam", "test001_rna.bam.bai",
           "test001_somatic_filtered.vcf", "test001_hla_typing.tsv"):
    shutil.copy2(FIXDIR / fn, case / "inputs" / fn)
(case / "notes").mkdir()
(case / "notes" / "README.txt").write_text("decoy: must not be classified\n")

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH = "Basic " + base64.b64encode(f"{_a['user']}:{_a['password']}".encode()).decode()

try:
    (ROOT / "data" / DB).unlink()
except OSError:
    pass
_srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB=DB, NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-disc95-sk",
             NEOAG_TASKQ_DIR="/tmp/neoag-disc95-tq",
             NEOAG_INPUT_ROOTS=str(TMPROOT) + os.pathsep + "/data1/neoag"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _cleanup():
    _srv.terminate()
    try:
        _srv.wait(timeout=8)
    except Exception:
        _srv.kill()
    try:
        (ROOT / "data" / DB).unlink()
    except OSError:
        pass
    shutil.rmtree(TMPROOT, ignore_errors=True)
    for d in ("/tmp/neoag-disc95-sk", "/tmp/neoag-disc95-tq"):
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup)


def call(method, path, body=None, timeout=240):
    req = urllib.request.Request(
        BASE + path, data=(json.dumps(body).encode()
                           if body is not None else None),
        method=method,
        headers={"Content-Type": "application/json", "Authorization": AUTH})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


for _ in range(80):
    try:
        urllib.request.urlopen(BASE + "/api/bootstrap", timeout=1)
        break
    except urllib.error.HTTPError:
        break                       # 401 = server alive, auth on
    except Exception:
        time.sleep(0.5)

# ---------- D1: the incident's own rejection, now EXPLICIT ----------
st, b = call("POST", "/api/native/discover", {"data_dir": "/etc"})
check("D1 unauthorized-but-existing dir -> explicit 400 naming the roots",
      st == 400 and b.get("ok") is False and "authorized" in str(b.get("error", "")),
      f"{st} {str(b)[:180]}")

# ---------- D2: other refusals stay explicit, never silent ----------
st, b = call("POST", "/api/native/discover", {"data_dir": str(TMPROOT / "nope")})
check("D2a missing dir -> 400 'not found' (nothing invented)",
      st == 400 and "not found" in str(b.get("error", "")), f"{st} {str(b)[:140]}")
st, b = call("POST", "/api/native/discover", {"data_dir": "relative/path"})
check("D2b relative path -> 400 'absolute ... required'",
      st == 400 and "absolute" in str(b.get("error", "")), f"{st} {str(b)[:140]}")

# ---------- D3: nested input folders are recognized from the ROOT ----------
t0 = time.time()
st, d = call("POST", "/api/native/discover", {"data_dir": str(case)})
dt = time.time() - t0
rows = d.get("inputs") or []
found_flags = {r.get("flag") for r in rows}
nested = [r for r in rows if "/inputs/" in r.get("path", "")]
check(f"D3a dataset ROOT (files nested under inputs/) discovers ({dt:.1f}s)",
      st == 200 and d.get("ok") is True and bool(rows), f"{st} {str(d)[:180]}")
check("D3b the nested paths were recognized without per-file input",
      bool(rows) and len(nested) == len(rows),
      f"{len(nested)}/{len(rows)} nested")
for want in ("tumor_dna_bam", "normal_dna_bam", "somatic_vcf", "hla_file"):
    check(f"D3c nested detection includes {want}", want in found_flags,
          str(sorted(found_flags)))

# ---------- D4: genuine native detector, structured, nothing fabricated --
check("D4a the GENUINE native detector ran (no web-layer mock substitute)",
      (d.get("engine") or {}).get("detector")
      == "neoag.open_neo.routing.scan_input_directory",
      str(d.get("engine"))[:160])
check("D4b structured result: inputs/ambiguous/missing/warnings/summary",
      all(k in d for k in ("inputs", "ambiguous", "missing", "warnings",
                           "summary")), str(list(d))[:160])
bad = [r for r in rows
       if not (pathlib.Path(r.get("path", "")).is_file()
               and r["path"].startswith(str(case)))]
check("D4c every cited input path exists under the scanned root",
      bool(rows) and not bad, str(bad)[:160])
check("D4d decoy non-input folder was not classified as data",
      not any("notes/README" in r.get("path", "") for r in rows))

# ---------- D5: frontend contract (static) ----------
i = APPJS.find("function renderPrepFlow")
body = APPJS[i:i + 900]
check("D5a renderPrepFlow must NOT touch #prep-error (incident wipe fix)",
      '$("prep-error")' not in body, body[:200])
seg = APPJS[APPJS.find('"/api/native/discover"'):
            APPJS.find("clearInterval(tick)")]
catch = seg[seg.find("} catch (e) {"):seg.find("} finally {")]
check("D5b inside catch: failure card is written AFTER renderPrepFlow rebuild",
      -1 < catch.find("renderPrepFlow();")
      < catch.find('$("prep-error").innerHTML = alertHtml('),
      str((catch.find("renderPrepFlow();"),
           catch.find('$("prep-error").innerHTML = alertHtml('))))
check("D5c busy state + duplicate-generation guard + timeout ceiling wired",
      "btn.disabled = busy" in APPJS and "++discReq" in APPJS
      and re.search(r'apiWithTimeout\(240000,\s*"POST",\s*\n?\s*"/api/native/discover"',
                    APPJS) is not None, "")
check("D5d the message carries the HTTP status for diagnosis",
      "HTTP ${e.status}" in seg, "")

print(f"\nverify_input_discovery: {PASS} pass, {FAIL} fail, {len(NOTES)} note(s)")
sys.exit(1 if FAIL else 0)
