"""Pre-submission readiness assessment (Milestone 10 §9).

One honest answer to "can the ENTIRE workflow the user just configured
actually run on this host?" - tools, references and models, genome
build, the confirmed input files, stage dependencies and permissions.

Principles:
 - the NATIVE planner remains the authority on scientific readiness:
   its own PASS/BLOCK verdict and blocking_issues are merged in
   verbatim, never re-judged by this module;
 - this module adds only the deployment-side facts the planner cannot
   see (writable scratch root, manifest files still present on disk,
   authorized input roots, deployment env sourcing);
 - ALL blockers are collected and returned together - the first
   blocker never hides the second;
 - readiness is computed from live checks on every call. Nothing here
   is cached across calls and nothing is fabricated: a check that
   cannot be made is reported as a warning with its real reason.
"""
from __future__ import annotations

import json
import os
import time

from . import native as native_mod

# Stable ids: the frontend translates these keys; `detail` stays a
# factual (English) description of the measured state.
# status: True ok / False blocking failure / None informational
# warning (with its honest reason, never a silent pass).


def _check(cid, ok, detail, blocking=True, kind="environment"):
    return {"id": cid, "ok": bool(ok) if ok is not None else None,
            "detail": detail, "blocking": bool(blocking) if ok is False
            else False, "kind": kind}


# ---------------------------------------------------------------------------
# M11 P0-7: blocker TAXONOMY, evaluated under the EFFECTIVE workflow.
#
# The brief demands that "what is missing" never comes back as one undifferentiated
# pile: a reference genome the SERVER lacks is somebody else's problem to fix;
# a patient file the case lacks is the user's; a tool gap is the operator's;
# and a dependency of a branch the user did NOT select must not block at all.
# Every entry therefore carries exactly one `kind`:
#   missing_patient_input      - data this case must supply
#   missing_server_reference   - reference asset this host must supply
#   ambiguous                  - two+ real candidates; needs a human pick
#   unsupported_format         - a file is present for a role but unusable
#   missing_tool               - toolchain/runtime the host must supply
#   unselected_stage_dependency- ONLY needed by a branch the workflow does
#                                not run -> advisory, never a blocker
# (Deployment-side checks additionally use environment / invalid_config /
#  not_authorized / planner_verdict - honest labels outside the six, never
#  smuggled into them.)
# ---------------------------------------------------------------------------

BLOCKER_KINDS = ("missing_patient_input", "missing_server_reference",
                 "ambiguous", "unsupported_format", "missing_tool",
                 "unselected_stage_dependency")

# fields whose value is a SERVER-side reference asset, not case data
SERVER_REF_FIELDS = {"reference_fasta", "gencode_gtf", "vep_cache",
                     "vep_bin", "rsem_reference", "hla_reference",
                     "annotation_gtf"}
# native routes belonging to the DNA structural-variant branch
_SV_ROUTES = {"sv_wgs", "sv_wes", "sv_intermediates"}


def workflow_assessment(disc, science=None):
    """Classify a discovery result under the EFFECTIVE selected workflow.

    `science` is the payload-shaped override dict; absent keys mean the
    interface's own defaults (M11 P0-6: the DNA-SV branch is OFF here
    unless explicitly ticked). Blocking is only asserted for items the
    selected workflow actually consumes; anything needed solely by an
    unselected branch is returned as an ADVISORY of kind
    unselected_stage_dependency so the user still sees what was left
    out and why. This never re-judges the native planner: its verdict
    arrives verbatim through assess()."""
    sci = dict(science or {})
    # effective DNA-SV selection (Web default: off -> skip True)
    sv_off = bool(sci.get("skip_dna_sv", True))
    blockers, advisories = [], []

    def route_of(reason):
        # native formats these as "required for sv_wgs"
        for r in _SV_ROUTES:
            if reason.endswith(r):
                return r
        return ""

    for m in disc.get("missing") or []:
        field = str(m.get("field") or "")
        reason = str(m.get("reason") or "")
        kind = ("missing_server_reference" if field in SERVER_REF_FIELDS
                else "missing_patient_input")
        if sv_off and route_of(reason) in _SV_ROUTES:
            advisories.append({
                "kind": "unselected_stage_dependency", "field": field,
                "reason": reason + " (the DNA SV branch is OFF here, so "
                                   "the selected workflow does not need "
                                   "it; tick it in Analysis options to "
                                   "make this a real requirement)"})
            continue
        blockers.append({"kind": kind, "field": field, "reason": reason})

    routes = disc.get("routes") or []
    sv_routes_present = [r for r in routes
                         if r.get("route") in _SV_ROUTES]
    if sv_off and sv_routes_present:
        advisories.append({
            "kind": "unselected_stage_dependency",
            "field": "dna_sv",
            "reason": "DNA SV inputs were detected but the DNA SV "
                      "branch is OFF by default here: the run will "
                      "record DNA_SV_UNASSESSED and no SV-derived "
                      "events unless the branch is ticked"})

    for a in disc.get("ambiguous") or []:
        field = str(a.get("field") or "")
        reason = str(a.get("reason") or "")
        if field == "inputs" and not a.get("candidates"):
            # route_inputs emits this when NO usable input was found -
            # that is a missing patient input, not a choice to make
            blockers.append({"kind": "missing_patient_input",
                             "field": field,
                             "reason": reason or
                             "no supported input detected"})
            continue
        blockers.append({"kind": "ambiguous", "field": field,
                         "reason": reason})

    for row in disc.get("inventory") or []:
        st = str(row.get("status") or "")
        if st in ("EMPTY", "INVALID", "NOT_WRITABLE"):
            blockers.append({
                "kind": "unsupported_format",
                "field": str(row.get("input_key") or ""),
                "reason": f"{row.get('path') or row.get('value') or ''}"
                          f": {st}"})

    return {"ready": not blockers, "blockers": blockers,
            "advisories": advisories,
            "effective": {"skip_dna_sv": sv_off},
            "note": "blockers evaluated for the workflow as selected; "
                    "the native planner verdict remains authoritative "
                    "at readiness time"}


def _input_paths_from_params(params):
    """Absolute-path values among the forwardable input keys (the same
    allowlist native.py uses)."""
    keys = native_mod._INPUT_KEYS + ("sample_manifest",)
    out = []
    for k in keys:
        v = params.get(k)
        if v in (None, "", []):
            continue
        for item in (v if isinstance(v, (list, tuple)) else [v]):
            s = str(item)
            if s.startswith("/"):
                out.append((k, s))
    return out


def _inputs_of_manifest(man):
    """(path, note) for every file the CONFIRMED manifest says the run
    reads: the native sample-manifest JSON itself plus the input files
    recorded inside it."""
    out = []
    prop = (man or {}).get("proposal") or {}
    mpath = prop.get("manifest_path") or ""
    if mpath:
        out.append(("sample_manifest", mpath))
        try:
            with open(mpath, encoding="utf-8") as f:
                data = json.load(f)
            for flag, val in (data.get("inputs") or {}).items():
                for item in (val if isinstance(val, (list, tuple))
                             else [val]):
                    s = str(item)
                    if s.startswith("/"):
                        out.append((flag, s))
        except (OSError, ValueError):
            pass
    return out


def assess(store, profile="gateway", params=None, manifest_id="",
           run_planner=True):
    """Full readiness for one prospective native run.

    profile      - "gateway" (real case) or "demo" (bundled fixtures).
    params       - resolved native params (manifest already merged).
    manifest_id  - set when the run rides a confirmed manifest (the UI
                   path); re-checks that record is still confirmed.
    run_planner  - False skips the (expensive) live planner pass: used
                   for the cheap server-side revalidation at submit,
                   where the planner verdict was just produced by the
                   readiness/preview call the UI made moments before.
    Returns {status, checks, blockers, warnings, plan, checked_at}.
    """
    params = dict(params or {})
    checks = []
    cap = native_mod.probe()

    # --- deployment runtime ---
    checks.append(_check(
        "native_runtime", cap.get("available"),
        (f"{native_mod.NATIVE_PY} + {native_mod.NATIVE_ROOT}: importable"
         if cap.get("available") else
         "native NeoAG python/source not importable: " +
         str(cap.get("import_error") or "probe failed")[:300]),
        kind="missing_tool"))
    if not cap.get("available"):
        return _finish(checks, plan=None)

    gw = cap.get("gateway") or {}
    checks.append(_check(
        "gateway_service", None if not gw.get("running") else True,
        (f"loopback gateway on {gw.get('url')} answered /health"
         if gw.get("running") else
         "the dedicated loopback gateway is not currently running; it "
         "auto-starts at submit time (same supervised start the run "
         "itself performs)"), blocking=False))

    tenv = cap.get("tools_env") or {}
    ok = bool(tenv.get("sourced_ok"))
    checks.append(_check(
        "tools_env", ok,
        ("deployment env sourced (" + str(tenv.get("script")) + ")"
         if ok else "conf/tools.env.sh could not be sourced - tool "
                    "resolution would use a bare PATH (known to "
                    "mis-report every tool UNAVAILABLE)"),
        kind="missing_tool"))
    for key, cid in (("tools_manifest", "tools_manifest"),
                     ("reference_manifest", "reference_manifest")):
        row = tenv.get(key) or {}
        checks.append(_check(
            cid, row.get("present"),
            (f"{row.get('path')} present" if row.get("present")
             else f"{row.get('path')} missing - capability planning "
                  "degrades without it"),
            kind="missing_server_reference"))

    tc = cap.get("toolchain") or {}
    ref = tc.get("reference_fasta") or {}
    checks.append(_check(
        "reference_fasta", ref.get("present"),
        f"{ref.get('path')}: " + ("present" if ref.get("present")
                                  else "MISSING - no genome reference"),
        kind="missing_server_reference"))
    build = str(params.get("genome_build") or "GRCh38")
    if build != "GRCh38":
        checks.append(_check(
            "genome_build", None,
            f"requested genome build {build}: this deployment's reference"
            " FASTA is the GRCh38 tree; the native planner states exactly"
            " which {b} assets exist".replace("{b}", build),
            blocking=False))
    vep = tc.get("vep_bin") or {}
    vcache = tc.get("vep_cache") or {}
    if vep.get("present") and vcache.get("present"):
        checks.append(_check("vep", True,
                             f"VEP {vep.get('path')} + cache "
                             f"{vcache.get('path')}", blocking=False))
    else:
        checks.append(_check(
            "vep", None,
            "VEP binary/cache not both present - annotation branches "
            "degrade or block; the planner reports the exact verdict",
            blocking=False, kind="missing_server_reference"))

    # --- configuration ---
    bad = native_mod.validate_scientific(params)
    checks.append(_check(
        "scientific_config", not bad,
        "every override is a real, in-range option" if not bad
        else "; ".join(bad), kind="invalid_config"))

    if not os.access(native_mod.WORK, os.W_OK):
        checks.append(_check(
            "work_writable", False,
            f"scratch root {native_mod.WORK} is not writable by the "
            "web server user"))
    else:
        checks.append(_check("work_writable", True,
                             f"{native_mod.WORK} writable",
                             blocking=False))

    # --- the confirmed inputs (manifest first, then raw params) ---
    manifest = None
    if manifest_id:
        manifest = store.manifest_get(str(manifest_id)) if store else None
        if not manifest:
            checks.append(_check("manifest", False,
                                 f"manifest {manifest_id} no longer "
                                 "exists - re-run discovery",
                                 kind="missing_patient_input"))
        elif manifest.get("status") != "confirmed":
            checks.append(_check(
                "manifest", False,
                f"manifest {manifest_id} is '{manifest.get('status')}', "
                "not confirmed - the confirmed manifest is the required "
                "input of record", kind="missing_patient_input"))
        else:
            checks.append(_check("manifest", True,
                                 f"manifest {manifest_id} confirmed",
                                 blocking=False))
    files = (_inputs_of_manifest(manifest) if manifest
             else _input_paths_from_params(params))
    if profile == "gateway":
        missing = [(flag, p) for flag, p in files if not os.path.exists(p)]
        if not files:
            checks.append(_check(
                "inputs_present", False,
                "no input files are on record for this run - confirm a "
                "data directory first", kind="missing_patient_input"))
        elif missing:
            checks.append(_check(
                "inputs_present", False,
                "input file(s) no longer exist: " +
                "; ".join(f"{flag}={p}" for flag, p in missing[:8]),
                kind="missing_patient_input"))
        else:
            checks.append(_check(
                "inputs_present", True,
                f"all {len(files)} recorded input file(s) exist",
                blocking=False))
        # authorization boundary: every input must live under an
        # authorized root (same rule discovery enforces).
        escapes = [(f, p) for f, p in files
                   if not any(p == r or p.startswith(r.rstrip(os.sep) + os.sep)
                              for r in native_mod.AUTHORIZED_INPUT_ROOTS)]
        if escapes:
            checks.append(_check(
                "inputs_authorized", False,
                "input(s) outside the authorized roots: " +
                "; ".join(f"{f}={p}" for f, p in escapes[:8]) +
                " (allowed: " + ", ".join(native_mod.AUTHORIZED_INPUT_ROOTS)
                + ")", kind="not_authorized"))
        else:
            checks.append(_check(
                "inputs_authorized", True,
                "inputs live under an authorized root: " +
                ", ".join(native_mod.AUTHORIZED_INPUT_ROOTS),
                blocking=False))

    if profile == "demo":
        ok = native_mod.demo_available()
        checks.append(_check(
            "demo_fixtures", ok,
            "bundled synthetic fixtures present" if ok else
            f"native demo fixtures missing under "
            f"{native_mod.NATIVE_ROOT}/data/fixtures"))

    # --- the native planner's own verdict (scientific authority) ---
    plan = None
    if (profile == "gateway" and run_planner and files):
        sid = str(params.get("sample_id") or params.get("case_id")
                  or "READINESS")
        outdir = native_mod.job_outdir(
            "readiness-" + time.strftime("%Y%m%d%H%M%S"))
        plan = native_mod.run_plan(params, outdir)
        if plan.get("status") == "PASS":
            checks.append(_check(
                "planner", True,
                f"native planner PASS ({len(plan.get('steps') or [])} "
                f"planned steps) - run_id {plan.get('run_id')}",
                blocking=False, kind="planner_verdict"))
        else:
            issues = plan.get("blocking_issues") or []
            detail = ("; ".join(str(x) for x in issues)
                      or f"planner status {plan.get('status')}: "
                         + str(plan.get("error") or
                               plan.get("detail") or "")[:200])
            checks.append(_check("planner", False, detail,
                                 kind="planner_verdict"))
        for ev in (plan.get("missing_evidence") or []):
            checks.append(_check("planner_evidence", None,
                                 f"missing evidence (planner does not "
                                 f"block on it): {ev}", blocking=False,
                                 kind="planner_verdict"))
    elif profile == "gateway" and not files:
        pass  # inputs_present already failed with the concrete reason
    elif profile == "gateway":
        checks.append(_check(
            "planner", None,
            "planner verdict refreshed by the readiness call the "
            "interface just ran (cheap revalidation here)",
            blocking=False))

    return _finish(checks, plan=plan)


def _finish(checks, plan=None):
    blockers = [c for c in checks if c["ok"] is False and c["blocking"]]
    warnings = [c for c in checks if c["ok"] is None]
    return {
        "status": "BLOCKED" if blockers else "READY",
        "checks": checks,
        "blockers": [f"{c['id']}: {c['detail']}" for c in blockers],
        "warnings": [f"{c['id']}: {c['detail']}" for c in warnings],
        "plan": ({"status": plan.get("status"),
                  "steps": [{"name": s.get("name"),
                             "status": s.get("status"),
                             "step_id": s.get("step_id")}
                            for s in (plan or {}).get("steps") or []],
                  "blocking_issues": (plan or {}).get("blocking_issues"),
                  "missing_evidence": (plan or {}).get("missing_evidence"),
                  "run_id": (plan or {}).get("run_id")}
                 if plan else None),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
