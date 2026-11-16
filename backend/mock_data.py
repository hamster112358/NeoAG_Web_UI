"""Synthetic demo data for the NeoAG web UI prototype.

EVERYTHING in this module is fabricated for UI demonstration:
 - sample IDs start with DEMO-;
 - "files" are names/metadata only, no real data lives behind them;
 - numeric "predictions" are generated from a seeded RNG, not from any model.
No patient data is referenced anywhere in this project.
"""
from __future__ import annotations

import random

DEMO_DATASET_VERSION = "mock-2026.11"

# Stage list mirrors configs/workflows/production_workflow.example.toml
# ([stages.*] keys) plus the unified presentation/ranking/report phases
# documented in docs/PRODUCTION_WORKFLOW.md and README.md ("What It Does").
STAGES = [
    {"id": "input_qc", "name": "Input QC & routing", "required": True,
     "note": "input inventory, missing/ambiguous checks, route plan"},
    {"id": "hla_typing", "name": "HLA typing consensus", "required": True,
     "note": "OptiType + SpecHLA/HLA-LA cross-check; must finish before peptide-HLA"},
    {"id": "purity_cnv", "name": "Purity / CNV / CCF", "required": True,
     "note": "FACETS + Sequenza/ASCAT/PURPLE cross-check"},
    {"id": "hla_loh", "name": "HLA LOH", "required": True,
     "note": "LOHHLA + SpecHLA; single-tool results stay explicit"},
    {"id": "rna_expression", "name": "RNA expression", "required": True,
     "note": "Salmon TPM (RSEM cross-check)"},
    {"id": "snv", "name": "SNV/InDel peptides", "required": True,
     "note": "VEP + pVACseq / sliding-window -> raw_events/raw_peptides"},
    {"id": "fusion_discovery", "name": "Fusion discovery", "required": False,
     "note": "EasyFuse/Arriba/STAR-Fusion; optional, LOW_CONFIDENCE on failure"},
    {"id": "fusion_peptides", "name": "Fusion peptides", "required": False,
     "note": "breakpoint-crossing review -> pVACfuse"},
    {"id": "splice_discovery", "name": "Aberrant splice discovery", "required": False,
     "note": "RegTools + pVACsplice; optional SNAF cross-check"},
    {"id": "splice_peptides", "name": "Splice peptides", "required": False,
     "note": "junction-crossing peptides with junction support"},
    {"id": "presentation", "name": "Presentation & APPM", "required": True,
     "note": "NetMHCpan, MHCflurry, NetMHCstabpan stability, NetChop cleavage"},
    {"id": "safety_escape", "name": "Safety & immune escape", "required": True,
     "note": "normal proteome/expression/ligandome + HLA-LOH/APM escape"},
    {"id": "ranking", "name": "Ranking", "required": True,
     "note": "weighted + evidence-consensus ranking"},
    {"id": "reports", "name": "Reports", "required": True,
     "note": "patient / technical / onepage reports via open-neo review"},
]
STAGE_BY_ID = {s["id"]: s for s in STAGES}

SAMPLES = [
    {
        "id": "DEMO-P001", "label": "Demo case P001 (synthetic WGS+RNA)",
        "assay": "WGS + tumour RNA", "genome_build": "GRCh38",
        "status": "inputs complete (mock)",
        "files": [
            {"path": "DEMO-P001/tumor.dna.bam", "kind": "BAM", "size": "41.2 GiB (mock)"},
            {"path": "DEMO-P001/normal.dna.bam", "kind": "BAM", "size": "39.8 GiB (mock)"},
            {"path": "DEMO-P001/tumor.rna_R1.fastq.gz", "kind": "FASTQ", "size": "6.1 GiB (mock)"},
            {"path": "DEMO-P001/tumor.rna_R2.fastq.gz", "kind": "FASTQ", "size": "5.9 GiB (mock)"},
            {"path": "DEMO-P001/somatic.pass.vcf.gz", "kind": "VCF", "size": "84 MiB (mock)"},
        ],
    },
    {
        "id": "DEMO-P002", "label": "Demo case P002 (synthetic WES only)",
        "assay": "WES, no RNA", "genome_build": "GRCh38",
        "status": "RNA inputs missing (mock) - fusion/splice stages would be blocked",
        "files": [
            {"path": "DEMO-P002/tumor.dna.bam", "kind": "BAM", "size": "9.7 GiB (mock)"},
            {"path": "DEMO-P002/normal.dna.bam", "kind": "BAM", "size": "9.4 GiB (mock)"},
            {"path": "DEMO-P002/somatic.pass.vcf.gz", "kind": "VCF", "size": "21 MiB (mock)"},
        ],
    },
    {
        "id": "DEMO-P003", "label": "Demo case P003 (paused mid-run, mock)",
        "assay": "WGS + tumour RNA", "genome_build": "GRCh38",
        "status": "historical mock run, stages through rna_expression present",
        "files": [
            {"path": "DEMO-P003/tumor.dna.bam", "kind": "BAM", "size": "44.0 GiB (mock)"},
            {"path": "DEMO-P003/normal.dna.bam", "kind": "BAM", "size": "43.1 GiB (mock)"},
            {"path": "DEMO-P003/result/rna/expression/tpm.tsv", "kind": "result", "size": "1.3 MiB (mock)"},
        ],
    },
]
SAMPLE_IDS = [s["id"] for s in SAMPLES]

# Synthetic gene symbols - deliberately not real genes, to make mock status obvious.
GENES = ["DEMOGA", "DEMOGB", "DEMOGC", "DEMOGD", "DEMOGE", "DEMOGF",
         "DEMOGG", "DEMOGH", "DEMOGJ", "DEMOGK"]
PEPTIDE_POOL = [
    "DEMOPEPTIDE", "GAXTRAVMTL", "DEMOQQNKLL", "BQFNSEVQFI", "DEMOGHIJKLM",
    "CVPRTDLWEI", "DEMOLMNPQRS", "DLKQSTFEV", "DEMOAETRTLY", "EQNVFSDGHL",
    "DEMOTVWXYZA", "FLARGHWQIN", "DEMOACDEFGHI", "GRSHITKJWM", "HDSLKJQWER",
]
HLA = ["HLA-A*02:01", "HLA-A*24:02", "HLA-B*07:02", "HLA-B*40:01", "HLA-C*07:02"]
BRANCHES = ["snv", "snv", "snv", "fusion", "splice"]  # branch mix for demo rows


# ---------------------------------------------------------------------------
# Ranking generation (deterministic, rule-based, seeded per job).
#
# The ranking decision made during a run (A2) must ACTUALLY change the
# outputs, so generation is split into explicit, rule-driven layers:
#   make_pool()       - the scored candidate pool (one deterministic draw)
#   apply_gate()      - presentation-stability gate (decision: gate14/gate20)
#   weighted_rank()   - weighted composite score table
#   consensus_rank()  - evidence-consensus table (rule-count based, can be
#                       legitimately EMPTY when no candidate meets the bar)
# make_rankings() assembles the tables the run's ranking choice asked for.
# Everything is computed by these rules from the seeded draw - no table is
# fabricated independently of them, and a zero-row result is reported as
# zero-row, not silently replaced.
# ---------------------------------------------------------------------------

CONSENSUS_RULES = [
    ("el_rank<=1.0", lambda r: r["pred_el_rank_pct"] <= 1.0),
    ("stability<=gate", None),            # gate threshold injected per run
    ("cleavage>=0.5", lambda r: r["pred_cleavage_score"] >= 0.5),
    ("immunogenicity>=0.5", lambda r: r["pred_immunogenicity"] >= 0.5),
    ("no_normal_hits", lambda r: r["safety_normal_hits"] == 0),
    ("rna_tpm>=1", lambda r: r["rna_expr_tpm_mock"] >= 1.0),
    ("ccf>=50", lambda r: r["pred_ccf_pct"] >= 50),
]
CONSENSUS_TIER = 5   # a candidate joins the consensus table at >=5/7 rules


def make_pool(job_id: str, sample_id: str, top_n: int = 12) -> list[dict]:
    """Deterministic synthetic candidate pool (seeded by job_id+sample)."""
    rng = random.Random(f"{job_id}:{sample_id}:{DEMO_DATASET_VERSION}")
    rows = []
    for i in range(top_n):
        el_rank = round(rng.uniform(0.05, 9.9), 2)
        # stability ranks are drawn so that roughly the better half of a
        # candidate list can pass the 1.4 production gate (mock realism;
        # a fully uniform 1.0-5.8 draw would make every gate a coin flip)
        stab = round(rng.uniform(1.0, 2.6) if i % 2 == 0
                     else rng.uniform(1.0, 5.8), 2)
        netchop = round(rng.uniform(0.01, 0.98), 2)
        immuno = round(rng.uniform(0.02, 0.95), 2)
        safety_hits = rng.choice([0, 0, 0, 0, 1, 2])
        expr_tpm = round(rng.uniform(0.4, 260.0), 1)
        ccf = round(rng.uniform(18.0, 99.0), 0)
        rows.append({
            "rank": i + 1,
            "peptide": PEPTIDE_POOL[(i + rng.randrange(0, 3)) % len(PEPTIDE_POOL)],
            "gene": GENES[rng.randrange(len(GENES))],
            "branch": BRANCHES[rng.randrange(len(BRANCHES))],
            "hla": HLA[rng.randrange(len(HLA))],
            "pred_el_rank_pct": el_rank,
            "pred_stability_rank": stab,
            "pred_cleavage_score": netchop,
            "pred_immunogenicity": immuno,
            "safety_normal_hits": safety_hits,
            "rna_expr_tpm_mock": expr_tpm,
            "pred_ccf_pct": ccf,
        })
    for r in rows:
        r["mock_combined_score"] = round(
            (100 - r["pred_el_rank_pct"]) * 0.45
            + max(0.0, (4.0 - r["pred_stability_rank"])) * 10
            + r["pred_cleavage_score"] * 15
            + r["pred_immunogenicity"] * 20
            - r["safety_normal_hits"] * 25
            + min(r["rna_expr_tpm_mock"], 50) * 0.2, 1)
        r["consensus_score"] = 0
        r["consensus_rules_met"] = []
        r["gate_pass"] = True
    return rows


def apply_gate(rows: list[dict], gate_rank: float) -> list[dict]:
    """Presentation stability gate (the run's presentation decision chose
    the threshold). Marks, but does not delete, below-gate rows so the
    table stays inspectable; consensus counting uses the same threshold."""
    for r in rows:
        r["gate_pass"] = bool(r["pred_stability_rank"] <= gate_rank)
    return rows


def weighted_rank(pool: list[dict]) -> tuple[list[dict], int]:
    """Weighted composite DELIVERABLE table: ordered by the mock combined
    score and filtered by the run's chosen stability gate - the gate
    decision therefore genuinely changes this artifact's contents (rows
    below the gate are excluded and counted, never silently dropped)."""
    ordered = sorted(pool, key=lambda r: (-r["mock_combined_score"],
                                          r["peptide"]))
    rows = [r for r in ordered if r["gate_pass"]]
    below_gate = len(ordered) - len(rows)
    out = []
    for i, r in enumerate(rows):
        c = dict(r)
        c["rank"] = i + 1
        c["ranking_strategy"] = "weighted"
        flags = []
        if c["safety_normal_hits"]:
            flags.append(f"{c['safety_normal_hits']} normal-proteome hit(s) (mock)")
        if c["rna_expr_tpm_mock"] < 1.0:
            flags.append("low expression (mock)")
        c["evidence_summary"] = (
            f"Mock evidence: el%rank {c['pred_el_rank_pct']}, stability rank "
            f"{c['pred_stability_rank']}, cleavage {c['pred_cleavage_score']}, "
            f"immunogenicity {c['pred_immunogenicity']}, RNA TPM "
            f"{c['rna_expr_tpm_mock']}, CCF {int(c['pred_ccf_pct'])}%."
            + (" Watch-outs: " + "; ".join(flags) if flags
               else " No mock watch-outs."))
        out.append(c)
    return out, below_gate


def consensus_rank(pool: list[dict], gate_rank: float) -> list[dict]:
    """Evidence-consensus table: rows meeting >= CONSENSUS_TIER of the
    seven documented rules, ordered by rules met then el%rank. The rule
    check is the SAME for every candidate, so this table is genuinely a
    different (deterministic) ordering than the weighted one - and can
    legitimately be empty."""
    rules = [(name, (lambda r, g=gate_rank: r["pred_stability_rank"] <= g)
              if fn is None else fn) for name, fn in CONSENSUS_RULES]
    scored = []
    for r in pool:
        met = [name for name, fn in rules if fn(r)]
        scored.append((len(met), -r["pred_el_rank_pct"], r["peptide"], met, r))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    out = []
    for n, (_, _, _, met, r) in enumerate(s for s in scored
                                          if s[0] >= CONSENSUS_TIER):
        c = dict(r)
        c["rank"] = n + 1
        c["ranking_strategy"] = "evidence-consensus"
        c["consensus_score"] = f"{len(met)}/{len(rules)}"
        c["consensus_rules_met"] = met
        out.append(c)
    return out


def make_rankings(job_id: str, sample_id: str, top_n: int,
                  gate_rank: float, want: list[str]
                  ) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    """Assemble exactly the ranking tables the run's ranking decision
    asked for ('weighted' and/or 'evidence-consensus'), gated by the
    run's presentation decision. Returns (tables, meta): an empty table
    is a real result and must be reported as such, never replaced or
    hidden. meta records rule side-effects (e.g. gate exclusions)."""
    pool = apply_gate(make_pool(job_id, sample_id, top_n), gate_rank)
    tables: dict[str, list[dict]] = {}
    meta: dict[str, dict] = {}
    if "weighted" in want:
        wt, below = weighted_rank(pool)
        tables["weighted"] = wt
        meta["weighted"] = {"gate_rank": gate_rank,
                            "below_gate_excluded": below,
                            "pool_size": len(pool)}
    if "evidence-consensus" in want:
        cs = consensus_rank(pool, gate_rank)
        tables["evidence-consensus"] = cs
        meta["evidence-consensus"] = {
            "gate_rank": gate_rank, "pool_size": len(pool),
            "tier": f">={CONSENSUS_TIER}/{len(CONSENSUS_RULES)} rules"}
    return tables, meta
