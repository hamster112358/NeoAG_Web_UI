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
    return errs


def _env():
    e = os.environ.copy()
    src = os.path.join(NATIVE_ROOT, "src")
    e["PYTHONPATH"] = src + (":" + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
    e.setdefault("NEOAG_TOOLS_ROOT", NATIVE_ROOT)
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
    for k in _INPUT_KEYS + _SCIENTIFIC_KEYS:
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
    for k in _INPUT_KEYS + _SCIENTIFIC_KEYS:
        v = params.get(k)
        if v in (None, "", []):
            continue
        payload[k] = (list(v) if k == "hla_alleles" and not isinstance(v, str)
                      else v)
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
# M6 PART D2: real input discovery for the unified native entry. The
# user names a data directory; this is a READ-ONLY scan of file NAMES
# (contents are never read - patient data safety), mapping discovered
# files onto the entry point's input roles. The proposal becomes the
# manifest the human confirms; the manifest is then authoritative and
# the native planner stays the authority on what can run (its --mode
# plan pass is the honest preflight).
_DISCOVER_MAX_FILES = 1500
_DISCOVER_MAX_DEPTH = 3
_SKIP_DIR_NAMES = (".git", "__pycache__", "node_modules", ".snakemake",
                   ".nextflow")


def _toks(fn: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9*]+", " ", fn.lower())


def _modality(lc: str) -> str:
    """tumor | normal | rna | unknown from filename words. Single-letter
    T/N tokens (the classic T/N pairing convention) count; R1/R2 are read
    numbers, never RNA. Human confirmation of the manifest is the safety
    net for genuinely ambiguous names."""
    words = [w for w in _toks(lc).split() if w]
    if any(w in ("rna", "trna", "rnaseq", "rnaseq") or
               (w.startswith("rna") and w != "rnaseq") for w in words):
        return "rna"
    if any(w in ("normal", "norm", "germline", "blood", "n", "ng")
           for w in words):
        return "normal"
    if any(w in ("t", "tum", "tumor", "tumour", "tumort") for w in words) \
            or "tumor" in lc or "tumour" in lc:
        return "tumor"
    return "unknown"


def _classify_file(fn: str):
    """Return (flag, role_label, confidence, basis) or a tuple with
    flag in ('ambiguous', 'germline_vcf', None)."""
    lc = fn.lower()
    toks = " " + _toks(fn) + " "
    tabular = lc.endswith((".tsv", ".csv", ".txt"))
    is_fastq = lc.endswith((".fastq", ".fq", ".fastq.gz", ".fq.gz"))
    is_vcf = lc.endswith((".vcf", ".vcf.gz"))
    is_bam = lc.endswith((".bam", ".cram"))
    if "fusion" in toks and tabular:
        return ("fusion_tsv", "Fusion caller table", 0.9,
                "fusion named tabular output")
    if ("junction" in toks or "sj.out" in lc or "splice" in toks) \
            and tabular:
        return ("splice_junction_tsv", "Splice junction table", 0.85,
                "junction-named tabular output")
    if "loh" in toks and tabular:
        return ("hla_loh_tsv", "HLA LOH evidence", 0.85,
                "LOH-named tabular output")
    if ("purity" in toks or "ploidy" in toks) and tabular:
        return ("purity_tsv", "Purity/ploidy", 0.85,
                "purity/ploidy-named table")
    if any(w in toks for w in ("cnv", "sequenza", "facets", "segments",
                               "purple")) and tabular:
        return ("cnv_tsv", "Copy-number segments", 0.8,
                "CNV-tool-named table")
    if ("hla" in toks or "typing" in toks) and tabular:
        return ("hla_file", "HLA typing result", 0.8,
                "HLA-named tabular output")
    if any(w in toks for w in ("expression", "tpm", "fpkm", "counts",
                               "quant", "salmon", "rsem")) and tabular:
        return ("expression_tsv", "Gene expression", 0.75,
                "expression-named table")
    if is_fastq:
        mod = _modality(lc)
        flag = {"tumor": "tumor_dna_fastq", "normal": "normal_dna_fastq",
                "rna": "tumor_rna_fastq"}.get(mod)
        if flag is None:
            return ("ambiguous", f"FASTQ reads with no tumor/normal/RNA "
                                 f"signal in the name", 0.4,
                    "filename lacks modality hints")
        return (flag, {"tumor_dna_fastq": "Tumor DNA reads",
                       "normal_dna_fastq": "Normal DNA reads",
                       "tumor_rna_fastq": "RNA reads"}[flag],
                0.7, "fastq + modality name hint")
    if is_bam:
        mod = _modality(lc)
        flag = {"tumor": "tumor_dna_bam", "normal": "normal_dna_bam",
                "rna": "tumor_rna_bam"}.get(mod)
        if flag is None:
            return ("ambiguous", "alignment with no tumor/normal/RNA "
                                 "signal in the name", 0.4,
                    "filename lacks modality hints")
        return (flag, {"tumor_dna_bam": "Tumor DNA alignments",
                       "normal_dna_bam": "Normal DNA alignments",
                       "tumor_rna_bam": "RNA alignments"}[flag],
                0.8, "bam/cram + modality name hint")
    if is_vcf:
        if any(w in toks for w in ("sv", "structural", "delly", "manta",
                                   "sopra")):
            return ("sv_vcf", "Structural variant calls", 0.85,
                    "SV-named VCF")
        if any(w in toks for w in ("somatic", "tn", "snpindel", "mutect",
                                   "strelka", "varscan", "msisensor",
                                   "snpeff", "vep", "filtered",
                                   "tumor")):
            return ("somatic_vcf", "Somatic variant calls", 0.85,
                    "somatic-caller-named VCF")
        if any(w in toks for w in ("germline", "normal", "gatk hc",
                                   "freebayes")):
            return ("germline_vcf", "Germline VCF (informational)", 0.7,
                    "germline-named VCF")
        return ("ambiguous", "VCF with no somatic/germline/SV signal",
                0.4, "filename lacks caller/modality hints")
    if lc.endswith(".bed"):
        return ("capture_bed", "Capture kit BED", 0.7, ".bed extension")
    return (None, "", 0.0, "")


def discover_inputs(data_dir: str) -> dict:
    """Read-only discovery of native entry inputs under data_dir."""
    out = {"data_dir": data_dir, "exists": False, "scanned_at": _now(),
           "inputs": [], "ambiguous": [], "informational": [],
           "unclassified": [], "missing": [], "notes": [],
           "sample_guess": "", "n_files_seen": 0, "capped": False}
    if not os.path.isdir(data_dir):
        out["error"] = "not a directory or not readable"
        return out
    out["exists"] = True
    base = os.path.abspath(data_dir)
    stem = os.path.basename(base.rstrip("/"))
    import re as _re
    if _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,24}", stem or "") \
            and stem.lower() not in ("data", "input", "inputs", "files",
                                     "raw", "bam", "vcf", "fastq"):
        out["sample_guess"] = stem
    n = 0
    capped = False
    stack = [(base, 0)]
    while stack:
        d, depth = stack.pop()
        if depth > _DISCOVER_MAX_DEPTH:
            continue
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in entries:
            p = os.path.join(d, fn)
            if fn.startswith(".") and depth:
                continue
            if os.path.isdir(p):
                if not os.path.islink(p) and fn not in _SKIP_DIR_NAMES:
                    stack.append((p, depth + 1))
                continue
            n += 1
            if n > _DISCOVER_MAX_FILES:
                capped = True
                break
            flag, role, conf, basis = _classify_file(fn)
            rel = os.path.relpath(p, base)
            if flag is None:
                if len(out["unclassified"]) < 60:
                    out["unclassified"].append(rel)
            elif flag == "ambiguous":
                out["ambiguous"].append({"file": rel, "reason": role})
            elif flag == "germline_vcf":
                out["informational"].append(
                    {"file": rel, "role": role,
                     "note": "the unified entry consumes somatic calls; "
                             "a germline VCF is recorded, not forwarded"})
            else:
                out["inputs"].append({"flag": flag, "role": role,
                                      "path": p, "confidence": conf,
                                      "basis": basis})
        if capped:
            break
    out["n_files_seen"] = n
    out["capped"] = capped
    if capped:
        out["notes"].append(f"scan stopped after {_DISCOVER_MAX_FILES} "
                            "files - move the case's files into one "
                            "flatter directory if inputs are missing")
    got = {i["flag"] for i in out["inputs"]}
    if not ({"somatic_vcf", "tumor_dna_bam", "tumor_dna_fastq"} & got):
        out["missing"].append(
            "no somatic variant calls AND no tumor DNA alignments/reads - "
            "the entry needs at least one of somatic VCF, tumor DNA BAM "
            "or tumor DNA FASTQ")
    if "hla_file" not in got:
        out["notes"].append(
            "no HLA typing result found - HLA stays unset; the native "
            "planner types HLA itself from DNA inputs when the toolchain "
            "is installed, and its plan reports exactly what is missing")
    if not ({"tumor_rna_bam", "tumor_rna_fastq"} & got):
        out["notes"].append(
            "no RNA inputs found - RNA-dependent branches (fusion, "
            "splice, RNA evidence) will be skipped by the plan")
    # deterministic order, and only the first file per single-valued flag
    # is forwarded; additional candidates surface as ambiguity for the
    # human - never a silent pick
    first = {}
    merged = []
    for i in out["inputs"]:
        if i["flag"] in _LIST_KEYS:
            merged.append(i)
            continue
        if i["flag"] not in first:
            first[i["flag"]] = i
            merged.append(i)
        else:
            prev = first[i["flag"]]
            out["ambiguous"].append(
                {"file": i["path"],
                 "reason": f"second candidate for "
                           f"{prev['role'].lower()} "
                           f"(kept: {os.path.basename(prev['path'])})"})
    out["inputs"] = merged
    return out


def params_from_manifest(manifest: dict) -> dict:
    """Confirmed manifest -> native_params (manifest is authoritative;
    this only maps what the human confirmed - removals and the final
    sample_id from the confirmation step included - and adds nothing
    the human did not see)."""
    params = {}
    prop = manifest.get("proposal") or {}
    conf = manifest.get("confirmed") or {}
    if not isinstance(conf, dict):
        conf = {}
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
    sid = str(conf.get("sample_id") or prop.get("sample_id") or "")
    if sid:
        params["sample_id"] = sid
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
