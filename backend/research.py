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
from collections import Counter

from . import evidence, inventory, mock_data, science, sources, versions
from .store import Store, new_id, now
from .mock_data import DEMO_DATASET_VERSION

LIVE_ENGINE_VERSION = "live-retrieval-2026.11-m6-r1"

# M6 A9: the ONLY recommendations a module may receive (fixed
# vocabulary, see backend/science.recommend for the evidence rules that
# license each category). The old M5 vocabulary is retired because it
# allowed "Consider candidate alternative" to coexist with "NO advantage
# established" - a contradiction a reader had to reconcile alone.
CONCLUSION_VOCABULARY = science.RECOMMENDATIONS

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
    """M6 A4: the retrieval starts from the module's SCIENTIFIC QUESTION
    and its task vocabulary (backend/science.py), not from bare keywords
    like 'ranking tools'."""
    prof = science.profile_for(target["id"], target.get("label") or "")
    plan = science.literature_queries(prof, target.get("label")
                                      or target["id"])
    return [q for q in plan
            if f"{q['source']}|{q['query']}" not in queries_done]




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
    # measured, not assumed: the unauthenticated GitHub core budget
    # (60/hour) gates the version-chain depth; exhausted budget is a
    # RECORDED SKIP reason, never a fake 'version unknown'
    budget = dict(sources.github_rate_limit())
    _step(store, publish, rid,
          f"GitHub API budget at start: {budget.get('core')} "
          f"core requests left"
          + (f" (resets {budget['reset_utc']})" if budget.get("reset_utc")
             else ""), prog,
          {"github_budget": budget})
    vcache, ft_budget = {}, {"n": 6}  # per-run chain cache + full-text cap
    def _attempted_ids():
        """targets that had at least one retrieval query (fresh or
        persisted) - what a stopped run may honestly claim as reached"""
        got = {e.get("target") for e in queries_log if e.get("target")}
        return {t["id"] for t in resolved_targets if t["id"] in got}
    if cancelled():
        store.research_set(rid, "cancelled",
                           _live_report(rid, prog, inv, [], "cancelled",
                                        reached_ids=set()),
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
        # literature phase interrupted: no conclusions yet - only
        # retrieval facts; candidate work never ran
        store.research_set(rid, "cancelled",
                           _live_report(rid, prog, inv, lit_by_target,
                                        "cancelled", reached_ids=set()),
                           finished=True)
        publish({"type": "research", "id": rid})
        return

    # ---- phase 2: dedup (by stable identifier, then title) + tools ----
    seen_keys, dedup_removed = set(), 0
    citations, no_id_excluded = [], 0
    for tid, recs in lit_by_target.items():
        for rec in recs:
            # source-native ids differ per source for the same paper -
            # only stable cross-source identifiers (DOI, PMCID) merge
            ids = [k for k in ((rec.get("doi") or "").lower(),
                               (rec.get("pmcid") or "").lower()) if k]
            key = next(iter(ids), None) or _norm_title(rec["title"])
            if key and key in seen_keys:
                dedup_removed += 1
                continue
            seen_keys.add(key)
            if not (rec.get("id") or rec.get("url")):
                no_id_excluded += 1
                continue
            citations.append({**rec, "targets": [tid]})
    _step(store, publish, rid,
          f"dedup: {dedup_removed} duplicate records removed; "
          f"{len(citations)} unique citations kept "
          f"({no_id_excluded} without resolvable id excluded)", prog)

    candidates = []
    done_targets = set()  # targets whose full candidate work completed
    for target in resolved_targets:
        if cancelled():
            break
        tid = target["id"]
        inv_stage = stages_by_id.get(tid) or {}
        prof = science.profile_for(tid, target.get("label") or "")
        # M6 A3/A4: candidate names come FIRST from the identified
        # baseline (current tools, never treated as alternatives) and
        # from tool identities extracted from the retrieved literature;
        # GitHub task-search hits are identity SUGGESTIONS only and must
        # still pass the same eligibility screen. No quotas: a module
        # may legitimately end up with zero suitable candidates.
        names = []
        for bt in (inv_stage.get("baseline") or {}).get("tools") or []:
            if bt["name"].startswith("NeoAG internal"):
                continue
            names.append({"name": bt["name"],
                          "origin": "current NeoAG baseline ("
                          + bt["role"] + ")",
                          "baseline": True,
                          "baseline_evidence": bt.get("evidence")})
        names += science.extract_tool_mentions(
            lit_by_target.get(tid, []), prof)
        gh_query = target["label"].split("(")[0].strip()
        tool_hits = []
        for q in ([{"source": "github", "query": gh_query,
                    "purpose": "task-named repositories (suggestions "
                               "only - screened like everything else)"}]
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
        names += [{"name": h["id"], "origin": f"GitHub task-search "
                   f"suggestion ({h['url']})"} for h in tool_hits[-4:]]
        # de-dup candidate names
        uniq, seenn = [], set()
        for n in names:
            k = n["name"].lower()
            if k not in seenn:
                seenn.add(k)
                uniq.append(n)
        for n in uniq[:10]:
            c = _verify_candidate(n, target, citations, per_source,
                                  failures, queries_log, done, store,
                                  publish, rid, vcache=vcache,
                                  budget=budget, ft=ft_budget,
                                  profile=prof)
            candidates.append(c)
            _step(store, publish, rid,
                  f"[{tid}] {c['name']}: eligibility -> "
                  f"{c['eligibility']} ({'; '.join(c['eligibility_reasons'])[:140]})",
                  prog, {})
        done_targets.add(tid)
    if cancelled():
        # a cancel after candidate work still publishes what was
        # honestly established so far - never discards evidence
        store.research_set(rid, "cancelled",
                           _live_report(rid, prog, inv, lit_by_target,
                                        "cancelled",
                                        candidates=candidates,
                                        citations=citations,
                                        resolved_targets=resolved_targets,
                                        reached_ids=done_targets),
                           finished=True)
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
                      queries_log, done, store, publish, rid,
                      vcache=None, budget=None, ft=None, profile=None):
    """Standardized candidate schema (D5, extended by M5 PART 4).
    Unverifiable fields say 'Not verified' - they are never filled with
    plausible estimates. New in PART 4: the version verification chain
    (4.2), full-context performance evidence (4.3) and artifact
    classification (4.4)."""
    tid = target["id"]
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
    q_bc, e_bc, _ = sources.anaconda_bioconda(slug)
    _record(per_source, failures, queries_log, "bioconda",
            f"verify:{slug}", len(q_bc), e_bc, tid,
            skip_ok_error="not-on-bioconda")

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
    bc = [r for r in q_bc if (r.get("id") or "") == slug]

    # ---- citations naming this candidate (evidence sources) ----
    name_l = n["name"].lower()
    named_cits = [c for c in citations
                  if name_l and name_l in
                  ((c.get("title") or "") + " " +
                   (c.get("abstract") or "")).lower()]

    # ---- 4.2 version verification chain ----
    vchain = versions.resolve(
        n["name"], repo=(gh[0]["id"] if gh else None),
        citations=named_cits,
        probes={"pypi": lambda _s: (q_py, e_py, {})},
        budget=budget, cache=vcache)
    stable_val = (vchain["values"].get("latest_stable") or {}).get("value")
    if stable_val and str(stable_val).startswith("commit"):
        stable_val = None  # a dev commit is not a stable to compare to

    # ---- 4.3 performance evidence WITH context ----
    ev = []
    for c in named_cits:
        cite = {"title": c["title"], "year": c.get("year"),
                "url": c.get("url"), "id": c.get("id"),
                "source": c.get("source")}
        rows = evidence.metric_rows(n["name"], c.get("abstract") or "",
                                    citation=cite, level="abstract")
        if not rows:
            rows = [{"metric": None, "value": None,
                     "quote": "names the tool; no explicit metric in "
                              "the retrieved abstract",
                     "citation": cite,
                     "evidence_level": (
                         "abstract: author-reported"
                         if name_l in (c.get("title") or "").lower()
                         else "abstract: contextual mention"),
                     "context_established": False,
                     "tested_version": "not stated by source",
                     "extracted_at": now()}]
        for r in rows:
            r.setdefault("citation", cite)
            r["quoted_metrics"] = ([f"{r['metric']} {r['value']}"]
                                   if r.get("metric")
                                   else ["no explicit metric in abstract"])
        ev.extend(rows[:3])
    # one full-text attempt per candidate (bounded run budget): the
    # efetch route works for open-access PMC deposits; Europe PMC's
    # fullTextXML sub-endpoint is unreachable from this host (measured)
    ft_note = ""
    if ft is not None and ft.get("n", 0) > 0:
        pcid = next((c.get("pmcid") for c in named_cits
                     if c.get("pmcid")), "")
        if pcid:
            ft["n"] -= 1
            xml, err = sources.pmc_fulltext(pcid)
            if xml:
                ftxt = evidence.xml_to_text(xml)
                for r in evidence.metric_rows(
                        n["name"], ftxt,
                        citation={"title": "full text via efetch db=pmc "
                                  f"(PMCID:{pcid})", "id": f"PMC:{pcid}",
                                  "url": f"https://pmc.ncbi.nlm.nih.gov"
                                         f"/articles/{pcid}/",
                                  "source": "ncbi-pmc"},
                        level="full text", max_rows=3):
                    r["from_fulltext"] = True
                    r["citation"] = {"id": f"PMC:{pcid}",
                                     "url": f"https://pmc.ncbi.nlm.nih"
                                            f".gov/articles/{pcid}/",
                                     "title": "full text (efetch)",
                                     "year": None,
                                     "source": "ncbi-pmc"}
                    r["quoted_metrics"] = ([f"{r['metric']} "
                                            f"{r['value']}"]
                                           if r.get("metric") else
                                           ["no metric sentence"])
                    ev.append(r)
            else:
                ft_note = f"full-text attempt failed: {err}"
    # cross-version guard: a number never transfers across versions
    for r in ev:
        if r.get("tested_version") or r.get("metric"):
            r["version_caveat"] = evidence.cross_version_caveat(
                r, stable_val)
    perf_summary = evidence.comparability_note(ev)
    # 4.4 what KIND of artifact is this?
    cls = versions.classify(
        [(gh[0].get("description") if gh else ""),
         (bt[0].get("summary") if bt else ""),
         (py[0].get("summary") if py else "")],
        archived=bool(gh and gh[0].get("archived")))
    # M6 A2: scientific eligibility screen (backend/science.py). The
    # text screened is ONLY what this run retrieved about THIS
    # candidate: registry blurbs + the titles of naming citations.
    blurb = " ".join(str(x or "") for x in [
        (gh[0].get("description") if gh else ""),
        (bt[0].get("summary") if bt else ""),
        (py[0].get("summary") if py else ""),
        " ".join((c.get("title") or "") for c in named_cits[:4])])
    if n.get("baseline"):
        elig = {"eligibility": "current baseline (not an alternative)",
                "reasons": [n["origin"]],
                "comparable_for_replacement": False,
                "confidence": "n/a"}
    else:
        # A literature-extracted candidate was ITSELF pulled out of
        # these citations' text, so a substring hit there is
        # self-reference, not corroboration - "named in retrieved
        # literature" only counts when the extractor's intro cue
        # actually introduced this name (method_context). For GitHub-
        # or registry-derived candidates, any naming citation is
        # independent evidence. (2026-11-17 negative fixture: "DQA101"
        # and "CoV-2" were mis-extracted from the same abstract that
        # introduced the real tool and would otherwise inherit its
        # naming-publication privilege.)
        lit_origin = (n.get("origin") or "").startswith(
            "named in retrieved literature")
        elig = science.screen_candidate(
            n["name"], blurb,
            profile=profile or science.GENERIC_PROFILE,
            origin=n.get("origin", ""),
            has_naming_source=bool(named_cits) and (
                not lit_origin or bool(n.get("method_context"))),
            artifact_class=cls["class"])
    # installed version: only meaningful for tools THIS deployment runs
    if "configured NeoAG workflow" in (n.get("origin") or ""):
        iv = versions.installed_version(base)
        installed = (f"{iv['value']} (evidence: {iv['evidence']})"
                     if iv.get("value") else
                     f"Not verified - {iv.get('reason', 'no evidence')}")
    else:
        installed = "n/a - not part of this deployment (alternative " \
                    "under consideration)"
    # None-coalescing through the `or` chain: a registry record that
    # exists but carries a null/blank field must print "Not verified",
    # never None and never an empty cell.
    verified_version = vchain["display"]
    license_ = (gh[0].get("license") if gh and
                gh[0].get("license") not in (None, "NOASSERTION") else
                (py[0].get("license") if py else
                 (bc[0].get("license") if bc else
                  (bt[0].get("license") if bt else "Not verified"))))
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
        ("current stable version", not vchain["values"].get(
            "latest_stable")),
        ("license", not license_ or license_ == "Not verified"),
        ("performance value WITH established experimental context",
         not any(r.get("context_established") for r in ev)),
        ("independent validation", not any(
            "independent" in e.get("evidence_level", "")
            for e in ev)),
        ("input/output format", True),  # never verified by these sources
    ) if miss]
    if ft_note:
        gaps.append(ft_note[:120])
    return {
        "name": n["name"],
        "eligibility": elig["eligibility"],
        "eligibility_reasons": elig["reasons"],
        "comparable_for_replacement": elig["comparable_for_replacement"],
        "screen_confidence": elig["confidence"],
        "is_baseline": bool(n.get("baseline")),
        "baseline_evidence": n.get("baseline_evidence"),
        "verified_version": verified_version,
        "version_chain": vchain,
        "installed_version": installed,
        "classification": cls,
        "task": target["label"],
        "role_in_neoag": n["origin"],
        "category": ("software (registry-verified)"
                     if (gh or py or bt or bc)
                     else "mentioned in literature only"),
        "artifact_class": cls["class"],
        "io_formats": "Not verified",
        "dependencies": "Not verified",
        "availability": ("found on " + ", ".join(
            s for s, ok in (("GitHub", gh), ("PyPI", py),
                            ("bio.tools", bt), ("bioconda", bc))
            if ok) if (gh or py or bt or bc) else "Not verified"),
        "license": license_ or "Not verified",
        "latest_release": ((vchain["values"].get("latest_release")
                            or {}).get("value")
                           or (py[0].get("version") if py else None)
                           or (bc[0].get("version") if bc else None)
                           or "Not verified"),
        "maintenance": maintenance,
        "primary_references": [e["citation"] for e in ev][:4] or
        "Not verified (no retrieved citation naming this tool)",
        "published_benchmark_evidence": ev or "Not verified",
        "performance_summary": perf_summary,
        "benchmark_versions": vchain["values"]["benchmark_versions"],
        "independent_validation": bool(
            [e for e in ev if "independent" in e.get("evidence_level",
                                                     "")]),
        "compatibility": "Not verified (integration surface not assessed "
                         "by these retrievals)",
        "strengths": "Not verified",
        "limitations": ("registry metadata + abstracts"
                        + (" + one full text" if any(
                            r.get("from_fulltext") for r in ev) else
                           " (full-text attempt unavailable)")),
        "evidence_gaps": gaps or ["none of the standard fields missing"],
        "next_action": ("run it on the deployment's own validation fixture "
                        "and compare on identical inputs - literature "
                        "claims are not adoption evidence"),
        "verification_hits": {
            "github": [h.get("url") for h in gh][:2],
            "pypi": [h.get("url") for h in py][:1],
            "biotools": [h.get("url") for h in bt][:2],
            "bioconda": [h.get("url") for h in bc][:1]},
    }


def _record(per_source, failures, queries_log, source, query, count, err,
            tid, skip_ok_error=None):
    per_source.setdefault(source, {"ok": 0, "fail": 0})
    entry = {"source": source, "query": query, "target": tid,
             "purpose": "candidate verification", "count": count,
             "error": err, "ts": now()}
    if err and skip_ok_error and skip_ok_error in str(err):
        # the registry gave a DEFINITE answer (e.g. "not on PyPI"):
        # a recorded outcome of the version check, never a failed
        # retrieval - tagged so report accounting never demotes a
        # module over it (see _live_report)
        entry["recorded_outcome"] = skip_ok_error
    queries_log.append(entry)
    if err and not (skip_ok_error and skip_ok_error in str(err)):
        per_source[source]["fail"] += 1
        failures.append(entry)
    elif not err:
        per_source[source]["ok"] += 1


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# M6 A6/A7: within-task comparison + A9 evidence-derived decisions
# ---------------------------------------------------------------------------

def _val(x):
    """Parse a reported metric value (0-1 or percent) for paired
    comparisons; None when not numeric."""
    s = str(x or "").strip().rstrip("%")
    try:
        v = float(s)
    except ValueError:
        return None
    return v / 100.0 if "%" in str(x) or v > 1.5 else v


def _usable_rows(c):
    ev = c.get("published_benchmark_evidence")
    return ([r for r in ev if isinstance(r, dict)
             and r.get("context_established")]
            if isinstance(ev, list) else [])


def _fmt_row(r):
    return (f"{r.get('metric')}={r.get('value')} on {r.get('dataset')}"
            f" [{r.get('tested_version') or 'version not stated'}]"
            + (" (author-reported)"
               if r.get("author_reported")
               or "author-reported" in r.get("evidence_level", "")
               else " (independent)"
               if r.get("independent")
               or "independent" in r.get("evidence_level", "") else ""))


def _superiority_rows(direct, baseline_cands):
    """A 'better' claim is licensed ONLY by a same-metric, same-dataset
    number on both sides - retrieval almost never produces that, which
    is exactly the point."""
    base_map = {}
    for bc in baseline_cands:
        for r in _usable_rows(bc):
            ds = (r.get("dataset") or "").lower()
            if ds in ("", "not stated by source"):
                continue
            key = (str(r.get("metric") or "").lower(), ds)
            v = _val(r.get("value"))
            if v is not None:
                base_map.setdefault(key, []).append(v)
    sup = []
    for c in direct:
        for r in _usable_rows(c):
            ds = (r.get("dataset") or "").lower()
            key = (str(r.get("metric") or "").lower(), ds)
            v = _val(r.get("value"))
            if v is None or key not in base_map:
                continue
            if all(v > b for b in base_map[key]):
                sup.append({"tool": c["name"], "row": r,
                            "beats": f"current tool {key[0]}="
                                     f"{max(base_map[key])} on {key[1]}"})
    return sup


def _task_comparisons(resolved_targets, candidates, stages_by_id):
    """One comparison block PER scientific module (M6 B2 section 3).
    Comparison rows contain ONLY direct alternatives; complementary
    methods, wrappers and screened-out hits are named WITH their reason
    but never dressed up as comparison rows."""
    out = []
    for t in resolved_targets:
        tid = t["id"]
        stage = stages_by_id.get(tid) or {}
        base = stage.get("baseline") or {}
        prof = science.profile_for(tid, t["label"])
        mine = [c for c in candidates if c["task"] == t["label"]]
        groups = {"complementary method": [],
                  "wrapper/integration (not an independent algorithm)": [],
                  "related but not comparable": [],
                  "insufficient information to classify": [],
                  "irrelevant": []}
        rows = []
        for c in mine:
            e = c["eligibility"]
            if e == "current baseline (not an alternative)":
                continue
            if e == "direct alternative":
                usable = _usable_rows(c)
                rows.append({
                    "tool": c["name"],
                    "version": c["verified_version"],
                    "evidence": ("; ".join(_fmt_row(r) for r in usable[:3])
                                 if usable else
                                 "relevant method; no performance number "
                                 "with established experimental context "
                                 "was retrieved for it"),
                    "license": c["license"],
                    "maintenance": c["maintenance"],
                    "why_comparable": "; ".join(c["eligibility_reasons"])
                                     [:200]})
            elif e in groups:
                groups[e].append(c["name"])
        out.append({
            "target": t["label"], "target_id": tid,
            "question": prof["question"],
            "baseline": {
                "identified": base.get("identified", False),
                "consensus": base.get("consensus", False),
                "tools": [{"name": b["name"], "role": b["role"],
                           "evidence": "; ".join(b["evidence"])[:220],
                           "pinned_version": b.get("pinned_version")}
                          for b in base.get("tools") or []],
                "note": base.get("note", "")},
            "rows": rows,
            "complementary": groups["complementary method"],
            "wrappers_integrations":
                groups["wrapper/integration (not an independent "
                       "algorithm)"],
            "related_not_comparable": groups["related but not comparable"],
            "insufficient": groups["insufficient information to classify"],
            "excluded_irrelevant": groups["irrelevant"],
            "note": ("only direct alternatives appear as comparison rows; "
                     "a wrapper integrates tools rather than competing "
                     "with them, and excluded candidates keep their "
                     "reason in the appendix")})
    return out


def _module_decision(target, candidates, stage, reached_fully=True):
    """M6 A9: a recommendation from the six-category fixed vocabulary,
    DERIVED from facts about the retrieved rows - the category cannot
    contradict the evidence, because it is computed from it."""
    tid = target["id"]
    prof = science.profile_for(tid, target["label"])
    base = stage.get("baseline") or {}
    mine = [c for c in candidates if c["task"] == target["label"]]
    baseline_cands = [c for c in mine if c.get("is_baseline")]
    direct = [c for c in mine if c["eligibility"] == "direct alternative"]
    comparable = [r for c in direct for r in _usable_rows(c)]
    independent = [r for r in comparable
                   if "independent" in r.get("evidence_level", "")]
    superiority = _superiority_rows(direct, baseline_cands)
    rec = science.recommend(
        baseline_identified=bool(base.get("identified")),
        direct_alts=direct, comparable_rows=comparable,
        independent_rows=independent, superiority_rows=superiority,
        profile=prof, coverage_partial=not reached_fully,
        gates_passed=True)
    return {"recommendation": rec["recommendation"],
            "conclusion": rec["recommendation"],  # legacy key
            "rationale": rec["rationale"],
            "action": rec["action"],
            "evidence_facts": {
                "candidates": len(mine),
                "direct_alternatives": len(direct),
                "comparable_rows": len(comparable),
                "independent_rows": len(independent),
                "superiority_rows": len(superiority),
                "baseline_identified": bool(base.get("identified")),
                "baseline_tools": [b["name"]
                                   for b in (base.get("tools") or [])]}}


def _live_status(per_source) -> str:
    ok = sum(v["ok"] for v in per_source.values())
    fail = sum(v["fail"] for v in per_source.values())
    if ok == 0:
        return "failed"
    if fail:
        return "incomplete"
    return "completed"


def _coverage_accounting(inv, resolved_targets, module_conclusions,
                         candidates, status, scope_ids=None):
    """4.7: completion is EVIDENCE COVERAGE, never 'all scheduled
    queries returned 200'. Every inventory stage is accounted for:
    researched-with-sufficient-evidence, researched-but-thin, or
    not-researched WITH its reason. M6 fix: a TARGETED run is complete
    for its own scope - modules outside the requested scope are labelled
    'not part of this run's scope', never 'the run stopped early', and
    they never poison the module-level decisions."""
    researched = {t["id"]: t.get("label") for t in resolved_targets}
    scope_ids = scope_ids if scope_ids is not None else set(researched)
    stopped_early = status in ("cancelled", "incomplete", "failed")
    sufficient, insufficient, missing = [], [], []
    for s in inv["stages"]:
        sid = s["id"]
        if sid not in researched:
            if sid not in scope_ids:
                missing.append({"id": sid,
                                "reason": "not part of this run's scope"})
            else:
                missing.append({
                    "id": sid,
                    "reason": ("run stopped (" + status + ") before this "
                               "module - resume keeps persisted queries"
                               if stopped_early else
                               "scheduled but not reached")})
            continue
        mc = module_conclusions.get(sid) or {}
        mine = [c for c in candidates if c["task"] == researched[sid]]
        verified = [c for c in mine if c["category"] ==
                    "software (registry-verified)"]
        ctx_rows = [r for c in mine
                    for r in c["published_benchmark_evidence"]
                    if isinstance(r, dict) and r.get("context_established")]
        if verified and ctx_rows:
            sufficient.append(sid)
        else:
            insufficient.append({
                "id": sid,
                "reason": (f"{len(verified)} registry-verified candidate(s), "
                           f"{len(ctx_rows)} context-established "
                           f"performance row(s) - conclusion: "
                           f"'{mc.get('conclusion', '?')}'")})
    # M6: a targeted run is COMPLETE FOR ITS SCOPE - modules outside
    # the requested scope are recorded but never make the run look
    # broken; only modules that were scheduled and not reached do.
    complete = not [m for m in missing if "scope" not in m["reason"]]
    return {
        "identified_modules": len(inv["stages"]),
        "researched_modules": len(researched),
        "sufficient_evidence": sufficient,
        "insufficient_evidence": insufficient,
        "not_researched": missing,
        "completeness": "complete" if complete else "partial",
        "note": ("status 'completed' means every issued query returned; "
                 "EVIDENCE completeness is THIS block. A partial review "
                 "is honest partial - and resumable."),
    }


def _next_actions(module_conclusions, coverage):
    """M6 B2 section 4: actions in decision-priority order, each tied to
    ONE module decision. Generic benchmark plans for irrelevant tools or
    modules without a defined endpoint are not produced (A9/B6):
    recommend() only emits a benchmark action when the module has
    relevant direct alternatives AND a published-metric endpoint."""
    prio = {"Candidate supported by comparative evidence": 0,
            "Recommend independent benchmark": 1,
            "Promising candidate - evaluate": 2,
            "Research incomplete": 3,
            "Insufficient evidence": 4,
            "Retain current tool": 5}
    acts = []
    for tid, mc in module_conclusions.items():
        rec = mc.get("recommendation") or mc.get("conclusion") or ""
        if rec == "Retain current tool":
            acts.append((5, f"[{tid}] Keep current tool - "
                            f"{mc['rationale'][:150]}"))
            continue
        if mc.get("action"):
            acts.append((prio.get(rec, 9),
                         f"[{tid}] {rec} -> {mc['action']}"))
    in_run_gaps = [m for m in coverage["not_researched"]
                   if "scope" not in m["reason"]]
    if in_run_gaps:
        acts.append((0, "Coverage: resume or re-run to cover "
                     f"{len(in_run_gaps)} module(s) this run did not "
                     "finish: " + ", ".join(m["id"] for m in
                                            in_run_gaps[:8])))
    acts.sort(key=lambda x: x[0])
    return [a[1] for a in acts]


def _live_report(rid, prog, inv, lit_by_target, status, candidates=None,
                 citations=None, resolved_targets=None, dedup_removed=0,
                 no_id_excluded=0, per_source=None, failures=None,
                 reached_ids=None):
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
    stages_by_id = {s["id"]: s for s in inv["stages"]}
    reached = (resolved_targets if reached_ids is None else
               [t for t in resolved_targets if t["id"] in reached_ids])
    # A8: a GitHub 403 rate-limit is a RECORDED gap in the version-
    # verification chain, not a failed retrieval of science - it must
    # not demote a module's evidence to 'research incomplete'. Only
    # genuine literature/registry retrieval failures do that. The same
    # honesty class covers definite registry-ABSENCE answers
    # ("not-on-pypi"/"not-on-bioconda"): the registry answered the
    # query, and "this package is not here" is an outcome, not a
    # failed retrieval (2026-11-17 live run: a completed 2-module run
    # with 7/7 quality gates was mislabelled 'Research incomplete'
    # because 32 honest not-on-registry answers counted as hard
    # failures). _record tags these at the source; the string check
    # also covers runs persisted before the tag existed.
    def _rate_only(err) -> bool:
        return "rate limit" in str(err or "").lower()

    def _recorded_outcome(q) -> bool:
        if q.get("recorded_outcome"):
            return True
        s = str(q.get("error") or "").lower()
        return _rate_only(s) or "not-on-pypi" in s or "not-on-bioconda" in s

    hard_fail_targets = {q.get("target")
                         for q in (prog.get("queries") or [])
                         if q.get("error") and q.get("target")
                         and not _recorded_outcome(q)}
    hard_failures = [f for f in failures if not _rate_only(
        f.get("error"))]
    task_comparisons = _task_comparisons(reached, candidates,
                                         stages_by_id)
    module_conclusions = {
        t["id"]: _module_decision(
            t, candidates, stages_by_id.get(t["id"]) or {},
            reached_fully=(t["id"] not in hard_fail_targets))
        for t in reached}
    coverage = _coverage_accounting(
        inv, reached, module_conclusions, candidates, status,
        scope_ids={t["id"] for t in resolved_targets})
    next_actions = _next_actions(module_conclusions, coverage)
    # ---- M6 A2 provenance: what was screened out, and why ----
    excluded = [
        {"name": c["name"], "module": c["task"],
         "eligibility": c["eligibility"],
         "reasons": c["eligibility_reasons"],
         "origin": c["role_in_neoag"],
         "where_it_came_from": c.get("verification_hits")}
        for c in candidates
        if c["eligibility"] in ("irrelevant",
                                "insufficient information to classify")]
    # ---- M6 A10 quality gates: preliminary vs decision-ready ----
    gates = science.quality_gates(
        targets=[t["id"] for t in reached],
        baselines={tc["target_id"]:
                   {"identified": tc["baseline"]["identified"]}
                   for tc in task_comparisons},
        candidates=candidates, excluded=excluded,
        citations_count=len(citations), failures=failures,
        coverage=coverage)
    in_run_gaps = [m for m in coverage["not_researched"]
                   if "scope" not in m["reason"]]
    ready = (science.decision_ready(gates) and not in_run_gaps
             and not hard_failures)
    rate_gaps = [f for f in failures if _rate_only(f.get("error"))]
    # ---- M6 B1: conclusion-first assessment (one minute to read) ----
    by_rec = Counter(m["recommendation"]
                     for m in module_conclusions.values())
    adv = {k: v for k, v in by_rec.items()
           if k in ("Candidate supported by comparative evidence",
                    "Promising candidate - evaluate",
                    "Recommend independent benchmark")}
    if adv.get("Candidate supported by comparative evidence"):
        headline = ("Directly comparable published evidence supports at "
                    "least one alternative - adoption still requires "
                    "validation on this deployment (see module table).")
    elif adv:
        headline = ("NO tool replacement is justified today. "
                    + ", ".join(f"{v}x {k}" for k, v in adv.items())
                    + f" - relevant candidates exist for {len(adv)} "
                      "outcome type(s) but no comparable performance "
                      "numbers were retrieved. The current tools stay "
                      "in place.")
    else:
        parts = ", ".join(f"{v}x {k}" for k, v in sorted(
            by_rec.items(), key=lambda kv: -kv[1]))
        headline = ("NO tool replacement is justified for any researched "
                    f"module ({parts}). Where zero suitable alternatives "
                    "were found, that is the finding - not a gap to "
                    "pad with irrelevant software.")
    findings = [
        f"[{tid}] {mc['recommendation']} - {mc['rationale'][:170]}"
        for tid, mc in module_conclusions.items()
        if mc["recommendation"] != "Retain current tool"][:6]
    assessment = {
        "headline": headline,
        "scope": (f"{len(reached)} module(s) researched this run "
                  f"({'targeted scope' if len(reached) < len(inv['stages']) else 'full pipeline'}); "
                  f"{len(in_run_gaps)} scheduled module(s) not finished"
                  + (f", {len(coverage['not_researched']) - len(in_run_gaps)} outside the requested scope"
                     if len(coverage["not_researched"]) > len(in_run_gaps)
                     else "")
                  + f" (scope completeness: {coverage['completeness']}); "
                  f"{len(citations)} unique literature records; "
                  f"{len(candidates)} candidates screened, "
                  f"{len(excluded)} excluded as irrelevant or "
                  "unclassifiable"),
        "top_findings": findings or [
            "every researched module retains its current tool"],
        "evidence_gaps": [
            tc["baseline"]["note"] for tc in task_comparisons
            if tc["baseline"]["note"]]
            + ([f"{len(hard_failures)} literature/registry retrieval "
                f"failure(s) recorded in provenance"]
               if hard_failures else [])
            + ([f"{len(rate_gaps)} GitHub verification query(ies) hit "
                "the shared rate budget - recorded as version-chain "
                "gaps, not evidence of absence (a re-run completes "
                "them)"] if rate_gaps else [])
            + ([] if not in_run_gaps else
               [f"run did not finish {len(in_run_gaps)} scheduled "
                "module(s) - resume keeps persisted queries"]),
        "next_action": next_actions[0] if next_actions else
                       "no action - all modules retain current tools",
        "decision_ready": ready,
        "review_class": ("decision-ready review" if ready else
                         "preliminary research - the quality gates "
                         "below did not all pass")}
    module_table = []
    for tc in task_comparisons:
        mc = module_conclusions.get(tc["target_id"]) or {}
        cur = ", ".join(t["name"] for t in tc["baseline"]["tools"]) or \
            "(baseline NOT identified)"
        best = tc["rows"][0]["tool"] if tc["rows"] else \
            ("-" if tc["baseline"]["identified"] else "blocked: "
             "baseline unidentified")
        ev_line = (tc["rows"][0]["evidence"][:120] if tc["rows"]
                   else "no candidate passed the scientific relevance "
                        "screen as a direct alternative")
        module_table.append({
            "module": tc["target"], "current NeoAG method": cur,
            "best relevant alternative": best, "evidence": ev_line,
            "recommendation": mc.get("recommendation", "not researched")})
    scientific_comparison = [
        {**tc, "decision": module_conclusions.get(tc["target_id"])}
        for tc in task_comparisons]
    version_license_findings = [
        {"tool": c["name"], "target": c["task"],
         "version_verdict": c["verified_version"],
         "is_development": bool((c["version_chain"]["values"].get(
             "latest_stable") or {}).get("development")),
         "unresolved_version": c["verified_version"] == "Not verified",
         "license": c["license"],
         "compatibility": c["compatibility"]}
        for c in candidates]
    gb = prog.get("github_budget") or {}
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
        # M6 B1: the FIRST thing a reader sees (also rendered first by
        # every export) - not citation counts or API bookkeeping.
        "assessment": assessment,
        "module_table": module_table,
        "decision_ready": ready,
        "review_class": assessment["review_class"],
        "quality_gates": gates,
        "executive_summary": [
            assessment["headline"],
            assessment["scope"],
            "recommendations: " + (", ".join(
                f"{v}x {k}" for k, v in sorted(
                    by_rec.items(), key=lambda kv: -kv[1])) or "none"),
            f"{total_ok} retrieval queries succeeded, {total_fail} "
            f"failed (recorded in provenance) - run status: {status}"],
        "inventory": {"repo": inv["repo"], "code_verified":
                      inv["code_verified"], "git_commit": inv["git_commit"],
                      "n_stages": len(inv["stages"]),
                      "captured_at": inv["captured_at"],
                      "stages": inv["stages"]},
        "candidates": candidates,
        "task_comparisons": task_comparisons,
        # M6 B2: main-narrative blocks (rendered before the appendix)
        "scientific_comparison": scientific_comparison,
        "excluded_candidates": excluded,
        "action_plan": [
            {"priority": i + 1, "module": a.split("]")[0].strip("["),
             "action": a}
            for i, a in enumerate(next_actions)],
        "module_conclusions": module_conclusions,
        "conclusion_vocabulary": CONCLUSION_VOCABULARY,
        "eligibility_vocabulary": science.ELIGIBILITY_VOCABULARY,
        "comparison": [
            {"target": c["task"], "tool": c["name"],
             "eligibility": c["eligibility"],
             "artifact_class": c["artifact_class"],
             "verified_version": c["verified_version"],
             "license": c["license"], "maintenance": c["maintenance"],
             "benchmark_evidence": (
                 f"{len(c['published_benchmark_evidence'])} retrieved "
                 f"mention(s)" if isinstance(
                     c["published_benchmark_evidence"], list) else
                 "Not verified"),
             "performance_summary": c["performance_summary"],
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
        "coverage_accounting": coverage,
        "recommended_next_actions": next_actions,
        "version_license_findings": version_license_findings,
        # B6: a benchmark is proposed ONLY for modules whose decision is
        # "Recommend independent benchmark" - never for every module and
        # never for irrelevant tools.
        "recommended_benchmarks": {
            tid: benchmarks[tid] for tid in benchmarks
            if (module_conclusions.get(tid) or {}).get("recommendation")
            == "Recommend independent benchmark"},
        "provenance": {
            "queries": (prog.get("queries") or [])[:400],
            "sources_consulted": sorted(per_source),
            "query_counts": per_source,
            "github_budget_at_start": gb or "not measured",
            "dedup_removed": dedup_removed,
            "exclusions": [
                {"reason": "duplicate record (normalised title)",
                 "count": dedup_removed},
                {"reason": "record without resolvable id",
                 "count": no_id_excluded}],
            "selection_criteria": "literature-first: candidate identities "
                                  "come from the identified baseline and "
                                  "from tool names extracted from "
                                  "retrieved papers; GitHub hits are "
                                  "identity suggestions only; EVERY "
                                  "candidate passes the fixed eligibility "
                                  "screen (science.screen_candidate) "
                                  "before entering any comparison; "
                                  "citations deduped by DOI/PMCID then "
                                  "normalised title",
            "failures": failures},
        "methodology": (
            "Scope -> BASELINE identification from the deployed NeoAG "
            "repo (workflow TOML stage+run-level groups, planner code "
            "wiring, runner scripts, conda envs, module dirs - each tool "
            "with file evidence) -> task-question-driven literature "
            "retrieval (Europe PMC, PubMed, OpenAlex; benchmark/review/"
            "method/benchmark-dataset query families) -> tool-identity "
            "extraction from titles/abstracts -> registry verification "
            "(GitHub, PyPI, bio.tools, bioconda) with a per-candidate "
            "VERSION CHAIN (official releases -> git tags -> registries "
            "-> repo version file -> papers; budget-skipped steps keep "
            "their reason) -> ELIGIBILITY SCREEN (direct alternative / "
            "complementary / wrapper / related-not-comparable / "
            "irrelevant / insufficient - from the candidate's own "
            "retrieved description, reasons persisted) -> performance "
            "evidence extraction with a context gate (abstracts plus "
            "bounded open-access full texts via NCBI efetch) -> "
            "evidence-derived six-category recommendations -> research "
            "quality gates deciding preliminary vs decision-ready. "
            "Full query log persisted in provenance.queries."),
        "limitations": [
            "abstracts/metadata are the evidence floor; full texts were "
            "attempted ONLY for open-access PMC deposits via efetch "
            "(Europe PMC's fullTextXML sub-endpoint is unreachable from "
            "this host - measured 2026-11-16); metrics without an "
            "established dataset/ground-truth context are quoted but "
            "excluded from comparisons",
            "NOT an exhaustive search of the literature",
            "no tool was executed: NO independent benchmark numbers exist "
            "in this report ('our own experiments' column is empty by "
            "design)",
            "source failures, if any, are listed in provenance.failures; "
            "status 'incomplete' means coverage gaps",
            "GitHub version-chain depth is gated by the shared "
            "unauthenticated 60 req/h budget"
            + (f" ({gb.get('core')} left at run start, resets "
               f"{gb.get('reset_utc')})" if gb.get("reset_utc") else
               "; steps skipped for budget keep their reason in each "
               "candidate's version_chain")
            + "; a skipped step is a recorded gap, never a fake 'version "
              "unknown'",
            "artifact classification is a labelled heuristic over short "
            "retrieved blurbs, not a code audit",
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
    rec = _example_recommendation(scope, ",".join(target_ids))
    # M7 §8: the example fixture mirrors the PRODUCTION report shape -
    # it leads with the assessment too, and the assessment itself says
    # in plain words that this is a labelled test fixture and not a
    # decision (decision_ready is never true for a mock).
    assessment = {
        "headline": ("MOCK example digest for testing the report UI - "
                     "NOT a decision: no live retrieval ran and every "
                     "number below is an illustrative placeholder."),
        "scope": (f"{len(keys)} module(s) in the example fixture's "
                  + ("full-pipeline" if scope == "full" else "targeted")
                  + " digest; live sources not connected"),
        "top_findings": [str(rec)[:200]] if rec else [],
        "evidence_gaps": ["example fixture: no live literature search, "
                          "no registry verification, no benchmarks - "
                          "submit a LIVE research run for a decision"],
        "next_action": ("submit a live research run when a real "
                        "decision is needed"),
        "decision_ready": False,
        "review_class": "MOCK example - not a review",
    }
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
        "recommendation": rec,
        "assessment": assessment,
        "review_class": assessment["review_class"],
        "decision_ready": False,
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
