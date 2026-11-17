"""S5 baseline context: which tools the DEPLOYMENT actually stands on,
at which evidence level (root-cause 1.4: "repository presence is not
execution; config is not deployment").

Evidence levels (PART 3 of the reliability brief, ordered):
  1 present-in-literature  named only in retrieved papers
  2 supported              the repo contains code that could run it
  3 declared-config        a workflow/config file names it
  4 enabled-profile        the active profile keeps its stage enabled
  5 planner-selected       runtime planner would select it
  6 executed               it ran in the reviewed run
  7 contributed            its output entered the reviewed run's result

A literature-review demo run NEVER exceeds level 3-4 (the workflow
TOML it reads is the repo's example file) - the report says exactly
that instead of implying the review measured execution.

Intended vs achieved consensus: required_tool_groups with
min_successful=2 declares an intended multi-tool consensus; whether a
specific sample achieved it is a RUN fact, recorded separately and
never inferred from configuration.

Pin parsing fixes the M6 defect (inventory.py's regex only accepted
name==ver while conda ymls write name=ver, so every pin was silently
dropped): both forms parse now, per-file, with file evidence.

The deployed-env probe is read-only: it lists conda-meta / site-
packages markers under configured env roots (default: the machine's
conda envs dir; overridable via NEOAG_ENV_ROOTS). No execution, no
patient data, bounded depth.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .store import now

LEVELS = {
    1: "present in retrieved literature only",
    2: "supported by code in the configured repo",
    3: "declared in a workflow/config file",
    4: "enabled in the active profile",
    5: "selected by the runtime planner",
    6: "executed in the reviewed run",
    7: "contributed to the reviewed run's output",
}

# name[=|==]version and bare names in conda env yml dependency lists
_PIN_RE = re.compile(
    r"^\s*-\s*([A-Za-z0-9_.\-]+)\s*(?:(?:==|=)\s*([\w.\-]+))?\s*$")


def parse_conda_env(path: Path) -> dict:
    """Parse one conda env yml -> {dep_name_lower: version|None} +
    channels. Handles name=X.Y, name==X.Y and unpinned '- name'."""
    out = {"pins": {}, "channels": [], "file": path.name,
           "error": None}
    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        out["error"] = f"{type(e).__name__}"
        return out
    in_deps = in_channels = False
    for line in text.splitlines():
        if re.match(r"^dependencies\s*:", line):
            in_deps, in_channels = True, False
            continue
        if re.match(r"^channels\s*:", line):
            in_deps, in_channels = False, True
            continue
        if re.match(r"^\S", line):        # new top-level key
            in_deps = in_channels = False
        if in_channels:
            m = re.match(r"^\s+-\s*([\w\-]+)\s*$", line)
            if m:
                out["channels"].append(m.group(1))
            continue
        if not in_deps:
            continue
        m = _PIN_RE.match(line)
        if m and re.search(r"[a-z]", m.group(1)):
            name = m.group(1).lower()
            if name in ("python", "r-base", "r", "pip", "conda"):
                continue
            out["pins"].setdefault(name, m.group(2))
    return out


def repo_conda_pins(repo: Path) -> dict:
    """tool -> list of {version, env file, channels} across all env
    ymls in the repo (first file wins per tool per file)."""
    pins: dict[str, list] = {}
    for env in sorted((repo / "conda").glob("*.y*ml")):
        parsed = parse_conda_env(env)
        for name, ver in parsed["pins"].items():
            pins.setdefault(name, []).append(
                {"version": ver, "file": env.name,
                 "channels": parsed["channels"]})
    return pins


# ---------------------------------------------------------------------------
# deployed-env probe (read-only)
# ---------------------------------------------------------------------------

def env_roots() -> list[Path]:
    raw = os.environ.get("NEOAG_ENV_ROOTS",
                         "/data1/neoag/miniforge3/envs")
    return [Path(p) for p in raw.split(os.pathsep) if p]


def probe_installed(name: str, roots: list[Path] | None = None) -> dict:
    """Installed version of a tool as conda-meta / dist-info markers
    under the configured env roots. Handles the R-package convention
    (conda-meta file 'r-<pkg>-*'), python dist-info, and a direct
    conda-meta entry (CLI tools). Read-only; returns evidence path.
    """
    out = {"installed_version": None, "evidence": None,
           "envs_seen": [], "checked_at": now()}
    slug = re.sub(r"[_]+", "-", (name or "").lower().strip())
    if not slug:
        out["reason"] = "empty tool name"
        return out
    for root in (roots if roots is not None else env_roots()):
        if not root.is_dir():
            continue
        try:
            envs = [d for d in root.iterdir() if d.is_dir()]
        except OSError:
            continue
        for env in sorted(envs):
            meta = env / "conda-meta"
            if not meta.is_dir():
                continue
            out["envs_seen"].append(env.name)
            try:
                cands = []
                for pat in (rf"{slug}-*.json", rf"r-{slug}-*.json",
                            rf"{slug.replace('-', '_')}-*.json"):
                    cands += list(meta.glob(pat))
                for f in sorted(cands):
                    stem = f.name[:-5]          # strip .json
                    m = re.match(
                        rf"^(?:r-)?{re.escape(slug)}-(\d[^_]*)_", stem,
                        re.I)
                    if not m:
                        continue
                    try:
                        j = json.loads(f.read_text(errors="replace"))
                        v = j.get("version") or m.group(1)
                    except Exception:
                        v = m.group(1)
                    if out["installed_version"] is None:
                        out["installed_version"] = v
                        out["evidence"] = f"conda-meta: {f}"
                    else:
                        out.setdefault("also", []).append(
                            f"{env.name}: {v}")
                # python dist-info route
                for di in env.glob(
                        f"lib/python3*/site-packages/{slug}-*.dist-info"):
                    v = di.name[len(slug) + 1:-len(".dist-info")]
                    if out["installed_version"] is None:
                        out["installed_version"] = v.strip("-")
                        out["evidence"] = f"dist-info: {di}"
            except OSError:
                continue
    if out["installed_version"] is None:
        out["reason"] = ("no conda-meta/dist-info marker for this tool "
                         "under the configured env roots - installed "
                         "version NOT verified (absence from this "
                         "probe is not proof of absence from the "
                         "machine)")
    return out


# ---------------------------------------------------------------------------
# baseline context assembly
# ---------------------------------------------------------------------------

def _pins_for_tool(pins: dict, tool: str) -> list:
    """Pin entries for a tool name, including the conda naming
    conventions a workflow config does not use: the R package may be
    pinned as 'r-<tool>' and CLI wrappers as '<tool>-utils'."""
    tl = (tool or "").lower()
    out = list(pins.get(tl, []))
    for alias in (f"r-{tl}", f"{tl}-utils"):
        out += pins.get(alias, [])
    return out


def level_for_stage(stage: dict, pins: dict,
                    env_probe_results: dict) -> dict:
    """Per-tool baseline context rows for one inventory stage. The
    inventory's code-grounded `baseline.tools` evidence fixes levels
    2-4; the deployment probe supplies installed versions; execution
    levels (6-7) are set by the caller ONLY on real run evidence (a
    literature review never sets them)."""
    rows = []
    for t in stage.get("baseline", {}).get("tools") or []:
        name = t["name"]
        if str(name).startswith("NeoAG internal"):
            rows.append({"tool": name, "level": None,
                         "level_label": "internal NeoAG code (no "
                                        "external tool)",
                         "declared_pin": None, "installed": None,
                         "evidence": t.get("evidence") or []})
            continue
        declared = None
        pin_files = _pins_for_tool(pins, name)
        for p in pin_files:
            if p.get("version"):
                declared = f"{name}={p['version']} ({p['file']}, " \
                           f"channels: {','.join(p['channels']) or '-'})"
                break
        if declared is None and pin_files:
            declared = f"declared unpinned in {pin_files[0]['file']}"
        installed = env_probe_results.get(name.lower())
        rows.append({
            "tool": name,
            "level": 4 if stage.get("enabled") else 3,
            "level_label": (LEVELS[4] if stage.get("enabled")
                            else LEVELS[3]),
            "role": t.get("role"),
            "declared_pin": declared,
            "pin_files": [p["file"] for p in pin_files],
            "installed": ({"version": installed.get("installed_version"),
                           "evidence": installed.get("evidence"),
                           "also": installed.get("also"),
                           "reason": installed.get("reason")}
                          if installed is not None else None),
            "evidence": t.get("evidence") or [],
        })
    return {"tools": rows}


def consensus_context(stage: dict, ran: dict | None = None) -> dict:
    """Intended vs achieved consensus, kept as separate fields
    (config never implies execution)."""
    grp = stage.get("consensus")
    out = {"intended": None, "achieved": None, "note": ""}
    ev = " ".join(str(e) for t in
                  (stage.get("baseline", {}).get("tools") or [])
                  for e in (t.get("evidence") or []))
    m = re.search(r"min_successful=(\d+)",
                  " ".join(t.get("role") or "" for t in
                           (stage.get("baseline", {}).get("tools")
                            or [])))
    n = len([t for t in
             (stage.get("baseline", {}).get("tools") or [])
             if "required_tool_groups" in
             ((t.get("role") or "") +
              " ".join(str(e) for e in (t.get("evidence") or [])))])
    if m and n:
        out["intended"] = {"min_successful": int(m.group(1)),
                           "declared_tools": n,
                           "evidence": "workflow config "
                                       "required_tool_groups"}
    if ran is not None:
        out["achieved"] = ran
        if out["intended"]:
            ok = sum(1 for v in (ran or {}).values() if v)
            out["achieved"] = {
                "tools_ran": [k for k, v in ran.items() if v],
                "meets_min_successful":
                    ok >= out["intended"]["min_successful"]}
    else:
        out["note"] = ("this review executed nothing; achieved "
                       "consensus is a RUN fact and no run was "
                       "reviewed here")
    return out
