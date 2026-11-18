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
from pathlib import Path

from . import (baselines, claims, claimverify, evidence, identity,
               inventory, mock_data, ontology, science, sources, versions)
from .store import Store, new_id, now
from .mock_data import DEMO_DATASET_VERSION

LIVE_ENGINE_VERSION = "live-retrieval-2026.11-m8-r1"

# M6 A9: the ONLY recommendations a module may receive (fixed
# vocabulary, see backend/science.recommend for the evidence rules that
# license each category). The old M5 vocabulary is retired because it
# allowed "Consider candidate alternative" to coexist with "NO advantage
# established" - a contradiction a reader had to reconcile alone.
CONCLUSION_VOCABULARY = science.RECOMMENDATION_LABELS_V2

# M8 S4: the internal relevance relationship (assess_relevance output)
# mapped onto the fixed eligibility vocabulary the report and UI use.
# "irrelevant" is ONLY ever reached from a POSITIVE mismatch (see
# science.assess_relevance); absence of vocabulary maps to
# "insufficient information to classify" and keeps the row open.
RELATIONSHIP_ELIGIBILITY = {
    "direct-alternative": "direct alternative",
    "complementary": "complementary method",
    "wrapper": "wrapper/integration (not an independent algorithm)",
    "unrelated": "irrelevant",
    "unresolved": "insufficient information to classify",
}

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
    # Resume durability (measured 2026-11-18): a skipped query is
    # only safe when its RECORDS come back with the skip. The
    # resumed dc8da8 run skipped persisted queries but rebuilt the
    # conclusion from freshly fetched records only - PureCN/CNVkit
    # vanished from the candidate set while the status said
    # 'completed'. Retrieval records are now persisted per query
    # and restored on resume; a done query without persisted
    # records is re-run instead of skipped.
    lit_records = dict(prog.get("lit_records") or {})
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
    icache = {}          # per-run identity-record cache
    followup_budget = {"n": 10}  # investigation follow-up queries per run
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
        plan = _literature_plan(target, set())
        lit_by_target.setdefault(target["id"], [])
        for q in plan:
            qkey = f"{q['source']}|{q['query']}"
            if qkey in done and qkey in lit_records:
                # resume: restore the evidence, do not just skip
                lit_by_target[target["id"]].extend(
                    lit_records[qkey])
                continue
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
                done.add(qkey)
                lit_records[qkey] = recs
                lit_by_target[target["id"]].extend(recs)
            _step(store, publish, rid,
                  f"[{target['id']}] {q['source']}: '{q['query'][:60]}' -> "
                  + (f"{len(recs)} records" if not err else f"FAILED {err}"),
                  prog, {"done_queries": sorted(done),
                         "queries": queries_log,
                         "lit_records": lit_records,
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
            citations.append({**rec, "targets": [tid],
                              "cid": _canon_cid(rec)})
    cid_keys = {c["cid"] for c in citations}
    _step(store, publish, rid,
          f"dedup: {dedup_removed} duplicate records removed; "
          f"{len(citations)} unique citations kept "
          f"({no_id_excluded} without resolvable id excluded)", prog)

    candidates = []
    row_docs = {}  # run-scope: candidate name -> retrieved document set
    done_targets = set()  # targets whose full candidate work completed
    for target in resolved_targets:
        if cancelled():
            break
        tid = target["id"]
        inv_stage = stages_by_id.get(tid) or {}
        prof = science.profile_for(tid, target.get("label") or "")
        task = ontology.task_for(tid)
        baseline_tool_names = [b["name"] for b in
                               (inv_stage.get("baseline") or {}).get(
                                   "tools") or []]
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
        gh_key = f"github|{gh_query}"
        if gh_key in done and gh_key in lit_records:
            tool_hits.extend(lit_records[gh_key])  # resume restore
        for q in ([{"source": "github", "query": gh_query,
                    "purpose": "task-named repositories (suggestions "
                               "only - screened like everything else)"}]
                  if gh_key not in done else []):
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
                done.add(gh_key)
                lit_records[gh_key] = recs
                tool_hits.extend(recs)
            _step(store, publish, rid,
                  f"[{tid}] github repos for task: -> "
                  + (f"{len(recs)}" if not err else f"FAILED {err}"),
                  prog, {"queries": queries_log,
                         "done_queries": sorted(done),
                         "lit_records": lit_records,
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
                                  task=task,
                                  known_names=[x["name"] for x in uniq],
                                  baseline_names=baseline_tool_names,
                                  icache=icache)
            row_docs[c["name"].lower()] = c.pop("_docs", None) or []
            candidates.append(c)
            _step(store, publish, rid,
                  f"[{tid}] {c['name']}: {c['eligibility']} "
                  f"(identity {c['identity_status']}, state "
                  f"{c['research_state']}) - "
                  f"{'; '.join(c['eligibility_reasons'])[:140]}",
                  prog, {})
        # ---- M8 investigation loop: bounded deepening pass. Rows
        # with recorded follow-up queries get those queries EXECUTED
        # within the run budget; literature naming the candidate
        # grows its document set, claims are re-verified and the
        # relevance verdict + research-state ladder are RE-DERIVED
        # from the grown evidence. Skips (budget) are recorded. ----
        for c in [x for x in candidates
                  if x["task"] == target["label"]
                  and x.get("followups_open")]:
            if cancelled():
                break
            _deepen_candidate(c, target, citations, per_source,
                              failures, queries_log, done, cid_keys,
                              store, publish, rid, budget=budget,
                              fb=followup_budget, task=task, prof=prof,
                              known_names=[x["name"] for x in uniq],
                              baseline_names=baseline_tool_names,
                              docs_by_name=row_docs,
                              lit_records=lit_records)
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


# ---------------------------------------------------------------------------
# M8 integration helpers (S3 identity attribution, S6 claim extraction,
# S7 independent verification). All deterministic over ALREADY-RETRIEVED
# text; no network, no model.
# ---------------------------------------------------------------------------

def _canon_cid(c: dict) -> str:
    """One canonical id per paper (DOI > PMCID > PMID > arXiv > id >
    normalised title): the doc_cid for claim extraction, the key for
    provenance accounting and for follow-up dedup."""
    for k, pfx in (("doi", ""), ("pmcid", ""), ("pmid", "pmid:"),
                   ("arxiv", "arxiv:")):
        v = str(c.get(k) or "").strip().lower()
        if v:
            return pfx + v
    cid = str(c.get("cid") or c.get("id") or "").strip().lower()
    if cid:
        return cid
    return "title:" + _norm_title(c.get("title") or "")


def _record_agrees_domain(task, rec) -> bool:
    """A NAME-EXACT registry record can still denote a different
    project (PyPI's `purple` is a color library, not the PURPLE CNV
    caller). A record may carry claims for this candidate only when
    its own description speaks the task's domain vocabulary, or says
    too little to contradict (PART 1.3: names never merge evidence)."""
    if not task:
        return True
    desc = str(rec.get("description") or rec.get("summary") or "")
    if not desc.strip():
        return True  # no description: nothing contradicts the identity
    return bool(ontology.match_domain(task, desc))


def _same_subject(subj, name) -> bool:
    s = str(subj or "").strip().lower()
    n = str(name or "").strip().lower()
    return bool(s) and bool(n) and (s == n or n in s or s in n)


def _verified_measurements(name, docs, known_names):
    """S6 + S7 per document: extract claims with exact spans, then let
    the INDEPENDENT verifier re-parse those spans from the original
    text. Returns (rows, log): rows are measurements that CONFIRMED as
    this candidate's (or were CORRECTED to it, with the verifier's
    corrected metric/subject applied); every rejected measurement,
    unresolved binding and foreign-attribution result goes into the
    log, which the candidate row carries verbatim - rejected items are
    accounted for, never silently dropped, and never used (PART 1.5
    fix: attribution errors die at the verifier, not in the report)."""
    rows, log = [], []
    for d in docs:
        if d.get("kind") not in ("citation-abstract", "fulltext"):
            continue
        if d.get("superseded_by"):
            continue  # same paper's full text already extracted once
        text = d.get("text") or ""
        if not text.strip():
            continue
        cl = claims.extract_claims(text, doc_cid=d["doc_cid"],
                                   level=d.get("kind"),
                                   known_names=known_names)
        verd = claimverify.verify_claims(text, cl, known_names)
        ctx = claims.extract_context(text)
        cite = d.get("citation") or {"title": d["doc_cid"]}
        author = ("author-reported" if name.lower()
                  in str(cite.get("title") or "").lower()
                  else "independent")
        for c, v in zip(cl, verd):
            for m, mv in zip(c.get("measurements", []),
                             v["measurement_verdicts"]):
                subj = c.get("subject")
                metric = m.get("metric")
                if mv["verdict"] == "correct":
                    subj = mv.get("found_subject") or subj
                    metric = mv.get("found_metric") or metric
                base = {
                    "metric": metric, "value": m.get("value"),
                    "ci": m.get("ci"), "quote": c.get("quote"),
                    "quote_span": c.get("quote_span"),
                    "value_span": m.get("value_span"),
                    "doc_cid": c.get("doc_cid"),
                    "binding": m.get("binding"),
                    "verified": mv["verdict"],
                    "verify_note": mv.get("note", ""),
                    "subject": subj, "citation": cite,
                    "context": dict(ctx),
                    "dataset": "not stated by source",
                    "tested_version": "not stated by source",
                    "evidence_level": f"{d['kind']}: {author} claim, "
                                      f"{mv['verdict']}"}
                if mv["verdict"] == "reject":
                    base["excluded"] = True
                    log.append(base)
                    continue
                if metric is None:
                    base["excluded"] = True
                    base["verify_note"] = ((base["verify_note"] + "; ")
                                           if base["verify_note"] else
                                           "") + ("metric unresolved - "
                                                  "ambiguous binding, "
                                                  "excluded from every "
                                                  "comparison")
                    log.append(base)
                    continue
                if not _same_subject(subj, name):
                    base["excluded"] = True
                    base["verify_note"] = (f"measurement belongs to "
                                           f"{subj!r}, not this "
                                           f"candidate")
                    log.append(base)
                    continue
                base["context_established"] = bool(
                    ctx.get("ground_truth") or ctx.get("modality")
                    or ctx.get("sample_design"))
                rows.append(base)
    # one claim, one row: the same (doc, subject, metric, value) may
    # legitimately appear twice inside ONE document (PMC full text
    # repeats its findings in prose and in the summary - measured
    # 2026-11-17: PureCN's 19.3%/91.2% sentence produced two rows
    # each); for comparisons that is still a single observation
    seen_claims: set = set()
    out_rows = []
    for r in rows:
        key = (r.get("doc_cid"), str(r.get("subject")).lower(),
               r.get("metric"), r.get("value"))
        if r.get("metric") and key in seen_claims:
            log.append({**r, "excluded": True, "verify_note":
                        "duplicate of the same claim within the same "
                        "document - counted once"})
            continue
        if r.get("metric"):
            seen_claims.add(key)
        out_rows.append(r)
    return out_rows, log


def _evidence_rows(docs, verified):
    """Merge per-document verified measurements with honest
    mention-only placeholder rows: a document naming the tool but
    yielding no surviving measurement is recorded AS a mention -
    never silently empty, never dressed up as data."""
    ev = []
    for d in docs:
        if d.get("kind") not in ("citation-abstract", "fulltext"):
            continue
        if d.get("superseded_by"):
            continue  # counted through the full text of the same paper
        cite = d.get("citation") or {"title": d["doc_cid"]}
        got = [r for r in verified if r["doc_cid"] == d["doc_cid"]]
        for r in got:
            r["from_fulltext"] = d["kind"] == "fulltext"
            r["quoted_metrics"] = [f"{r['metric']} {r['value']}"]
        ev.extend(got)
        if not got:
            ev.append({"metric": None, "value": None,
                       "quote": "names the tool; no measurement claim "
                                "survived the independent "
                                "verification pass",
                       "citation": cite, "doc_cid": d["doc_cid"],
                       "from_fulltext": d["kind"] == "fulltext",
                       "evidence_level": f"{d['kind']}: mention only",
                       "context_established": False,
                       "tested_version": "not stated by source",
                       "quoted_metrics": ["no surviving metric"],
                       "extracted_at": now()})
    return ev


def _mv_summary(verified, vlog):
    return {"confirmed": len([r for r in verified
                              if r["verified"] == "confirm"]),
            "corrected": len([r for r in verified
                              if r["verified"] == "correct"]),
            "rejected": len([x for x in vlog
                             if x.get("verified") == "reject"]),
            "foreign_or_unbound": len([
                x for x in vlog if x.get("verified") != "reject"]),
            "items": vlog[:24]}


def _verify_candidate(n, target, citations, per_source, failures,
                      queries_log, done, store, publish, rid,
                      vcache=None, budget=None, ft=None, task=None,
                      known_names=(), baseline_names=(), icache=None):
    """Standardized candidate schema (D5; M8 scientific overhaul).
    Unverifiable fields say 'Not verified' - never a plausible
    estimate. M8 adds: an IDENTITY record (S3 - projects never merge
    on a name alone), the full document set for the relevance layer
    (S4 - positive evidence only), per-document claim extraction plus
    an INDEPENDENT verification pass (S6/S7), and version/license
    attribution gated on the identity verdict with domain-agreement
    screening of name-colliding records (PART 1.1/1.3/1.4/1.5 fixes)."""
    tid = target["id"]
    task = task or ontology.task_for(tid)
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
    # Bioconductor is the canonical registry for the R/Bioconductor
    # tools this pipeline runs (PureCN, faCets, ...); its landing page
    # carries the release version AND the project's GitHub link, which
    # is the missing merge bridge for tools whose bioconda recipe is
    # 'bioconductor-<name>' (measured 2026-11-17: PureCN stuck at one
    # description axis)
    q_bo, e_bo, _ = sources.bioconductor_package(base)
    _record(per_source, failures, queries_log, "bioconductor",
            f"verify:{base}", len(q_bo), e_bo, tid,
            skip_ok_error="not-on-bioconductor")

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
    bo = [r for r in q_bo if (r.get("id") or "").lower() ==
          base.lower()]

    # ---- citations naming this candidate (evidence sources) ----
    name_l = n["name"].lower()
    named_cits = [c for c in citations
                  if name_l and name_l in
                  ((c.get("title") or "") + " " +
                   (c.get("abstract") or "")).lower()]

    # ---- S4 document set: EVERY piece of text this run retrieved
    # about this candidate (registry blurbs, naming-citation titles +
    # abstracts, full text). The relevance layer never sees less than
    # what exists (PART 1.1/1.2 fix: a thin blurb is not an
    # investigation; a well-documented tool no longer collapses to
    # one under-read description) ----
    docs = []
    if gh:
        docs.append({"doc_cid": f"github:{gh[0].get('id')}",
                     "kind": "registry-description",
                     "text": str(gh[0].get("description") or "")})
    if bt:
        docs.append({"doc_cid": f"biotools:{bt[0].get('id')}",
                     "kind": "registry-summary",
                     "text": str(bt[0].get("summary") or "")})
    if py:
        docs.append({"doc_cid": f"pypi:{py[0].get('id')}",
                     "kind": "registry-summary",
                     "text": str(py[0].get("summary") or "")})
    for c in named_cits[:6]:
        cite = {"title": c.get("title"), "year": c.get("year"),
                "url": c.get("url"), "id": c.get("id"),
                "source": c.get("source")}
        text = (((c.get("title") or "") + ". " +
                 (c.get("abstract") or "")).strip())
        if text.strip(" ."):
            docs.append({"doc_cid": c.get("cid") or _canon_cid(c),
                         "kind": "citation-abstract",
                         "text": text, "citation": cite})
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
                ft_cid = f"pmc:{pcid.lower()}"
                docs.append({"doc_cid": ft_cid,
                             "kind": "fulltext",
                             "text": evidence.xml_to_text(xml),
                             "citation": {
                                 "id": f"PMC:{pcid}",
                                 "url": "https://pmc.ncbi.nlm.nih.gov"
                                        f"/articles/{pcid}/",
                                 "title": "full text (efetch db=pmc)",
                                 "year": None, "source": "ncbi-pmc"}})
                # one physical paper must corroborate ONCE: when the
                # full text of a paper is in hand, that same paper's
                # abstract document (reached via its DOI cid, the
                # full text via its PMCID cid) is textually a subset
                # and is superseded for measurement extraction
                # (measured 2026-11-17: every measurement of the
                # diagnostics paper appeared twice, inflating the
                # verified-pair count). Provenance is untouched - the
                # abstract stays citable; extraction just does not
                # count the same sentences twice.
                for d in docs:
                    if d.get("kind") != "citation-abstract" or \
                            d.get("superseded_by"):
                        continue
                    for c in named_cits:
                        if str(c.get("pmcid") or "").lower() == \
                                pcid.lower() and \
                                (c.get("cid") or _canon_cid(c)) == \
                                d["doc_cid"]:
                            d["superseded_by"] = ft_cid
                            break
            else:
                ft_note = f"full-text attempt failed: {err}"

    # ---- S3 identity: WHICH PROJECT does this name denote? Axes are
    # corroborating evidence axes (naming paper, registry record,
    # repository description in the task's domain, component
    # recognition); resolved needs >=2. A name collision ends up
    # contested/unresolved and is NEVER merged (PART 1.3/1.4). ----
    extra_ev = []
    for c in named_cits[:4]:
        tl = str(c.get("title") or "")
        if name_l in tl.lower():
            extra_ev.append({"axis": "naming-paper",
                             "source": "literature",
                             "detail": f"paper title names it: "
                                       f"{tl[:80]} "
                                       f"({c.get('id') or c.get('cid')})"})
    reg_hits = ([dict(r, source="bio.tools") for r in bt] +
                [dict(r, source="bioconda") for r in bc] +
                [dict(r, source="pypi") for r in py] +
                [dict(r, source="bioconductor",
                      home=(r.get("repo_url") or None)) for r in bo])

    def _probe(project, tool):
        full = str(project).replace("github.com/", "", 1)
        res = sources.github_repo_top_level(full)
        if res.get("error"):
            return None
        return any(str(e).lower() == tool.lower() or
                   str(e).lower().startswith(tool.lower() + "-") or
                   str(e).lower().startswith(tool.lower() + "_")
                   for e in res.get("entries", []))

    ident = identity.resolve_identity(
        n["name"], task, named_cits, registry_hits=reg_hits,
        github_hits=gh, extra_evidence=extra_ev,
        component_probe=(_probe if budget is not None else None),
        budget=budget, cache=icache)
    attr_block = identity.blocks_attribution(ident)
    # attribution filter: project-scoped claims (version, license,
    # availability) may only ride on records that BOTH survived the
    # identity verdict and agree with the task's domain - a name-exact
    # record for a different project is excluded (its existence stays
    # visible in identity.considered)
    def _attr(hits):
        if attr_block:
            return []
        return [r for r in hits if _record_agrees_domain(task, r)]
    gh_a, py_a = _attr(gh), _attr(py)
    bc_a, bt_a = _attr(bc), _attr(bt)

    # ---- 4.2 version verification chain - the repo and registry
    # records feeding it are chosen ONLY among attributable ones, so
    # a colliding package's version can never become this candidate's
    # "latest stable" ----
    vchain = versions.resolve(
        n["name"], repo=(gh_a[0]["id"] if gh_a else None),
        citations=named_cits,
        probes={"pypi": lambda _s: (py_a, e_py, {})},
        budget=budget, cache=vcache,
        # root-cause 1.4: this deployment installs from bioconda, so a
        # baseline tool's "stable" is its DEPLOYMENT-CHANNEL version -
        # another distribution's number must not become the verdict
        prefer=("bioconda" if n.get("baseline") else None))
    stable_val = (vchain["values"].get("latest_stable") or {}).get("value")
    if stable_val and str(stable_val).startswith("commit"):
        stable_val = None  # a dev commit is not a stable to compare to

    # ---- S6 + S7: per-document claim extraction (grammar with exact
    # spans) followed by an INDEPENDENT verification pass re-parsing
    # those spans; only surviving measurements become evidence rows
    # (PART 1.5 fix: the PureCN 16.9/95.5-class misbinding is caught
    # by the verifier, and corrections/rejections are recorded) ----
    verified, vlog = _verified_measurements(n["name"], docs,
                                            known_names)
    ev = _evidence_rows(docs, verified)
    # cross-version guard: a number never transfers across versions
    for r in ev:
        if r.get("tested_version") or r.get("metric"):
            r["version_caveat"] = evidence.cross_version_caveat(
                r, stable_val)
    perf_summary = evidence.comparability_note(ev)
    # 4.4 what KIND of artifact is this? - classification inputs
    # prefer records attributable to the resolved project
    _g1 = ((gh_a or gh)[:1] or [{}])[0]
    _b1 = ((bt_a or bt)[:1] or [{}])[0]
    _p1 = ((py_a or py)[:1] or [{}])[0]
    cls = versions.classify(
        [str(_g1.get("description") or ""),
         str(_b1.get("summary") or ""),
         str(_p1.get("summary") or "")],
        archived=bool(_g1.get("archived")))
    # ---- S4 relevance by POSITIVE evidence only (assess_relevance).
    # The screened text is EVERYTHING retrieved about this candidate.
    # The M6 self-reference guard is kept: a literature-extracted name
    # WITHOUT an intro cue gets citation abstracts removed from the
    # relevance documents (an abstract that introduced a different
    # tool is not evidence about this one - the DQA101/CoV-2
    # fixture), while they still feed claim extraction. Absence of
    # vocabulary leaves the row UNRESOLVED with follow-up queries -
    # never "irrelevant" (PART 1.1 fix). ----
    lit_origin = (n.get("origin") or "").startswith(
        "named in retrieved literature")
    self_ref_only = (lit_origin and not n.get("method_context"))
    s4_docs = [d for d in docs
               if not (self_ref_only and
                       d.get("kind") == "citation-abstract")]
    rd = None
    if n.get("baseline"):
        elig = {"eligibility": "current baseline (not an alternative)",
                "reasons": [n["origin"]],
                "comparable_for_replacement": False,
                "confidence": "n/a"}
        relationship = "baseline"
        rstate = {"research_state": "n/a (current baseline tool)",
                  "state_transitions": [],
                  "research_states_remaining":
                      list(science.RESEARCH_STATES), "followups": []}
    else:
        rd = science.assess_relevance(
            n["name"], s4_docs, task=task, task_id=tid,
            identity=ident, artifact_class=cls["class"],
            lit_introduced=bool(n.get("method_context")),
            availability={"GitHub": gh_a, "PyPI": py_a,
                          "bio.tools": bt_a, "bioconda": bc_a},
            verified_claims=len(verified),
            baseline_name=(list(baseline_names) or [""])[0])
        relationship = rd["relationship"]
        elig = {"eligibility": RELATIONSHIP_ELIGIBILITY.get(
                    relationship,
                    "insufficient information to classify"),
                "reasons": rd["reasons"],
                "comparable_for_replacement":
                    rd["comparable_for_replacement"],
                "confidence": rd["confidence"]}
        rstate = rd
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
    # never None and never an empty cell. M8: the records feeding the
    # claim are the ATTRIBUTABLE ones only - a name-colliding package
    # or an unresolved identity carries no version/license string
    # (PART 1.3/1.4 fix; raw queries stay in version_chain and
    # verification_hits as provenance, not as claims).
    verified_version = vchain["display"]
    license_ = (gh_a[0].get("license") if gh_a and
                gh_a[0].get("license") not in (None, "NOASSERTION") else
                (py_a[0].get("license") if py_a else
                 (bc_a[0].get("license") if bc_a else
                  (bt_a[0].get("license") if bt_a else
                   "Not verified"))))
    if attr_block and not n.get("baseline"):
        verified_version = (f"Not verified - {attr_block} (raw "
                            "registry queries retained in "
                            "version_chain/verification_hits)")
        license_ = "Not verified - " + attr_block
    version_claimed = (verified_version if verified_version and
                       not str(verified_version).startswith(
                           "Not verified") else None)
    license_claimed = (license_ if license_ and
                       not str(license_).startswith("Not verified")
                       else None)
    maintenance = "Not verified"
    if gh_a:
        pushed = str(gh_a[0].get("pushed_at") or "")[:10]
        if gh_a[0].get("archived"):
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
    if attr_block and not n.get("baseline"):
        gaps.append(f"identity: {attr_block}")
    if relationship == "unresolved":
        gaps.append("relevance: no positive evidence of relevance or "
                    "irrelevance retrieved yet - follow-up queries "
                    "recorded, investigation stays open")
    return {
        "name": n["name"],
        "eligibility": elig["eligibility"],
        "eligibility_reasons": elig["reasons"],
        "comparable_for_replacement": elig["comparable_for_replacement"],
        "screen_confidence": elig["confidence"],
        # ---- M8 scientific-stage outputs (S3/S4/S6/S7) ----
        "relationship": relationship,
        "identity": ident,
        "identity_status": ident["status"],
        "relevance_basis": (rd or {}).get("basis") or [],
        "research_state": rstate["research_state"],
        "state_transitions": rstate.get("state_transitions") or [],
        "research_states_remaining":
            rstate.get("research_states_remaining") or [],
        "followups": rstate.get("followups") or [],
        "followups_open": bool(rstate.get("followups")),
        "version_claimed": version_claimed,
        "license_claimed": license_claimed,
        "rejected_measurements_used": 0,
        "measurement_verification": _mv_summary(verified, vlog),
        "method_context": bool(n.get("method_context")),
        "is_baseline": bool(n.get("baseline")),
        "baseline_evidence": n.get("baseline_evidence"),
        "verified_version": verified_version,
        "version_chain": vchain,
        "installed_version": installed,
        "classification": cls,
        "task": target["label"],
        "role_in_neoag": n["origin"],
        "category": ("software (registry-verified)"
                     if (gh_a or py_a or bt_a or bc_a) else
                     ("software (name-exact registry records exist; "
                      "identity unresolved)"
                      if (gh or py or bt or bc) else
                      "mentioned in literature only")),
        "artifact_class": cls["class"],
        "io_formats": "Not verified",
        "dependencies": "Not verified",
        "availability": (
            "found on " + ", ".join(
                s for s, ok in (("GitHub", gh_a), ("PyPI", py_a),
                                ("bio.tools", bt_a), ("bioconda", bc_a))
                if ok) if (gh_a or py_a or bt_a or bc_a) else
            (f"Not verified - {attr_block}" if attr_block else
             ("Not verified - name-exact registry records exist but "
              "none is attributable to a corroborated project (see "
              "identity.considered)"
              if (gh or py or bt or bc) else "Not verified"))),
        "license": license_ or "Not verified",
        "latest_release": ((vchain["values"].get("latest_release")
                            or {}).get("value")
                           or (py_a[0].get("version") if py_a else None)
                           or (bc_a[0].get("version") if bc_a else None)
                           or "Not verified"),
        "maintenance": maintenance,
        "primary_references": [e["citation"] for e in ev][:4] or
        "Not verified (no retrieved citation naming this tool)",
        "published_benchmark_evidence": ev,
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
        # run-scope only (popped by the caller before the row is
        # stored in the report): documents the deepening pass may
        # grow without re-fetching
        "_docs": docs,
    }


def _deepen_candidate(c, target, citations, per_source, failures,
                      queries_log, done, cid_keys, store, publish, rid,
                      budget=None, fb=None, task=None, prof=None,
                      known_names=(), baseline_names=(),
                      docs_by_name=None, lit_records=None):
    """M8 investigation loop: execute one row's RECORDED follow-up
    queries (bounded per run). New literature naming the candidate
    grows its document set; claims are re-extracted and RE-VERIFIED;
    the relevance verdict and research-state ladder are re-derived
    from the grown evidence. Honest accounting: executed, duplicate
    and budget-skipped queries all land in provenance and on the
    row - a skip is recorded, never silently dropped."""
    tid = target["id"]
    task = task or ontology.task_for(tid)
    name = c["name"]
    key_l = name.lower()
    still_open, executed, new_recs_all = [], [], []

    def _ingest(recs_):
        """Take one query's records into this candidate's citation
        set and (when the text names it) its document set. Shared by
        the execute path and the resume-restore path, so a resumed
        run sees the SAME grown evidence the original run grew."""
        for rec in recs_ or []:
            if not (rec.get("id") or rec.get("url")):
                continue
            cid = _canon_cid(rec)
            if cid in cid_keys:
                continue
            cid_keys.add(cid)
            cc = {**rec, "targets": [tid], "cid": cid}
            citations.append(cc)
            new_recs_all.append(cc)
            blob = ((cc.get("title") or "") + " " +
                    (cc.get("abstract") or "")).lower()
            if key_l in blob:
                docs_g = (docs_by_name or {}).setdefault(key_l, [])
                text = (((cc.get("title") or "") + ". " +
                         (cc.get("abstract") or "")).strip())
                if text.strip(" ."):
                    docs_g.append({"doc_cid": cid,
                                   "kind": "citation-abstract",
                                   "text": text,
                                   "citation": {
                                       "title": cc.get("title"),
                                       "year": cc.get("year"),
                                       "url": cc.get("url"),
                                       "id": cc.get("id"),
                                       "source": cc.get("source")}})
    for q in c.get("followups") or []:
        src = q.get("source") or "europepmc"
        qtext = str(q.get("query") or "")
        qkey = f"{src}|{qtext}"
        if fb is not None and fb.get("n", 0) <= 0:
            still_open.append({**q, "status": "not executed - run "
                                            "follow-up budget "
                                            "exhausted"})
            continue
        if qkey in done:
            restored = (lit_records or {}).get(qkey) or []
            if restored:
                _ingest(restored)
                still_open.append({**q,
                                   "status": "already run - "
                                             "persisted records "
                                             "restored (resume)"})
            else:
                still_open.append({**q,
                                   "status": "identical query "
                                             "already run this run - "
                                             "no new records"})
            continue
        recs, err = [], None
        if src == "github":
            left = (budget or {}).get("core")
            if left is not None and left < 1:
                still_open.append({**q, "status": "not executed - "
                                             "GitHub budget exhausted "
                                             "this hour"})
                continue
            term = (qtext.split('"')[1] if '"' in qtext
                    else (qtext.replace("official implementation", "")
                          .strip() or name))
            recs, err, _ = sources.github_repo_search(term, 3)
            if err is None and budget is not None and left is not None:
                budget["core"] = left - 1
        else:
            adapter = sources.ALL_ADAPTERS.get(src)
            if adapter is None:
                still_open.append({**q,
                                   "status": f"source {src!r} not "
                                             f"available this run"})
                continue
            try:
                recs, err, _ = adapter(qtext, 5)
            except Exception as e:
                recs, err = [], f"{type(e).__name__}: {str(e)[:120]}"
        _record(per_source, failures, queries_log, src,
                f"followup:{qtext[:70]}", len(recs), err, tid,
                purpose="investigation follow-up")
        executed.append({"query": qtext, "source": src,
                         "records": len(recs or []), "error": err})
        if err is None:
            done.add(qkey)
            if lit_records is not None:
                lit_records[qkey] = recs or []
            if fb is not None:
                fb["n"] = fb.get("n", 0) - 1
        _ingest(recs)
    # did NEW literature introduce this name as a method? (the cue
    # grammar decides, not a count)
    mi = {}
    if prof and new_recs_all:
        try:
            mi = {m["name"].lower(): bool(m.get("method_context"))
                  for m in science.extract_tool_mentions(new_recs_all,
                                                         prof)}
        except Exception:
            mi = {}
    lit_now = (bool(c.get("method_context")) or
               bool(mi.get(key_l)))
    # re-extract + re-verify over the GROWN document set
    docs = (docs_by_name or {}).get(key_l) or []
    verified, vlog = _verified_measurements(name, docs, known_names)
    prev_ident = c.get("identity") or {}
    if prev_ident.get("status") == "resolved":
        ident = prev_ident
    else:
        named_now = [x for x in citations
                     if key_l in ((x.get("title") or "") + " " +
                                  (x.get("abstract") or "")).lower()]
        extra_now = [{"axis": "naming-paper", "source": "literature",
                      "detail": f"paper title names it: "
                                f"{str(x.get('title') or '')[:80]} "
                                f"({x.get('id') or x.get('cid')})"}
                     for x in named_now[:4]
                     if key_l in str(x.get("title") or "").lower()]
        id_lit = identity.resolve_identity(
            name, task, named_now, extra_evidence=extra_now,
            component_probe=None, budget=None, cache=None)
        ident = (id_lit if (id_lit.get("n_axes") or 0) >=
                 (prev_ident.get("n_axes") or 0)
                 else (prev_ident or id_lit))
    lit_origin = (c.get("role_in_neoag") or "").startswith(
        "named in retrieved literature")
    s4_docs = [d for d in docs
               if not (lit_origin and not lit_now and
                       d.get("kind") == "citation-abstract")]
    rd = science.assess_relevance(
        name, s4_docs, task=task, task_id=tid, identity=ident,
        artifact_class=c.get("artifact_class") or "",
        lit_introduced=lit_now,
        availability={k: bool(c.get("verification_hits", {})
                              .get(v))
                      for k, v in (("GitHub", "github"),
                                   ("PyPI", "pypi"),
                                   ("bio.tools", "biotools"),
                                   ("bioconda", "bioconda"))},
        verified_claims=len(verified),
        baseline_name=(list(baseline_names) or [""])[0])
    c["relationship"] = rd["relationship"]
    c["eligibility"] = RELATIONSHIP_ELIGIBILITY.get(
        rd["relationship"], "insufficient information to classify")
    c["eligibility_reasons"] = rd["reasons"]
    c["comparable_for_replacement"] = rd["comparable_for_replacement"]
    c["screen_confidence"] = rd["confidence"]
    c["relevance_basis"] = rd["basis"]
    c["research_state"] = rd["research_state"]
    c["state_transitions"] = rd["state_transitions"]
    c["research_states_remaining"] = rd["research_states_remaining"]
    c["followups"] = still_open + list(rd["followups"])
    c["followups_open"] = bool(c["followups"])
    c["method_context"] = lit_now
    c["identity"] = ident
    c["identity_status"] = ident.get("status", "not attempted")
    if ident.get("status") == "resolved":
        c["evidence_gaps"] = [g for g in (c.get("evidence_gaps") or [])
                              if not str(g).startswith("identity")]
        if not c.get("version_claimed"):
            c["deepening_note"] = (
                "identity resolved during deepening; the version "
                "chain was NOT re-run (no budget spent) - version "
                "stays 'Not verified' until a re-run")
    c["published_benchmark_evidence"] = _evidence_rows(docs, verified)
    c["measurement_verification"] = _mv_summary(verified, vlog)
    c["deepening"] = executed
    _step(store, publish, rid,
          f"[{tid}] {name}: deepening executed {len(executed)} "
          f"follow-up query(ies), {len(new_recs_all)} new records -> "
          f"{c['eligibility']} ({c['research_state']})",
          store.research_progress(rid),
          {"done_queries": sorted(done),
           "lit_records": lit_records or {}})


def _record(per_source, failures, queries_log, source, query, count, err,
            tid, skip_ok_error=None, purpose="candidate verification"):
    per_source.setdefault(source, {"ok": 0, "fail": 0})
    entry = {"source": source, "query": query, "target": tid,
             "purpose": purpose, "count": count,
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
# M6 A6/A7 within-task comparison; M8 re-implements the evidence feed on
# the verified-measurement pipeline (S6/S7) + comparability classes (S8)
# ---------------------------------------------------------------------------

def _fmt_mrow(r):
    return (f"{r.get('metric')}={r.get('value')}"
            + (f" (CI {r['ci']})" if r.get("ci") else "")
            + ("" if r.get("context_established")
               else " [conditions not stated]")
            + f" [{r.get('doc_cid')}]")


def _task_comparisons(resolved_targets, candidates, stages_by_id,
                      decisions=None, baseline_ctx=None):
    """One comparison block PER scientific module (M6 B2 section 3).
    Comparison rows contain ONLY direct alternatives; complementary
    methods, wrappers and unclassified hits are named WITH their reason
    but never dressed up as comparison rows. Rows show the VERIFIED
    measurements (S6/S7) and the best comparability class of a baseline
    pair (S8) - an unverified quote never appears as data."""
    out = []
    for t in resolved_targets:
        tid = t["id"]
        stage = stages_by_id.get(tid) or {}
        base = stage.get("baseline") or {}
        prof = science.profile_for(tid, t["label"])
        comps = ((decisions or {}).get(tid) or {}).get("comparisons") or []
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
                meas = [r for r in c["published_benchmark_evidence"] or []
                        if isinstance(r, dict) and r.get("metric")
                        and r.get("verified") in ("confirm", "correct")]
                rank = {"direct": 3, "indirect": 2, "descriptive": 1,
                        "unverified": 0}
                best = max((x for x in comps
                            if x.get("candidate") == c["name"]),
                           key=lambda x: rank.get(x.get("class"), -1),
                           default=None)
                rows.append({
                    "tool": c["name"],
                    "version": c["verified_version"],
                    "identity": c.get("identity_status",
                                      "not attempted"),
                    "research_state": c.get("research_state"),
                    "evidence": (
                        "; ".join(_fmt_mrow(r) for r in meas[:3])
                        if meas else
                        "relevant method; no measurement claim survived "
                        "the independent verification pass for it"),
                    "comparability": (
                        f"best pair vs {best.get('baseline_name')}: "
                        f"{best.get('class')} - "
                        f"{str(best.get('note'))[:140]}"
                        if best else
                        "no baseline measurement shares this "
                        "candidate's metric"),
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
                "context": (baseline_ctx or {}).get(tid),
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
    """M8 S8+S9: the module's stage outputs assembled into comparability
    pairs and the five-label synthesis. The recommendation is a PURE
    function of the facts (label_facts); the recommendation gate
    re-derives the label from the very same dict, so a stored label can
    never drift from the evidence."""
    tid = target["id"]
    task = ontology.task_for(tid)
    base = stage.get("baseline") or {}
    mine = [c for c in candidates if c["task"] == target["label"]]
    baseline_cands = [c for c in mine if c.get("is_baseline")]
    direct = [c for c in mine
              if c.get("relationship") == "direct-alternative"]

    def _meas(c):
        # comparison-grade measurements: survived the independent
        # verification pass AND (for alternatives) sit on a resolved
        # project identity - a contested name's numbers could belong
        # to either project, so they enter no comparison
        if not c.get("is_baseline") and \
                c.get("identity_status") != "resolved":
            return []
        return [r for r in c.get("published_benchmark_evidence") or []
                if isinstance(r, dict) and r.get("metric")
                and r.get("verified") in ("confirm", "correct")]

    base_meas = [(c["name"], r) for c in baseline_cands
                 for r in _meas(c)]
    comparisons = []
    for c in direct:
        for r in _meas(c):
            pair = next(
                ((bn, br) for bn, br in base_meas
                 if str(br.get("metric") or "").lower()
                 == str(r.get("metric") or "").lower()), None)
            if pair is None:
                continue
            cmp_ = science.compare_measurements(
                {"metric": pair[1].get("metric"),
                 "value": pair[1].get("value"), "verified": True,
                 "context": pair[1].get("context") or {},
                 "name": pair[0]},
                {"metric": r.get("metric"), "value": r.get("value"),
                 "verified": True, "context": r.get("context") or {},
                 "name": c["name"]})
            cmp_["candidate"] = c["name"]
            cmp_["baseline_name"] = pair[0]
            cmp_["values"] = {"baseline": pair[1].get("value"),
                              "alternative": r.get("value")}
            comparisons.append(cmp_)

    def _flag(name, pred):
        return any(x.get("candidate") == name and pred(x)
                   for x in comparisons)

    gate_rows = [{
        "name": c["name"],
        "is_baseline": bool(c.get("is_baseline")),
        "relationship": c.get("relationship") or "unresolved",
        "basis": c.get("relevance_basis") or [],
        "reasons": c.get("eligibility_reasons") or [],
        "identity_status": c.get("identity_status", "not attempted"),
        "version_claimed": c.get("version_claimed"),
        "license_claimed": c.get("license_claimed"),
        "rejected_measurements_used": 0,
        "superiority_claim": _flag(
            c["name"], lambda x: x.get("winner") == "alternative"),
        "direct_verified_pair": _flag(
            c["name"], lambda x: x.get("class") == "direct"),
        "followups_open": bool(c.get("followups_open")),
    } for c in mine]
    relevance_rows = [{
        "name": c["name"],
        "relationship": c.get("relationship") or "unresolved",
        "followups_open": bool(c.get("followups_open")),
    } for c in mine if not c.get("is_baseline")]
    qtys = ontology.quantities(task) or []
    label_facts = {
        "baseline_identified": bool(base.get("identified")),
        "coverage_partial": not reached_fully,
        "relevance_rows": relevance_rows,
        "comparisons": comparisons,
        "endpoint_defined": bool(qtys),
        "endpoint": ", ".join(qtys)}
    prov = science.recommend_v2(**label_facts)
    base_ev = "; ".join(str(e) for b in (base.get("tools") or [])
                        for e in (b.get("evidence") or []))[:300]
    return {"recommendation": prov["label"],
            "conclusion": prov["label"],  # legacy key
            "rationale": prov["rationale"],
            "action": prov["action"],
            "conditions_met": prov["conditions_met"],
            "conditions_unmet": prov["conditions_unmet"],
            "label_facts": label_facts,
            "comparisons": comparisons,
            "gate_rows": gate_rows,
            "baseline_evidence": base_ev,
            "evidence_facts": {
                "candidates": len(mine),
                "direct_alternatives": len(direct),
                "verified_measurement_pairs": len(comparisons),
                "direct_comparable_pairs": len(
                    [x for x in comparisons if x.get("class")
                     == "direct"]),
                "open_followups": len(
                    [r for r in relevance_rows if r["followups_open"]]),
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
    """M6 B2 section 4 (labels updated to the M8 five-label vocabulary
    by recommend_v2): actions in decision-priority order, each tied to
    ONE module decision. A benchmark action exists only where
    recommend_v2 derived one - the module has relevant alternatives, a
    defined endpoint and no verified comparable numbers - never for
    irrelevant or unclassified tools."""
    prio = {"Promising candidate for further evaluation": 0,
            "Recommend targeted benchmark": 1,
            "Review incomplete": 2,
            "No justified change identified": 3,
            "Evidence supports retaining current method": 4}
    acts = []
    for tid, mc in module_conclusions.items():
        rec = mc.get("recommendation") or mc.get("conclusion") or ""
        if rec == "Evidence supports retaining current method":
            acts.append((4, f"[{tid}] Keep current method - "
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
    # ---- M8 S5 baseline context per module: conda pins, installed
    # versions (read-only deployment probe) and intended-vs-achieved
    # consensus kept as SEPARATE fields - a literature review never
    # claims execution happened. ----
    baseline_ctx = {}
    try:
        pins = baselines.repo_conda_pins(Path(inv.get("repo") or "/"))
    except Exception:
        pins = {}
    _probe_cache = {}
    for t in reached:
        stg = stages_by_id.get(t["id"]) or {}
        for b in (stg.get("baseline") or {}).get("tools") or []:
            bn = str(b["name"]).lower()
            if bn not in _probe_cache and not bn.startswith(
                    "neoag internal"):
                try:
                    _probe_cache[bn] = baselines.probe_installed(
                        b["name"])
                except Exception as e:
                    _probe_cache[bn] = {
                        "reason": f"probe failed: {type(e).__name__}"}
        try:
            baseline_ctx[t["id"]] = {
                "levels": baselines.level_for_stage(stg, pins,
                                                    _probe_cache),
                "consensus": baselines.consensus_context(stg)}
        except Exception as e:
            baseline_ctx[t["id"]] = {
                "error": f"baseline context unavailable: "
                         f"{type(e).__name__}"}
    # ---- M8 S9: module synthesis + per-module SCIENTIFIC gates.
    # Pass 1 derives the label from the stage facts; the gates then
    # check all eight dimensions INCLUDING label consistency. If any
    # non-recommendation gate fails, the label is re-synthesised with
    # those failures as input ("Review incomplete") and the gates are
    # re-run against the final label so pass 2 is the authoritative
    # record. No constant True anywhere. ----
    module_conclusions = {}
    gates_flat = []
    for t in reached:
        tid = t["id"]
        mc = _module_decision(t, candidates,
                              stages_by_id.get(tid) or {},
                              reached_fully=(tid not in
                                             hard_fail_targets))
        tqs = [q for q in (prog.get("queries") or [])
               if q.get("target") == tid]
        t_cids = [(c.get("cid") or _canon_cid(c)) for c in citations
                  if tid in (c.get("targets") or [])]
        state = {
            "target": tid,
            "baseline": {
                "identified": mc["label_facts"]["baseline_identified"],
                "evidence": mc["baseline_evidence"]},
            "candidates": mc["gate_rows"],
            "coverage": {
                "attempted": len(tqs),
                "accounted": len([q for q in tqs
                                  if "count" in q or "error" in q]),
                "outages": len([q for q in tqs if q.get("error")
                                and not _recorded_outcome(q)])},
            "citation_ids": t_cids,
            "citation_ids_unique": len(set(t_cids)),
            "engine_version": LIVE_ENGINE_VERSION,
            "label_proposed": mc["recommendation"],
            "label_facts": mc["label_facts"]}
        g = science.scientific_gates(state)
        failing = [x for x in g if not x["passed"]
                   and x["dimension"] != "recommendation"]
        if failing:
            facts2 = dict(mc["label_facts"],
                          gate_failures=[
                              f"{x['dimension']}: "
                              + ("; ".join(str(v) for v in
                                           x["violations"])
                                 if x["violations"]
                                 else str(x["detail"])[:90])
                              for x in failing])
            final = science.recommend_v2(**facts2)
            mc.update(recommendation=final["label"],
                      conclusion=final["label"],
                      rationale=final["rationale"],
                      action=final["action"],
                      conditions_met=final["conditions_met"],
                      conditions_unmet=final["conditions_unmet"])
            mc["label_facts"] = facts2
            g = science.scientific_gates({**state,
                                          "label_proposed":
                                              final["label"],
                                          "label_facts": facts2})
        mc["gates"] = [{**x, "target": tid, "check": x["dimension"]}
                       for x in g]
        gates_flat.extend(mc["gates"])
        module_conclusions[tid] = mc
    task_comparisons = _task_comparisons(reached, candidates,
                                         stages_by_id,
                                         decisions=module_conclusions,
                                         baseline_ctx=baseline_ctx)
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
    # ---- M8: decision-ready ONLY when every scientific gate actually
    # passed (per-module, flattened here); the old constant-True
    # quality gates are gone ----
    in_run_gaps = [m for m in coverage["not_researched"]
                   if "scope" not in m["reason"]]
    # A module whose own verdict is "Review incomplete" says the
    # evaluation could not be completed - that is NOT a decision-ready
    # state however many gates pass (non-negotiable 6: absence of
    # evidence is never decision-readiness; measured 2026-11-17:
    # run res-20261117-190518 shipped decision_ready=true with the
    # purity_cnv module labelled "Review incomplete").
    _incomplete_modules = [
        t for t, m in module_conclusions.items()
        if m.get("recommendation") == "Review incomplete"]
    ready = (science.decision_ready_v2(gates_flat) and not in_run_gaps
             and not hard_failures and not _incomplete_modules)
    rate_gaps = [f for f in failures if _rate_only(f.get("error"))]
    # ---- M6 B1: conclusion-first assessment (one minute to read) ----
    by_rec = Counter(m["recommendation"]
                     for m in module_conclusions.values())
    adv = {k: v for k, v in by_rec.items()
           if k in ("Promising candidate for further evaluation",
                    "Recommend targeted benchmark")}
    if adv.get("Promising candidate for further evaluation"):
        headline = ("Independently verified, directly comparable "
                    "published evidence favours at least one "
                    "candidate - adoption still requires validation "
                    "on this deployment (see module table).")
    elif adv:
        headline = ("NO tool replacement is justified today. "
                    + ", ".join(f"{v}x {k}" for k, v in adv.items())
                    + f" - relevant candidates exist for {len(adv)} "
                      "module(s) but no verified comparable numbers "
                      "were retrieved. The current tools stay in "
                      "place.")
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
        if mc["recommendation"] not in (
            "No justified change identified",
            "Evidence supports retaining current method")][:6]
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
        # M8: per-module eight-dimension scientific gates, flattened;
        # each row keeps its module + a "check" alias of the dimension
        # for existing renderers
        "quality_gates": gates_flat,
        "gate_dimensions": science.GATE_DIMENSIONS,
        "recommendation_vocabulary": science.RECOMMENDATION_LABELS_V2,
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
                 f"{len(c['published_benchmark_evidence'])} "
                 f"document row(s), "
                 f"{(c.get('measurement_verification') or {}).get('confirmed', 0)}"
                 f" confirmed / "
                 f"{(c.get('measurement_verification') or {}).get('corrected', 0)}"
                 f" corrected / "
                 f"{(c.get('measurement_verification') or {}).get('rejected', 0)}"
                 f" rejected measurement(s)"),
             "performance_summary": c["performance_summary"],
             "independently_validated": c["independent_validation"],
             "evidence_gaps": "; ".join(c["evidence_gaps"])}
            for c in candidates],
        "literature_citations": [
            {"title": c["title"], "year": c.get("year"),
             "url": c.get("url"), "source": c.get("source"),
             "id": c.get("id"), "cid": c.get("cid"),
             "targets": c.get("targets")}
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
            == "Recommend targeted benchmark"},
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
                                  "candidate then passes the M8 chain - "
                                  "identity resolution (>=2 corroborating "
                                  "axes, never merged on a name), "
                                  "relevance by POSITIVE evidence only "
                                  "(keyword absence never means "
                                  "irrelevance), per-document claim "
                                  "extraction with exact spans, an "
                                  "INDEPENDENT verification pass, and "
                                  "comparability classification before "
                                  "any pair may support a decision; "
                                  "citations deduped by DOI/PMCID then "
                                  "normalised title",
            "citation_id_scheme": "DOI > PMCID > PMID > arXiv > record "
                                  "id > normalised title (canonical cid "
                                  "on every citation)",
            "failures": failures},
        "methodology": (
            "M8 scientific workflow (docs/SCIENTIFIC_ARCHITECTURE_"
            "2026-11-17.md): S1 scope + code-grounded baseline + "
            "question-driven retrieval -> S2 task ontology (what each "
            "module's endpoint quantities are) -> S3 IDENTITY "
            "RESOLUTION per candidate name (>=2 corroborating evidence "
            "axes; contested/unresolved names carry no version/"
            "license/performance claim) -> S4 RELEVANCE by positive "
            "evidence only, with a research-state ladder and recorded "
            "follow-up queries; a bounded deepening pass EXECUTES "
            "those follow-ups within the run budget -> S5 baseline "
            "levels (declared pins, installed probe, intended-vs-"
            "achieved consensus as separate fields) -> S6 per-document "
            "claim extraction (grammar, exact spans, CI/range/p-value "
            "masking) -> S7 INDEPENDENT verification pass over the "
            "same spans (confirm / correct / reject) -> S8 "
            "COMPARABILITY classes (direct requires same metric + "
            "matched documented conditions incl. modality; indirect "
            "never licenses superiority) -> S9 five-label synthesis "
            "as a pure function of the stage facts + eight scientific "
            "gate dimensions (identity, relevance, baseline, evidence, "
            "comparability, recommendation consistency, coverage, "
            "provenance integrity); DECISION-READY only when every one "
            "actually passes. Full query log persisted in provenance."
            "queries."),
        "limitations": [
            "abstracts/metadata are the evidence floor; full texts were "
            "attempted ONLY for open-access PMC deposits via efetch "
            "(Europe PMC's fullTextXML sub-endpoint is unreachable from "
            "this host - measured 2026-11-16); every metric shown was "
            "re-parsed independently from its quoted span - rejected "
            "attributions are recorded but enter NO comparison, and a "
            "DIRECT pair additionally requires matched documented "
            "conditions (silence about sequencing modality alone keeps "
            "a pair indirect)",
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
