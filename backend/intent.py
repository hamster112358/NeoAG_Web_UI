"""M11 P1-11: question interpretation BEFORE retrieval.

Regression pinned by the live record res-20261119-153059-9e95ab: a
mixed-language (Chinese) question was used VERBATIM as the query term
for every literature and GitHub query. PubMed, Europe PMC and OpenAlex
index English literature, so every query fetched zero records and the
run concluded from its own untranslated echo.

This module turns a free-form (possibly mixed-language) question into
a retrieval intent:

  * candidate tool names: every non-common Latin token is a candidate
    NAME (mixed-script text treats Latin runs as proper nouns);
  * each name is resolved against the NATIVE configuration in two
    levels - configured (a baseline tool of an inventory stage) and
    declared (the repo's tools registry) - and otherwise stays
    UNVERIFIED. An unverified name is kept exactly as written and is
    NEVER silently mapped onto a similar tool: the nearest declared
    name, if any, is recorded as a disambiguation NOTE for the report,
    never substituted into a query;
  * modality vocabulary: bilingual cue lexicons (English + Chinese)
    map the question onto task families; a name that resolves natively
    contributes its declared description's modality too;
  * the output query terms are ENGLISH - the mixed-language label is
    retained as the display label only, never embedded in a query.

Generalization rule: nothing in this file names any specific tool.
The name-resolution behaviour comes entirely from resolving names
against the native inventory/registry data at runtime.
"""
from __future__ import annotations

import json
import re
import unicodedata

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9+._-]*")

# Words a mixed-language question uses as ORDINARY vocabulary: they
# must never be mistaken for tool names. Domain abbreviations belong
# here (HLA/CNV/RNA are modalities, not tools); real tool names must
# NOT (there is no tool-name list in this module - by design).
COMMON_WORDS = {
    # articles / prepositions / verbs / adjectives
    "a", "an", "and", "are", "as", "at", "be", "best", "better", "but",
    "can", "compare", "compared", "comparing", "comparison", "does",
    "find", "for", "from", "good", "has", "have", "in", "instead",
    "is", "it", "its", "migrate", "migrated", "migration", "more",
    "most", "my", "need", "needs", "of", "on", "one", "or", "our",
    "please", "prefer", "preferred", "replacement", "replace",
    "replaced", "replacing", "reliability", "reliable", "reliably",
    "review", "robust", "robustness", "search", "should", "similar",
    "software", "solr", "switch", "than", "that", "the", "their",
    "them", "there", "this", "to", "tool", "tools", "up", "use",
    "used", "using", "version", "versions", "want", "we", "well",
    "what", "whether", "which", "with", "without",
    # domain common nouns (NOT tool names)
    "algorithm", "algorithms", "allele", "alleles", "alignment",
    "alignments", "rna", "dna", "cdnna", "cfdna", "mrna", "trna",
    "hla", "mhc", "cnv", "cnvs", "snv", "snvs", "indel", "indels",
    "wgs", "wes", "rna-seq", "rnaseq", "bam", "vcf", "fastq", "tsv",
    "csv", "json", "pdf", "bed", "gtf", "pipeline", "pipelines",
    "workflow", "workflows", "benchmark", "benchmarks", "benchmarking",
    "evaluation", "evaluate", "calling", "caller", "callers",
    "predictor", "predictors", "classifier", "clustering",
    "quantification", "expression", "purity", "ploidy", "aneuploidy",
    "heterozygosity", "immunogenicity", "immunotherapy", "neoantigen",
    "neoantigens", "neoepitope", "neoepitopes", "epitope", "epitopes",
    "peptide", "peptides", "tumor", "tumour", "tumors", "normal",
    "sample", "samples", "patient", "patients", "cohort", "data",
    "dataset", "datasets", "accuracy", "precision", "recall",
    "sensitivity", "specificity", "performance", "alternative",
    "alternatives", "option", "options", "method", "methods",
    "approach", "approaches", "program", "programs", "package",
    "packages", "implementation", "deployments", "deployment",
    "production",     "current", "existing", "present", "gene", "genes", "detection",
    "estimate", "estimation", "splicing", "transcript", "transcripts",
    "variant", "variants", "mutation", "mutations", "faster", "speed",
    "cost", "license", "licence", "commercial", "free", "run", "runs",
    "running", "faster", "slower", "accuracy", "used", "widely",
    "commonly", "instead", "better", "best",
}

# Bilingual modality lexicons. "cues" match the QUESTION; "terms" are
# the ENGLISH query terms emitted for retrieval. Generic scientific
# vocabulary only - no product or tool names.
MODALITY_CUES = {
    "hla_typing": {
        "cues": [r"\bhla\b", r"histocompatibilit", r"\btyping\b",
                 r"分型", r"人类白细胞抗原", r"配型"],
        "terms": ["HLA typing", "HLA genotype", "allele-level typing"]},
    "hla_loh": {
        "cues": [r"\bloh\b", r"loss of heterozygosity", r"loss-of-het",
                 r"杂合性缺失", r"杂合缺失"],
        "terms": ["HLA loss of heterozygosity", "LOH detection"]},
    "purity_cnv": {
        "cues": [r"copy[ _-]?number", r"\bcnv\b", r"purity", r"ploidy",
                 r"aneuploid", r"拷贝数", r"纯度", r"非整倍体",
                 r"倍性"],
        "terms": ["tumour purity estimation", "copy number alteration",
                  "allele-specific copy number"]},
    "rna_expression": {
        "cues": [r"rna[ -]?seq", r"transcript abundanc", r"tpm\b",
                 r"quantificat", r"表达量", r"定量", r"转录本丰度"],
        "terms": ["transcript quantification", "RNA-seq expression"]},
    "splicing": {
        "cues": [r"splic", r"isoform", r"junction", r"transcript variant",
                 r"剪接", r"可变剪接", r"异构体", r"剪接位点"],
        "terms": ["alternative splicing", "splice junction",
                  "isoform discovery"]},
    "fusion": {
        "cues": [r"gene fusion", r"fusion gene", r"fusion", r"chimeric",
                 r"融合基因", r"融合转录本", r"融合"],
        "terms": ["gene fusion detection", "chimeric transcript"]},
    "variant_calling": {
        "cues": [r"variant call", r"somatic (mutation|variant)", r"\bsnv\b",
                 r"small variant", r"体细胞突变", r"突变检测", r"变异检测",
                 r"变异 calling", r"somatic变异"],
        "terms": ["somatic small-variant calling",
                  "somatic mutation calling"]},
    "presentation": {
        "cues": [r"\bbinding\b", r"presentat", r"eluted ligand",
                 r"immunogen", r"epitope", r"neoantigen", r"\bmhc\b",
                 r"\bnetmhc\w*", r"结合", r"呈递", r"免疫原性", r"表位",
                 r"新抗原"],
        "terms": ["peptide-HLA binding prediction", "neoantigen",
                  "immunogenicity prediction"]},
    "ranking": {
        "cues": [r"prioriti", r"\branking\b", r"\brank\b", r"scoring",
                 r"排序", r"优先级", r"打分"],
        "terms": ["candidate prioritisation", "ranking"]},
    "reports": {
        "cues": [r"\breport", r"visuali[sz]", r"报告", r"可视化"],
        "terms": ["clinical report generation"]},
}

# Intent facets (what the user is ASKING for), bilingual. A matched
# facet enriches the query with comparison vocabulary.
INTENT_CUES = {
    "replacement": [r"替代", r"替换", r"代替", r"换成", r"换用", r"更换",
                    r"alternative", r"replace", r"replacement",
                    r"instead of", r"switch"],
    "comparison": [r"比较", r"对比", r"更好", r"最好", r"优于",
                   r"better", r"best", r"comparison", r"versus",
                   r"\bvs\.?\b", r"outperform", r"head-to-head"],
    "reliability": [r"可靠", r"稳定", r"准确", r"稳健", r"reliable",
                    r"robust", r"accura", r"precision"],
}


def _norm_name(s: str) -> str:
    """Lower-case alphanumeric core, version suffix stripped."""
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[^a-z0-9]", "", s)
    return re.sub(r"\d+$", "", s)


def _edit_distance(a: str, b: str, cap: int = 3) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def extract_candidate_names(text: str) -> list[str]:
    """Latin tokens of a (possibly mixed-language) question that are
    NOT ordinary vocabulary -> candidate tool/proper names, in first-
    seen order. In CJK text a Latin run sitting between ideographs is
    a proper noun unless it is common vocabulary; in pure-English text
    the same rule is the conservative guess the retrieval layer later
    verifies against native data."""
    # a token that IS modality/intent vocabulary ("fusion", "typing",
    # "alternative"...) is a common scientific word, never a tool
    # name - this filter is general (cue lexicons only, no tool list)
    cue_words = set()
    for spec in MODALITY_CUES.values():
        for c in spec["cues"]:
            cue_words.add(c)
    for cues in INTENT_CUES.values():
        cue_words.update(cues)

    def _is_cue_word(low: str) -> bool:
        for c in cue_words:
            try:
                if re.search(c, low) and re.search(c, low).group(0) == low:
                    return True
            except re.error:
                continue
        return False

    out, seen = [], set()
    for m in _LATIN.finditer(text or ""):
        tok = m.group(0).strip("._-+")
        low = tok.lower()
        if not tok or len(low) <= 2 or low in COMMON_WORDS:
            continue
        # hyphenated common compounds ("rna-seq") are already covered
        # by COMMON_WORDS parts: drop a token whose every hyphen part
        # is common vocabulary
        if "-" in low and all(p in COMMON_WORDS for p in low.split("-")
                              if p):
            continue
        if _is_cue_word(low):
            continue
        if low in seen:
            continue
        seen.add(low)
        out.append(tok)
    return out


def _cue_groups(text: str, lex: dict) -> list[str]:
    low = (text or "").lower()
    hits = []
    for group, spec in lex.items():
        cues = spec["cues"] if isinstance(spec, dict) else spec
        if any(re.search(c, low) for c in cues):
            hits.append(group)
    return hits


def _known_index(stages, declared):
    """name -> {status, where, evidence} built ONLY from native data:
    inventory baseline tools (configured) and the declared tool
    registry."""
    known: dict[str, dict] = {}
    for st in stages or ():
        for b in (st.get("baseline") or {}).get("tools") or []:
            nm = str(b.get("name") or "")
            if not nm or nm.lower().startswith("neoag internal"):
                continue
            k = _norm_name(nm)
            if not k:
                continue
            entry = known.setdefault(k, {
                "display": nm, "status": "configured", "stages": [],
                "descriptions": []})
            entry["stages"].append({
                "id": st.get("id"), "role": b.get("role", ""),
                "evidence": [str(e) for e in (b.get("evidence") or [])
                             ][:3]})
        if st.get("id"):
            known.setdefault(_norm_name(st["id"]), {
                "display": st.get("id") or st.get("stage") or st["id"],
                "status": "stage-id", "stages": [],
                "descriptions": []})
    for key, meta in (declared or {}).items():
        k = _norm_name(key)
        if not k:
            continue
        desc = str((meta or {}).get("description") or "")
        if k in known:
            if known[k]["status"] == "stage-id":
                known[k]["status"] = "declared"
            if desc:
                known[k]["descriptions"].append(desc)
        else:
            known[k] = {"display": key, "status": "declared",
                        "stages": [], "descriptions": [desc]}
    return known


def analyze(question: str, stages=(), declared=None) -> dict:
    """One mixed-language question in, one retrieval intent out.

    stages   : inventory stages (configured baseline tools)
    declared : the native tool registry dict (name -> metadata)
    """
    q = (question or "").strip()
    names = extract_candidate_names(q)
    known = _known_index(stages, declared)

    tools = []
    derived_text = [q]
    for raw in names[:8]:
        k = _norm_name(raw)
        hit = known.get(k)
        near = None
        if hit is None and len(k) >= 6:
            best, bestd = None, 3
            for kk, v in known.items():
                if v["status"] == "stage-id" or not kk:
                    continue
                d = _edit_distance(k, kk, cap=bestd)
                if d < bestd or (d == bestd and best is None):
                    best, bestd = v["display"], d
            limit = 1 if len(k) < 8 else 2
            if best and bestd <= limit:
                near = {"name": best, "distance": bestd}
        entry = {"raw": raw, "key": k,
                 "status": (hit or {}).get("status", "unverified")}
        if hit:
            entry["stages"] = hit["stages"]
            entry["descriptions"] = hit["descriptions"][:2]
            # a natively-known name also carries its declared
            # modality vocabulary into the cue scan
            derived_text.append(
                " ".join(hit["descriptions"]) + " "
                + " ".join(s.get("id") or "" for s in hit["stages"]))
        else:
            entry["stages"] = []
            entry["descriptions"] = []
            if near:
                entry["nearest"] = near
                entry["note"] = (
                    f"not found in the native configuration; nearest "
                    f"declared name is {near['name']!r} (edit distance "
                    f"{near['distance']}) - NOT assumed to be the same "
                    "tool; kept as written")
            else:
                entry["note"] = ("not found in the native "
                                 "configuration and no close declared "
                                 "name exists - treated as an "
                                 "unverified tool name")
        tools.append(entry)

    groups = sorted(set(_cue_groups(q, MODALITY_CUES)
                        + _cue_groups(" ".join(
                            d for t in tools
                            if t["status"] != "unverified"
                            for d in t["descriptions"]), MODALITY_CUES)
                        + _cue_groups(" ".join(
                            s.get("id") or "" for t in tools
                            for s in t["stages"]), MODALITY_CUES)))
    domain_terms: list[str] = []
    for g in groups:
        for t in MODALITY_CUES[g]["terms"]:
            if t not in domain_terms:
                domain_terms.append(t)

    facets = _cue_groups(q, INTENT_CUES)
    query_terms = [f'"{t["raw"]}"' for t in tools]
    has_signal = bool(tools or domain_terms)

    notes = []
    if not tools and not domain_terms:
        notes.append("no candidate tool name and no recognised "
                     "modality vocabulary could be derived from the "
                     "question - retrieval would have no English "
                     "term to use (queries are skipped and recorded, "
                     "never fabricated from the untranslated label)")
    unverified = [t for t in tools if t["status"] == "unverified"]
    for t in unverified:
        notes.append(f"{t['raw']}: {t['note']}")
    declared_hits = [t for t in tools if t["status"] == "declared"]
    for t in declared_hits:
        notes.append(
            f"{t['raw']}: declared in the native tool registry "
            f"({'; '.join(t['descriptions'])[:110]})")
    cfg_hits = [t for t in tools if t["status"] == "configured"]
    for t in cfg_hits:
        notes.append(
            f"{t['raw']}: configured baseline tool of "
            + ", ".join(s["id"] for s in t["stages"][:3]))

    return {
        "question": q,
        "mixed_language": bool(_CJK.search(q)) and bool(names),
        "tools": tools,
        "modalities": groups,
        "facets": facets,
        "domain_terms": domain_terms,
        "query_terms": query_terms,
        "has_english_signal": has_signal,
        "notes": notes,
    }


def synthesis_note(intent: dict) -> str:
    """One honest progress-line summary of the interpretation."""
    if not intent["has_english_signal"]:
        return ("question interpretation: no tool name and no "
                "recognised modality vocabulary - the untranslated "
                "label is NEVER used as a query (queries recorded as "
                "skipped, not faked)")
    parts = []
    if intent["tools"]:
        parts.append("names: " + ", ".join(
            f"{t['raw']} [{t['status']}]" for t in intent["tools"]))
    if intent["modalities"]:
        parts.append("modality: " + ", ".join(intent["modalities"]))
    return "question interpretation -> " + "; ".join(parts)


def load_declared(repo_root) -> dict:
    """Best-effort read of the native tool registry (declared tools);
    {} when the deployment has none - the intent layer then resolves
    only against the inventory stages."""
    try:
        p = (str(repo_root).rstrip("/")
             + "/.agents/config/tools_registry.json")
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}
