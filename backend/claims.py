"""S6 complete experimental-claim extraction (root-cause 1.5).

The old extractor paired the first metric word in a sentence with the
first number after it - which turned "PureCN yielded 16.9%
sensitivity and 95.5% specificity" into (sensitivity, 95.5%), let a
PURPLE sentence yield ClinCNV's 67.8%, and read the "95" of
"Wilson 95% CI" as a sensitivity value.

Grammar implemented here:

  * Extraction runs ONCE per document (not per candidate). A
    measurement belongs to the SUBJECT of its clause; claims for a
    candidate are the claims whose clause subject alias-matches it.
    Numbers belonging to a comparator stay on the comparator's claim
    - cross-tool contamination becomes structurally impossible.

  * Sentence -> segments: strong boundaries (';', 'whereas'/'while'/
    'however'...) AND coordinated subject lists (", native PureCN
    16.9%/95.5%, and PURPLE 58.6%/92.3%" -> one segment per
    name-value run).

  * Binding is sequential over an ordered token stream of METRIC /
    VALUE items. A value binds to the metric its connective junk
    allows ("16.9% sensitivity" value-first; "sensitivity of 95.5%"
    metric-first; junk containing 'and'/'or'/another metric blocks
    the link, so each number meets exactly one metric).

  * CI handling: confidence LEVELS ("95% CI"), ranges inside
    parentheses and pure-numeric parentheticals following a value are
    masked before binding; the masked text is kept as the
    measurement's CI field, never as its value.

  * Ordered pairs: "sensitivity/specificity of 67.8%/85.1%" binds
    positionally; "Name 16.9%/95.5%" inherits the metric pair already
    established in the same sentence; without such a frame the pair
    is emitted with metric=None + ambiguous=True.

  * A measurement whose clause has no bindable subject is emitted
    with subject=None / ambiguous=True: visible with its exact span,
    excluded from every comparison.

No tool names are hardcoded; known_names only NORMALISES a parsed
subject phrase (alias match). Subject detection is syntactic.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# vocabularies
# ---------------------------------------------------------------------------

METRIC_TERMS = [
    (r"\bauprc-el\b", "AUPRC-EL"),
    (r"\bauprc\b", "AUPRC"),
    (r"\bauroc\b", "AUROC"),
    (r"\bauc\b", "AUC"),
    (r"area under (?:the\s+)?(?:pr|precision[\s\u2013\u2014-]*recall|"
     r"receiver\s+operating\s+characteristic)(?:\s+curve)?"
     r"(?:\s*\([^)]*\))?", "AUC-family"),
    (r"\bf[1-][ -]?score\b|\bf-measure\b|\bf1\b", "F1"),
    (r"positive predictive value|\bppv\b", "precision"),
    (r"negative predictive value|\bnpv\b", "NPV"),
    (r"\bprecision\b", "precision"),
    (r"\brecall\b", "recall"),
    (r"\bsensitivity\b", "sensitivity"),
    (r"\bspecificity\b", "specificity"),
    (r"\baccuracy\b", "accuracy"),
    (r"spearman(?:'?s)?(?: rank)?(?: correlation| coefficient| rho)?",
     "Spearman"),
    (r"pearson(?:'?s)?(?: correlation| coefficient| r)?", "Pearson"),
    (r"concordance correlation|\bccc\b", "concordance correlation"),
    (r"mean absolute error|\bmae\b", "MAE"),
    (r"root mean squared error|\brmse\b", "RMSE"),
    (r"median absolute error", "MedAE"),
    (r"error rate", "error rate"),
    (r"\bjaccard\b|\bdice\b", "overlap coefficient"),
    (r"\bbalanced accuracy\b", "balanced accuracy"),
    (r"\bmcc\b|matthews", "MCC"),
]
_METRIC_RX = re.compile("(" + ")|(".join(t for t, _ in METRIC_TERMS)
                        + ")", re.I)
_CANON = {i: cname for i, (_, cname) in enumerate(METRIC_TERMS)}


def _metric_canon(m: re.Match) -> str:
    for gi in range(1, len(METRIC_TERMS) + 1):
        if m.group(gi) is not None:
            return _CANON[gi - 1]
    return m.group(0).lower()


# values: percent, decimal, slashed pair; ranges only via masking
_VALUE_RX = re.compile(
    r"(?P<pair>\d{1,3}(?:\.\d+)?\s?%\s*/\s*\d{1,3}(?:\.\d+)?\s?%"
    r"|\d+\.\d+\s*/\s*\d+\.\d+)"
    r"|(?P<pct>\d{1,3}(?:\.\d+)?)\s?%"
    r"|(?P<dec>[01]\.\d{2,4})"
    r"|(?P<num>\d+\.\d{2,4})")
_CI_LABEL_RX = re.compile(r"\s*(?:\bci\b|confidence interval|credible "
                          r"interval|credible region)", re.I)
# parentheses: CI-labelled, or PURE numeric (ranges/counts - never a
# bare point value a metric could claim)
_CI_PAREN_RX = re.compile(r"[(\[][^()\[\]]{0,90}?(?:\bci\b|confidence "
                          r"interval|credible interval)[^()\[\]]{0,90}?"
                          r"[)\]]", re.I)
_NUMPAREN_RX = re.compile(
    r"[(\[]\s*\d[\d.,\s%\u2013\u2014/-]*(?:to\s+[\d.,\s%]+)?[)\]]")
_P_RX = re.compile(r"^\s*[pP]\s*[<>=]")

_SEGMENT_SPLIT_RX = re.compile(
    r"(?<=[;\u2013\u2014])\s*"
    r"|\s+(?:whereas|while|however|conversely|in contrast)\s+"
    r"|,\s*(?=(?:and\s+)?(?:\w+\s+)?[A-Z][\w'\-]*\s+\d[\d.]*\s*%?\s*/)"
    r"|,\s*(?=(?:and\s+)?[A-Z][\w'\-]*\s+\d[\d.]*\s*%?\s*/)")
_LEAD_STRIP_RX = re.compile(r"^\s*(?:and|with|or|while|both)\s+", re.I)

_SUBJECT_RX = re.compile(
    r"^\s*(?:the\s+|a\s+|both\s+)?"
    r"((?:[A-Z][\w'+\-]*|[a-z]+[A-Z][\w'+\-]*|[\w+.\-]*\d[\w.\-]*)"
    r"(?:\s+(?:[A-Z][\w'+\-]*|[a-z]+[A-Z][\w'+\-]*|caller|tool|method|"
    r"pipeline|software|algorithm|approach|model|rule|standalone|native"
    r"|median-bin))*)"
    r"(?=[\s,;]|$)", re.I)

_STOP_SUBJECT = {
    "results", "conclusion", "conclusions", "background", "methods",
    "here", "we", "our", "us", "this", "that", "these", "each",
    "furthermore", "moreover", "however", "overall", "notably",
    "importantly", "additionally", "specifically", "abstract",
    "introduction", "discussion", "table", "figure", "fig",
    "supplementary", "authors", "purpose", "materials", "when",
    "under", "using", "across", "among", "whereas", "although",
    "all", "several", "multiple", "fixed", "native", "standalone",
}

_VERB_OWN_RX = re.compile(
    r"\b(yield\w*|achiev\w*|attain\w*|deliver\w*|reach\w*|produc\w*"
    r"|obtain\w*|outperform\w*|improv\w*|exhibit\w*|demonstrat\w*"
    r"|score\w*)\b", re.I)

# passive performance idiom: "recovered by AneuFinder", "called by
# PureCN", "Using AneuFinder, profiles are recovered..." - the agent
# follows the preposition, closest to the value
_BY_RX = re.compile(
    r"\b(?:by|via|using)\s+(?:the\s+|a\s+)?"
    r"((?:[A-Z][\w'+\-]*|[a-z]+[A-Z][\w'\-]*)"
    r"(?:\s+(?:[A-Z][\w'+\-]*|[a-z]+[A-Z][\w'\-]*|caller|tool|method"
    r"|pipeline|software|algorithm|approach|model))*)", re.I)

# a proper-noun-ish tool mention anywhere: internal caps, digits, or
# Capitalised word (>=3 chars, mixed case beyond first letter is NOT
# required - gene-like ALLCAPS and Title-case both qualify)
_ANY_SUBJ_RX = re.compile(r"\b([A-Z][A-Za-z0-9'+\-]+|[A-Z]{2,}\d*|[a-z]+"
                          r"[A-Z][\w'\-]*)\b")
_NAMEISH_RX = re.compile(r"([A-Z][\w'+\-]*|[a-z]+[A-Z][\w'\-]*|"
                         r"[\w.\-]*\d[\w.\-]*)")

_CONNECTIVE_RX = re.compile(
    r"^(?:[\s:=,]+|\((?:[A-Za-z][A-Za-z0-9\-]{0,10})\)|of|was|were|"
    r"is|are|at|in|as|with|up\s+to|reach(?:ed|es)?|attain(?:ed|s)?|"
    r"show(?:ed|s)?|obtain(?:ed|s)?|score[s]?|value[s]?|"
    r"estimate[s]?|approx(?:\.|imately)?|about|around)*$", re.I)


def blank_tags(text: str) -> str:
    """Replace HTML/XML tags with same-length blanks so character
    offsets (and every span we record) stay valid on the ORIGINAL
    abstract string."""
    return re.sub(r"<[^>]{1,120}?>", lambda m: " " * len(m.group(0)),
                  text or "")


class Claim(dict):
    """dict with helpers - serialized as-is (report data)."""


def _subject_norm(surface: str, known_names) -> tuple[str | None, str]:
    s = (surface or "").strip(" .,;:()\"'")
    if not s:
        return None, ""
    low = s.lower()
    words = low.split()
    if not words:
        return None, ""
    # alias match FIRST: a modifier-led phrase ("native PureCN",
    # "fixed CNVkit median-bin log2 rule") still names its tool
    for name in known_names or ():
        nl = str(name).lower()
        if nl == low or re.search(rf"(?:^|[\s\-]){re.escape(nl)}"
                                  rf"(?:$|[\s\-])", low):
            return nl, s
    if words[0] in _STOP_SUBJECT or low in _STOP_SUBJECT:
        # no alias: only salvage a later name-shaped word, and even
        # that only if it is not itself a stop word
        m = _NAMEISH_RX.search(s)
        if m and m.group(1).lower() not in _STOP_SUBJECT and (
                re.search(r"[a-z][A-Z]|\d", m.group(1))
                or (m.group(1)[:1].isupper() and len(m.group(1)) > 3
                    and not m.group(1).isupper())):
            return m.group(1).lower(), s
        return None, ""
    # generic subject: keep a single word when it is name-shaped
    m = _NAMEISH_RX.search(s)
    if m and len(m.group(1)) >= 3 and (
            re.search(r"[a-z][A-Z]|\d", m.group(1))
            or (m.group(1)[:1].isupper() and len(m.group(1)) > 3
                and not m.group(1).isupper())):
        tok = m.group(1).lower()
        if tok in _STOP_SUBJECT:
            return None, ""
        return tok, s
    return None, ""


def _clause_subject(seg: str, known_names, first_value: int | None = None
                    ):
    """(subject_norm, surface, span, owners) for one segment."""
    subj = surf = ""
    span = (0, 0)
    owners: set[str] = set()
    m = _SUBJECT_RX.match(seg)
    if m:
        subj, surf = _subject_norm(m.group(1), known_names)
        span = m.span(1)
        if subj:
            owners.add(subj)
    verb = _VERB_OWN_RX.search(seg)
    if verb:
        head = seg[:verb.start()]
        cands = [am for am in _ANY_SUBJ_RX.finditer(head)
                 if len(am.group(1)) >= 3
                 and am.group(1).lower() not in _STOP_SUBJECT
                 and (re.search(r"[a-z][A-Z]|\d", am.group(1))
                      or (am.group(1)[:1].isupper()
                          and (len(am.group(1)) > 3
                               or am.group(1).isupper())))]
        if cands:
            n, s = _subject_norm(cands[-1].group(1), known_names)
            if n:
                subj, surf, span = n, s, cands[-1].span(1)
                owners.add(n)
    if not subj:
        # passive idiom "recovered by AneuFinder ... (Pearson r =
        # 0.855)": the AGENT closest before the first bound value
        best = None
        for bm in _BY_RX.finditer(seg):
            if first_value is not None and bm.start() > first_value:
                break
            n, s = _subject_norm(bm.group(1), known_names)
            if n:
                best = (n, s, bm.span(1))
        if best:
            subj, surf, span = best
            owners.add(subj)
    return subj, surf, span, owners


def extract_claims(text: str, *, doc_cid: str, level: str,
                   known_names=(), max_claims: int = 40) -> list[dict]:
    """Claim dicts with measurements + exact spans, from one document
    text (abstract or full text)."""
    out: list[dict] = []
    if not text:
        return out
    body = blank_tags(text)
    pos, sents = 0, []
    for part in re.split(r"(?<=[.;])\s+", body):
        idx = body.find(part, pos)
        if idx < 0:
            idx = pos
        sents.append((part, idx))
        pos = idx + len(part)
    for sent, s0 in sents:
        if len(out) >= max_claims or not _METRIC_RX.search(sent):
            continue
        if _P_RX.search(sent.strip()):
            continue
        masked = _masked_spans(sent)
        seg_spans = _segment_spans(sent)
        frame: list[str] | None = None
        for seg, coff in seg_spans:
            if len(out) >= max_claims or not seg.strip():
                continue
            # blank (NOT delete) so every span stays valid vs the
            # sentence-level masked spans
            seg = _LEAD_STRIP_RX.sub(lambda m: " " * len(m.group(0)),
                                     seg)
            toks = _tokens(seg, masked, coff)
            if not toks["metrics"] and not toks["values"]:
                continue
            if (not toks["metrics"] and not toks["pair_values"]
                    and frame is None):
                continue       # numbers but no metric frame -> not a
                               # performance claim (counts, sizes)
            mses, frame = _bind(seg, toks, masked, coff, frame)
            if not mses:
                continue
            first_v = mses[0]["value_span"][0] - coff
            subj, surf, sspan, owners = _clause_subject(seg,
                                                        known_names,
                                                        first_v)
            claim = Claim(
                subject=subj, subject_surface=surf,
                subject_span=[coff + sspan[0], coff + sspan[1]],
                subjects_seen=sorted(owners),
                ambiguous=bool(subj is None and len(owners) != 1),
                quote=seg.strip()[:400],
                quote_span=[s0, s0 + len(sent)],
                doc_cid=doc_cid, level=level, measurements=mses)
            out.append(claim)
    return out


def _masked_spans(sent: str) -> list[dict]:
    """Spans to exclude from binding, each with role: 'ci' (labelled
    CI span) or 'numeric-paren' (range/count annotation)."""
    out = []
    for rx, role in ((_CI_PAREN_RX, "ci"), (_NUMPAREN_RX,
                                            "numeric-paren")):
        for m in rx.finditer(sent):
            out.append({"start": m.start(), "end": m.end(),
                        "role": role, "text": m.group(0).strip("()[] ")})
    return out


def _segment_spans(sent: str) -> list[tuple[str, int]]:
    """Segments with absolute offsets (splits preserve offsets because
    every pattern is zero-width lookahead or after-fixed separator)."""
    segs, last = [], 0
    boundaries = [0]
    for m in _SEGMENT_SPLIT_RX.finditer(sent):
        if m.end() > m.start():
            boundaries.append(m.end())
    boundaries.append(len(sent))
    for a, b in zip(boundaries, boundaries[1:]):
        seg = sent[a:b]
        if seg.strip():
            segs.append((seg, a))
    return segs


def _tokens(seg: str, masked: list[dict], coff: int) -> dict:
    """Ordered token lists with spans RELATIVE to seg. Masked content
    is invisible; percent numbers that are CI labels are marked."""
    def _in_mask(a, b):
        return any(mk["start"] - coff <= a and b <= mk["end"] - coff
                   for mk in masked)
    metrics = []
    for m in _METRIC_RX.finditer(seg):
        if _in_mask(m.start(), m.end()):
            continue
        metrics.append({"canon": _metric_canon(m),
                        "surface": m.group(0).strip(),
                        "start": m.start(), "end": m.end()})
    values = []
    for m in _VALUE_RX.finditer(seg):
        if _in_mask(m.start(), m.end()):
            continue
        ci_label = bool(_CI_LABEL_RX.match(seg[m.end():m.end() + 32]))
        values.append({
            "surface": m.group(0).strip(),
            "start": m.start(), "end": m.end(),
            "pair": m.group("pair"), "pct": m.group("pct"),
            "dec": m.group("dec"), "num": m.group("num"),
            "ci_label": ci_label, "used": False})
    metrics = [m for m in metrics if not _overlap_mask(m, masked, coff)]
    values = [v for v in values if not _overlap_mask(v, masked, coff)]
    return {"metrics": metrics,
            "values": [v for v in values if not v["ci_label"]],
            "pair_values": [v for v in values if v["pair"]
                            and not v["ci_label"]]}


def _overlap_mask(tok, masked, coff):
    return any(mk["start"] - coff < tok["end"]
               and tok["start"] < mk["end"] - coff for mk in masked)


def _slash_pairs(seg: str, metrics: list[dict]) -> list[list[dict]]:
    out, i = [], 0
    while i < len(metrics) - 1:
        a, b = metrics[i], metrics[i + 1]
        if re.fullmatch(r"\s*/\s*", seg[a["end"]:b["start"]]):
            out.append([a, b])
            i += 2
            continue
        i += 1
    return out


def _split_pair(pv: str) -> tuple[str, str]:
    a, b = [x.strip() for x in pv.split("/", 1)]
    return a, b


def _junk_ok(seg: str, a: int, b: int) -> bool:
    """Text between a number and a metric must be pure connective
    ('of', ':', 'was', a space...) and short; 'and', 'or', another
    metric or a long gap BLOCKS the link."""
    return (b - a) <= 24 and bool(_CONNECTIVE_RX.match(seg[a:b] or ""))


def _connect_next(seg: str, v: dict, metrics: list[dict]):
    """Value-first idiom: earliest metric AFTER the value whose
    intervening junk is connective ("16.9% sensitivity")."""
    cands = [m for m in metrics
             if m["start"] >= v["end"]
             and _junk_ok(seg, v["end"], m["start"])]
    return cands[0] if cands else None


def _connect_prev(seg: str, v: dict, metrics: list[dict]):
    """Metric-first idiom: nearest metric BEFORE the value whose
    intervening junk is connective ("sensitivity of 95.5%")."""
    cands = [m for m in metrics
             if m["end"] <= v["start"]
             and _junk_ok(seg, m["end"], v["start"])]
    return cands[-1] if cands else None


def _nearest_pair(seg: str, pr: list[dict], values: list[dict]):
    """Unused value-pair positionally linked to a metric-pair, on
    either side ("sensitivity/specificity of 67.8%/85.1%" or
    "67.8%/85.1% sensitivity/specificity")."""
    for v in values:
        if v["used"] or not v["pair"]:
            continue
        if v["start"] >= pr[1]["end"]:
            if _junk_ok(seg, pr[1]["end"], v["start"]):
                return v
        elif v["end"] <= pr[0]["start"]:
            if _junk_ok(seg, v["end"], pr[0]["start"]):
                return v
    return None


def _part_span(v: dict, idx: int) -> tuple[int, int]:
    """Span of one half of a slash-pair value inside the segment."""
    a, _, b = v["surface"].partition("/")
    if idx == 0:
        return v["start"], v["start"] + len(a.rstrip())
    return v["end"] - len(b.lstrip()), v["end"]


def _attach_ci(coff: int, v: dict, masked: list[dict]):
    """A masked parenthetical that STARTS just after the value is its
    interval, never a value of its own."""
    vend = coff + v["end"]
    best = None
    for mk in masked:
        gap = mk["start"] - vend
        if 0 <= gap <= 25 and (best is None
                               or (mk["role"] == "ci"
                                   and best["role"] != "ci")):
            best = mk
    if best is None:
        return None, None
    return best["text"], [best["start"], best["end"]]


def _measurement(masked: list[dict], coff: int, canon, surface,
                 mspan, part: str, v: dict, idx: int,
                 binding: str) -> dict:
    ci, ci_span = _attach_ci(coff, v, masked)
    a, b = _part_span(v, idx)
    return {
        "metric": canon,
        "metric_surface": surface,
        "metric_span": mspan,
        "value": part,
        "value_span": [a + coff, b + coff],
        "ci": ci,
        "ci_span": ci_span,
        "binding": binding,
    }


def _bind(seg: str, toks: dict, masked: list[dict], coff: int,
          frame: list[str] | None):
    """Return (measurements, new_frame) for one segment.

    Order: slash metric-pairs bind positionally to value-pairs
    first; then every remaining (non-pair) value tries the
    value-first idiom, then the metric-first idiom; a value-pair
    with no metric-pair in reach inherits the metric pair
    established earlier IN THE SAME SENTENCE (frame), or - with no
    frame either - is recorded with metric=None / ambiguous=True so
    every comparison must exclude it. Because junk containing
    'and'/'or'/another metric blocks a link, each number meets
    exactly one metric.
    """
    ms: list[dict] = []
    metrics = list(toks["metrics"])
    values = list(toks["values"])
    pairs = _slash_pairs(seg, metrics)

    # 1) metric-pair <-> value-pair, positional
    for pr in pairs:
        pv = _nearest_pair(seg, pr, values)
        if pv is None:
            continue
        a, b = _split_pair(pv["pair"])
        for mm, part, idx in ((pr[0], a, 0), (pr[1], b, 1)):
            ms.append(_measurement(
                masked, coff, mm["canon"], mm["surface"],
                [coff + mm["start"], coff + mm["end"]],
                part, pv, idx, "positional-pair"))
        pv["used"] = True
        for m in pr:
            metrics.remove(m)

    # 2) single values: value-first, else metric-first (value-first
    #    wins when both are possible, so "X% metric and Y% metric2"
    #    binds each number to the metric it touches)
    for v in values:
        if v["used"] or v["pair"]:
            continue
        m_after = _connect_next(seg, v, metrics)
        if m_after is not None:
            ms.append(_measurement(
                masked, coff, m_after["canon"], m_after["surface"],
                [coff + m_after["start"], coff + m_after["end"]],
                v["surface"], v, 0, "value-first"))
            v["used"] = True
            metrics.remove(m_after)
            continue
        m_before = _connect_prev(seg, v, metrics)
        if m_before is not None:
            ms.append(_measurement(
                masked, coff, m_before["canon"], m_before["surface"],
                [coff + m_before["start"], coff + m_before["end"]],
                v["surface"], v, 0, "metric-first"))
            v["used"] = True
            metrics.remove(m_before)

    # 3) value-pairs without a metric-pair: same-sentence frame, or
    #    an explicit ambiguous record (visible, never compared)
    for v in values:
        if v["used"] or not v["pair"]:
            continue
        v["used"] = True
        a, b = _split_pair(v["pair"])
        if frame:
            for mm, part, idx in ((frame[0], a, 0), (frame[1], b, 1)):
                ms.append(_measurement(masked, coff, mm, mm, None,
                                       part, v, idx,
                                       "sentence-frame"))
        else:
            for part, idx in ((a, 0), (b, 1)):
                mrec = _measurement(masked, coff, None, None, None,
                                    part, v, idx, "unresolved-pair")
                mrec["ambiguous"] = True
                ms.append(mrec)

    new_frame = [m["canon"] for m in pairs[0]] if pairs else frame
    return ms, new_frame
