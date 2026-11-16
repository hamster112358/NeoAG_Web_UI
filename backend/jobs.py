"""Mock job engine with genuine cooperative cancellation/pause, dual
execution modes (autonomous/controlled) and a persistent decision history.

Design notes (see README "Job control design"):
 - state transitions are durable (sqlite) BEFORE the in-memory thread reacts;
 - the worker polls cancel/pause flags at every substep boundary; nothing is
   ever "assumed finished" - cancellation marks the job cancelled and the
   partial stage output is discarded (mock);
 - in production the same boundaries map onto SIGTERM of the stage subprocess
   group (the current NeoAg gateway has NO kill endpoint - tracked as an
   integration task);
 - every log line is marked (mock): the prototype never simulates successful
   real computation.

Dual execution modes (same engine, same validity gates, only WHO resolves a
decision point differs):
 - autonomous  : decision points are auto-resolved to the VALIDATED default,
                 recorded as engine decisions with rationale; bounded
                 recovery = one automatic retry per stage failure, then stop
                 and report - safe automation is never forced;
 - controlled  : every decision point (input mapping, parameter choice,
                 stage failure) blocks the job and asks the user; options
                 carry validated/alternative flags and policy references;
 - switching mode mid-run is a recorded decision applied at the next stage
   boundary;
 - skipping an essential stage is possible ONLY as an explicit recorded
   user decision and marks the run production_release=false (never silent).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import threading
import time

from . import mock_data
from . import native as native_mod
from . import versions as versions_mod
from .store import Store, new_id, now
from .policies import POLICY_VERSION

# Version stamping for per-run snapshots: a recorded run keeps the version
# strings it actually ran under; later releases never rewrite them.
ENGINE_VERSION = "mock-job-engine-2026.11-r3"
NATIVE_ENGINE_VERSION = "neoag-native-adapter-2026.11-r1"
# Declared (NOT executed) tool stack of the mock engine - honest labels,
# so a snapshot can never be mistaken for a real software manifest.
MOCK_TOOLCHAIN = {
    "label": ("declared mock toolchain - the demo engine simulates these "
              "stages; no real tool binary was invoked"),
    "mock_dataset": mock_data.DEMO_DATASET_VERSION,
    "mock_predictors": ["mock-NetMHCpan-scoring", "mock-MHCflurry-scoring",
                        "mock-NetMHCstabpan-scoring", "mock-NetChop-scoring"],
}
GATE_RANKS = {"gate14": 1.4, "gate20": 2.0}
RANKING_ARTIFACTS = {"both": ["weighted", "evidence-consensus"],
                     "weighted": ["weighted"]}


def classify_outcome(registered: list[dict]) -> str:
    """Honest terminal classification (A1). Derived ONLY from the
    artifact availability statuses actually registered for the run:
      delivered         - every requested artifact generated, rows > 0
      zero_candidates   - all artifacts generated but contain zero rows
      partial           - some artifacts available, some empty/missing
      generation_failed - output generation itself failed
      no_artifacts      - nothing was registered at all
    None of these may be reported as 'delivery' when artifacts are
    missing; the job lifecycle state ('completed') stays separate."""
    if any(a["status"] == "failed_generation" for a in registered):
        return "generation_failed"
    if not registered:
        return "no_artifacts"
    if all(a["status"] == "empty" for a in registered):
        return "zero_candidates"
    if all(a["status"] == "missing" for a in registered):
        return "no_artifacts"
    if any(a["status"] in ("empty", "missing") for a in registered):
        return "partial"
    return "delivered"

# substeps x 0.35s sleep per stage (tuned so a full mock run takes ~30 s)
STAGE_WEIGHTS = {
    "input_qc": 4, "hla_typing": 6, "purity_cnv": 6, "hla_loh": 5,
    "rna_expression": 5, "snv": 8, "fusion_discovery": 6, "fusion_peptides": 5,
    "splice_discovery": 5, "splice_peptides": 5, "presentation": 9,
    "safety_escape": 7, "ranking": 5, "reports": 4,
}
STEP_SLEEP = 0.35

STAGE_LINES = {
    "input_qc": "input inventory + route plan -> input_qc/ (mock)",
    "hla_typing": "OptiType + SpecHLA cross-check -> hla/consensus/ (mock)",
    "purity_cnv": "FACETS/Sequenza/PURPLE -> purity/consensus/ (mock)",
    "hla_loh": "LOHHLA + SpecHLA -> hla_loh/ consensus (mock)",
    "rna_expression": "Salmon quantification -> rna/expression/ (mock)",
    "snv": "VEP + peptide windows -> merged/raw_peptides.tsv (mock)",
    "fusion_discovery": "EasyFuse/STAR-Fusion candidates (mock)",
    "fusion_peptides": "breakpoint-crossing peptide review (mock)",
    "splice_discovery": "RegTools junction screen (mock)",
    "splice_peptides": "junction-crossing peptide review (mock)",
    "presentation": "NetMHCpan/MHCflurry/stability/cleavage scoring (mock)",
    "safety_escape": "normal proteome/expression/ligandome review (mock)",
    "ranking": "weighted + evidence-consensus ranking (mock)",
    "reports": "patient/technical/onepage drafts (mock)",
}

# Decision points exist in BOTH modes; mode only changes who resolves them.
# Each pipeline decision is fully specified (A4): stable key, category,
# trigger, policy + version, mandatory flag, source, effect class, and an
# EXPLICIT option->execution mapping that the engine actually applies.
# The old "validated" option flag suggested scientific validation that the
# mock engine has never performed; options now carry `policy_default` (a
# documented default to apply autonomously) plus an honest
# `validation_status` string, and the UI labels the default accordingly.
DECISION_POINTS = {
    "input_qc": {
        "key": "input.rna_pair",
        "category": "pipeline",
        "decision_type": "input_mapping",
        "trigger": "2 candidate RNA FASTQ pairs match the sample in the "
                   "mock input inventory",
        "question": ("Two RNA FASTQ pairs in the mock inventory match this "
                     "sample. Which pair feeds the RNA expression branch?"),
        "options": [
            {"id": "matched", "label": "tumor.rna_R1/R2.fastq.gz (paired, "
             "sample-id matched, same run)", "policy_default": True,
             "validation_status": ("documented default (route plan); not "
                                   "independently validated on this data"),
             "effect": {"rna_inputs": "matched_pair"}},
            {"id": "legacy", "label": "legacy_run_R1/R2.fastq.gz (older run, "
             "labelling ambiguity)", "policy_default": False,
             "validation_status": "not validated - labelling ambiguity known",
             "effect": {"rna_inputs": "legacy_pair",
                        "warning": "legacy RNA pair: expression evidence "
                                   "carries a labelling-ambiguity caveat"}},
        ],
        "rationale_default": ("sample-id match and same sequencing run - "
                              "mirrors the route plan documented in Skill2"),
        "evidence": ["mock inventory: 2 candidate pairs", "sample-id prefix match",
                     "both pairs indexed (mock)"],
        "policy_id": "POL-007",
        "mandatory": True,
        "effect_class": "input_selection",
        "source": "jobs.py DECISION_POINTS (mock mirror of Skill2 route plan)",
    },
    "presentation": {
        "key": "presentation.stability_gate",
        "category": "pipeline",
        "decision_type": "parameter_choice",
        "trigger": "entry to the presentation stage",
        "question": "Stability gate threshold applied to presentation evidence.",
        "options": [
            {"id": "gate14", "label": "stability rank <= 1.4 (deployed "
             "production gate)", "policy_default": True,
             "validation_status": ("deployed criterion per POL-001; the mock "
                                   "engine does not re-validate it"),
             "effect": {"stability_max_rank": 1.4,
                        "evidence_tier": "production"}},
            {"id": "gate20", "label": "stability rank <= 2.0 (relaxed, "
             "exploratory only)", "policy_default": False,
             "validation_status": "exploratory setting; changes evidence tier",
             "effect": {"stability_max_rank": 2.0,
                        "evidence_tier": "exploratory"}},
        ],
        "rationale_default": ("1.4 is the deployed production criterion; the "
                              "relaxed option exists for exploration and "
                              "would flag results as non-production"),
        "evidence": ["mock: deployed gate max_stabpan_rank=1.4",
                     "relaxing the gate would change the evidence tier"],
        "policy_id": "POL-001",
        "mandatory": True,
        "effect_class": "threshold_selection",
        "source": "policies.py POL-001 (NeoAG README / PRODUCTION_REQUIRED_TOOLS)",
    },
    "ranking": {
        "key": "ranking.outputs",
        "category": "pipeline",
        "decision_type": "parameter_choice",
        "trigger": "entry to the ranking stage",
        "question": "Which ranking output(s) should the run produce?",
        "options": [
            {"id": "both", "label": "weighted + evidence-consensus (both, as "
             "production requires)", "policy_default": True,
             "validation_status": ("documented production pairing; the mock "
                                   "consensus rules are demo rules"),
             "effect": {"ranking_artifacts": ["weighted",
                                              "evidence-consensus"]}},
            {"id": "weighted", "label": "weighted only (exploratory)",
             "policy_default": False,
             "validation_status": "exploratory setting; no consensus "
                                  "comparison artifact is produced",
             "effect": {"ranking_artifacts": ["weighted"]}},
        ],
        "rationale_default": ("production ranking emits weighted and "
                              "evidence-consensus rankings together"),
        "evidence": ["mock: consensus comparison is the review baseline"],
        "policy_id": "POL-003",
        "mandatory": True,
        "effect_class": "output_selection",
        "source": "policies.py POL-003 + production workflow docs",
    },
}
REQUIRED_STAGES = {s["id"] for s in mock_data.STAGES if s["required"]}


class JobManager:
    def __init__(self, store: Store, publish):
        self.store = store
        self.publish = publish
        self.controls: dict[str, dict] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.resolutions: dict[str, dict] = {}   # job_id -> {decision_id: choice}
        self.mode_requests: dict[str, str] = {}  # job_id -> requested mode

    # ----- submission -----
    def submit(self, sample_id: str, stage_ids: list[str], mode: str,
               execution_mode: str = "controlled", simulate_failure: bool = False,
               requested_by: str = "user", kind: str = "mock-analysis",
               manifest_id: str | None = None,
               requested_config: dict | None = None,
               conv_id: str = "") -> dict:
        stages = [mock_data.STAGE_BY_ID[s] for s in stage_ids if s in mock_data.STAGE_BY_ID]
        if not stages:
            raise ValueError("no valid stages selected")
        if execution_mode not in ("autonomous", "controlled"):
            raise ValueError("execution_mode must be autonomous|controlled")
        spec = {"stage_ids": [s["id"] for s in stages], "mode": mode,
                "execution_mode": execution_mode,
                "simulate_failure": simulate_failure,
                "dataset_version": mock_data.DEMO_DATASET_VERSION,
                "manifest_id": manifest_id,
                "requested_config": requested_config or {}}
        job_id = new_id("job")
        # Mirrors gateway policy POL-005: execute-class requests need a human
        # approval before they may run, even for mock work (boundary drill).
        # This holds in BOTH execution modes - autonomy never bypasses the
        # destructive-operation approval boundary.
        appr = self.store.approval_create(
            new_id("appr"), job_id,
            f"start mock analysis for {sample_id} ({len(stages)} stages, "
            f"{mode} settings, {execution_mode} mode)",
            "MEDIUM (mock of an execute-class request)",
            "Approving queues the MOCK job. No real pipeline is connected; "
            "with the real gateway this class maps to HIGH risk / approved=true.")
        self.store.job_create(job_id, kind, sample_id, spec,
                              "blocked", appr["id"], conv_id=conv_id)
        self.store.event_add(job_id, "state",
                             f"blocked: waiting for approval {appr['id']}")
        self.store.audit(requested_by, "job.create.blocked", job_id,
                         {"sample": sample_id, "stages": len(stages),
                          "execution_mode": execution_mode, "approval": appr["id"]})
        self.publish({"type": "approval", "id": appr["id"]})
        self.publish({"type": "job", "id": job_id})
        return {"job_id": job_id, "approval_id": appr["id"]}

    def submit_native(self, sample_id: str, profile: str = "demo",
                      native_params: dict | None = None, stub: bool = False,
                      requested_by: str = "user",
                      conv_id: str = "") -> dict:
        """Formal NEOAG job on the NATIVE execution system (Milestone 5,
        PART 1). Two profiles, both real NeoAG code paths:
          demo    - `neoag.cli run-demo`: the deployed repo's own safe
                    synthetic execution (fixture inputs, predictors
                    explicitly stubbed, no patient data / licensed
                    binaries / network). Completes -> real artifacts.
          gateway - `open-neo run --mode execute` through the native
                    controlled-execution gateway (the ONLY way native
                    code permits heavy execution; verified: direct
                    execute returns GATEWAY_REQUIRED). Requires real
                    inputs and a working toolchain; failures are
                    reported honestly, never rerouted to the mock engine.
        Same approval boundary as mock execute-class work: a human
        approves BEFORE anything runs (POL-005)."""
        if profile not in ("demo", "gateway"):
            raise ValueError("profile must be demo|gateway")
        if profile == "gateway" and not (native_params or {}).get("sample_id"):
            raise ValueError("gateway profile requires native_params.sample_id")
        params = dict(native_params or {})
        params.setdefault("sample_id", sample_id)
        spec = {"engine": "native", "native_profile": profile,
                "native_params": params, "stub": bool(stub),
                "execution_mode": "autonomous",
                "requested_config": dict(params)}
        job_id = new_id("job")
        if profile == "demo":
            summary = ("start NATIVE demo execution for sample "
                       f"{sample_id} - real NeoAG pipeline code, bundled "
                       "demo fixtures, predictors stubbed")
            risk = "MEDIUM (native runner, synthetic fixture inputs only)"
            note = ("Approving runs `neoag.cli run-demo` from the deployed "
                    "NeoAG source. No patient data, no licensed binaries, "
                    "no network. Outputs are real files under the run's "
                    "scratch directory.")
        else:
            summary = ("start NATIVE execution via the NeoAG gateway for "
                       f"case {params.get('case_id') or sample_id} "
                       "(planning first, then execute after this approval)")
            risk = "HIGH (execute-class on the native gateway)"
            note = ("Approving plans through the native planner and submits "
                    "the execution to NeoAG's controlled-execution gateway "
                    "with approved=true. Cancel is NOT supported by the "
                    "native gateway (verified limitation): cancelling here "
                    "marks the Web job cancelled but the gateway job runs "
                    "to completion.")
        appr = self.store.approval_create(new_id("appr"), job_id, summary,
                                          risk, note)
        self.store.job_create(job_id, "neoag-native-run", sample_id, spec,
                              "blocked", appr["id"], conv_id=conv_id)
        self.store.event_add(job_id, "state",
                             f"blocked: native job waiting for approval "
                             f"{appr['id']} ({profile} profile)")
        self.store.audit(requested_by, "job.native.create.blocked", job_id,
                         {"sample": sample_id, "profile": profile,
                          "inputs": sorted(params.keys()),
                          "approval": appr["id"]})
        self.publish({"type": "approval", "id": appr["id"]})
        self.publish({"type": "job", "id": job_id})
        return {"job_id": job_id, "approval_id": appr["id"]}

    # ---------- benchmark jobs (Milestone 5 PART 6) ----------
    BENCH_STRATEGIES = ("weighted", "evidence-consensus")

    @staticmethod
    def _dataset_consent_ok(appr: dict | None,
                            dataset_ref: str = "") -> bool:
        """PART 3 dataset consent = an APPROVED dataset-download consent
        card: the gate card written by the agent (risk 'dataset' or a
        structured kind 'dataset_download'). The approval 'action'
        column holds free prose (job summaries), so a substring match
        there would let an ordinary benchmark card masquerade as
        consent. When the consent card names a dataset it must match
        the requested one - consent to one dataset is not blanket
        consent."""
        if not appr or appr.get("state") != "approved":
            return False
        kind_ok = False
        ds_name = ""
        if str(appr.get("risk") or "") == "dataset":
            kind_ok = True
        if str(appr.get("action") or "") == "gate_op":
            try:
                d = json.loads(appr.get("detail") or "{}")
            except (ValueError, TypeError):
                d = {}
            strd = d.get("structured") or {}
            if strd.get("kind") == "dataset_download":
                kind_ok = True
                ds_name = str(strd.get("dataset") or "")
        if not kind_ok:
            return False
        if ds_name and dataset_ref:
            return ds_name.strip().lower() == dataset_ref.strip().lower()
        return True

    def submit_benchmark(self, brief: dict, sample_id: str = "DEMO-BENCH",
                         requested_by: str = "user",
                         conv_id: str = "") -> dict:
        """A benchmark is a FORMAL job: the full experiment brief
        (hypothesis / tools / baseline / dataset / metrics / compute /
        risks) is shown on the approval card, and NOTHING runs before a
        human approves it. Dataset rule (PART 3): only the synthetic
        demo fixture is free - any other dataset must carry an APPROVED
        dataset-download CONSENT card (the PART 3 gate card, see
        _dataset_consent_ok); free prose that merely mentions the word
        'dataset' is not consent. A benchmark job never downloads data.
        Candidate tools are probed read-only:
        an external tool not installed on this host gets a reviewable
        run plan recorded, never simulated measurements."""
        if not isinstance(brief, dict):
            raise ValueError("benchmark brief must be an object")
        hyp = str(brief.get("hypothesis") or "").strip()
        tools = brief.get("tools") or {}
        cand = str(tools.get("candidate") or "").strip()
        base = str(tools.get("baseline") or "").strip()
        metrics = [str(m) for m in (brief.get("metrics") or [])
                   if str(m).strip()]
        if not hyp or not cand or not base:
            raise ValueError("benchmark brief needs hypothesis, "
                             "tools.candidate and tools.baseline")
        if not metrics:
            raise ValueError("benchmark brief needs explicit metrics - a "
                             "comparison without a stated metric is not a "
                             "benchmark")
        ds = brief.get("dataset") or {}
        src = str(ds.get("source") or "demo_fixture")
        if src != "demo_fixture":
            cid = str(ds.get("consent_approval_id") or "")
            appr = self.store.approval_get(cid) if cid else None
            ds_ref = str(ds.get("id") or src)
            if not self._dataset_consent_ok(appr, ds_ref):
                raise ValueError(
                    f"dataset {ds_ref!r} is not the synthetic demo "
                    "fixture and consent_approval_id does not reference "
                    "an APPROVED dataset-download consent card "
                    "(dataset-specific) - benchmark jobs never download "
                    "datasets, an operator must supply the data first")
        cfg = brief.get("config") or {}
        try:
            gate_b = float(cfg.get("baseline_gate", 1.4))
            gate_c = float(cfg.get("candidate_gate", 1.4))
        except (TypeError, ValueError):
            raise ValueError("gates must be numbers")
        strat = str(brief.get("candidate_strategy")
                    or "evidence-consensus")
        if strat not in self.BENCH_STRATEGIES:
            raise ValueError("candidate_strategy must be one of "
                             + "/".join(self.BENCH_STRATEGIES)
                             + " (strategies this deployment can execute)"
                             + " - external tools run via plan-only arms")
        spec = {
            "engine": "benchmark",
            "benchmark": {
                "hypothesis": hyp,
                "tools": {"candidate": cand, "baseline": base},
                "baseline_strategy": "weighted",
                "candidate_strategy": strat,
                "metrics": metrics,
                "compute_estimate": str(brief.get("compute_estimate")
                                        or "not stated"),
                "risks": [str(r) for r in (brief.get("risks") or [])],
                "dataset": {"source": src, "id": ds.get("id") or src,
                            "consent_approval_id":
                                ds.get("consent_approval_id")},
                "gates": {"baseline": gate_b, "candidate": gate_c}},
            "dataset_version": mock_data.DEMO_DATASET_VERSION,
            "execution_mode": "controlled",
            "requested_config": {"benchmark": True,
                                 "top_n": int(brief.get("top_n", 12))}}
        job_id = new_id("job")
        summary = (f"BENCHMARK proposal - hypothesis: {hyp[:180]} | "
                   f"candidate '{cand}' vs baseline '{base}' on dataset "
                   f"{ds.get('id') or src} | metrics: "
                   f"{', '.join(metrics)} | compute: "
                   f"{spec['benchmark']['compute_estimate']}")
        note = ("Approving runs the benchmark harness on this host. Arms "
                "using the deployment's own ranking strategies execute "
                "for real on identical seeded inputs with a full "
                "reproducibility record; a candidate tool that is not "
                "installed here is NOT simulated - its arm records a "
                "reviewable run plan and the result states that. No "
                "dataset downloads happen at run time.")
        if spec["benchmark"]["risks"]:
            note += (" Stated risks: "
                     + "; ".join(spec["benchmark"]["risks"])[:400])
        appr = self.store.approval_create(
            new_id("appr"), job_id, summary,
            "MEDIUM (benchmark harness on synthetic demo fixture)", note)
        self.store.job_create(job_id, "benchmark", sample_id, spec,
                              "blocked", appr["id"], conv_id=conv_id)
        self.store.event_add(job_id, "state",
                             f"blocked: benchmark brief waiting for "
                             f"approval {appr['id']}")
        self.store.audit(requested_by, "job.benchmark.create.blocked",
                         job_id,
                         {"hypothesis": hyp[:200], "candidate": cand,
                          "baseline": base,
                          "dataset": spec["benchmark"]["dataset"],
                          "approval": appr["id"]})
        self.publish({"type": "approval", "id": appr["id"]})
        self.publish({"type": "job", "id": job_id})
        return {"job_id": job_id, "approval_id": appr["id"]}

    @staticmethod
    def _spearman(a: list[str], b: list[str]) -> float | None:
        """Rank correlation over the items both rankings contain (ties:
        first occurrence). None when fewer than 3 shared items - the
        harness must not compute a correlation from a stub."""
        ia = {k: i for i, k in reversed(list(enumerate(a)))}
        ib = {k: i for i, k in reversed(list(enumerate(b)))}
        shared = [k for k in a if k in ib]
        n = len(shared)
        if n < 3:
            return None
        ra = [ia[k] for k in shared]
        rb = [ib[k] for k in shared]
        ma, mb = sum(ra) / n, sum(rb) / n
        cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
        va = sum((x - ma) ** 2 for x in ra)
        vb = sum((y - mb) ** 2 for y in rb)
        if va <= 0 or vb <= 0:
            return None
        return round(cov / (va ** 0.5 * vb ** 0.5), 4)

    def _run_benchmark(self, job: dict, spec: dict) -> None:
        """Benchmark harness. Both arms receive the IDENTICAL seeded
        candidate pool; metrics are computed from the produced tables,
        not asserted. The run is pinned by a reproducibility record
        (seed, input/output checksums, environment, versions, measured
        wall time). An external candidate tool that is not installed
        here is recorded as a plan-only arm with a read-only probe
        result - never a simulated measurement."""
        job_id = job["id"]
        b = spec["benchmark"]
        top_n = int((spec.get("requested_config") or {}).get("top_n", 12))
        seed = f"{job_id}:{job['sample_id']}:{spec['dataset_version']}"
        benchdir = os.path.join(str(self.store.data_dir), "benchmarks",
                                job_id)
        os.makedirs(benchdir, exist_ok=True)
        t0 = time.monotonic()
        self.store.job_set(job_id, state="running", started_at=now(),
                           stage="benchmark: inputs", progress=5.0)
        self.store.event_add(
            job_id, "log",
            f"(benchmark harness) hypothesis: {b['hypothesis']}")
        self.store.event_add(job_id, "log",
                             f"(benchmark harness) seed pinned: {seed}; "
                             "both arms will see the identical input pool")

        def _cancelled() -> bool:
            if not self._flag(job_id, "cancel"):
                return False
            self.store.job_set(job_id, state="cancelled", finished_at=now(),
                               error="cancelled by user mid-benchmark - "
                                     "partial files kept under the run dir")
            self.store.event_add(job_id, "state", "cancelled")
            self.publish({"type": "job", "id": job_id})
            return True

        pool = mock_data.make_pool(job_id, job["sample_id"], top_n)
        pool_sha = hashlib.sha256(
            json.dumps(pool, sort_keys=True, ensure_ascii=False)
            .encode()).hexdigest()
        self.store.event_add(job_id, "log",
                             f"(benchmark harness) input pool: {len(pool)} "
                             f"candidate(s), sha256 {pool_sha[:16]}...")
        if _cancelled():
            return
        # read-only probes for BOTH named tools (installed version honesty)
        probes = {arm: versions_mod.installed_version(b["tools"][arm])
                  for arm in ("baseline", "candidate")}
        for arm in ("baseline", "candidate"):
            p = probes[arm]
            if p.get("value"):
                pline = (f"installed version {p['value']} "
                         f"({p.get('evidence')})")
            else:
                pline = ("not verified - " + str(
                    p.get("reason") or
                    "no version evidence in the deployed tools layout"))
            self.store.event_add(
                job_id, "log",
                f"(benchmark harness) tool probe {arm}="
                f"{b['tools'][arm]}: {pline}")
        arms: dict[str, dict] = {}
        registered: list[dict] = []
        warnings: list[str] = []
        # baseline arm: the deployment's current weighted ranking
        self.store.job_set(job_id, stage="benchmark: baseline arm",
                           progress=30.0)
        try:
            base_pool = mock_data.make_pool(job_id, job["sample_id"], top_n)
            base_tbl, base_meta = mock_data.weighted_rank(
                mock_data.apply_gate(base_pool, b["gates"]["baseline"]))
            arms["baseline"] = {
                "kind": "deployed strategy", "strategy": "weighted",
                "gate": b["gates"]["baseline"], "rows": base_tbl,
                "meta": base_meta, "tool": b["tools"]["baseline"],
                "installed": probes["baseline"]}
        except Exception as e:
            arms["baseline"] = {"kind": "deployed strategy",
                                "error": str(e)[:300], "rows": []}
            warnings.append(f"baseline arm failed: {e}")
        if _cancelled():
            return
        # candidate arm: deployed strategy when requested (real, paired),
        # otherwise a PLAN (never simulated) with the read-only probe
        self.store.job_set(job_id, stage="benchmark: candidate arm",
                           progress=50.0)
        if b["candidate_strategy"] in self.BENCH_STRATEGIES:
            try:
                cand_pool = mock_data.make_pool(job_id, job["sample_id"],
                                                top_n)
                if b["candidate_strategy"] == "weighted":
                    cand_tbl, cand_meta = mock_data.weighted_rank(
                        mock_data.apply_gate(cand_pool,
                                             b["gates"]["candidate"]))
                else:
                    cand_tbl = mock_data.consensus_rank(
                        mock_data.apply_gate(cand_pool,
                                             b["gates"]["candidate"]),
                        b["gates"]["candidate"])
                    cand_meta = {"gate": b["gates"]["candidate"],
                                 "tier": f">={mock_data.CONSENSUS_TIER}/"
                                         f"{len(mock_data.CONSENSUS_RULES)}"
                                         " rules"}
                arms["candidate"] = {
                    "kind": "deployed strategy",
                    "strategy": b["candidate_strategy"],
                    "gate": b["gates"]["candidate"], "rows": cand_tbl,
                    "meta": cand_meta, "tool": b["tools"]["candidate"],
                    "installed": probes["candidate"]}
            except Exception as e:
                arms["candidate"] = {"kind": "deployed strategy",
                                     "error": str(e)[:300], "rows": []}
                warnings.append(f"candidate arm failed: {e}")
        else:
            arms["candidate"] = {
                "kind": "plan-only (external tool not executed)",
                "tool": b["tools"]["candidate"],
                "installed": probes["candidate"],
                "rows": [],
                "plan": (
                    "TO RUN THIS ARM: an operator must install/enable "
                    f"'{b['tools']['candidate']}' (production tool "
                    "install requires explicit authorization - this app "
                    "never installs or patches tools silently), pin its "
                    "exact version, feed it the identical input pool "
                    "recorded in this bundle (input_pool.json), and write "
                    "its ranked output to arm_candidate.tsv in the same "
                    "schema as arm_baseline.tsv. Then resubmit this "
                    "benchmark brief; the harness will compute the "
                    "metrics from real outputs."),
                "note": ("NO measurements were produced for this arm and "
                         "none were invented")}
            warnings.append(
                f"candidate '{b['tools']['candidate']}' is not a strategy "
                "this deployment executes - arm recorded as PLAN ONLY "
                "(no simulated measurements)")
        if _cancelled():
            return
        # metrics: computed from the two tables, or explicitly absent
        self.store.job_set(job_id, stage="benchmark: metrics",
                           progress=70.0)
        a_rows = arms["baseline"].get("rows") or []
        c_rows = arms["candidate"].get("rows") or []
        a_keys = [r.get("peptide") for r in a_rows]
        c_keys = [r.get("peptide") for r in c_rows]
        k = min(len(a_keys), len(c_keys), top_n)
        metrics = {
            "stated_metrics": b["metrics"],
            "computed": None,
            "note": ""}
        if a_rows and c_rows and k >= 2:
            overlap = len(set(a_keys[:k]) & set(c_keys[:k]))
            both = set(a_keys) & set(c_keys)
            metrics["computed"] = {
                "top_k_overlap": {"value": round(overlap / k, 4),
                                  "k": k,
                                  "definition": f"shared entries in the "
                                  f"top-{k} of both rankings / k"},
                "rank_correlation_spearman": {
                    "value": self._spearman(a_keys, c_keys),
                    "definition": "Spearman rho over the shared peptides "
                                  "(first-occurrence ranks); null when "
                                  "fewer than 3 shared items"},
                "candidate_set_fraction": {
                    "value": round(len(both) / max(len(set(a_keys)
                                                      | set(c_keys)), 1), 4),
                    "definition": "Jaccard of the two candidate sets"}}
            metrics["note"] = ("values COMPUTED from the two produced "
                               "tables (see arm files); recompute is "
                               "possible from the pinned seed + checksums")
        else:
            metrics["note"] = (
                "NOT computed: at least one arm produced no table "
                "(plan-only or failed arm). The stated metrics remain "
                f"{b['metrics']} and are UNANSWERED by this run - no "
                "number is substituted")
        wall = round(time.monotonic() - t0, 3)
        # persist the reproducibility record + arm files (real bytes on
        # disk; the results-bundle export ships them)
        repro = {
            "kind": "benchmark_reproducibility_record",
            "job_id": job_id, "seed": seed,
            "input_pool_sha256": pool_sha,
            "dataset_version": spec["dataset_version"],
            "dataset_source": b["dataset"],
            "ground_truth": (
                "the mock fixture pool is a deterministic function of the "
                "seed (mock_data.make_pool) - 'ground truth' here is the "
                "identical-input property, NOT validated biology"),
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "engine": ENGINE_VERSION, "policy": POLICY_VERSION,
                "wall_time_seconds_measured": wall},
            "tools": {"baseline": {"name": b["tools"]["baseline"],
                                   **probes["baseline"]},
                      "candidate": {"name": b["tools"]["candidate"],
                                    **probes["candidate"]}},
            "arms": {arm: {k2: v2 for k2, v2 in a.items()
                           if k2 != "rows"}
                     for arm, a in arms.items()},
            "metrics": metrics,
            "commands_note": ("the harness calls backend/mock_data "
                              "functions in-process (no subprocess was "
                              "spawned); external-arm commands would be "
                              "recorded verbatim here")}
        files = {"reproducibility.json": json.dumps(repro, indent=1,
                                                    ensure_ascii=False),
                 "benchmark_metrics.json": json.dumps(metrics, indent=1,
                                                      ensure_ascii=False),
                 "input_pool.json": json.dumps(pool, indent=1,
                                               ensure_ascii=False)}
        for arm in ("baseline", "candidate"):
            rows = arms[arm].get("rows")
            if rows:
                files[f"arm_{arm}.tsv"] = (
                    "\t".join(str(x) for x in rows[0].keys()) + "\n"
                    + "\n".join("\t".join(
                        json.dumps(r.get(c), ensure_ascii=False)
                        if isinstance(r.get(c), (dict, list))
                        else str(r.get(c)) for c in rows[0])
                        for r in rows))
            else:
                files[f"arm_{arm}.plan.txt"] = str(
                    arms[arm].get("plan") or arms[arm].get("error")
                    or "no output")
        for name, text in files.items():
            with open(os.path.join(benchdir, name), "w") as f:
                f.write(text)
            registered.append(self.store.artifact_add(
                job_id, "benchmark_output", name, None,
                os.path.join(benchdir, name), "available",
                text.count("\n") + 1,
                hashlib.sha256(text.encode()).hexdigest()[:16], {}))
            self.store.event_add(job_id, "log",
                                 f"(benchmark harness) wrote {name}")
        outcome = classify_outcome(registered)
        result = {
            "engine": "benchmark", "engine_version": ENGINE_VERSION,
            "disclaimer": ("BENCHMARK ON MOCK FIXTURE - inputs are the "
                           "synthetic demo pool, not patient data. "
                           "Computed values are real computations on that "
                           "fixture; they say NOTHING about real-world "
                           "tool performance."),
            "outcome": outcome,
            "outcome_note": (
                "both arms executed for real on identical seeded inputs"
                if (a_rows and c_rows) else
                "AT LEAST ONE ARM HAS NO EXECUTED OUTPUT - the stated "
                "metrics are unanswered by this run (see metrics.note)"),
            "brief": b,
            "metrics": metrics,
            "arms_summary": {arm: {k2: v2 for k2, v2 in a.items()
                                   if k2 != "rows"}
                             for arm, a in arms.items()},
            "reproducibility": repro,
            "warnings": warnings,
            "outdir": benchdir,
            "provenance": {"job_id": job_id, "seed": seed,
                           "input_pool_sha256": pool_sha,
                           "dataset_version": spec["dataset_version"]}}
        self.store.job_set(job_id, state="completed", finished_at=now(),
                           progress=100.0, stage=None, outcome=outcome,
                           result_json=json.dumps(result,
                                                  ensure_ascii=False))
        self.store.event_add(job_id, "state",
                             f"completed (benchmark): outcome={outcome}, "
                             f"metrics computed={'yes' if metrics['computed'] else 'NO - unanswered'}")
        self.store.audit("engine", "job.benchmark.completed", job_id,
                         {"outcome": outcome,
                          "computed": bool(metrics["computed"]),
                          "wall_s": wall})
        self.publish({"type": "job", "id": job_id})

    def on_approval_decided(self, appr_id: str, approve: bool) -> None:
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT id FROM jobs WHERE approval_id=?", (appr_id,)).fetchall()
        for r in rows:
            job = self.store.job_get(r["id"])
            if not job or job["state"] != "blocked":
                continue
            if approve:
                self.store.job_set(job["id"], state="queued")
                self.store.event_add(job["id"], "state", "queued (approval granted)")
                self._launch(job["id"])
            else:
                self.store.job_set(job["id"], state="cancelled", finished_at=now(),
                                   error="approval denied by user")
                self.store.event_add(job["id"], "state", "cancelled (approval denied)")
            self.publish({"type": "job", "id": job["id"]})

    # ----- controls -----
    def request_cancel(self, job_id: str) -> bool:
        job = self.store.job_get(job_id)
        if not job or job["state"] in ("completed", "failed", "cancelled"):
            return False
        self.store.job_set(job_id, cancel_requested=1,
                           cancel_reason="requested by user")
        if job["state"] != "running":
            # queued/blocked/paused: no running substep loop will observe the
            # flag, so cancel immediately (worker reacts via its own checks).
            self.store.job_set(job_id, state="cancelled", finished_at=now(),
                               error="cancelled by user while " + job["state"])
            self.store.event_add(job_id, "state", "cancelled by user")
            for d in self.store.decision_list(job_id):
                if d["status"] == "pending":
                    self.store.decision_set(d["id"], "aborted",
                                            "job cancelled while pending", "user",
                                            status="cancelled")
            self.publish({"type": "job", "id": job_id})
        self.store.audit("user", "job.cancel.requested", job_id)
        return True

    def request_pause(self, job_id: str) -> bool:
        job = self.store.job_get(job_id)
        if not job or job["state"] != "running":
            return False
        if json.loads(job["spec_json"] or "{}").get("engine") == "native":
            self.store.event_add(job_id, "log",
                "pause requested - NOT supported for native runs: the "
                "pipeline executes inside NeoAG's gateway or as one "
                "supervised runner process with no safe pause point. "
                "Cancel (with its honest caveats) is the only stop control.")
            return False
        self.store.job_set(job_id, pause_requested=1)
        self.controls.setdefault(job_id, {})["pause"] = True
        self.store.audit("user", "job.pause.requested", job_id)
        return True

    def request_resume(self, job_id: str) -> bool:
        job = self.store.job_get(job_id)
        if not job or job["state"] != "paused":
            return False
        self.store.job_set(job_id, pause_requested=0)
        self.controls.setdefault(job_id, {})["pause"] = False
        self.store.audit("user", "job.resume.requested", job_id)
        return True

    def request_mode(self, job_id: str, mode: str) -> dict | None:
        if mode not in ("autonomous", "controlled"):
            return None
        job = self.store.job_get(job_id)
        if not job or job["state"] in ("completed", "failed", "cancelled"):
            return None
        cur = (json.loads(job["spec_json"]) or {}).get("execution_mode", "controlled")
        if json.loads(job["spec_json"] or "{}").get("engine") == "native":
            return {"ok": False,
                    "error": "mode switching applies to mock decision points; "
                             "native runs take their scientific options from "
                             "the submitted configuration"}
        if mode == cur:
            return {"ok": False, "error": "already in that mode"}
        self.mode_requests[job_id] = mode
        self.store.event_add(job_id, "log",
                             f"mode switch to {mode} requested - will apply at "
                             "the next stage boundary (recorded as a decision)")
        self.store.audit("user", "job.mode.requested", job_id,
                         {"from": cur, "to": mode})
        self.publish({"type": "job", "id": job_id})
        return {"ok": True, "applies_at": "next stage boundary"}

    def resolve_decision(self, d_id: str, choice: str, note: str = "") -> dict | None:
        d = self.store.decision_get(d_id)
        if not d or d["status"] != "pending":
            return None
        labels = {o["id"] for o in d["options"]}
        if choice not in labels:
            return None
        self.store.decision_set(d_id, choice, note or "resolved by user", "user")
        self.resolutions.setdefault(d["job_id"], {})[d_id] = choice
        self.publish({"type": "job", "id": d["job_id"]})
        return {"ok": True}

    # ----- worker -----
    def _launch(self, job_id: str) -> None:
        t = threading.Thread(target=self._run, args=(job_id,), daemon=True)
        self.threads[job_id] = t
        t.start()

    def resume_queued(self) -> None:
        """(re)launch queued jobs at startup; blocked jobs wait for approval."""
        for job in self.store.job_list():
            if job["state"] == "queued":
                self._launch(job["id"])

    def _flag(self, job_id: str, name: str) -> bool:
        ctrl = self.controls.setdefault(job_id, {})
        if name in ctrl:
            return bool(ctrl[name])
        col = "cancel_requested" if name == "cancel" else "pause_requested"
        job = self.store.job_get(job_id)
        return bool(job and job[col])

    def _sleep_checked(self, job_id: str, seconds: float) -> str:
        """Sleep in small slices; return 'cancel'/'pause'/'ok'."""
        end = time.time() + seconds
        while time.time() < end:
            if self._flag(job_id, "cancel"):
                return "cancel"
            if self._flag(job_id, "pause"):
                return "pause"
            time.sleep(0.15)
        return "cancel" if self._flag(job_id, "cancel") else (
            "pause" if self._flag(job_id, "pause") else "ok")

    # ----- decision points -----
    def _make_decision(self, job_id: str, stage: dict, dp: dict) -> str:
        d_id = new_id("dec")
        self.store.decision_add({
            "id": d_id, "ts": now(), "decided_at": None, "job_id": job_id,
            "stage": stage["name"], "decision_type": dp["decision_type"],
            "question": dp["question"], "options": dp["options"],
            "chosen": None, "rationale": None,
            "evidence": dp.get("evidence", []), "policy_id": dp.get("policy_id"),
            "policy_version": POLICY_VERSION, "decided_by": None,
            "status": "pending",
            "key": dp.get("key"), "category": dp.get("category", "pipeline"),
            "trigger": dp.get("trigger"), "source": dp.get("source"),
            "mandatory": dp.get("mandatory", True),
            "effect_class": dp.get("effect_class"),
            "downstream": {"option_effects":
                           {o["id"]: o.get("effect", {})
                            for o in dp["options"]}}})
        self.store.event_add(job_id, "log",
                             f"decision point ({dp['decision_type']}): waiting "
                             f"for user - see Approvals/Decisions ({d_id})")
        return d_id

    def _auto_decide(self, job_id: str, stage: dict, dp: dict) -> tuple[str, str]:
        """Returns (decision_id, chosen_option_id)."""
        default = next(o["id"] for o in dp["options"] if o.get("policy_default"))
        d_id = new_id("dec")
        self.store.decision_add({
            "id": d_id, "ts": now(), "decided_at": now(),
            "job_id": job_id, "stage": stage["name"],
            "decision_type": dp["decision_type"], "question": dp["question"],
            "options": dp["options"], "chosen": default,
            "rationale": ("autonomous mode: applied documented policy "
                          "default (not a scientific validation claim) - "
                          + dp["rationale_default"]),
            "evidence": dp.get("evidence", []), "policy_id": dp.get("policy_id"),
            "policy_version": POLICY_VERSION, "decided_by": "engine",
            "status": "resolved",
            "key": dp.get("key"), "category": dp.get("category", "pipeline"),
            "trigger": dp.get("trigger"), "source": dp.get("source"),
            "mandatory": dp.get("mandatory", True),
            "effect_class": dp.get("effect_class")})
        self.store.event_add(job_id, "log",
                             f"autonomous: resolved {dp['decision_type']} -> "
                             f"'{default}' (policy default, recorded)")
        return d_id, default

    def _apply_effect(self, job_id: str, d_id: str | None, dp: dict,
                      choice: str) -> dict:
        """Look up what the chosen option maps to and record it on the
        decision row - the decision history then states, per decision,
        exactly what downstream configuration the choice produced."""
        effect = {}
        for o in dp["options"]:
            if o["id"] == choice:
                effect = o.get("effect", {})
                break
        if d_id:
            self.store.decision_set_effect(d_id, {
                "chosen": choice, "applied_effect": effect,
                "effect_class": dp.get("effect_class"),
                "applied_at": now()})
        self.store.event_add(job_id, "log",
                             f"decision effect applied ({dp.get('effect_class')}): "
                             f"{json.dumps(effect, ensure_ascii=False)}")
        return effect

    def _await_decision(self, job_id: str, d_id: str) -> str:
        """Block the worker until the user resolves the decision (or the job
        is cancelled). Returns the chosen option id ('aborted' on cancel)."""
        while True:
            res = self.resolutions.get(job_id, {}).get(d_id)
            if res is not None:
                return res
            if self._flag(job_id, "cancel"):
                return "aborted"
            time.sleep(0.25)

    # ----- native engine worker (Milestone 5 PART 1: one task system,
    # two real engines). Nothing here reimplements NeoAG's planner or
    # scientific routing; native code decides routes and stages. -----
    def _poll_flag(self, job_id: str):
        return "cancel" if self._flag(job_id, "cancel") else None

    def _cancel_native(self, job_id: str, note: str) -> None:
        self.store.job_set(job_id, state="cancelled", finished_at=now(),
                           error=note)
        self.store.event_add(job_id, "state", f"cancelled: {note}")
        self.publish({"type": "job", "id": job_id})

    def _fail_native(self, job_id: str, outdir: str, profile: str,
                     code: str, message: str, detail) -> None:
        # register whatever the attempt really produced (forensics), then
        # fail honestly - a failed native run is NEVER downgraded into a
        # mock success.
        arts, _un = native_mod.collect_artifacts(outdir)
        self._register_native_artifacts(job_id, arts, profile)
        err = f"NATIVE FAILURE {code}: {message}"[:500]
        self.store.job_set(job_id, state="failed", finished_at=now(),
                           outcome="generation_failed", error=err,
                           result_json=json.dumps({
                               "engine": "native",
                               "engine_version": NATIVE_ENGINE_VERSION,
                               "native_profile": profile, "ok": False,
                               "failure_code": code,
                               "failure_detail": str(detail or "")[:1500],
                               "outdir": outdir}, ensure_ascii=False))
        self.store.event_add(job_id, "state", f"failed: {err}")
        self.publish({"type": "job", "id": job_id})

    def _register_native_artifacts(self, job_id: str, arts: list,
                                   profile: str) -> list:
        """Register REAL files produced by native execution: path, size,
        sha256, row counts for tables. Availability status is derived
        from the file itself - never assumed."""
        tmap = {"tsv": "candidate_table", "csv": "candidate_table",
                "json": "json", "md": "plan_md", "log": "log",
                "html": "report_html", "txt": "text",
                "xls": "prediction_table", "vcf": "vcf",
                "toml": "config"}
        registered = []
        for a in arts:
            name = a["name"]
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            status = "available" if a["bytes"] > 0 else "empty"
            rows = None
            if ext in ("tsv", "csv") and 0 < a["bytes"] < 20_000_000:
                try:
                    with open(a["path"], "rb") as f:
                        rows = sum(1 for _ in f) - 1
                except OSError:
                    rows = None
                if rows is not None and rows <= 0:
                    status = "empty"
            registered.append(self.store.artifact_add(
                job_id, tmap.get(ext, "file"), name, None, a["path"],
                status, rows, a["sha256"][:16],
                {"native": True, "profile": profile,
                 "sha256_full": a["sha256"]}))
        for a in registered[:60]:
            self.store.event_add(
                job_id, "log",
                f"native artifact: {a['name']} [{a['status']}]"
                + (f" {a['rows']} row(s), checksum {a['checksum']}"
                   if a["rows"] else ""))
        if len(registered) > 60:
            self.store.event_add(job_id, "log",
                                 f"({len(registered) - 60} more artifact "
                                 "lines omitted from the event log)")
        return registered

    def _finalize_native(self, job_id: str, outdir: str, profile: str,
                         params: dict, ok: bool, failure, gw) -> None:
        arts, unreadable = native_mod.collect_artifacts(outdir)
        registered = self._register_native_artifacts(job_id, arts, profile)

        # Deliverable classes follow the NATIVE output layout (verified
        # against the demo run's real files): generated HTML reports,
        # the scoring/ranking tables and run provenance. Empty
        # AUXILIARY tables (e.g. ccf_conflicts.tsv = "no conflicts")
        # stay registered with their honest 'empty' status and are
        # reported as warnings - a no-finding table must not silently
        # vanish, but it also does not fake a delivery gap.
        def _deliverable(name: str) -> bool:
            return (name.startswith("reports/") and name.endswith(".html")
                    or name.startswith("scoring/")
                    or name.startswith("ranking/")
                    or name == "provenance.json")
        deliverables = [a for a in registered if _deliverable(a["name"])]
        aux_empty = [a["name"] for a in registered
                     if a["status"] == "empty" and not _deliverable(a["name"])]
        warnings = []
        if aux_empty:
            warnings.append(
                f"{len(aux_empty)} auxiliary table(s) legitimately contain "
                "zero rows (no-finding outputs such as conflicts tables); "
                "listed per artifact: " + ", ".join(aux_empty[:8])
                + ("..." if len(aux_empty) > 8 else ""))
        if profile == "demo":
            warnings.append(
                "native 'demo' profile: the REAL NeoAG pipeline runner "
                "(neoag.cli run-demo) executed on the deployed repo's "
                "bundled fixture inputs with predictors explicitly "
                "stubbed - a genuine native execution and real artifacts, "
                "but scientifically demo data, not a patient result")
        if unreadable:
            warnings.append(f"{len(unreadable)} produced file(s) could not "
                            "be read for checksumming (listed in result)")
        if ok:
            outcome = classify_outcome(deliverables or registered)
            result = {"engine": "native",
                      "engine_version": NATIVE_ENGINE_VERSION,
                      "native_profile": profile, "ok": True,
                      "gateway_job_id": gw,
                      "artifact_count": len(registered),
                      "deliverable_count": len(deliverables),
                      "unreadable": unreadable[:10],
                      "warnings": warnings, "outdir": outdir,
                      "inputs": params}
            self.store.job_set(job_id, state="completed", finished_at=now(),
                               outcome=outcome,
                               result_json=json.dumps(result,
                                                      ensure_ascii=False))
            self.store.event_add(job_id, "state",
                                 f"completed (native {profile}) - outcome "
                                 f"{outcome}; {len(registered)} real "
                                 f"artifact(s) registered under {outdir}")
        else:
            result = {"engine": "native",
                      "engine_version": NATIVE_ENGINE_VERSION,
                      "native_profile": profile, "ok": False,
                      "gateway_job_id": gw,
                      "failure_code": (failure or {}).get("code"),
                      "failure_detail": (failure or {}).get("detail"),
                      "artifact_count": len(registered),
                      "warnings": warnings, "outdir": outdir,
                      "inputs": params}
            self.store.job_set(job_id, state="failed", finished_at=now(),
                               outcome="generation_failed",
                               error="NATIVE FAILURE "
                                     f"{(failure or {}).get('code')}"[:200],
                               result_json=json.dumps(result,
                                                      ensure_ascii=False))
            self.store.event_add(job_id, "state",
                                 "failed: native execution reported failure "
                                 f"({(failure or {}).get('code')}) - partial "
                                 "outputs, if any, registered for forensics")
        self.publish({"type": "job", "id": job_id})

    def _run_native(self, job: dict, spec: dict) -> None:
        job_id = job["id"]
        profile = spec.get("native_profile", "demo")
        params = dict(spec.get("native_params") or {})
        params.setdefault("case_id", job["sample_id"])
        sample_id = job["sample_id"]
        cap = native_mod.probe()
        outdir = native_mod.job_outdir(job_id)
        # A5 snapshot, same write-once discipline as mock runs: freezes
        # the toolchain coordinates this real run actually saw.
        self.store.snapshot_add(job_id, {
            "captured_at": now(), "job_id": job_id, "kind": job["kind"],
            "sample_id": sample_id, "conv_id": job.get("conv_id") or "",
            "engine": "native", "engine_version": NATIVE_ENGINE_VERSION,
            "native_profile": profile, "native_params": params,
            "requested_config": spec.get("requested_config") or {},
            "execution_mode_at_start": "autonomous",
            "policy_version": POLICY_VERSION,
            "toolchain": cap.get("toolchain"),
            "gateway_url": cap.get("gateway", {}).get("url"),
            "cancel_supported_by_native_gateway":
                cap.get("cancel_supported_by_native_gateway"),
        })
        self.store.job_set(job_id, state="running", started_at=now(),
                           stage="native")
        self.store.event_add(job_id, "state",
                             f"running: NATIVE engine ({profile} profile) - "
                             "no mock fallback exists; failures stay failed. "
                             "Per-run snapshot recorded.")
        self.publish({"type": "job", "id": job_id})
        if not cap["available"]:
            return self._fail_native(
                job_id, outdir, profile, "NATIVE_UNAVAILABLE",
                "native NeoAG source/python is not importable on this host; "
                "the Web UI refuses to silently fall back to mock (Milestone "
                "5 1.6)", cap)
        if profile == "demo":
            self.store.event_add(job_id, "log",
                "native 'demo' profile: runs the real NeoAG pipeline "
                "(neoag.cli run-demo) from the deployed source on bundled "
                "fixtures, predictors stubbed; cancel works here because it "
                "is our supervised subprocess")
            res = native_mod.run_demo(outdir, sample_id=sample_id,
                                      poll=lambda: self._poll_flag(job_id))
            if res.get("status") == "DEMO_CANCELLED":
                return self._cancel_native(
                    job_id, "native demo process terminated (SIGTERM)")
            return self._finalize_native(
                job_id, outdir, profile, params, res["ok"],
                None if res["ok"] else {
                    "code": res.get("status") or f"rc={res.get('rc')}",
                    "detail": (res.get("error")
                               or res.get("log_tail", ""))[-800:]}, None)
        # ---- gateway profile ----
        self.store.event_add(job_id, "log",
            "native 'gateway' profile: real native planner first "
            "(read-only), then execution submitted through NeoAG's "
            "controlled-execution gateway; failures are reported, never "
            "rerouted to the mock engine")
        plan = native_mod.run_plan(params, os.path.join(outdir, "plan"))
        self.store.job_set(job_id,
                           native_plan_json=json.dumps(plan)[:400000])
        steps = plan.get("steps") or []
        self.store.event_add(job_id, "log",
            f"native plan: status={plan.get('status')} steps={len(steps)} "
            f"run_id={plan.get('run_id')} (full plan stored on the job)")
        for s in steps:
            self.store.event_add(job_id, "log",
                f"  plan step {s.get('step_id')} {s.get('name')}: "
                f"{s.get('status')}"
                + (f" - {str(s.get('detail'))[:160]}" if s.get("detail")
                   else ""))
        if plan.get("blocking_issues"):
            self.store.event_add(job_id, "log",
                "plan blocking issues: "
                + ", ".join(str(b) for b in plan["blocking_issues"]))
        self.publish({"type": "job", "id": job_id})
        if not plan.get("ok"):
            return self._fail_native(
                job_id, outdir, profile,
                "PLAN_" + str(plan.get("status")),
                "native planner did not clear the run - NOTHING was "
                "executed (plan outputs retained)", plan)
        if self._flag(job_id, "cancel"):
            return self._cancel_native(job_id,
                "cancelled after planning, before any execution")
        ex = native_mod.submit_execute(params, outdir,
                                       stub=bool(spec.get("stub")))
        if not ex.get("ok"):
            return self._fail_native(job_id, outdir, profile,
                str(ex.get("status")), "native gateway submission failed",
                ex.get("detail"))
        gw = ex["gw_job_id"]
        self.store.job_set(job_id, gw_job_id=gw)
        self.store.event_add(job_id, "log",
            f"submitted to native gateway: job {gw} (risk "
            f"{ex.get('risk')}). HONEST LIMITATION: the native gateway has "
            "no cancel endpoint (verified against its source) - cancelling "
            "this Web job will not stop the gateway job.")
        self.publish({"type": "job", "id": job_id})
        last = None
        final = None
        deadline = time.time() + 4 * 3600
        while time.time() < deadline:
            js = native_mod.gw_status(gw)
            stt = js.get("status")
            if stt != last:
                self.store.event_add(job_id, "log",
                                     f"gateway job {gw}: {stt}")
                last = stt
                self.publish({"type": "job", "id": job_id})
            if stt not in ("QUEUED", "RUNNING", "STATUS_UNREACHABLE"):
                final = js
                break
            if self._flag(job_id, "cancel"):
                return self._cancel_native(job_id,
                    f"Web job cancelled; GATEWAY JOB {gw} KEEPS RUNNING to "
                    "completion (native gateway has no cancel route - "
                    "verified). Its outputs remain on disk under "
                    f"{outdir} and stay linked here.")
            time.sleep(5)
        if final is None:
            return self._fail_native(job_id, outdir, profile,
                "GATEWAY_MONITOR_TIMEOUT",
                f"gateway job {gw} was still not terminal after 4h of "
                "monitoring", None)
        ok = final.get("status") == "PASS"
        return self._finalize_native(
            job_id, outdir, profile, params, ok,
            None if ok else {"code": str(final.get("status")),
                             "detail": str(final.get("failure_reason")
                                           or "")[:600]}, gw)

    def _run(self, job_id: str) -> None:
        job = self.store.job_get(job_id)
        if not job:
            return
        spec = json.loads(job["spec_json"] or "{}")
        if spec.get("engine") == "native":
            return self._run_native(job, spec)
        if spec.get("engine") == "benchmark":
            return self._run_benchmark(job, spec)
        stage_ids = spec.get("stage_ids", [])
        exec_mode = spec.get("execution_mode", "controlled")
        rng = random.Random(job_id)
        total = sum(STAGE_WEIGHTS.get(s, 5) for s in stage_ids)
        done = 0
        skipped_essential: list[str] = []
        failed_stage: dict | None = None
        chosen_by_key: dict[str, str] = {}
        # Immutable per-run snapshot (A5): inputs, versions, requested
        # config and the decision-option map AS OF THIS RUN. Written once
        # (INSERT OR IGNORE); policy/default changes afterwards cannot
        # retroactively alter what this run recorded.
        self.store.snapshot_add(job_id, {
            "captured_at": now(), "job_id": job_id,
            "kind": job["kind"], "sample_id": job["sample_id"],
            "conv_id": job.get("conv_id") or "",
            "manifest_id": spec.get("manifest_id"),
            "stage_plan": stage_ids,
            "requested_config": spec.get("requested_config") or {},
            "execution_mode_at_start": exec_mode,
            "simulate_failure": bool(spec.get("simulate_failure")),
            "engine_version": ENGINE_VERSION,
            "policy_version": POLICY_VERSION,
            "dataset_version": spec.get("dataset_version"),
            "toolchain": MOCK_TOOLCHAIN,
            "seed": f"{job_id}:{job['sample_id']}",
            "decision_point_definitions": {
                sid: {"key": dp.get("key"), "policy": dp.get("policy_id"),
                      "policy_version": POLICY_VERSION,
                      "option_effects": {o["id"]: o.get("effect", {})
                                         for o in dp["options"]}}
                for sid, dp in DECISION_POINTS.items()},
        })
        self.store.job_set(job_id, state="running", started_at=now())
        self.store.event_add(job_id, "state",
                             f"running ({exec_mode} mode) - MOCK execution, no "
                             "real pipeline connected (per-run snapshot "
                             "recorded: config + versions are frozen for "
                             "this run)")
        self.publish({"type": "job", "id": job_id})
        for sid in stage_ids:
            stage = mock_data.STAGE_BY_ID.get(sid)
            if not stage:
                continue
            # ---- mode switch at safe boundary ----
            req_mode = self.mode_requests.pop(job_id, None)
            if req_mode and req_mode != exec_mode:
                self.store.decision_add({
                    "id": new_id("dec"), "ts": now(), "decided_at": now(),
                    "job_id": job_id, "stage": stage["name"],
                    "decision_type": "mode_switch", "question":
                    "Switch execution mode mid-run?",
                    "options": [{"id": exec_mode, "label": f"stay {exec_mode}"},
                                {"id": req_mode, "label": f"switch to {req_mode}"}],
                    "chosen": req_mode,
                    "rationale": "user requested; applied at stage boundary",
                    "evidence": [f"previous mode {exec_mode}",
                                 "identical validity gates in both modes"],
                    "policy_id": None, "policy_version": POLICY_VERSION,
                    "decided_by": "user", "status": "resolved",
                    "key": "execution.mode", "category": "pipeline",
                    "trigger": "user requested a mode switch mid-run",
                    "source": "UI mode control",
                    "mandatory": False,
                    "effect_class": "execution_mode",
                    "downstream": {"chosen": req_mode,
                                   "applied_effect": {"execution_mode":
                                                      req_mode}}})
                exec_mode = req_mode
                self.store.event_add(job_id, "state",
                                     f"mode switched to {exec_mode} at stage "
                                     f"boundary '{stage['name']}'")
                self.publish({"type": "job", "id": job_id})
            self.store.job_set(job_id, stage=stage["name"])
            self.store.event_add(job_id, "stage",
                                 f"MOCK stage start: {stage['name']} ({stage['note']})")
            self.publish({"type": "job", "id": job_id})
            # ---- decision point (both modes; resolver differs) ----
            dp = DECISION_POINTS.get(sid)
            if dp:
                if exec_mode == "controlled":
                    d_id = self._make_decision(job_id, stage, dp)
                    self.store.job_set(job_id, state="blocked")
                    self.store.event_add(job_id, "state",
                                         "blocked: awaiting your decision")
                    self.publish({"type": "job", "id": job_id})
                    choice = self._await_decision(job_id, d_id)
                    self.resolutions.get(job_id, {}).pop(d_id, None)
                    if choice == "aborted":
                        self.store.job_set(job_id, state="cancelled",
                                           finished_at=now(),
                                           error="cancelled while awaiting decision")
                        self.publish({"type": "job", "id": job_id})
                        return
                    self.store.job_set(job_id, state="running")
                    self.store.event_add(job_id, "state",
                                         f"resumed: decision '{choice}' applied")
                    self.publish({"type": "job", "id": job_id})
                else:
                    d_id, choice = self._auto_decide(job_id, stage, dp)
                if choice != "skip_stage":
                    self._apply_effect(job_id, d_id, dp, choice)
                    chosen_by_key[dp["key"]] = choice
                if choice == "skip_stage":
                    if sid in REQUIRED_STAGES:
                        skipped_essential.append(stage["name"])
                        self.store.event_add(
                            job_id, "log",
                            f"WARNING: essential stage '{stage['name']}' skipped "
                            "by explicit user decision - run will be marked "
                            "production_release=false")
                    else:
                        self.store.event_add(job_id, "log",
                                             f"optional stage '{stage['name']}' "
                                             "skipped by user decision")
                    done += STAGE_WEIGHTS.get(sid, 5)
                    self.store.job_set(job_id, progress=round(100.0 * done / total, 1))
                    continue
            # ---- substeps, then bounded recovery on injected failure ----
            n = STAGE_WEIGHTS.get(sid, 5)
            attempts = 0
            while True:
                stopped = "ok"
                for k in range(n):
                    stopped = self._sleep_checked(job_id, STEP_SLEEP)
                    if stopped != "ok":
                        break
                    if k == 0:
                        self.store.event_add(job_id, "log", STAGE_LINES.get(sid, "(mock)"))
                    else:
                        self.store.event_add(
                            job_id, "log",
                            rng.choice([
                                f"  processing shard {k + 1}/{n} (mock)",
                                f"  {int(rng.uniform(50, 4000))} rows buffered (mock)",
                                "  checkpoint updated (mock)",
                            ]))
                    done += 1
                    self.store.job_set(job_id, progress=round(min(
                        100.0, 100.0 * done / total), 1))
                    self.publish({"type": "job", "id": job_id})
                if stopped == "cancel":
                    self.store.job_set(job_id, state="cancelled", finished_at=now(),
                                       error="cancelled by user during stage "
                                             f"{stage['name']} (partial mock output discarded)")
                    self.store.event_add(job_id, "state", "cancelled by user")
                    self.store.audit("engine", "job.cancelled", job_id, {"stage": sid})
                    self.publish({"type": "job", "id": job_id})
                    return
                if stopped == "pause":
                    self.store.job_set(job_id, state="paused")
                    self.store.event_add(job_id, "state",
                                         f"paused at stage boundary: {stage['name']}")
                    self.publish({"type": "job", "id": job_id})
                    while self._flag(job_id, "pause"):
                        if self._flag(job_id, "cancel"):
                            break
                        time.sleep(0.3)
                    if self._flag(job_id, "cancel"):
                        self.store.job_set(job_id, state="cancelled", finished_at=now(),
                                           error="cancelled by user while paused")
                        self.store.event_add(job_id, "state", "cancelled by user")
                        self.publish({"type": "job", "id": job_id})
                        return
                    self.store.job_set(job_id, state="running")
                    self.store.event_add(job_id, "state", "resumed")
                    self.publish({"type": "job", "id": job_id})
                # ---- demo failure + bounded recovery (mode-dependent) ----
                if sid == "presentation" and spec.get("simulate_failure"):
                    gate = ("production gate would BLOCK release (POL-001): "
                            "NetMHCstabpan stability result missing")
                    if exec_mode == "autonomous":
                        if attempts >= 1:
                            self.store.event_add(job_id, "error",
                                                 gate + " - retry did not "
                                                 "recover; stopping instead of "
                                                 "silently skipping (POL-001)")
                            failed_stage = stage
                            break
                        attempts += 1
                        self.store.event_add(job_id, "log",
                                             "autonomous bounded recovery: "
                                             "retrying stage once (mock)")
                        continue
                    # controlled mode: block and ask (retry / skip / terminate);
                    # retry is bounded - the option disappears after one use
                    opts = [
                        {"id": "retry", "label": "Retry stage once (re-run "
                         "this stage; bounded recovery)",
                         "policy_default": True,
                         "effect": {"action": "re-run this stage once"}},
                        {"id": "skip", "label": "Skip stage - allowed only "
                         "as explicit override; run marked "
                         "production_release=false",
                         "policy_default": False,
                         "effect": {"action": "skip stage",
                                    "production_release": False}},
                        {"id": "terminate", "label": "Terminate run",
                         "policy_default": False,
                         "effect": {"action": "fail the run"}},
                    ] if attempts < 1 else [
                        {"id": "skip", "label": "Skip stage - allowed only "
                         "as explicit override; run marked "
                         "production_release=false (retry already used)",
                         "policy_default": False,
                         "effect": {"action": "skip stage",
                                    "production_release": False}},
                        {"id": "terminate", "label": "Terminate run",
                         "policy_default": False,
                         "effect": {"action": "fail the run"}},
                    ]
                    d_id = new_id("dec")
                    self.store.decision_add({
                        "id": d_id, "ts": now(), "decided_at": None,
                        "job_id": job_id, "stage": stage["name"],
                        "decision_type": "error_recovery",
                        "question": f"Stage '{stage['name']}' failed (mock). "
                        "How should the run continue?",
                        "options": opts,
                        "chosen": None, "rationale": None,
                        "evidence": ["mock failure injection at presentation",
                                     "policy: missing stability evidence blocks "
                                     "production release (POL-001)"],
                        "policy_id": "POL-001", "policy_version": POLICY_VERSION,
                        "decided_by": None, "status": "pending",
                        "key": f"recovery.{sid}", "category": "pipeline",
                        "trigger": f"stage '{sid}' failed (mock injection)",
                        "source": "jobs.py failure path (POL-001)",
                        "mandatory": True,
                        "effect_class": "error_recovery",
                        "downstream": {"option_effects":
                                       {o["id"]: o.get("effect", {})
                                        for o in opts}}})
                    self.store.job_set(job_id, state="blocked", error=gate
                                       + " - awaiting decision")
                    self.store.event_add(job_id, "error", gate)
                    self.publish({"type": "job", "id": job_id})
                    choice = self._await_decision(job_id, d_id)
                    self.resolutions.get(job_id, {}).pop(d_id, None)
                    chosen_effect = next(
                        (o.get("effect", {}) for o in opts
                         if o["id"] == choice), {"action": "aborted"})
                    self.store.decision_set_effect(d_id, {
                        "chosen": choice, "applied_effect": chosen_effect,
                        "effect_class": "error_recovery", "applied_at": now()})
                    if choice == "aborted":
                        self.store.job_set(job_id, state="failed",
                                           finished_at=now(),
                                           error=gate + " - run terminated")
                        self.publish({"type": "job", "id": job_id})
                        return
                    if choice == "retry":
                        attempts += 1
                        self.store.job_set(job_id, state="running", error=None)
                        self.store.event_add(job_id, "log",
                                             "retry requested - re-running this "
                                             "stage (recovery bounded to one "
                                             "attempt)")
                        self.publish({"type": "job", "id": job_id})
                        continue
                    if choice == "skip":
                        skipped_essential.append(stage["name"])
                        self.store.event_add(job_id, "log",
                                             "stage skipped by explicit user "
                                             "decision; run marked "
                                             "production_release=false")
                        self.store.job_set(job_id, state="running", error=None)
                        self.publish({"type": "job", "id": job_id})
                        break
                    self.store.job_set(job_id, state="failed", finished_at=now(),
                                       error=gate + " - terminated by user decision")
                    self.publish({"type": "job", "id": job_id})
                    return
                break
            if failed_stage is not None:
                break
        # ---- completion ----
        if failed_stage is not None:
            self.store.job_set(job_id, state="failed", finished_at=now(),
                               error=failed_stage["name"] + ": " + (
                                   "stability evidence missing - autonomous "
                                   "bounded recovery exhausted; stopping rather "
                                   "than silently skipping (POL-001)"))
            self.publish({"type": "job", "id": job_id})
            return
        # ---- execution completed: generate the OUTPUT artifacts the
        # decisions asked for, register each with an honest availability
        # status, then classify the run's outcome (A1/A2/A3). The job's
        # lifecycle state stays 'completed' (execution really finished);
        # what EXISTS and is AVAILABLE is whatever the artifacts table
        # shows - never an assumption.
        release_ok = not skipped_essential
        warnings: list[str] = []
        gate_rank = GATE_RANKS.get(chosen_by_key.get(
            "presentation.stability_gate", "gate14"), 1.4)
        want = RANKING_ARTIFACTS.get(chosen_by_key.get(
            "ranking.outputs", "both"), ["weighted", "evidence-consensus"])
        if chosen_by_key.get("input.rna_pair") == "legacy":
            warnings.append("legacy RNA pair chosen by decision: expression "
                            "evidence carries a labelling-ambiguity caveat")
        if gate_rank > 1.4:
            warnings.append(f"stability gate relaxed to {gate_rank} by "
                            "decision: evidence tier is exploratory")
        self.store.event_add(job_id, "log",
                             "execution completed - generating output "
                             f"artifacts per decisions (gate={gate_rank}, "
                             f"ranking={'+'.join(want)})")
        self.publish({"type": "job", "id": job_id})
        registered: list[dict] = []
        tables: dict[str, list[dict]] = {}
        gen_meta: dict[str, dict] = {}
        try:
            tables, gen_meta = mock_data.make_rankings(
                job_id, job["sample_id"], top_n=int(spec.get("top_n", 12)),
                gate_rank=gate_rank, want=want)
        except Exception as e:            # honest generation-failure path
            for strat in want:
                registered.append(self.store.artifact_add(
                    job_id, "candidate_table", strat + ".tsv", strat,
                    f"(mock)/{job_id}/ranking/{strat}.tsv",
                    "failed_generation", None, None,
                    {"error": str(e)[:300]}))
            warnings.append(f"output generation failed: {e}")
        for strat in want:
            rows = tables.get(strat)
            if rows is None:      # generator produced no table for a
                                   # requested artifact: that is a real gap
                registered.append(self.store.artifact_add(
                    job_id, "candidate_table", f"{strat}_ranking.tsv", strat,
                    f"(mock)/{job_id}/ranking/{strat}.tsv",
                    "missing", None, None,
                    {"reason": "output generator returned no table for this "
                               "requested artifact"}))
                continue
            status = "available" if rows else "empty"
            checksum = hashlib.sha256(
                json.dumps(rows, sort_keys=True, ensure_ascii=False)
                .encode("utf-8")).hexdigest()[:16]
            meta = {"generated_by": ENGINE_VERSION, **gen_meta.get(strat, {}),
                    "rule": ("mock weighted composite score, rows below the "
                             "chosen stability gate excluded (counted)"
                             if strat == "weighted" else
                             f"mock consensus rules >= "
                             f"{mock_data.CONSENSUS_TIER}/7")}
            if meta.get("below_gate_excluded"):
                warnings.append(
                    f"{meta['below_gate_excluded']} candidate(s) excluded "
                    f"from the weighted table by stability gate "
                    f"{gate_rank} (rule recorded in artifact meta)")
            registered.append(self.store.artifact_add(
                job_id, "candidate_table", f"{strat}_ranking.tsv", strat,
                f"(mock)/{job_id}/ranking/{strat}.tsv",
                status, len(rows), checksum, meta))
        for a in registered:
            self.store.event_add(
                job_id, "log",
                f"artifact registered: {a['name']} [{a['status']}] "
                + (f"{a['rows']} row(s), checksum {a['checksum']}"
                   if a["status"] == "available" else
                   ("0 rows - zero candidates is a real result, not hidden"
                    if a["status"] == "empty" else
                    ("requested artifact missing - NOT deliverable"
                     if a["status"] == "missing" else "generation failed"))))
        outcome = classify_outcome(registered)
        if not release_ok:
            warnings.append("essential stage(s) skipped by decision: "
                            + ", ".join(skipped_essential))
        outcome_notes = {
            "delivered": "all requested output artifacts generated and "
                         "registered as available",
            "zero_candidates": "execution completed; every requested "
                               "candidate table legitimately contains ZERO "
                               "candidates - this is a result, not a failure",
            "partial": "execution completed, but at least one requested "
                       "output artifact is empty/missing - results shown "
                       "are PARTIAL",
            "generation_failed": "execution completed but output "
                                 "GENERATION failed - there are no usable "
                                 "results despite completed execution",
            "no_artifacts": "execution completed but produced no output "
                            "artifacts at all - nothing is deliverable"}
        if release_ok:
            warnings.append("production release permitted (mock gate)")
        primary = next((t for t in ("weighted", "evidence-consensus")
                        if t in tables), None)
        primary_rows = tables.get(primary, []) if primary else []
        decisions_summary = [
            {"key": d.get("key"), "decision_type": d["decision_type"],
             "question": d["question"], "chosen": d["chosen"],
             "decided_by": d["decided_by"], "policy_id": d.get("policy_id"),
             "policy_version": d.get("policy_version")}
            for d in self.store.decision_list(job_id)
            if d["status"] == "resolved"]
        effective_config = dict(spec.get("requested_config") or {})
        effective_config.update({
            "stability_max_rank": gate_rank,
            "ranking_artifacts": want,
            "rna_pair": chosen_by_key.get("input.rna_pair", "matched"),
            "execution_mode_final": exec_mode,
            "top_n": int(spec.get("top_n", 12))})
        unavailable = [a["name"] for a in registered
                       if a["status"] != "available"]
        result = {
            "disclaimer": ("COMPUTATIONAL PREDICTIONS ON MOCK DATA - not "
                           "experimentally validated, not clinical results"),
            "dataset_version": spec.get("dataset_version"),
            "production_release": release_ok,
            "release_note": ("production release permitted (mock gate)" if
                             release_ok else
                             "production release BLOCKED: essential stage(s) "
                             f"{', '.join(skipped_essential)} skipped by explicit "
                             "user decision (recorded in decision history)"),
            "outcome": outcome,
            "outcome_note": outcome_notes[outcome],
            "delivery_claim": ("this run DELIVERS: " + ", ".join(
                a["name"] for a in registered
                if a["status"] == "available")) if any(
                a["status"] == "available" for a in registered) else
                "this run delivers NOTHING - see outcome_note",
            "unavailable_artifacts": unavailable,
            "warnings": warnings,
            "manifest": {
                "job_id": job_id,
                "manifest_ref": "artifacts table, job_id=" + job_id,
                "requested_config": spec.get("requested_config") or {},
                "effective_config": effective_config,
                "requested_vs_effective_note": (
                    "effective_config merges the requested config with the "
                    "decisions actually applied by this run; keys the mock "
                    "engine does not consume stay listed as requested"),
                "execution_mode": exec_mode,
                "versions": {"engine": ENGINE_VERSION,
                             "policy": POLICY_VERSION,
                             "dataset": spec.get("dataset_version"),
                             "toolchain": MOCK_TOOLCHAIN},
                "artifacts": [{"id": a["id"], "name": a["name"],
                               "type": a["type"], "strategy": a["strategy"],
                               "path": a["path"], "status": a["status"],
                               "rows": a["rows"], "checksum": a["checksum"],
                               "generated_at": a["created_at"]}
                              for a in registered],
                "decisions": decisions_summary,
                "snapshot": "run_snapshots table, job_id=" + job_id,
                "generated_at": now()},
            "candidates": primary_rows,
            "candidates_strategy": primary,
            # A2: choosing "both" rankings must SHOW both tables;
            # each strategy's rows travel with the run result.
            "candidates_by_strategy": tables,
            "provenance": {"job_id": job_id, "engine": ENGINE_VERSION,
                           "sample": job["sample_id"],
                           "execution_mode": exec_mode,
                           "generator_seed": f"{job_id}:{job['sample_id']}"}}
        self.store.job_set(job_id, state="completed", finished_at=now(),
                           progress=100.0, stage=None, outcome=outcome,
                           result_json=json.dumps(result, ensure_ascii=False))
        self.store.event_add(job_id, "state",
                             f"completed (mock): outcome={outcome} - "
                             + ", ".join(f"{a['name']}[{a['status']}]"
                                         for a in registered)
                             + ("" if release_ok else " - release blocked"))
        self.store.audit("engine", "job.completed", job_id,
                         {"production_release": release_ok,
                          "outcome": outcome,
                          "artifacts": [a["status"] for a in registered]})
        self.publish({"type": "job", "id": job_id})
