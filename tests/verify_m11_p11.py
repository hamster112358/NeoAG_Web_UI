#!/usr/bin/env python3
"""M11 P1-11 battery: Discovery science - interpretation before retrieval.

Regression pinned (live record res-20261119-153059-9e95ab): the Chinese
question "POLYSOLVER和sovalign的可靠替代" was embedded VERBATIM in all 5
queries; zero records came back; the headline still declared "NO tool
replacement is justified for any researched module" while
decision_ready=false and the module verdict said "baseline missing".

This battery is fully DETERMINISTIC (no network, no server): the intent
layer runs against a fixture inventory + fixture declared-registry, and
run_live runs with faked source adapters. One host-reality check
(read-only) confirms the same resolution against THIS deployment's
native config, skipped when the repo is absent.

Pinned behaviour:
  * the mixed-language question is interpreted into ENGLISH query terms
    with per-tool separation; NO query carries a non-ASCII character
    or the verbatim label;
  * names resolve against the native configuration (configured >
    declared > unverified); an unverified name is kept AS WRITTEN with
    a disambiguation note and never silently mapped onto a similar
    tool;
  * a natively-resolved name grounds the module's baseline (native
    config = declared evidence), an unverified one is labelled
    UNVERIFIED in the baseline note;
  * zero results => ONE bounded broadening pass, then an honest
    "Research could not be completed; no comparison was possible"
    headline - NEVER "NO tool replacement is justified";
  * adequacy gates over nothing report N/A (passed=None), never a
    vacuous PASS; integrity gates stay boolean; N/A is never
    decision-ready;
  * a run with no derivable English term records the SKIP (the label
    is never echoed as a query);
  * licensing vocabulary ladder classes; "none-declared" is NOT a
    licence statement;
  * the GitHub core budget gates the releases probe as a recorded
    planned skip (no burned 403, no silent null);
  * GENERALIZATION without any POLYSOLVER keyword in backend code
    (grep-enforced) across HLA / CNV / fusion / splicing / presentation
    questions, Chinese and English.
Run: python3 tests/verify_m11_p11.py   (no port - offline)
"""
import atexit
import json
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# keep even the incidental import of backend.server (for publish) off
# the live database
os.environ["NEOAG_DEMO_DB"] = "m11p11.sqlite3"
os.environ["NEOAG_DEMO_PORT"] = "8783"


def _cleanup():
    for p in (ROOT / "data").glob("m11p11.sqlite3*"):
        try:
            p.unlink()
        except OSError:
            pass
    shutil.rmtree(ROOT / "data" / "exports-m11p11", ignore_errors=True)
    shutil.rmtree(ROOT / "data" / "attachments-m11p11",
                  ignore_errors=True)


atexit.register(_cleanup)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS" if ok else "FAIL") + f"  {name}"
          + (f"  [{detail}]" if detail and not ok else ""))


# ---------------------------------------------------------------------------
# fixtures: a mini native configuration (stages + declared registry).
# The fixture KEYS mirror the real registry only in kind; all matching
# logic is name-independent (no tool names exist in backend/ - check E1).
# ---------------------------------------------------------------------------
STAGES = [
    {"id": "hla_typing", "stage": "HLA typing", "enabled": True,
     "baseline": {"identified": True, "consensus": False, "tools": [
         {"name": "optitype", "role": "current",
          "evidence": ["neoag.toml [stages.hla_typing]"]}]}},
    {"id": "purity_cnv", "stage": "Purity/CNV", "enabled": True,
     "baseline": {"identified": True, "consensus": False, "tools": [
         {"name": "facets", "role": "current",
          "evidence": ["neoag.toml [stages.purity_cnv]"]}]}},
    {"id": "fusion_discovery", "stage": "Fusion", "enabled": True,
     "baseline": {"identified": True, "consensus": False, "tools": [
         {"name": "arriba", "role": "current",
          "evidence": ["neoag.toml [stages.fusion]"]}]}},
    {"id": "splice_discovery", "stage": "Splicing", "enabled": True,
     "baseline": {"identified": True, "consensus": False, "tools": [
         {"name": "splicemutr", "role": "current",
          "evidence": ["neoag.toml [stages.splice]"]}]}},
    {"id": "presentation", "stage": "Presentation", "enabled": True,
     "baseline": {"identified": True, "consensus": False, "tools": [
         {"name": "netmhcpan", "role": "current",
          "evidence": ["neoag.toml [stages.presentation]"]}]}},
]
DECLARED = {
    "polysolver": {
        "description": "Optional HLA typing dependency used by LOHHLA "
                       "workflows.",
        "env": ["POLYSOLVER_HOME", "NOVOALIGN_LICENSE_FILE"],
        "kind": "hla_tool_dependency"},
    "optitype": {"description": "OptiType HLA class I typer.",
                 "kind": "hla_tool"},
    "facets": {"description": "Facets purity/CNV.", "kind": "cnv_tool"},
    "arriba": {"description": "Arriba fusion caller.",
               "kind": "rna_fusion_tool"},
}
FIX_INV = {"repo": "/nonexistent-fixture-repo", "code_verified": True,
           "git_commit": "0" * 10, "captured_at": "2026-11-20T00:00:00Z",
           "stages": STAGES, "limitations": ["fixture inventory"]}

from backend import intent as intent_lib  # noqa: E402
from backend import science  # noqa: E402

# ================= PART A: the interpretation layer ========================
QREG = "POLYSOLVER和sovalign的可靠替代"
it = intent_lib.analyze(QREG, stages=STAGES, declared=DECLARED)
by = {t["raw"]: t for t in it["tools"]}
check("A1 the regression question yields BOTH named tools, kept apart: "
      "POLYSOLVER resolves to the native DECLARED rung, sovalign stays "
      "UNVERIFIED (never silently conflated with anything)",
      set(by) == {"POLYSOLVER", "sovalign"}
      and by["POLYSOLVER"]["status"] == "declared"
      and by["sovalign"]["status"] == "unverified"
      and any("sovalign" in n.lower() and "unverified" in n.lower()
              for n in it["notes"]))
check("A2 the declared resolution carries the native description "
      "(modality comes from NATIVE data: HLA typing), not from a "
      "hardcoded keyword map",
      "hla_typing" in it["modalities"]
      and any("HLA" in d for t in it["tools"]
              for d in t["descriptions"]))
check("A3 facets detected bilingually (replacement + reliability from "
      "the Chinese wording)",
      "replacement" in it["facets"] and "reliability" in it["facets"])
GEN = [
    ("有没有比CNVkit更准确的纯度拷贝数估计工具", "purity_cnv"),
    ("有没有更好的HLA分型工具", "hla_typing"),
    ("fusion gene detection 有什么替代方法", "fusion"),
    ("检测剪接异构体有没有更可靠的工具", "splicing"),
    ("MHC结合力预测用什么工具比现用的更可靠", "presentation"),
    ("Are there better alternatives to OptiType for HLA typing?",
     "hla_typing"),
]
# NOTE: modality keys are the GENERIC cue groups (hla_typing /
# purity_cnv / fusion / splicing / presentation) - not stage ids.
_badA4 = []
for q, want in GEN:
    g = intent_lib.analyze(q, stages=STAGES, declared=DECLARED)
    if want not in g["modalities"]:
        _badA4.append((q, g["modalities"]))
check("A4 generalization WITHOUT any tool keyword in the code: "
      "CNV/HLA/fusion/splicing/presentation questions resolve to their "
      "modality in Chinese AND English (fixture lexicons are generic "
      "science vocabulary)", not _badA4, str(_badA4)[:200])
check("A5 English tool names in English prose are extracted and "
      "resolved against native data (OptiType -> configured via the "
      "inventory stage)",
      any(t["raw"] == "OptiType" and t["status"] == "configured"
          for t in intent_lib.analyze(
              "Are there better alternatives to OptiType for HLA "
              "typing?", stages=STAGES, declared=DECLARED)["tools"]))
_near = intent_lib.analyze("facett的替代方案", stages=STAGES,
                           declared=DECLARED)
_nt = _near["tools"][0] if _near["tools"] else {}
check("A6 a misspelled near-miss is DISAMBIGUATED, not corrected: it "
      "stays unverified and the nearest declared name is recorded as a "
      "note only (a silent correction would be a fabricated identity)",
      _nt.get("status") == "unverified"
      and _nt.get("nearest", {}).get("name") == "facets"
      and any("NOT assumed to be the same" in n for n in _near["notes"]),
      str(_nt)[:200])
_no = intent_lib.analyze("帮我看看这个结果怎么处理", stages=STAGES,
                         declared=DECLARED)
check("A7 a question with no tool name and no modality vocabulary "
      "reports NO English signal (the caller must record an honest "
      "skip, not fabricate a query)",
      not _no["has_english_signal"])

# ================= PART B: query construction contract ======================
plan = science.literature_queries(science.intent_profile(it), QREG,
                                  intent=it)
check("B1 every query is pure ASCII - the mixed-language label never "
      "enters a query (regression: 5 verbatim Chinese queries)",
      plan and all(q["query"].isascii() for q in plan),
      str([q["query"] for q in plan if not q["query"].isascii()])[:150])
_pol = [q for q in plan if "POLYSOLVER" in q["query"]]
_sov = [q for q in plan if "sovalign" in q["query"]
        and "POLYSOLVER" not in q["query"]]
check("B2 per-tool separation: each named tool gets its OWN benchmark "
      "query; no blended 'POLYSOLVER和sovalign' string",
      _pol and _sov)
check("B3 the domain angle is queried in ENGLISH terms derived from "
      "the natively-resolved name (reviews/methods/benchmarks)",
      any(("review" in q["query"].lower() or "benchmark"
           in q["query"].lower())
          and "HLA" in q["query"] for q in plan))
check("B4 zero-result broadening is BOUNDED (<=2 queries) and drops "
      "every qualifier (names alone)",
      len(science.broaden_after_zero(it)) <= 2
      and all(q["query"].isascii()
              and " AND " not in q["query"]
              for q in science.broaden_after_zero(it)))
legacy = science.literature_queries(science.profile_for("purity_cnv"),
                                    "Purity/CNV")
check("B5 known-target behaviour unchanged (native vocabulary path "
      "intact - 4 queries, purity terms, no CJK)",
      len(legacy) == 4 and all(q["query"].isascii() for q in legacy)
      and "purity" in legacy[0]["query"].lower())
check("B6 the legacy fallback ALSO strips non-ASCII: a custom label "
      "with no ASCII vocabulary and no intent yields NO queries "
      "(never an echoed label)",
      science.literature_queries(
          science.profile_for("custom:x"), "纯中文标签问题",
          intent=None) == [])

# ================= PART C: E2E run_live replay, faked sources ===============
from backend import (baselines, research, sources, store as store_mod)  # noqa: E402
from backend import inventory as inventory_mod  # noqa: E402

# keep the true functions for Part E checks (the replay
# monkeypatches the modules)
_REAL_GH_SEARCH = sources.github_repo_search
_REAL_LOAD_INV = inventory_mod.load_inventory
_REAL_LOAD_DECLARED = intent_lib.load_declared


def _run_replay(label, recs=()):
    def adapter(q, n):
        return [dict(r) for r in recs], None, {}

    sources.ALL_ADAPTERS = {"europepmc": adapter, "pubmed": adapter,
                            "openalex": adapter}
    sources.github_repo_search = lambda t, n=3, **kw: ([], None, {})
    sources.github_rate_limit = lambda: {"core": 30,
                                         "reset_utc": None,
                                         "checked_at": "fixture"}
    sources.pypi_package = lambda s, n=1: ([], "not-on-pypi (fixture)",
                                           {})
    sources.biotools_search = lambda t, n=2: ([], "fixture", {})
    sources.anaconda_bioconda = lambda t, n=1: ([], "fixture", {})
    sources.bioconductor_package = lambda t, n=1: ([], "fixture", {})
    sources.github_releases_tags = lambda r: {
        "releases": [], "tags": [], "error": None}
    sources.github_version_file = lambda r: {}
    sources.github_head_commit = lambda r: {}
    sources.github_repo_top_level = lambda r: {"entries": [],
                                               "error": None}
    sources.pmc_fulltext = lambda p: (None, "fixture: no full text")
    inventory_mod.load_inventory = lambda: FIX_INV
    intent_lib.load_declared = lambda repo: DECLARED
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="m11p11"))
    try:
        st = store_mod.Store(tmp)
        rid = "res-fixture-1"
        tid = "custom:" + "".join(
            c if c.isalnum() else "-" for c in label)[:24]
        st.research_add(rid, "targeted", tid, engine="live",
                        targets=[tid])
        with st.lock:
            st.db.execute(
                "UPDATE research SET progress_json=? WHERE id=?",
                (json.dumps({"targets": [{"id": tid, "label": label,
                                          "custom": True}],
                             "steps": [], "done_queries": [],
                             "queries": [], "note": "queued"},
                            ensure_ascii=False), rid))
            st.db.commit()
        research.run_live(rid, "targeted", [tid], st, resume=False)
        row = st.research_get(rid)
        rep = row["report"]
        if isinstance(rep, str):
            rep = json.loads(rep)
        return row, rep
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


row1, rep1 = _run_replay(QREG)  # zero records from every source
qs = ((rep1.get("provenance") or {}).get("queries")) or []
real_qs = [q for q in qs if q.get("source") != "plan"]
check("C1 replay: queries ACTUALLY ASKED are all ASCII - the "
      "untranslated label is nowhere in provenance queries "
      f"({len(real_qs)} queries)",
      real_qs and all(q["query"].isascii() for q in real_qs)
      and not any(QREG in q["query"] for q in real_qs),
      str([q["query"] for q in real_qs if not q["query"].isascii()])[:150])
_pq = [q for q in real_qs if "POLYSOLVER" in q["query"]]
_sq = [q for q in real_qs if "sovalign" in q["query"]
       and "POLYSOLVER" not in q["query"]]
check("C2 replay: per-tool separation survives into provenance "
      "(separate POLYSOLVER and sovalign queries + domain queries)",
      _pq and _sq)
check("C3 replay: after the all-zero primary plan EXACTLY ONE bounded "
      "broadening pass ran (purpose names it) - then the run stops "
      "asking",
      1 <= len([q for q in real_qs
                if "broadening" in str(q.get("purpose", ""))]) <= 2)
tc = (rep1.get("scientific_comparison")
      or rep1.get("task_comparisons") or [{}])[0]
bl = tc.get("baseline") or {}
check("C4 replay: the baseline is grounded in the NATIVE CONFIG - "
      "POLYSOLVER enters the baseline block with registry evidence, "
      "and the baseline note marks sovalign UNVERIFIED (declared "
      "rung, never an executed-claim)",
      bl.get("identified")
      and any("POLYSOLVER" in t["name"] for t in bl.get("tools") or [])
      and all(t.get("evidence") for t in bl.get("tools") or [])
      and "sovalign" in (bl.get("note") or "")
      and "UNVERIFIED" in (bl.get("note") or ""))
mc = list((rep1.get("module_conclusions") or {}).values())
check("C5 replay: the module verdict is Research-incomplete class - "
      "NOT a recommendation",
      mc and all(m["recommendation"] == "Review incomplete"
                 for m in mc)
      and rep1.get("decision_ready") is False)
hl = (rep1.get("assessment") or {}).get("headline", "")
check("C6 replay: the headline ABSTAINS honestly and NEVER claims "
      "'NO tool replacement is justified' over an incomplete run "
      "(the regression's exact overclaim)",
      "could not be completed" in hl.lower()
      and "no tool replacement is justified" not in hl)
check("C7 replay: the executive summary carries the same honest "
      "headline (no second verdict channel)",
      any("could not be completed" in line.lower()
          for line in rep1.get("executive_summary") or [])
      and not any("NO tool replacement is justified" in line
                  for line in rep1.get("executive_summary") or []))
gmap = {g["dimension"]: g for g in rep1.get("quality_gates") or []}
check("C8 replay: adequacy gates over NOTHING report N/A (passed=None) "
      "- never a vacuous PASS (the regression showed identity/relevance/"
      "evidence/comparability 'passed' with zero records)",
      all(gmap[d]["passed"] is None for d in
          ("identity", "relevance", "evidence", "comparability"))
      and all(gmap[d]["class"] == "adequacy" for d in
              ("identity", "baseline", "relevance", "evidence",
               "comparability"))
      and all(gmap[d]["class"] == "integrity" for d in
              ("recommendation", "coverage", "provenance integrity")))
check("C9 replay: the integrity gates still report real booleans "
      "(coverage accounted for the zero-record queries incl. the "
      "broadening pass)",
      gmap["coverage"]["passed"] is True
      and gmap["provenance integrity"]["passed"] is True)
check("C10 replay: the report states its interpretation "
      "(question_interpretation: name statuses + notes) - the run "
      "shows what the question was taken to mean",
      bool((rep1.get("question_interpretation") or {}))
      and any(t["status"] == "unverified"
              for v in (rep1.get("question_interpretation") or {}).values()
              for t in v.get("tools") or []))
check("C11 replay: the licence ladder is stated with the findings",
      "ladder" in str(rep1.get("licence_vocabulary", "")).lower())

row2, rep2 = _run_replay("帮我看看这个结果怎么处理")  # no signal
qs2 = (rep2.get("provenance") or {}).get("queries") or []
check("C12 no-signal question: the SKIP is RECORDED (source 'plan', "
      "honest reason) and NOT ONE retrieval query fired - the label "
      "was never echoed",
      any(q.get("source") == "plan" and q.get("recorded_outcome")
          for q in qs2)
      and not [q for q in qs2 if q.get("source") != "plan"])
hl2 = (rep2.get("assessment") or {}).get("headline", "")
check("C13 no-signal question: same honest abstention headline, "
      "decision_ready false",
      ("could not be completed" in hl2.lower()
       or "nothing was researched" in hl2.lower())
      and rep2.get("decision_ready") is False, hl2[:120])

# ================= PART D: pure functions ==================================
gs = science.scientific_gates({
    "target": "custom:x",
    "baseline": {"identified": False, "evidence": ""},
    "candidates": [],
    "coverage": {"attempted": 3, "accounted": 3, "outages": 0},
    "citation_ids": [], "citation_ids_unique": 0,
    "engine_version": "v", "label_proposed": "Review incomplete",
    "label_facts": {"baseline_identified": False,
                    "retrieved_records": 0}})
gm = {g["dimension"]: g for g in gs}
check("D1 gates over zero candidates: baseline FAILS for real, the "
      "subjectless adequacy gates report N/A, integrity gates PASS "
      "honestly, decision_ready false",
      gm["baseline"]["passed"] is False
      and gm["identity"]["passed"] is None
      and gm["coverage"]["passed"] is True
      and not science.decision_ready_v2(gs))
_row = {"name": "A", "is_baseline": False,
        "relationship": "direct-alternative",
        "basis": [{"claim": "domain", "term": "purity"}],
        "reasons": [], "identity_status": "resolved",
        "version_claimed": "2.0", "license_claimed": "BSD",
        "rejected_measurements_used": 0, "superiority_claim": False,
        "direct_verified_pair": True, "followups_open": 0}
gs2 = science.scientific_gates({
    "target": "purity_cnv",
    "baseline": {"identified": True, "evidence": "pin"},
    "candidates": [dict(_row)],
    "coverage": {"attempted": 3, "accounted": 3, "outages": 0},
    "citation_ids": ["DOI:1"], "citation_ids_unique": 1,
    "engine_version": "v",
    "label_proposed": "No justified change identified",
    "label_facts": {"baseline_identified": True, "retrieved_records": 7,
                    "relevance_rows": [{"name": "A",
                                        "relationship":
                                            "direct-alternative"}]}})
gm2 = {g["dimension"]: g for g in gs2}
check("D2 with real rows the adequacy gates are BOOLEAN again (N/A is "
      "only for no-subject - no over-N/A)",
      all(gm2[d]["passed"] is not None for d in
          ("identity", "relevance", "evidence", "comparability")))
check("D3 recommend_v2: an empty literature feed can never yield "
      "'No justified change identified' - it yields Review "
      "incomplete; a fed review still can",
      science.recommend_v2(baseline_identified=True,
                           retrieved_records=0)["label"]
      == "Review incomplete"
      and science.recommend_v2(
          baseline_identified=True, retrieved_records=5,
          relevance_rows=[{"name": "X", "relationship": "unrelated",
                           "basis": [{"claim": "off-domain"}]}])
      ["label"] == "No justified change identified")
check("D4 headline law: the verdict sentence is licensed ONLY when no "
      "module is incomplete; all-incomplete abstains; mixed counts "
      "honestly",
      "could not be completed"
      in science.evaluation_headline(["Review incomplete"] * 3, 0)
      and "NO tool replacement is justified"
      not in science.evaluation_headline(
          ["Review incomplete", "No justified change identified"], 4)
      and "NO tool replacement is justified"
      in science.evaluation_headline(
          ["No justified change identified"], 40))
check("D5 licence ladder: 'none-declared'/NOASSERTION is NOT a licence "
      "statement; a registry SPDX label is declared-but-unverified",
      science.license_rung("none-declared")[0] == "not-stated"
      and science.license_rung("NOASSERTION")[0] == "not-stated"
      and science.license_rung("Apache-2.0")[0] == "declared-unverified")

# ================= PART E: budget gate + hygiene + host reality ============
sources.github_repo_search = _REAL_GH_SEARCH
_calls = []


def _fake_http(url, timeout=10, **kw):
    _calls.append(url)
    if "/search/repositories" in url:
        return {"items": [{"full_name": "o/r", "html_url":
                           "https://github.com/o/r", "description": "x",
                           "license": {"spdx_id": "MIT"}}],
                "total_count": 1}
    if "/releases/latest" in url:
        return {"tag_name": "v1", "published_at": "2025-01-01"}
    raise AssertionError(f"unexpected network call in test: {url}")


_saved_http = sources._http_json
sources._http_json = _fake_http
try:
    _calls.clear()
    out, err, meta = sources.github_repo_search(
        "anything", 3, budget={"core": 0})
    check("E1 GitHub budget GATES the plan: with core=0 the releases/"
          "latest CORE probe is a RECORDED planned skip (releases_note) "
          "- never a burned 403, never a silent null that reads as "
          "'no releases'",
          out and out[0].get("releases_note")
          and "exhausted" in out[0]["releases_note"]
          and not any("/releases/latest" in u for u in _calls),
          str(out)[:180])
    _calls.clear()
    out2, _, _ = sources.github_repo_search("anything", 3,
                                            budget={"core": 5})
    check("E2 with budget left, the probe still runs normally (the "
          "gate does not disable it)",
          out2 and out2[0].get("latest_release") == "v1"
          and any("/releases/latest" in u for u in _calls),
          str(out2)[:180])
finally:
    sources._http_json = _saved_http

_hard = []
for p in sorted((ROOT / "backend").glob("*.py")):
    txt = p.read_text(encoding="utf-8").lower()
    if "polysolver" in txt or "sovalign" in txt:
        _hard.append(p.name)
check("E3 NO tool keyword is hardcoded anywhere in backend/: the "
      "POLYSOLVER-class behaviour is pure name-resolution against "
      "native data, so the layer generalizes to any tool named in any "
      "question", not _hard, str(_hard))
_na = []
if "N/A" not in (ROOT / "frontend" / "app.js").read_text(
        encoding="utf-8"):
    _na.append("app.js")
if "N/A" not in (ROOT / "backend" / "exports.py").read_text(
        encoding="utf-8"):
    _na.append("exports.py")
check("E4 every display surface renders N/A explicitly (app.js + "
      "exports; the JSON keeps null)", not _na, str(_na))
inventory_mod.load_inventory = _REAL_LOAD_INV
intent_lib.load_declared = _REAL_LOAD_DECLARED
try:
    _real_inv = _REAL_LOAD_INV()
    _repo = _real_inv.get("repo") or ""
    _real_decl = _REAL_LOAD_DECLARED(_repo) if _repo else {}
    if not _real_decl:
        print("NOTE  E5 skipped: this host has no native tool registry "
              "at the configured repo")
    else:
        _real = intent_lib.analyze(QREG, stages=_real_inv["stages"],
                                   declared=_real_decl)
        _rb = {t["raw"]: t["status"] for t in _real["tools"]}
        check("E5 host reality: THIS deployment's native config "
              "resolves the same names the same way (declared vs "
              "unverified - read-only probe, fixture-free)",
              _rb.get("POLYSOLVER") == "declared"
              and _rb.get("sovalign") == "unverified",
              str(_rb))
except Exception as e:
    print(f"NOTE  E5 skipped ({type(e).__name__}: host repo not "
          f"available here)")

print()
print(f"verify_m11_p11: {len(PASS)} pass, {len(FAIL)} fail")
sys.exit(1 if FAIL else 0)
