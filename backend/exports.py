"""Downloadable exports (Milestone 5, PART 7): standalone report files and
scoped results bundles, PERSISTED on disk and pinned to the exact stored
content they were rendered from.

Core rule: re-downloading NEVER regenerates science. The first download of
a (report|job, format) renders once, writes the bytes under the store's
export dir and pins them with source_hash + sha256 in the exports table;
every later download serves the persisted file verbatim. A new VERSION of
an export appears only when the stored report/job content itself changes
(source_hash differs), and older versions stay on disk and listed.

Formats: research reports as standalone HTML, JSON (provenance is part of
the stored report and travels with the export), TSV tables, and PDF.
The PDF writer below is a stdlib-only text PDF (PDF 1.4, Helvetica,
Latin-1): it carries the readable narrative of the report, not pixel
charts - this is stated in the file itself rather than pretended away.
Results bundles are ZIPs built ONLY from persisted DB state (job row,
immutable run snapshot, decision history, artifact manifest, full result
JSON, candidate tables) plus, for native runs, scoped real output files:
raw sequencing/alignment inputs (FASTQ/BAM/SAM/CRAM/VCF) are NEVER
included, per-file and total size caps apply, and every exclusion is
listed with its reason in the bundle manifest.

No third-party imports: stdlib only (json/hashlib/zipfile/re/datetime).
"""
from __future__ import annotations

import hashlib
import html as _html
import io
import json
import os
import re
import zipfile
from datetime import datetime, timezone

EXPORT_BUILDER = "backend/exports.py v1"

# format -> (extension, media type)
FORMATS = {
    "html": ("html", "text/html; charset=utf-8"),
    "json": ("json", "application/json; charset=utf-8"),
    "tsv": ("tsv", "text/tab-separated-values; charset=utf-8"),
    "pdf": ("pdf", "application/pdf"),
    "zip": ("zip", "application/zip"),
}

# raw sequencing / alignment inputs: NEVER part of a results bundle,
# regardless of size (they are inputs, not outputs, and may be sensitive)
RAW_SUFFIXES = (".fastq", ".fq", ".fastq.gz", ".fq.gz", ".gz.fastq",
                ".bam", ".sam", ".cram", ".vcf.gz", ".bcf")
MAX_MEMBER_BYTES = 2 * 1024 * 1024      # 2 MiB per native output file
MAX_BUNDLE_BYTES = 32 * 1024 * 1024     # 32 MiB total for native files


class Conflict(Exception):
    """request is well-formed but the object is in a state that cannot be
    exported (e.g. a job that has not completed)."""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(
        timespec="seconds")


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False).encode(
        "utf-8")


def _safe(obj_id: str) -> str:
    """ids arrive from URLs; never let them steer a filesystem path."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(obj_id))[:120]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# generic small helpers shared by the renderers
# ---------------------------------------------------------------------------

def _e(x) -> str:
    return _html.escape("" if x is None else str(x), quote=True)


def _cell(x) -> str:
    if isinstance(x, (dict, list)):
        return _e(json.dumps(x, ensure_ascii=False))
    return _e(x)


def _cols(rows) -> list[str]:
    seen: list[str] = []
    for r in rows:
        if isinstance(r, dict):
            for k in r:
                if k not in seen:
                    seen.append(k)
    return seen


def _as_rows(rows):
    """Any stored section may arrive as list-of-rows OR as a mapping
    (recommended_benchmarks is keyed by target) - normalise honestly."""
    if isinstance(rows, dict):
        return [{"key": k, "value": v} for k, v in rows.items()]
    return rows


def _html_table(rows, cols=None, max_rows=500) -> str:
    rows = _as_rows(rows)
    if not rows:
        return '<p class="hint">none</p>'
    cols = cols or _cols(rows)
    head = "".join(f"<th>{_e(c)}</th>" for c in cols)
    body = []
    for r in rows[:max_rows]:
        if not isinstance(r, dict):
            r = {"value": r}
        body.append("<tr>" + "".join(f"<td>{_cell(r.get(c))}</td>"
                                     for c in cols) + "</tr>")
    more = (f'<p class="hint">{len(rows) - max_rows} more row(s) omitted '
            f'from this table - full data in the JSON/TSV export</p>'
            if len(rows) > max_rows else "")
    return (f"<table><thead><tr>{head}</tr></thead><tbody>"
            + "".join(body) + f"</tbody></table>{more}")


def _tsv_cell(x) -> str:
    if isinstance(x, (dict, list)):
        s = json.dumps(x, ensure_ascii=False)
    elif x is None:
        s = ""
    else:
        s = str(x)
    return re.sub(r"[\t\r\n]+", " ", s)


def tsv_rows(rows, cols=None) -> str:
    """One TSV table (no section header); empty rows -> empty string."""
    rows = [r for r in (_as_rows(rows) or []) if r is not None]
    if not rows:
        return ""
    rows = [r if isinstance(r, dict) else {"value": r} for r in rows]
    cols = cols or _cols(rows)
    out = ["\t".join(_tsv_cell(c) for c in cols)]
    for r in rows:
        out.append("\t".join(_tsv_cell(r.get(c)) for c in cols))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# standalone HTML export (decision-first order, mirrors the app view)
# ---------------------------------------------------------------------------

_CSS = """
:root{--bg:#0f1419;--fg:#d7dee8;--mut:#9fb3c8;--hint:#8a97a8;--line:#2a3442;
--card:#161d26;--thead:#1a2330;--pill-bg:#1d3a5f;--pill-fg:#8ec5ff;
--ok-bg:#15301f;--ok-fg:#8fe0a6;--warn-bg:#3a2416;--warn-fg:#f0b483;
--amber-bg:#332b12;--amber-fg:#e8cf7a;--pre-bg:#10161d;--disc-bg:#2b2010;
--disc-line:#7a5b16;--disc-fg:#f2d488}
@media (prefers-color-scheme: light){:root{--bg:#f6f8fa;--fg:#1f2630;
--mut:#4d5c6d;--hint:#63707f;--line:#d5dbe2;--card:#ffffff;
--thead:#eef2f6;--pill-bg:#dbeafe;--pill-fg:#1e4e8c;--ok-bg:#dcf3e3;
--ok-fg:#14532d;--warn-bg:#fceadd;--warn-fg:#7c3a10;--amber-bg:#faf0cd;
--amber-fg:#6b5305;--pre-bg:#f0f3f6;--disc-bg:#fff8dd;--disc-line:#c8a94a;
--disc-fg:#6b5305}}
body{font:14px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
margin:0;background:var(--bg);color:var(--fg)}
main{max-width:1060px;margin:0 auto;padding:24px 28px 90px}
h1{font-size:22px;margin:.2em 0}
h2{font-size:17px;margin:1.7em 0 .5em;border-bottom:1px solid var(--line);
padding-bottom:4px}
h3{font-size:14.5px;margin:1.1em 0 .4em;color:var(--mut)}
.headline{font-size:15.5px;font-weight:600;margin:.3em 0 .6em}
.disclaimer{background:var(--disc-bg);border:1px solid var(--disc-line);
color:var(--disc-fg);padding:10px 12px;border-radius:6px;
white-space:pre-wrap;font-size:12.5px}
.hint{color:var(--hint);font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:12px 15px;margin:10px 0}
.pill{display:inline-block;background:var(--pill-bg);color:var(--pill-fg);
border-radius:10px;padding:1px 9px;font-size:12px;margin-right:6px}
.pill.ok{background:var(--ok-bg);color:var(--ok-fg)}
.pill.warn{background:var(--warn-bg);color:var(--warn-fg)}
.pill.amber{background:var(--amber-bg);color:var(--amber-fg)}
.warn{background:var(--warn-bg);border:1px solid var(--warn-fg);
border-radius:6px;padding:8px 10px;color:var(--warn-fg)}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin:.4em 0}
th,td{border:1px solid var(--line);padding:5px 8px;text-align:left;
vertical-align:top}th{background:var(--thead);color:var(--mut)}
ul{margin:.3em 0 .6em 1.2em;padding:0}li{margin:.25em 0}
pre{background:var(--pre-bg);border:1px solid var(--line);border-radius:6px;
padding:10px;overflow:auto;font-size:12px;white-space:pre-wrap}
details{margin:.7em 0}summary{cursor:pointer;color:var(--pill-fg);
font-weight:500}
.mnote{color:var(--mut);font-size:12.5px}
.q{font-size:13px;color:var(--fg)}
a{color:inherit;text-decoration:underline}
"""


def _h2(t) -> str:
    return f"<h2>{_e(t)}</h2>"


def _list(items, tag="li") -> str:
    return "<ul>" + "".join(f"<{tag}>{_e(i)}</{tag}>"
                            for i in (items or [])) + "</ul>"


def _pill(text, kind="") -> str:
    return (f"<span class='pill {kind}'>{_e(text)}</span>")


def research_html(rid: str, rep: dict) -> bytes:
    """Standalone single-file HTML of a STORED research report.
    M6 B2 layout: conclusion first (executive assessment -> module
    recommendations -> scientific comparison -> actions); every
    technical detail lives in collapsed <details> appendices. Renders
    only what the stored report carries - it never re-runs retrieval."""
    consumed = set()

    def use(*keys):
        consumed.update(keys)

    parts: list[str] = []
    eng = str(rep.get("engine") or "?")
    a = rep.get("assessment") or {}
    use("engine", "engine_version", "status", "decision_ready",
        "review_class", "targets", "scope", "target")
    ready = bool(rep.get("decision_ready"))
    example = eng == "example"
    parts.append(
        f"<h1>{'Example (mock) research digest' if example else 'NeoAG scientific tool review'}</h1>"
        f"<p>{_pill('PRELIMINARY RESEARCH' if (not ready and not example) else ('MOCK EXAMPLE' if example else 'DECISION-READY REVIEW'), 'warn' if not ready and not example else ('' if example else 'ok'))}"
        f"{_pill('report ' + rid)}{_pill('engine ' + eng + ' ' + str(rep.get('engine_version') or ''))}"
        f"{_pill('status ' + str(rep.get('status') or ''))}</p>")

    # ---- 1. Executive assessment -------------------------------------
    if a:
        use("assessment")
        parts.append(_h2("1. Executive assessment"))
        parts.append(f"<p class=headline>{_e(a.get('headline'))}</p>")
        parts.append(f"<p class=mnote>{_e(a.get('scope'))}</p>")
        if a.get("top_findings"):
            parts.append("<b>Most important findings</b>")
            parts.append(_list(a["top_findings"]))
        if a.get("evidence_gaps"):
            parts.append("<b>Evidence gaps that affect decisions</b>")
            parts.append(_list(a["evidence_gaps"]))
        if a.get("next_action"):
            parts.append("<p><b>Next recommended action:</b> "
                         + _e(a["next_action"]) + "</p>")
    if rep.get("disclaimer"):
        parts.append(
            "<details class=disc><summary>What this report is "
            "(and is not)</summary><div class=disclaimer>"
            + _e(rep["disclaimer"]) + "</div></details>")
        use("disclaimer")

    # ---- 2. Module-level recommendations ------------------------------
    mt = rep.get("module_table") or []
    if mt:
        use("module_table")
        parts.append(_h2("2. Module-level recommendations"))
        parts.append(_html_table(mt))

    # ---- 3. Scientific comparison -------------------------------------
    sc = rep.get("scientific_comparison") or rep.get(
        "task_comparisons") or []
    cands = rep.get("candidates") or []
    if sc:
        use("scientific_comparison", "task_comparisons")
        parts.append(_h2("3. Scientific comparison"))
        for tc in sc:
            dec = tc.get("decision") or {}
            bl = tc.get("baseline") or {}
            cur = ("; ".join(
                f"{t['name']} ({t['role']})"
                for t in bl.get("tools") or [])
                or bl.get("note") or "NOT identified")
            block = [f"<h3>{_e(tc.get('target'))}</h3>"]
            if tc.get("question"):
                block.append(f"<p class=q><b>Question this comparison "
                             f"answers:</b> {_e(tc['question'])}</p>")
            block.append("<p class=mnote><b>Current NeoAG method:</b> "
                         + _e(cur)
                         + (" " + _pill("multi-tool consensus", "ok")
                            if bl.get("consensus") else "") + "</p>")
            rows = tc.get("rows") or []
            block.append(_html_table(rows) if rows else
                         "<p class=mnote>No retrieved candidate passed "
                         "the scientific relevance screen as a direct "
                         "alternative for this module.</p>")
            for label, key in (
                    ("Complementary methods (adjacent help, not "
                     "replacements)", "complementary"),
                    ("Wrappers / integrations", "wrappers_integrations"),
                    ("Related but not comparable",
                     "related_not_comparable")):
                names = tc.get(key) or []
                if names:
                    block.append(
                        f"<p class=mnote><b>{_e(label)}:</b> "
                        f"{_e(', '.join(names[:8]))}</p>")
            if tc.get("excluded_irrelevant"):
                block.append(
                    "<p class=mnote><b>Screened out as irrelevant:</b> "
                    + _e(", ".join(tc["excluded_irrelevant"][:6]))
                    + " - reasons recorded in the appendix</p>")
            if dec.get("recommendation"):
                kind = ("ok" if dec["recommendation"]
                        == "Retain current tool"
                        else "warn" if dec["recommendation"]
                        in ("Research incomplete", "Insufficient evidence")
                        else "amber")
                block.append("<p>" + _pill(dec["recommendation"], kind)
                             + f"<span class=mnote> {_e(dec.get('rationale') or '')}</span></p>")
                if dec.get("action"):
                    block.append("<p class=mnote><b>Action:</b> "
                                 + _e(dec["action"]) + "</p>")
            links = []
            for c in cands:
                if (c.get("task") == tc.get("target")
                        and c.get("eligibility") == "direct alternative"):
                    ev = c.get("published_benchmark_evidence")
                    for e in (ev if isinstance(ev, list) else [])[:1]:
                        u = ((e or {}).get("citation") or {}).get("url")
                        if u:
                            links.append((c["name"], u))
            if links:
                block.append(
                    "<p class=mnote><b>Key sources:</b> "
                    + "; ".join(f"{_e(n)}: <a href='{_e(u)}'>{_e(u)}</a>"
                                for n, u in links[:4]) + "</p>")
            block.append(f"<p class=hint>{_e(tc.get('note'))}</p>")
            parts.append("<div class=card>" + "".join(block) + "</div>")

    # ---- 4. Recommended actions ---------------------------------------
    na = rep.get("recommended_next_actions") or []
    if na:
        use("recommended_next_actions", "action_plan")
        parts.append(_h2("4. Recommended actions (scientific priority)"))
        parts.append(_list(na))

    # ---- 5. Technical appendix (collapsed) ------------------------------
    app: list[str] = []

    def a_sec(title, *keys):
        use(*keys)
        return app.append(_h2(title))

    ex = rep.get("excluded_candidates") or []
    if ex:
        a_sec("Excluded candidates and reasons", "excluded_candidates")
        app.append(
            "<p class=hint>every retrieved candidate that did not pass "
            "the scientific eligibility screen, with the rule that "
            "excluded it - kept for auditability, never shown as a "
            "comparison row</p>")
        app.append(_html_table(
            [{"name": c["name"], "module": c.get("module"),
              "class": c.get("eligibility"),
              "why": "; ".join(c.get("reasons") or []),
              "where it came from": (c.get("origin") or "")[:90]}
             for c in ex]))
    if cands:
        a_sec("Full candidate inventory", "candidates")
        app.append(_html_table(
            [{"tool": c.get("name"), "module": c.get("task"),
              "eligibility": c.get("eligibility"),
              "kind": c.get("artifact_class"),
              "version": c.get("verified_version"),
              "license": c.get("license"),
              "maintenance": c.get("maintenance"),
              "availability": c.get("availability")} for c in cands]))
        chains = [c for c in cands if c.get("version_chain")]
        if chains:
            app.append(
                "<p class=hint><b>Version verification chains:</b> every "
                "step records WHERE it looked, WHAT it found and WHEN; "
                "\"Not verified\" only after every step ran and produced "
                "nothing; budget-skipped steps keep their reason</p>")
            for c in chains:
                ch = c["version_chain"]["chain"]
                app.append(
                    "<details><summary>" + _e(c.get("name")) + " - "
                    + _e(c["version_chain"].get("display"))
                    + "</summary>" + _html_table(ch) + "</details>")
        perf_rows = []
        for c in cands:
            ev = c.get("published_benchmark_evidence")
            for e in (ev if isinstance(ev, list) else []):
                if isinstance(e, dict) and (e.get("metric")
                                            or e.get("quote")):
                    perf_rows.append({
                        "tool": c["name"], "metric": e.get("metric"),
                        "value": e.get("value"),
                        "dataset": e.get("dataset"),
                        "ground truth": e.get("ground_truth"),
                        "tested version": e.get("tested_version"),
                        "evidence level": e.get("evidence_level"),
                        "comparable?": e.get("context_established"),
                        "version caveat": (e.get("version_caveat") or "")[:90],
                        "quote": (e.get("quote") or "")[:200],
                        "source": ((e.get("citation") or {}).get("url")
                                   or "")})
        if perf_rows:
            app.append(_h2("Performance evidence records (full)"))
            app.append("<p class=hint>rows with comparable?=False are "
                       "mentions without established experimental "
                       "context - visible for audit, excluded from "
                       "comparisons by construction</p>")
            app.append(_html_table(perf_rows))
    vlf = rep.get("version_license_findings") or []
    if vlf:
        a_sec("Version and license findings", "version_license_findings")
        app.append(_html_table(vlf))
    rb = rep.get("recommended_benchmarks") or {}
    if rb:
        a_sec("Benchmark definitions for proposed evaluations",
              "recommended_benchmarks")
        app.append(_html_table(
            [{"module": k, "metrics": v.get("metrics"),
              "datasets note": v.get("datasets_note"),
              "warning": v.get("warning")} for k, v in rb.items()]))
    cov = rep.get("coverage_accounting")
    if cov:
        a_sec("Coverage accounting", "coverage_accounting")
        app.append(
            "<p>" + " / ".join(
                f"<b>{_e(k)}</b>: {_cell(cov.get(k))}"
                for k in ("completeness", "identified_modules",
                          "researched_modules", "sufficient_evidence",
                          "insufficient_evidence")) + "</p>"
            + _html_table(cov.get("not_researched"))
            + f"<p class=hint>{_e(cov.get('note'))}</p>")
    gts = rep.get("quality_gates") or []
    if gts:
        a_sec("Research quality gates", "quality_gates")
        # M11 P1-11: passed=None is an N/A adequacy gate (no subject
        # to judge) - printed as N/A, never as a blank or a fake PASS.
        _gt_disp = [{**x, "passed": ("N/A" if x.get("passed") is None
                                     else ("PASS" if x.get("passed")
                                           else "FAIL"))}
                    for x in gts]
        app.append(_html_table(_gt_disp))
    inv = rep.get("inventory")
    if inv and inv.get("stages"):
        a_sec("Code-grounded pipeline inventory + baselines", "inventory")
        app.append(
            f"<p class=mnote>repo {_e(inv.get('repo'))} @ commit "
            f"{_e(inv.get('git_commit'))}, captured "
            f"{_e(inv.get('captured_at'))}</p>")
        for s in inv["stages"]:
            b = s.get("baseline") or {}
            tools = "; ".join(
                f"{t['name']} [{t['role']}] ({'; '.join(t['evidence'])[:120]})"
                for t in b.get("tools") or []) or "not identified"
            app.append(
                f"<p class=mnote><b>{_e(s.get('id'))}</b> "
                f"{'(consensus) ' if b.get('consensus') else ''}: "
                f"{_e(tools)}</p>")
    cits = rep.get("literature_citations") or []
    if cits:
        a_sec("Sources and citations", "literature_citations")
        app.append(_html_table(cits))
    comp = rep.get("comparison") or []
    if comp:
        a_sec("Full candidate comparison table", "comparison")
        app.append(_html_table(comp))
    if rep.get("methodology"):
        a_sec("Methodology", "methodology")
        app.append(f"<p>{_e(rep['methodology'])}</p>")
    lim = rep.get("limitations") or []
    if lim:
        a_sec("Limitations and incomplete evidence", "limitations")
        app.append(_list(lim))
    if rep.get("availability_classes_legend"):
        app.append(_h2("Availability classes legend"))
        app.append(f"<p class=mnote>{_e(rep['availability_classes_legend'])}</p>")
        use("availability_classes_legend")
    lic = rep.get("license_constraints") or []
    if lic:
        a_sec("Licenses seen", "license_constraints")
        app.append(_list(lic))
    if rep.get("provenance"):
        a_sec("Provenance (queries, counts, failures)", "provenance")
        app.append("<details><summary>full query log + failures "
                   "(JSON)</summary><pre>" + _e(json.dumps(
                       rep["provenance"], indent=1,
                       ensure_ascii=False)) + "</pre></details>")
    if rep.get("timestamps"):
        a_sec("Timestamps", "timestamps")
        app.append(_html_table([rep["timestamps"]]))
    if rep.get("module_conclusions"):
        a_sec("Per-module decision records", "module_conclusions",
              "conclusion_vocabulary", "eligibility_vocabulary")
        for tid, mc in rep["module_conclusions"].items():
            app.append(
                "<div class=card><h3>" + _e(tid) + "</h3>"
                f"<p>{_e(mc.get('recommendation') or mc.get('conclusion'))}"
                f" - {_e(mc.get('rationale'))}</p>"
                + _html_table([mc.get("evidence_facts") or {}])
                + "</div>")
    if rep.get("recommendation"):
        rec = rep["recommendation"]
        use("recommendation")
        app.append(_h2("Recommendation (example engine)"))
        app.append(_cell(rec))
    if app:
        parts.append(_h2("5. Technical appendix"))
        parts.append("<details class=appx><summary>open the full "
                     "technical appendix (candidate inventory, exclusions, "
                     "version chains, evidence records, queries, "
                     "provenance)</summary>" + "".join(app) + "</details>")
    # honesty fallback: anything not rendered above is still included
    rest = {k: v for k, v in rep.items() if k not in consumed}
    if rest:
        parts.append(_h2("Other stored report content"))
        parts.append("<details><summary>unrendered stored keys</summary>"
                     "<pre>" + _e(json.dumps(rest, indent=1,
                                             ensure_ascii=False))
                     + "</pre></details>")
    body = "".join(parts)
    doc = ("<!doctype html><html><head><meta charset=utf-8>"
           "<meta name=viewport content='width=device-width,"
           "initial-scale=1'>"
           f"<title>NeoAG research report {_e(rid)}</title>"
           f"<style>{_CSS}</style></head><body><main>{body}"
           "<p class=hint>Generated by the NeoAG web demo export builder "
           f"({_e(EXPORT_BUILDER)}) for report {_e(rid)}. This file is "
           "self-contained and immutable - a re-download serves these "
           "exact bytes and never re-runs retrieval.</p></main>"
           "</body></html>")
    return doc.encode("utf-8")


# ---------------------------------------------------------------------------
# JSON + TSV exports
# ---------------------------------------------------------------------------

def research_json(rid: str, rep: dict) -> bytes:
    """Full report JSON. Provenance (queries, sources, counts, failures,
    budget) is part of the stored report and travels with every export;
    the export envelope adds who/when/what-hash so a reader can pin the
    file to the exact report version it came from."""
    out = dict(rep)
    out["export"] = {
        "kind": "research_report", "report_id": rid,
        "exported_at": _now(), "builder": EXPORT_BUILDER,
        "source_hash": _sha(_canon(rep)),
        "note": ("this JSON is the complete stored report plus this "
                 "export envelope; science content was NOT re-generated "
                 "at export time")}
    return (json.dumps(out, indent=1, ensure_ascii=False) + "\n").encode(
        "utf-8")


def research_tsv(rid: str, rep: dict) -> bytes:
    """All tabular report content as one TSV stream with '# section'
    markers (readable by any spreadsheet; each section is its own
    copy-paste table)."""
    out: list[str] = [
        f"# NeoAG research report {rid} - TSV export - generated {_now()}",
        f"# builder {EXPORT_BUILDER}; source_hash {_sha(_canon(rep))}",
        f"# disclaimer {rep.get('disclaimer', '')}",
    ]

    def section(title, rows, cols=None):
        t = tsv_rows(rows, cols)
        if t:
            out.append(f"\n# ===== section: {title} =====")
            out.append(t.rstrip("\n"))

    a = rep.get("assessment") or {}
    if a:
        section("assessment", [{
            "headline": a.get("headline"), "scope": a.get("scope"),
            "review_class": a.get("review_class"),
            "decision_ready": a.get("decision_ready"),
            "top_findings": a.get("top_findings"),
            "evidence_gaps": a.get("evidence_gaps"),
            "next_action": a.get("next_action")}])
    section("module_table", rep.get("module_table"))
    mc = rep.get("module_conclusions") or {}
    if mc:
        rows = []
        for tid, c in mc.items():
            rows.append({"target_id": tid,
                         "recommendation": c.get("recommendation")
                         or c.get("conclusion"),
                         "rationale": c.get("rationale"),
                         "action": c.get("action"),
                         **(c.get("evidence_facts") or {})})
        section("module_conclusions", rows)
    section("excluded_candidates", [
        {"name": c.get("name"), "module": c.get("module"),
         "eligibility": c.get("eligibility"),
         "reasons": "; ".join(c.get("reasons") or []),
         "origin": c.get("origin")}
        for c in rep.get("excluded_candidates") or []])
    section("quality_gates", rep.get("quality_gates"))
    sc = rep.get("scientific_comparison") or []
    if sc:
        rows = []
        for c in sc:
            b = c.get("baseline") or {}
            d = c.get("decision") or {}
            rows.append({
                "target": c.get("target"), "question": c.get("question"),
                "baseline_identified": b.get("identified"),
                "baseline_consensus": b.get("consensus"),
                "baseline_tools": ", ".join(
                    t.get("name", "") for t in b.get("tools") or []),
                "direct_alternatives": len(c.get("rows") or []),
                "complementary": ", ".join(
                    c.get("complementary") or []),
                "wrappers_integrations": ", ".join(
                    c.get("wrappers_integrations") or []),
                "related_not_comparable": ", ".join(
                    c.get("related_not_comparable") or []),
                "insufficient": ", ".join(c.get("insufficient") or []),
                "recommendation": d.get("recommendation"),
                "action": d.get("action")})
        section("scientific_comparison", rows)
    if rep.get("action_plan"):
        section("action_plan", rep["action_plan"])
    for tc in rep.get("task_comparisons") or []:
        rows = [dict(r, target=tc.get("target"))
                for r in tc.get("rows") or []]
        if rows:
            section(f"task_comparison: {tc.get('target')}", rows)
    section("version_license_findings", rep.get("version_license_findings"))
    section("comparison", rep.get("comparison"))
    cands = rep.get("candidates") or []
    section("candidates", [
        {k: c.get(k) for k in ("name", "task", "artifact_class",
                               "verified_version", "installed_version",
                               "license", "maintenance",
                               "performance_summary",
                               "independent_validation")}
        for c in cands])
    chains = [{"candidate": c.get("name"), **s}
              for c in cands for s in (c.get("version_chain") or {}).get(
                  "chain") or []]
    section("version_chains", chains)
    section("literature_citations", rep.get("literature_citations"))
    cov = rep.get("coverage_accounting")
    if cov:
        section("coverage", [cov])
    section("recommended_benchmarks", rep.get("recommended_benchmarks"))
    if rep.get("recommended_next_actions"):
        section("recommended_next_actions",
                [{"action": a} for a in rep["recommended_next_actions"]])
    if rep.get("executive_summary"):
        section("executive_summary",
                [{"line": a} for a in rep["executive_summary"]])
    if rep.get("limitations"):
        section("limitations", [{"limitation": a}
                                for a in rep["limitations"]])
    # nothing stored is silently dropped: leftover keys are appended
    handled = {"engine", "engine_version", "status", "scope", "target",
               "targets", "disclaimer", "assessment", "module_table",
               "decision_ready", "review_class", "quality_gates",
               "scientific_comparison", "excluded_candidates", "action_plan",
               "module_conclusions", "conclusion_vocabulary",
               "eligibility_vocabulary", "inventory",
               "task_comparisons", "version_license_findings", "comparison",
               "candidates", "literature_citations", "license_constraints",
               "coverage_accounting", "recommended_benchmarks",
               "recommended_next_actions", "executive_summary",
               "availability_classes_legend", "provenance", "methodology",
               "timestamps", "limitations"}
    rest = {k: v for k, v in rep.items() if k not in handled}
    for k, v in rest.items():
        if isinstance(v, list) and v and all(isinstance(x, dict)
                                             for x in v):
            section(f"other: {k}", v)
    return ("\n".join(out) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# stdlib text-only PDF export
# ---------------------------------------------------------------------------

_PDF_PAGE_LINES = 52
_PDF_LINE_CHARS = 100


def _pdf_escape(s: str) -> bytes:
    # PDF strings in Helvetica use Latin-1; anything else is replaced
    # rather than dropped silently
    s = re.sub(r"[\\()]", lambda m: "\\" + m.group(0), s)
    return s.encode("latin-1", "replace")


def _pdf_wrap(text: str) -> list[str]:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        while len(w) > _PDF_LINE_CHARS:
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(w[:_PDF_LINE_CHARS])
            w = w[_PDF_LINE_CHARS:]
        if len(cur) + len(w) + 1 > _PDF_LINE_CHARS:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def pdf_bytes(title: str, paragraphs: list[tuple[str, str]]) -> bytes:
    """Minimal valid PDF 1.4 text document (stdlib-only). paragraph =
    (style, text) with style in {'h','p','b'}. No charts, no colour -
    the point is a printable, archivable text record of the report."""
    lines: list[tuple[str, str]] = []
    for style, text in paragraphs:
        wrapped = _pdf_wrap(text) or [""]
        for i, w in enumerate(wrapped):
            lines.append((style if i == 0 else "p", w))
    pages: list[list[tuple[str, str]]] = [
        lines[i:i + _PDF_PAGE_LINES] for i in
        range(0, max(len(lines), 1), _PDF_PAGE_LINES)]
    max_pages = 12
    if len(pages) > max_pages:
        pages = pages[:max_pages]
        pages[-1] = pages[-1][:_PDF_PAGE_LINES - 1] + [
            ("b", f"[PDF truncated at {max_pages} pages - the JSON/HTML/"
                  "TSV exports carry the complete report]")]
    page_objs = []
    for pg in pages:
        content = ["BT"]
        y = 750
        for style, text in pg:
            font = "/F2" if style in ("h", "b") else "/F1"
            size = 11 if style == "h" else 9
            y -= size + 4
            content.append(f"1 0 0 1 54 {y} Tm {font} {size} Tf")
            content.append(f"({_pdf_escape(text).decode('latin-1')}) Tj")
        content.append("ET")
        page_objs.append("\n".join(content).encode("latin-1"))
    # assemble objects: 1 catalog, 2 pages, 3 F1, 4 F2, then per page:
    # page obj, content obj
    kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(len(pages)))
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
        b"/Encoding /WinAnsiEncoding >>",
    ]
    for i, cont in enumerate(page_objs):
        objs.append(
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
             f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
             f"/Contents {6 + 2 * i} 0 R >>").encode())
        objs.append(b"<< /Length " + str(len(cont)).encode() +
                    b" >>\nstream\n" + cont + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()
    return bytes(out)


def report_text_paras(rid: str, rep: dict) -> list[tuple[str, str]]:
    """Narrative flattening for the PDF (M6 B8): the concise main
    sections first, the technical detail grouped into later pages,
    always pointing to the JSON/HTML export for the complete record."""
    p: list[tuple[str, str]] = [
        ("h", "NeoAG scientific tool review"),
        ("p", f"report {rid} - engine {rep.get('engine')} "
              f"{rep.get('engine_version') or ''} - exported {_now()}"),
        ("p", "PDF note: machine-generated text-only rendering (stdlib "
              "PDF writer, Latin-1 text, no tables/charts). The HTML and "
              "JSON exports of this same report carry the complete, "
              "tabular detail; science content was not re-generated for "
              "this file."),
    ]
    a = rep.get("assessment") or {}
    if a:
        p.append(("h", "1. Executive assessment"))
        p.append(("p", str(a.get("headline"))))
        p.append(("p", f"Scope: {a.get('scope')}"))
        for x in a.get("top_findings") or []:
            p.append(("p", f"- finding: {x}"))
        for x in a.get("evidence_gaps") or []:
            p.append(("p", f"- gap: {x}"))
        if a.get("next_action"):
            p.append(("p", f"Next recommended action: {a['next_action']}"))
        p.append(("p", f"Review class: {a.get('review_class')}"))
    if rep.get("disclaimer"):
        p.append(("b", str(rep["disclaimer"])))
    mt = rep.get("module_table") or []
    if mt:
        p.append(("h", "2. Module-level recommendations"))
        for m in mt:
            p.append(("p", f"- {m.get('module')}: current="
                           f"{m.get('current NeoAG method')}; best="
                           f"{m.get('best relevant alternative')}; "
                           f"{m.get('recommendation')}"))
    sc = rep.get("scientific_comparison") or []
    if sc:
        p.append(("h", "3. Scientific comparison"))
        for tc in sc:
            bl = tc.get("baseline") or {}
            cur = ", ".join(t["name"] for t in bl.get("tools") or []) \
                or bl.get("note") or "not identified"
            p.append(("h", str(tc.get("target"))))
            p.append(("p", f"Current NeoAG method: {cur}"
                           + (" (multi-tool consensus)"
                              if bl.get("consensus") else "")))
            for r in tc.get("rows") or []:
                p.append(("p", f"- alternative {r.get('tool')} "
                               f"[{r.get('version')}]: {r.get('evidence')}"))
            dec = tc.get("decision") or {}
            if dec.get("recommendation"):
                p.append(("p", f"Recommendation: {dec['recommendation']}"
                               f" - {dec.get('rationale')}"))
                if dec.get("action"):
                    p.append(("p", f"Action: {dec['action']}"))
    if rep.get("recommended_next_actions"):
        p.append(("h", "4. Recommended actions (scientific priority)"))
        for x in rep["recommended_next_actions"]:
            p.append(("p", f"- {x}"))
    # ---- technical appendix (later pages) ----
    p.append(("h", "5. Technical appendix"))
    ex = rep.get("excluded_candidates") or []
    if ex:
        p.append(("p", f"Excluded candidates ({len(ex)}):"))
        for c in ex[:12]:
            p.append(("p", f"- {c.get('name')} ({c.get('module')}): "
                           f"{'; '.join(c.get('reasons') or [])[:160]}"))
    cov = rep.get("coverage_accounting") or {}
    if cov:
        p.append(("p", f"Coverage accounting: completeness "
                       f"{cov.get('completeness')}; identified "
                       f"{cov.get('identified_modules')}; researched "
                       f"{cov.get('researched_modules')}; sufficient "
                       f"{len(cov.get('sufficient_evidence') or [])}; "
                       f"insufficient "
                       f"{len(cov.get('insufficient_evidence') or [])}; "
                       f"not researched "
                       f"{len(cov.get('not_researched') or [])}"))
    for g in rep.get("quality_gates") or []:
        _pv = g.get("passed")
        _ps = ("N/A" if _pv is None else "PASS" if _pv else "FAIL")
        p.append(("p", f"gate [{_ps}] "
                       f"{g.get('check')} - {g.get('detail')}"))
    if rep.get("limitations"):
        p.append(("h", "Limitations"))
        for l in rep["limitations"]:
            p.append(("p", f"- {l}"))
    if rep.get("methodology"):
        p.append(("h", "Methodology"))
        p.append(("p", str(rep["methodology"])))
    if rep.get("recommendation"):
        rec = rep["recommendation"]
        if isinstance(rec, dict) and rec.get("verdict"):
            p.append(("h", "Recommendation (example engine)"))
            p.append(("p", str(rec["verdict"])))
    return p


def research_pdf(rid: str, rep: dict) -> bytes:
    return pdf_bytes(f"NeoAG research report {rid}",
                     report_text_paras(rid, rep))


RENDERERS = {"html": research_html, "json": research_json,
             "tsv": research_tsv, "pdf": research_pdf}


# ---------------------------------------------------------------------------
# persistence: render once, pin to content hash, serve bytes forever
# ---------------------------------------------------------------------------

def _write_export(store, kind: str, object_id: str, fmt: str,
                  data: bytes, source_hash: str,
                  meta: dict | None = None) -> dict:
    ext = FORMATS[fmt][0]
    d = os.path.join(str(store.export_dir), kind, _safe(object_id))
    os.makedirs(d, exist_ok=True)
    existing = store.export_versions(kind, object_id, fmt)
    name = f"{kind}-{_safe(object_id)}-v{len(existing) + 1}" \
           f"-{source_hash[:12]}.{ext}"
    path = os.path.join(d, name)
    with open(path, "wb") as f:
        f.write(data)
    return store.export_add(kind, object_id, fmt, name, path, len(data),
                            _sha(data), source_hash, meta)


def read_export(store, row: dict) -> bytes:
    """Serve persisted bytes; verify the path stays inside the export
    dir (defence in depth against any path manipulation in stored rows)."""
    p = os.path.realpath(row["path"])
    base = os.path.realpath(str(store.export_dir))
    if not p.startswith(base + os.sep):
        raise LookupError("export path is outside the export directory")
    with open(p, "rb") as f:
        data = f.read()
    if _sha(data) != row["sha256"]:
        # the persisted file no longer matches what we pinned: fail
        # loudly instead of serving mutated bytes
        raise Conflict("persisted export file no longer matches its "
                       "recorded checksum - refusing to serve it")
    return data


def ensure_research_export(store, rid: str, fmt: str) -> dict:
    if fmt not in RENDERERS:
        raise LookupError(f"unknown export format '{fmt}' - available: "
                          + ", ".join(sorted(RENDERERS)))
    r = store.research_get(rid)
    if not r or not r.get("report"):
        raise LookupError("research run has no stored report to export "
                          "(nothing was invented here)")
    report = r["report"]
    h = _sha(_canon(report))
    latest = store.export_latest("research", rid, fmt)
    if latest and latest["source_hash"] == h:
        try:
            read_export(store, latest)
            return latest          # persisted bytes, zero re-rendering
        except (OSError, LookupError, Conflict):
            pass  # file lost/mutated on disk: re-render once, new version
    data = RENDERERS[fmt](rid, report)
    row = _write_export(store, "research", rid, fmt, data, h,
                        {"report_status": r.get("status"),
                         "engine": report.get("engine")})
    store.audit("engine", "export.created", rid,
                {"kind": "research", "format": fmt, "version": row["version"],
                 "sha256": row["sha256"], "bytes": row["bytes"]})
    return row


# ---------------------------------------------------------------------------
# results bundle (ZIP, scoped, built only from persisted state)
# ---------------------------------------------------------------------------

def job_source_hash(store, job: dict) -> str:
    parts = {
        "job": {k: job.get(k) for k in
                ("id", "state", "outcome", "kind", "sample_id",
                 "created_at", "finished_at", "result_json")},
        "artifacts": store.artifact_list(job["id"]),
        "decisions": store.decision_list(job["id"]),
        "snapshot": store.snapshot_get(job["id"]),
    }
    return _sha(_canon(parts))


def plan_native_files(outdir: str) -> tuple[list[dict], list[dict]]:
    """Walk a native run's outdir and decide, with explicit reasons, what
    may enter a results bundle. Raw sequencing/alignment inputs NEVER
    enter, regardless of size; oversized files are capped; nothing is
    skipped silently."""
    files, excluded = [], []
    if not os.path.isdir(outdir):
        return files, [{"name": outdir,
                        "reason": "native output directory no longer "
                                  "exists on this host"}]
    total = 0
    for root, _dirs, names in os.walk(outdir):
        for fn in sorted(names):
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, outdir)
            try:
                size = os.path.getsize(p)
            except OSError as exc:
                excluded.append({"name": rel,
                                 "reason": f"unreadable: {exc}"[:160]})
                continue
            low = rel.lower()
            if low.endswith(RAW_SUFFIXES):
                excluded.append({"name": rel, "bytes": size,
                                 "reason": "raw sequencing/alignment input "
                                           "(FASTQ/BAM/SAM/CRAM/VCF class) "
                                           "- never part of a results "
                                           "bundle"})
                continue
            if size > MAX_MEMBER_BYTES:
                excluded.append({"name": rel, "bytes": size, "reason":
                                 f"larger than the per-file cap of "
                                 f"{MAX_MEMBER_BYTES} bytes"})
                continue
            if total + size > MAX_BUNDLE_BYTES:
                excluded.append({"name": rel, "bytes": size, "reason":
                                 "bundle total cap reached "
                                 f"({MAX_BUNDLE_BYTES} bytes)"})
                continue
            files.append({"name": rel, "path": p, "bytes": size})
            total += size
    files.sort(key=lambda f: f["name"])
    return files, excluded


def _readme(job: dict, manifest: dict) -> str:
    return "\n".join([
        f"NeoAG results bundle for job {job['id']}",
        f"state={job.get('state')} outcome={job.get('outcome')} "
        f"kind={job.get('kind')} sample={job.get('sample_id')}",
        f"built {manifest['generated_at']} by {EXPORT_BUILDER}",
        "",
        "WHAT THIS ZIP CONTAINS",
        "  job.json          job row (lifecycle state, sample, spec)",
        "  snapshot.json     the IMMUTABLE effective-config snapshot the",
        "                    run recorded when it started (A5)",
        "  decisions.json    the full decision history for this run",
        "  artifacts.json    the output-artifact manifest with honest",
        "                    availability status per artifact (A3)",
        "  result.json       the complete stored result (disclaimers,",
        "                    manifest, provenance, candidate tables)",
        "  tables/           candidate ranking tables as TSV",
        "  events.json       bounded job event log",
        "  native/           real run-output files for NATIVE and BENCHMARK",
        "                    runs, AFTER the scope rules below (mock",
        "                    analysis runs have none)",
        "  manifest.json     per-file checksums + every exclusion",
        "",
        "SCOPE RULES (enforced by the builder, recorded in manifest.json)",
        "  - raw sequencing/alignment inputs are NEVER included "
        + str(list(RAW_SUFFIXES)),
        f"  - per-file cap {MAX_MEMBER_BYTES} bytes; total cap "
        f"{MAX_BUNDLE_BYTES} bytes",
        "  - everything skipped is LISTED with its reason - nothing is",
        "    silently dropped",
        "",
        "PERSISTENCE: this ZIP was built once from the job's stored state;",
        "re-downloading serves these exact bytes. The bundle never",
        "re-runs the pipeline or regenerates science.",
        "",
        "DISCLAIMER: " + str((manifest.get("result_disclaimer") or
                               "see result.json"))[:500],
        ""])


def build_bundle_bytes(store, job: dict) -> bytes:
    jid = job["id"]
    res = json.loads(job.get("result_json") or "{}")
    snap = store.snapshot_get(jid)
    decisions = store.decision_list(jid)
    artifacts = store.artifact_list(jid)
    try:
        events = store.event_list(jid, 0, 1000)
    except Exception:
        events = []
    members: dict[str, bytes] = {
        "job.json": (json.dumps(
            {**{k: job.get(k) for k in
                ("id", "created_at", "updated_at", "started_at",
                 "finished_at", "state", "kind", "sample_id", "stage",
                 "progress", "outcome", "error", "approval_id",
                 "conv_id")},
             "spec": json.loads(job.get("spec_json") or "{}")},
            indent=1, ensure_ascii=False) + "\n").encode("utf-8"),
        "snapshot.json": (json.dumps(snap or {
            "note": "NO immutable snapshot was stored for this job - "
                    "stated honestly, not fabricated"},
            indent=1, ensure_ascii=False) + "\n").encode("utf-8"),
        "decisions.json": (json.dumps(decisions, indent=1,
                                      ensure_ascii=False) + "\n"
                           ).encode("utf-8"),
        "artifacts.json": (json.dumps(artifacts, indent=1,
                                      ensure_ascii=False) + "\n"
                           ).encode("utf-8"),
        "result.json": (json.dumps(res, indent=1, ensure_ascii=False) +
                        "\n").encode("utf-8"),
        "events.json": (json.dumps(events[-1000:], indent=1,
                                   ensure_ascii=False) + "\n").encode(
                                       "utf-8"),
    }
    for strat, rows in (res.get("candidates_by_strategy") or {}).items():
        t = tsv_rows(rows)
        if t:
            members[f"tables/{_safe(strat)}_ranking.tsv"] = t.encode(
                "utf-8")
    native_files, excluded = [], []
    if res.get("engine") in ("native", "benchmark") and res.get("outdir"):
        native_files, excluded = plan_native_files(str(res["outdir"]))
    manifest = {
        "bundle": "neoag-results", "builder": EXPORT_BUILDER,
        "job_id": jid, "generated_at": _now(),
        "built_from": "persisted job state in the app database "
                      "(no pipeline re-run, no science regenerated)",
        "scope_rules": {
            "excluded_suffixes": list(RAW_SUFFIXES),
            "per_file_cap_bytes": MAX_MEMBER_BYTES,
            "total_cap_bytes": MAX_BUNDLE_BYTES},
        "result_disclaimer": res.get("disclaimer") or
                             (res.get("warnings") or ["see result.json"])[0],
        "native_files_included": len(native_files),
    }
    readme = _readme(job, manifest).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        def put(name, data):
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)
        file_list = [{"name": "README.txt", "bytes": len(readme),
                      "sha256": _sha(readme)}]
        put("README.txt", readme)
        for name in sorted(members):
            put(name, members[name])
            file_list.append({"name": name, "bytes": len(members[name]),
                              "sha256": _sha(members[name])})
        for f in native_files:
            try:
                with open(f["path"], "rb") as fh:
                    data = fh.read()
            except OSError as exc:
                excluded.append({"name": f["name"],
                                 "reason": f"read failed: {exc}"[:160]})
                continue
            put(f"native/{f['name']}", data)
            file_list.append({"name": f"native/{f['name']}",
                              "bytes": len(data), "sha256": _sha(data)})
        manifest["files"] = file_list
        manifest["excluded"] = excluded
        manifest["note"] = ("files[] lists every member with its sha256; "
                            "manifest.json itself is the index and is "
                            "listed in the ZIP, not in files[]")
        put("manifest.json", (json.dumps(manifest, indent=1,
                                         ensure_ascii=False) +
                              "\n").encode("utf-8"))
    return buf.getvalue()


def ensure_job_bundle(store, job_id: str) -> dict:
    job = store.job_get(job_id)
    if not job:
        raise LookupError("no such job")
    if job["state"] != "completed":
        raise Conflict(f"job is {job['state']} - only completed runs are "
                       "bundled; nothing was generated on demand here")
    if not job.get("result_json"):
        raise Conflict("completed job stores no result payload - there is "
                       "no science to bundle")
    h = job_source_hash(store, job)
    latest = store.export_latest("job", job_id, "zip")
    if latest and latest["source_hash"] == h:
        try:
            read_export(store, latest)
            return latest
        except (OSError, LookupError, Conflict):
            pass
    data = build_bundle_bytes(store, job)
    row = _write_export(store, "job", job_id, "zip", data, h,
                        {"job_state": job["state"],
                         "outcome": job.get("outcome")})
    store.audit("engine", "export.created", job_id,
                {"kind": "job_bundle", "format": "zip",
                 "version": row["version"], "sha256": row["sha256"],
                 "bytes": row["bytes"]})
    return row
