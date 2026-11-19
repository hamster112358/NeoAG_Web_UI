#!/usr/bin/env python3
"""M11 P0-6 battery: scientific selection grounded in native stages.

Pinned behaviour:
  * run-direction rows: the DNA SV branch is OFF by default HERE
    (web_run_default False, different from native's default, so the
    payload states skip_dna_sv explicitly - silence would mean the
    opposite of what the UI shows); ticking it RUNS the branch.
  * switching DNA-SV off must not disable RNA fusion: proven against
    the REAL native planner on the read-only CXLMINI case (both plans
    PASS, fusion & splice rows stay READY, no new blockers) and by a
    structural assertion on the deployed production generator (the
    skip flag gates only the dna_sv stage; fusion evidence lines are
    collected independently).
  * the tumor/normal identity check is not casually skippable: the
    backend refuses skip_bam_matcher without a separate explicit
    confirmation (identity_verified_elsewhere), which the UI only
    produces via a second written-confirmation click; the ack is
    recorded with the job and NEVER forwarded to a native command.
  * "Planning details (technical)" is no longer a section of the
    normal page - it lives under the technical-view toggle.
Run: python3 tests/verify_m11_p06.py
"""
import atexit
import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import types
import urllib.request
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 8788
MAN = ("/data2/caojingqi/neoag_agent_runs/cxl_e2e_mini_20261119/"
       "CXLMINI_sample_manifest.rnabam.json")
GEN = ("/data2/caojingqi/original_OpenNeoAG/neoag/scripts/"
       "generate_production_from_results_manifest.py")
PASS, FAIL = [], []

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

sys.path.insert(0, str(ROOT))
_stub = types.ModuleType("backend.server")
_stub.publish = lambda ev: None
sys.modules.setdefault("backend.server", _stub)
from backend import native  # noqa: E402
from backend.neoag_params import SCIENCE_CONTROLS  # noqa: E402


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS" if ok else "FAIL") + f"  {name}"
          + (f"  [{detail}]" if detail and not ok else ""))


byid = {c["id"]: c for c in SCIENCE_CONTROLS}

# ============ 1. the taxonomy: run-rows with honest defaults ========
sv, guard = byid["skip_dna_sv"], byid["skip_bam_matcher"]
check("1a DNA SV is stated in the RUN direction and OFF by default "
      "here (web_run_default False, run_row), with its honest "
      "consequence (DNA_SV_UNASSESSED)",
      sv.get("run_row") is True and sv.get("web_run_default") is False
      and "DNA_SV_UNASSESSED" in sv.get("affects", ""))
check("1b the SV row's own text states the RNA independence (off-SV "
      "must not disable RNA fusion - stated in the UI, not only in "
      "code)",
      "RNA" in sv.get("affects", "")
      and "fusion" in sv.get("affects", "").lower())
check("1c the identity check is marked guarded and RUNS by default "
      "(skipping it takes a separate confirmation)",
      guard.get("guarded") is True
      and guard.get("web_run_default") is True)

# ============ 2. backend validation of the guard =====================
e1 = native.validate_scientific({"skip_bam_matcher": True})
e2 = native.validate_scientific({"skip_bam_matcher": True,
                                 "identity_verified_elsewhere": True})
e3 = native.validate_scientific({"skip_bam_matcher": False})
e4 = native.validate_scientific({"skip_dna_sv": True})
check("2a skip_bam_matcher WITHOUT the explicit confirmation is "
      "REFUSED, naming why",
      bool(e1) and "identity" in " ".join(e1).lower(), str(e1))
check("2b with the separate confirmation it is accepted (a deliberate, "
      "recordable decision)", e2 == [], str(e2))
check("2c guard running (False) and the SV skip bool stay valid",
      e3 == [] and e4 == [])

# ============ 3. flag forwarding semantics ===========================
p_off = native.build_plan_argv(
    {"sample_id": "X", "skip_dna_sv": True}, "/tmp/p06argv")
p_on = native.build_plan_argv(
    {"sample_id": "X", "skip_dna_sv": False}, "/tmp/p06argv")
p_ack = native.build_plan_argv(
    {"sample_id": "X", "skip_bam_matcher": True,
     "identity_verified_elsewhere": True}, "/tmp/p06argv")
check("3a skip_dna_sv=True forwards --skip-dna-sv; False forwards "
      "NOTHING (native default = branch runs) - so the UI's OFF "
      "default must always be stated, which the payload does",
      "--skip-dna-sv" in p_off and "--skip-dna-sv" not in p_on)
check("3b the identity confirmation is a Web-layer field: it is never "
      "part of a native command",
      "identity_verified_elsewhere" not in " ".join(p_ack)
      and "--skip-bam-matcher" in p_ack)

# ============ 4. deployed-generator decoupling (structural) ==========
gen = ""
try:
    gen = open(GEN, encoding="utf-8").read()
except OSError:
    pass
skip_lines = [ln.strip() for ln in gen.splitlines() if "args.skip_dna_sv" in ln]
check("4a every skip_dna_sv reference in the deployed generator is "
      "DNA-side only (2 input checks, the DNA_SV_UNASSESSED limitation, "
      "one dna_sv stage gate) - none mentions fusion",
      len(skip_lines) == 4
      and "if not args.skip_dna_sv and (sv_vcfs or bam_pair):" in skip_lines
      and "if args.skip_dna_sv or not (sv_vcfs or bam_pair):" in skip_lines
      and not any(("fusion" in ln.lower() or "rna" in ln.lower())
                  for ln in skip_lines),
      "generator structure changed - re-audit (lines: %r)" % skip_lines)
check("4b the fusion-evidence stage in the deployed generator is gated "
      "on fusion inputs alone (easyfuse/star-fusion/arriba/...) and "
      "never on skip_dna_sv",
      "if args.easyfuse or args.easyfuse_unfiltered or args.star_fusion"
      " or args.arriba or args.fusioncatcher or args.jaffal"
      " or args.fusion_caller_root:" in gen
      and "DNA_SV_UNASSESSED" in gen,
      "generator structure changed - re-audit")

# ============ 5. the REAL native planner: off-SV keeps RNA/fusion ====
# Read-only case (CXLMINI mini). Plans write only to /tmp outdirs.
planA = planB = None
try:
    base = {"sample_id": "CXLMINI", "case_id": "CXLMINI",
            "sample_manifest": MAN, "genome_build": "GRCh38"}
    planA = native.run_plan(dict(base), "/tmp/p06planA")
    planB = native.run_plan(dict(base, skip_dna_sv=True), "/tmp/p06planB")
except Exception as e:  # noqa: BLE000
    print(f"  [plan probe unavailable: {e}]")
mdA = (planA or {}).get("run_plan_md") or ""
mdB = (planB or {}).get("run_plan_md") or ""
def _ready(md, route):
    for line in md.splitlines():
        if line.strip().startswith(f"- {route} ->"):
            return line
    return ""
check("5a real planner, real inputs: the SV-OFF plan still PASSes with "
      "NO new blocking issues (turning the branch off never breaks "
      "the workflow)",
      bool(planA) and bool(planB)
      and planA.get("status") == "PASS" and planB.get("status") == "PASS"
      and not planB.get("blocking_issues"),
      f"A={planA and planA.get('status')} B={planB and planB.get('status')}")
check("5b off-DNA-SV does NOT disable RNA fusion or splice: both "
      "routes stay READY in the SV-off plan (native verdicts, not "
      "our claims)",
      "[READY]" in _ready(mdB, "fusion")
      and "[READY]" in _ready(mdB, "splice")
      and _ready(mdA, "fusion") == _ready(mdB, "fusion")
      and _ready(mdA, "splice") == _ready(mdB, "splice"),
      f"fusionB=({_ready(mdB, 'fusion')}) spliceB=({_ready(mdB, 'splice')})")

# ============ 6. live API: the guard's pair is enforced at the edge ==
srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="m11p06.sqlite3",
             NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-m11p06-skills",
             NEOAG_TASKQ_DIR="/tmp/neoag-m11p06-taskq",
             NEOAG_GATE_TOKEN_DISABLE="1"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _cleanup():
    srv.terminate()
    try:
        srv.wait(timeout=8)
    except Exception:
        srv.kill()
    try:
        (ROOT / "data" / "m11p06.sqlite3").unlink()
    except OSError:
        pass
    shutil.rmtree(ROOT / "data" / "exports-m11p06", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p06-skills", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p06-taskq", ignore_errors=True)
    shutil.rmtree("/tmp/p06planA", ignore_errors=True)
    shutil.rmtree("/tmp/p06planB", ignore_errors=True)


atexit.register(_cleanup)


def api(method, path, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method, headers={"Authorization": AUTH_HEADER})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


up = False
for _ in range(40):
    try:
        s, _ = api("GET", "/api/bootstrap")
        up = s == 200
        if up:
            break
    except Exception:
        time.sleep(0.5)
check("6a server boots (isolated :8788)", up)

if up:
    s1, r1 = api("POST", "/api/jobs", {
        "engine": "native", "synthetic_demo": True,
        "native_params": {"skip_bam_matcher": True}})
    s2, r2 = api("POST", "/api/jobs", {
        "engine": "native", "synthetic_demo": True,
        "native_params": {"skip_bam_matcher": True,
                          "identity_verified_elsewhere": True}})
    check("6b POST /api/jobs refuses to queue a run that turns the "
          "identity guard off WITHOUT the confirmation (400, reason "
          "named)",
          s1 == 400 and "identity" in str(r1).lower(),
          f"{s1} {str(r1)[:90]}")
    check("6c with the separate confirmation the decision is accepted "
          "and KEPT with the job (auditable, revocable only by rerun)",
          s2 == 202 and bool(r2.get("job_id")), f"{s2} {str(r2)[:90]}")

# ============ 7. frontend contracts ==================================
appjs = (ROOT / "frontend" / "app.js").read_text()
check("7a the payload states run-rows EXPLICITLY (checked=RUN -> "
      "skip=not checked), so the SV-off default can never silently "
      "flip to native's run-by-default",
      "fwd[c.id] = !el.checked;" in appjs
      and "identity_verified_elsewhere = true" in appjs)
check("7b an unconfirmed guarded skip is NOT submitted (the guard "
      "stays on unless the separate ack is ticked)",
      "if (!(ack && ack.checked)) return;" in appjs)
check("7c Planning details is gated by the technical view (hidden on "
      "the normal page)",
      "el.hidden = !(hasPlan && state.techView);" in appjs
      and "showPlanBlock(true, true);" in appjs)
check("7d i18n: the two new strings exist in BOTH dictionaries",
      appjs.count('"opt.offDefault": "') == 2
      and appjs.count('"opt.guardAck": "') == 2)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
