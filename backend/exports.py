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
body{font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,
sans-serif;margin:0;background:#0f1419;color:#d7dee8}
main{max-width:1100px;margin:0 auto;padding:24px 28px 80px}
h1{font-size:22px;margin:.2em 0}h2{font-size:17px;margin:1.6em 0 .5em;
border-bottom:1px solid #2a3442;padding-bottom:4px}
h3{font-size:14px;margin:1.1em 0 .4em;color:#9fb3c8}
.disclaimer{background:#2b2010;border:1px solid #7a5b16;color:#f2d488;
padding:10px 12px;border-radius:6px;white-space:pre-wrap}
.hint{color:#8a97a8;font-size:12px}
.card{background:#161d26;border:1px solid #26313f;border-radius:8px;
padding:12px 14px;margin:10px 0}
.pill{display:inline-block;background:#1d3a5f;color:#8ec5ff;border-radius:
10px;padding:1px 9px;font-size:12px;margin-right:6px}
.warn{background:#3a2416;border:1px solid #7a4a1a;border-radius:6px;
padding:8px 10px;color:#f0b483}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin:.4em 0}
th,td{border:1px solid #2a3442;padding:4px 7px;text-align:left;
vertical-align:top}th{background:#1a2330;color:#9fb3c8}
ul{margin:.3em 0 .6em 1.2em;padding:0}li{margin:.25em 0}
pre{background:#10161d;border:1px solid #26313f;border-radius:6px;
padding:10px;overflow:auto;font-size:12px;white-space:pre-wrap}
details{margin:.6em 0}summary{cursor:pointer;color:#8ec5ff}
.mnote{color:#9fb3c8;font-size:12.5px}
"""


def _h2(t) -> str:
    return f"<h2>{_e(t)}</h2>"


def _list(items, tag="li") -> str:
    return "<ul>" + "".join(f"<{tag}>{_e(i)}</{tag}>"
                            for i in (items or [])) + "</ul>"


def _five_part(ev: dict) -> str:
    labels = [
        ("what_evidence_supports_it", "What evidence supports it"),
        ("expected_advantage", "Expected advantage"),
        ("directly_comparable", "Directly comparable?"),
        ("directly_comparable_note", "Comparability note"),
        ("usable_in_current_deployment", "Usable in the current deployment"),
        ("remains_to_verify", "Remains to verify"),
    ]
    lis = []
    for k, lab in labels:
        if k in ev:
            lis.append(f"<li><b>{_e(lab)}:</b> {_cell(ev[k])}</li>")
    return f"<ul class='five'>{''.join(lis)}</ul>" if lis else ""


def research_html(rid: str, rep: dict) -> bytes:
    """Standalone single-file HTML of a stored research report (either
    engine). Every section renders only if the stored report carries it -
    the export can never claim more than what was retrieved."""
    consumed = set()

    def use(*keys):
        consumed.update(keys)

    parts: list[str] = []
    eng = str(rep.get("engine") or "?")
    parts.append(
        f"<h1>NeoAG tool-research report</h1><p><span class=pill>"
        f"engine: {_e(eng)} {_e(rep.get('engine_version') or '')}</span>"
        f"<span class=pill>report {_e(rid)}</span>"
        f"<span class=pill>status: {_e(rep.get('status') or 'completed')}"
        f"</span> <span class=hint>exported {_e(_now())} - this file is "
        "standalone and immutable: the server serves these exact bytes on "
        "every re-download</span></p>")
    if rep.get("disclaimer"):
        parts.append(f'<div class=disclaimer>{_e(rep["disclaimer"])}</div>')
        use("disclaimer")
    use("engine", "engine_version", "status")
    if rep.get("executive_summary"):
        parts.append(_h2("Executive recommendations"))
        parts.append(_list(rep["executive_summary"]))
        use("executive_summary")
    mc = rep.get("module_conclusions") or {}
    if mc:
        parts.append(_h2("Per-module conclusions (decision first)"))
        for tid, c in mc.items():
            ex = c.get("explanation") or {}
            parts.append(
                "<div class=card><h3>" + _e(c.get("target") or tid) +
                f" <span class=hint>[{_e(tid)}]</span></h3>"
                f"<p><span class=pill>{_e(c.get('conclusion'))}</span>"
                f"<span class=mnote>{_e(c.get('why'))}</span></p>"
                + _five_part(ex)
                + ("<p class=mnote><b>Recommended evaluation:</b> "
                   + _e(c.get("recommended_evaluation")) + "</p>"
                   if c.get("recommended_evaluation") else "")
                + "</div>")
        use("module_conclusions", "conclusion_vocabulary")
    if rep.get("recommendation"):
        rec = rep["recommendation"]
        parts.append(_h2("Recommendation"))
        parts.append(
            "<div class=card>"
            + (f"<p>{_e(rec['verdict'])}</p>" if isinstance(rec, dict)
               and rec.get("verdict") else _cell(rec))
            + (_list(rec.get("experiments_before_adoption"))
               if isinstance(rec, dict) else "")
            + "</div>")
        use("recommendation")
    tcs = rep.get("task_comparisons") or []
    if tcs:
        parts.append(_h2("Evidence-based comparison by scientific task"))
        for tc in tcs:
            cur = tc.get("current_tools") or []
            cur_line = "; ".join(
                f"{c.get('name')} [{c.get('version')}] installed: "
                f"{c.get('installed')}" for c in cur)
            groups = tc.get("alternatives_by_kind") or {}
            gline = "; ".join(f"{k}: {', '.join(v)}"
                              for k, v in groups.items()) or "none"
            parts.append(
                f"<div class=card><h3>{_e(tc.get('target'))}</h3>"
                f"<p class=mnote><b>Current NeoAG tool(s):</b> "
                f"{_e(cur_line)}</p>"
                f"<p class=mnote><b>Alternatives by kind:</b> {_e(gline)}"
                "</p>"
                + _html_table(tc.get("rows"))
                + f"<p class=hint>{_e(tc.get('note'))}</p></div>")
        use("task_comparisons")
    vlf = rep.get("version_license_findings") or []
    if vlf:
        parts.append(_h2("Version, license and compatibility findings"))
        parts.append(_html_table(vlf))
        use("version_license_findings")
    na = rep.get("recommended_next_actions") or []
    if na:
        parts.append(_h2("Recommended next actions"))
        parts.append(_list(na))
        use("recommended_next_actions")
    rb = rep.get("recommended_benchmarks") or []
    if rb:
        parts.append(_h2("Recommended benchmarks"))
        parts.append(_html_table(rb))
        use("recommended_benchmarks")
    comp = rep.get("comparison") or []
    if comp:
        parts.append(_h2("Candidate comparison (full retrieved table)"))
        parts.append(_html_table(comp))
        use("comparison")
    cands = rep.get("candidates") or []
    chains = [c for c in cands if c.get("version_chain")]
    if chains:
        parts.append(_h2("Version verification chains"))
        parts.append(
            '<p class=hint>every step records WHERE it looked, WHAT it '
            'found and WHEN; "Not verified" only after every step ran and '
            'produced nothing</p>')
        for c in chains:
            ch = c["version_chain"]["chain"]
            parts.append(
                "<details><summary>" + _e(c.get("name")) + " — "
                + _e(c["version_chain"].get("display")) + "</summary>"
                + _html_table(ch)
                + ("<p class=mnote><b>benchmark-version mentions:</b> "
                   + _cell(c["version_chain"].get("benchmark_versions"))
                   + "</p>" if c["version_chain"].get(
                       "benchmark_versions") else "")
                + "</details>")
        use("candidates")
    cits = rep.get("literature_citations") or []
    if cits:
        parts.append(_h2("Sources and citations"))
        parts.append(_html_table(cits))
        use("literature_citations")
    cov = rep.get("coverage_accounting")
    if cov:
        parts.append(_h2("Coverage accounting"))
        parts.append(
            "<p>" + " / ".join(
                f"<b>{_e(k)}</b>: {_cell(cov.get(k))}"
                for k in ("completeness", "identified_modules",
                          "researched_modules", "sufficient_evidence",
                          "insufficient_evidence")) + "</p>"
            + _html_table(cov.get("not_researched"))
            + f"<p class=hint>{_e(cov.get('note'))}</p>")
        use("coverage_accounting")
    inv = rep.get("inventory")
    if inv and inv.get("stages"):
        parts.append(_h2("Code-grounded pipeline inventory"))
        parts.append(
            f"<p class=mnote>repo {_e(inv.get('repo'))} @ commit "
            f"{_e(inv.get('git_commit'))}, captured "
            f"{_e(inv.get('captured_at'))}</p>")
        st = [{"id": s.get("id"), "label": s.get("label"),
               "current_tools": s.get("current_tools"),
               "provenance": s.get("provenance")}
              for s in inv["stages"]]
        parts.append(_html_table(st))
        use("inventory")
    if rep.get("methodology"):
        parts.append(_h2("Methodology"))
        parts.append(f"<p>{_e(rep['methodology'])}</p>")
        use("methodology")
    lim = rep.get("limitations") or []
    if lim:
        parts.append(_h2("Limitations and incomplete evidence"))
        parts.append(_list(lim))
        use("limitations")
    if rep.get("availability_classes_legend"):
        parts.append(_h2("Availability classes legend"))
        legend = rep["availability_classes_legend"]
        if isinstance(legend, list):
            parts.append(_html_table(legend))
        elif isinstance(legend, dict):
            parts.append(_html_table([{"class": k, "meaning": v}
                                      for k, v in legend.items()]))
        else:
            parts.append(f"<p>{_e(legend)}</p>")
        use("availability_classes_legend")
    lic = rep.get("license_constraints") or []
    if lic:
        parts.append(_h2("Licenses seen"))
        parts.append(_list(lic))
        use("license_constraints")
    if rep.get("provenance"):
        parts.append(_h2("Provenance"))
        parts.append("<pre>" + _e(json.dumps(
            rep["provenance"], indent=1, ensure_ascii=False)) + "</pre>")
        use("provenance")
    if rep.get("timestamps"):
        parts.append(_h2("Timestamps"))
        parts.append(_html_table([rep["timestamps"]]))
        use("timestamps")
    use("targets", "scope", "target")
    # honesty fallback: anything not rendered above is still included,
    # verbatim, so the export never silently drops stored content
    rest = {k: v for k, v in rep.items() if k not in consumed}
    if rest:
        parts.append(_h2("Other stored report content"))
        parts.append("<pre>" + _e(json.dumps(rest, indent=1,
                                             ensure_ascii=False)) + "</pre>")
    body = "".join(parts)
    doc = ("<!doctype html><html><head><meta charset=utf-8>"
           "<meta name=viewport content='width=device-width,"
           "initial-scale=1'>"
           f"<title>NeoAG research report {_e(rid)}</title>"
           f"<style>{_CSS}</style></head><body><main>{body}"
           "<p class=hint>Generated by the NeoAG web demo export builder "
           f"({_e(EXPORT_BUILDER)}). Report id {_e(rid)}. This file is "
           "self-contained - no network access is needed to read it."
           "</p></main></body></html>")
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

    mc = rep.get("module_conclusions") or {}
    if mc:
        rows = []
        for tid, c in mc.items():
            ex = c.get("explanation") or {}
            rows.append({"target_id": tid, "target": c.get("target"),
                         "conclusion": c.get("conclusion"),
                         "why": c.get("why"),
                         "evidence_basis": c.get("evidence_basis"),
                         "directly_comparable": ex.get(
                             "directly_comparable"),
                         "usable_in_current_deployment": ex.get(
                             "usable_in_current_deployment"),
                         "recommended_evaluation": c.get(
                             "recommended_evaluation")})
        section("module_conclusions", rows)
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
               "targets", "disclaimer", "module_conclusions",
               "task_comparisons", "version_license_findings", "comparison",
               "candidates", "literature_citations", "coverage_accounting",
               "recommended_benchmarks", "recommended_next_actions",
               "executive_summary", "limitations"}
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
    """Narrative flattening of the report for the PDF - decisions first,
    always pointing to the JSON/HTML export for the complete detail."""
    p: list[tuple[str, str]] = [
        ("h", "NeoAG tool-research report"),
        ("p", f"report {rid} - engine {rep.get('engine')} "
              f"{rep.get('engine_version') or ''} - exported {_now()}"),
        ("p", "PDF note: machine-generated text-only rendering (stdlib "
              "PDF writer, Latin-1 text, no tables/charts). The HTML and "
              "JSON exports of this same report carry the complete, "
              "tabular detail; science content was not re-generated for "
              "this file."),
    ]
    if rep.get("disclaimer"):
        p.append(("b", str(rep["disclaimer"])))
    for s in rep.get("executive_summary") or []:
        p.append(("p", s))
    for tid, c in (rep.get("module_conclusions") or {}).items():
        p.append(("h", f"{c.get('target') or tid}: {c.get('conclusion')}"))
        p.append(("p", f"Why: {c.get('why')}"))
        ex = c.get("explanation") or {}
        if ex.get("expected_advantage"):
            p.append(("p", f"Expected advantage: {ex['expected_advantage']}"))
        if ex.get("remains_to_verify"):
            p.append(("p", f"Remains to verify: {ex['remains_to_verify']}"))
        if c.get("recommended_evaluation"):
            p.append(("p", f"Recommended evaluation: "
                           f"{c['recommended_evaluation']}"))
    if rep.get("recommendation"):
        rec = rep["recommendation"]
        if isinstance(rec, dict) and rec.get("verdict"):
            p.append(("h", "Recommendation"))
            p.append(("p", str(rec["verdict"])))
            for e in rec.get("experiments_before_adoption") or []:
                p.append(("p", f"- {e}"))
    if rep.get("recommended_next_actions"):
        p.append(("h", "Recommended next actions"))
        for a in rep["recommended_next_actions"]:
            p.append(("p", f"- {a}"))
    cov = rep.get("coverage_accounting") or {}
    if cov:
        p.append(("h", "Coverage accounting"))
        p.append(("p", f"completeness {cov.get('completeness')}; "
                       f"identified {cov.get('identified_modules')}; "
                       f"researched {cov.get('researched_modules')}; "
                       f"sufficient {len(cov.get('sufficient_evidence') or [])}; "
                       f"insufficient {len(cov.get('insufficient_evidence') or [])}; "
                       f"not researched {len(cov.get('not_researched') or [])}"))
    if rep.get("limitations"):
        p.append(("h", "Limitations"))
        for l in rep["limitations"]:
            p.append(("p", f"- {l}"))
    if rep.get("methodology"):
        p.append(("h", "Methodology"))
        p.append(("p", str(rep["methodology"])))
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
