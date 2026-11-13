"""Scientifically/operationally validated policy rules, kept SEPARATE from
anything the agent suggests.

Each rule is quoted from the real NeoAG repository documentation or gateway
implementation (read-only inspection 2026-11-13). The prototype renders these
with a "validated policy" badge; agent-generated text gets an "agent
suggestion" badge. Never merge the two channels.
"""
from __future__ import annotations

POLICY_VERSION = "2026-11-13-r2"

POLICIES = [
    {
        "id": "POL-001",
        "text": ("Production ranking requires both NetMHCstabpan stability and "
                 "NetChop 3.1d cleavage evidence; missing results block "
                 "production release rather than being silently skipped."),
        "source": "README.md / docs/PRODUCTION_REQUIRED_TOOLS.md",
    },
    {
        "id": "POL-002",
        "text": ("The pipeline produces computational triage and validation-"
                 "planning outputs. It does not make clinical diagnoses, "
                 "clinical resistance calls, or validated treatment "
                 "recommendations."),
        "source": "README.md 'Important boundary'",
    },
    {
        "id": "POL-003",
        "text": ("Missing or single-tool evidence must be reported as partial "
                 "evidence, never as a negative biological result."),
        "source": "README.md Skill3",
    },
    {
        "id": "POL-004",
        "text": ("A production fusion peptide must cross the fusion breakpoint "
                 "or contain fusion-derived novel amino acids and retain caller, "
                 "frame, junction-read and transcript-expression evidence; the "
                 "splice rule is analogous for exon junctions."),
        "source": "docs/PRODUCTION_WORKFLOW.md section 3",
    },
    {
        "id": "POL-005",
        "text": ("HIGH-risk requests (execute/resume on real inputs, overwrite, "
                 "force, hpc_submit, install-mode) require an explicit human "
                 "approval (approved=true); without it the gateway answers "
                 "APPROVAL_REQUIRED and does nothing."),
        "source": "src/neoag/controlled_execution/gateway.py _risk/_approval_required",
    },
    {
        "id": "POL-006",
        "text": ("Write/output paths must be inside gateway allowed roots; "
                 "install and delete actions are refused outright by the "
                 "gateway (UNSUPPORTED_HIGH_RISK_ACTION)."),
        "source": "src/neoag/controlled_execution/gateway.py _validate_request",
    },
    {
        "id": "POL-007",
        "text": ("Dry-run is the default; --execute runs missing stages and "
                 "reuses stages whose declared outputs exist; --force only when "
                 "a completed stage must be recomputed."),
        "source": "docs/PRODUCTION_WORKFLOW.md section 4",
    },
    {
        "id": "POL-008",
        "text": ("HLA typing must finish before peptide-HLA prediction; "
                 "purity, CNV, HLA LOH and RNA evidence may run in parallel and "
                 "are merged during ranking."),
        "source": "docs/PRODUCTION_WORKFLOW.md section 2",
    },
]
POLICY_BY_ID = {p["id"]: p for p in POLICIES}
