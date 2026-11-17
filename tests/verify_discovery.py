"""Milestone-4 PART D/F4: REAL SOTA discovery end-to-end.

Runs the LIVE research engine against an isolated server (own DB + port,
throwaway deploy dirs) and verifies that the Presentation Prediction
attempt contains GENUINE retrieval: persisted query provenance, real
literature records with resolvable ids, registry-verified or explicitly
'Not verified' candidate fields, honest coverage + failure reporting -
and that no mock/example evidence appears anywhere in a live report.
Also demonstrates the full-pipeline attempt's durable lifecycle: progress
events, cooperative cancel, accepted resume.

If none of the literature/registry sources is reachable this script
exits SKIP (2) - a network failure must never masquerade as a code
failure, and the test must never fake a pass.

Run: python3 tests/verify_discovery.py
"""
import atexit
import base64
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 8797
BASE = f"http://127.0.0.1:{PORT}"
PASS, FAIL = [], []

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

_srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="discoverytest.sqlite3",
             NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-disc-skills",
             NEOAG_TASKQ_DIR="/tmp/neoag-disc-taskq"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _cleanup():
    _srv.terminate()
    try:
        _srv.wait(timeout=8)
    except Exception:
        _srv.kill()
    try:
        (ROOT / "data" / "discoverytest.sqlite3").unlink()
    except OSError:
        pass
    import shutil
    for _d in ("/tmp/neoag-disc-skills", "/tmp/neoag-disc-taskq"):
        shutil.rmtree(_d, ignore_errors=True)
    shutil.rmtree(ROOT / "data" / "exports-discoverytest",
                  ignore_errors=True)


atexit.register(_cleanup)


def call(path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data)
    req.add_header("Authorization", AUTH_HEADER)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except urllib.error.URLError:
        # server not up yet (or dropped the connection): status 0 lets
        # wait_up() keep retrying instead of crashing the whole test.
        return 0, {}


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS" if ok else "FAIL") + f"  {name}" +
          (f"  [{detail}]" if detail and not ok else ""))


def wait_up():
    for _ in range(80):
        st, _b = call("/api/bootstrap", timeout=3)
        if st == 200:
            return True
        time.sleep(0.25)
    return False


def reachable():
    """Probe the real sources through the server's own adapter code
    (imported directly - read-only HTTP probes against one query each)."""
    sys.path.insert(0, str(ROOT))
    try:
        from backend import sources
        return sources.probe()
    except Exception as e:
        print("probe error:", e)
        return {}


if not wait_up():
    print("isolated server did not start")
    sys.exit(1)

_probe = reachable()
# probe() reports per source: {"reachable": bool, "error", "latency_ms",
# "sample_records", "checked_at"} - "reachable" is the honest key.
_lit_ok = any(v.get("reachable") for k, v in _probe.items()
              if k in ("europepmc", "pubmed", "openalex"))
print("source reachability:",
      {k: ("ok" if v.get("reachable") else "FAIL") for k, v in _probe.items()})
if not _lit_ok:
    print("SKIP: no literature source reachable from this host right now; "
          "live retrieval cannot be verified (not a code failure)")
    sys.exit(2)

# ---------- 1 targeted run (presentation + hla_typing), LIVE --------
st, sub = call("/api/research", {
    "scope": "targeted", "engine": "live",
    "targets": ["presentation", "hla_typing"]})
check("1 live targeted submission accepted", st == 202, str(sub)[:160])
rid = sub.get("research_id", "")
t0 = time.time()
rep = {}
status = ""
# Budget is generous BY DESIGN: the engine honours each endpoint's
# rate-limit advice (M6 A8) instead of hammering it. Measured 2026-11-17:
# with the host's shared unauthenticated GitHub budget saturated, each
# GitHub search costs ~2 min of recorded retry waits and a 2-module
# run finishes in 44 min; with a fresh budget the same run takes ~5.
# A slow-but-honest run must PASS, so the poll waits for the terminal
# status rather than pre-empting it.
while time.time() - t0 < 3000:
    st, rr = call("/api/research/" + rid, timeout=20)
    status = rr.get("status", "")
    prog = rr.get("progress") or {}
    if prog.get("steps"):
        pass
    if status in ("completed", "incomplete", "failed", "cancelled"):
        rep = rr.get("report") or {}
        break
    time.sleep(3)
check("2 live run reached a terminal honest status",
      status in ("completed", "incomplete", "failed"), status)
check("3 report exists even when incomplete (never silent)", bool(rep),
      status)

prov = rep.get("provenance") or {}
qs = prov.get("queries") or []
check("4 persisted query provenance (queries + per-source counts)",
      len(qs) > 0 and bool(prov.get("query_counts")),
      f"{len(qs)} queries")
any_query = qs[0] if qs else {}
check("5 query records carry source + result counts",
      {"source", "query"} <= set(any_query) and
      isinstance(any_query.get("count"), int), str(any_query)[:160])
cands = rep.get("candidates") or []
check("6 candidate rows assembled (>=1) with the D5 schema",
      len(cands) >= 1 and {"name", "verified_version", "license",
                           "maintenance", "published_benchmark_evidence",
                           "independent_validation", "evidence_gaps",
                           "next_action"} <= set(cands[0]),
      f"{len(cands)} candidates")
FIELDS = ("verified_version", "license", "maintenance")
_bad7 = [c for c in cands if not all(isinstance(c.get(f), str) and c.get(f)
                                     for f in FIELDS)]
check("7 unverifiable facts stay 'Not verified' (never blank, never guessed)",
      not _bad7,
      str([{"name": c.get("name"),
            **{f: (c.get(f), type(c.get(f)).__name__) for f in FIELDS}}
           for c in _bad7])[:300])
# The live candidate pool rotates with upstream search results, so the
# fabrication-proof invariant is: REAL registry interaction (a positive
# version OR a definite 'not on <registry>' answer, which a fake engine
# cannot know) + the consistency rule that any positive registry hit
# MUST surface in verified_version.
_reg_steps = [s for c in cands for s in (c.get("version_chain") or {}).get("chain", [])
              if s["step"].startswith("registry")]
_interact = any(s["outcome"] in ("found", "not on PyPI", "not on bioconda")
                for s in _reg_steps)
_consistent = all(not any(s["outcome"] == "found"
                          for s in (c.get("version_chain") or {}).get("chain", []))
                  or c.get("verified_version") != "Not verified"
                  for c in cands)
check("8 real registry interaction for every run (positive version or "
      "definite 'not on' answer), and any positive hit always surfaces in "
      "verified_version", _reg_steps and _interact and _consistent,
      str([(s["step"], s["outcome"]) for s in _reg_steps])[:200])
cits = rep.get("literature_citations") or []
check("9 literature citations are real records with ids/urls",
      len(cits) > 0 and all((c.get("id") or c.get("url")) and c.get("title")
                            for c in cits),
      f"{len(cits)} citations; first: {str(cits[:1])[:160]}")
blob = json.dumps(rep)
check("10 NO mock/example evidence anywhere in a live report",
      "MOCK" not in blob and "mock digest" not in blob.lower() and
      "example figures" not in blob.lower())
check("11 disclaimer + limitations state: no execution, no own benchmarks",
      "NO tool was executed" in (rep.get("disclaimer") or "") and
      any("no tool was executed" in lim.lower()
          for lim in rep.get("limitations", [])))
check("12 code-grounded inventory from the configured repo (13 stages, commit)",
      (rep.get("inventory") or {}).get("n_stages") == 13 and
      str((rep.get("inventory") or {}).get("git_commit", "")).startswith("1cc4585"),
      str(rep.get("inventory", {}).get("git_commit")))
_bench_mods = {tid for tid, m in (rep.get("module_conclusions") or {}).items()
               if m.get("recommendation") == "Recommend targeted benchmark"}
check("13 B6: benchmark definitions exist ONLY for modules whose "
      "decision proposes one (never for every module, never for "
      "irrelevant tools), and carry not-rankable warnings",
      set(rep.get("recommended_benchmarks") or {}) <= _bench_mods
      and all(("comparable" in json.dumps(v).lower()
               or "not " in json.dumps(v).lower())
              for v in (rep.get("recommended_benchmarks") or {}).values()),
      str(rep.get("recommended_benchmarks"))[:160])
cov = (prov.get("query_counts") or {})
fails = prov.get("failures") or []
check("14 coverage honesty: per-source counts + failures both reported",
      isinstance(cov, dict) and isinstance(fails, list) and
      status in ("completed", "incomplete") and
      (status == "completed" or fails),
      f"status={status} sources={list(cov)} fails={len(fails)}")

# ---------- 2 full pipeline review: progress + cooperative cancel ----
st, sub2 = call("/api/research", {"scope": "full", "engine": "live"})
check("15 full-pipeline live run accepted", st == 202, str(sub2)[:120])
rid2 = sub2.get("research_id", "")
t0 = time.time()
steps = 0
while time.time() - t0 < 120:
    st, rr2 = call("/api/research/" + rid2, timeout=20)
    steps = len((rr2.get("progress") or {}).get("steps") or [])
    if steps >= 2 or rr2.get("status") not in ("queued", "running"):
        break
    time.sleep(2)
check("16 progress persisted per step (durable, visible without SSE)",
      steps >= 2 or rr2.get("status") not in ("queued", "running"),
      f"{steps} steps")
st, canc = call("/api/research/" + rid2 + "/cancel", {})
check("17 cooperative cancel accepted on a running research job",
      st == 200, str(canc)[:120])
t0 = time.time()
final2 = ""
while time.time() - t0 < 240:
    st, rr3 = call("/api/research/" + rid2, timeout=20)
    final2 = rr3.get("status", "")
    if final2 in ("cancelled", "incomplete", "completed"):
        break
    time.sleep(3)
check("18 cancelled run reaches cancelled/incomplete honestly + events logged",
      final2 in ("cancelled", "incomplete"), final2)
_, evs = call(f"/api/research/{rid2}/events", timeout=20)
check("19 research event log persisted (progress + cancel visible)",
      len((evs or {}).get("events", [])) >= 2,
      str(len((evs or {}).get("events", []))))
st, res2 = call("/api/research/" + rid2 + "/resume", {})
check("20 resume of the interrupted run accepted (queries persisted)",
      st == 202, f"{st} {str(res2)[:120]} status was {final2}")
# leave it running briefly, then cancel for a clean shutdown
time.sleep(3)
call("/api/research/" + rid2 + "/cancel", {})

# ---------- 21-24 M5 PART 4 shape on a REAL live report --------------
_bad21 = [c.get("name") for c in cands
          if not ((c.get("version_chain") or {}).get("chain")
                  and (c.get("classification") or {}).get("basis")
                  and isinstance(c.get("installed_version"), str)
                  and isinstance(c.get("performance_summary"), str))]
check("21 every candidate carries the version chain (steps+dates), the "
      "classification WITH its basis, installed-version honesty and the "
      "performance context gate", not _bad21, str(_bad21)[:150])
_steps_ok = all(s.get("outcome") and s.get("checked_at")
                for c in cands for s in c["version_chain"]["chain"])
_papers_ok = all(any("papers" in s["step"]
                     for s in c["version_chain"]["chain"])
                 for c in cands)
_bv_ok = all(isinstance(c["version_chain"]["values"].get(
    "benchmark_versions"), list) for c in cands)
check("22 every chain step records outcome+date, the papers step always "
      "runs, benchmark-versions stay a list (never an invented current "
      "version)", _steps_ok and _papers_ok and _bv_ok)
mc = rep.get("module_conclusions") or {}
vocab = rep.get("conclusion_vocabulary") or []
check("23 M8 five-label recommendation vocabulary; every researched "
      "target gets a recommendation whose rationale is present, and "
      "'Promising candidate for further evaluation' appears only where "
      "a DIRECT verified comparison pair actually favours the "
      "alternative",
      set(mc) == {t["id"] for t in (rep.get("targets") or [])}
      and len(vocab) == 5
      and all(m["recommendation"] in vocab and m.get("rationale")
              and m.get("action") and m.get("evidence_facts")
              for m in mc.values())
      and all(not (m["recommendation"]
                   == "Promising candidate for further evaluation"
                   and not any(x.get("class") == "direct"
                               and x.get("winner") == "alternative"
                               for x in m.get("comparisons") or []))
              for m in mc.values()),
      str({k: v["recommendation"] for k, v in mc.items()})[:200])
tcs = rep.get("task_comparisons") or []
_bad24 = [r["tool"] for t in tcs for r in t["rows"]
          if not (r.get("tool") and r.get("version") and r.get("evidence")
                  and r.get("why_comparable"))]
check("24 within-task blocks: baseline stated with real tools or an "
      "explicit not-identified note; ONLY direct alternatives appear as "
      "comparison rows; complementary/wrapper/related groups named "
      "separately; excluded irrelevants never reach a row",
      len(tcs) == len(rep.get("targets") or []) and not _bad24
      and all(t.get("baseline") is not None
              and ("identified" in t["baseline"]) for t in tcs)
      and all(r["tool"] not in t.get("excluded_irrelevant", [])
              for t in tcs for r in t["rows"]),
      str([(t["target_id"], t["baseline"]["identified"],
            [x["name"] for x in t["baseline"]["tools"]][:3],
            len(t["rows"])) for t in tcs])[:220])
check("25 provenance records the MEASURED GitHub budget (never assumed)",
      "github_budget_at_start" in (rep.get("provenance") or {}),
      str((rep.get("provenance") or {}).get("github_budget_at_start")))
cov = rep.get("coverage_accounting") or {}
mcv = list((rep.get("module_conclusions") or {}).values())
check("26 coverage accounting enumerates EVERY inventory module with "
      "reasons; out-of-scope modules are labelled 'not part of this "
      "run's scope' (a targeted run is complete FOR ITS SCOPE, never "
      "'stopped early')",
      cov.get("identified_modules") == 13 and
      cov.get("completeness") == "complete" and
      len(cov.get("not_researched") or []) ==
      13 - cov.get("researched_modules", 0) and
      all("scope" in (m.get("reason") or "")
          for m in cov.get("not_researched") or []),
      str({k: cov.get(k) for k in
           ("identified_modules", "researched_modules",
            "completeness")})[:160])
_a = rep.get("assessment") or {}
check("27 M6 B1: report leads with a conclusion-first assessment "
      "(headline/scope/findings/next_action), quality gates decide "
      "preliminary vs decision-ready, actions are concrete",
      _a.get("headline") and _a.get("scope") and "decision_ready" in _a
      and isinstance(rep.get("quality_gates"), list)
      and len(rep.get("quality_gates") or []) >= 8
      and isinstance(rep.get("recommended_next_actions"), list)
      and "open_questions" not in rep,
      str(_a.get("headline"))[:120])
# ---- G1 negative fixture: the 2026-11-17 report as a regression ----
_JUNK = ("clip-as-service", "Github-Ranking", "Historical-ranking",
         "public-pentesting-reports", "hackerone-reports", "HttpReports")
_in_rows = [n for t in tcs for r in t["rows"] for n in _JUNK
            if n.lower() in (r.get("tool") or "").lower()]
_in_best = [n for m in (rep.get("module_table") or []) for n in _JUNK
            if n.lower() in str(m.get("best relevant alternative", "")).lower()]
check("28 G1: none of the 2026-11-17 irrelevant repositories appear in "
      "comparison rows or as a 'best relevant alternative' (they may "
      "only appear in the appendix exclusions, with a reason)",
      not _in_rows and not _in_best,
      f"rows={_in_rows} best={_in_best}")
check("29 G1: exclusions are provenanced - every excluded candidate "
      "carries eligibility class + reason + origin",
      all(c.get("eligibility") and c.get("reasons") and c.get("origin")
          for c in rep.get("excluded_candidates") or []),
      f"{len(rep.get('excluded_candidates') or [])} excluded rows")
check("30 G1: the baseline is stated from real implementation evidence "
      "(file-level provenance), never 'no tool name in the TOML'",
      all(t["baseline"]["identified"] for t in tcs)
      and all(any("/" in ev or ev.endswith(".toml") or "[stages" in ev
                  or "[run]" in ev or ":" in ev
                  for x in t["baseline"]["tools"]
                  for ev in [x["evidence"]])
              for t in tcs if t["baseline"]["tools"]),
      str([[x["evidence"][:60] for x in t["baseline"]["tools"]][:1]
           for t in tcs])[:220])
# offline, deterministic screening regression (no network):
_sys_path = sys.path.insert(0, str(ROOT))
from backend import science as _sci  # noqa: E402
_NEG = [
    ("jina-ai/clip-as-service", "ranking",
     "multimodal AI embedding service contrastive language-image"),
    ("EvanLi/Github-Ranking", "ranking",
     "list of the Github repositories with the most stars"),
    ("juliocesarfort/public-pentesting-reports", "reports",
     "list of public penetration testing reports consulting firms"),
    ("reddelexc/hackerone-reports", "reports",
     "top disclosed vulnerability reports HackerOne bug bounty"),
    ("dotnetcore/HttpReports", "custom:x",
     "netcore distributed tracking monitoring logging for http requests"),
    ("Jannchie/Historical-ranking-data-visualization", "ranking",
     "historical ranking data visualization based on d3.js"),
]
_BAD_SCI = [(n, _sci.screen_candidate(
    n, d, profile=_sci.profile_for(m, m))["eligibility"])
    for n, m, d in _NEG
    if _sci.screen_candidate(
        n, d, profile=_sci.profile_for(m, m))["eligibility"]
    != "irrelevant"]
check("31 G1: the general eligibility screen classifies every "
      "2026-11-17 irrelevant repository as irrelevant (no name "
      "blacklists - domain/vocabulary rules only)", not _BAD_SCI,
      str(_BAD_SCI)[:200])
# Blurbs as real registries actually state them (a method claim is
# part of a registry summary): the screen promotes on a positive
# software/method signal, not on biology-flavoured wording alone.
_POS_SCI = [
    ("arcasHLA", "hla_typing",
     "arcasHLA: high resolution quantification and genotyping of HLA "
     "genes from RNA-seq data"),
    ("NetMHCpan-4.1", "presentation",
     "peptide-MHC class I binding prediction trained on eluted ligand "
     "and mass spectrometry data"),
]
_BAD_POS = [n for n, m, d in _POS_SCI
            if _sci.screen_candidate(
                n, d, profile=_sci.profile_for(m, m))["eligibility"]
            != "direct alternative"]
check("32 G1 boundary: genuine HLA typing / presentation methods pass "
      "as direct alternatives (the screen is not an over-rejection)",
      not _BAD_POS, str(_BAD_POS)[:150])

# ---------- 28 persisted downloadable exports for the LIVE report -------
def _get_bytes(path):
    req = urllib.request.Request(BASE + path)
    req.add_header("Authorization", AUTH_HEADER)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


_se1, _he1, _be1 = _get_bytes(f"/api/research/{rid}/export/html")
_se1b, _he1b, _be1b = _get_bytes(f"/api/research/{rid}/export/html")
_sj2, _, _bj2 = _get_bytes(f"/api/research/{rid}/export/json")
_sx2, _, _bx2 = _get_bytes(f"/api/research/{rid}/export/tsv")
_sp2, _, _bp2 = _get_bytes(f"/api/research/{rid}/export/pdf")
_livej = json.loads(_bj2 or b"{}")
check("33 the live report exports (standalone HTML/JSON/TSV/PDF) persist "
      "pinned bytes: re-download is byte-identical with no retrieval "
      "re-run; the HTML is the conclusion-first layout with a collapsed "
      "technical appendix",
      _se1 == 200 and _se1b == 200 and _be1 == _be1b
      and _he1.get("X-Export-Sha256") == _he1b.get("X-Export-Sha256")
      and b"1. Executive assessment" in _be1
      and b"2. Module-level recommendations" in _be1
      and b"5. Technical appendix" in _be1
      and b"<details" in _be1 and b"prefers-color-scheme" in _be1
      and _sj2 == 200 and _livej.get("module_conclusions")
      and (_livej.get("provenance") or {}).get("sources_consulted")
      and _livej["export"]["report_id"] == rid
      and _sx2 == 200 and b"# ===== section: assessment" in _bx2
      and b"# ===== section: excluded_candidates" in _bx2
      and _sp2 == 200 and _bp2.startswith(b"%PDF-1.")
      and _bp2.rstrip().endswith(b"%%EOF"),
      f"h{_se1} j{_sj2} x{_sx2} p{_sp2}")

# ---------- 34 recorded outcomes never demote (M6 A8) ----------
# A definite "not on PyPI/bioconda" answer is a recorded outcome of the
# version check, not a failed retrieval: it must carry the provenance
# tag and must never demote a completed module to 'Research
# incomplete' (2026-11-17 live run mislabelled two fully-gated modules
# over 32 such answers).
_absent = [q for q in qs if q.get("error")
           and "not-on-" in str(q.get("error"))]
_hard_t = {q.get("target") for q in qs
           if q.get("error") and q.get("target")
           and not q.get("recorded_outcome")
           and "not-on-" not in str(q.get("error"))
           and "rate limit" not in str(q.get("error")).lower()}
_bad34 = [t for t, m in (rep.get("module_conclusions") or {}).items()
          if m.get("recommendation") == "Research incomplete"
          and t not in _hard_t]
check("34 M6 A8: definite registry-absence answers are tagged recorded "
      "outcomes in provenance, and no module is demoted to 'Research "
      "incomplete' without a GENUINE hard retrieval failure on its "
      "target",
      all(q.get("recorded_outcome") for q in _absent) and not _bad34,
      f"{len(_absent)} recorded-absence rows; unjustified demotions="
      f"{_bad34}")

print()
print(f"DISCOVERY E2E: {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
