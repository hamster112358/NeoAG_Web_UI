"""Performance-evidence extraction with FULL context (M5 PART 4.3).

The rule the whole module serves: a number is only quotable when the
experimental context around it is established (what metric, on what
dataset, against what ground truth, how many events, which tool
version, under what conditions, in which publication, author-reported
or independently measured). Retrieved abstracts rarely establish that
context, so most abstract numbers arrive labelled
`context_established: false` - they are visible as quotes but are
EXCLUDED from comparison rows, instead of being silently ranked.

Full-text route: Europe PMC's fullTextXML sub-endpoint is NOT
reachable from this host (measured 2026-11-16: even the
documentation's canonical example returns 404 - only /search works),
so the full-text attempt goes through NCBI efetch db=pmc on the
already-verified eutils host, and only for citations that carry a
PMCID (open-access subset). A failed or absent full text is recorded
honestly; abstract-level evidence remains the floor.
"""
from __future__ import annotations

import re

from .store import now

METRIC_RE = re.compile(
    r"\b(AUROC|AUPRC|AUC|F1[- ]?score|F1|precision|recall|sensitivity|"
    r"specificity|Spearman(?:'?s)?|Pearson|accuracy|PPV|NPV|Jaccard|"
    r"positive predictive value|negative predictive value)\b"
    r"[^.;]{0,60}?(\d{1,3}\.\d{2,4}|\d{1,3}(?:\.\d+)?\s?%)", re.I)

DATASET_CUE_RE = re.compile(
    r"\b(IEDB|ICBC(?:\s*III)?|CPTAC|TCGA|SEQC|ICGC|GTEx|BIRDT?|"
    r"PEAKR|HLA[\s-]?Ligand[\s-]?Atlas|MHLA|1KGP?|1000\s?Genomes"
    r"|cBioPortal|MILESTONE)\b")
TRUTH_CUE_RE = re.compile(
    r"(mass[\s-]?spec|eluted[\s-]?ligand|immunopeptidom|IC\s?50|"
    r"binding assay|differential display|curated (?:truth|benchmark)"
    r"|clinical response|recurrence[- ]free)", re.I)
COUNT_RE = re.compile(
    r"([\d][\d,\.]*)\s*(peptides|patients|samples|tumou?rs?|alleles"
    r"|ligands|sequences|variants|breakpoints)", re.I)
COND_CUE_RE = re.compile(
    r"(pan-?specific|allele[\s-]specific|hold[\s-]?out"
    r"|cross-?validation|training (?:set|data)|independent (?:test"
    r"|validation)|de ?novo assembly)", re.I)
VER_NEAR_RE = re.compile(
    r"\b(?:v|ver\.?|version)\s*v?\d+(?:\.\d+)+\b", re.I)

COMP_CUE_RE = re.compile(r"benchmark|comparat|evaluation|assess|"
                         r"head[- ]to[- ]head", re.I)


def _author_paper(name: str, citation: dict) -> bool:
    return bool(name) and name.lower() in (
        citation.get("title") or "").lower()


def metric_rows(name: str, text: str, *, citation: dict, level: str,
                max_rows: int = 4) -> list[dict]:
    """Mine METRIC-shaped statements out of one text (abstract or
    full-text body). Each row keeps the quote, so a human can re-judge
    the extraction; context fields say 'not stated by source' unless
    the same sentence (or its immediate ±120 chars) actually says
    otherwise."""
    rows = []
    sentences = re.split(r"(?<=[.;])\s+", text or "")
    name_l = (name or "").lower()
    for sent in sentences:
        if len(rows) >= max_rows:
            break
        m = METRIC_RE.search(sent)
        if not m:
            continue
        if name_l and name_l not in sent.lower():
            # the number is not about THIS candidate in this sentence -
            # widen to the neighbouring window, never beyond it
            idx = sentences.index(sent)
            window = " ".join(sentences[max(0, idx - 1):idx + 2])
            if name_l not in window.lower():
                continue
        idx = sentences.index(sent)
        w = _context(sentences, idx)
        ds = DATASET_CUE_RE.search(w)
        gt = TRUTH_CUE_RE.search(w)
        cnt = COUNT_RE.search(w)
        ver = VER_NEAR_RE.search(w)
        ap = _author_paper(name, citation)
        comp = bool(COMP_CUE_RE.search((citation.get("title") or "")
                                       + " " + w))
        rows.append({
            "metric": m.group(1), "value": m.group(2).strip(),
            "metric_definition": "as stated in the quoted sentence - "
                                 "full definition not verified",
            "quote": sent.strip()[:300],
            "dataset": ds.group(0) if ds else "not stated by source",
            "ground_truth": gt.group(0) if gt else
                            "not stated by source",
            "event_or_sample_count": cnt.group(0) if cnt else
                                     "not stated by source",
            "tested_version": ver.group(0) if ver else
                              "not stated by source",
            "conditions": COND_CUE_RE.search(w).group(0)
                          if COND_CUE_RE.search(w)
                          else "not stated by source",
            "source_publication": {
                "id": citation.get("id"), "title": citation.get("title"),
                "year": citation.get("year"), "url": citation.get("url"),
                "retrieved_from": citation.get("source")},
            "author_reported": ap and not comp,
            "independent": comp and not ap,
            "evidence_level": (
                f"{level}: " + (
                    "author-reported" if ap and not comp else
                    "independent comparative text" if comp and not ap
                    else "contextual mention")),
            # ONLY an explicitly named dataset or ground-truth assay in
            # the window makes a number comparable:
            "context_established": bool(ds or gt),
            "extracted_at": now()})
    return rows


def _context(sentences: list[str], idx: int) -> str:
    """Context for one metric sentence = the sentence itself, plus the
    PRECEDING sentence only if that one carries no metric of its own
    (so a dataset named two sentences back can support 'evaluated on
    it', but one metric sentence can never lend its dataset/ground-
    truth to a neighbouring metric sentence - that bleed would
    misattribute context, which is exactly what this gate exists to
    prevent)."""
    w = sentences[idx]
    if idx > 0 and not METRIC_RE.search(sentences[idx - 1]):
        w = sentences[idx - 1] + " " + w
    return w


def xml_to_text(xml: str) -> str:
    """Crude but honest: strip JATS markup to plain text for sentence
    mining (tables remain as flattened rows, which is enough for the
    cue regexes - no parser dependency is introduced)."""
    t = re.sub(r"<(sec|title|abstract|p|td|th|tr)[^>]*>", " \n",
               xml or "")
    t = re.sub(r"<[^>]+>", " ", t)
    import html as _h
    return _h.unescape(re.sub(r"[ \t]+", " ", t))


def cross_version_caveat(row: dict, stable_value: str | None) -> str:
    """The 4.2 rule: published performance for an older version does
    NOT transfer to a newer one without evidence."""
    tv = (row.get("tested_version") or "")
    stated = tv if tv and tv != "not stated by source" else None
    if stated and stable_value and stable_value not in stated:
        return (f"measured with {stated}; the currently verified stable "
                f"is {stable_value} - this number must NOT be credited "
                f"to the current version without re-measurement")
    if stated and not stable_value:
        return (f"measured with {stated}; current stable version not "
                f"verified - comparability to today's release unknown")
    if not stated:
        return ("tested version not stated by the source - the number "
                "cannot be attributed to any particular release")
    return ""


def comparability_note(rows: list[dict]) -> str:
    usable = [r for r in rows if r.get("context_established")]
    if not usable:
        return ("no performance value with established experimental "
                "context was retrieved - quotes above are mentions, "
                "not comparable results; a same-conditions benchmark "
                "would be required")
    return (f"{len(usable)} of {len(rows)} retrieved values carry an "
            f"explicit dataset/ground-truth context; only those are "
            f"eligible for comparison, and each still needs its "
            f"version caveat checked")
