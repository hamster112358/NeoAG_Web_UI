"""Code-grounded inventory of REAL NeoAG configuration and execution
options, extracted from the frozen NeoAG repository (read-only):

  /data2/caojingqi/original_OpenNeoAG/neoag/src/neoag/open_neo/cli.py
  /data2/caojingqi/original_OpenNeoAG/neoag/configs/workflows/production_workflow.example.toml
  /data2/caojingqi/original_OpenNeoAG/neoag/src/neoag/controlled_execution/gateway.py
  /data2/caojingqi/original_OpenNeoAG/neoag/docs/PRODUCTION_WORKFLOW.md

Nothing here is invented: every row cites the code location it came from.
The demo UI renders this inventory; the demo job engine still simulates
execution - the inventory describes what WOULD be configurable in a
real run (POL-002 separation).
"""
from __future__ import annotations

RUN_CLI = "src/neoag/open_neo/cli.py"
TOML = "configs/workflows/production_workflow.example.toml"
GW = "src/neoag/controlled_execution/gateway.py"

PARAM_GROUPS = [
    {
        "group": "Run basics",
        "expert": False,
        "items": [
            {"id": "sample_id", "label": "Sample ID", "type": "string",
             "default": "SAMPLE001 (workflow TOML)", "required": True,
             "desc": "Patient/case identifier threaded through every stage and report.",
             "source": f"{TOML} [run]"},
            {"id": "outdir", "label": "Output directory", "type": "path",
             "default": "results/<SAMPLE>_production", "required": True,
             "desc": "Where stage outputs, evidence and reports land.",
             "source": f"{RUN_CLI} _add_common_output"},
            {"id": "mode", "label": "Run mode", "type": "choice",
             "values": ["plan", "dry-run", "execute", "resume", "ranking-only"],
             "default": "dry-run first (POL-007: plan before execute)",
             "desc": "plan/dry-run compute without side effects; execute writes; resume continues a previous run; ranking-only recomputes final tables.",
             "source": f"{RUN_CLI} run.add_argument('--mode')"},
            {"id": "approved", "label": "Execute approval", "type": "flag",
             "default": False,
             "desc": "HIGH-risk requests (execute/overwrite/hpc_submit) answer 403 APPROVAL_REQUIRED without approved=true - a human gate, never a UI default.",
             "source": f"{GW} _risk()/execute_approval_required lines 174-279"},
            {"id": "case_id", "label": "Case ID", "type": "string", "default": None,
             "desc": "Groups jobs that belong to one clinical case.",
             "source": f"{RUN_CLI} _add_common_output"},
            {"id": "allow_partial", "label": "Allow partial run", "type": "flag",
             "default": False,
             "desc": "Continue when non-required stages fail instead of aborting the run.",
             "source": f"{RUN_CLI} run.add_argument('--allow-partial')"},
            {"id": "timeout", "label": "Run timeout", "type": "int(seconds)",
             "default": 7200,
             "desc": "Hard wall-clock limit per run.",
             "source": f"{RUN_CLI} run.add_argument('--timeout')"},
        ],
    },
    {
        "group": "Input data (DNA / RNA)",
        "expert": False,
        "items": [
            {"id": "input_dir", "label": "Input directory", "type": "path",
             "default": None,
             "desc": "Root the input detector scans for sequencing files; users describe the location, the agent proposes the manifest.",
             "source": f"{RUN_CLI} run.add_argument('--input-dir')"},
            {"id": "tumor_dna_bam", "label": "Tumor DNA BAM", "type": "path",
             "default": "auto-detected from input_dir",
             "desc": "Somatic SNV branch input.",
             "source": f"{RUN_CLI} run.add_argument('--tumor-dna-bam')"},
            {"id": "normal_dna_bam", "label": "Matched normal DNA BAM", "type": "path",
             "default": "auto-detected",
             "desc": "Required for somatic calling; absence forces review.",
             "source": f"{RUN_CLI} run.add_argument('--normal-dna-bam')"},
            {"id": "tumor_rna_bam", "label": "Tumor RNA BAM", "type": "path",
             "default": "auto-detected",
             "desc": "Feeds fusion and splice discovery branches.",
             "source": f"{RUN_CLI} run.add_argument('--tumor-rna-bam')"},
            {"id": "assay_type", "label": "Assay type", "type": "choice",
             "values": ["WGS", "WES", "PANEL", "CAPTURE", "RNA"], "default": None,
             "desc": "Changes detector thresholds and peptide-pulling windows.",
             "source": f"{RUN_CLI} run.add_argument('--assay-type')"},
            {"id": "somatic_vcf", "label": "Somatic VCF", "type": "path",
             "default": "pipeline-generated",
             "desc": "Pre-computed variant calls can replace the SNV branch.",
             "source": f"{RUN_CLI} run.add_argument('--somatic-vcf')"},
        ],
    },
    {
        "group": "Precomputed evidence overrides",
        "expert": True,
        "items": [
            {"id": "fusion_tsv", "label": "Fusion calls TSV", "type": "path",
             "desc": "Skip fusion discovery when calls already exist.",
             "source": f"{RUN_CLI} run.add_argument('--fusion-tsv')"},
            {"id": "splice_junction_tsv", "label": "Splice junction TSV", "type": "path",
             "desc": "Skip splice discovery when junctions already exist.",
             "source": f"{RUN_CLI} run.add_argument('--splice-junction-tsv')"},
            {"id": "hla_file", "label": "HLA alleles file", "type": "path",
             "desc": "Bypass HLA typing with a consensus file.",
             "source": f"{RUN_CLI} run.add_argument('--hla-file')"},
            {"id": "expression_tsv", "label": "Gene expression TSV", "type": "path",
             "desc": "Skip RNA quantification with precomputed TPM.",
             "source": f"{RUN_CLI} run.add_argument('--expression-tsv')"},
            {"id": "purity_tsv", "label": "Purity TSV", "type": "path",
             "desc": "Supply tumor purity instead of running FACETS/Sequenza/PURPLE.",
             "source": f"{RUN_CLI} run.add_argument('--purity-tsv')"},
            {"id": "cnv_tsv", "label": "CNV segments TSV", "type": "path",
             "desc": "Supply copy-number segments.",
             "source": f"{RUN_CLI} run.add_argument('--cnv-tsv')"},
            {"id": "hla_loh_tsv", "label": "HLA-LOH TSV", "type": "path",
             "desc": "Supply loss-of-heterozygosity evidence.",
             "source": f"{RUN_CLI} run.add_argument('--hla-loh-tsv')"},
            {"id": "peptide_csv", "label": "Peptide CSV", "type": "path",
             "desc": "Direct peptide input for re-ranking.",
             "source": f"{RUN_CLI} run.add_argument('--peptide-csv')"},
        ],
    },
    {
        "group": "RNA branch routing",
        "expert": True,
        "items": [
            {"id": "rna_quant_method", "label": "RNA quant method", "type": "choice",
             "values": ["auto", "salmon", "rsem"], "default": "auto",
             "desc": "Selects the transcript quantification backend.",
             "source": f"{RUN_CLI} run.add_argument('--rna-quant-method')"},
            {"id": "rna_threads", "label": "RNA threads", "type": "int",
             "default": 16, "desc": "Parallelism for quantification.",
             "source": f"{RUN_CLI} run.add_argument('--rna-threads')"},
            {"id": "easyfuse_ref", "label": "EasyFuse reference", "type": "path",
             "desc": "EasyFuse resource root; missing ref = silent empty outputs (audit trap).",
             "source": f"{RUN_CLI} run.add_argument('--easyfuse-ref')"},
            {"id": "snaf_workflow", "label": "SNAF workflow", "type": "path",
             "desc": "Splice-aware fusion alternative route.",
             "source": f"{RUN_CLI} run.add_argument('--snaf-workflow')"},
            {"id": "splicemutr_workflow", "label": "SpliceMutR workflow", "type": "path",
             "desc": "Alternative splice caller route.",
             "source": f"{RUN_CLI} run.add_argument('--splicemutr-workflow')"},
        ],
    },
    {
        "group": "SV branch",
        "expert": True,
        "items": [
            {"id": "sv_vcf", "label": "SV VCF (repeatable)", "type": "path",
             "desc": "Structural-variant calls per sample.",
             "source": f"{RUN_CLI} run.add_argument('--sv-vcf', action='append')"},
            {"id": "skip_bam_matcher", "label": "Skip BAM matcher", "type": "flag",
             "default": False, "desc": "Skip tumor/normal identity check.",
             "source": f"{RUN_CLI} run.add_argument('--skip-bam-matcher')"},
            {"id": "skip_dna_sv", "label": "Skip DNA SV branch", "type": "flag",
             "default": False, "desc": "Disable SV calling entirely.",
             "source": f"{RUN_CLI} run.add_argument('--skip-dna-sv')"},
            {"id": "sv_threads", "label": "SV threads", "type": "int",
             "default": 8, "desc": "Nextflow executor parallelism.",
             "source": f"{RUN_CLI} run.add_argument('--sv-threads')"},
            {"id": "sv_memory_gb", "label": "SV memory (GB)", "type": "float",
             "default": 48.0, "desc": "Nextflow memory request.",
             "source": f"{RUN_CLI} run.add_argument('--sv-memory-gb')"},
        ],
    },
    {
        "group": "Reference data",
        "expert": True,
        "items": [
            {"id": "reference_fasta", "label": "Reference FASTA", "type": "path",
             "desc": "Genome build anchor; must match genome_build.",
             "source": f"{RUN_CLI} run.add_argument('--reference-fasta')"},
            {"id": "gencode_gtf", "label": "GENCODE GTF", "type": "path",
             "desc": "Transcript annotation.",
             "source": f"{RUN_CLI} run.add_argument('--gencode-gtf')"},
            {"id": "vep_cache", "label": "VEP cache", "type": "path",
             "desc": "Variant effect cache; build-mismatch = silent stale calls.",
             "source": f"{RUN_CLI} run.add_argument('--vep-cache')"},
            {"id": "genome_build", "label": "Genome build", "type": "string",
             "default": "GRCh38",
             "desc": "Cache/reference consistency key.",
             "source": f"{RUN_CLI} run.add_argument('--genome-build')"},
            {"id": "netmhcpan_home", "label": "NetMHCpan home", "type": "path",
             "desc": "Licensed predictor location (license-gated).",
             "source": f"{RUN_CLI} run.add_argument('--netmhcpan-home')"},
        ],
    },
    {
        "group": "Ranking and reporting",
        "expert": False,
        "items": [
            {"id": "event_top_n", "label": "Event top-N", "type": "int",
             "default": 20,
             "desc": "Event-level rows kept in ranking outputs.",
             "source": f"{RUN_CLI} run.add_argument('--event-top-n')"},
            {"id": "candidate_top_n", "label": "Candidate top-N", "type": "int",
             "default": 100,
             "desc": "Peptide-HLA candidates kept per track.",
             "source": f"{RUN_CLI} run.add_argument('--candidate-top-n')"},
            {"id": "reports", "label": "Reports", "type": "choice",
             "values": ["patient", "technical", "onepage"],
             "default": "patient,technical",
             "desc": "Report bundles written at run end.",
             "source": f"{TOML} [run] reports"},
            {"id": "weighted_baseline", "label": "Weighted baseline", "type": "path",
             "desc": "Override ranking weights (audit-tracked; changes are provenance-recorded).",
             "source": f"{RUN_CLI} run.add_argument('--weighted-baseline')"},
            {"id": "evidence_consensus_rules", "label": "Evidence-consensus rules", "type": "path",
             "desc": "Override consensus rule file.",
             "source": f"{RUN_CLI} run.add_argument('--evidence-consensus-rules')"},
        ],
    },
    {
        "group": "Tool routing policy",
        "expert": True,
        "items": [
            {"id": "automatic_tool_policy", "label": "Automatic tool policy",
             "type": "choice", "values": ["all-available", "balanced", "minimal"],
             "default": "all-available",
             "desc": "How aggressively the router picks alternative tools when the primary is unavailable.",
             "source": f"{RUN_CLI} run.add_argument('--automatic-tool-policy')"},
            {"id": "execution_profile", "label": "Execution profile", "type": "string",
             "default": "local",
             "desc": "Site-specific executor profile.",
             "source": f"{RUN_CLI} run.add_argument('--execution-profile')"},
        ],
    },
]

STAGE_GRAPH = [
    {"id": "hla_typing", "label": "HLA typing", "required": True, "depends_on": [],
     "source": f"{TOML} [stages.hla_typing]"},
    {"id": "purity_cnv", "label": "Purity / CNV", "required": False, "depends_on": [],
     "source": f"{TOML} [stages.purity_cnv]"},
    {"id": "hla_loh", "label": "HLA-LOH", "required": False,
     "depends_on": ["hla_typing", "purity_cnv"],
     "source": f"{TOML} [stages.hla_loh]"},
    {"id": "rna_expression", "label": "RNA expression", "required": False,
     "depends_on": [], "source": f"{TOML} [stages.rna_expression]"},
    {"id": "snv", "label": "SNV calling", "required": True,
     "depends_on": ["hla_typing"], "source": f"{TOML} [stages.snv] source=pVACseq"},
    {"id": "fusion_discovery", "label": "Fusion discovery", "required": False,
     "depends_on": ["rna_expression"],
     "source": f"{TOML} [stages.fusion_discovery]"},
    {"id": "fusion_peptides", "label": "Fusion peptides", "required": False,
     "depends_on": ["hla_typing", "fusion_discovery"],
     "source": f"{TOML} [stages.fusion_peptides] source=pVACfuse"},
    {"id": "splice_discovery", "label": "Splice discovery", "required": False,
     "depends_on": ["rna_expression"],
     "source": f"{TOML} [stages.splice_discovery]"},
    {"id": "splice_peptides", "label": "Splice peptides", "required": False,
     "depends_on": ["hla_typing", "splice_discovery"],
     "source": f"{TOML} [stages.splice_peptides] source=pVACsplice"},
]

# Derived at import time from the job engine's OWN definition (jobs.py),
# so this inventory can never drift from what the engine actually asks and
# applies (Milestone-4 A4: one formalized source of truth per decision).
from .jobs import DECISION_POINTS as _RUN_DP  # noqa: E402

DECISION_POINTS = [
    {"stage": sid, "type": dp["decision_type"], "question": dp["question"],
     "policy": dp["policy_id"], "key": dp["key"],
     "mandatory": bool(dp.get("mandatory")),
     "default": next((o["label"] for o in dp["options"]
                      if o.get("policy_default")),
                     "no default - always asks"),
     "source": dp.get("source", "jobs.py DECISION_POINTS")}
    for sid, dp in _RUN_DP.items()
]

HUMAN_REQUIRED = [
    {"gate": "Execute approval",
     "rule": "Execute-class requests stay blocked until a human approves (POL-005 mirror).",
     "source": "jobs.py submit() + approvals table"},
    {"gate": "Stage failure recovery",
     "rule": "Retry / skip / terminate is a user decision in controlled mode; bounded to one auto-retry in autonomous mode.",
     "source": "jobs.py worker failure path"},
    {"gate": "Skip-essential consequence",
     "rule": "Skipping a required stage forces production_release=false with a release note.",
     "source": "jobs.py skip branch"},
]

# Which parameters ordinary users should never need to touch (expert=True
# groups above) is a design statement, not a code fact - the code exposes
# them; the UI hides them behind an advanced disclosure by default.
NEVER_TOUCH_BY_DEFAULT = [
    "tools_manifest", "reference_manifest", "asset roots",
    "nextflow configs", "container images", "predictor homes",
]

# ---------------------------------------------------------------------------
# Milestone 5 PART 2: the SAME code-grounded facts, reorganised into the
# four user-facing concerns. Concern assignment is a design statement
# (labelled as such); the parameter rows, defaults and CLI flags are the
# code facts already cited above - nothing new is invented here.
#   inputs     A: inputs & sample (agent-managed; planner auto-discovers)
#   science    B: scientific configuration, grouped by NeoAG module
#   execution  C: execution & environment settings (hidden by default)
#   managed    set by the approval/execution flow, never a user form field
CONCERN_BY_GROUP = {
    "Run basics": "execution",
    "Input data (DNA / RNA)": "inputs",
    "Precomputed evidence overrides": "inputs",
    "RNA branch routing": "science",
    "SV branch": "science",
    "Reference data": "execution",
    "Ranking and reporting": "science",
    "Tool routing policy": "execution",
}
CONCERN_BY_ITEM = {  # overrides inside mixed groups
    "sample_id": "inputs", "case_id": "inputs",
    "approved": "managed", "mode": "execution",
    "outdir": "execution", "allow_partial": "execution",
    "timeout": "execution",
    # M6 PART E: resource knobs, reference locations and file-override
    # paths are execution/environment matters, not scientific choices -
    # the science panel keeps only structured controls that demonstrably
    # change native execution (SCIENCE_CONTROLS below).
    "rna_threads": "execution", "easyfuse_ref": "execution",
    "snaf_workflow": "execution", "splicemutr_workflow": "execution",
    "sv_vcf": "inputs", "sv_threads": "execution",
    "sv_memory_gb": "execution",
    "reports": "execution", "weighted_baseline": "execution",
    "evidence_consensus_rules": "execution",
    "genome_build": "execution",  # rendered as a radio in the science
                                  # panel (SCIENCE_CONTROLS), not here
}

# ---------------------------------------------------------------------------
# Milestone 6 PART E: the curated SCIENTIFIC control set.
# A control appears here only if, on 2026-11-17, all three hold:
#   - the flag exists in the deployed unified entry (open_neo/cli.py run
#     subparser) AND in the gateway /open/run ROUTE_SPECS optional list
#     (src/neoag/controlled_execution/gateway.py), so the choice genuinely
#     changes what a native run does;
#   - the Web layer forwards it (backend/native.py _INPUT_KEYS /
#     _SCIENTIFIC_KEYS -> build_plan_argv and the gateway payload);
#   - the value is fully constrained (fixed choices, bounded integers,
#     checkboxes) and re-validated server-side (native.validate_scientific)
#     - no free-text CLI parameter is ever shown as a scientific control.
# Unset on every control means "native default" - nothing is forwarded and
# the deployed defaults (cited per row) apply.
# Scientific decisions the deployed code makes on its own have NO knob
# here; they are listed in SCIENCE_INFO with their code citations instead.
SCIENCE_SECTIONS = {
    "rna_fusion_splice": "RNA expression evidence",
    "tool_routing": "Evidence-source routing when a primary tool is missing",
    "structural_variants": "Structural-variant branch",
    "ranking_reporting": "Ranking windows",
    "reference_consistency": "Reference build consistency",
}
SCIENCE_CONTROLS = [
    {"id": "rna_quant_method", "section": "rna_fusion_splice",
     "label": "Transcript quantification backend",
     "control": "radio", "options": ["auto", "salmon", "rsem"],
     "native_default": "auto",
     "affects": "Which quantifier produces the expression evidence that "
                "feeds the fusion/splice branches and expression scoring.",
     "evidence": f"{RUN_CLI} run.add_argument('--rna-quant-method', "
                 "choices=[auto, salmon, rsem])"},
    {"id": "automatic_tool_policy", "section": "tool_routing",
     "label": "Alternative-tool policy",
     "control": "radio",
     "options": ["all-available", "balanced", "minimal"],
     "native_default": "all-available",
     "affects": "Which alternative tools the planner may route to when a "
                "primary tool is unavailable - changes the evidence "
                "sources that reach scoring, not just resource use.",
     "evidence": f"{RUN_CLI} run.add_argument('--automatic-tool-policy', "
                 "choices=[all-available, balanced, minimal])"},
    {"id": "skip_dna_sv", "section": "structural_variants",
     "label": "Disable the DNA SV calling branch",
     "control": "checkbox", "native_default": False,
     "affects": "The run produces no SV-derived peptide events at all; "
                "supplying an SV VCF input is then pointless.",
     "evidence": f"{RUN_CLI} run.add_argument('--skip-dna-sv')"},
    {"id": "skip_bam_matcher", "section": "structural_variants",
     "label": "Skip the tumor/normal identity check",
     "control": "checkbox", "native_default": False,
     "affects": "Turns off the BAM sample-identity (contamination/swap) "
                "guard. Only tick when identity was verified elsewhere - "
                "this weakens a safety check.",
     "evidence": f"{RUN_CLI} run.add_argument('--skip-bam-matcher')"},
    {"id": "event_top_n", "section": "ranking_reporting",
     "label": "Event top-N",
     "control": "number", "min": 1, "max": 500, "step": 1,
     "native_default": 20,
     "affects": "How many event-level rows the final ranking keeps - the "
                "review window for junction/fusion/SV events.",
     "evidence": f"{RUN_CLI} run.add_argument('--event-top-n', type=int, "
                 "default=20)"},
    {"id": "candidate_top_n", "section": "ranking_reporting",
     "label": "Candidate top-N",
     "control": "number", "min": 1, "max": 2000, "step": 1,
     "native_default": 100,
     "affects": "How many peptide-HLA candidates are kept per track - "
                "the window the shortlist is drawn from.",
     "evidence": f"{RUN_CLI} run.add_argument('--candidate-top-n', "
                 "type=int, default=100)"},
    {"id": "genome_build", "section": "reference_consistency",
     "label": "Genome build",
     "control": "radio", "options": ["GRCh38", "GRCh37"],
     "native_default": "GRCh38",
     "affects": "Consistency key locking reference FASTA, VEP cache and "
                "loci assets to one build; a mismatch silently produces "
                "stale calls, so only builds this deployment has "
                "validated assets for may be chosen.",
     "evidence": f"{RUN_CLI} run.add_argument('--genome-build', "
                 "default=GRCh38); build check "
                 "src/neoag/open_neo/auto_config.py:367-376"},
]

# Scientific decisions with NO exposed knob: the deployed code decides
# them, and the UI says where instead of pretending to offer a control.
SCIENCE_INFO = [
    {"title": "Presentation predictors & consensus",
     "decided": "the capability planner derives the predictor set and its "
                "required minimum from the installed toolchain - a web "
                "choice could not run tools that are not installed.",
     "evidence": "src/neoag/open_neo/capability_planner.py:831-871"},
    {"title": "HLA-LOH handling",
     "decided": "LOH evidence (a manifest input) marks LOH alleles during "
                "scoring; the scoring rule itself is fixed in code, so "
                "there is no threshold knob to offer.",
     "evidence": "src/neoag/scoring.py:262-267"},
    {"title": "CNV / CCF uncertainty",
     "decided": "CCF and CNV evidence feed fixed caution notes and caps "
                "in scoring (clonality caution below CCF 0.75; RNA-only "
                "unresolved events handled explicitly) - no exposed "
                "weight or threshold.",
     "evidence": "src/neoag/scoring.py:281-298"},
    {"title": "Fusion evidence quality",
     "decided": "RNA-only fusion events get a priority cap read from the "
                "weighted ranking profile; overriding that profile file "
                "is the expert execution path (--weighted-baseline), not "
                "a science toggle.",
     "evidence": "src/neoag/scoring.py:401-404"},
]
# module label/purpose for concern B; the tools column is NOT stated
# here - it is derived at request time from the code-grounded inventory
# (load_inventory stages), so it can never drift from the repo.
SCIENCE_MODULES = {
    "RNA branch routing": {
        "id": "rna_fusion_splice",
        "label": "RNA quantification, fusion & splice evidence",
        "purpose": "Transcript-level evidence: gene expression, fusion "
                   "calls and splice junctions feeding the RNA peptide "
                   "branches.",
        "stages": ["rna_expression", "fusion_discovery",
                   "splice_discovery"]},
    "SV branch": {
        "id": "structural_variants",
        "label": "Structural variants",
        "purpose": "SV calling branch feeding SV-derived peptide evidence.",
        "stages": ["sv"]},
    "Ranking and reporting": {
        "id": "ranking_reporting",
        "label": "Ranking & reporting",
        "purpose": "Final candidate ranking (top-N windows) and the "
                   "report bundles written at run end.",
        "stages": ["ranking", "reports"]},
}


def params_view() -> list[dict]:
    """PARAM_GROUPS enriched with concern + module + CLI flag fields.
    Additive: every original key (group/expert/items/source/...) stays,
    so existing consumers keep working."""
    import re as _re
    out = []
    for g in PARAM_GROUPS:
        gg = dict(g)
        concern = CONCERN_BY_GROUP.get(g["group"], "execution")
        gg["concern"] = concern
        mod = SCIENCE_MODULES.get(g["group"])
        if mod:
            gg["module"] = dict(mod)
        items = []
        for it in g["items"]:
            ii = dict(it)
            ii["concern"] = CONCERN_BY_ITEM.get(it["id"], concern)
            m = _re.search(r"'(--[\w-]+)'", str(it.get("source") or ""))
            if m:
                ii["cli"] = m.group(1)
            items.append(ii)
        gg["items"] = items
        out.append(gg)
    return out
