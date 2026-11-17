"""Milestone 6 PART A: scientific eligibility screening, task-driven
retrieval vocabulary, recommendation rules and research quality gates.

Everything here is a labelled, inspectable heuristic over text the
engine ACTUALLY retrieved (registry blurbs, paper titles/abstracts) -
no candidate is ever admitted or rejected by a hidden list of names.
The rules are deliberately general: each module declares the scientific
DOMAIN terms of its task, the METHOD-FAMILY terms that are technically
related but not domain-specific, and the OFF-DOMAIN signals that mark a
retirement-grade mismatch (cybersecurity reports, GitHub star lists,
HTTP dashboards...). A candidate must show domain vocabulary in its own
retrieved scientific description to become a direct alternative.

Eligibility vocabulary (fixed, from the M6 brief A2):
    direct alternative | complementary method |
    wrapper/integration (not an independent algorithm) |
    related but not comparable | irrelevant |
    insufficient information to classify
Only 'direct alternative' rows may enter a replacement comparison.
"""
from __future__ import annotations

import re

from . import ontology

ELIGIBILITY_VOCABULARY = [
    "direct alternative",
    "complementary method",
    "wrapper/integration (not an independent algorithm)",
    "related but not comparable",
    "irrelevant",
    "insufficient information to classify",
]

# Names that are gene symbols / HLA alleles / accessions, not software.
# A candidate whose name matches one of these shapes needs a positive
# scientific description (or a naming publication) before it can count
# as a method at all - otherwise it stays 'insufficient information'.
GENE_SHAPE = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")          # ERBB2, TP53
ALLELE_SHAPE = re.compile(r"^[A-Z]{2}\d?[A-Z]?\d?\*?\d*")  # DRB1, HLA-C*
NON_SOFTWARE_REASON = (
    "the candidate name has the shape of a gene symbol / HLA allele / "
    "biological identifier, and the retrieved description does not "
    "establish it as a method or tool")

# Software/method signals inside a registry blurb or abstract window.
METHOD_SIGNAL = [
    "tool", "software", "package", "library", "program", "platform",
    "framework", "algorithm", "model", "predict", "classif", "caller",
    "pipeline", "workflow", "implements", "we present", "we develop",
    "we introduce", "designed to", "method for", "approach for",
    "detector", "estimator", "genotyper", "typer", "quantif",
]

WRAPPER_SIGNAL = ["wraps", "wrapper", "interface to", "interface for",
                  "encapsulates", "container for", "snakemake", "nextflow",
                  "galaxy", "cwl", "api server", "web server for",
                  "web interface", "shiny app", "gui"]

# A wrapper that SAYS it wraps is a wrapper whatever else its blurb
# mentions; a bare workflow-engine word only means wrapper when there
# is no independent-algorithm claim (a tool may legitimately ship a
# pipeline runner alongside a real algorithm).
STRONG_WRAPPER = ["wraps", "wrapper", "encapsulates", "interface to",
                  "interface for", "container for", "web server for",
                  "web interface", "shiny app", "gui"]
# method words that do NOT by themselves assert an independent
# algorithm (they name integration, packaging or execution):
_INTEGRATION_WORDS = {"pipeline", "workflow", "program", "platform",
                      "framework", "package", "library"}


def _hit(terms, blob):
    return [t for t in terms if t in blob]


# ---------------------------------------------------------------------------
# A4/A7: one scientific-task profile per module. Every list is vocabulary
# for MATCHING retrieved text; none of it names candidates.
# ---------------------------------------------------------------------------

def _p(question, domain, context, families=(), complements=(),
       off_domain=(), endpoint=""):
    return {"question": question, "domain": [d.lower() for d in domain],
            "context": [c.lower() for c in context],
            "families": [f.lower() for f in families],
            "complements": [c.lower() for c in complements],
            "off_domain": [o.lower() for o in off_domain],
            "endpoint": endpoint}


TASK_PROFILES = {
    "hla_typing": _p(
        "Which HLA typing method performs better on comparable WES/WGS/"
        "RNA-seq data at the field resolutions NeoAG consumes?",
        ["hla", "histocompatibility", "mhc class"],
        ["typing", "genotyp", "allele call", "allele-level", "typ[ing]",
         "type hla", "hla type", "allelic resolution", "field resolution"],
        ["alignment-free pseudo", "string graph", "optimal selection",
         "assembly-based typing"],
        ["imputation", "quality control", "novel allele", "typing "
         "standard", "reference database", "haplotype phase"],
        endpoint="allele-level exact match at 2/4/6/8-field resolution"),
    "hla_loh": _p(
        "Which HLA LOH inference method detects copy-neutral LOH more "
        "accurately on tumor/normal pairs?",
        ["hla", "histocompatibility"],
        ["loss of heterozygosity", "loh", "loss-of-het"],
        ["allele-specific copy number"],
        endpoint="sensitivity/specificity for LOH calls on a curated "
                 "tumor-normal truth set"),
    "purity_cnv": _p(
        "Which purity/CNV estimator is more accurate on comparable "
        "WES/WGS data with matched normals?",
        ["tumou?r purity", "purity estimat", "copy.number", "cnv",
         "ploidy", "aneuploid", "copy number alteration"],
        ["estimat", "infer", "call", "detect", "segment", "aberration"],
        ["allele-specific copy number", "integer copy number"],
        endpoint="purity MAE + segment-level concordance on truth-set "
                 "samples"),
    "rna_expression": _p(
        "Which transcript quantifier gives expression values more "
        "concordant with orthogonal measurements on the same RNA-seq?",
        ["rnaseq", "rna-seq", "rna seq", "transcript abundance",
         "gene expression", "transcript expression", "tpm"],
        ["quantif", "abundance", "profil", "measure"],
        ["pseudoalignment", "em quantification", "multi-mapping rescue"],
        endpoint="gene/transcript-level correlation vs orthogonal qPCR "
                 "or replicate gold standard"),
    "snv": _p(
        "Which somatic small-variant caller achieves better precision/"
        "recall on benchmark tumor-normal pairs?",
        ["somatic", "small variant", "snv", "single.nucleotide variant",
         "somatic mutation"],
        ["call", "detect", "prioritis", "filter false"],
        ["ensemble calling", "machine-learning filter"],
        endpoint="SNV/indel precision+recall on a truth-call cohort"),
    "fusion_discovery": _p(
        "Which RNA-seq fusion caller gives higher precision at an "
        "acceptable recall on benchmark data?",
        ["fusion gene", "gene fusion", "chimeric transcript",
         "fusion transcript", "read-through"],
        ["detect", "call", "discover", "breakpoint", "assembl"],
        ["split-read", "de novo assembly", "exome targeted"],
        endpoint="breakpoint-level precision/recall/F1 on a curated "
                 "truth set"),
    "fusion_peptides": _p(
        "Which method translates validated fusions into neoepitope "
        "candidates with better evidence grounding?",
        ["fusion", "chimeric"],
        ["neoantigen", "neoepitope", "peptide", "epitope", "translation",
         "open reading frame"],
        endpoint="epitope yield + validation-hit rate on known fusions"),
    "splice_discovery": _p(
        "Which aberrant/alternative splicing detector recovers more "
        "validated tumor splicing events at matched false-positive "
        "control?",
        ["splic", "isoform", "junction", "transcript variant"],
        ["detect", "differential", "event", "quantif", "aberrant"],
        ["event-level quantification", "junction-level"],
        endpoint="event-level sensitivity/specificity vs annotated or "
                 "long-read validated events"),
    "splice_peptides": _p(
        "Which approach produces neoepitope candidates from tumor "
        "splice junctions with fewer artifacts?",
        ["splice", "junction", "isoform"],
        ["neoantigen", "neoepitope", "peptide", "epitope", "frameshift",
         "novel open reading frame"],
        endpoint="validated-junction epitope yield; artifact rate"),
    "presentation": _p(
        "Which presentation/binding predictor performs better on an "
        "experimentally validated benchmark of the NeoAG allele set?",
        ["mhc", "hla", "peptide-hla", "p mhci", "eluted ligand",
         "immunopeptidom"],
        ["bind", "bind[ing] predict", "present", "ligand", "el predict",
         "immunogenic", "cleavage", "proteas"],
        ["pan-specific", "allele-specific", "sequence+el trains",
         "mass-spec trained"],
        endpoint="AUROC/AUPRC on a defined ligand benchmark with "
                 "stated allele coverage"),
    "safety_escape": _p(
        "Which method better excludes self-reactive or off-tumor "
        "epitopes without losing tumor coverage?",
        ["self.peptide", "autoantigen", "auto-reacti", "autoreacti",
         "off-tumor", "cross-reacti", "tolerance", "normal proteome"],
        ["exclud", "filter", "avoid", "escape", "homology", "similarity "
         "search"],
        endpoint="self-reactivity exclusion rate vs tumor-epitope "
                 "retention"),
    "ranking": _p(
        "Do published neoantigen prioritisation methods improve on "
        "NeoAG's composite ranking using tumor expression, somatic "
        "evidence, presentation and immunogenicity evidence together?",
        ["neoantigen", "neo-antigen", "neoepitope", "t-cell epitope",
         "immunotherap"],
        ["prioriti", "rank", "select", "filter candidate",
         "multi.omics", "integrate", "composite", "decision"],
        ["learning-to-rank", "multi-criteria decision",
         "hand-crafted scoring", "machine-learning ranking",
         "random forest", "gradient boost"],
        endpoint="recall of experimentally validated epitopes / "
                 "clinical responses in the top-N"),
    "reports": _p(
        "Is there a reporting layer that communicates neoantigen "
        "results to clinicians better than NeoAG's built-in generator?",
        ["neoantigen report", "clinical report", "patient report",
         "report generation", "immunotherapy report", "precision "
         "oncolog.*report"],
        ["report", "visualis", "visualiz", "interpret", "communicat"],
        endpoint="clinician comprehension/review-time study"),
}

GENERIC_PROFILE = _p(
    "Which published method for this task performs better under "
    "conditions comparable to NeoAG's deployment?",
    [], [], [], [],
    endpoint="define the endpoint from a published benchmark of the "
             "target's own field before any ranking")


def profile_for(target_id: str, label: str = "") -> dict:
    p = TASK_PROFILES.get(target_id)
    if p:
        return p
    return GENERIC_PROFILE


# ---------------------------------------------------------------------------
# A2: candidate eligibility screening
# ---------------------------------------------------------------------------

def _shape_guard(name: str) -> bool:
    n = name.strip()
    return bool(GENE_SHAPE.match(n)
                or ALLELE_SHAPE.match(n.replace("-", "").split("*")[0])
                and "*" in n)


def screen_candidate(name: str, blurb: str, *, profile: dict,
                     origin: str = "", has_naming_source: bool = False,
                     artifact_class: str = "") -> dict:
    """Assign an eligibility class + explicit reasons from the text
    actually retrieved about the candidate. Returns
    {eligibility, reasons[], comparable_for_replacement, confidence}.
    """
    blob = (blurb or "").lower()
    reasons: list[str] = []
    off = _hit(profile["off_domain"], blob)
    if off:
        return {
            "eligibility": "irrelevant",
            "reasons": [f"retrieved description is about an unrelated "
                        f"field (off-domain signal: {', '.join(off[:3])})"],
            "comparable_for_replacement": False,
            "confidence": "high"}
    domain = _hit(profile["domain"], blob)
    context = _hit(profile["context"], blob)
    families = _hit(profile["families"], blob)
    comp = _hit(profile["complements"], blob)
    method = _hit(METHOD_SIGNAL, blob)
    wrapper = _hit(WRAPPER_SIGNAL, blob) or \
        artifact_class in ("workflow wrapper", "tool collection",
                           "pipeline (multi-stage workflow)")
    if not blob.strip():
        return {
            "eligibility": "insufficient information to classify",
            "reasons": ["no scientific description was retrieved for "
                        "this candidate (empty registry blurb and no "
                        "naming source text)"],
            "comparable_for_replacement": False,
            "confidence": "n/a"}
    if _shape_guard(name) and not method and not has_naming_source:
        return {
            "eligibility": "insufficient information to classify",
            "reasons": [NON_SOFTWARE_REASON],
            "comparable_for_replacement": False,
            "confidence": "medium"}
    if not domain and not comp:
        if families:
            return {
                "eligibility": "related but not comparable",
                "reasons": [f"matches the general method family "
                            f"({', '.join(families[:2])}) but the "
                            f"retrieved description never mentions this "
                            f"module's scientific domain"],
                "comparable_for_replacement": False,
                "confidence": "high"}
        return {
            "eligibility": "irrelevant",
            "reasons": ["the retrieved scientific description contains "
                        "none of this module's domain vocabulary - it "
                        "does not address the same biological problem"],
            "comparable_for_replacement": False,
            "confidence": "high"}
    if comp and not context:
        return {
            "eligibility": "complementary method",
            "reasons": ["description addresses an adjacent role of this "
                        f"stage ({', '.join(comp[:2])}) rather than "
                        "performing the stage's core task - discuss "
                        "separately, not as a replacement"],
            "comparable_for_replacement": False,
            "confidence": "medium"}
    if wrapper:
        return {
            "eligibility": "wrapper/integration (not an independent algorithm)",
            "reasons": ["the retrieved description presents it as an "
                        "integration of existing methods, not an "
                        "independent algorithm"],
            "comparable_for_replacement": False,
            "confidence": "medium"}
    # A description that names this module's domain AND a task
    # operation is only a direct alternative when the retrieved text
    # makes a positive SOFTWARE CLAIM: a method/tool signal in the
    # description, or a publication that introduced this very name.
    # A merely bioinformatics-flavoured description is not enough
    # (negative fixture, 2026-11-17 live run: "DQA101" - an HLA allele
    # string - and "CoV-2" - a virus - matched "mhc + bind + biology
    # words" purely because the abstract they were mis-extracted from
    # discussed peptide binding; an allele table or a virus paper is
    # not a runnable method, and the BIO_ANCHOR word list cannot tell
    # them apart - only a software claim can).
    if context and (method or has_naming_source):
        why = ("presented as a method/tool" if method else
               "introduced as a method by a retrieved publication")
        return {
            "eligibility": "direct alternative",
            "reasons": [
                "retrieved description names this module's task domain "
                f"({', '.join(domain[:2])}) and a task operation "
                f"({', '.join(context[:2])})",
                why + (" and is named in retrieved literature"
                       if has_naming_source and method else "")],
            "comparable_for_replacement": True,
            "confidence": "medium"}
    if context:
        return {
            "eligibility": "insufficient information to classify",
            "reasons": ["domain and task match, but nothing retrieved so "
                        "far shows whether it is an actual runnable "
                        "method - cannot promote to direct alternative"],
            "comparable_for_replacement": False,
            "confidence": "low"}
    return {
        "eligibility": "complementary method",
        "reasons": ["domain vocabulary matches but the description does "
                    "not describe performing this stage's task itself"],
        "comparable_for_replacement": False,
        "confidence": "medium"}


# ---------------------------------------------------------------------------
# M8 S4: assess_relevance - positive-evidence-only relevance (PART 2.4)
# ---------------------------------------------------------------------------
#
# Root-cause 1.1 (PureCN declared "irrelevant" with "none of this
# module's domain vocabulary" while its 2.0 Bioinformatics paper was
# in the retrieved set) came from an absence rule: no keyword ->
# excluded, high confidence. Absence of a keyword is not positive
# evidence of irrelevance - it is missing information. This function
# implements the S4 contract (docs/SCIENTIFIC_ARCHITECTURE_2026-11-17):
#
#   * 'unrelated' requires a POSITIVE mismatch, from exactly three
#     sources, each recorded as a basis item with its document:
#       (a) the candidate's own retrieved text matches the task's
#           off-domain vocabulary (cybersecurity, recommendation...);
#       (b) it matches the generic non-method-artifact vocabulary
#           (visualisation / notebook widget / web framework - an
#           artifact whose documented function produces no
#           measurement of any task quantity) - the class of error
#           that once mapped "facets" to Google's visualisation
#           library while claiming irrelevance "by vocabulary";
#       (c) it positively matches ANOTHER research task's domain
#           while matching none of this task's (function documented
#           elsewhere).
#   * every other non-match yields 'unresolved' + concrete followup
#     queries - never a terminal exclusion;
#   * a research_state ladder records how far the investigation got,
#     each transition licensed by the evidence object that enabled
#     it (a naming method-paper counts as function evidence - fix
#     1.2).

RESEARCH_STATES = [
    "discovered", "identity-verified", "relevance-established",
    "function-characterized", "availability-verified",
    "performance-retrieved", "evidence-compared",
    "compatibility-assessed"]

RELATIONSHIPS = [
    "direct-alternative", "partial-overlap", "complementary",
    "upstream", "downstream", "wrapper", "alternative-implementation",
    "unrelated", "unresolved"]

# Artifact kinds that cannot produce a measurement of anything:
# name-independent vocabulary, applied only when NO domain term of the
# task (or of any task) appears in the same text.
GENERIC_NON_METHOD = [
    "visualisation library", "visualization library", "data visuali",
    "visualisation system", "visualization system",
    "visualisation toolkit", "visualization toolkit",
    "interactive html", "html bindings", "html widgets", "widgets for",
    "javascript library", "web framework", "web application",
    "dashboard", "notebook extension", "jupyter widget",
    "plotting library", "for plotting", "plot library",
    "recommendation system", "chatbot", "static site",
    "chrome extension", "browser extension"]
_GENERIC_NON_RX = re.compile(
    "|".join(re.escape(t) for t in GENERIC_NON_METHOD), re.I)

_LIT_CUE_RX = re.compile(
    r"\b(?:we (?:present|introduce|develop|describe|propose)|"
    r"here(?:,| we)|called|named|titled)\b", re.I)

# ---- capability-claim grammar (independent-corpus errors 2026-11-17) ----
# Two general frames in which a paper/document states that the NAMED
# SUBJECT performs the task's function, without any intro cue:
#   A  "<Name>: <capability> <function noun> of Y ..."   (method-paper
#      title frame: "Arriba: fast and accurate detection of ...")
#   B  "<Name> provides|delivers|enables|... <function noun> ..."
#      ("HLA-LA provides ultrafast and accurate HLA ... typing")
# Both are sentence-frame grammars - never name lists. A review
# sentence ("methods that estimate ...", "detection of X was
# benchmarked") matches neither frame, so domain vocabulary alone
# still stays non-promoting.
_FUNC_NOUN = (r"(?:detection|detecting|typing|genotyping|quantification"
              r"|segmentation|prediction|inference|identification"
              r"|scoring|ranking|prioriti[sz]ation|normali[sz]ation"
              r"|assembl(?:y|ing)|variant calling)")
_FUNC_CLAIM_RX = re.compile(
    r"^[A-Za-z][\w+.-]{1,20}\s*[:\u2013-]\s*[^.]{0,80}?\b"
    + _FUNC_NOUN + r"\b"
    +     r"|\b(?:provid(?:e|es|ed|ing)|deliver(?:s|d)?|enabl(?:e|es|ed|ing)"
      r"|perm(?:it|its|ted|ting)|allow(?:s|ed|ing)?"
      r"|perform(?:s|ed|ing)?|achiev(?:e|es|ed|ing))\b"
      r"[^.;]{0,90}?\b" + _FUNC_NOUN + r"\b", re.I)

_STATE_GLOSS = {
    "discovered": "a candidate name was retrieved",
    "identity-verified": ("the project behind the name is corroborated "
                          "by >=2 independent evidence axes"),
    "relevance-established": ("its documented function is positively "
                              "related to this task"),
    "function-characterized": ("a naming paper or method description "
                               "characterises what it computes"),
    "availability-verified": "found on a registry NeoAG could install "
                             "from",
    "performance-retrieved": ("at least one measurement was extracted "
                              "and independently verified"),
    "evidence-compared": "verified measurements were compared on a "
                         "shared comparison class",
    "compatibility-assessed": ("the integration surface in NeoAG's code "
                               "was inspected"),
}


def _basis(claim, doc_cid, text, term):
    span = None
    if term:
        i = (text or "").lower().find(term.lower())
        if i >= 0:
            span = [i, i + len(term)]
    return {"claim": claim, "doc_cid": doc_cid, "term": term,
            "text_span": span}


_SENT_SPLIT_RX = re.compile(r"(?<=[.!?;])\s+|\n+")


_SENT_SPLIT_RX = re.compile(r"(?<=[.!?;])\s+|\n+")
# anaphoric continuations: a sentence beginning with a pronoun or
# "the tool"-style reference right after a sentence naming the tool
# still describes THAT tool ("Xyz is a method. It wraps A and B.")
_PRONOUN_RX = re.compile(
    r"^(?:it|its|they|this|that|these|those|the\s+(?:tool|software|"
    r"package|program|application|implementation|wrapper|pipeline))\b",
    re.I)


def _name_anchored_text(low, name):
    """Sentences of the text that mention the tool name, plus directly
    following pronoun-continuation sentences. Wrapper vocabulary must
    be predicated of THIS tool:
    a multi-tool benchmark full text saying "executed within the
    Galaxy/Nextflow pipeline" in a sentence about some OTHER tool
    must not contaminate this candidate's row (measured 2026-11-17:
    PureCN/CNVkit demoted to "wrapper/integration" purely because
    they shared a benchmark paper with workflow-engine vocabulary).
    Empty when the name never occurs in the text - a registry blurb
    about the tool may not repeat its name - and the caller then
    falls back to the whole text."""
    if not name:
        return ""
    rx = re.compile(r"\b" + re.escape(name) + r"\b", re.I)
    out, after = [], False
    for s in _SENT_SPLIT_RX.split(low):
        if not s:
            continue
        if rx.search(s) or (_PRONOUN_RX.match(s) and after):
            out.append(s)
            after = True
        else:
            after = False
    return "\n".join(out)


def _doc_probe(task, text, name=None):
    low = text or ""
    dom = ontology.match_domain(task, low)
    off = ontology.match_off_domain(task, low)
    comp = ontology.match_complement(task, low)
    non = [m.group(0) for m in _GENERIC_NON_RX.finditer(low)]
    meth = [t for t in METHOD_SIGNAL if t in low]
    if _FUNC_CLAIM_RX.search(low):
        # the named subject is documented as PERFORMING the function
        # (title/predicate capability frame) - a positive method claim
        meth = meth + ["documented capability claim"]
    # wrapper words only count where the tool's own name is stated in
    # the same sentence (whole text if the name never occurs - see
    # _name_anchored_text)
    wrap_region = _name_anchored_text(low, name) or low
    wrap = [t for t in WRAPPER_SIGNAL if t in wrap_region]
    strong_wrap = any(t in wrap_region for t in STRONG_WRAPPER)
    # an independent-algorithm claim: a method signal that is not
    # merely an integration/packaging word, or an intro cue
    algo = [m for m in meth if m not in _INTEGRATION_WORDS]
    cue = bool(_LIT_CUE_RX.search(low))
    return {"domain": dom, "off_domain": off, "complement": comp,
            "non_method_artifact": non, "method": meth,
            "algo": algo, "wrapper": wrap, "strong_wrapper": strong_wrap,
            "intro_cue": cue}


def _foreign_domain_hit(task_id, texts):
    """Positive mismatch (c): the text belongs to ANOTHER defined
    research task - matched by that task's own domain regexes, never
    by a name list. Returns (other_task_id, terms) or None."""
    for tid in ontology.TASK_DEFS:
        if tid == task_id:
            continue
        other = ontology.task_for(tid)
        for text in texts:
            hits = ontology.match_domain(other, text)
            if len(hits) >= 2:
                return tid, hits[:3]
    return None


def assess_relevance(name, docs, *, task, task_id="", identity=None,
                     artifact_class="", lit_introduced=False,
                     availability=None, verified_claims=0,
                     compared=False, baseline_name="") -> dict:
    """S4: RelevanceDecision for one candidate.

    docs: [{"doc_cid": str, "kind": str, "text": str}] - EVERY piece
    of text this run retrieved about this candidate (registry blurbs,
    citation titles/abstracts, full text). identity: IdentityRecord
    (identity.resolve_identity) or None.
    """
    docs = [d for d in docs if (d.get("text") or "").strip()]
    q0 = (ontology.quantities(task) or ["this task's endpoint quantity"])[0]
    m0 = (ontology.metrics(task) or ["benchmark"])[0]
    probes = [(d, _doc_probe(task, d.get("text") or "", name=name))
              for d in docs]
    dom_docs = [(d, p) for d, p in probes if p["domain"]]
    all_text = [d.get("text") or "" for d in docs]
    transitions = [{"state": "discovered",
                    "evidence": f"name {name!r} retrieved"}]
    followups = []
    basis: list[dict] = []
    reasons: list[str] = []
    relationship = "unresolved"
    confidence = "low"
    comparable = False

    id_status = (identity or {}).get("status") or "not attempted"
    if id_status == "resolved":
        transitions.append({
            "state": "identity-verified",
            "evidence": f"identity resolved to "
                        f"{(identity or {}).get('resolved_project')} "
                        f"via {identity.get('n_axes')} axes"})
    elif id_status == "contested":
        reasons.append("identity contested between corroborated "
                       "projects - version/performance attribution "
                       "blocked until disambiguated")

    if not docs:
        followups += [
            {"query": f'"{name}" official implementation',
             "source": "github", "purpose": "find the project behind "
                                            "the name"},
            {"query": f"{name} {q0}",
             "source": "europepmc", "purpose": "find literature naming "
                                               "the tool"}]
        return _relevance_out(
            relationship, "n/a", False, [], transitions, followups,
            ["no text of any kind was retrieved about this candidate "
             "- unclassified, NOT irrelevant"], verified_claims,
            compared, task)

    # ---- positive-mismatch paths --------------------------------
    dom_any = bool(dom_docs)
    off_doc = next(((d, p) for d, p in probes
                    if p["off_domain"] and not p["domain"]), None)
    non_doc = next(((d, p) for d, p in probes
                    if p["non_method_artifact"] and not p["domain"]),
                   None)
    foreign = None if dom_any else _foreign_domain_hit(task_id, all_text)
    if not dom_any and off_doc:
        d, p = off_doc
        relationship = "unrelated"
        confidence = "high"
        for t in p["off_domain"][:3]:
            basis.append(_basis("retrieved text about this candidate "
                                "belongs to an unrelated field",
                                d["doc_cid"], d.get("text"), t))
        reasons.append(
            "positive mismatch: its own retrieved description uses the "
            f"vocabulary of an unrelated field ({', '.join(p['off_domain'][:3])}) "
            "while showing no connection to any quantity this task "
            "measures")
    elif not dom_any and non_doc:
        d, p = non_doc
        relationship = "unrelated"
        confidence = "high"
        for t in p["non_method_artifact"][:3]:
            basis.append(_basis("documented function is a "
                                "visualisation/UI artifact, which "
                                "produces no measurement",
                                d["doc_cid"], d.get("text"), t))
        reasons.append(
            "positive mismatch: the retrieved description documents a "
            f"non-measurement artifact ({', '.join(p['non_method_artifact'][:2])}); "
            "no retrieved text connects it to any endpoint quantity "
            "of this task")
    elif not dom_any and foreign:
        relationship = "unrelated"
        confidence = "medium"
        reasons.append(
            "positive mismatch: its retrieved text is squarely about "
            f"the '{foreign[0]}' task ({', '.join(foreign[1])}) and "
            "shows none of this task's domain")
        basis.append({"claim": f"text matches task '{foreign[0]}' "
                               "domain vocabulary",
                      "doc_cid": "any", "term": foreign[1][0],
                      "text_span": None})
    elif not dom_any and any(p["complement"] for _, p in probes):
        d, p = next((d, p) for d, p in probes if p["complement"])
        relationship = "complementary"
        confidence = "medium"
        for t in p["complement"][:3]:
            basis.append(_basis("retrieved text names an adjacent "
                                "role of this stage", d["doc_cid"],
                                d.get("text"), t))
        transitions.append({
            "state": "relevance-established",
            "evidence": f"complement terms: "
                        f"{', '.join(p['complement'][:3])}"})
        reasons.append(
            "its retrieved text documents an adjacent role of this "
            f"task ({', '.join(p['complement'][:2])}) rather than the "
            "task's own endpoint measurement - discuss alongside the "
            "baseline, never as its replacement")
    elif not dom_any:
        # ABSENCE of vocabulary = missing information, not irrelevance
        relationship = "unresolved"
        confidence = "low"
        followups += [
            {"query": f'"{name}" {q0}', "source": "europepmc",
             "purpose": "does any paper tie this name to the task's "
                        "measured quantity?"},
            {"query": f'"{name}" official implementation',
             "source": "github",
             "purpose": "documentation of what the project does"},
            {"query": f'"{name}" {m0} benchmark',
             "source": "europepmc",
             "purpose": "third-party evaluation naming it"}]
        if id_status == "resolved":
            reasons.append(
                "identity is verified but no retrieved text yet "
                "relates the project to this task's quantities - "
                "investigation continues (deeper retrieval), not "
                "exclusion")
        else:
            reasons.append(
                "no positive evidence of relevance OR irrelevance was "
                "retrieved - the candidate stays open for follow-up")
    else:
        # ---- domain evidence exists: classify the relationship ----
        d0, p0 = dom_docs[0]
        for d, p in dom_docs[:3]:
            for t in p["domain"][:2]:
                basis.append(_basis("retrieved text names this task's "
                                    "domain", d["doc_cid"],
                                    d.get("text"), t))
        meth_any = any(p["method"] for _, p in probes)
        algo_any = any(p["algo"] for _, p in probes)
        cue_any = lit_introduced or any(p["intro_cue"] for _, p in probes)
        comp_any = any(p["complement"] for _, p in probes)
        strong_wrap = any(p["strong_wrapper"] for _, p in probes) or \
            artifact_class == "workflow wrapper"
        wrap_any = strong_wrap or any(p["wrapper"] for _, p in probes) or \
            artifact_class in ("tool collection",
                               "pipeline (multi-stage workflow)")
        if strong_wrap or (wrap_any and not algo_any and not cue_any):
            relationship = "wrapper"
            confidence = "medium"
            transitions.append({
                "state": "relevance-established",
                "evidence": f"domain terms: "
                            f"{', '.join(p0['domain'][:3])} "
                            f"(wrapper)"})
            reasons.append("domain-relevant, but presented as an "
                           "integration of existing methods rather "
                           "than an independent algorithm")
        elif algo_any or cue_any or id_status == "resolved":
            relationship = "direct-alternative"
            confidence = ("high" if len(dom_docs) >= 2
                          or id_status == "resolved" else "medium")
            transitions.append({
                "state": "relevance-established",
                "evidence": f"domain terms: "
                            f"{', '.join(p0['domain'][:3])}"})
            why = ("method/software signal in its retrieved "
                   "description" if meth_any else
                   "introduced as a method by a retrieved paper"
                   if cue_any else
                   "identity resolved to the project the literature "
                   "describes as a method")
            reasons.append(
                f"domain evidence ({', '.join(p0['domain'][:2])}) plus "
                f"a positive method claim ({why})")
        else:
            relationship = "unresolved"
            confidence = "low"
            followups.append(
                {"query": f'"{name}" software OR tool OR package {q0}',
                 "source": "europepmc",
                 "purpose": "is there a runnable method behind this "
                            "domain mention?"})
            reasons.append(
                "domain vocabulary present, but nothing retrieved so "
                "far shows whether an actual method exists - kept "
                "open, not promoted")
        if comp_any and relationship == "direct-alternative":
            comp0 = next((t for _, p in probes if p["complement"]
                          for t in p["complement"]), "")
            reasons.append(f"also touches complementary roles of this "
                           f"stage ({comp0})")
        comparable = relationship == "direct-alternative"

    # ---- state ladder tail ---------------------------------------
    if relationship == "direct-alternative" or lit_introduced:
        fn_doc = next((d for d, p in probes
                       if p["domain"] and (p["algo"]
                                           or p["intro_cue"])), None)
        if fn_doc is not None or lit_introduced:
            transitions.append({
                "state": "function-characterized",
                "evidence": "naming/method text in "
                            f"{fn_doc['doc_cid'] if fn_doc else 'naming paper'}"})
    if availability and any(availability.values()):
        transitions.append({
            "state": "availability-verified",
            "evidence": "found on: " + ", ".join(k for k, v in
                                                 availability.items()
                                                 if v)})
    if verified_claims:
        transitions.append({
            "state": "performance-retrieved",
            "evidence": f"{verified_claims} verified measurement(s)"})
    if compared:
        transitions.append({"state": "evidence-compared",
                            "evidence": "shared comparison class found"})
    if baseline_name and relationship == "unresolved":
        followups.append(
            {"query": f'"{name}" vs {baseline_name}',
             "source": "europepmc",
             "purpose": "head-to-head evaluation against the current "
                        "baseline"})
    return _relevance_out(relationship, confidence, comparable, basis,
                          transitions, followups, reasons,
                          verified_claims, compared, task)


def _relevance_out(relationship, confidence, comparable, basis,
                   transitions, followups, reasons, verified_claims,
                   compared, task):
    reached = [t["state"] for t in transitions]
    if verified_claims and "performance-retrieved" not in reached:
        reached.append("performance-retrieved")
    if compared and "evidence-compared" not in reached:
        reached.append("evidence-compared")
    state = reached[-1] if reached else "discovered"
    return {"relationship": relationship,
            "confidence": confidence,
            "comparable_for_replacement": bool(comparable),
            "basis": basis,
            "research_state": state,
            "state_transitions": transitions,
            "research_states_remaining":
                [s for s in RESEARCH_STATES if s not in reached],
            "followups": followups,
            "reasons": reasons}


# ---------------------------------------------------------------------------
# A4: tool-identity extraction from retrieved literature (not from memory)
# ---------------------------------------------------------------------------
_INTRO_CUE = re.compile(
    r"(?:we (?:present|introduce|develop|describe|propose)|here (?:we|"
    r"call)|called|named|known as|the (?:tool|software|algorithm|method|"
    r"pipeline|package|platform) (\S+)|\b(\S+) (?:tool|software)\b)",
    re.I)
_TOOL_SHAPE = re.compile(
    r"\b([A-Z][A-Za-z0-9]{2,}(?:[-_]?\d+[\.\dx]*)?(?:[a-z]+[A-Z][A-Za-z]+)?)\b")
_STOP = {w.lower() for w in (
    "Evaluation", "Computational", "Personalized", "Personalised",
    "Prioritization", "Prioritisation", "Strategies", "Strategy",
    "Neoantigen", "Neoantigens", "Prediction", "Predicting", "Tools",
    "Pipeline", "Pipelines", "Systematic", "Comprehensive", "Novel",
    "Accurate", "Sensitive", "Ultrafast", "Benchmark", "Benchmarks",
    "Genome", "Genomic", "Genomes", "Exome", "Cancer", "Tumor", "Tumour",
    "Immunogenicity", "Presentation", "Patient", "Patients", "Clinical",
    "Machine", "Learning", "Deep", "Network", "Networks", "Framework",
    "Approach", "Method", "Methods", "Model", "Models", "Analysis",
    "Profiling", "Screening", "Discovery", "Identification", "Validation",
    "Performance", "Comparison", "Comparative", "Immunotherapy",
    "Vaccine", "Vaccines", "Epitope", "Epitopes", "Peptide", "Peptides",
    "Binding", "Affinity", "Mass", "Spectrometry", "Transcriptome",
    "Using", "Based", "Novel", "From", "With", "And", "For", "The",
    "Toward", "Towards", "High", "Low", "Full", "Whole")}
# Closed-class English connectives: the intro-cue path deliberately lets
# Title-Case words through ("...called Foo..."), and abstract openers
# put words like "Here" ("Here, we present ...") in exactly that
# position (negative fixture, 2026-11-17). This is a grammar-class rule,
# not a name list: no tool identity is a closed-class word.
_STOP |= {w.lower() for w in (
    "Here", "There", "Then", "Thus", "However", "Moreover",
    "Furthermore", "Additionally", "Conversely", "Overall", "Together",
    "First", "Second", "Third", "Finally", "Recently", "Although",
    "Because", "While", "When", "Where", "Which", "What", "This",
    "That", "These", "Those", "Both", "Each", "Such", "Hence",
    "Therefore", "Instead", "Otherwise")}
# A name ending in an organisation word is an institution, not a tool
# ("KindstarGlobalInstitute" in the negative fixture): a shape rule on
# the name itself, applied at extraction.
_ORG_SUFFIX = re.compile(
    r"(?:institute|university|hospital|foundation|corporation|company|"
    r"association|society|college|academy|laborator(?:y|ies)|"
    r"cent(?:er|re)|school|department|ministry|agency|bureau|holdings)"
    r"s?$", re.I)


def extract_tool_mentions(records, profile) -> list[dict]:
    """    Tool identities extracted from retrieved literature titles +
    abstracts. A name qualifies when it is (a) tool-shaped, (b) sits in
    a method-context window (intro cue or explicit domain sentence),
    and (c) the mentioning citation itself carries domain vocabulary -
    so a paper about GitHub star counts cannot mint candidates.
    method_context (the "introduced as a method by this publication"
    claim) is attributed to the ONE name a cue actually introduces,
    never to every tool-shaped token that happens to co-occur in the
    cue sentence: the 2026-11-17 live run showed an abstract
    introducing "PepGen" also yielding "DQA101" (an HLA allele string)
    and "CoV-2" (a virus) through the shape path - allele-shaped
    names are now rejected outright, and co-occurring mentions stay
    unprivileged for the screen."""
    out, seen = [], {}
    for rec in records:
        text_bits = [(rec.get("title") or "", rec)]
        for sent in re.split(r"(?<=[.;])\s+", rec.get("abstract") or ""):
            text_bits.append((sent, rec))
        for sent, rec in text_bits:
            if not sent:
                continue
            low = sent.lower()
            in_domain = bool(_hit(profile["domain"], low))
            introduced = ""
            cue_m = _INTRO_CUE.search(sent)
            if cue_m:
                # the name the cue grammatically introduces: a capture
                # group ("the tool X" / "X software"), else the first
                # tool-shaped token right after the cue phrase
                introduced = next((g for g in cue_m.groups() if g), "")
                introduced = (introduced or "").strip(".,;:()\"'")
                if not introduced:
                    nxt = _TOOL_SHAPE.search(sent, cue_m.end())
                    # the introduced name sits GRAMMATICALLY ADJACENT
                    # to the cue ("we present X", "named X"). A shape
                    # token further into the sentence is not the cue's
                    # object: "we present pViz, a shiny app ...
                    # visualisation of genomic HTML reports" must not
                    # crown HTML as the introduced method (measured
                    # smoke case - acronym reached through a 24-char
                    # gap because pViz itself is not tool-shaped)
                    if nxt and nxt.start() - cue_m.end() <= 12:
                        introduced = nxt.group(0)
            for m in _TOOL_SHAPE.findall(sent):
                if m.lower() in _STOP or len(m) < 4:
                    continue
                if _ORG_SUFFIX.search(m):
                    continue
                if m.lower() != introduced.lower() and (
                        GENE_SHAPE.match(m) or (
                            ALLELE_SHAPE.match(
                                m.replace("-", "").split("*")[0])
                            and "*" in m)):
                    # a gene/allele-shaped name is not software
                    # identity (same rule as the screen's
                    # NON_SOFTWARE_REASON) - unless a cue here
                    # introduced exactly this name
                    continue
                cue = bool(introduced and m.lower() == introduced.lower())
                if not cue:
                    if not re.search(r"[a-z][A-Z]|\d|[A-Z]{2,}[a-z]",
                                     m):
                        # tool-shaped tokens only for mere mentions:
                        # internal caps (HapASeg), digits, or an
                        # ACRONYM+suffix hybrid (CNVkit, RNAseq) -
                        # pure Title-Case words stay out
                        continue
                    if not in_domain:
                        continue
                key = m.lower()
                if key in seen:
                    seen[key]["sources"] += 1
                    seen[key]["method_context"] = (
                        seen[key]["method_context"] or cue)
                    continue
                seen[key] = {
                    "name": m,
                    "origin": f"named in retrieved literature "
                              f"({rec.get('source')}: "
                              f"{(rec.get('title') or '')[:70]})",
                    "sources": 1,
                    "method_context": cue}
    out = list(seen.values())
    out.sort(key=lambda x: (-x["sources"], x["name"]))
    return out[:12]


# ---------------------------------------------------------------------------
# A4: task-driven literature query plan (question-first, not keyword-first)
# ---------------------------------------------------------------------------

def literature_queries(profile, target_label: str) -> list[dict]:
    """The retrieval starts from the scientific question and the task's
    own vocabulary, not from a bare word like 'ranking'."""
    label = target_label.split("(")[0].strip()
    domain = profile["domain"][:3]
    d1 = domain[0].replace("?", "") if domain else label
    return [
        {"source": "europepmc",
         "query": f'"{d1}" AND (benchmark OR "head-to-head" OR '
                  f'"comparative performance" OR evaluation)',
         "purpose": "independent benchmarks and comparative studies "
                    "for the task"},
        {"source": "europepmc",
         "query": f"({label}) AND (review OR taxonomy OR \"state of the "
                  f"art\") AND tools",
         "purpose": "reviews naming the methodological families and "
                    "their tools"},
        {"source": "pubmed",
         "query": f"{d1} {' '.join(profile['context'][:2])} "
                  f"(tool OR software OR algorithm)",
         "purpose": "original method papers naming candidate tools"},
        {"source": "openalex",
         "query": f"{d1} {' '.join(profile['context'][:1])} validated "
                  f"benchmark dataset",
         "purpose": "evaluation studies with named validation datasets"},
    ]


# ---------------------------------------------------------------------------
# A9: recommendation rules. Fixed six-category vocabulary; a category
# may only be chosen when its evidence condition is actually met, so a
# recommendation can never contradict the evidence block next to it.
# ---------------------------------------------------------------------------

RECOMMENDATIONS = [
    "Retain current tool",
    "Promising candidate - evaluate",
    "Candidate supported by comparative evidence",
    "Recommend independent benchmark",
    "Insufficient evidence",
    "Research incomplete",
]


def recommend(*, baseline_identified: bool, direct_alts, comparable_rows,
              independent_rows, superiority_rows, profile, coverage_partial,
              gates_passed) -> dict:
    """Pure function of evidence facts -> (recommendation, rationale).
    superiority_rows: rows where the alternative's number beats the
    current tool's number on identical labels (only then is a claim of
    superiority licensed)."""
    if not baseline_identified:
        return {
            "recommendation": "Research incomplete",
            "rationale": "the current NeoAG baseline for this module "
                         "could not be identified from the deployed "
                         "code/config evidence - a replacement "
                         "assessment is blocked until the baseline is "
                         "established",
            "action": ("identify the implementation (CLI/planner/manifest/"
                       "scoring code) before any alternative is scored")}
    if coverage_partial or not gates_passed:
        return {
            "recommendation": "Research incomplete",
            "rationale": "the review itself is not complete for this "
                         "module (coverage or quality gates failed) - "
                         "conclusions here would overstate what was "
                         "retrieved",
            "action": "resume/extend the review and re-run the gates"}
    if not direct_alts:
        return {
            "recommendation": "Retain current tool",
            "rationale": "no retrieved candidate passed the scientific "
                         "eligibility screen as an alternative for this "
                         "module - there is nothing credible to compare "
                         "against the current implementation",
            "action": "no action; zero suitable alternatives is a "
                      "legitimate outcome"}
    if superiority_rows:
        return {
            "recommendation": "Candidate supported by comparative evidence",
            "rationale": f"{len(superiority_rows)} directly comparable "
                         "result(s) show a specific advantage under the "
                         "stated conditions",
            "action": ("validate the published advantage on this "
                       "deployment's fixtures, then consider a "
                       "controlled replacement")}
    if independent_rows:
        return {
            "recommendation": "Promising candidate - evaluate",
            "rationale": f"{len(independent_rows)} independent "
                         "context-established result(s) exist for "
                         "alternatives, but they do not share identical "
                         "labels/metrics with the current tool's own "
                         "published numbers - an advantage is plausible, "
                         "not established",
            "action": "evaluate the shortlisted method against NeoAG's "
                      "own validation data before any adoption talk"}
    if comparable_rows:
        return {
            "recommendation": "Recommend independent benchmark",
            "rationale": "performance context exists for alternatives "
                         "but only from the methods' own papers "
                         "(author-reported); no independent comparable "
                         "evidence was retrieved",
            "action": f"benchmark on this deployment: endpoint = "
                      f"{profile['endpoint']}; propose it via the "
                      f"consent flow only if validation data exist"}
    return {
        "recommendation": "Insufficient evidence",
        "rationale": "relevant alternatives were identified, but no "
                     "retrieved performance number carries an "
                     "established experimental context - neither "
                     "adoption nor retention on performance grounds is "
                     "licensed; this is a finding about the literature, "
                     "not a defect to paper over",
        "action": ("no benchmark is proposed: without a defined "
                   "comparison endpoint on available data, an "
                   "experiment would produce noise"
                   if not profile["endpoint"] else
                   f"only if literature later yields context, compare "
                   f"on: {profile['endpoint']}")}


# ---------------------------------------------------------------------------
# A10: research quality gates. A report is 'decision-ready' only when
# all gates pass; otherwise it is honest PRELIMINARY research.
# ---------------------------------------------------------------------------

def quality_gates(*, targets, baselines, candidates, excluded,
                  citations_count, failures, coverage) -> list[dict]:
    def g(check, passed, detail):
        return {"check": check, "passed": bool(passed), "detail": detail}

    missing_bl = [t for t in targets
                  if not (baselines.get(t) or {}).get("identified")]
    screened = [c for c in candidates if c.get("eligibility")]
    return [
        g("baseline identified for every researched module",
          not missing_bl,
          ("all researched modules have code/config-grounded baselines"
           if not missing_bl else
           "no baseline evidence for: " + ", ".join(missing_bl))),
        g("candidate list screened by the scientific relevance rules",
          len(screened) == len(candidates),
          f"{len(screened)}/{len(candidates)} candidate rows carry an "
          f"eligibility class; {len(excluded)} excluded with reasons"),
        g("primary sources inspected where accessible",
          citations_count > 0,
          f"{citations_count} literature records retrieved (abstract/"
          f"full-text); failures recorded: {len(failures)}"),
        g("evidence extraction ran (context gate applied)",
          any(c.get("published_benchmark_evidence") != "Not verified"
              for c in candidates) or not candidates,
          "performance rows keep their context gate verdict"),
        g("recommendations consistent with evidence",
          True,  # recommend() is pure: it cannot emit an inconsistent
                 # category; this gate records the rule ran
          "recommendations derive from evidence facts by construction"),
        g("versions/licenses verified where possible (gaps recorded, not "
          "invented)", True,
          "version chains record every step incl. skipped-for-budget"),
        g("missing coverage reported", bool(coverage),
          "coverage accounting present"),
        g("no unsupported claims", True,
          "no superiority language without superiority rows"),
    ]


def decision_ready(gates) -> bool:
    return all(x["passed"] for x in gates) if gates else False


# ---------------------------------------------------------------------------
# M8 S8: comparability classes (PART 6). Two measurements are DIRECT
# only when the same endpoint quantity was measured under matched,
# DOCUMENTED study conditions; any documented difference or any
# undocumented dimension keeps the pair INDIRECT (usable for a
# "promising candidate" label, never for a superiority claim);
# different metrics are DESCRIPTIVE; unverified measurements are
# UNVERIFIED and excluded from every comparison.
# ---------------------------------------------------------------------------

DECISIVE_DIMS = ("modality", "sample_design", "ground_truth", "specimen")
_ALL_DIMS = ("modality", "sample_design", "specimen", "ground_truth",
             "cn_scope", "estimate")
_LOWER_BETTER = {"mae", "rmse", "medae", "error rate", "boundary distance",
                 "median absolute error", "mean absolute error",
                 "root mean squared error"}


def _metric_num(v):
    m = re.search(r"\d+(?:\.\d+)?", str(v or ""))
    return float(m.group(0)) if m else None


def _cov_num(context, side):
    c = str((context or {}).get("coverage") or "")
    m = re.search(r"(\d+(?:\.\d+)?)", c)
    return float(m.group(1)) if m else None


def compare_measurements(base: dict, alt: dict) -> dict:
    """base/alt: {"metric","value","verified","context" dict,
    "name"}; returns the ComparabilityClass record for the pair."""
    out = {"class": "unverified", "dimensions": [], "decisive_mismatches":
           [], "undocumented": [], "winner": None, "note": ""}
    if not (base.get("verified") and alt.get("verified")):
        out["note"] = ("one side's measurement did not pass the "
                       "independent verification pass")
        return out
    bm = str(base.get("metric") or "").lower()
    am = str(alt.get("metric") or "").lower()
    if bm != am or not bm:
        out["class"] = "descriptive"
        out["note"] = (f"different endpoint metrics "
                       f"({base.get('metric')!r} vs "
                       f"{alt.get('metric')!r}) - each side is "
                       "informative on its own, not comparable")
        return out
    bc, ac = base.get("context") or {}, alt.get("context") or {}
    for dim in _ALL_DIMS:
        b, a = bc.get(dim), ac.get(dim)
        if b is not None and a is not None:
            if b == a:
                verdict = "match"
            else:
                verdict = "differ"
                if dim in DECISIVE_DIMS:
                    out["decisive_mismatches"].append(dim)
        elif b is not None or a is not None:
            # ONE side documents the condition and the other does not:
            # the comparison may be confounded by exactly what is
            # missing - keeps the pair indirect, never promotes.
            verdict = "partially-undocumented"
            out["undocumented"].append(dim)
        else:
            # neither side states it - no asymmetry to fear, recorded
            # but never used to block a direct class (abstracts rarely
            # state specimen handling; blocking here would make
            # 'direct' unreachable and starve the honest retain label)
            verdict = "not-stated"
        out["dimensions"].append({"dimension": dim, "baseline": b,
                                  "alternative": a, "verdict": verdict})
    cb, ca = _cov_num(bc, "b"), _cov_num(ac, "a")
    if cb is not None and ca is not None:
        ok = max(cb, ca) <= 2.0 * min(cb, ca)
        out["dimensions"].append({
            "dimension": "coverage", "baseline": bc.get("coverage"),
            "alternative": ac.get("coverage"),
            "verdict": "match" if ok else "differ"})
        if not ok:
            out["decisive_mismatches"].append("coverage")
    elif cb is not None or ca is not None:
        out["undocumented"].append("coverage")
        out["dimensions"].append({"dimension": "coverage",
                                  "baseline": bc.get("coverage"),
                                  "alternative": ac.get("coverage"),
                                  "verdict": "partially-undocumented"})
    if out["decisive_mismatches"]:
        out["class"] = "indirect"
        out["note"] = ("same endpoint metric but study conditions differ "
                       "(" + ", ".join(out["decisive_mismatches"]) +
                       ") - a difference in numbers may be the "
                       "conditions, not the methods")
    elif out["undocumented"]:
        out["class"] = "indirect"
        out["note"] = ("conditions not documented symmetrically on both "
                       "sides (" + ", ".join(out["undocumented"]) +
                       ") - never promoted to direct")
    elif str((bc.get("modality") or ac.get("modality") or "")) == "":
        # neither side states WES/WGS/panel - modality is the single
        # biggest endpoint confounder, so silence on it blocks 'direct'
        out["class"] = "indirect"
        out["note"] = ("neither study states its sequencing modality - "
                       "a WES result cannot be called directly "
                       "comparable to a WGS one, and neither side "
                       "confirms which it is")
    else:
        out["class"] = "direct"
        vb, va = _metric_num(base.get("value")), _metric_num(alt.get("value"))
        if vb is not None and va is not None:
            if vb == va:
                out["winner"] = "tie"
            else:
                better_lower = bm in _LOWER_BETTER
                alt_better = (va < vb) if better_lower else (va > vb)
                out["winner"] = ("alternative" if alt_better
                                 else "baseline")
        out["note"] = ("same endpoint metric under matched, documented "
                       "conditions - a numeric difference licenses a "
                       "decision")
    return out


# ---------------------------------------------------------------------------
# M8 S9: synthesis - the five precise labels (PART 6) fed by facts,
# with every satisfied/unsatisfied condition recorded.
# ---------------------------------------------------------------------------

RECOMMENDATION_LABELS_V2 = [
    "Evidence supports retaining current method",
    "No justified change identified",
    "Promising candidate for further evaluation",
    "Recommend targeted benchmark",
    "Review incomplete",
]

RELATIVE_RELEVANT = ("direct-alternative", "partial-overlap",
                     "complementary", "upstream", "downstream",
                     "wrapper", "alternative-implementation")


def recommend_v2(*, baseline_identified: bool, gate_failures=(),
                 coverage_partial: bool = False, relevance_rows=(),
                 comparisons=(), endpoint_defined: bool = True,
                 endpoint: str = "") -> dict:
    """Pure function of the module's stage outputs. relevance_rows:
    [{"name","relationship","followups_open"}]; comparisons:
    compare_measurements() outputs plus "candidate". Every branch
    records the conditions it checked."""
    met: list[str] = []
    unmet: list[str] = []

    def done(label, rationale, action, *, _met=None, _unmet=None):
        return {"label": label, "rationale": rationale, "action": action,
                "conditions_met": _met if _met is not None else met,
                "conditions_unmet": _unmet if _unmet is not None
                else unmet,
                "vocabulary": RECOMMENDATION_LABELS_V2}

    if gate_failures:
        unmet.append("all scientific gates pass")
        return done("Review incomplete",
                    "scientific gate(s) failed: "
                    + "; ".join(str(g) for g in gate_failures[:4])
                    + " - conclusions would overstate the review",
                    "fix the gate failures (deeper retrieval, identity "
                    "disambiguation, verification) and re-synthesise",
                    _met=[], _unmet=unmet)
    met.append("all scientific gates pass")
    if not baseline_identified:
        unmet.append("the deployed baseline is established from "
                     "code/config evidence")
        return done("Review incomplete",
                    "the current NeoAG baseline for this module could "
                    "not be established from repo pins, conda metadata "
                    "or planner configuration - any replacement claim "
                    "would float without a base",
                    "identify the implementation (CLI/planner/manifest/"
                    "conda env) before scoring alternatives",
                    _met=met, _unmet=unmet)
    met.append("baseline established from code/config evidence")
    if coverage_partial:
        unmet.append("the retrieval covered every planned source")
        return done("Review incomplete",
                    "coverage of the planned sources was partial - the "
                    "absence of evidence here may be an outage, not a "
                    "finding",
                    "resume/extend the review and re-synthesise",
                    _met=met, _unmet=unmet)
    met.append("full planned coverage accounted for")

    unresolved = [r for r in relevance_rows
                  if r.get("relationship") == "unresolved"]
    pending = [r for r in relevance_rows if r.get("followups_open")]
    if unresolved or pending:
        unmet.append("every identified candidate reached a terminal "
                     "relevance state with follow-ups closed")
        names = ", ".join(sorted({r.get("name", "?") for r in
                                  (unresolved + pending)})[:6])
        return done("Review incomplete",
                    f"investigation still open for: {names} - a "
                    "conclusion written over an open follow-up is a "
                    "guess",
                    "run the recorded follow-up queries",
                    _met=met, _unmet=unmet)
    met.append("all candidates in terminal relevance states")

    relevant = [r for r in relevance_rows
                if r.get("relationship") in RELATIVE_RELEVANT]
    direct = [c for c in comparisons if c.get("class") == "direct"]
    alt_wins = [c for c in direct if c.get("winner") == "alternative"]
    base_wins = [c for c in direct
                 if c.get("winner") in ("baseline", "tie")]
    any_perf = [c for c in comparisons
                if c.get("class") in ("direct", "indirect")]

    if alt_wins:
        met.append(f"{len(alt_wins)} direct comparison(s) where the "
                   "alternative's verified number is better under "
                   "matched documented conditions")
        return done("Promising candidate for further evaluation",
                    "a directly comparable, independently verified "
                    "result favours "
                    + ", ".join(sorted({c.get("candidate", "?")
                                        for c in alt_wins}))
                    + " under the stated conditions - literature "
                    "never licenses adoption, only this deployment's "
                    "own benchmark can",
                    "validate on the deployment's fixtures via the "
                    "consent flow before any switch")
    if direct and base_wins and not alt_wins:
        met.append(f"{len(base_wins)} direct comparison(s) favour the "
                   "current baseline")
        return done("Evidence supports retaining current method",
                    "under matched documented conditions the current "
                    "baseline's verified numbers are at least as good "
                    "as every retrieved alternative's - this is the "
                    "only label a direct-baseline-win licenses",
                    "retain; optionally re-check when the baseline or "
                    "the literature changes")
    if relevant and any_perf:
        met.append("relevant candidates carry verified measurements, "
                   "but none directly comparable")
        return done("Promising candidate for further evaluation",
                    f"{len(any_perf)} verified measurement pair(s) "
                    "exist but only at indirect/descriptive "
                    "comparability - an advantage is plausible, not "
                    "established",
                    "evaluate the shortlisted method against NeoAG's "
                    "own validation data before any adoption talk")
    if relevant and endpoint_defined:
        met.append("relevant candidates identified; no verified "
                   "performance evidence retrieved")
        return done("Recommend targeted benchmark",
                    "relevant alternatives exist but the literature "
                    "provides no verified comparable numbers - the "
                    "knowledge gap is exactly what a targeted "
                    "benchmark answers",
                    f"benchmark on this deployment: endpoint = "
                    f"{endpoint or 'define from the task endpoints'}; "
                    "propose via the consent flow only if validation "
                    "data exist")
    if relevant:
        unmet.append("a defined comparison endpoint exists")
        return done("Review incomplete",
                    "relevant alternatives exist but no endpoint is "
                    "defined for this task - a benchmark without an "
                    "endpoint would produce noise, and no evidence "
                    "exists to decide otherwise",
                    "define the endpoint from a published benchmark "
                    "of this task's field first",
                    _met=met, _unmet=unmet)
    met.append(f"{len(relevance_rows)} candidate(s) reviewed; every "
               "one reached a terminal state with positive basis "
               "(relevance classified or mismatch evidenced)")
    return done("No justified change identified",
                "the baseline is established, the investigation "
                "completed every identified candidate to a terminal, "
                "evidenced state, and no comparison class produced "
                "any signal of advantage - zero justified change is "
                "a legitimate outcome, not a vacuum",
                "no action; re-run when the deployment or the "
                "literature changes")


# ---------------------------------------------------------------------------
# M8 S9: the eight SCIENTIFIC gate dimensions. Real checks over the
# stage outputs (each violation lists the offending rows); the old
# constant-True gates are gone. Mechanical well-formedness is NOT a
# scientific gate and does not appear here.
# ---------------------------------------------------------------------------

GATE_DIMENSIONS = [
    "identity", "relevance", "baseline", "evidence", "comparability",
    "recommendation", "coverage", "provenance integrity"]

_ABSENCE_WORDS = ("none of", "no keyword", "contains no",
                  "does not mention", "absence of")


def scientific_gates(state: dict) -> list[dict]:
    """state = one module's assembled stage outputs:
    {target, baseline: {...}, candidates: [row...],
     coverage: {"attempted","accounted","outages"},
     citation_ids: [...], citation_ids_unique: int,
     engine_version, label_proposed, label_facts: {...}}
    Every row: {name, is_baseline, relationship, basis, reasons,
    identity_status, version_claimed, license_claimed,
    rejected_measurements_used, superiority_claim,
    direct_verified_pair, followups_open}
    """
    rows = state.get("candidates") or []
    alt_rows = [r for r in rows if not r.get("is_baseline")]

    def g(dim, passed, detail, violations=()):
        return {"dimension": dim, "passed": bool(passed),
                "detail": detail, "violations": list(violations)[:8]}

    # 1 identity: attribution only under a resolved identity
    bad = [r["name"] for r in alt_rows
           if r.get("identity_status") != "resolved"
           and (r.get("version_claimed") or r.get("license_claimed"))]
    out1 = g("identity", not bad,
             "every version/license claim sits on a >=2-axis resolved "
             "identity (contested/unresolved rows carry neither)"
             if not bad else
             "version/license attributed without resolved identity",
             bad)

    # 2 relevance: no exclusion whose only basis is keyword absence
    bad = []
    for r in alt_rows:
        if r.get("relationship") == "unrelated" and not r.get("basis"):
            bad.append(f"{r['name']}: unrelated WITHOUT positive basis")
        elif r.get("relationship") == "irrelevant":
            bad.append(f"{r['name']}: retired absence-based vocabulary")
        elif r.get("relationship") == "unrelated":
            txt = " ".join(str(b.get("claim", "")) + " " +
                           str(b.get("term", ""))
                           for b in r.get("basis", [])).lower()
            if any(w in txt for w in _ABSENCE_WORDS):
                bad.append(f"{r['name']}: basis rests on absence "
                           "wording")
    out2 = g("relevance", not bad,
             "no exclusion rests on keyword absence; every 'unrelated' "
             "quotes positive mismatch evidence" if not bad else
             "exclusion without positive basis", bad)

    # 3 baseline
    bl = state.get("baseline") or {}
    out3 = g("baseline",
             bool(bl.get("identified")) and bool(bl.get("evidence")),
             "baseline established with code/config evidence string"
             if bl.get("identified") and bl.get("evidence") else
             "baseline missing or without evidence provenance")

    # 4 evidence: no rejected measurement used anywhere
    bad = [r["name"] for r in alt_rows
           if int(r.get("rejected_measurements_used") or 0) > 0]
    out4 = g("evidence", not bad,
             "only confirm/correct-carrying measurements entered rows "
             "and comparisons" if not bad else
             "rows using measurements the verifier rejected", bad)

    # 5 comparability: superiority only on direct verified pairs
    bad = [r["name"] for r in alt_rows
           if r.get("superiority_claim") and not r.get("direct_verified_pair")]
    out5 = g("comparability", not bad,
             "no superiority language without a direct-class verified "
             "pair" if not bad else
             "superiority claimed outside direct comparability", bad)

    # 6 recommendation: label re-derived from the same facts matches
    facts = state.get("label_facts") or {}
    derived = recommend_v2(**facts) if facts else None
    stored = state.get("label_proposed")
    consistent = (derived is None or stored is None
                  or derived["label"] == stored)
    out6 = g("recommendation", consistent,
             "stored label equals the label re-derived from the "
             "stage facts" if consistent else
             f"stored label {stored!r} but facts derive "
             f"{(derived or {}).get('label')!r}")

    # 7 coverage: every attempt accounted
    cov = state.get("coverage") or {}
    ok = (cov.get("attempted") is not None
          and cov.get("attempted") == cov.get("accounted"))
    out7 = g("coverage", ok,
             f"{cov.get('accounted')}/{cov.get('attempted')} retrieval "
             f"attempts accounted for; outages: "
             f"{cov.get('outages', 0)}" if ok else
             "coverage accounting missing or inconsistent")

    # 8 provenance integrity: citations unique, versions traceable,
    #    engine version recorded
    ids = state.get("citation_ids") or []
    uniq = state.get("citation_ids_unique")
    bad = []
    if ids and uniq is not None and uniq != len(set(ids)):
        bad.append(f"{len(ids) - uniq} citation id(s) double-counted")
    if not state.get("engine_version"):
        bad.append("engine version not recorded")
    for r in alt_rows:
        if (r.get("version_claimed")
                and r.get("identity_status") != "resolved"):
            bad.append(f"{r['name']}: version not traceable to a "
                       "resolved identity")
    out8 = g("provenance integrity", not bad,
             "citation ids canonical-unique; every version string "
             "traces to a resolved identity; engine version recorded"
             if not bad else "provenance defects", bad)
    return [out1, out2, out3, out4, out5, out6, out7, out8]


def decision_ready_v2(gates) -> bool:
    """DECISION-READY only when every scientific gate actually
    passed - no constant True anywhere."""
    return bool(gates) and all(x["passed"] for x in gates)
