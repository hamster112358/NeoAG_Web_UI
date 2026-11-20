#!/usr/bin/env python3
"""M11 P1-12 battery: humanized notices + ONE shared exec/science
phrasing pair on every surface.

Pinned behaviour (all measured on the shipped tree, none remembered):
  * the shared backend phrasing module turns ANY payload (dict, JSON,
    list, stderr text) into one plain sentence: never a brace, never
    a newline, trimming is stated, and the internal-only phrase of
    the regression never appears;
  * stored conversation rows are grepped end-to-end (real server +
    real stored messages): no brace characters, no dict patterns,
    no internal phrases;
  * a real task-failure notice passes through the module and lands
    in the owning conversation with the EXECUTION sentence and the
    SCIENTIFIC sentence separate and labeled;
  * the guidance text states only controls that really exist (pause
    is never promised where the card disables it);
  * frontend: one shared phrasing pair (sciPill/execPill/
    statusPairText) is what the Tasks head, the unified rows, the
    Discovery rows and the Results picker/header all compose; the ad
    hoc research-only pill and the raw-outcome dropdown label are
    gone; en/zh key parity stays enforced.
Run: python3 tests/verify_m11_p12.py   (isolated :8811)
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
import tempfile
import time
import urllib.request
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PORT = 8811
PASS, FAIL = [], []

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

APPJS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")

srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="m11p12.sqlite3",
             NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-m11p12-skills",
             NEOAG_TASKQ_DIR="/tmp/neoag-m11p12-taskq",
             NEOAG_GATE_TOKEN_DISABLE="1"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _cleanup():
    srv.terminate()
    try:
        srv.wait(timeout=8)
    except Exception:
        srv.kill()
    try:
        (ROOT / "data" / "m11p12.sqlite3").unlink()
    except OSError:
        pass
    shutil.rmtree("/tmp/neoag-m11p12-skills", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p12-taskq", ignore_errors=True)


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

from backend import phrasing as P  # noqa: E402

# ---------- 1. phrasing module units ----------
d = {"exit_code": 1, "detail": {"stderr": "boom\nfailed at step 7"}}
s1 = P.human_detail(d)
check("P1 human_detail renders dict fields without braces",
      "{" not in s1 and "}" not in s1 and "[" not in s1
      and "exit_code" in s1 and "step 7" in s1, s1)
s2 = P.human_detail('{"code": "NATIVE_PLAN_FAIL", "msg": "no ref"}')
check("P2 human_detail PARSES a JSON string and renders its fields",
      "{" not in s2 and "NATIVE_PLAN_FAIL" in s2 and "no ref" in s2, s2)
s3 = P.human_detail(["first [issue]", "second {issue}"])
check("P3 human_detail flattens lists brace-free",
      "{" not in s3 and "}" not in s3 and "first" in s3, s3)
s4 = P.human_detail("line one\nline two\twith   spaces\n")
check("P4 multi-line input collapses to ONE line",
      "\n" not in s4 and "line one line two" in s4, s4)
big = "x " * 400
s5 = P.human_detail(big)
check("P5 long text is trimmed AND the trim is stated (never silent)",
      "trimmed from" in s5 and len(s5) <= 260, f"len={len(s5)}")
s6 = P.human_detail("Gateway detail: the plan blocked")
check("P6 the internal-only phrase is STRIPPED, content kept",
      "gateway" not in s6.lower() and "plan blocked" in s6, s6)
check("P7 None -> empty string", P.human_detail(None) == "")

ep = P.execution_phrase("failed", True)
check("P8 execution sentence is labeled and says computation ran",
      ep.startswith("Execution status:") and "computation had started" in ep)
sp = P.scientific_phrase("nothing - no results were produced")
check("P9 scientific sentence is its own labeled sentence",
      sp.startswith("Scientific outcome:"))

t1 = P.turn_ended_text("failed", {"detail": "died {here: [1]}"})
check("P10 relay terminal: no braces, no banned phrase, honesty kept",
      "{" not in t1 and "}" not in t1
      and "gateway detail" not in t1.lower()
      and "does not fabricate" in t1 and "failed" in t1, t1)

rep = {"reason": "the planner did not clear this run",
       "blockers": ["missing file {x}", "no reference genome"],
       "next_step": "resolve the first blocking issue",
       "started_computation": False,
       "code": "PLAN_BLOCKED", "phase": "planning"}
fn = P.failure_notice_text("job-7", rep, ["input discovery"], 0, "T")
check("P11 failure notice: labeled EXECUTION sentence present",
      "Execution status: failed" in fn and "before any computation" in fn)
check("P12 failure notice: labeled SCIENTIFIC sentence present",
      "Scientific outcome: nothing" in fn)
check("P13 failure notice: brace-free, banned-phrase-free, reason kept",
      "{" not in fn and "}" not in fn and "gateway detail"
      not in fn.lower() and "planner did not clear" in fn, fn)
check("P14 failure notice keeps blockers + next step",
      "Blocking issue" in fn and "Next step:" in fn)

check("P15 control guidance never promises pause where it is disabled",
      "cannot be paused" in P.CONTROL_GUIDANCE
      and "Stop/cancel" in P.CONTROL_GUIDANCE)

# ---------- 2. shipped-tree greps ----------
hits = []
for py in (ROOT / "backend").glob("*.py"):
    if py.name == "phrasing.py":
        continue
    if re.search(r"gateway\s+detail",
                 py.read_text(encoding="utf-8"), re.I):
        hits.append(py.name)
check("P16 the internal-only phrase appears NOWHERE in backend/ "
      "(except the module's own concatenated constant)", not hits,
      str(hits))
agent_src = (ROOT / "backend" / "agent.py").read_text(encoding="utf-8")
jobs_src = (ROOT / "backend/jobs.py".replace("/", os.sep)).read_text(
    encoding="utf-8")
check("P17 the relay terminal message goes through the shared module",
      "phrasing.turn_ended_text(" in agent_src)
check("P18 every relay exception path humanizes its payload",
      agent_src.count("phrasing.human_detail(") >= 3)
check("P19 task-failure notices go through the shared module",
      "phrasing.failure_notice_text(" in jobs_src)
check("P20 the mock-mode control answer is the honest guidance",
      "phrasing.CONTROL_GUIDANCE" in agent_src
      and "Use the pause / stop / resume buttons" not in agent_src)

# ---------- 3. frontend: ONE shared phrasing pair everywhere ----------
check("P21 the shared pair helpers exist",
      "function sciPill(" in APPJS and "function execPill(" in APPJS
      and "function statusPairText(" in APPJS)
check("P22 the ad-hoc research-only pill is deleted",
      "function resPill(" not in APPJS and "resPill(" not in APPJS)
for surf, frag in (
        ("Tasks head", "function taskHeadHtml"),
        ("unified rows", "function jobRowHtml"),
        ("Discovery rows", "function renderDiscovery"),
        ("Results picker", "async function renderResults")):
    seg = APPJS.split(frag, 1)[1][:2600] if frag in APPJS else ""
    uses = any(h in seg for h in ("sciPill(", "execPill(",
                                  "statusPairText("))
    check(f"P23 {surf} compose the shared helpers", uses)
check("P24 the raw-outcome dropdown label is gone",
      'j.outcome.replace(/_/g, " ")' not in APPJS)
en_txt = APPJS.split("  en: {", 1)[1].split("  zh: {", 1)[0]
zh_txt = APPJS.split("  zh: {", 1)[1].split("};", 1)[0]
en_keys = set(re.findall(r'^\s*"([^"]+)":', en_txt, re.M))
zh_keys = set(re.findall(r'^\s*"([^"]+)":', zh_txt, re.M))
check("P25 en/zh dictionaries carry the identical key set",
      en_keys == zh_keys,
      f"en-only={sorted(en_keys - zh_keys)[:6]} "
      f"zh-only={sorted(zh_keys - en_keys)[:6]}")

# ---------- 4. real stored conversation rows: zero raw dicts ----------
st, c, _h = api("POST", "/api/chat", {"text": "status", "title": "p12"})
cid = c.get("conv_id") or ""
check("P26 chat turn stored (fresh conversation)", st == 200 and bool(cid))
time.sleep(3.0)
st, msgs, _h = api("GET", f"/api/conversations/{cid}")
rows = msgs if isinstance(msgs, list) else msgs.get("messages", [])
bad = [m for m in rows
       if "{" in str(m.get("text", "")) or "}" in str(m.get("text", ""))
       or re.search(r"gateway\s+detail", str(m.get("text", "")), re.I)]
check("P27 every stored message row is brace-free and clean",
      st == 200 and rows and not bad,
      json.dumps(bad[:1])[:200] if bad else f"n={len(rows)}")

# ---------- 5. research rows carry the scientific class ----------
st, r, _h = api("POST", "/api/research", {"scope": "full",
                                          "target": "presentation",
                                          "engine": "example"})
rid = (r or {}).get("research_id", "")
done = False
t0 = time.time()
while time.time() - t0 < 30:
    st, rr, _h = api("GET", f"/api/research/{rid}")
    if (rr or {}).get("status") == "completed":
        done = True
        break
    time.sleep(0.5)
st, boot, _h = api("GET", "/api/bootstrap")
rrow = next((x for x in boot.get("research", [])
             if x.get("id") == rid), {})
check("P28 research run completed (example engine)", done)
check("P29 boot research row carries the scientific field",
      "outcome" in rrow and rrow["outcome"] in
      ("", "decision_ready", "preliminary"), str(rrow.get("outcome")))
check("P30 a non-decision_ready review is labeled preliminary, "
      "never silently 'completed' science",
      rrow.get("outcome") in ("preliminary", "decision_ready"),
      str(rrow))

# ---------- 6. failure notice lands in the conversation via the
#             REAL store (in-process JobManager, temp db) ----------
sys.path.insert(0, str(ROOT))
from backend import jobs as J  # noqa: E402
from backend.store import Store  # noqa: E402

tmpd = tempfile.mkdtemp(prefix="m11p12-store")
try:
    store2 = Store(pathlib.Path(tmpd))
    jm = J.JobManager(store2, lambda e: None)
    cv = store2.conv_create("p12 failure")
    store2.job_create("job-p12", "mock-analysis", "S1", {}, "running",
                      None, conv_id=cv["id"])
    report = J.JobManager._humanize_native_failure(
        "GATEWAY_SUBMISSION_FAILED", "refused", None, "gateway",
        "submission")
    check("P31 gateway-submission next step is human phrasing",
          "gateway detail" not in " ".join(
              str(v) for v in report.values()).lower()
          and "technical details" in report["next_step"],
          report["next_step"])
    jm._notify_failure("job-p12", report, [], 0)
    rows2 = store2.msg_list(conv_id=cv["id"])
    txt = " ".join(str(m.get("text", "")) for m in rows2)
    check("P32 the stored failure row: labeled pair, no braces, no "
          "internal phrase",
          "Execution status: failed" in txt
          and "Scientific outcome:" in txt
          and "{" not in txt and "gateway detail" not in txt.lower(),
          txt[:180])
finally:
    shutil.rmtree(tmpd, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
