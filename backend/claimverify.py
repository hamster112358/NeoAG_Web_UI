"""S7 independent claim verification (PART 6 of the M8 brief).

claims.py (S6) extracts measurements with a sequential token
grammar. An extraction must NEVER vouch for itself, so this module
re-derives every (subject, metric, value) association a second time
using a DIFFERENT strategy:

  * proximity: within a sentence-bounded window around the recorded
    value span, a metric term counts as supporting the value only
    when it lies within _MAX_GAP characters AND no other number sits
    between metric and value AND the gap is not a CI/p-value label;
  * cross-subject: the nearest known tool name to the value is
    computed by raw character distance - if it disagrees with the
    extraction's subject, the verdict is 'correct' with the closer
    name;
  * span integrity: the recorded spans must re-project onto the
    ORIGINAL document text (value text, metric surface, subject
    alias), and the recorded number must re-parse from the span.

Verdicts (one per measurement):
  confirm  - the independent strategy derives the same association
  correct  - the association exists but a component is wrong;
             'found_*' fields carry the better reading
  reject   - the association cannot be grounded at all (span does
             not reproduce, number is a CI bound/level, metric
             absent, subject absent)

Downstream (S9) may only USE confirm-ed measurements; 'correct' ones
are usable with the found_* replacement; 'reject' ones are listed
with their reason and excluded from every comparison. The verifier
imports ONLY the metric vocabulary from claims.py - no binding code.
"""
from __future__ import annotations

import re

from .claims import METRIC_TERMS

_MAX_GAP = 60          # metric <-> value, chars
_NUM_RX = re.compile(r"\d[\d.,]*\s?%?")
_CI_NEAR_RX = re.compile(r"(?:\bci\b|confidence|credible)", re.I)
_PVAL_RX = re.compile(r"^[pP]\s*[<>=]", re.I)
_ALIAS_RX_CACHE: dict[str, re.Pattern] = {}


def _blank_tags(text: str) -> str:
    return re.sub(r"<[^>]{1,120}?>", lambda m: " " * len(m.group(0)),
                  text or "")


def _metric_rx() -> re.Pattern:
    alts = [t for t, _ in METRIC_TERMS]
    return re.compile("(" + ")|(".join(alts) + ")", re.I)


_METRIC_RX = _metric_rx()
_CANON = {i + 1: cname for i, (_, cname) in enumerate(METRIC_TERMS)}


def _canon(m: re.Match) -> str:
    for gi in range(1, len(METRIC_TERMS) + 1):
        if m.group(gi) is not None:
            return _CANON[gi]
    return m.group(0).lower()


def _num(tok: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", tok or "")
    return float(m.group(0)) if m else None


def _alias(name: str) -> re.Pattern:
    rx = _ALIAS_RX_CACHE.get(name)
    if rx is None:
        rx = re.compile(rf"(?:^|[\s\-/]){re.escape(name)}(?=$|[\s\-/])",
                        re.I)
        _ALIAS_RX_CACHE[name] = rx
    return rx


def _sentence_bounds(doc: str, a: int, b: int) -> tuple[int, int]:
    lo = max(doc.rfind(". ", 0, a), doc.rfind("; ", 0, a),
             doc.rfind(".\n", 0, a))
    m = re.search(r"[.;](?=\s|$)", doc[b:])
    hi = b + (m.end() if m else len(doc) - b)
    return max(lo + 1, 0), min(hi + 1, len(doc))


def _numbers_between(doc: str, a: int, b: int, skip: list[tuple[int, int]]
                     ) -> bool:
    """True when some number strictly between a and b is neither
    part of the value itself nor of an accepted skip span."""
    for m in _NUM_RX.finditer(doc, max(a, 0), min(b, len(doc))):
        s, e = m.start(), m.end()
        if not m.group(0).strip(" .,"):
            continue
        if any(s >= sa and e <= eb for sa, eb in skip):
            continue
        return True
    return False


def verify_measurement(doc: str, claim: dict, meas: dict,
                       known_names=()) -> dict:
    """Independent verdict for ONE measurement of ONE claim."""
    doc = _blank_tags(doc)
    v = meas.get("value")
    metric = meas.get("metric")
    vspan = meas.get("value_span") or [0, 0]
    out = {"verdict": "reject", "found_metric": None,
           "found_value": None, "found_subject": None, "note": ""}

    # --- span integrity ------------------------------------------
    raw_val = doc[vspan[0]:vspan[1]].strip()
    if not raw_val or (raw_val.replace(" ", "")
                       != (v or "").strip().replace(" ", "")):
        out["note"] = f"value span {raw_val!r} != claimed {v!r}"
        return out
    is_pct = raw_val.endswith("%")
    if _num(raw_val) is None or _num(raw_val) != _num(v):
        out["note"] = "value number does not re-parse from span"
        return out
    mspan = meas.get("metric_span")
    if mspan and doc[mspan[0]:mspan[1]].strip().lower() != \
            (meas.get("metric_surface") or "").lower():
        out["note"] = "metric span does not reproduce its surface"
        return out

    # --- CI / p-value contamination ------------------------------
    after = doc[vspan[1]:vspan[1] + 14]
    if _num(raw_val) in (90.0, 95.0, 97.5, 99.0) \
            and _CI_NEAR_RX.search(doc[vspan[1]:vspan[1] + 8]):
        out["note"] = "number is a confidence LEVEL, not a value"
        return out
    if _PVAL_RX.match(after.strip()):
        out["note"] = "number is adjacent to a p-value label"
        return out
    # value sitting inside a bracketed interval ("(64.0-85.3%)")
    if doc[max(0, vspan[0] - 1):vspan[0]] in "([" or re.match(
            r"\s*[-\u2013\u2014]\s*\d+\.\d+\s?%\s*[)\]]", after):
        out["note"] = "number is a confidence-interval bound"
        return out

    if metric is None:
        out["note"] = (meas.get("binding") or "") + \
                      ": metric unresolved"
        return out

    lo, hi = _sentence_bounds(doc, vspan[0], vspan[1])
    window = doc[lo:hi]

    # --- metric support: strict proximity for directly-bound
    #     values, sentence-level attestation (with clean subject)
    #     for frame-inherited ones ---------------------------------
    binding = meas.get("binding") or ""
    permissive = binding in ("sentence-frame", "positional-pair")
    max_gap = 170 if binding == "sentence-frame" else _MAX_GAP
    veto = not permissive
    direct_gap: int | None = None
    closer: str | None = None
    closer_gap = 10 ** 9
    for m in _METRIC_RX.finditer(window):
        canon = _canon(m)
        ms_, me_ = lo + m.start(), lo + m.end()
        if ms_ >= vspan[1]:
            gap, na, nb = ms_ - vspan[1], vspan[1], ms_
        elif me_ <= vspan[0]:
            gap, na, nb = vspan[0] - me_, me_, vspan[0]
        else:
            continue
        if gap > max_gap:
            continue
        seg = doc[na:nb]
        if _CI_NEAR_RX.search(seg) or _PVAL_RX.match(seg.strip()):
            continue
        if veto and _numbers_between(doc, na, nb, skip=[]):
            continue          # another number sits between
        if canon == metric:
            direct_gap = gap if direct_gap is None else min(direct_gap,
                                                            gap)
        elif gap < closer_gap:
            closer, closer_gap = canon, gap
    if direct_gap is None:
        if closer is not None and not permissive:
            out["verdict"] = "correct"
            out["found_metric"] = closer
            out["note"] = (f"nearest unblocked metric is {closer!r}, "
                           f"not {metric!r}")
            return out
        out["note"] = (f"no unblocked occurrence of {metric!r} "
                       f"within {max_gap} chars of the value")
        return out
    if (not permissive) and closer is not None \
            and closer_gap + 4 < direct_gap:
        out["verdict"] = "correct"
        out["found_metric"] = closer
        out["note"] = (f"{closer!r} sits closer to the value than "
                       f"{metric!r}")
        return out

    # --- subject cross-check --------------------------------------
    # The claimed subject must be PRESENT in the sentence (any tool
    # name, not only known candidates). A rival candidate name only
    # outranks it when the rival is closer to the value AND sits in
    # the same clause (no comma/semicolon between rival and value).
    names = {str(n).lower() for n in known_names or () if n}
    subject = (claim.get("subject") or "").lower()

    def dist(nm: str) -> int | None:
        best = None
        for mm in _alias(nm).finditer(doc, lo, hi):
            s, e = mm.start(), mm.end()
            d = (vspan[0] - e) if e <= vspan[0] else \
                (s - vspan[1]) if s >= vspan[1] else 0
            best = d if best is None or d < best else best
        return best

    def same_clause_as_value(nm: str) -> bool:
        for mm in _alias(nm).finditer(doc, lo, hi):
            s, e = mm.start(), mm.end()
            a, b = (e, vspan[0]) if e <= vspan[0] else \
                (vspan[1], s) if s >= vspan[1] else (0, 0)
            if a <= b and not any(ch in doc[a:b] for ch in ",;"):
                return True
        return False

    claimed = dist(subject) if subject else None
    if claimed is not None and claimed > 120:
        claimed = None            # same sentence, but not its topic
    if claimed is None:
        rivals = [(nm, d) for nm in names if nm != subject
                  for d in [dist(nm)] if d is not None and d <= 60
                  and same_clause_as_value(nm)]
        if rivals:
            nm, _ = min(rivals, key=lambda x: x[1])
            out["verdict"] = "correct"
            out["found_subject"] = nm
            out["note"] = (f"{subject or '?'} not the sentence's "
                           f"tool; same-clause name is {nm!r}")
            return out
        out["note"] = (f"subject {subject or '?'} not present near "
                       "the value")
        return out
    nearer = [nm for nm in names
              if nm != subject and same_clause_as_value(nm)
              and dist(nm) is not None and dist(nm) < claimed]
    if nearer:
        nm = min(nearer, key=lambda x: dist(x))
        out["verdict"] = "correct"
        out["found_subject"] = nm
        out["note"] = (f"{nm!r} is closer to the value than "
                       f"{subject!r}")
        return out

    out["verdict"] = "confirm"
    out["note"] = "independent proximity parse reproduces the " \
                  "association"
    return out


def verify_claims(doc: str, claims: list[dict], known_names=()
                  ) -> list[dict]:
    """Verdict per claim: 'rejected' only when EVERY measurement is
    rejected; 'corrected' when any measurement was corrected."""
    out = []
    for c in claims:
        verdicts = [verify_measurement(doc, c, m, known_names)
                    for m in c.get("measurements", [])]
        states = {v["verdict"] for v in verdicts}
        overall = ("confirmed" if states == {"confirm"} else
                   "corrected" if "correct" in states else
                   "rejected")
        out.append({"doc_cid": c.get("doc_cid"),
                    "subject": c.get("subject"),
                    "ambiguous": c.get("ambiguous"),
                    "verdict": overall,
                    "quote_span": c.get("quote_span"),
                    "measurement_verdicts": verdicts})
    return out
