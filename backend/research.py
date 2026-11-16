"""SOTA discovery engine (PART D).

Two engines, chosen EXPLICITLY at submission (D1):
 - 'live'     : real multi-source retrieval (literature + software
   registries via backend/sources.py) over the code-grounded inventory
   of the configured NeoAG repo (backend/inventory.py). All numbers in
   the report come from actual retrieved content; anything not
   verifiable is written as 'Not verified'. Unreachable sources are
   recorded as failures and reflected in coverage - the report is then
   'incomplete', never silently padded.
 - 'example'  : the hand-written EXAMPLE digest kept ONLY as an
   explicitly selected testing option (labels every row as MOCK
   content). Real Discovery requests never route here by default.

Live runs are durable background jobs: progress persists per query
(resumable), cancellation is cooperative, and the provenance section
lists every query, count, dedup, exclusion (+reason) and failure.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time

from . import inventory, mock_data, sources
from .store import Store, new_id, now
from .mock_data import DEMO_DATASET_VERSION

LIVE_ENGINE_VERSION = "live-retrieval-2026.11-r1"

AVAILABILITY = ("open-source | freely available with restrictions | "
                "license-required | commercial | unavailable | unclear")

# ---------------------------------------------------------------------------
# D6: task-specific metric guidance. Metrics are listed ONLY for the
# named task; nothing here is a result, and incomparable metrics are
# explicitly flagged.
# ---------------------------------------------------------------------------
TASK_METRICS = {
    "presentation": {
        "metrics": ["AUROC for EL/binding classification (comparable only "
                    "within identical label sets + allele/peptide "
                    "distributions)",
                    "AUPRC when negatives dominate",
                    "ligand recall at fixed false-positive rate"],
        "datasets_note": "use the benchmark's own task definition (hold-out "
                         "scheme, allele coverage, decoy construction)",
        "warning": "AUC values computed on different datasets/label "
                   "definitions are NOT rankable against each other"},
    "stability": {
        "metrics": ["rank correlation (Spearman) with measured half-life",
                    "classification AUC for a fixed stability threshold"],
        "datasets_note": "measured half-life datasets are small; state the "
                         "assay",
        "warning": "stability ranks from different pan-specificity models "
                   "are not directly comparable"},
    "fusion_discovery": {
        "metrics": ["breakpoint-level precision/recall/F1 against a curated "
                    "truth set", "gene-pair-level P/R"],
        "datasets_note": "call-level truth sets (e.g. benchmark cohorts) "
                         "define what counts as a true breakpoint",
        "warning": "F1 values from different truth sets are not comparable"},
    "hla_typing": {
        "metrics": ["allele-level exact match at 2/4/6/8-field resolution",
                    "novel-allele detection rate"],
        "datasets_note": "results depend strongly on read length and "
                         "sequencing type (RNA vs DNA)",
        "warning": "accuracy at 2-field and 8-field resolution are "
                   "different metrics - never mix in one ranking"},
    "rna_expression": {
        "metrics": ["gene-level Spearman/Pearson vs orthogonal qPCR",
                    "transcript-level concordance"],
        "datasets_note": "quantification method (salmon/rsem) and annotation "
                         "version change the metric",
        "warning": "TPM from different pipelines is comparable only with "
                   "identical annotation and normalisation"},
}
_DEFAULT_METRICS = {
    "metrics": ["Not verified - define the benchmark task, labels and "
                "metric before any ranking is meaningful"],
    "datasets_note": "no dataset is proposed for this custom target",
    "warning": "custom target: metrics must be chosen from published "
               "benchmark definitions, not invented"}

_METRIC_RE = re.compile(
    r"(AUC|AUROC|AUPRC|F1[- ]?score|F1|precision|recall|sensitivity|"
    r"specificity|Spearman|accuracy|PPV|NPV)[^.]{0,40}?"
    r"(0?\.\d{2,3}|\d{1,3}(?:\.\d+)?\s?%)", re.I)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (text or "").lower()).strip("-")[:40]


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())[:80]


# ---------------------------------------------------------------------------
# submission (single pathway for UI + agent - E3)
# ---------------------------------------------------------------------------

def submit_research(store, scope, targets=None, custom=None, conv_id="",
                    actor="user", engine="live"):
    """ONE canonical submission pathway used by POST /api/research and by
    the agent, so a chat request and a UI request are the SAME workflow."""
    scope = scope if scope in ("full", "targeted") else "targeted"
    engine = engine if engine in ("live", "example") else "live"
    if engine == "example":
        # the example engine is a fixed test fixture with its own small
        # target set; live research is scoped against the real inventory
        known = {k: {"id": k, "label": v.get("stage") or k}
                 for k, v in TARGETS.items()}
    else:
        known = {t["id"]: t for t in inventory.targets_for_ui()}
    resolved, unknown = [], []
    for t in targets or []:
        if t in known:
            resolved.append({"id": t, "label": known[t]["label"]})
        else:
            unknown.append(t)
    for c in custom or []:
        c = str(c).strip()[:80]
        if c:
            resolved.append({"id": "custom:" + _slug(c), "label": c,
                             "custom": True})
    if scope == "full":
        resolved = [{"id": s, "label": known[s]["label"]}
                    for s in known]
    elif not resolved:
        raise ValueError("targeted scope requires at least one known or "
                         "custom target")
    rid = new_id("res")
    store.research_add(rid, scope,
                       ",".join(r["id"] for r in resolved)[:200],
                       engine=engine, conv_id=conv_id,
                       targets=[r["id"] for r in resolved])
    with store.lock:
        store.db.execute(
            "UPDATE research SET progress_json=? WHERE id=?",
            (json.dumps({"targets": resolved, "steps": [], "done_queries": [],
                         "queries": [], "note": "queued"},
                        ensure_ascii=False), rid))
        store.db.commit()
    store.audit(actor, "research.submit", rid,
                {"scope": scope, "engine": engine,
                 "targets": [r["id"] for r in resolved]})
    t = threading.Thread(target=run_research,
                         args=(rid, scope, [r["id"] for r in resolved],
                               store, None), daemon=True)
    t.start()
    return {"research_id": rid, "status_url": f"/api/research/{rid}",
            "engine": engine, "unknown_targets": unknown}


def resume_research(store, rid: str) -> bool:
    """Durable continuation: re-launch a run that was interrupted,
    cancelled or ended incomplete; already-persisted queries are skipped."""
    r = store.research_get(rid)
    if not r or r["status"] not in ("incomplete", "failed", "cancelled",
                                    "running", "queued"):
        return False
    if r["status"] in ("running",):
        return False  # live thread still owns it
    targets = (r.get("progress") or {}).get("targets") or []
    scope = r["scope"]
    with store.lock:
        store.db.execute(
            "UPDATE research SET status='queued', cancel_requested=0 "
            "WHERE id=?", (rid,))
        store.db.commit()
    store.audit("user", "research.resume", rid, {})
    threading.Thread(target=run_research,
                     args=(rid, scope, [t["id"] for t in targets], store,
                           True), daemon=True).start()
    return True


def sweep_interrupted_research(store) -> int:
    """Startup honesty: research rows still 'running'/'queued' lost their
    thread with the previous process -> mark incomplete + resumable."""
    n = 0
    with store.lock:
        rows = store.db.execute(
            "SELECT id FROM research WHERE status IN "
            "('running','queued')").fetchall()
        for r in rows:
            store.db.execute(
                "UPDATE research SET status='incomplete' WHERE id=?",
                (r["id"],))
            n += 1
        store.db.commit()
    for r in rows:
        store.research_event_add(r["id"],
                                 "interrupted by server restart - "
                                 "resume available (persisted queries are "
                                 "kept)")
    return n


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def run_research(rid, scope, targets, store: Store, resume=None):
    """resume=None -> fresh; True -> continue persisted progress;
    (kept signature-compatible with the legacy call sites)."""
    r = store.research_get(rid)
    engine = (r or {}).get("engine") or "example"
    if engine == "live":
        run_live(rid, scope, targets, store, bool(resume))
    else:
        run_example(rid, scope, targets, store)


def _step(store, publish, rid, text, prog, prog_extra=None):
    prog["steps"] = prog.get("steps", []) + [text]
    if prog_extra:
        prog.update(prog_extra)
    store.research_set_progress(rid, prog)
    store.research_event_add(rid, text)
    if publish:
        publish({"type": "research", "id": rid})


# ---------------------------------------------------------------------------
# LIVE engine
# ---------------------------------------------------------------------------

def _literature_plan(target, queries_done):
    tid, label = target["id"], target["label"]
    if tid.startswith("custom:"):
        base = label
    else:
        base = {"presentation": "MHC-I neoantigen peptide-HLA presentation "
                                "prediction",
                "hla_typing": "HLA typing from sequencing data",
                "purity_cnv": "tumor purity copy number estimation",
                "hla_loh": "HLA loss of heterozygosity detection",
                "rna_expression": "RNA-seq transcript quantification "
                                  "tumour expression",
                "snv": "somatic SNV calling neoantigen peptide generation",
                "fusion_discovery": "RNA-seq fusion gene detection",
                "fusion_peptides": "fusion neoantigen breakpoint peptides",
                "splice_discovery": "aberrant alternative splicing detection "
                                    "tumour",
                "splice_peptides": "splice junction neoantigen peptides",
                "safety_escape": "neoantigen safety normal proteome "
                                 "autoantigen avoidance",
                "ranking": "neoantigen ranking prioritisation "
                           "immunogenicity",
                "reports": "neoantigen report clinical presentation",
                }.get(tid, tid.replace("_", " "))
    plan = [
        {"source": "europepmc",
         "query": f"{base} AND (benchmark OR comparison)",
         "purpose": "published benchmarks/comparisons for this task"},
        {"source": "pubmed",
         "query": f"{base} prediction tools",
         "purpose": "method papers naming candidate tools"},
        {"source": "openalex",
         "query": f"{base} tool evaluation",
         "purpose": "broader scholarly evaluation literature"},
    ]
    out = [q for q in plan
           if f"{q['source']}|{q['query']}" not in queries_done]
    return out


# Generic scholarly-title words that regex-extraction from paper titles
# otherwise mistakes for tool names ("Evaluation", "Personalized", ...).
# Heuristic and intentionally incomplete - the shape rule below is the
# primary signal, this list catches the frequent Title-Case offenders.
_TITLE_STOPWORDS = {w.lower() for w in (
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
)}


def _tool_names_for(target, lit_records, inv_stage):
    """Candidate tools from (a) the configured repo's own stage
    definition when it names tools, (b) tool/repository hits actually
    retrieved from GitHub for the task, (c) tool-shaped proper names in
    retrieved titles. Nothing is injected from memory.

    (c) is filtered deliberately: a word from a paper title is treated
    as a tool ONLY if it looks tool-shaped (lower->U internal capitals
    or a digit, e.g. NetMHCpan / MHCflurry / 4.1) and is not a common
    scholarly word from _TITLE_STOPWORDS. Recurrence across several
    titles is NOT accepted as a name signal - generic words like
    "Evaluation" or "Using" recur constantly - and without this filter
    they became "registry-verified" candidates whose same-named repos
    misattributed licenses. Pure Title-Case tool names that only
    appear in prose can still enter via (a) the configured workflow or
    (b) GitHub task search; title-only misses are a documented limit."""
    names = []
    if inv_stage:
        for t in inv_stage.get("current_tools") or []:
            names.append({"name": t, "origin": "configured NeoAG workflow "
                          "TOML (current tool)"})
    src_of, seen = {}, {}
    for r in lit_records:
        for m in set(re.findall(r"\b([A-Z][A-Za-z0-9]{3,}(?:-?\d+[\.\dx]*)?"
                                r")\b", r.get("title") or "")):
            seen[m] = True
            src_of.setdefault(m, r["source"])
    for m in seen:
        if m.lower() in _TITLE_STOPWORDS:
            continue
        if not re.search(r"[a-z][A-Z]|\d", m):
            continue
        if m not in [n["name"] for n in names]:
            names.append({"name": m, "origin": f"named in retrieved "
                          f"title ({src_of[m]})"})
    return names[:8]


def run_live(rid, scope, targets, store, resume):
    from .server import publish
    prog = store.research_progress(rid) or {}
    prog.pop("cancel_requested", None)
    prog.pop("status", None)
    done = set(prog.get("done_queries") or [])
    queries_log = prog.get("queries") or []
    resolved_targets = prog.get("targets") or [
        {"id": t, "label": t} for t in targets]
    with store.lock:
        store.db.execute("UPDATE research SET status='running' WHERE id=?",
                         (rid,))
        store.db.commit()
    _step(store, publish, rid, f"live research started "
          f"({LIVE_ENGINE_VERSION}); engine performs REAL retrieval", prog)

    def cancelled():
        return bool(store.research_progress(rid).get("cancel_requested"))

    inv = inventory.load_inventory()
    stages_by_id = {s["id"]: s for s in inv["stages"]}
    _step(store, publish, rid,
          f"code-grounded inventory: {len(inv['stages'])} stages from "
          f"{inv['repo']} (code_verified={inv['code_verified']}, "
          f"commit {str(inv['git_commit'])[:10]})", prog,
          {"inventory": inv})
    if cancelled():
        store.research_set(rid, "cancelled",
                           _live_report(rid, prog, inv, [], "cancelled"),
                           finished=True)
        publish({"type": "research", "id": rid})
        return

    lit_by_target = {}
    per_source = {}
    failures = []
    # ---- phase 1: literature retrieval per target ----
    for target in resolved_targets:
        if cancelled():
            break
        plan = _literature_plan(target, done)
        lit_by_target.setdefault(target["id"], [])
        for q in plan:
            adapter = sources.ALL_ADAPTERS.get(q["source"])
            recs, err, meta = ([], "unknown source", {})
            if adapter:
                try:
                    recs, err, meta = adapter(q["query"], 8)
                except Exception as e:
                    recs, err = [], f"{type(e).__name__}: {str(e)[:150]}"
            entry = {"source": q["source"], "query": q["query"],
                     "target": target["id"], "purpose": q["purpose"],
                     "count": len(recs), "error": err, "ts": now()}
            queries_log.append(entry)
            per_source.setdefault(q["source"], {"ok": 0, "fail": 0})
            if err:
                per_source[q["source"]]["fail"] += 1
                failures.append({"source": q["source"],
                                 "query": q["query"], "error": err})
            else:
                per_source[q["source"]]["ok"] += 1
                done.add(f"{q['source']}|{q['query']}")
                lit_by_target[target["id"]].extend(recs)
            _step(store, publish, rid,
                  f"[{target['id']}] {q['source']}: '{q['query'][:60]}' -> "
                  + (f"{len(recs)} records" if not err else f"FAILED {err}"),
                  prog, {"done_queries": sorted(done),
                         "queries": queries_log,
                         "coverage": {"per_source": per_source,
                                      "failures": failures}})
    if cancelled():
        store.research_set(rid, "cancelled",
                           _live_report(rid, prog, inv, lit_by_target,
                                        "cancelled"), finished=True)
        publish({"type": "research", "id": rid})
        return

    # ---- phase 2: dedup + candidate tools ----
    seen_titles, dedup_removed = set(), 0
    citations, no_id_excluded = [], 0
    for tid, recs in lit_by_target.items():
        for rec in recs:
            key = _norm_title(rec["title"])
            if key and key in seen_titles:
                dedup_removed += 1
                continue
            seen_titles.add(key)
            if not (rec.get("id") or rec.get("url")):
                no_id_excluded += 1
                continue
            citations.append({**rec, "targets": [tid]})
    _step(store, publish, rid,
          f"dedup: {dedup_removed} duplicate records removed; "
          f"{len(citations)} unique citations kept "
          f"({no_id_excluded} without resolvable id excluded)", prog)

    tool_hits = []
    candidates = []
    for target in resolved_targets:
        if cancelled():
            break
        tid = target["id"]
        inv_stage = stages_by_id.get(tid)
        names = _tool_names_for(target, lit_by_target.get(tid, []), inv_stage)
        # retrieve software hits for the TASK (grounded in the query, not
        # in a remembered tool list)
        gh_query = target["label"].split("(")[0].strip()
        for q in ([{"source": "github", "query": gh_query,
                    "purpose": "task-named repositories"}]
                  if f"github|{gh_query}" not in done else []):
            recs, err, _ = sources.github_repo_search(q["query"], 4)
            entry = {"source": "github", "query": q["query"], "target": tid,
                     "purpose": q["purpose"], "count": len(recs),
                     "error": err, "ts": now()}
            queries_log.append(entry)
            per_source.setdefault("github", {"ok": 0, "fail": 0})
            if err:
                per_source["github"]["fail"] += 1
                failures.append(entry)
            else:
                per_source["github"]["ok"] += 1
                done.add(f"github|{gh_query}")
                tool_hits.extend(recs)
            _step(store, publish, rid,
                  f"[{tid}] github repos for task: -> "
                  + (f"{len(recs)}" if not err else f"FAILED {err}"),
                  prog, {"queries": queries_log,
                         "coverage": {"per_source": per_source,
                                      "failures": failures}})
        names += [{"name": h["id"], "origin": f"GitHub task search hit "
                   f"({h['url']})"} for h in tool_hits[-4:]]
        # de-dup candidate names
        uniq, seenn = [], set()
        for n in names:
            k = n["name"].lower()
            if k not in seenn:
                seenn.add(k)
                uniq.append(n)
        for n in uniq[:6]:
            c = _verify_candidate(n, target, citations, per_source,
                                  failures, queries_log, done, store,
                                  publish, rid)
            candidates.append(c)
    if cancelled():
        store.research_set(rid, "cancelled",
                           _live_report(rid, prog, inv, lit_by_target,
                                        "cancelled"), finished=True)
        publish({"type": "research", "id": rid})
        return

    status = _live_status(per_source)
    report = _live_report(rid, prog, inv, lit_by_target, status,
                          candidates=candidates, citations=citations,
                          resolved_targets=resolved_targets,
                          dedup_removed=dedup_removed,
                          no_id_excluded=no_id_excluded,
                          per_source=per_source, failures=failures)
    store.research_set(rid, status, report, finished=True)
    store.audit("engine", "research.completed", rid,
                {"engine": "live", "status": status,
                 "candidates": len(candidates),
                 "citations": len(citations)})
    _step(store, publish, rid,
          f"report compiled: {len(candidates)} verified candidate rows, "
          f"{len(citations)} citations, status={status}", prog)
    publish({"type": "research", "id": rid})


def _verify_candidate(n, target, citations, per_source, failures,
                      queries_log, done, store, publish, rid):
    """Standardized candidate schema (D5). Unverifiable fields say
    'Not verified' - they are never filled with plausible estimates."""
    tid = target["id"]
    gh, py, bt = [], [], []
    # Probe registry name: for GitHub-derived "owner/repo" candidates use
    # the repo part; otherwise the first word of the candidate name.
    base = (n["name"].split("/")[-1].split() or [""])[0]
    slug = base.lower()
    q_gh, e_gh, _ = sources.github_repo_search(base, 2)
    _record(per_source, failures, queries_log, "github",
            f"verify:{base}", len(q_gh), e_gh, tid)
    q_py, e_py, _ = sources.pypi_package(slug, 1)
    _record(per_source, failures, queries_log, "pypi",
            f"verify:{slug}", len(q_py), e_py, tid,
            skip_ok_error="not-on-pypi")
    q_bt, e_bt, _ = sources.biotools_search(base, 2)
    _record(per_source, failures, queries_log, "biotools",
            f"verify:{base}", len(q_bt), e_bt, tid)

    # A registry hit only counts as evidence about THIS candidate when
    # the record is actually named after it - never a topically near
    # repository. Queried counts stay in provenance exactly as returned.
    def _about(r):
        blob = (str(r.get("id") or "") + " " +
                str(r.get("title") or "")).lower()
        return bool(base) and base.lower() in blob
    gh = [r for r in q_gh if _about(r)]
    py = [r for r in q_py if (r.get("id") or "") == slug
          or base.lower() in (r.get("title") or "").lower()]
    bt = [r for r in q_bt if _about(r)]
    # benchmark evidence from ALREADY retrieved citations for this target
    ev = []
    name_l = n["name"].lower()
    for c in citations:
        blob = (c.get("title") or "") + " " + (c.get("abstract") or "")
        if name_l and name_l in blob.lower():
            ms = [m.group(0)[:120] for m in _METRIC_RE.finditer(
                c.get("abstract") or "")][:3]
            own = name_l in (c.get("title") or "").lower()
            comp = bool(re.search(r"benchmark|comparat|evaluation|assess",
                                  blob, re.I))
            ev.append({
                "citation": {"title": c["title"], "year": c.get("year"),
                             "url": c.get("url"), "id": c.get("id"),
                             "source": c.get("source")},
                "quoted_metrics": ms or ["no explicit metric in abstract"],
                "evidence_level": (
                    "author-reported (abstract)" if own and not comp else
                    "independent published - abstract-level" if comp and
                    not own else "contextual (abstract mention)"),
                "note": "extracted from retrieved abstract text; "
                        "full-text benchmark context NOT verified"})
    # None-coalescing through the `or` chain: a registry record that
    # exists but carries a null/blank field must print "Not verified",
    # never None and never an empty cell.
    verified_version = ((py[0].get("version") if py else None)
                        or (gh[0].get("latest_release") if gh else None)
                        or "Not verified")
    license_ = (gh[0].get("license") if gh and
                gh[0].get("license") not in (None, "NOASSERTION") else
                (py[0].get("license") if py else
                 (bt[0].get("license") if bt else "Not verified")))
    maintenance = "Not verified"
    if gh:
        pushed = str(gh[0].get("pushed_at") or "")[:10]
        if gh[0].get("archived"):
            maintenance = "archived"
        elif pushed and pushed >= "2025-05-01":
            maintenance = f"active (last push {pushed})"
        elif pushed:
            maintenance = f"low activity (last push {pushed})"
    gaps = [f for f, miss in (
        ("exact version", verified_version == "Not verified"),
        ("license", not license_ or license_ == "Not verified"),
        ("published benchmark metric", not ev),
        ("independent validation", not any(
            "independent" in e["evidence_level"] for e in ev)),
        ("input/output format", True),  # never verified by these sources
    ) if miss]
    return {
        "name": n["name"],
        "verified_version": verified_version,
        "task": target["label"],
        "role_in_neoag": n["origin"],
        "category": ("software (registry-verified)" if (gh or py or bt)
                     else "mentioned in literature only"),
        "io_formats": "Not verified",
        "dependencies": "Not verified",
        "availability": ("found on " + ", ".join(
            s for s, ok in (("GitHub", gh), ("PyPI", py),
                            ("bio.tools", bt)) if ok)
            if (gh or py or bt) else "Not verified"),
        "license": license_ or "Not verified",
        "latest_release": ((gh[0].get("latest_release") if gh else None)
                           or (py[0].get("version") if py else None)
                           or "Not verified"),
        "maintenance": maintenance,
        "primary_references": [e["citation"] for e in ev][:4] or
        "Not verified (no retrieved citation naming this tool)",
        "published_benchmark_evidence": ev or "Not verified",
        "independent_validation": bool(
            [e for e in ev if "independent" in e["evidence_level"]]),
        "compatibility": "Not verified (integration surface not assessed "
                         "by these retrievals)",
        "strengths": "Not verified",
        "limitations": ("only registry metadata + abstracts were retrieved"
                        ),
        "evidence_gaps": gaps or ["none of the standard fields missing"],
        "next_action": ("run it on the deployment's own validation fixture "
                        "and compare on identical inputs - literature "
                        "claims are not adoption evidence"),
        "verification_hits": {
            "github": [h.get("url") for h in gh][:2],
            "pypi": [h.get("url") for h in py][:1],
            "biotools": [h.get("url") for h in bt][:2]},
    }


def _record(per_source, failures, queries_log, source, query, count, err,
            tid, skip_ok_error=None):
    per_source.setdefault(source, {"ok": 0, "fail": 0})
    entry = {"source": source, "query": query, "target": tid,
             "purpose": "candidate verification", "count": count,
             "error": err, "ts": now()}
    queries_log.append(entry)
    if err and not (skip_ok_error and skip_ok_error in str(err)):
        per_source[source]["fail"] += 1
        failures.append(entry)
    elif not err:
        per_source[source]["ok"] += 1


def _live_status(per_source) -> str:
    ok = sum(v["ok"] for v in per_source.values())
    fail = sum(v["fail"] for v in per_source.values())
    if ok == 0:
        return "failed"
    if fail:
        return "incomplete"
    return "completed"


def _live_report(rid, prog, inv, lit_by_target, status, candidates=None,
                 citations=None, resolved_targets=None, dedup_removed=0,
                 no_id_excluded=0, per_source=None, failures=None):
    candidates = candidates or []
    citations = citations or []
    resolved_targets = resolved_targets or (prog.get("targets") or [])
    per_source = per_source or (prog.get("coverage") or {}).get(
        "per_source") or {}
    failures = failures if failures is not None else \
        (prog.get("coverage") or {}).get("failures") or []
    total_ok = sum(v["ok"] for v in per_source.values())
    total_fail = sum(v["fail"] for v in per_source.values())
    benchmarks = {}
    for t in resolved_targets:
        benchmarks[t["id"]] = TASK_METRICS.get(
            t["id"] if not t["id"].startswith("custom:") else "",
            _DEFAULT_METRICS)
    return {
        "engine": "live-retrieval",
        "engine_version": LIVE_ENGINE_VERSION,
        "disclaimer": (
            "REAL RETRIEVAL, NOT A BENCHMARK. Every record was fetched "
            "live from the listed public sources; metric quotes come from "
            "abstract/metadata text and were NOT reproduced here. Fields "
            "that could not be verified say 'Not verified'. This is a "
            "literature/registry review - NO tool was executed and NO "
            "independent benchmarking was performed. Coverage is bounded: "
            "see methodology + limitations."),
        "status": status,
        "scope": ("full pipeline" if len(resolved_targets) > 4 else
                  "targeted"),
        "targets": [{"id": t["id"], "label": t["label"]}
                    for t in resolved_targets],
        "executive_summary": [
            f"{len(candidates)} candidate rows assembled and verified "
            f"against live registries for {len(resolved_targets)} target(s)",
            f"{len(citations)} unique literature citations retrieved "
            f"(abstract/metadata level only)",
            f"coverage: {total_ok} successful queries, {total_fail} failed "
            f"queries - {status}",
            "no execution or independent benchmarking performed - any "
            "adoption decision requires runs on the deployment's own "
            "fixtures (see recommended_benchmarks)"],
        "inventory": {"repo": inv["repo"], "code_verified":
                      inv["code_verified"], "git_commit": inv["git_commit"],
                      "n_stages": len(inv["stages"]),
                      "captured_at": inv["captured_at"],
                      "stages": inv["stages"]},
        "candidates": candidates,
        "comparison": [
            {"target": c["task"], "tool": c["name"],
             "verified_version": c["verified_version"],
             "license": c["license"], "maintenance": c["maintenance"],
             "benchmark_evidence": (
                 f"{len(c['published_benchmark_evidence'])} retrieved "
                 f"mention(s)" if isinstance(
                     c["published_benchmark_evidence"], list) else
                 "Not verified"),
             "independently_validated": c["independent_validation"],
             "evidence_gaps": "; ".join(c["evidence_gaps"])}
            for c in candidates],
        "literature_citations": [
            {"title": c["title"], "year": c.get("year"),
             "url": c.get("url"), "source": c.get("source"),
             "id": c.get("id"), "targets": c.get("targets")}
            for c in citations[:60]],
        "license_constraints": sorted({c["license"] for c in candidates
                                       if c["license"] != "Not verified"}),
        "open_questions": [
            "Do candidate metrics replicate on THIS deployment's data?",
            "Are retrieved licenses current for the pinned versions?",
            "Which candidates integrate with the existing output schema?"],
        "recommended_benchmarks": benchmarks,
        "provenance": {
            "queries": (prog.get("queries") or [])[:400],
            "sources_consulted": sorted(per_source),
            "query_counts": per_source,
            "dedup_removed": dedup_removed,
            "exclusions": [
                {"reason": "duplicate record (normalised title)",
                 "count": dedup_removed},
                {"reason": "record without resolvable id",
                 "count": no_id_excluded}],
            "selection_criteria": "per-target multi-query retrieval; top "
                                  "results per query (page size 8); dedup "
                                  "by normalised title; candidate tools "
                                  "taken ONLY from configured-workflow "
                                  "tools and retrieved titles/repositories",
            "failures": failures},
        "methodology": (
            "Scope -> code-grounded inventory of the configured NeoAG "
            "repository -> per-target multi-query retrieval across "
            "literature sources (Europe PMC, PubMed, OpenAlex) and "
            "software registries (GitHub, PyPI, bio.tools) -> "
            "per-candidate existence/version/license verification from "
            "registries -> abstract-level benchmark evidence extraction "
            "-> standardised comparison rows. Full query log persisted "
            "in provenance.queries."),
        "limitations": [
            "abstracts/metadata only - full texts were not read; quoted "
            "metrics lack full benchmark context until checked",
            "NOT an exhaustive search of the literature",
            "no tool was executed: NO independent benchmark numbers exist "
            "in this report ('our own experiments' column is empty by "
            "design)",
            "source failures, if any, are listed in provenance.failures; "
            "status 'incomplete' means coverage gaps",
            ] + inv["limitations"],
        "availability_classes_legend": AVAILABILITY,
        "timestamps": {"started": (prog.get("steps") or [""])[0:1],
                       "compiled_at": now(),
                       "engine": LIVE_ENGINE_VERSION},
    }


# ---------------------------------------------------------------------------
# EXAMPLE engine (explicitly selected testing option only)
# ---------------------------------------------------------------------------

TARGETS = {
    "presentation": {"stage": "Presentation (MHC-I EL / ligand prediction)",
                     "current": "NetMHCpan 4.1b + MHCflurry 2.x",
                     "candidates": [
        {"tool": "NetMHCpan 4.1b", "version": "4.1b",
         "availability_class": "license-required (free academic license)",
         "license": "academic free / commercial paid (DTU)",
         "source": "Reynisson et al. 2020, NAR (mock citation example)",
         "publication_year": 2020, "evidence_quality": "high (widely used)",
         "accuracy_example": "example EL AUC ~0.94 on mock benchmark extract",
         "perf_example": "~10k peptides/min/core (mock figure)",
         "io_compat": "peptide+HLA in, binding scores out; pVACtools-integrated",
         "maintenance": "active, DTU Health Tech site (mock)",
         "notes": "current NeoAG tool - baseline for comparisons"},
        {"tool": "MHCflurry 2.x", "version": "2.0.x",
         "availability_class": "open-source (Apache-2.0)",
         "license": "Apache-2.0", "source": "O'Donnell et al. 2020, Cell (mock)",
         "publication_year": 2020, "evidence_quality": "high",
         "accuracy_example": "example EL AUC ~0.93 on mock benchmark extract",
         "perf_example": "GPU-friendly, ~50k peptides/s on GPU (mock figure)",
         "io_compat": "Python API; pVACtools-integrated",
         "maintenance": "active on GitHub (mock)", "notes": "current tool"},
        {"tool": "MHCnuggets 2.x", "version": "2.3.x",
         "availability_class": "open-source (MIT)",
         "license": "MIT", "source": "Shao et al. 2020, Immunology (mock)",
         "publication_year": 2020, "evidence_quality": "moderate (older model)",
         "accuracy_example": "example EL AUC ~0.89 on mock benchmark extract",
         "perf_example": "CPU hours for cohort scale (mock figure)",
         "io_compat": "Python API; no pVACtools plugin maintained",
         "maintenance": "low activity (mock)", "notes": "literature alternative"},
        {"tool": "BigMHC", "version": "1.x",
         "availability_class": "open-source",
         "license": "GPL-compatible (mock - verify before adopt)",
         "source": "Wang et al. 2023, NMI (mock)", "publication_year": 2023,
         "evidence_quality": "moderate (single-group benchmark)",
         "accuracy_example": "example immunogenicity AUC ~0.76 (mock)",
         "perf_example": "seconds per peptide list (mock)",
         "io_compat": "standalone scripts; wrapper needed",
         "maintenance": "limited (mock)", "notes": "candidate for EL+IM readout"},
    ]},
    "stability": {"stage": "Peptide-MHC stability",
                  "current": "NetMHCstabpan 1.0",
                  "candidates": [
        {"tool": "NetMHCstabpan 1.0", "version": "1.0",
         "availability_class": "license-required (free academic license)",
         "license": "DTU academic license",
         "source": "Rasmussen et al. 2016, JI (mock)", "publication_year": 2016,
         "evidence_quality": "high (production gate dependency)",
         "accuracy_example": "example Spearman ~0.79 on mock extract",
         "perf_example": "bulk web/API (mock)", "io_compat": "pVACtools-integrated",
         "maintenance": "stable, rarely updated (mock)",
         "notes": "REQUIRED by POL-001 for production ranking"},
        {"tool": "MHCStab-EL retraining on TransferLearned pan", "version": "-",
         "availability_class": "unclear (no maintained release)",
         "license": "research code only", "source": "literature only (mock)",
         "publication_year": 2021, "evidence_quality": "low (no runnable release)",
         "accuracy_example": "no example (not independently benchmarked)",
         "perf_example": "n/a", "io_compat": "incompatible as-is",
         "maintenance": "none (mock)", "notes": "availability risk example"},
    ]},
    "fusion": {"stage": "Fusion discovery",
               "current": "EasyFuse + STAR-Fusion (+Arriba optional)",
               "candidates": [
        {"tool": "EasyFuse 1.x", "version": "1.3.x",
         "availability_class": "freely available with restrictions (non-commercial)",
         "license": "non-commercial use (mock - verify)",
         "source": "Weber et al. 2021, Nat Commun (mock)", "publication_year": 2021,
         "evidence_quality": "high (published head-to-head)",
         "accuracy_example": "example F1 ~0.85 on mock extract",
         "perf_example": "~4 h/case RNA (mock)", "io_compat": "custom container",
         "maintenance": "active (mock)", "notes": "current tool"},
        {"tool": "STAR-Fusion 1.x", "version": "1.13",
         "availability_class": "open-source (GPL-3)",
         "license": "GPL-3", "source": "Haas et al. 2019 (mock)",
         "publication_year": 2019, "evidence_quality": "high",
         "accuracy_example": "example F1 ~0.80 on mock extract",
         "perf_example": "~2 h/case RNA (mock)", "io_compat": "standard STAR stack",
         "maintenance": "active (mock)", "notes": "current secondary caller"},
        {"tool": "Arriba 2.x", "version": "2.4",
         "availability_class": "freely available with restrictions (academic)",
         "license": "academic free; commercial paid", "source": "Uhlmann 2022 (mock)",
         "publication_year": 2022, "evidence_quality": "high",
         "accuracy_example": "example F1 ~0.83 on mock extract",
         "perf_example": "<1 h/case (mock)", "io_compat": "STAR + ChimericSAM",
         "maintenance": "active (mock)", "notes": "fast, but license review needed"},
    ]},
    "hla_typing": {"stage": "HLA typing",
                   "current": "OptiType + SpecHLA/HLA-LA",
                   "candidates": [
        {"tool": "OptiType 1.3.x", "version": "1.3.2+",
         "availability_class": "freely available with restrictions (academic)",
         "license": "academic", "source": "Szolek 2014 (mock)",
         "publication_year": 2014, "evidence_quality": "high",
         "accuracy_example": "example accuracy ~97% on mock extract",
         "perf_example": "minutes/case (mock)", "io_compat": "RNA/DNA BAM in",
         "maintenance": "stable (mock)", "notes": "current tool"},
        {"tool": "specHLA", "version": "1.0+",
         "availability_class": "open-source", "license": "GPL (mock - verify)",
         "source": "2024 preprint (mock)", "publication_year": 2024,
         "evidence_quality": "moderate (single paper)",
         "accuracy_example": "example accuracy ~98% on mock extract",
         "perf_example": "minutes/case, long-read aware (mock)",
         "io_compat": "BAM in; requires its container",
         "maintenance": "active (mock)", "notes": "also powers HLA LOH in NeoAG"},
    ]},
}

FULL_PIPELINE_TARGETS = list(TARGETS.keys())


def _example_recommendation(scope: str, target: str) -> dict:
    return {
        "verdict": ("No replacement recommended from this mock digest - the "
                    "current NeoAG tool set is already at or above the "
                    "mock literature baseline; adoption is NOT concluded."),
        "experiments_before_adoption": [
            "1. reproduce the candidate's published metric on NeoAG's own "
            "validation fixture (validated dataset, not literature numbers)",
            "2. run current vs candidate on the same 3-5 demo cases and "
            "record overlap/precision on identical inputs",
            "3. review the exact license text for commercial use and "
            "container redistribution (availability classes can change)",
            "4. pin tool version + container digest in the tools manifest "
            "before any production run",
            "5. confirm output-schema compatibility with raw_events/"
            "raw_peptides normalization (pVACtools-format mapping)",
        ],
    }


def build_example_report(scope: str, target_ids: list[str]) -> dict:
    keys = FULL_PIPELINE_TARGETS if scope == "full" else target_ids
    comparison = []
    for k in keys:
        meta = TARGETS.get(k)
        if not meta:
            comparison.append({"stage": k, "tool": "custom target",
                               "availability_class": "n/a",
                               "license": "n/a",
                               "evidence_quality": "n/a",
                               "accuracy_example": "",
                               "independently_benchmarked": False,
                               "basis": "MOCK EXAMPLE digest has no data "
                                        "for custom targets",
                               "notes": "use the LIVE engine for real "
                                        "retrieval"})
            continue
        for c in meta["candidates"]:
            comparison.append({
                "stage": meta["stage"],
                "current_in_neoag": c["tool"] in meta["current"],
                "tool": c["tool"], "version": c["version"],
                "availability_class": c["availability_class"],
                "license": c["license"],
                "source_citation": c["source"],
                "publication_year": c["publication_year"],
                "evidence_quality": c["evidence_quality"],
                "accuracy_example": c["accuracy_example"],
                "perf_example": c["perf_example"],
                "io_compat": c["io_compat"],
                "maintenance": c["maintenance"],
                "notes": c["notes"],
                "basis": "literature-derived (MOCK digest - numbers are "
                         "illustrative placeholders)",
                "independently_benchmarked": False,
            })
    return {
        "disclaimer": ("MOCK RESEARCH DIGEST - example content only, "
                       "selected deliberately as the 'example' testing "
                       "engine. No live literature search, no downloads, "
                       "no benchmarks were performed. Numbers are "
                       "illustrative placeholders."),
        "engine": "example",
        "scope": scope,
        "target": "full pipeline" if scope == "full" \
            else ",".join(target_ids),
        "availability_classes_legend": AVAILABILITY,
        "comparison": comparison,
        "recommendation": _example_recommendation(scope, ",".join(target_ids)),
        "provenance": {"engine": "example-mock", "dataset_version":
                       DEMO_DATASET_VERSION,
                       "live_sources_connected": False},
    }


RESEARCH_STEPS = [
    "scanning configured literature sources (MOCK - no network access)",
    "reading tool documentation for current versions (MOCK)",
    "checking repositories and release dates (MOCK)",
    "classifying availability and licensing (MOCK)",
    "extracting example accuracy/performance figures from mock digest (MOCK)",
    "separating literature-derived claims from independent benchmarks (MOCK)",
    "compiling recommendation + pre-adoption experiment list (MOCK)",
]


def run_example(rid: str, scope: str, target_ids: list[str],
                store: Store) -> None:
    from .server import publish
    prog = {"targets": [{"id": t, "label": t} for t in target_ids],
            "steps": [], "done_queries": [], "queries": []}
    with store.lock:
        store.db.execute("UPDATE research SET status='running' WHERE id=?",
                         (rid,))
        store.db.commit()
    for line in RESEARCH_STEPS:
        _step(store, publish, rid, line, prog)
        time.sleep(0.55)
    report = build_example_report(scope, target_ids)
    store.research_set(rid, "completed", report, finished=True)
    store.audit("engine", "research.completed", rid,
                {"scope": scope, "target": ",".join(target_ids),
                 "engine": "example"})
    publish({"type": "research", "id": rid})
