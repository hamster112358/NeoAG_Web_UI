"""Milestone-8: the scientific reliability chain, offline.

Covers the ten-stage architecture (docs/SCIENTIFIC_ARCHITECTURE_2026-11-17.md)
with deterministic fixtures only - no network, no live services, own temp
store. Sections:

  1  claims.py     measurement extraction + doc-absolute spans (S6)
  2  claimverify.py independent re-parse: confirms, corrects, rejects (S7)
  3  science.py    positive-evidence relevance + research-state ladder (S4)
  4  science.py    comparability classes + five-label synthesis + gates (S8/S9)
  5  identity.py   identity key merging (S3)
  6  science.py    tool-mention extraction hygiene (S1)
  7  research.py   FULL run_live chain with faked sources (E2E)

Run: python3 tests/verify_scientific.py
"""
import copy
import json
import os
import pathlib
import shutil
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_stub = types.ModuleType("backend.server")
_stub.publish = lambda ev: None
sys.modules.setdefault("backend.server", _stub)

PASS, FAIL = [], []


def chk(label, cond, extra=""):
    if cond:
        PASS.append(label)
        print("PASS", label)
    else:
        FAIL.append(label)
        print("FAIL", label, ("| " + str(extra)[:400]) if extra else "")


# ---------------------------------------------------------------- 1. claims
from backend import claims, claimverify  # noqa: E402

NAMES = ["PureCN", "PURPLE", "CNVkit", "ClinCNV"]
TEXT0 = ("Native standalone PureCN yielded 16.9% sensitivity and "
         "95.5% specificity at absCN \u2265 14;")
TEXT1 = ("ClinCNV yielded sensitivity/specificity of 67.8%/85.1%, native "
         "PureCN 16.9%/95.5% at absCN \u2265 14, and PURPLE 58.6%/92.3%.")
TEXT2 = ("<b>Results:</b> On the development cohort, the fixed CNVkit "
         "median-bin log2 rule yielded 76.3% sensitivity (Wilson 95% CI, "
         "64.0-85.3%) and 91.0% specificity (81.8-95.8%), with F1 81.8% "
         "and \u03ba = 0.679.")


def meas_of(t, subject):
    out = {}
    for c in claims.extract_claims(t, doc_cid="X", level="abstract",
                                   known_names=NAMES):
        if c["subject"] == subject:
            assert not c["ambiguous"], c
            for m in c["measurements"]:
                out[m["metric"]] = m["value"]
    return out


m = meas_of(TEXT0, "purecn")
chk("S6 PureCN 16.9/95.5 extracted",
    m.get("sensitivity") == "16.9%" and m.get("specificity") == "95.5%", m)
m = meas_of(TEXT1, "clincnv")
chk("S6 ClinCNV positional pair",
    m.get("sensitivity") == "67.8%" and m.get("specificity") == "85.1%", m)
m = meas_of(TEXT1, "purple")
chk("S6 PURPLE frame pair 58.6/92.3 (not PureCN's numbers)",
    m.get("sensitivity") == "58.6%" and m.get("specificity") == "92.3%", m)
m = meas_of(TEXT1, "purecn")
chk("S6 PureCN frame pair inside shared sentence",
    m.get("sensitivity") == "16.9%" and m.get("specificity") == "95.5%", m)
c_cnk = [c for c in claims.extract_claims(TEXT2, doc_cid="X",
        level="abstract", known_names=NAMES) if c["subject"] == "cnvkit"]
chk("S6 CNVkit with CI spans",
    len(c_cnk) == 1 and
    {(x["metric"], x["value"]) for x in c_cnk[0]["measurements"]} ==
    {("sensitivity", "76.3%"), ("specificity", "91.0%"),
     ("F1", "81.8%")} and
    all(x.get("ci") for x in c_cnk[0]["measurements"]
        if x["metric"] != "F1"),
    [(x["metric"], x["value"], x.get("ci"))
     for x in c_cnk[0]["measurements"]] if c_cnk else "no claim")
# doc-absolute spans (regression: spans used to be sentence-relative and
# landed on wrong offsets as soon as a document had more than one sentence)
DOC_MULTI = ("Earlier methods were noisy. " + TEXT0 + " Later work "
             "improved on this.")
spans_ok = True
for c in claims.extract_claims(DOC_MULTI, doc_cid="X", level="abstract",
                               known_names=NAMES):
    if DOC_MULTI[c["subject_span"][0]:c["subject_span"][1]].lower() \
            != c["subject"]:
        spans_ok = False
    for mm in c["measurements"]:
        if DOC_MULTI[mm["value_span"][0]:mm["value_span"][1]] != mm["value"]:
            spans_ok = False
chk("S6 spans are doc-absolute (multi-sentence doc)", spans_ok)

# ------------------------------------------------------------- 2. claimverify
bad = []
for t in (TEXT0, TEXT1, TEXT2):
    cs = claims.extract_claims(t, doc_cid="X", level="abstract",
                               known_names=NAMES)
    for c, v in zip(cs, claimverify.verify_claims(t, cs, NAMES)):
        for mm, mv in zip(c["measurements"], v["measurement_verdicts"]):
            if mv["verdict"] != "confirm":
                bad.append((c["subject"], mm["metric"], mv["verdict"],
                            mv["note"][:60]))
chk("S7 true extractions all confirm", not bad, bad)

cs0 = claims.extract_claims(TEXT0, doc_cid="X", level="abstract",
                            known_names=NAMES)
cs1 = claims.extract_claims(TEXT1, doc_cid="X", level="abstract",
                            known_names=NAMES)


def mutate(cls, fn):
    c2 = copy.deepcopy(cls)
    fn(c2)
    return c2


MUTS = [
    (TEXT0, mutate(cs0[0], lambda c: c.update(subject="purple")),
     {"correct"}, "subject -> name absent from sentence"),
    (TEXT0, mutate(cs0[0], lambda c: c["measurements"][0].update(
        metric="specificity")), {"correct"}, "metric swap"),
    (TEXT0, mutate(cs0[0], lambda c: c["measurements"][0].update(
        value="99.9%")), {"reject"}, "fabricated value"),
    (TEXT0, mutate(cs0[0], lambda c: c["measurements"][0]["value_span"]
                   .__setitem__(1, 30)), {"reject"}, "broken span"),
    (TEXT1, mutate([c for c in cs1 if c["subject"] == "purecn"][0],
                   lambda c: c.update(subject="purple")), {"correct"},
     "frame subject moved to farther name"),
    (TEXT1, mutate([c for c in cs1 if c["subject"] == "purple"][0],
                   lambda c: c["measurements"][0].update(
                       metric="accuracy")), {"reject"},
     "frame metric not in text"),
]
missed = []
for doc, c, want, label in MUTS:
    v = claimverify.verify_measurement(doc, c, c["measurements"][0], NAMES)
    if v["verdict"] not in want:
        missed.append((label, v["verdict"], v["note"][:60]))
chk("S7 every mutation caught", not missed, missed)

# ------------------------------------------------------------ 3. relevance S4
from backend import ontology, science  # noqa: E402

TASK = ontology.task_for("purity_cnv")


def rel(name, docs, **kw):
    return science.assess_relevance(name, docs, task=TASK,
                                    task_id="purity_cnv", **kw)


CASES = [
    ("no evidence stays unresolved/discovered",
     ("GhostTool", []),
     lambda r: r["relationship"] == "unresolved"
     and r["research_state"] == "discovered"),
    ("review-only domain mention: unresolved, NOT relevance-established",
     ("ReviewX", [{"doc_cid": "C1", "kind": "citation-abstract",
       "text": "We review methods that estimate tumour purity and copy "
               "number on TCGA cohort data."}]),
     lambda r: r["relationship"] == "unresolved"
     and r["research_state"] == "discovered"),
    ("complement-only -> complementary",
     ("DeconvX", [{"doc_cid": "C1", "kind": "citation-abstract",
       "text": "Our subclone deconvolution framework refines clonal "
               "structure; variant allele frequency modeling."}]),
     lambda r: r["relationship"] == "complementary"),
    ("method paper -> direct-alternative + function-characterized",
     ("PureCN", [{"doc_cid": "C1", "kind": "citation-abstract",
        "text": "We present PureCN, a method for purity estimation and "
                "copy number calling from exome tumor sequencing data."}]),
     lambda r: r["relationship"] == "direct-alternative"
     and r["research_state"] == "function-characterized"
     and r["comparable_for_replacement"],
     {"identity": {"status": "resolved",
                   "resolved_project": "github.com/marcho/purecn",
                   "n_axes": 3}}),
    ("viz library -> unrelated WITH positive basis",
     ("facets", [{"doc_cid": "G1", "kind": "github-description",
       "text": "HTML bindings for interactive HTML elements to display "
               "data frames - a data visualization library and notebook "
               "widget."}]),
     lambda r: r["relationship"] == "unrelated" and len(r["basis"]) > 0),
    ("other field's vocabulary -> unrelated",
     ("NetGuard", [{"doc_cid": "C2", "kind": "citation-abstract",
        "text": "A cybersecurity framework for network intrusion "
                "detection."}]),
     lambda r: r["relationship"] == "unrelated"),
    ("foreign task text -> unrelated",
     ("FusionX", [{"doc_cid": "C3", "kind": "citation-abstract",
       "text": "Gene fusion detection from RNA-seq: chimeric transcript "
               "assembly with breakpoint calling."}]),
     lambda r: r["relationship"] == "unrelated"),
    ("wrapper stays wrapper, not an alternative",
     ("WrapCNV", [{"doc_cid": "R1", "kind": "github-description",
       "text": "Snakemake wrapper that runs purity estimation copy "
               "number callers as a pipeline."}]),
     lambda r: r["relationship"] == "wrapper"
     and r["research_state"] != "function-characterized"),
    ("mixed-case tool name minted from own paper -> direct",
     ("HapASeg", [{"doc_cid": "C9", "kind": "citation-abstract",
       "text": "HapASeg: we present a haplotype-aware method for "
               "allele-specific copy number segmentation from tumor "
               "sequencing."}]),
     lambda r: r["relationship"] == "direct-alternative"
     and r["research_state"] == "function-characterized",
     {"lit_introduced": True}),
    ("shared-paper wrapper vocabulary does NOT contaminate this row",
     ("PureCN", [{"doc_cid": "P1", "kind": "fulltext",
       "text": "Benchmarking copy number callers. PureCN estimates "
               "purity and copy number from targeted sequencing. "
               "ToolX was executed within the Galaxy pipeline using a "
               "Snakemake wrapper around its web interface. All "
               "methods called copy number variants on the same "
               "samples."}]),
     lambda r: r["relationship"] != "wrapper"
     and r["relationship"] == "direct-alternative"),
    ("the SAME paper's wrapper row still reads wrapper (no over-sweep)",
     ("ToolX", [{"doc_cid": "P1", "kind": "fulltext",
       "text": "Benchmarking copy number callers. PureCN estimates "
               "purity and copy number from targeted sequencing. "
               "ToolX was executed within the Galaxy pipeline using a "
               "Snakemake wrapper around its web interface. All "
               "methods called copy number variants on the same "
               "samples."}]),
     lambda r: r["relationship"] == "wrapper"),
    ("pronoun continuation describes the tool (it wraps ...)",
     ("SegFlowCNV", [{"doc_cid": "G7", "kind": "github-description",
       "text": "SegFlowCNV: copy number segmentation for exomes. It "
               "wraps existing callers behind a Nextflow web "
               "interface."}]),
     lambda r: r["relationship"] == "wrapper"),
]
for item in CASES:
    label, args, pred = item[0], item[1], item[2]
    kw = item[3] if len(item) > 3 else {}
    r = rel(*args, **kw)
    chk("S4 " + label, pred(r), r)

chk("S6 capability frame covers predicate inflections",
    bool(science._FUNC_CLAIM_RX.search(
        "Xyz achieved accurate segmentation of tumor genomes."))
    and bool(science._FUNC_CLAIM_RX.search(
        "Xyz provides copy number segmentation."))
    and bool(science._FUNC_CLAIM_RX.search(
        "Xyz enabled detection of focal deletions at scale."))
    and bool(science._FUNC_CLAIM_RX.search(
        "Xyz: inference of allele-specific copy number profiles.")),
    "inflection gap (2026-11-17: 'achieved' missed)")

# --------------------------------------------------------- 4. comparability S8
mk = lambda m_, v, ver, ctx, name="x": {  # noqa: E731
    "metric": m_, "value": v, "verified": ver, "context": ctx,
    "name": name}
C1 = {"modality": "WES", "sample_design": "matched-normal",
      "ground_truth": "cell-line", "cn_scope": "allele-specific",
      "estimate": "segmentation", "coverage": "~50x"}
r = science.compare_measurements(
    mk("sensitivity", "95.5%", False, {}),
    mk("sensitivity", "76.3%", True, {}))
chk("S8 unverified side -> unverified", r["class"] == "unverified", r)
r = science.compare_measurements(
    mk("sensitivity", "95.5%", True, {}),
    mk("specificity", "76.3%", True, {}))
chk("S8 different metrics -> descriptive", r["class"] == "descriptive", r)
r = science.compare_measurements(
    mk("sensitivity", "76.3%", True, dict(C1), name="base"),
    mk("sensitivity", "95.5%", True, dict(C1), name="alt"))
chk("S8 identical conditions, higher-better -> direct, alt wins",
    r["class"] == "direct" and r["winner"] == "alternative", r)
r = science.compare_measurements(
    mk("MAE", "0.021", True, dict(C1), name="base"),
    mk("MAE", "0.031", True, dict(C1), name="alt"))
chk("S8 lower-better metric -> baseline wins",
    r["class"] == "direct" and r["winner"] == "baseline", r)
c2 = dict(C1)
c2["modality"] = "WGS"
r = science.compare_measurements(
    mk("sensitivity", "76.3%", True, dict(C1)),
    mk("sensitivity", "95.5%", True, c2))
chk("S8 decisive mismatch -> indirect",
    r["class"] == "indirect" and "modality" in r["decisive_mismatches"], r)
c3 = dict(C1)
del c3["ground_truth"]
r = science.compare_measurements(
    mk("sensitivity", "76.3%", True, dict(C1)),
    mk("sensitivity", "95.5%", True, c3))
chk("S8 undocumented decisive dim -> indirect, not decisive",
    r["class"] == "indirect" and "ground_truth" in r["undocumented"]
    and not r["decisive_mismatches"], r)
r = science.compare_measurements(
    mk("sensitivity", "76.3%", True, {"modality": "WES"}),
    mk("sensitivity", "95.5%", True, {"modality": "WES"}))
chk("S8 minimal stated modality -> direct", r["class"] == "direct", r)
r = science.compare_measurements(
    mk("sensitivity", "76.3%", True, {}),
    mk("sensitivity", "95.5%", True, {}))
chk("S8 modality silent -> never direct", r["class"] == "indirect", r)

# ------------------------------------------------------- 4b. labels + gates S9
okrow = {"name": "A", "relationship": "direct-alternative",
         "followups_open": 0}
F = dict(baseline_identified=True, relevance_rows=[okrow],
         comparisons=[{"class": "direct", "winner": "alternative",
                       "candidate": "A"}], endpoint="purity MAE")
LBL = {"promising": "Promising candidate for further evaluation",
       "retain": "Evidence supports retaining current method",
       "benchmark": "Recommend targeted benchmark",
       "incomplete": "Review incomplete",
       "none": "No justified change identified"}
chk("S9 verified direct pair, alt wins -> Promising",
    science.recommend_v2(**F)["label"] == LBL["promising"])
chk("S9 baseline wins -> Retain",
    science.recommend_v2(**dict(F, comparisons=[{"class": "direct",
        "winner": "baseline", "candidate": "A"}]))["label"]
    == LBL["retain"])
chk("S9 no performance -> targeted benchmark",
    science.recommend_v2(**dict(F, comparisons=[]))["label"]
    == LBL["benchmark"])
chk("S9 no defined endpoint -> Review incomplete",
    science.recommend_v2(**dict(F, comparisons=[],
        endpoint_defined=False))["label"] == LBL["incomplete"])
chk("S9 open followups -> Review incomplete",
    science.recommend_v2(**dict(F, comparisons=[], relevance_rows=[
        {"name": "A", "relationship": "unresolved",
         "followups_open": 2}]))["label"] == LBL["incomplete"])
chk("S9 all terminal irrelevant -> No justified change",
    science.recommend_v2(**dict(F, comparisons=[], relevance_rows=[
        {"name": "A", "relationship": "unrelated",
         "followups_open": 0}]))["label"] == LBL["none"])
chk("S9 gate failure forces Review incomplete",
    science.recommend_v2(**dict(F, comparisons=[], relevance_rows=[
        {"name": "A", "relationship": "unrelated", "followups_open": 0}],
        gate_failures=["identity: x"]))["label"] == LBL["incomplete"])
d = science.recommend_v2(**F)
chk("S9 decision records conditions + vocabulary",
    d["conditions_met"] and "vocabulary" in d)

row_ok = {"name": "A", "is_baseline": False,
          "relationship": "direct-alternative",
          "basis": [{"claim": "domain", "term": "purity"}],
          "reasons": [], "identity_status": "resolved",
          "version_claimed": "2.0", "license_claimed": "BSD",
          "rejected_measurements_used": 0, "superiority_claim": False,
          "direct_verified_pair": True, "followups_open": 0}
FACTS = dict(baseline_identified=True, relevance_rows=[okrow],
             comparisons=[{"class": "direct", "winner": "baseline",
                           "candidate": "A"}], endpoint="purity MAE")
STATE = {"target": "purity_cnv",
         "baseline": {"identified": True, "evidence": "conda pin 3.2.0"},
         "candidates": [dict(row_ok)],
         "coverage": {"attempted": 12, "accounted": 12, "outages": 0},
         "citation_ids": ["DOI:1", "PMID:2"], "citation_ids_unique": 2,
         "engine_version": "live-retrieval-2026.11-m8-r1",
         "label_proposed": LBL["retain"], "label_facts": FACTS}
gs = science.scientific_gates(STATE)
chk("S9 eight gate dimensions recorded",
    [x["dimension"] for x in gs] == science.GATE_DIMENSIONS, gs)
chk("S9 clean state passes every gate", all(x["passed"] for x in gs),
    [x for x in gs if not x["passed"]])
chk("S9 decision_ready_v2 agrees", science.decision_ready_v2(gs))
bad_state = dict(STATE)
bad_state["candidates"] = [dict(row_ok, identity_status="contested",
    version_claimed="1.0.1", superiority_claim=True,
    direct_verified_pair=False, rejected_measurements_used=2)]
bad_state["citation_ids"] = ["DOI:1", "DOI:1"]
bad_state["citation_ids_unique"] = 1
bad_state["coverage"] = {"attempted": 12, "accounted": 11, "outages": 1}
bad_state["label_facts"] = dict(FACTS, relevance_rows=[
    {"name": "A", "relationship": "unresolved", "followups_open": 1}])
bad_state["label_proposed"] = LBL["none"]
gs = science.scientific_gates(bad_state)
fails = {x["dimension"] for x in gs if not x["passed"]}
chk("S9 contaminated state fails the right gates",
    {"identity", "evidence", "comparability", "recommendation",
     "coverage", "provenance integrity"} <= fails, sorted(fails))
chk("S9 contaminated state is not decision-ready",
    not science.decision_ready_v2(gs))
rs = dict(STATE)
rs["candidates"] = [dict(row_ok, relationship="unrelated", basis=[],
                         identity_status="resolved")]
rel_g = next(x for x in science.scientific_gates(rs)
             if x["dimension"] == "relevance")
chk("S9 relevance gate catches basisless 'irrelevant'",
    not rel_g["passed"], rel_g)
rs["candidates"] = [dict(row_ok, relationship="unrelated", basis=[
    {"claim": "the retrieved description contains none of this module's "
              "domain vocabulary", "term": ""}],
    identity_status="resolved")]
rel_g = next(x for x in science.scientific_gates(rs)
             if x["dimension"] == "relevance")
chk("S9 relevance gate rejects absence-of-keyword wording",
    not rel_g["passed"], rel_g)

# ------------------------------------------------------------- 5. identity S3
from backend import identity  # noqa: E402

k_url = identity._norm_key("proj", "github.com/etal/cnvkit")
k_bare = identity._norm_key("proj", "etal/cnvkit")
chk("S3 bare owner/repo key merges with github URL key",
    k_url == k_bare == "gh:etal/cnvkit", (k_url, k_bare))
ident = identity.resolve_identity(
    "CNVkit", TASK,
    registry_hits=[{"id": "cnvkit", "title": "CNVkit",
                    "url": "https://github.com/etal/cnvkit",
                    "summary": "DNA copy number detection",
                    "license": "Apache-2.0", "source": "bioconda"}],
    github_hits=[{"id": "etal/cnvkit",
                  "url": "https://github.com/etal/cnvkit",
                  "description": "DNA copy number profiling for "
                                 "research exomes and targeted panels",
                  "license": "MIT", "stars": 600, "archived": False}])
chk("S3 registry link + github hit merge -> resolved",
    ident["status"] == "resolved"
    and not identity.blocks_attribution(ident),
    (ident["status"], [c.get("project") for c in
                       ident.get("considered") or []]))
ident_home = identity.resolve_identity(
    "CNVkit", TASK,
    registry_hits=[{"id": "cnvkit", "title": "cnvkit",
                    "url": "https://anaconda.org/bioconda/cnvkit",
                    "home": "https://github.com/etal/cnvkit",
                    "summary": "Copy number variant detection",
                    "version": "0.9.14", "source": "bioconda"}],
    github_hits=[{"id": "etal/cnvkit",
                  "url": "https://github.com/etal/cnvkit",
                  "description": "DNA copy number profiling for "
                                 "research exomes and targeted panels",
                  "stars": 600, "archived": False}])
chk("S3 registry home URL keyed on project, not the registry page "
    "(anaconda.org record corroborates the github project)",
    ident_home["status"] == "resolved" and ident_home.get("n_axes") == 2,
    (ident_home["status"], ident_home.get("n_axes"),
     [c.get("project") for c in ident_home.get("considered") or []]))
_AB_STG = ("STGBench: a benchmark for somatic copy number detection. "
           "The suite is available at https://github.com/Icarus200110/"
           "STGBench. It evaluates callers on CNVs in simulated "
           "cohorts.")
_stg_cit = [{"id": "pmc:stg", "title": "", "abstract": _AB_STG}]
stg = identity.resolve_identity(
    "STGBench", TASK, citations=_stg_cit,
    extra_evidence=[{"axis": "naming-paper",
                     "detail": "paper presents STGBench"}],
    github_hits=[{"id": "Icarus200110/STGBench", "description": "",
                  "stars": 1, "archived": False}])
_stg_merged = [c for c in (stg.get("considered") or [])
               if {"literature-url", "github-name-hit"}
               <= set(c.get("axes") or {})]
chk("S3 sentence-final period trimmed from paper URL so it merges "
    "with the github hit (2026-11-17: 'STGBench.' split the project)",
    stg["status"] == "resolved" and bool(_stg_merged),
    (stg["status"], [c.get("project") for c in
                     stg.get("considered") or []]))
cnvs = identity.resolve_identity(
    "CNVs", TASK, citations=_stg_cit,
    component_probe=lambda proj, name: True)
chk("S3 generic plural acronym must NOT inherit another tool's "
    "availability URL or get component-probed through it",
    cnvs["status"] != "resolved" and "component" not in
    "".join("".join((c.get("axes") or {}).keys())
            for c in cnvs.get("considered") or []),
    (cnvs["status"], [c.get("project") for c in
                      cnvs.get("considered") or []]))
hm = identity.resolve_identity(
    "PURPLE", TASK,
    citations=[{"id": "doi:hm", "title": "",
                "abstract": "PURPLE is part of the hmftools suite, "
                            "available at https://github.com/hmftools/"
                            "solutions for purity and ploidy."}],
    extra_evidence=[{"axis": "naming-paper",
                     "detail": "paper presents PURPLE/hmftools"}])
chk("S3 org-style mention stays valid when name and URL share the "
    "sentence (PURPLE-inside-hmftools class)",
    hm["status"] == "resolved" and "hmftools" in
    str(hm.get("resolved_project")),
    (hm["status"], hm.get("resolved_project")))
coll = identity.resolve_identity(
    "PURPLE", TASK,
    registry_hits=[{"id": "purple", "title": "purple",
                    "url": "https://pypi.org/project/purple",
                    "summary": "color palette generation",
                    "license": "MIT", "source": "pypi"}],
    github_hits=[{"id": "parkerlab/purple",
                  "url": "https://github.com/parkerlab/purple",
                  "description": "purity ploidy estimation",
                  "stars": 30, "archived": False}])
chk("S3 same-name collision from different projects does not resolve",
    coll["status"] != "resolved" and identity.blocks_attribution(coll),
    (coll["status"], [c.get("project") for c in
                      coll.get("considered") or []]))

# ------------------------------------------------------- 6. mention hygiene S1
pprof = science.profile_for("presentation", "presentation")
rec = {"title": "PepGen: conditional generation of peptides for MHC "
                "binding",
       "abstract": "Here we present PepGen, a tool for prioritizing "
                   "candidate peptides. We evaluated binding to "
                   "HLA-DQA1*01 alleles and validated SARS-CoV-2 "
                   "specific peptides with binding assays.",
       "source": "europepmc"}
mm = {x["name"]: x for x in science.extract_tool_mentions([rec], pprof)}
chk("S1 the cue introduces the tool, method_context on PepGen only",
    mm.get("PepGen", {}).get("method_context") is True
    and not any(v.get("method_context") for k, v in mm.items()
                if k != "PepGen"),
    {k: v.get("method_context") for k, v in mm.items()})
chk("S1 allele-shaped names never minted; gene-shaped never crowned",
    "DQA101" not in mm and "*" not in "".join(mm)
    and not any(science.GENE_SHAPE.match(k) for k in mm), list(mm))
cprof = science.profile_for("purity_cnv", "Purity/CNV")
rec2 = {"title": "pViz: an interactive dashboard",
        "abstract": "We present pViz, a shiny app that renders genomic "
                    "copy number HTML reports for exploration.",
        "source": "europepmc"}
mm2 = {x["name"]: x for x in science.extract_tool_mentions([rec2], cprof)}
chk("S1 intro-cue object is grammatically adjacent (no HTML crown)",
    not any(v.get("method_context") for v in mm2.values())
    and "HTML" not in mm2,
    {k: v.get("method_context") for k, v in mm2.items()})
rec3 = [{"title": "CNVkit: genome-wide copy number detection",
         "abstract": "CNVkit uses hybridisation capture panels to "
                     "detect copy number alterations.",
         "source": "europepmc"}]
mm3 = {x["name"] for x in science.extract_tool_mentions(rec3, cprof)}
chk("S1 acronym+suffix hybrid names are minted", "CNVkit" in mm3,
    sorted(mm3))
sc = science.screen_candidate(
    "CoV-2", "PepGen: conditional generation of peptides for MHC "
             "binding", profile=pprof, has_naming_source=False)
chk("S1 virus token gets no eligibility claim",
    sc["eligibility"] == "insufficient information to classify"
    and not sc["comparable_for_replacement"], sc["eligibility"])

# ------------------------------------ 6b. one paper corroborates ONCE
from backend import research as _research  # noqa: E402

_AB = ("BenchX benchmarks callers. CNVkit yielded 76.3% sensitivity "
       "and 91.0% specificity on cohort A.")
_FT = _AB + " No further results."
_docs2 = [{"doc_cid": "10.3390/x", "kind": "citation-abstract",
           "text": _AB, "citation": {"title": "t"}},
          {"doc_cid": "pmc:pmc9", "kind": "fulltext",
           "text": _FT, "citation": {"title": "t"}}]
_rows0, _ = _research._verified_measurements("CNVkit", _docs2,
                                             {"cnvkit"})
_n0 = len([r for r in _rows0 if r.get("value")])
_docs2[0]["superseded_by"] = "pmc:pmc9"
_rows1, _ = _research._verified_measurements("CNVkit", _docs2,
                                             {"cnvkit"})
_n1 = len([r for r in _rows1 if r.get("value")])
chk("S6/S7 abstract of a paper whose full text is in hand counts "
    "ONCE (2026-11-17: rows duplicated via DOI + PMCID cids)",
    _n0 == 2 * _n1 and _n1 >= 2, (_n0, _n1))

# ------------------------------------------------------------- 7. E2E run_live
REC_A = {"id": "epmc|A", "source": "europepmc",
         "doi": "10.1000/bench.2024", "pmcid": "",
         "title": "Benchmark of tumor purity and copy number callers "
                  "on simulated cohorts", "year": 2024,
         "url": "https://doi.org/10.1000/bench.2024",
         "abstract": "We benchmarked CNVkit and PureCN for tumor purity "
                     "and copy number estimation on simulated whole-"
                     "exome cohorts with matched normal truth. CNVkit "
                     "achieved a sensitivity of 76.3% and a specificity "
                     "of 95.5%. PureCN achieved a sensitivity of 58.6% "
                     "and a specificity of 92.3%."}
REC_B = {"id": "epmc|B", "source": "europepmc",
         "doi": "10.1000/purecn", "pmcid": "PMC111",
         "title": "PureCN: normalization and segmentation of copy number "
                  "data", "year": 2016,
         "url": "https://doi.org/10.1000/purecn",
         "abstract": "We present PureCN, an R package for purity and "
                     "copy number calling from targeted or whole-exome "
                     "sequencing data. PureCN attained a sensitivity of "
                     "61.2% on cell-line whole-exome benchmarks with "
                     "matched normals."}
REC_C = {"id": "epmc|C", "source": "europepmc",
         "doi": "10.1000/visualise", "pmcid": "",
         "title": "pViz: an interactive dashboard for genomic "
                  "visualisation", "year": 2023,
         "url": "https://doi.org/10.1000/visualise",
         "abstract": "We present pViz, a shiny app and dashboard for "
                     "interactive visualisation of genomic HTML "
                     "reports."}
GH = {
    "cnvkit": [{"id": "etal/cnvkit",
                "url": "https://github.com/etal/cnvkit",
                "description": "DNA copy number profiling for research "
                               "exomes and targeted panels",
                "license": "MIT", "pushed_at": "2026-01-01",
                "stars": 600, "archived": False}],
    "purecn": [{"id": "mbmooers/purecn",
                "url": "https://github.com/mbmooers/purecn",
                "description": "Purity and copy number estimation from "
                               "exome sequencing",
                "license": "Artistic-2.0", "pushed_at": "2026-02-01",
                "stars": 200, "archived": False}],
    "pviz": [{"id": "viz/pviz", "url": "https://github.com/viz/pviz",
              "description": "shiny dashboard for genomic visualisation "
                             "reports", "license": "MIT",
              "pushed_at": "2025-09-01", "stars": 5, "archived": False}],
}


def _faked_sources():
    from backend import sources

    def adapter(q, n):
        return [dict(REC_A), dict(REC_B), dict(REC_C)], None, {}

    def empty(term, n=1, **kw):
        return [], "not-found (fixture)", {}

    sources.ALL_ADAPTERS = {"europepmc": adapter, "pubmed": adapter,
                            "openalex": adapter}
    sources.github_repo_search = lambda t, n=3: (
        GH.get(t.lower(), [])[:n], None, {})
    sources.pypi_package = lambda s, n=1: ([], "not-on-pypi (fixture)",
                                           {})
    sources.biotools_search = lambda t, n=2: (
        [{"id": "cnvkit", "title": "CNVkit",
          "url": "https://github.com/etal/cnvkit",
          "summary": "CNVkit is a software tool for DNA copy number "
                     "detection from whole-exome and targeted panel "
                     "sequencing", "license": "Apache-2.0"}]
        if t.lower() == "cnvkit" else [], None, {})

    def _bc(s):
        if s == "cnvkit":
            return ([{"id": "cnvkit", "version": "0.9.12",
                      "url": "https://bioconda.github.io/recipes/"
                            "cnvkit/README.html",
                      "license": "BSD-3-Clause"}], None, {})
        return [], "not-on-bioconda (fixture)", {}

    sources.anaconda_bioconda = _bc
    sources.bioconductor_package = empty
    sources.github_rate_limit = lambda: {"core": 50, "reset_utc": None}
    sources.github_releases_tags = lambda r: {
        "releases": ([{"tag": "v0.9.12", "prerelease": False}]
                     if "cnvkit" in r else []),
        "tags": ["v0.9.12"] if "cnvkit" in r else [], "head": {}}
    sources.github_version_file = lambda r: {}
    sources.github_head_commit = lambda r: {}
    sources.github_repo_top_level = lambda r: {"entries": [],
                                               "error": "fixture"}
    sources.pmc_fulltext = lambda p: (None, "fixture: no full text")


def _e2e():
    from backend import inventory, research, store as store_mod
    _faked_sources()
    stage = {"id": "purity_cnv",
             "baseline": {"identified": True, "consensus": False,
                          "tools": [{
                              "name": "PureCN",
                              "role": "purity caller (workflow config)",
                              "evidence": ["workflow.toml: stage.purity "
                                           "tools=[PureCN]"],
                              "pinned_version": None}],
                          "note": ""},
             "enabled": True}
    inventory.load_inventory = lambda: {
        "repo": "/nonexistent-fixture-repo", "code_verified": True,
        "git_commit": "0" * 10,
        "captured_at": "2026-11-17T00:00:00Z",
        "stages": [stage], "limitations": ["fixture inventory"]}
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="m8verify"))
    try:
        st = store_mod.Store(tmp)
        rid = "res-fixture-1"
        st.research_add(rid, "targeted", "purity_cnv", engine="live",
                        targets=["purity_cnv"])
        with st.lock:
            st.db.execute(
                "UPDATE research SET progress_json=? WHERE id=?",
                (json.dumps({"targets": [{"id": "purity_cnv",
                                          "label": "Purity/CNV"}],
                             "steps": [], "done_queries": [],
                             "queries": [], "note": "queued"}), rid))
            st.db.commit()
        research.run_live(rid, "targeted", ["purity_cnv"], st,
                          resume=False)
        row = st.research_get(rid)
        rep = row["report"]
        if isinstance(rep, str):
            rep = json.loads(rep)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rep


try:
    rep = _e2e()
except Exception as exc:  # noqa: BLE001
    rep = None
    chk("E2E run_live completes with faked sources", False,
        f"{type(exc).__name__}: {exc}")
if rep is not None:
    chk("E2E run_live completes with faked sources", True)
    cands = {c["name"]: c for c in rep["candidates"]}
    ck = cands.get("CNVkit", {})
    chk("E2E CNVkit identity resolved from two corroborating axes",
        ck.get("identity_status") == "resolved",
        ck.get("identity"))
    chk("E2E CNVkit relevance is direct-alternative on positive basis",
        ck.get("relationship") == "direct-alternative",
        (ck.get("relationship"), ck.get("eligibility_reasons")))
    chk("E2E no dashboard/acronym phantom candidates",
        "HTML" not in cands and "pViz" not in cands, list(cands))
    ck_meas = [r for r in ck.get("published_benchmark_evidence", [])
               if r.get("metric") in ("sensitivity", "specificity")]
    chk("E2E CNVkit's verified rows carry CNVkit's own numbers",
        {(r["metric"], r["value"]) for r in ck_meas} ==
        {("sensitivity", "76.3%"), ("specificity", "95.5%")},
        [(r["metric"], r["value"], r.get("subject")) for r in ck_meas])
    chk("E2E baseline tool's numbers never bound to the alternative",
        all(r.get("value") not in ("58.6%", "92.3%", "61.2%")
            for r in ck_meas), ck_meas)
    b = [c for c in rep["candidates"] if c.get("is_baseline")]
    chk("E2E baseline carries verified measurement rows",
        bool(b) and any(r.get("metric") and r.get("verified")
                        for r in b[0]["published_benchmark_evidence"]),
        b[0]["published_benchmark_evidence"][:2] if b else "no baseline")
    mc = rep["module_conclusions"]["purity_cnv"]
    chk("E2E a direct verified pair exists, none unverified",
        any(x["class"] == "direct" for x in mc["comparisons"])
        and all(x["class"] != "unverified" for x in mc["comparisons"]),
        mc["comparisons"])
    chk("E2E verified direct pair with winning alternative -> Promising",
        mc["recommendation"] == LBL["promising"],
        (mc["recommendation"],
         [(x.get("candidate"), x.get("winner"))
          for x in mc["comparisons"]]))
    gates = rep["quality_gates"]
    chk("E2E eight gates recorded per module",
        len(gates) == 8 and all("target" in g and "check" in g
                                for g in gates), gates)
    chk("E2E decision_ready iff all gates pass AND no module is "
        "Review incomplete (non-negotiable 6)",
        (all(g["passed"] for g in gates)
         and not [m for m in rep["module_conclusions"].values()
                  if m["recommendation"] == LBL["incomplete"]])
        == rep["decision_ready"],
        [(g["dimension"], g["passed"]) for g in gates]
        + [("label", m["recommendation"])
           for m in rep["module_conclusions"].values()])
    chk("E2E baseline context block present",
        rep["task_comparisons"][0]["baseline"].get("context") is not None)
    chk("E2E published_benchmark_evidence is always a list",
        all(isinstance(c["published_benchmark_evidence"], list)
            for c in rep["candidates"]))
    chk("E2E every citation carries a canonical cid",
        all(c.get("cid") for c in rep["literature_citations"]),
        rep["literature_citations"][:3])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed"
      + (f" - FAILURES: {FAIL}" if FAIL else " - ALL PASS"))
sys.exit(1 if FAIL else 0)
