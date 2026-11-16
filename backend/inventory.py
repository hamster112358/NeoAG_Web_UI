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


def load_inventory(force: bool = False) -> dict:
    """Parse the configured repo; deterministic per repo content."""
    repo = Path(NEOAG_REPO)
    toml_path = repo / TOML_REL
    stages, limitations = [], []
    code_verified = toml_path.is_file()
    if code_verified:
        try:
            data = tomllib.loads(toml_path.read_text())
            for sid, sdef in (data.get("stages") or {}).items():
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
    return {
        "captured_at": now(),
        "repo": str(repo),
        "repo_readable": repo.is_dir(),
        "config_file": str(toml_path) if code_verified else None,
        "git_commit": _read_git_commit(repo) if repo.is_dir() else None,
        "code_verified": code_verified,
        "stages": stages,
        "limitations": limitations + [
            "inventory reads configuration files only - actual deployed "
            "tool versions come from the tools manifest / environment "
            "and are NOT verified here",
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
