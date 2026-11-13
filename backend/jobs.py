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

import json
import random
import threading
import time

from . import mock_data
from .store import Store, new_id, now
from .policies import POLICY_VERSION

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
# options[0] is the VALIDATED default (mirrors POL-007 'validated defaults').
DECISION_POINTS = {
    "input_qc": {
        "decision_type": "input_mapping",
        "question": ("Two RNA FASTQ pairs in the mock inventory match this "
                     "sample. Which pair feeds the RNA expression branch?"),
        "options": [
            {"id": "matched", "label": "tumor.rna_R1/R2.fastq.gz (paired, "
             "sample-id matched, same run)", "validated": True},
            {"id": "legacy", "label": "legacy_run_R1/R2.fastq.gz (older run, "
             "labelling ambiguity)", "validated": False},
        ],
        "rationale_default": ("sample-id match and same sequencing run - "
                              "mirrors the route plan documented in Skill2"),
        "evidence": ["mock inventory: 2 candidate pairs", "sample-id prefix match",
                     "both pairs indexed (mock)"],
        "policy_id": "POL-007",
    },
    "presentation": {
        "decision_type": "parameter_choice",
        "question": "Stability gate threshold applied to presentation evidence.",
        "options": [
            {"id": "gate14", "label": "stability rank <= 1.4 (validated "
             "production gate)", "validated": True},
            {"id": "gate20", "label": "stability rank <= 2.0 (relaxed, "
             "exploratory only)", "validated": False},
        ],
        "rationale_default": ("1.4 is the deployed production criterion; the "
                              "relaxed option exists for exploration and "
                              "would flag results as non-production"),
        "evidence": ["mock: deployed gate max_stabpan_rank=1.4",
                     "relaxing the gate would change the evidence tier"],
        "policy_id": "POL-001",
    },
    "ranking": {
        "decision_type": "parameter_choice",
        "question": "Which ranking output(s) should the run produce?",
        "options": [
            {"id": "both", "label": "weighted + evidence-consensus (both, as "
             "production requires)", "validated": True},
            {"id": "weighted", "label": "weighted only (exploratory)",
             "validated": False},
        ],
        "rationale_default": ("production ranking emits weighted and "
                              "evidence-consensus rankings together"),
        "evidence": ["mock: consensus comparison is the review baseline"],
        "policy_id": "POL-003",
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
               requested_by: str = "user") -> dict:
        stages = [mock_data.STAGE_BY_ID[s] for s in stage_ids if s in mock_data.STAGE_BY_ID]
        if not stages:
            raise ValueError("no valid stages selected")
        if execution_mode not in ("autonomous", "controlled"):
            raise ValueError("execution_mode must be autonomous|controlled")
        spec = {"stage_ids": [s["id"] for s in stages], "mode": mode,
                "execution_mode": execution_mode,
                "simulate_failure": simulate_failure,
                "dataset_version": mock_data.DEMO_DATASET_VERSION}
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
        self.store.job_create(job_id, "mock-analysis", sample_id, spec,
                              "blocked", appr["id"])
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
            "status": "pending"})
        self.store.event_add(job_id, "log",
                             f"decision point ({dp['decision_type']}): waiting "
                             f"for user - see Approvals/Decisions ({d_id})")
        return d_id

    def _auto_decide(self, job_id: str, stage: dict, dp: dict) -> str:
        validated = next(o["id"] for o in dp["options"] if o.get("validated"))
        self.store.decision_add({
            "id": new_id("dec"), "ts": now(), "decided_at": now(),
            "job_id": job_id, "stage": stage["name"],
            "decision_type": dp["decision_type"], "question": dp["question"],
            "options": dp["options"], "chosen": validated,
            "rationale": ("autonomous mode: applied validated default - "
                          + dp["rationale_default"]),
            "evidence": dp.get("evidence", []), "policy_id": dp.get("policy_id"),
            "policy_version": POLICY_VERSION, "decided_by": "engine",
            "status": "resolved"})
        self.store.event_add(job_id, "log",
                             f"autonomous: resolved {dp['decision_type']} -> "
                             f"'{validated}' (validated default, recorded)")
        return validated

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
        self.store.job_set(job_id, state="running", started_at=now())
        self.store.event_add(job_id, "state",
                             f"running ({exec_mode} mode) - MOCK execution, no "
                             "real pipeline connected")
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
                    "decided_by": "user", "status": "resolved"})
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
                    choice = self._auto_decide(job_id, stage, dp)
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
                         "this stage; bounded recovery)", "validated": True},
                        {"id": "skip", "label": "Skip stage - allowed only "
                         "as explicit override; run marked "
                         "production_release=false", "validated": False},
                        {"id": "terminate", "label": "Terminate run",
                         "validated": True},
                    ] if attempts < 1 else [
                        {"id": "skip", "label": "Skip stage - allowed only "
                         "as explicit override; run marked "
                         "production_release=false (retry already used)",
                         "validated": False},
                        {"id": "terminate", "label": "Terminate run",
                         "validated": True},
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
                        "decided_by": None, "status": "pending"})
                    self.store.job_set(job_id, state="blocked", error=gate
                                       + " - awaiting decision")
                    self.store.event_add(job_id, "error", gate)
                    self.publish({"type": "job", "id": job_id})
                    choice = self._await_decision(job_id, d_id)
                    self.resolutions.get(job_id, {}).pop(d_id, None)
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
        rows = mock_data.make_candidates(job_id, job["sample_id"],
                                         top_n=int(spec.get("top_n", 12)))
        release_ok = not skipped_essential
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
            "candidates": rows,
            "provenance": {"job_id": job_id, "engine": "mock",
                           "sample": job["sample_id"],
                           "execution_mode": exec_mode,
                           "generator_seed": f"{job_id}:{job['sample_id']}"}}
        self.store.job_set(job_id, state="completed", finished_at=now(), progress=100.0,
                           stage=None, result_json=json.dumps(result, ensure_ascii=False))
        self.store.event_add(job_id, "state",
                             f"completed (mock): {len(rows)} synthetic candidates"
                             + ("" if release_ok else " - release blocked"))
        self.store.audit("engine", "job.completed", job_id,
                         {"production_release": release_ok})
        self.publish({"type": "job", "id": job_id})
