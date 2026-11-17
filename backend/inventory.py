"""Code-grounded inventory of the CONFIGURED NeoAG repository (D2).

Reads (read-only) the configured repo's production workflow TOML and
extracts the real stage set; anything not evidenced in those files is
labelled, never invented. If the repo is not readable from here the
inventory says so explicitly and falls back to the demo's built-in
stage plan, clearly labelled as NOT code-verified.

This is what the Full Pipeline Review enumerates - all real stages,
even where no alternative tool is found - so a review can never
quietly shrink to a convenient subset.
"""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from .store import now

NEOAG_REPO = os.environ.get(
    "NEOAG_REPO", "/data2/caojingqi/original_OpenNeoAG/neoag")
TOML_REL = "configs/workflows/production_workflow.example.toml"
# Stages documented for the production workflow but not present as
# [stages.*] entries in the example TOML (presentation/ranking/reports
# are run-level phases in docs/PRODUCTION_WORKFLOW.md). Listed here so
# the inventory STAYS COMPLETE, with their provenance labelled.
DOC_LEVEL_STAGES = [
    {"id": "presentation", "name": "Presentation & APPM scoring",
     "provenance": "docs/PRODUCTION_WORKFLOW.md (run-level phase, not a "
                   "[stages.*] key in the example workflow TOML)"},
    {"id": "safety_escape", "name": "Safety & immune escape",
     "provenance": "docs/PRODUCTION_WORKFLOW.md (run-level phase)"},
    {"id": "ranking", "name": "Ranking",
     "provenance": "docs/PRODUCTION_WORKFLOW.md (run-level phase)"},
    {"id": "reports", "name": "Reports",
     "provenance": "docs/PRODUCTION_WORKFLOW.md (run-level phase)"},
]


def _read_git_commit(repo: Path) -> str:
    try:
        head = (repo / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split("ref:", 1)[1].strip()
            return (repo / ".git" / ref).read_text().strip()[:40]
        return head[:40]
    except Exception:
        return "unknown (not readable)"


# ---------------------------------------------------------------------------
# M6 A3: baseline identification from the ACTUAL deployed implementation.
# A stage's tools may be declared in the workflow TOML (stage-level OR
# run-level groups), referenced as runner scripts in its command, wired
# in the planner code, implemented as an internal module, or pinned in
# the conda environment files. Absence of a name in one TOML key is
# NOT evidence of absence - every candidate source below is searched and
# every tool asserted with file-level evidence.
# ---------------------------------------------------------------------------

PLANNER_SCAN = {  # stage -> relpath whose code wires the stage's tools
    "hla_typing": "src/neoag/open_neo/capability_planner.py"}
# tokens looked for in scripts/, conda/, modules/, src/ stems per stage
STAGE_SCAN_TOKENS = {
    "hla_typing": ["optitype", "spechla", "hla_la", "hlaconsensus",
                   "hla_consensus"],
    "splice_discovery": ["splicemutr", "spladder"],
    "fusion_discovery": ["fusioncatcher", "arriba", "starfusion",
                         "easyfuse"],
    "purity_cnv": ["ascat", "sequenza", "facets", "purple"],
    "ranking": ["score", "appm"],
    "reports": ["report"],
    "safety_escape": ["safety", "immune_escape", "escape"],
    "presentation": ["netmhcpan", "mhcflurry", "netmhcstabpan", "netchop",
                     "binding_predictors", "appm"],
}
INTERNAL_STAGES = {"ranking", "reports", "safety_escape"}
_SCRIPT_TOKEN = re.compile(
    r"scripts/run_([a-z0-9][a-z0-9_]*?)_(?:sample|container|multi_tool|"
    r"multi|fastq_to_tpm|loh|example)\.sh")


def _conda_pins(repo: Path) -> dict:
    """tool -> (version|None, env file) from the repo's env yml files.

    M8 fix (root-cause 1.4): the old regex accepted only 'name==ver'
    while conda ymls write 'name=ver', silently dropping every pin
    (facets/sequenza/ascat all parsed to None against the real repo).
    Parsing is delegated to baselines.parse_conda_env, which handles
    name=X.Y, name==X.Y and unpinned forms; the tuple shape is kept
    for existing call sites."""
    from .baselines import repo_conda_pins
    pins = {}
    for tool, entries in repo_conda_pins(repo).items():
        first = next((e for e in entries if e.get("version")),
                     entries[0] if entries else None)
        if first:
            pins.setdefault(tool, (first.get("version"), first["file"]))
    return pins


def _planner_tools(repo: Path, rel: str) -> list[dict]:
    out = []
    try:
        lines = (repo / rel).read_text(errors="replace").splitlines()
    except OSError:
        return out
    for i, ln in enumerate(lines, 1):
        for m in re.finditer(r'run_([a-z0-9_]+?)_sample\.sh|tool == "([a-z0-9_]+)"',
                             ln):
            tok = m.group(1) or m.group(2)
            if tok and len(tok) > 2:
                out.append({"token": tok, "evidence": f"{rel}:{i}"})
    return out


def identify_baseline(repo: Path, stage_id: str, sdef, run_cfg, pins,
                      scan_names) -> dict:
    """Evidence-grounded baseline for one stage. Never invents a tool:
    each entry names the exact file (and line where applicable)."""
    tools, consensus = [], False

    def add(name, role, evidence):
        name = str(name).strip()
        if not name:
            return
        for t in tools:
            if t["name"].lower() == name.lower():
                t["evidence"].append(evidence)
                return
        pin = pins.get(name.lower())
        tools.append({"name": name, "role": role, "evidence": [evidence],
                      "pinned_version": pin[0] if pin and pin[0] else
                      ("declared in " + pin[1] + " (version unpinned)"
                       if pin else None)})

    if isinstance(sdef, dict):
        if sdef.get("source"):
            add(sdef["source"], "declared stage source",
                f"{TOML_REL} [stages.{stage_id}].source")
        cmd = str(sdef.get("command") or "")
        for m in _SCRIPT_TOKEN.finditer(cmd):
            add(m.group(1), "runner script named by the stage command",
                f"{TOML_REL} [stages.{stage_id}].command -> scripts/")
    # run-level required groups: run.required_tool_groups keys ARE
    # stage ids (purity_cnv, hla_loh) - multi-tool consensus stages
    grp = (run_cfg.get("required_tool_groups") or {}).get(stage_id)
    if isinstance(grp, dict):
        for t in grp.get("tools") or []:
            add(t, f"required_tool_groups member (min_successful="
                   f"{grp.get('min_successful')})",
                f"{TOML_REL} [run].required_tool_groups.{stage_id}")
        consensus = int(grp.get("min_successful") or 1) > 1
    if stage_id == "presentation":
        preds = run_cfg.get("presentation_predictors") or []
        for t in preds:
            add(t, "presentation consensus predictor",
                f"{TOML_REL} [run].presentation_predictors")
        consensus = len(preds) > 1
    # planner code wiring (e.g. hla_typing runs optitype/spechla/hla_la
    # and a consensus step - invisible in the TOML)
    if stage_id in PLANNER_SCAN:
        # the planner file wires several stages - keep only the hits
        # whose token belongs to THIS stage's vocabulary
        toks = STAGE_SCAN_TOKENS.get(stage_id, [])
        for hit in _planner_tools(repo, PLANNER_SCAN[stage_id]):
            if any(t in hit["token"] or hit["token"] in t for t in toks):
                add(hit["token"], "wired in planner code", hit["evidence"])
    # stages whose baseline is NeoAG's own code, not an external tool:
    # their token hits are named as the internal implementation
    if stage_id in INTERNAL_STAGES:
        for tok in STAGE_SCAN_TOKENS.get(stage_id, []):
            hits = [where for nm, where in scan_names if tok in nm][:3]
            if hits and not any(t["role"].startswith("internal")
                                for t in tools):
                add(f"NeoAG internal implementation ('{tok}')",
                    "internal stage code (no external tool to replace)",
                    "; ".join(hits))
        return {"identified": bool(tools), "consensus": False,
                "tools": tools,
                "note": "" if tools else (
                    "no internal implementation files matched")
                       or "no internal implementation files matched"}
    # repo-wide stem scan for the stage's tokens (conda envs, scripts,
    # modules) - a file named after the tool in the deployed repo is
    # direct implementation evidence
    for tok in STAGE_SCAN_TOKENS.get(stage_id, []):
        if "consensus" in tok:
            # a consensus/merge step names the stage as multi-tool
            if any(nm.startswith(tok[:4]) and "consensus" in nm
                   for nm, _ in scan_names):
                consensus = True
            continue
        for nm, where in scan_names:
            if tok in nm and not any(
                    tok in t["name"].lower() or
                    t["name"].lower() in tok for t in tools):
                add(tok, f"implementation evidence in repo ({where})",
                    where)
    # internal-implementation stages (ranking/reports/safety): the
    # baseline IS NeoAG code, not an external tool
    if not tools:
        for tok in STAGE_SCAN_TOKENS.get(stage_id, []):
            hits = [where for nm, where in scan_names if tok in nm][:3]
            if hits:
                add(f"NeoAG internal implementation ('{tok}')",
                    "internal stage code (no external tool to replace)",
                    "; ".join(hits))
    note = ""
    if not tools:
        note = ("no baseline evidence found in workflow TOML, planner "
                "code, runner scripts, conda envs or module dirs - a "
                "replacement assessment is BLOCKED until the baseline "
                "is identified")
    return {"identified": bool(tools), "consensus": consensus,
            "tools": tools, "note": note}


def load_inventory(force: bool = False) -> dict:
    """Parse the configured repo; deterministic per repo content."""
    repo = Path(NEOAG_REPO)
    toml_path = repo / TOML_REL
    stages, limitations = [], []
    run_cfg, stage_defs = {}, {}
    code_verified = toml_path.is_file()
    if code_verified:
        try:
            data = tomllib.loads(toml_path.read_text())
            run_cfg = data.get("run") or {}
            stage_defs = data.get("stages") or {}
            for sid, sdef in stage_defs.items():
                tools = []
                for key in ("source", "tools", "primary", "tool"):
                    v = sdef.get(key) if isinstance(sdef, dict) else None
                    if v:
                        tools.append(str(v))
                stages.append({
                    "id": sid, "name": sid.replace("_", " ").title(),
                    "enabled": (sdef.get("enabled", True)
                                if isinstance(sdef, dict) else True),
                    "current_tools": tools,
                    "provenance": f"{TOML_REL} [stages.{sid}]",
                    "code_verified": True})
        except Exception as e:
            limitations.append(f"workflow TOML parse failed: "
                               f"{type(e).__name__}: {str(e)[:160]}")
            code_verified = False
    if not code_verified:
        limitations.append(
            "configured NeoAG repo/TOML not readable - stage list below "
            "is the demo's built-in plan and is NOT code-verified")
        from .mock_data import STAGES
        stages = [{"id": s["id"], "name": s["name"], "enabled": True,
                   "current_tools": [],
                   "provenance": "demo built-in stage plan (unverified "
                                 "against repo)", "code_verified": False}
                  for s in STAGES]
    # keep the inventory COMPLETE: append run-level phases documented in
    # the repo docs but absent from the TOML (labelled, not merged)
    known = {s["id"] for s in stages}
    for d in DOC_LEVEL_STAGES:
        if d["id"] not in known:
            stages.append({"id": d["id"], "name": d["name"],
                           "enabled": True, "current_tools": [],
                           "provenance": d["provenance"],
                           "code_verified": False})
    # M6 A3: per-stage baseline from every declared/implemented source
    pins, scan_names = {}, []
    if repo.is_dir():
        pins = _conda_pins(repo)
        for sub, kind in (("scripts", "file"), ("modules", "dir"),
                          ("src/neoag", "file")):
            base = repo / sub
            if not base.is_dir():
                continue
            it = base.iterdir()
            for p in it:
                stem = p.name.lower().rsplit(".", 1)[0]
                if kind == "file" and not p.is_file():
                    continue
                scan_names.append((stem, f"{sub}/{p.name}"))
    for s in stages:
        s["baseline"] = identify_baseline(
            repo, s["id"], stage_defs.get(s["id"]), run_cfg, pins,
            scan_names)
        s["current_tools"] = sorted({
            *[t.lower() for t in s.get("current_tools") or []],
            *[t["name"] for t in s["baseline"]["tools"]
              if not t["name"].startswith("NeoAG internal")]})
    n_blocked = [s["id"] for s in stages if not s["baseline"]["identified"]]
    return {
        "captured_at": now(),
        "repo": str(repo),
        "repo_readable": repo.is_dir(),
        "config_file": str(toml_path) if code_verified else None,
        "git_commit": _read_git_commit(repo) if repo.is_dir() else None,
        "code_verified": code_verified,
        "stages": stages,
        "baseline_blocked_stages": n_blocked,
        "limitations": limitations + [
            "baseline identification asserts tools with file evidence "
            "from the configured repo (TOML / planner code / runner "
            "scripts / conda envs / module dirs); the actual DEPLOYED "
            "binary version of each tool is a separate question, "
            "verified per candidate via the version/installed-version "
            "chains",
            "this list describes stages; a Discovery run still must "
            "state which sources it could not reach"],
    }


def targets_for_ui() -> list[dict]:
    """Target selector list for the Discovery UI (D2): derived from this
    inventory, not hardcoded."""
    return [{"id": s["id"], "label": s["name"],
             "code_verified": s.get("code_verified", False),
             "tools": s.get("current_tools") or []}
            for s in load_inventory()["stages"]]
