"""S2 task ontology: what scientific question each NeoAG module answers.

One TaskDef per module, written as DATA: the biological question, the
measurable endpoints (each with its output quantity, how ground truth
is constructed for it, and the metrics that score it), the input
modalities and their preprocessing dependencies, the outputs the
module produces, and the downstream NeoAG consumers that read those
outputs. Relevance, identity disambiguation, comparability and metric
binding all consult this ontology - it is the single place where
"what is this module for" is defined.

Vocabulary rule (root-cause 1.1): every matcher is an explicit
alternation compiled to a regex ("copy[ _-]?number" style), never a
pseudo-regex dotted literal matched by a substring test. The module
self-tests on import-time data: every declared term must match its
sample string (see self_test()) so a term that cannot match real
English prose is a build failure, not a silent scientific exclusion.
"""
from __future__ import annotations

import re

MATCHER_CLASS = re.compile  # documented: terms ARE regexes here

# ---------------------------------------------------------------------------
# TaskDef construction
# ---------------------------------------------------------------------------


def _ep(quantity, truth, *metrics):
    return {"quantity": quantity, "ground_truth": truth,
            "metrics": list(metrics)}


def T(question, endpoints, modalities, preprocessing, outputs, consumers,
      domain_terms, complement_terms, off_domain_terms, sample_corpus):
    """Build one TaskDef. domain_terms decide 'this text talks about
    my problem'; complement_terms mark adjacent roles (e.g. QC);
    off_domain_terms are POSITIVE signals of a different field
    (cybersecurity, web-dev, star lists...) - the ONLY textual basis
    on which a candidate may be called unrelated without a resolved
    identity."""
    def _rx(terms):
        parts = [t for t in terms if t]
        if not parts:
            return None
        rx = re.compile("|".join(f"(?:{t})" for t in parts), re.I)
        return rx
    return {"question": question, "endpoints": endpoints,
            "modalities": modalities, "preprocessing": preprocessing,
            "outputs": outputs, "consumers": consumers,
            "domain_terms": domain_terms,
            "complement_terms": complement_terms,
            "off_domain_terms": off_domain_terms,
            "domain_rx": _rx(domain_terms),
            "complement_rx": _rx(complement_terms),
            "off_domain_rx": _rx(off_domain_terms),
            "sample_corpus": sample_corpus}


# Common ground-truth phrases reused across tasks
_GT_TRUTH_CALLS = "curated truth calls on a benchmark cohort"

TASK_DEFS = {
    "purity_cnv": T(
        question=(
            "Which purity/CNV method estimates the quantities NeoAG "
            "consumes (tumour purity, ploidy, total and allele-specific "
            "copy number, segmentation, LOH, multiplicity) more "
            "accurately under conditions comparable to this deployment?"),
        endpoints=[
            _ep("tumour purity (fraction of cancer cells in the sample)",
                "orthogonal estimate or known-mixture truth (cell lines, "
                "simulations, multi-assay consensus)",
                "MAE", "median absolute error", "concordance correlation"),
            _ep("tumour ploidy / mean copy number",
                "karyotype-anchored or simulated ground truth",
                "exact match rate", "within-one error rate"),
            _ep("total copy number per segment",
                "segment-level truth from array/orthogonal assay or "
                "simulation",
                "per-segment accuracy", "segment-level concordance"),
            _ep("allele-specific (major/minor) copy number",
                "phase-anchored or simulated truth",
                "integer exact-match rate", "single-allele error rate"),
            _ep("copy-number segmentation boundaries",
                "annotated breakpoints (simulated or curated)",
                "boundary distance", "region F1"),
            _ep("loss-of-heterozygosity events",
                "curated LOH calls",
                "sensitivity", "specificity", "F1"),
            _ep("subclonal copy-number structure",
                "single-cell or simulated subclone truth",
                "cluster concordance"),
            _ep("per-variant copy-number multiplicity",
                "derived from truth allele-specific CN",
                "multiplicity exact-match rate"),
        ],
        modalities=["WES", "WGS", "targeted panel",
                    "SNP array (for some tools)"],
        preprocessing=[
            "alignment to a reference genome",
            "somatic SNV/indel calling producing a tumor(-normal) VCF",
            "BAF/allele-depth extraction (genotype panel or "
            "germline SNP site list)",
            "coverage/log2 ratio computation with GC and mappability "
            "correction",
            "matched normal preferred; tumor-only modes exist per tool",
        ],
        outputs=["purity estimate", "ploidy estimate",
                 "segment table (start, end, nCN, bCN/VAF, log2)",
                 "per-variant multiplicity (tool-dependent)"],
        consumers=[
            "cellularity / CCF calculation for variant clustering",
            "integer copy number + multiplicity feeding neoantigen "
            "prioritisation (LOH-aware selection)",
            "HLA-LOH and allele-specific expression interpretation",
            "report evidence tables (purity.tsv, cnv_segments.tsv)",
        ],
        domain_terms=[
            r"t?umou?r purity", r"tumour? cell fraction",
            r"purity estimat\w*", r"purity inferen\w*",
            r"copy[ _-]?number", r"\bcnv\b", r"\bcnas?\b",
            r"ploidy", r"aneuploid\w*", r"allele[ -]specific copy number",
            r"integer copy number", r"loss of heterozygosity",
            r"\blok\b", r"loss-of-heterozygosity",
            r"somatic copy[ -]number alteration",
        ],
        complement_terms=[
            r"variant allele frequency", r"\bvaf\b", r"clonal deconvolution",
            r"subclone\w*", r"mutational burden", r"\btmb\b",
            r"genom\w* instability", r"loss of heterozygosity detection "  # LOH-only tools
            r"from (?:hla|expression)",
        ],
        off_domain_terms=[
            r"cyber\w*", r"network intrusion", r"web application",
            r"star rating\w*", r"recommendation system", r"image caption",
            r"\bprotein (?:folding|structure prediction)\b",
            r"chatbot", r"\bllm\b", r"large language model",
            r"sentiment analys\w*", r"fraud detection",
        ],
        sample_corpus=[
            "tumor purity estimation from exome data",
            "copy-number alterations and ploidy",
            "allele-specific copy number segments",
            "tumor purity", "CNV caller", "loss of heterozygosity",
        ],
    ),
    "hla_typing": T(
        question=(
            "Which HLA typing method performs better on comparable "
            "WES/WGS/RNA-seq data at the field resolutions NeoAG "
            "consumes?"),
        endpoints=[
            _ep("HLA allele calls at 2/4/6/8-field resolution",
                "Sanger/long-read or orthogonal high-resolution typing",
                "allele-level exact match at stated field resolution",
                "novel-allele detection rate"),
        ],
        modalities=["WES", "WGS", "RNA-seq"],
        preprocessing=["alignment", "region BAM or assembled contigs",
                       "reference allele database version"],
        outputs=["class I/II allele calls per locus",
                 "confidence scores"],
        consumers=["peptide-HLA binding prediction (all presentation "
                   "predictors consume the typed alleles)",
                   "HLA-LOH inference", "report"],
        domain_terms=[
            r"\bhla\b", r"histocompatibility", r"mhc class",
            r"allele[ -]level(?:s)? (?:resolut\w*|typ\w*)",
            r"typing accuracy", r"field resolution",
        ],
        complement_terms=[r"imputation", r"reference database",
                          r"haplotype phase", r"novel allele",
                          r"typing quality control"],
        off_domain_terms=[r"cyber\w*", r"web application", r"fraud",
                          r"image caption"],
        sample_corpus=["HLA typing from whole exome sequencing",
                       "allele-level resolution at 2 and 6 field",
                       "MHC class I typing accuracy"],
    ),
    "hla_loh": T(
        question=(
            "Which HLA LOH inference method detects copy-neutral and "
            "copy-number LOH more accurately on tumor/normal pairs?"),
        endpoints=[
            _ep("HLA-LOH status calls (LOH yes/no, allelic imbalance)",
                "curated truth from orthogonal typing of both samples "
                "or spike-in mixtures",
                "sensitivity", "specificity"),
        ],
        modalities=["WES", "WGS", "RNA-seq"],
        preprocessing=["HLA typing of tumor and normal", "VAF/allelic "
                       "ratio extraction at HLA loci"],
        outputs=["per-allele LOH call + supporting allelic imbalance"],
        consumers=["presentation stage allele selection",
                   "neoantigen LOH-aware filtering"],
        domain_terms=[r"\bhla\b", r"histocompatibility",
                      r"loss of heterozygosity", r"\blok\b",
                      r"loss-of-het"],
        complement_terms=[r"allele[ -]specific copy number",
                          r"copy[ -]neutral loh"],
        off_domain_terms=[r"cyber\w*", r"web application", r"fraud"],
        sample_corpus=["HLA loss of heterozygosity in tumors",
                       "LOH at the HLA locus"],
    ),
    "rna_expression": T(
        question=(
            "Which transcript quantifier gives expression values more "
            "concordant with orthogonal measurements on the same "
            "RNA-seq?"),
        endpoints=[
            _ep("gene/transcript abundance (TPM/counts)",
                "qPCR or replicate-consensus gold standard",
                "gene-level Spearman/Pearson correlation",
                "transcript-level concordance"),
        ],
        modalities=["RNA-seq (bulk)"],
        preprocessing=["adapter trimming", "annotation/-transcriptome "
                       "version", "multi-mapping resolution"],
        outputs=["counts/TPM tables per sample"],
        consumers=["expression filtering of neoantigen candidates",
                   "splicing evidence (PSI context)"],
        domain_terms=[r"rna[ -]?seq", r"transcript abundance",
                      r"gene expression", r"transcript expression",
                      r"\btpm\b", r"quantification"],
        complement_terms=[r"differential expression", r"isoform usage",
                          r"multi-?mapping"],
        off_domain_terms=[r"cyber\w*", r"web application", r"fraud"],
        sample_corpus=["transcript quantification from RNA-seq",
                       "TPM correlation with qPCR"],
    ),
    "snv": T(
        question=(
            "Which somatic small-variant caller achieves better "
            "precision/recall on benchmark tumor-normal pairs?"),
        endpoints=[
            _ep("somatic SNV/indel calls", _GT_TRUTH_CALLS,
                "precision", "recall", "F1 at fixed thresholds"),
        ],
        modalities=["WES", "WGS", "panel"],
        preprocessing=["alignment + duplicate marking", "base quality "
                       "recalibration (pipeline-dependent)"],
        outputs=["somatic VCF"],
        consumers=["CNV/purity stage VAF/BAF input",
                   "neoantigen peptide generation"],
        domain_terms=[r"somatic", r"small variant", r"\bsnv\b",
                      r"single[ -]nucleotide variant",
                      r"somatic mutation"],
        complement_terms=[r"germline calling", r"joint genotyping",
                          r"ensemble calling"],
        off_domain_terms=[r"cyber\w*", r"web application", r"fraud"],
        sample_corpus=["somatic small variant calling benchmarks",
                       "SNV precision recall tumor normal"],
    ),
    "fusion_discovery": T(
        question=(
            "Which RNA-seq fusion caller gives higher precision at an "
            "acceptable recall on benchmark data?"),
        endpoints=[
            _ep("fusion transcript calls (gene pairs + breakpoints)",
                "curated breakpoint-level truth sets",
                "breakpoint-level precision", "recall", "F1",
                "gene-pair-level precision/recall"),
        ],
        modalities=["RNA-seq", "targeted fusion panels"],
        preprocessing=["alignment (split-read or fusion-aware)",
                       "annotation of chimeric reads"],
        outputs=["fusion call tables with supporting evidence"],
        consumers=["fusion peptide translation", "report"],
        domain_terms=[r"fusion gene", r"gene fusion",
                      r"chimeric transcript", r"fusion transcript",
                      r"read[ -]through"],
        complement_terms=[r"structural variant", r"read[ -]through "
                          r"artifacts"],
        off_domain_terms=[r"cyber\w*", r"web application", r"fraud"],
        sample_corpus=["gene fusion detection from RNA-seq",
                       "chimeric transcript callers"],
    ),
    "fusion_peptides": T(
        question=(
            "Which method translates validated fusions into neoepitope "
            "candidates with better evidence grounding?"),
        endpoints=[
            _ep("novel peptide sequences from fusion reading frames",
                "mass-spectrometry identifications or curated "
                "validated junction peptides",
                "validated-junction epitope yield", "artifact rate"),
        ],
        modalities=["RNA-seq", "fusion call tables"],
        preprocessing=["validated fusion input", "reading-frame / ORF "
                       "annotation"],
        outputs=["novel peptide FASTA with junction provenance"],
        consumers=["presentation scoring", "ranking"],
        domain_terms=[r"fusion", r"chimeric", r"neoantigen",
                      r"neoepitope", r"open reading frame",
                      r"novel peptide"],
        complement_terms=[r"ribosome profiling", r"translation of "
                          r"circles"],
        off_domain_terms=[r"cyber\w*", r"web application", r"fraud"],
        sample_corpus=["fusion neoantigen peptide translation",
                       "novel open reading frame from fusion junctions"],
    ),
    "splice_discovery": T(
        question=(
            "Which aberrant/alternative splicing detector recovers more "
            "validated tumor splicing events at matched false-positive "
            "control?"),
        endpoints=[
            _ep("aberrant/alternative splicing events (junctions, PSI)",
                "annotated events, long-read validation, or "
                "RT-PCR-confirmed sets",
                "event-level sensitivity", "specificity"),
        ],
        modalities=["RNA-seq"],
        preprocessing=["splice-aware alignment", "annotation version"],
        outputs=["event tables (junction, PSI, tumor-vs-normal "
                 "differential)"],
        consumers=["splice peptide generation"],
        domain_terms=[r"splic\w*", r"isoform", r"splice junction",
                      r"transcript variant", r"aberrant splicing"],
        complement_terms=[r"event[ -]level quantification",
                          r"junction[ -]level", r"isoform switching"],
        off_domain_terms=[r"cyber\w*", r"web application", r"fraud"],
        sample_corpus=["aberrant splicing detection from RNA-seq",
                       "alternative splice junction discovery"],
    ),
    "splice_peptides": T(
        question=(
            "Which approach produces neoepitope candidates from tumor "
            "splice junctions with fewer artifacts?"),
        endpoints=[
            _ep("novel peptides from frameshifted/retained junction "
                "ORFs",
                "validated junction peptide sets / MS identifications",
                "validated-junction epitope yield", "artifact rate"),
        ],
        modalities=["RNA-seq", "splicing event tables"],
        preprocessing=["validated event input", "ORF annotation"],
        outputs=["novel peptide FASTA"],
        consumers=["presentation scoring", "ranking"],
        domain_terms=[r"splic\w*", r"junction", r"isoform",
                      r"neoantigen", r"neoepitope", r"frameshift"],
        complement_terms=[r"nonsense[ -]mediated", r"translation"],
        off_domain_terms=[r"cyber\w*", r"web application", r"fraud"],
        sample_corpus=["splice junction neoepitopes",
                       "frameshift peptides from tumor isoforms"],
    ),
    "presentation": T(
        question=(
            "Which presentation/binding predictor performs better on an "
            "experimentally validated benchmark of the NeoAG allele "
            "set?"),
        endpoints=[
            _ep("peptide-HLA binding affinity/eligibility scores",
                "biochemical binding assays or eluted-ligand mass "
                "spectrometry (state which)",
                "AUROC", "AUPRC", "ligand recall at fixed FPR"),
            _ep("immunogenicity (T-cell recognition) predictions",
                "T-cell assay readouts (small, assay-specific)",
                "AUROC/AUPRC on stated label set"),
        ],
        modalities=["peptide + HLA allele inputs (in silico)"],
        preprocessing=["allele set from typing stage",
                       "peptide length/prediction mode"],
        outputs=["binding/eligibility scores per peptide-HLA pair"],
        consumers=["composite ranking", "shortlist gating"],
        domain_terms=[r"\bmhc\b", r"\bhla\b", r"peptide[ -]hla",
                      r"p\s?mhc", r"eluted ligand",
                      r"immunopeptidom\w*", r"binding affinity",
                      r"binding predict\w*", r"antigen presentation"],
        complement_terms=[r"pan[ -]specific", r"allele[ -]specific",
                          r"cleavage", r"proteasom\w*",
                          r"immunogenicity"],
        off_domain_terms=[r"cyber\w*", r"web application", r"fraud",
                          r"image caption"],
        sample_corpus=["peptide-HLA binding prediction benchmarks",
                       "eluted ligand mass spectrometry training",
                       "MHC class I presentation predictors"],
    ),
    "safety_escape": T(
        question=(
            "Which method better excludes self-reactive or off-tumor "
            "epitopes without losing tumor coverage?"),
        endpoints=[
            _ep("self/off-tumor epitope exclusion decisions",
                "self-peptide/normal-proteome databases + curated "
                "autoantigen sets",
                "self-reactivity exclusion rate",
                "tumor-epitope retention rate"),
        ],
        modalities=["peptide lists (in silico)", "self-proteome DB"],
        preprocessing=["normal proteome / self-peptide collection "
                       "construction"],
        outputs=["flagged/rejected epitope calls with reason"],
        consumers=["shortlist safety gating"],
        domain_terms=[r"self[ -]peptide", r"autoantigen",
                      r"auto[ -]?reactiv\w*", r"self[ -]reactiv\w*",
                      r"off[ -]tumor",
                      r"cross[ -]reactiv\w*", r"immune tolerance",
                      r"normal proteome", r"central tolerance"],
        complement_terms=[r"homology search", r"similarity search",
                          r"decoy"],
        off_domain_terms=[r"cyber\w*", r"web application", r"fraud"],
        sample_corpus=["off-tumor shared antigen exclusion",
                       "self-reactivity filtering of neoepitopes"],
    ),
    "ranking": T(
        question=(
            "Do published neoantigen prioritisation methods improve on "
            "NeoAG's composite ranking using tumor expression, somatic "
            "evidence, presentation and immunogenicity evidence "
            "together?"),
        endpoints=[
            _ep("ranked neoantigen candidate lists",
                "experimentally validated epitopes or clinical "
                "responses recovered in the top-N",
                "recall of validated epitopes in top-N",
                "average precision against validated sets"),
        ],
        modalities=["multi-omic evidence tables (in silico)"],
        preprocessing=["all upstream stage outputs"],
        outputs=["ranked shortlist"],
        consumers=["report", "clinical review"],
        domain_terms=[r"neoantigen", r"neo[ -]?antigen",
                      r"neoepitope", r"t[ -]cell epitope",
                      r"immunotherap\w*", r"prioriti[sz]ation"],
        complement_terms=[r"learning[ -]to[ -]rank",
                          r"multi[ -]criteria decision",
                          r"ensemble consensus"],
        off_domain_terms=[r"cyber\w*", r"web application", r"fraud",
                          r"e[ -]commerce", r"click[ -]through"],
        sample_corpus=["neoantigen prioritisation method comparison",
                       "neoepitope ranking benchmarks"],
    ),
    "reports": T(
        question=(
            "Is there a reporting layer that communicates neoantigen "
            "results to clinicians better than NeoAG's built-in "
            "generator?"),
        endpoints=[
            _ep("clinician-facing result reports",
                "clinician comprehension / review-time studies",
                "comprehension score", "review time"),
        ],
        modalities=["result data (in silico)"],
        preprocessing=["result normalization"],
        outputs=["generated report documents"],
        consumers=["clinical review"],
        domain_terms=[r"neoantigen report", r"clinical report",
                      r"patient report", r"report generation",
                      r"immunotherapy report"],
        complement_terms=[r"visuali[sz]ation", r"interpretation aid"],
        off_domain_terms=[r"cyber\w*", r"web application", r"fraud"],
        sample_corpus=["clinical reporting of neoantigen results",
                       "report generation for immunotherapy review"],
    ),
}

GENERIC_TASK = T(
    question=(
        "Which published method for this task performs better under "
        "conditions comparable to NeoAG's deployment?"),
    endpoints=[], modalities=[], preprocessing=[], outputs=[],
    consumers=[], domain_terms=[], complement_terms=[],
    off_domain_terms=[], sample_corpus=[])


def task_for(target_id: str) -> dict:
    """S2 lookup: TaskDef for one stage id (custom targets get the
    explicitly-empty generic task - whose empty domain list makes the
    relevance stage report 'task undefined', never default-approve)."""
    return TASK_DEFS.get(target_id, GENERIC_TASK)


def endpoint_question(task: dict) -> str:
    """One-line scientific question including the endpoint quantities,
    used by identity disambiguation and follow-up query construction."""
    q = task["question"]
    return q


def quantities(task: dict) -> list[str]:
    return [e["quantity"] for e in task["endpoints"]]


def metrics(task: dict) -> list[str]:
    return sorted({m for e in task["endpoints"] for m in e["metrics"]})


def match_domain(task: dict, text: str) -> list[str]:
    """Positive domain matches (regex) found in text - the returned
    list of matched spans is the EVIDENCE basis for relevance."""
    rx = task.get("domain_rx")
    if not rx or not text:
        return []
    return sorted({m.group(0).lower() for m in rx.finditer(text)})


def match_off_domain(task: dict, text: str) -> list[str]:
    rx = task.get("off_domain_rx")
    if not rx or not text:
        return []
    return sorted({m.group(0).lower() for m in rx.finditer(text)})


def match_complement(task: dict, text: str) -> list[str]:
    rx = task.get("complement_rx")
    if not rx or not text:
        return []
    return sorted({m.group(0).lower() for m in rx.finditer(text)})


def self_test() -> list[str]:
    """Return problems with the ontology (empty list = clean). Every
    matcher must compile (checked at import) and every task's domain
    regex must match each of its own sample strings; a sample that
    fails means the vocabulary is broken (root-cause 1.1: dead terms
    silently cause wrong exclusions)."""
    problems = []
    for tid, t in TASK_DEFS.items():
        if t is GENERIC_TASK:
            continue
        for s in t["sample_corpus"]:
            if not t["domain_rx"] or not t["domain_rx"].search(s):
                problems.append(
                    f"{tid}: sample string {s!r} matches no domain "
                    f"term of its own task")
        for term in t["domain_terms"]:
            try:
                re.compile(term)
            except re.error as e:
                problems.append(f"{tid}: domain term {term!r} does "
                                f"not compile: {e}")
    return problems
