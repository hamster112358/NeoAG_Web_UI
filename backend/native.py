"""Native NeoAG execution adapter (Milestone 5, PART 1).

This module does NOT reimplement the NeoAG Planner. It wraps the real
deployed open-neo interfaces and lets NeoAG's own code decide which
branches to run:

 - planning / preflight: `python -m neoag.open_neo.cli run --mode plan`
   (subprocess against the deployed source tree; verified to produce
   input_qc/route_plan.json + run_plan.md on synthetic inputs),
 - execution: NeoAG refuses direct heavy execution by design
   (skill_result blocking_issues = ["GATEWAY_REQUIRED"]); the native
   mechanism is the controlled_execution gateway. We supervise a
   dedicated loopback gateway instance (own port, own state dir, write
   paths restricted to our scratch root via --allowed-root) and submit
   through its documented POST /open/run route, exactly like
   execution_adapters.submit_gateway_run does.

Every claim here was verified against
/data2/caojingqi/original_OpenNeoAG/neoag (read-only) on 2026-11-16:
 - gateway main(): --host/--port/--project-root/--outdir/--allowed-root
 - GET /health -> {"status":"OK", routes:[...]};
   GET /job-status/<id> -> stored job row (status/result/output_dir);
   POST /open/run -> 202 {job_id, status_url} or 403 APPROVAL_REQUIRED
   for HIGH-risk payloads without approved=true;
 - execute with stub=true runs the production runner with
   --binding-stub --immunogenicity-stub (real planner, real runner,
   stubbed predictors - honest label, not a mock pipeline);
 - no cancel route exists on the gateway: cancel is honestly reported
   as unsupported at the native layer (limitation, not hidden).

The Web side only orchestrates and visualises; scientific routing
stays inside NeoAG.
"""
import hashlib
import json
import os
import shlex
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request

# --- real deployment coordinates (env-overridable for tests) -------------
NATIVE_PY = os.environ.get(
    "NEOAG_NATIVE_PY", "/data1/neoag/miniforge3/envs/neoag-tools/bin/python")
NATIVE_ROOT = os.environ.get(
    "NEOAG_NATIVE_ROOT", "/data2/caojingqi/original_OpenNeoAG/neoag")
WORK = os.environ.get("NEOAG_NATIVE_WORK", "/data1/neoag/work/neoag_web_native")
GW_PORT = int(os.environ.get("NEOAG_NATIVE_GATEWAY_PORT", "8760"))
GW_HOST = "127.0.0.1"

# Deployment coordinates for the real reference/toolchain (verified
# present on this host 2026-11-16; each overridable for other deploys).
# These are Execution Settings (config concern C), NOT scientific
# switches: they point native code at assets it already knows how to
# use. probe() reports presence honestly.
REF_FASTA = os.environ.get(
    "NEOAG_REFERENCE_FASTA",
    "/data1/neoag/references/data/ref/hg38/GRCh38.fa")
VEP_BIN = os.environ.get(
    "NEOAG_VEP_BIN",
    "/data1/neoag/miniforge3/envs/neoag-vep/bin/vep")
VEP_CACHE = os.environ.get(
    "NEOAG_VEP_CACHE", "/data1/neoag/references/data/vep")
VEP_CACHE_VERSION = os.environ.get("NEOAG_VEP_CACHE_VERSION", "105")
VEP_PLUGINS = os.environ.get("NEOAG_VEP_PLUGINS",
                             "/data1/neoag/vep_plugins")

PLAN_TIMEOUT_DEFAULT = 240        # plan mode measured well under this
EXEC_TIMEOUT_DEFAULT = 7200       # forwarded to the native runner

_lock = threading.Lock()
_probe_cache = {"ts": 0.0, "val": None}

# keys the Web may forward; each maps 1:1 to a real open-neo CLI flag /
# gateway /open/run field (ROUTE_SPECS optional list). Unknown web keys
# must never leak through.
# M6 PART D1/D3: the deployed unified entry (`open-neo run`, help text
# "Detect inputs, route, run the pipeline") consumes RAW inputs directly
# - --input-dir / BAM / FASTQ were verified present in the deployed
# cli.py run subparser AND in the gateway /open/run ROUTE_SPECS optional
# list on 2026-11-17, so the Web layer must be able to pass them: the
# user names a data directory, discovery maps files to these roles, and
# NeoAG's own planner decides which branches run.
_INPUT_KEYS = ("somatic_vcf", "hla_alleles", "hla_file", "expression_tsv",
               "transcript_expression_tsv", "rna_evidence_tsv",
               "fusion_tsv", "splice_junction_tsv", "sv_vcf", "purity_tsv",
               "cnv_tsv", "hla_loh_tsv", "normal_expression",
               "normal_hla_ligands", "reference_proteome", "capture_bed",
               "peptide_csv", "raw_peptides", "tumor_sample_id",
               "normal_sample_id", "assay_type",
               "input_dir", "tumor_dna_bam", "normal_dna_bam",
               "tumor_rna_bam", "tumor_dna_fastq", "normal_dna_fastq",
               "tumor_rna_fastq", "rna_quant_method",
               "skip_bam_matcher", "skip_dna_sv")
# CLI flags declared action="append": a list value repeats the flag
_LIST_KEYS = ("tumor_dna_fastq", "normal_dna_fastq", "tumor_rna_fastq",
              "sv_vcf")
# genome_build is passed explicitly in build_plan_argv (never looped -
# it would duplicate the flag). automatic_tool_policy added for M6 PART E;
# assay_type / rna_quant_method / skip_* already ride _INPUT_KEYS. Each
# row verified against the deployed cli.py AND gateway /open/run
# ROUTE_SPECS optional list on 2026-11-17 (see neoag_params
# SCIENCE_CONTROLS for the user-facing statement of the same set).
_SCIENTIFIC_KEYS = ("event_top_n", "candidate_top_n", "genome_build",
                    "automatic_tool_policy")
# M7: the confirmed Manifest is the single source of input truth. Both
# the native CLI (`--sample-manifest`, cli.py:140) and the gateway
# `/open/run` ROUTE_SPECS list (gateway.py:113) carry `sample_manifest`
# (verified against deployed source 2026-11-17), so the plan and execute
# paths forward the manifest FILE rather than re-derived input fields.
_MANIFEST_KEYS = ("sample_manifest",)

# M6 PART E: server-side validation of every scientific override before
# it can reach a run - the UI shows only constrained controls, and the
# backend independently rejects anything else ("no unvalidated free-text
# CLI params"). Choice sets/ranges mirror the deployed code defaults.
_SCIENCE_CHOICE = {
    "assay_type": ("WGS", "WES", "PANEL", "CAPTURE", "RNA"),
    "rna_quant_method": ("auto", "salmon", "rsem"),
    "automatic_tool_policy": ("all-available", "balanced", "minimal"),
    "genome_build": ("GRCh38", "GRCh37"),
}
_SCIENCE_RANGE = {"event_top_n": (1, 500), "candidate_top_n": (1, 2000)}
_SCIENCE_BOOL = ("skip_bam_matcher", "skip_dna_sv")


def validate_scientific(params):
    """Return a list of human-readable problems with scientific overrides
    (empty = every value is a real, in-range option). Unknown keys are
    NOT reported here - they are already dropped by the forward whitelist;
    this guards the values of keys that WILL be forwarded."""
    errs = []
    for k, allowed in _SCIENCE_CHOICE.items():
        v = params.get(k)
        if v not in (None, "") and str(v) not in allowed:
            errs.append(f"{k}={v!r} is not one of {list(allowed)}")
    for k, (lo, hi) in _SCIENCE_RANGE.items():
        v = params.get(k)
        if v in (None, ""):
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            errs.append(f"{k}={v!r} is not an integer")
            continue
        if not lo <= n <= hi:
            errs.append(f"{k}={n} is outside the supported range "
                        f"{lo}..{hi}")
    for k in _SCIENCE_BOOL:
        v = params.get(k)
        if v not in (None, "", True, False):
            errs.append(f"{k} must be true or false")
    # M11 P0-6: the sample-identity guard is not casually skippable.
    # Turning it off is a deliberate, recorded decision: it is accepted
    # only together with an explicit acknowledgement (which the Web UI
    # can only produce by a second, separate confirmation click). The
    # ack itself is a Web-layer field - it is NOT in the forwarding
    # whitelist, so it never reaches a native command.
    if params.get("skip_bam_matcher") is True and \
            params.get("identity_verified_elsewhere") is not True:
        errs.append(
            "skip_bam_matcher turns off the tumor/normal sample-identity "
            "guard and is only accepted with an explicit "
            "identity_verified_elsewhere confirmation")
    return errs


TOOLS_ENV_SCRIPT = os.path.join(NATIVE_ROOT, "conf", "tools.env.sh")
TOOLS_MANIFEST = os.path.join(NATIVE_ROOT, "configs", "tools",
                              "tools_manifest.yaml")
REFERENCE_MANIFEST = os.path.join(NATIVE_ROOT, "configs", "references",
                                  "reference_manifest.yaml")

_source_cache = {"ts": 0.0, "env": None}


def _sourced_tools_env():
    """Deployment environment exactly as every NeoAG stage command sees it:
    the result of sourcing conf/tools.env.sh (which also sources the site
    overlay tools.env.local.sh). Without this the capability planner's
    shutil.which() ran on the server's bare PATH and marked every installed
    tool UNAVAILABLE (2026-11-19 incident, TEST001 job
    job-20261119-092608-9e9740). Returns None on any failure - callers then
    fall back to the plain inherited environment. Cached briefly."""
    now = time.time()
    with _lock:
        if _source_cache["env"] is not None and now - _source_cache["ts"] < 300:
            return _source_cache["env"]
    env = None
    if os.path.isfile(TOOLS_ENV_SCRIPT):
        try:
            r = subprocess.run(
                ["/bin/bash", "-c",
                 'source "%s" >/dev/null 2>&1 && env -0' % TOOLS_ENV_SCRIPT],
                capture_output=True, timeout=90)
            if r.returncode == 0 and r.stdout:
                env = {}
                for item in r.stdout.split(b"\0"):
                    if not item:
                        continue
                    k, sep, v = item.partition(b"=")
                    if not sep:
                        continue
                    key = k.decode(errors="replace")
                    if key in ("SHLVL", "_", "PS1", "SHELLOPTS", "BASHOPTS"):
                        continue
                    env[key] = v.decode(errors="replace")
                if not env.get("PATH"):
                    env = None
        except Exception:                              # noqa: BLE001
            env = None
    with _lock:
        _source_cache.update(ts=now, env=env)
    return env


def _env():
    sourced = _sourced_tools_env()
    # the sourced deployment env is a superset of the server env (the bash
    # subshell inherits it and tools.env.sh only adds/overrides), so when
    # present it is the correct base; fall back to the bare copy only if
    # sourcing failed
    e = dict(sourced) if sourced else os.environ.copy()
    src = os.path.join(NATIVE_ROOT, "src")
    e["PYTHONPATH"] = src + (":" + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
    e.setdefault("NEOAG_TOOLS_ROOT", NATIVE_ROOT)
    # OPEN_NEO_REFERENCE_ROOT (needed by reference_manifest.yaml resolution)
    # comes from the sourced deployment env; in fallback mode the plain
    # os.environ copy already carries it if the operator exported it.
    # the deployed tree is frozen/read-only from our side: never let an
    # import write __pycache__ into it
    e["PYTHONDONTWRITEBYTECODE"] = "1"
    # point native code at the deployment's real reference/toolchain;
    # only export paths that actually exist (no phantom coordinates)
    for var, val in (("NEOAG_REFERENCE_FASTA", REF_FASTA),
                     ("NEOAG_VEP_BIN", VEP_BIN),
                     ("NEOAG_VEP_CACHE", VEP_CACHE),
                     ("NEOAG_VEP_PLUGINS", VEP_PLUGINS)):
        if val and os.path.exists(val):
            e.setdefault(var, val)
    if VEP_CACHE_VERSION and VEP_CACHE and os.path.exists(VEP_CACHE):
        e.setdefault("NEOAG_VEP_CACHE_VERSION", VEP_CACHE_VERSION)
    return e


def job_outdir(job_id):
    d = os.path.join(WORK, "jobs", str(job_id))
    os.makedirs(d, exist_ok=True)
    return d


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# --------------------------------------------------------------------------
def probe(force=False):
    """Capability inspection, cached 60 s. Never raises."""
    with _lock:
        if not force and time.time() - _probe_cache["ts"] < 60 and _probe_cache["val"]:
            return _probe_cache["val"]
    out = {"checked_at": _now()}
    out["python"] = NATIVE_PY
    out["python_exists"] = os.path.exists(NATIVE_PY)
    out["root"] = NATIVE_ROOT
    out["root_exists"] = os.path.isdir(os.path.join(NATIVE_ROOT, "src", "neoag"))
    out["import_ok"] = False
    if out["python_exists"] and out["root_exists"]:
        try:
            r = subprocess.run(
                [NATIVE_PY, "-c",
                 "import sys; sys.path.insert(0,%r); import neoag.open_neo.run as r;"
                 "print('ok')" % os.path.join(NATIVE_ROOT, "src")],
                capture_output=True, text=True, timeout=60, env=_env())
            out["import_ok"] = r.returncode == 0 and "ok" in r.stdout
            if not out["import_ok"]:
                out["import_error"] = (r.stderr or r.stdout)[-300:]
        except Exception as exc:                      # noqa: BLE001
            out["import_error"] = str(exc)[:300]
    out["gateway"] = gateway_status()
    out["available"] = bool(out["import_ok"])
    # execution wiring honesty: which real coordinates are actually present
    out["toolchain"] = {
        "reference_fasta": {"path": REF_FASTA, "present": os.path.exists(REF_FASTA)},
        "vep_bin": {"path": VEP_BIN, "present": os.path.exists(VEP_BIN)},
        "vep_cache": {"path": VEP_CACHE,
                      "present": os.path.isdir(os.path.join(VEP_CACHE, "homo_sapiens")),
                      "cache_version": VEP_CACHE_VERSION},
        "vep_plugins": {"path": VEP_PLUGINS, "present": os.path.isdir(VEP_PLUGINS)},
        "demo_fixtures": {"present": demo_available()},
    }
    # honest discovery wiring (2026-11-19): does the deployment toolchain
    # env actually reach our subprocesses?
    out["tools_env"] = {
        "script": TOOLS_ENV_SCRIPT,
        "present": os.path.isfile(TOOLS_ENV_SCRIPT),
        "sourced_ok": _sourced_tools_env() is not None,
        "tools_manifest": {"path": TOOLS_MANIFEST,
                           "present": os.path.isfile(TOOLS_MANIFEST)},
        "reference_manifest": {"path": REFERENCE_MANIFEST,
                               "present": os.path.isfile(REFERENCE_MANIFEST)},
    }
    out["cancel_supported_by_native_gateway"] = False  # verified: no such route
    with _lock:
        _probe_cache.update(ts=time.time(), val=out)
    return out


# --------------------------------------------------------------------------
def gateway_status():
    """GET /health against the supervised gateway. Never raises."""
    try:
        with urllib.request.urlopen(
                f"http://{GW_HOST}:{GW_PORT}/health", timeout=3) as r:
            d = json.loads(r.read().decode())
        return {"running": d.get("status") == "OK", "port": GW_PORT,
                "url": f"http://{GW_HOST}:{GW_PORT}",
                "routes": d.get("routes"), "checked_at": _now()}
    except Exception as exc:                           # noqa: BLE001
        return {"running": False, "port": GW_PORT,
                "url": f"http://{GW_HOST}:{GW_PORT}",
                "error": str(exc)[:200], "checked_at": _now()}


def ensure_gateway():
    """Start the dedicated gateway if not already healthy. Returns
    gateway_status dict; on failure contains honest error + log tail."""
    st = gateway_status()
    if st["running"]:
        return st
    os.makedirs(os.path.join(WORK, "gateway-state"), exist_ok=True)
    log_path = os.path.join(WORK, "gateway.log")
    cmd = [NATIVE_PY, "-m", "neoag.controlled_execution.gateway",
           "--host", GW_HOST, "--port", str(GW_PORT),
           "--project-root", NATIVE_ROOT,
           "--outdir", os.path.join(WORK, "gateway-state"),
           "--allowed-root", WORK]
    try:
        with open(log_path, "ab") as lf:
            subprocess.Popen(cmd, cwd=NATIVE_ROOT, env=_env(),
                             stdout=lf, stderr=lf, start_new_session=True,
                             close_fds=True)
    except Exception as exc:                           # noqa: BLE001
        return {**st, "running": False, "start_error": str(exc)[:300]}
    deadline = time.time() + 20
    while time.time() < deadline:
        st = gateway_status()
        if st["running"]:
            return st
        time.sleep(0.5)
    tail = ""
    try:
        tail = open(log_path, "rb").read()[-500:].decode("utf-8", "replace")
    except OSError:
        pass
    return {**st, "running": False,
            "start_error": "gateway did not answer /health within 20 s",
            "log_tail": tail}


def _post_json(url, payload, timeout=30):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:                              # noqa: BLE001
            return e.code, {}


def gw_submit(payload):
    """POST /open/run. Returns (http_status, body). Caller supplies an
    already-validated payload; approved=True must only be set after the
    Web approval gate actually passed."""
    st = ensure_gateway()
    if not st["running"]:
        return 0, {"status": "GATEWAY_UNAVAILABLE", "detail": st}
    return _post_json(st["url"] + "/open/run", payload)


def gw_status(gw_job_id):
    try:
        with urllib.request.urlopen(
                f"http://{GW_HOST}:{GW_PORT}/job-status/{gw_job_id}",
                timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as exc:                           # noqa: BLE001
        return {"status": "STATUS_UNREACHABLE", "error": str(exc)[:200]}


# --------------------------------------------------------------------------
def build_plan_argv(params, outdir):
    argv = [NATIVE_PY, "-m", "neoag.open_neo.cli", "run", "--mode", "plan",
            "--no-doctor", "--no-sync-public-assets",
            "--project-root", NATIVE_ROOT,
            "--outdir", outdir,
            "--case-id", str(params.get("case_id") or params.get("sample_id")),
            "--sample-id", str(params.get("sample_id")),
            "--genome-build", str(params.get("genome_build") or "GRCh38")]
    for k in _INPUT_KEYS + _SCIENTIFIC_KEYS + _MANIFEST_KEYS:
        v = params.get(k)
        if v in (None, "", []):
            continue
        if k == "hla_alleles":
            argv += ["--hla-alleles", json.dumps(list(v))
                     if not isinstance(v, str) else v]
        elif k in ("skip_bam_matcher", "skip_dna_sv"):
            if v:                          # store_true flags: no value
                argv += ["--" + k.replace("_", "-")]
        elif k in _LIST_KEYS and isinstance(v, (list, tuple)):
            for item in v:                 # action=append: repeat the flag
                argv += ["--" + k.replace("_", "-"), str(item)]
        else:
            argv += ["--" + k.replace("_", "-"), str(v)]
    # 2026-11-19: without the manifests the planner has no refs dict and no
    # command_template access, so facets/sequenza/purple/spechla-loh rows all
    # degrade to UNAVAILABLE/TEMPLATE_REQUIRED. Both flags exist on the
    # deployed run subparser (cli.py:145-146).
    for flag, path in (("--tools-manifest", TOOLS_MANIFEST),
                       ("--reference-manifest", REFERENCE_MANIFEST)):
        if os.path.isfile(path):
            argv += [flag, path]
    return argv


def run_plan(params, outdir, timeout=PLAN_TIMEOUT_DEFAULT):
    """Real native planning/preflight. Returns an honest dict; on any
    failure status is a clear string, never a fake plan."""
    os.makedirs(outdir, exist_ok=True)
    argv = build_plan_argv(params, outdir)
    cmd = " ".join(shlex.quote(a) for a in argv)
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, env=_env(), cwd=NATIVE_ROOT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "PLAN_TIMEOUT", "command": cmd,
                "error": f"plan exceeded {timeout}s"}
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "status": "PLAN_SPAWN_FAILED", "command": cmd,
                "error": str(exc)[:400]}
    result_path = os.path.join(outdir, "skill_result.json")
    plan = None
    try:
        with open(result_path, encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, ValueError):
        plan = None
    rp_md = ""
    try:
        rp_md = open(os.path.join(outdir, "run_plan.md"),
                     encoding="utf-8").read()[:20000]
    except OSError:
        pass
    if plan is None:
        return {"ok": False, "status": "PLAN_NO_RESULT", "command": cmd,
                "rc": r.returncode,
                "error": (r.stderr or r.stdout)[-500:], "stdout": r.stdout[:2000]}
    return {"ok": plan.get("status") in ("PASS",), "command": cmd,
            "status": plan.get("status"),
            "run_id": plan.get("run_id"),
            "blocking_issues": plan.get("blocking_issues") or [],
            "missing_evidence": plan.get("missing_evidence") or [],
            "steps": plan.get("steps") or [],
            "outputs": plan.get("outputs") or {},
            "run_plan_md": rp_md, "rc": r.returncode,
            "plan_path": result_path}


def build_execute_payload(params, outdir, stub=False, timeout=EXEC_TIMEOUT_DEFAULT):
    """Gateway payload using ONLY fields from the /open/run ROUTE_SPECS
    allowlist (verified against gateway source 2026-11-16)."""
    payload = {"case_id": str(params.get("case_id") or params.get("sample_id")),
               "sample_id": str(params.get("sample_id")),
               "genome_build": str(params.get("genome_build") or "GRCh38"),
               "mode": "execute",
               "approved": True,          # only reached after Web approval
               "outdir": outdir,
               "doctor": False,
               "sync_public_assets": False,
               "timeout": int(timeout)}
    for k in _INPUT_KEYS + _SCIENTIFIC_KEYS + _MANIFEST_KEYS:
        v = params.get(k)
        if v in (None, "", []):
            continue
        payload[k] = (list(v) if k == "hla_alleles" and not isinstance(v, str)
                      else v)
    # same rationale as build_plan_argv: /open/run ROUTE_SPECS carries
    # tools_manifest/reference_manifest (verified against gateway source
    # 2026-11-19) and run_open_neo forwards them to the capability planner.
    if os.path.isfile(TOOLS_MANIFEST):
        payload["tools_manifest"] = TOOLS_MANIFEST
    if os.path.isfile(REFERENCE_MANIFEST):
        payload["reference_manifest"] = REFERENCE_MANIFEST
    if stub:
        payload["stub"] = True
    return payload


def submit_execute(params, outdir, stub=False,
                   timeout=EXEC_TIMEOUT_DEFAULT):
    """Submit a REAL native execution through the native gateway.
    Returns dict with ok + gateway job_id or an honest blocked state."""
    cap = probe()
    if not cap["available"]:
        return {"ok": False, "status": "NATIVE_UNAVAILABLE",
                "detail": "native NeoAG source/python not importable; "
                          "see capability probe", "capability": cap}
    os.makedirs(outdir, exist_ok=True)
    payload = build_execute_payload(params, outdir, stub=stub, timeout=timeout)
    code, body = gw_submit(payload)
    if code == 202 and body.get("job_id"):
        return {"ok": True, "status": body.get("status"),
                "gw_job_id": body["job_id"], "http": code,
                "status_url": body.get("status_url"), "risk": body.get("risk_level")}
    if code == 403:                       # gateway-level approval demanded
        return {"ok": False, "status": "GATEWAY_APPROVAL_REQUIRED",
                "http": code, "detail": body}
    return {"ok": False, "status": "GATEWAY_SUBMISSION_FAILED",
            "http": code, "detail": body}


# --------------------------------------------------------------------------
_ARTIFACT_NAMES = ("skill_result.json", "run_state.json", "run_plan.md")


def collect_artifacts(outdir, max_files=200):
    """Real files produced by a native run (bounded walk). sha256 + size;
    no content is read beyond the hash. Skips nothing silently except
    unreadable files, which are reported."""
    arts, unreadable, n = [], [], 0
    for root, _dirs, files in os.walk(outdir):
        for fn in sorted(files):
            p = os.path.join(root, fn)
            n += 1
            if n > max_files:
                break
            try:
                h = hashlib.sha256()
                size = 0
                with open(p, "rb") as f:
                    while True:
                        b = f.read(1 << 20)
                        if not b:
                            break
                        size += len(b)
                        h.update(b)
            except OSError as exc:
                unreadable.append({"path": p, "error": str(exc)[:120]})
                continue
            rel = os.path.relpath(p, outdir)
            arts.append({"name": rel, "path": p, "bytes": size,
                         "sha256": h.hexdigest()[:64],
                         "priority": 0 if fn in _ARTIFACT_NAMES else 1})
        if n > max_files:
            break
    arts.sort(key=lambda a: (a["priority"], a["name"]))
    for a in arts:
        a.pop("priority")
    return arts, unreadable


def tail_log(outdir, name="run_full.log", limit=4000):
    for cand in (os.path.join(outdir, "logs", name),
                 os.path.join(outdir, name)):
        try:
            with open(cand, "rb") as f:
                f.seek(max(0, os.path.getsize(cand) - limit * 2))
                return f.read().decode("utf-8", "replace")[-limit:]
        except OSError:
            continue
    return ""


# --------------------------------------------------------------------------
# M7 2.2/2.3: REAL input discovery, executed by NeoAG's OWN detector.
# The user names ONE data directory; we invoke the deployed
# neoag.open_neo.routing scanner + inventory + router in the native
# environment (subprocess) - NOT a second web-layer input router. The
# native detector classifies VCFs by CONTENT (SVTYPE header), tables by
# HEADER GROUPS (fusion/splice/peptide) and HLA files by allele pattern;
# the inventory reports existence, size, BAM-index presence, VCF
# genotype-sample columns and detected genome builds; the router states
# which branches are READY and what is missing/ambiguous. The proposal
# becomes the manifest the human confirms; the CONFIRMED manifest is
# written in the native open-neo-sample-v1 schema and passed to the
# planner/executor as --sample-manifest (single source of input truth -
# native precedence: manifest -> explicit CLI -> directory scan).
AUTHORIZED_INPUT_ROOTS = [
    os.path.realpath(r) for r in os.environ.get(
        "NEOAG_INPUT_ROOTS", "/data1/neoag").split(os.pathsep) if r]

_SCAN_SCRIPT = r"""
import json, sys
from pathlib import Path
from neoag.open_neo import routing
q = json.loads(sys.argv[1])
root = Path(q["data_dir"]).resolve()
if not root.is_dir():
    print(json.dumps({"error": "not a directory or not readable: "
                               + str(root)}))
    raise SystemExit(0)
detected, ambiguous, warnings = routing.scan_input_directory(
    root, max_files=1000)
matched = set()
for v in detected.values():
    for x in (v if isinstance(v, list) else [v]):
        matched.add(str(Path(x).resolve()))
n_files, unmatched = 0, []
for f in sorted(p for p in root.rglob("*") if p.is_file()):
    n_files += 1
    if n_files > 1000:
        warnings.append("listing capped at 1000 files")
        break
    if str(f.resolve()) not in matched and len(unmatched) < 80:
        unmatched.append(str(f.relative_to(root)))
inventory = routing.build_inventory(detected)
routes, missing, ambiguous2, warnings2 = routing.route_inputs(detected)
print(json.dumps({
    "dir": str(root),
    "detected": detected,
    "ambiguous": ambiguous + ambiguous2,
    "warnings": warnings + warnings2,
    "inventory": inventory,
    "routes": [{"route": r.route, "skill": r.skill, "status": r.status,
                "reason": r.reason} for r in routes],
    "missing": missing,
    "n_files": n_files,
    "unmatched": unmatched,
}))
"""

_ROLE_LABEL = {
    "tumor_dna_bam": ("Tumor DNA alignments", "sequencing_dna"),
    "normal_dna_bam": ("Matched normal DNA alignments", "sequencing_dna"),
    "tumor_dna_fastq": ("Tumor DNA reads", "sequencing_dna"),
    "normal_dna_fastq": ("Matched normal DNA reads", "sequencing_dna"),
    "tumor_rna_bam": ("RNA alignments", "sequencing_rna"),
    "tumor_rna_fastq": ("RNA reads", "sequencing_rna"),
    "somatic_vcf": ("Somatic variant calls", "processed_evidence"),
    "sv_vcf": ("Structural variant calls", "processed_evidence"),
    "fusion_tsv": ("Fusion caller table", "processed_evidence"),
    "splice_junction_tsv": ("Splice junction table", "processed_evidence"),
    "hla_file": ("HLA typing result", "processed_evidence"),
    "peptide_csv": ("Existing peptide-HLA table", "processed_evidence"),
    "expression_tsv": ("Gene expression table", "processed_evidence"),
    "purity_tsv": ("Purity/ploidy table", "processed_evidence"),
    "cnv_tsv": ("Copy-number segments", "processed_evidence"),
    "hla_loh_tsv": ("HLA LOH evidence", "processed_evidence"),
    "capture_bed": ("Capture kit BED", "processed_evidence"),
}

_VCF_KEY = {"input_key": "somatic_vcf"}


def authorized_dir(data_dir: str):
    """Resolve + authorization-check a requested input directory.
    Returns (real_path, error_string - empty when allowed)."""
    if not data_dir or not os.path.isabs(data_dir):
        return "", "an absolute directory path is required"
    real = os.path.realpath(data_dir)
    if not os.path.isdir(real):
        return "", f"directory not found or not readable: {real}"
    for root in AUTHORIZED_INPUT_ROOTS:
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            return real, ""
    return "", ("not an authorized input location - input directories "
                "must live under: " + ", ".join(AUTHORIZED_INPUT_ROOTS))


def native_tree(data_dir: str, sub: str = "") -> dict:
    """M9 §5.3: ONE directory level listed from the real filesystem for
    the input-review tree (lazy loading keeps large directories cheap).
    Web-layer listing only - scientific roles come exclusively from the
    native detector (discover_inputs), never from this listing.
    Read-only; confined to the authorized directory (no escaping
    sub-paths, symlinks are listed but never followed)."""
    real, err = authorized_dir(data_dir)
    if err:
        return {"ok": False, "error": err}
    sub = (sub or "").strip().strip("/")
    if ".." in [p for p in sub.split("/") if p]:
        return {"ok": False, "error": "invalid subdirectory"}
    target = os.path.realpath(os.path.join(real, sub) if sub else real)
    if target != real and not target.startswith(
            real.rstrip(os.sep) + os.sep):
        return {"ok": False, "error": "path escapes the scanned directory"}
    if not os.path.isdir(target):
        return {"ok": False, "error": "not a directory"}
    MAX = 500
    try:
        names = sorted(os.listdir(target))
    except OSError as e:
        return {"ok": False, "error": f"directory not readable: {e}"}
    entries, n_total = [], len(names)
    for name in names[:MAX]:
        p = os.path.join(target, name)
        try:
            stt = os.stat(p, follow_symlinks=False)
        except OSError:
            continue
        isdir = stat.S_ISDIR(stt.st_mode)
        entries.append({"name": name, "dir": bool(isdir),
                        "size": 0 if isdir else stt.st_size,
                        "rel": (sub + "/" + name).lstrip("/")})
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"ok": True, "data_dir": real, "sub": sub,
            "entries": entries, "n_total": n_total,
            "truncated": n_total > MAX}


def native_scan(data_dir: str) -> dict:
    """Run NeoAG's OWN scanner/inventory/router over data_dir."""
    real, err = authorized_dir(data_dir)
    if err:
        return {"ok": False, "error": err, "data_dir": data_dir}
    cap = probe()
    if not cap.get("available"):
        return {"ok": False, "data_dir": real,
                "error": ("native NeoAG is not importable on this host, "
                          "and input discovery is performed BY the native "
                          "detector by design - no web-layer substitute "
                          "scan is offered. Capability probe: "
                          + str(cap.get("import_error") or cap)[:300])}
    try:
        r = subprocess.run(
            [NATIVE_PY, "-c", _SCAN_SCRIPT,
             json.dumps({"data_dir": real})],
            capture_output=True, text=True, timeout=180, env=_env(),
            cwd=NATIVE_ROOT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "data_dir": real,
                "error": "native scan timed out after 180s"}
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "data_dir": real,
                "error": f"native scan could not run: {str(exc)[:300]}"}
    try:
        out = json.loads(r.stdout)
    except ValueError:
        return {"ok": False, "data_dir": real,
                "error": "native scan produced no parseable result: "
                         + (r.stderr or r.stdout)[-300:]}
    if out.get("error"):
        return {"ok": False, "data_dir": real, "error": out["error"]}
    return {"ok": True, "data_dir": real, **out,
            "engine": {"detector":
                       "neoag.open_neo.routing.scan_input_directory",
                       "root": NATIVE_ROOT,
                       "note": "classification performed by the deployed "
                               "NeoAG code itself (VCFs by content, "
                               "tables by header, alleles by pattern)"}}


def discover_inputs(data_dir: str) -> dict:
    """M7 presentation of the native scan: concise, non-expert-readable
    summary + expandable details. Ambiguities come back as STRUCTURED
    choices (field + candidates) for the confirmation step - the user
    picks among real files, never retypes paths blind."""
    raw = native_scan(data_dir)
    if not raw.get("ok"):
        return {"ok": False, "data_dir": data_dir,
                "error": raw.get("error", "discovery failed")}
    inv = {r["input_key"]: r for r in raw.get("inventory") or []}
    inputs = []
    for flag, value in sorted((raw.get("detected") or {}).items()):
        label, cat = _ROLE_LABEL.get(flag, (flag, "processed_evidence"))
        row = inv.get(flag) or {}
        for path in (value if isinstance(value, list) else [value]):
            inputs.append({"flag": flag, "role": label, "category": cat,
                           "path": path,
                           "status": row.get("status", ""),
                           "size_bytes": row.get("size_bytes", 0),
                           "bam_index": ("present" if row.get("bam_index")
                                         else ("missing"
                                               if path.endswith(".bam")
                                               else ""))})
    # tumor/normal relationship FROM EVIDENCE: the somatic VCF's
    # genotype columns are the authority (native inventory read them).
    vcf_row = inv.get("somatic_vcf") or {}
    vcf_samples = [s for s in str(vcf_row.get("vcf_samples") or "").split(",")
                   if s]
    tumor_sid = normal_sid = ""
    rel_note = ""
    if len(vcf_samples) == 2:
        t = [s for s in vcf_samples
             if s.lower().endswith(("_t", "-t", "_tumor", "_tumour"))
             or "tum" in s.lower()]
        n = [s for s in vcf_samples if s not in t]
        if len(t) == 1 and len(n) == 1:
            tumor_sid, normal_sid = t[0], n[0]
            rel_note = (f"paired tumor/normal samples {tumor_sid} / "
                        f"{normal_sid} read from the somatic VCF "
                        "genotype columns")
        else:
            rel_note = ("the VCF carries two genotype samples ("
                        + ", ".join(vcf_samples)
                        + ") - which is the tumor needs one confirmation")
    elif len(vcf_samples) == 1:
        tumor_sid = vcf_samples[0]
        rel_note = f"single genotype sample {tumor_sid} in the VCF"
    ambiguous = []
    for a in raw.get("ambiguous") or []:
        field = str(a.get("field") or "")
        reason = str(a.get("reason") or "")
        # native formats the one pickable case as
        # "multiple directory-scan candidates: p1;p2;..." - the FIRST
        # path carries the prose prefix, so the prefix must be stripped
        # before collecting (else the human is shown one option too few).
        cands = []
        for chunk in reason.split(";"):
            chunk = chunk.strip()
            if "candidates:" in chunk:
                chunk = chunk.split("candidates:", 1)[1].strip()
            if chunk.startswith("/"):
                cands.append(chunk)
        label = _ROLE_LABEL.get(field, (field,))[0]
        ambiguous.append({"field": field, "label": label,
                          "reason": reason, "candidates": cands})
    if vcf_samples and not tumor_sid:
        ambiguous.append({
            "field": "tumor_sample_id", "label": "Which sample is the tumor?",
            "reason": "two VCF samples, unclear which is tumor",
            "candidates": vcf_samples})
    dna = [i for i in inputs if i["category"] == "sequencing_dna"]
    rna = [i for i in inputs if i["category"] == "sequencing_rna"]
    ev = [i for i in inputs if i["category"] == "processed_evidence"]
    routes = raw.get("routes") or []
    ready = bool(routes) and not raw.get("missing") and not ambiguous
    stem = os.path.basename(raw["data_dir"].rstrip("/"))
    return {
        "ok": True, "data_dir": raw["data_dir"], "scanned_at": _now(),
        "engine": raw["engine"],
        "summary": {
            "case_guess": stem, "tumor_sample_id": tumor_sid,
            "normal_sample_id": normal_sid, "relationship": rel_note,
            "dna": [i["role"] for i in dna],
            "rna": [i["role"] for i in rna],
            "evidence": [i["role"] for i in ev],
            "n_recognized": len(inputs), "n_files": raw.get("n_files", 0)},
        "inputs": inputs, "ambiguous": ambiguous,
        "missing": raw.get("missing") or [],
        "warnings": raw.get("warnings") or [],
        "routes": routes, "unmatched": raw.get("unmatched") or [],
        "inventory": raw.get("inventory") or [],
        "ready": ready,
        "native_hla_note": (
            "HLA alleles, if a typing result was found, are read by the "
            "native code from that file - you never enter alleles "
            "yourself" if any(i["flag"] == "hla_file" for i in inputs)
            else "no HLA typing result found - the native planner types "
                 "HLA itself from DNA inputs when the toolchain is "
                 "installed, and its plan reports what it needs"),
    }


def write_native_manifest(data_dir: str, sample_id: str,
                          picked: dict | None = None) -> dict:
    """Confirmed manifest in the NATIVE open-neo-sample-v1 schema
    (schema verified in deployed run.py / routing.normalize_manifest
    2026-11-17). `picked` resolves structured ambiguities with the
    human's choices {field: path}. The file lands in our scratch work
    dir and is what the planner/executor receives via
    --sample-manifest: one manifest, no re-entry anywhere later."""
    raw = native_scan(data_dir)
    if not raw.get("ok"):
        raise ValueError(raw.get("error", "discovery failed"))
    detected = dict(raw.get("detected") or {})
    for field, path in (picked or {}).items():
        if field in _ROLE_LABEL and isinstance(path, str) and path:
            if not os.path.realpath(path).startswith(
                    raw["data_dir"].rstrip(os.sep) + os.sep):
                raise ValueError(
                    f"choice for {field} is outside the scanned "
                    "directory - re-scan instead of free-path entry")
            detected[field] = path
    if not detected:
        raise ValueError("no recognizable inputs in "
                         f"{raw['data_dir']} - nothing to manifest")
    sid = str(sample_id or os.path.basename(
        raw["data_dir"].rstrip("/")) or "CASE001")
    vcf_row = next((r for r in raw.get("inventory") or []
                    if r.get("input_key") == "somatic_vcf"), {})
    vcf_samples = [s for s in str(vcf_row.get("vcf_samples") or "").split(",")
                   if s]
    tumor = next((s for s in vcf_samples if "tum" in s.lower()
                  or s.lower().endswith(("_t", "-t"))), "")
    normal = next((s for s in vcf_samples if s != tumor), "") \
        if tumor and len(vcf_samples) == 2 else ""
    data = {"schema_version": "open-neo-sample-v1",
            "case_id": sid, "sample_id": sid,
            "genome_build": "GRCh38",
            "inputs": dict(detected),
            "input_dir": raw["data_dir"]}
    if tumor:
        data["tumor_sample_id"] = tumor
        data["inputs"]["tumor_sample_id"] = tumor
    if normal:
        data["normal_sample_id"] = normal
        data["inputs"]["normal_sample_id"] = normal
    os.makedirs(os.path.join(WORK, "manifests"), exist_ok=True)
    path = os.path.join(WORK, "manifests",
                        "sample_manifest.%s.json" % time.strftime("%Y%m%d%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    proposal = {
        "kind": "native", "mock": False,
        "note": ("inputs detected by the NATIVE NeoAG scanner over "
                 f"{raw['data_dir']}; the confirmed manifest below is the "
                 "single input source - planning and execution receive "
                 "this file, never re-typed values"),
        "source_description": f"data directory {raw['data_dir']}"[:400],
        "sample_id": sid, "data_dir": raw["data_dir"],
        "manifest_path": path,
        "inputs": [{"flag": k, "role": _ROLE_LABEL.get(k, (k,))[0],
                    "path": (v if isinstance(v, str) else ";".join(v))}
                   for k, v in sorted(detected.items())],
        "missing": raw.get("missing") or [],
        "ambiguous": raw.get("ambiguous") or [],
        "notes": raw.get("warnings") or []}
    return proposal


def params_from_manifest(manifest: dict) -> dict:
    """Confirmed manifest -> native params. The manifest FILE itself is
    forwarded (--sample-manifest) - native reads the confirmed inputs
    from it; we add only the case identity and the scanned directory
    (for native's own QC cross-check). Nothing is re-derived here."""
    prop = manifest.get("proposal") or {}
    conf = manifest.get("confirmed") or {}
    if not isinstance(conf, dict):
        conf = {}
    sid = str(conf.get("sample_id") or prop.get("sample_id") or "")
    params = {"sample_id": sid, "case_id": sid}
    if prop.get("manifest_path") and os.path.exists(prop["manifest_path"]):
        params["sample_manifest"] = prop["manifest_path"]
    else:
        # pre-M7 manifests (role flags inline): forward what the human
        # confirmed, minus removals
        removed = {str(x) for x in (conf.get("removed") or [])}
        inputs = [i for i in (prop.get("inputs") or [])
                  if i.get("path") not in removed]
        for i in (conf.get("added") or []):
            if isinstance(i, dict) and i.get("flag") and i.get("path"):
                inputs.append(i)
        for i in inputs:
            flag, path = i.get("flag"), i.get("path")
            if not flag or not path:
                continue
            if flag in _LIST_KEYS:
                params.setdefault(flag, []).append(path)
            else:
                params.setdefault(flag, path)
    if prop.get("data_dir"):
        params["input_dir"] = prop["data_dir"]
    return params


# --------------------------------------------------------------------------
# Native DEMO profile: `neoag.cli run-demo` - the deployed repo's own
# sanctioned safe synthetic execution path (fixture inputs +
# immunogenicity stub; no patient data, no licensed binaries, no
# network). It runs the REAL pipeline runner (neoag.pipeline.run) on
# consistent fixture data, so it completes and produces real artifacts.
# This is NOT the mock engine: the scientific code actually executes;
# only the inputs are the repo's demo fixtures and the predictors are
# explicitly stubbed (honest labels in every event).
_DEMO_FIXTURES = ("pvacseq_aggregated.tsv", "netmhcpan_example.xls",
                  "mhcflurry_predictions.csv")


def demo_available():
    d = os.path.join(NATIVE_ROOT, "data", "fixtures")
    return os.path.isdir(d) and all(
        os.path.exists(os.path.join(d, f)) for f in _DEMO_FIXTURES)


def run_demo(outdir, sample_id="DEMO-NEOAG", timeout=1800, poll=None):
    """Run `neoag.cli run-demo` as a supervised subprocess. `poll` is an
    optional callback returning 'cancel' to abort (SIGTERM then KILL -
    OUR subprocess, unlike gateway jobs where no such control exists).
    Returns an honest dict; never fakes success."""
    os.makedirs(outdir, exist_ok=True)
    if not demo_available():
        return {"ok": False, "status": "DEMO_FIXTURES_MISSING",
                "error": f"native fixtures not found under "
                         f"{NATIVE_ROOT}/data/fixtures"}
    argv = [NATIVE_PY, "-m", "neoag.cli", "run-demo",
            "--outdir", outdir, "--sample-id", sample_id]
    cmd = " ".join(shlex.quote(a) for a in argv)
    log_path = os.path.join(outdir, "run_demo.log")
    try:
        with open(log_path, "wb") as lf:
            proc = subprocess.Popen(argv, cwd=NATIVE_ROOT, env=_env(),
                                    stdout=lf, stderr=subprocess.STDOUT,
                                    start_new_session=True)
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "status": "DEMO_SPAWN_FAILED", "command": cmd,
                "error": str(exc)[:400]}
    deadline = time.time() + timeout
    while proc.poll() is None:
        if time.time() > deadline:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            return {"ok": False, "status": "DEMO_TIMEOUT", "command": cmd,
                    "error": f"run-demo exceeded {timeout}s",
                    "log_tail": tail_log(outdir, "run_demo.log")}
        if poll is not None and poll() == "cancel":
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            return {"ok": False, "status": "DEMO_CANCELLED", "command": cmd,
                    "log_tail": tail_log(outdir, "run_demo.log")}
        time.sleep(0.5)
    rc = proc.returncode
    out = {"ok": rc == 0, "rc": rc, "command": cmd,
           "log_tail": tail_log(outdir, "run_demo.log")}
    if rc != 0:
        out["status"] = "DEMO_RUN_FAILED"
    return out
