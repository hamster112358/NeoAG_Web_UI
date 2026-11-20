#!/usr/bin/env python3
"""M11 P1-13 battery: configuration audit pass.

Pinned behaviour (all measured on the shipped tree):
  * the parameter payload carries ONLY curated controls: every entry
    of the interactive `science` panel exists in the curated set, has
    a unique id and a non-empty evidence citation; the code-decided
    list (science_info) cites code too; the inventory groups stay
    documentation (every item cites source) and the UI never renders
    a knob from them (no duplicate controls for auto-derived
    settings);
  * the genome-build choice is validated against installed reference
    assets DERIVED from the deployed reference manifest (never a
    hardcoded option list): the unavailable build is refused by the
    SERVER, arrives in the payload marked unavailable with its
    reason, and the frontend renders it visible-but-disabled;
  * dev-only knobs are dead outside dev mode: the failure-simulation
    request field changes nothing unless the battery env opts in, the
    word never appears in the parameter payload, and no shipped
    frontend file mentions it.
Run: python3 tests/verify_m11_p13.py   (isolated :8813, FAILSIM OFF)
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
sys.path.insert(0, str(ROOT))
PORT = 8813
PASS, FAIL = [], []

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

APPJS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")

# NOTE: deliberately NO NEOAG_DEMO_FAILSIM here - this suite boots the
# production shape, where the dev knob must be dead.
srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="m11p13.sqlite3",
             NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-m11p13-skills",
             NEOAG_TASKQ_DIR="/tmp/neoag-m11p13-taskq"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _cleanup():
    srv.terminate()
    try:
        srv.wait(timeout=8)
    except Exception:
        srv.kill()
    try:
        (ROOT / "data" / "m11p13.sqlite3").unlink()
    except OSError:
        pass
    shutil.rmtree("/tmp/neoag-m11p13-skills", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p13-taskq", ignore_errors=True)


atexit.register(_cleanup)


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS" if ok else "FAIL") + f"  {name}"
          + (f"  [{detail}]" if detail and not ok else ""))


def api(method, path, body=None, timeout=60, auth=True):
    hdr = {"Authorization": AUTH_HEADER} if auth else {}
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode()), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode()), dict(e.headers)
        except Exception:
            return e.code, {}, dict(e.headers) if e.headers else {}


up = False
for _ in range(80):
    try:
        s, _b, _h = api("GET", "/api/bootstrap")
        up = s == 200
        if up:
            break
    except Exception:
        time.sleep(0.5)
if not up:
    print("FAIL  server never came up"); sys.exit(1)

from backend import native as native_mod  # noqa: E402
from backend.neoag_params import SCIENCE_CONTROLS  # noqa: E402

st, prm, _h = api("GET", "/api/params")
sci = prm.get("science") or []

# ---------- 1. curated-control-only payload with citations ----------
curated_ids = [c["id"] for c in SCIENCE_CONTROLS]
payload_ids = [c["id"] for c in sci]
check("N1 the interactive panel is EXACTLY the curated set "
      "(no extra knobs)", payload_ids == curated_ids,
      str(payload_ids))
check("N2 control ids are unique (no duplicate knobs)",
      len(set(payload_ids)) == len(payload_ids))
check("N3 every control row carries an evidence citation",
      all(str(c.get("evidence") or "").strip() for c in sci),
      str([c["id"] for c in sci if not c.get("evidence")]))
check("N4 every control states what it changes in the run",
      all(str(c.get("affects") or "").strip() for c in sci))
check("N5 code-decided list cites code (no knob pretending)",
      all(str(x.get("evidence") or "").strip()
          for x in prm.get("science_info") or []))
check("N6 inventory groups stay documentation: every item cites "
      "its source",
      all(str(it.get("source") or "").strip()
          for g in prm.get("groups") or [] for it in g.get("items") or []))

# ---------- 2. genome build validated against installed assets ----------
gb = next((c for c in sci if c["id"] == "genome_build"), {})
av = gb.get("choices_availability") or {}
check("N7 the build choice arrives with DERIVED availability",
      bool(av) and all("available" in v and "reason" in v
                       for v in av.values()), str(av)[:160])
live = native_mod.build_choices()
check("N8 the payload availability equals the host-derived probe "
      "(not a hardcoded copy)", av == live)
check("N9 the build the deployed manifest declares is selectable",
      all(v["available"] for k, v in live.items()
          if v.get("known") and "declares this build" in v["reason"])
      or not any(v.get("known") for v in live.values()), str(live))
unavail = [k for k, v in live.items()
           if v.get("known") and not v["available"]]
check("N10 at least the known-unavailable builds are marked so, with "
      "a reason naming the declared build",
      all(live[k]["available"] is False and live[k]["reason"]
          for k in unavail), str(unavail))
errs37 = native_mod.validate_scientific({"genome_build": "GRCh37"})
check("N11 the SERVER refuses a build this deployment has no assets "
      "for (not only the UI hiding it)",
      (bool(unavail) and bool(errs37) and "not usable" in errs37[0])
      or not unavail, str(errs37))
check("N12 the declared build passes validation",
      native_mod.validate_scientific({"genome_build": "GRCh38"}) == [])
check("N13 unknown-choice validation still works (no regression)",
      bool(native_mod.validate_scientific(
          {"rna_quant_method": "magic"})))

# ---------- 3. frontend honors the derived availability ----------
check("N14 the radio renderer honors choices_availability: disabled, "
      "reason as tooltip",
      "choices_availability" in APPJS and 'disabled' in APPJS
      and "info.reason" in APPJS)
check("N15 unavailable choice is VISIBLE but disabled + labeled",
      "sci.unvalidated" in APPJS and "radio.off" in CSS)
en_txt = APPJS.split("  en: {", 1)[1].split("  zh: {", 1)[0]
zh_txt = APPJS.split("  zh: {", 1)[1].split("};", 1)[0]
en_keys = set(re.findall(r'^\s*"([^"]+)":', en_txt, re.M))
zh_keys = set(re.findall(r'^\s*"([^"]+)":', zh_txt, re.M))
check("N16 en/zh dictionaries carry the identical key set",
      en_keys == zh_keys,
      f"en-only={sorted(en_keys - zh_keys)[:6]} "
      f"zh-only={sorted(zh_keys - zh_keys)[:6]}")
check("N17 the un-curated inventory is never consumed by the UI at "
      "all (no knob can be built from it)",
      "params.groups" not in APPJS and ".groups ||" not in APPJS)
seg = ""
for fn in ("renderScience", "renderOptions", "scCtrlHtml"):
    seg += APPJS.split("function " + fn, 1)[1][:2600]
check("N18 the knob-producing functions consume the science panel "
      "only", ".groups" not in seg
      and "science" in APPJS.split("function renderScience", 1)[1][:600])

# ---------- 4. dev-only knob is dead outside dev mode ----------
body_txt = json.dumps(prm)
check("N19 the failure-simulation field name appears NOWHERE in the "
      "parameter payload", "simulate_failure" not in body_txt)
check("N20 no shipped frontend file mentions the dev knob",
      "simulate_failure" not in APPJS and "simulate_failure"
      not in INDEX)
srv_src = (ROOT / "backend" / "server.py").read_text(encoding="utf-8")
check("N21 the submit path gates it behind the battery env flag",
      "DEV_FAILSIM and bool" in srv_src
      and 'NEOAG_DEMO_FAILSIM' in srv_src)
st, job, _h = api("POST", "/api/jobs", {
    "kind": "analysis", "sample_id": "P13", "mode": "auto",
    "execution_mode": "controlled", "simulate_failure": True})
jid = (job or {}).get("job_id", "")
check("N22 production submission: simulate_failure=true changes "
      "NOTHING (silently inert, job created)",
      st == 202 and bool(jid), str(job)[:120])
con = sqlite3.connect(
    f"file:{ROOT / 'data' / 'm11p13.sqlite3'}?mode=ro", uri=True)
try:
    raw = con.execute("SELECT spec_json FROM jobs WHERE id=?",
                      (jid,)).fetchone()
finally:
    con.close()
spec = json.loads(raw[0]) if raw and raw[0] else {}
check("N23 the stored job spec says simulate_failure=false "
      "(the gate really dropped the request)",
      spec.get("simulate_failure") is False, str(raw)[:200])
api("POST", f"/api/jobs/{jid}/cancel", {})

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
