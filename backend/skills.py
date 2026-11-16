"""Agent Skills (PART C): editable ADVISORY instructions with version
history, deployed to the CodeBuddy-native skills directory of the agent
workspace.

Key invariants enforced here:
 - Skills are advisory text for the agent only. Enforcement (the
   PreToolUse gate, single-use grants, policies.py rules, thresholds)
   lives elsewhere and does NOT read skills. Editing or deleting a
   skill can therefore never weaken a protection (C4); the editor
   cannot touch any file outside the skills directory, and reserved
   protected names are refused.
 - Deployment writes ~/.codebuddy/skills/<slug>/SKILL.md inside the
   agent workspace (the native project-skills location CodeBuddy jobs
   load when a NEW session starts). A live agent session never picks
   up mid-session changes - deploy() therefore reports the activation
   state instead of claiming instant effect (C2).
 - The initial skill set reuses the instruction content already
   deployed in the workspace (neoag-operations) rather than
   duplicating it, plus one Web-awareness and one scientific-evidence
   skill (C3).
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from .store import now

AGENT_WS = "/data1/neoag/agents/neoag_agent_ws"
# NEOAG_SKILLS_DIR lets verification batteries deploy into a throwaway
# directory instead of the live agent workspace (test isolation); the
# live server never sets it, so its skills still land in the workspace.
import os as _os
SKILLS_DIR = _os.environ.get("NEOAG_SKILLS_DIR",
                             AGENT_WS + "/.codebuddy/skills")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")
# Names that could collide with enforcement or workspace machinery:
PROTECTED_NAMES = {"gate", "gate.py", "post", "post.py", "settings",
                   "settings.json", "codebuddy", ".codebuddy",
                   "codebuddy.md", "neoag-approvals"}
CATEGORIES = ["NeoAG Operation", "NeoAG Web Awareness",
              "Scientific Evidence & Research"]

WEB_AWARENESS = """---
name: neoag-web-awareness
description: How to query the NeoAG Web backend authoritatively (formal tasks, artifacts, decisions, snapshots, research reports) instead of guessing from chat memory. Use for ANY question about tasks/analyses/reports this interface has run.
---

# NeoAG Web awareness

You are the agent inside the NeoAG Web interface. The interface's
persisted records - not your conversation memory - are authoritative
for what has run and what it produced.

## Task query API (official; read-only)
A helper is deployed at `.neoag/taskquery.py` (inside your workspace):
- `python3 .neoag/taskquery.py list [n]` - recent formal tasks
  (id, kind, state, outcome, sample, conv link, timestamps)
- `python3 .neoag/taskquery.py get <job_id>` - full detail: effective
  config, decision history with applied effects, artifact manifest
  with availability statuses, failure info
- `python3 .neoag/taskquery.py snapshot <job_id>` - the immutable
  per-run configuration snapshot
- `python3 .neoag/taskquery.py research [n]` /
  `research get <id>` - discovery/research reports and their coverage
It authenticates with the workspace task token and only performs GET
requests. Use it whenever asked about runs; never answer from chat
memory alone and never query the database directly.

## Resolving "the analysis I just ran"
Match candidate tasks by conversation link first, then recency, then
sample id. If MORE THAN ONE task plausibly matches, ask the user ONE
focused question naming the candidates (ids + sample + state + time).
Never silently assume the most recent run is the one they mean.

## Honesty about interface state
A completed task may have outcome delivered / zero_candidates /
partial / generation_failed / no_artifacts - report exactly what the
manifest says, including empty tables, and never describe an artifact
as delivered when its status is not 'available'.
"""

EVIDENCE = """---
name: scientific-evidence-research
description: Rules for tool/literature research in this interface - evidence-level separation, verifiable citations, license/version verification, no cross-comparable-metric ranking, honest coverage. Use for ANY SOTA/tool/benchmark/literature question.
---

# Scientific evidence & research discipline

Research must come from actual retrieval (the interface's live
Discovery engine or your own read-only fetches), never from memory of
plausible-sounding numbers.

## Evidence levels - always separate, never blend
1. author-reported: the tool's own paper claims.
2. independent published: a DIFFERENT group's benchmark/comparison -
   cite it explicitly, and note whether you saw the full text or only
   an abstract.
3. our own experiments: only numbers actually run in this deployment
   with a recorded run directory. If none exist, say so.

## Hard rules
- Never rank tools by comparing AUCs from different datasets, label
  definitions or peptide sets. If metrics are incomparable, present
  them side-by-side and say they are not rankable.
- Quote metric values only with their benchmark task, dataset and
  evaluation labels attached; if a paper's abstract does not state
  them, write "metric context not verified" rather than guessing.
- Verify existence/version/license from primary sources (repository,
  package index, official docs); anything unverifiable is marked
  "Not verified", never filled with a plausible estimate.
- Every citation needs a resolvable identifier (DOI/PMID/arXiv/URL)
  plus the source you actually retrieved it from and the date.
- A report must state what was searched, what failed, and its
  limitations; never promise exhaustiveness.
- No execution of candidate tools without an approved run through
  the normal job-control pathway; literature review and independent
  benchmarking are different claims and must not be merged.
"""

OPERATIONS_FALLBACK = """---
name: neoag-operations
description: Operational playbook for NeoAG neoantigen pipeline work on this server - interpreting analysis requests, locating inputs, selecting documented defaults, running/diagnosing stages, recording decisions and provenance, and handling destructive operations. Use for ANY NeoAG/pipeline/neoantigen task.
---

# NeoAG operations playbook

(Seeded copy - the Skills editor is now the authoritative source for
this playbook; the operator can edit it here.)

## Interpreting an analysis request
Identify the case, locate and sanity-check inputs, build a short
input manifest (role, path, confidence, basis), state the plan with
documented defaults and expected outputs. Ask ONE focused question
for genuinely ambiguous or missing essential inputs.

## Running and diagnosing
Prefer the documented CLI of the configured repo; read README and
configs/workflows/*.toml rather than guessing flags. On failure read
the stage log tail first, apply at most one concrete fix and one
retry, then report exact errors.

## Decisions and provenance
Every scientifically consequential choice gets a decisions.jsonl row
(ts, decision, options, chosen, rationale, evidence, policy_version)
in the run directory; keep commands.sh so runs are reconstructable.
"""


def validate_skill(slug: str, content: str, title: str = "") -> list[str]:
    """Editor-side validation. Returns a list of human-readable errors
    (empty list = acceptable)."""
    errs: list[str] = []
    if not SLUG_RE.match(slug or ""):
        errs.append("slug must be 3-61 chars, lowercase letters/digits/"
                    "dashes, starting with a letter")
    if slug in PROTECTED_NAMES or (slug or "").startswith("."):
        errs.append(f"'{slug}' collides with a protected workspace name "
                    "- skill refused")
    if not (content or "").strip():
        errs.append("skill content is empty")
    body = (content or "").lstrip()
    if not body.startswith("---"):
        errs.append("content must start with '---' YAML frontmatter "
                    "(name/description)")
    else:
        fm = body.split("---", 2)
        if len(fm) < 3:
            errs.append("frontmatter is not closed by a second '---'")
        else:
            if not re.search(r"^name:\s*\S+", fm[1], re.M):
                errs.append("frontmatter needs a 'name:' field")
    if len(content or "") > 60_000:
        errs.append("skill content too large (>60k chars)")
    return errs


def render_check(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def ensure_seed_skills(store) -> list[str]:
    """Create the initial skill set ONCE (empty table only). Reuses the
    existing native playbook file for neoag-operations if present."""
    if store.skill_list():
        return []
    created = []
    ops_content = OPERATIONS_FALLBACK
    ops_source = "built-in fallback summary"
    try:
        f = SKILLS_DIR + "/neoag-operations/SKILL.md"
        import pathlib
        existing = pathlib.Path(f).read_text()
        if existing.strip():
            ops_content = existing
            ops_source = "imported from the native workspace skill file (existing instructions reused)"
    except Exception:
        pass
    for slug, title, cat, content, src in (
            ("neoag-operations", "NeoAG operations playbook",
             CATEGORIES[0], ops_content, ops_source),
            ("neoag-web-awareness", "NeoAG Web task awareness",
             CATEGORIES[1], WEB_AWARENESS,
             "seeded with the interface's official task-query contract"),
            ("scientific-evidence-research",
             "Scientific evidence & research discipline",
             CATEGORIES[2], EVIDENCE,
             "seeded with the project's evidence-level rules")):
        store.skill_create(slug, slug, title, cat, content, source=src)
        created.append(slug)
    if created:
        store.audit("system", "skills.seed", "-", {"created": created})
    return created


def deploy(store, slug: str | None = None) -> dict:
    """Write enabled skills to the native skills directory. Honest
    activation reporting: writes are real; effect applies to NEW agent
    sessions, and per-skill deployed state is recorded and returned."""
    import os
    import pathlib
    skills = [s for s in store.skill_list() if slug in (None, s["slug"])]
    if slug and not skills:
        return {"ok": False, "error": "no such skill"}
    results = []
    for s in skills:
        if not s["enabled"]:
            results.append({"slug": s["slug"], "state": "disabled - not "
                            "deployed", "version": s["version"]})
            continue
        errs = validate_skill(s["slug"], s["content"], s["title"])
        if errs:
            results.append({"slug": s["slug"], "state": "rejected",
                            "errors": errs})
            continue
        try:
            d = pathlib.Path(SKILLS_DIR) / s["slug"]
            d.mkdir(parents=True, exist_ok=True)
            p = d / "SKILL.md"
            p.write_text(s["content"])
            p.chmod(0o644)
            digest = render_check(s["content"])
            store.skill_set_deployed(s["id"], s["version"], digest)
            results.append({"slug": s["slug"], "version": s["version"],
                            "state": "written", "path": str(p),
                            "hash": digest[:12]})
        except Exception as e:
            results.append({"slug": s["slug"], "state": "write failed",
                            "error": str(e)[:200]})
    store.audit("user", "skills.deploy", slug or "all",
                {"results": [r.get("state") for r in results]})
    return {
        "ok": True, "results": results,
        "activation_note": ("Files written are picked up by NEW CodeBuddy "
                            "job sessions in the agent workspace. A live "
                            "agent session keeps whatever it loaded at "
                            "dispatch - start a new chat turn after the "
                            "session ends, or ask the agent to confirm "
                            "which skills it loaded."),
    }


def activation_state(store) -> list[dict]:
    """Per-skill honesty: is the CURRENT version actually deployed?"""
    out = []
    for s in store.skill_list():
        deployed_ok = (s["deployed_version"] == s["version"] and
                       s["deployed_hash"] == render_check(s["content"]))
        out.append({"id": s["id"], "slug": s["slug"], "name": s["name"],
                    "title": s["title"], "category": s["category"],
                    "version": s["version"], "enabled": bool(s["enabled"]),
                    "deployed_version": s["deployed_version"],
                    "deployed_at": s["deployed_at"],
                    "active_in_new_sessions": bool(s["enabled"]) and
                    deployed_ok})
    return out
