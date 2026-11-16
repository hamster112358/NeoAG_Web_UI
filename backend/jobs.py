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
import random
import threading
import time

from . import mock_data
from .store import Store, new_id, now
from .policies import POLICY_VERSION

# Version stamping for per-run snapshots: a recorded run keeps the version
# strings it actually ran under; later releases never rewrite them.
ENGINE_VERSION = "mock-job-engine-2026.11-r3"
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

    def _run(self, job_id: str) -> None:
        job = self.store.job_get(job_id)
        if not job:
            return
        spec = json.loads(job["spec_json"] or "{}")
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
