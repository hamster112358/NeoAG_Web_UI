---
name: neoag-operations
description: Operational playbook for NeoAG neoantigen pipeline work on this server - interpreting analysis requests, locating inputs, selecting documented defaults, running/diagnosing stages, recording decisions and provenance, and handling destructive operations. Use for ANY NeoAG/pipeline/neoantigen task.
---

# NeoAG operations playbook

Complements the always-loaded workspace rules; do not restate them.

## Interpreting an analysis request
1. Identify the case: sample IDs, tumor/normal/RNA inputs, HLA type
   source, desired scope (single stage vs full workflow).
2. Locate inputs by directory listing / file-name patterns / existing
   manifests. Verify existence and basic sanity (non-empty, readable).
   Never assume two similarly named files belong together - check
   read groups, headers, or paired metadata.
3. Build a short input manifest (role, path, confidence, basis) and
   state it back. If a role is ambiguous or missing, ask ONE question
   naming the candidates.
4. State the plan: stages to run, tool/reference versions, documented
   defaults you will use, expected outputs, rough run class (quick
   test vs production).

## Where things live
- Frozen pipeline reference: /data2/caojingqi/original_OpenNeoAG/neoag
  (READ ONLY - immutable by operator invariant; never write).
- Control copy: /data2/caojingqi/neoag_control (READ ONLY).
- Dev copy: /data2/caojingqi/neoag_dev (pipeline development work -
  any write here requires the user's explicit prior request; treat as
  high-impact).
- Tool environment: `source <repo>/conf/tools.env.sh` sets
  NEOAG_TOOLS_ROOT etc; the neoag-tools conda env is at
  /data1/neoag/miniforge3/envs/neoag-tools.
- Your writable job roots: this workspace, /tmp, and
  /data2/caojingqi/neoag_agent_runs/<slug>/ for anything a user asks
  you to compute.

## Running work
- Formal pipeline executions are registered, not launched: use
  `python3 .neoag/taskquery.py job submit '<json>'` (the execution
  gate blocks raw `open-neo run --mode execute` / `neoag.cli run*`
  calls - no approval path; `job status <id>` / `job events <id>`
  track the registered run). Planning first is encouraged and NOT
  blocked: `open-neo run --mode plan ...` executes nothing and shows
  the real route/steps.
- Prefer the documented CLI for inspection (read the repo's README and
  configs/workflows/production_workflow.example.toml for the current
  interface; do not guess flags - check --help and the example TOML).
- Non-patient test fixtures (e.g. HCC1395 public fixture, the dev
  tree's test data) are the correct targets for verification runs.
- Long-running stages: launch in the background with logs under the
  run directory, then poll status files; report progress and read
  errors from logs rather than guessing.
- For a stage failure: read the stage log tail first, match against
  the repo's troubleshooting docs, apply at most one concrete fix and
  one retry, then report exact errors.
- Public benchmark fixtures and demo/synthetic data are fine to
  generate. Do not start production patient runs unless the user
  explicitly asks; treat such a request as high-impact: restate case,
  scope and expected outputs before starting.

## Decisions and provenance (required, not optional)
- Every scientifically consequential choice gets a decisions.jsonl
  row (see workspace rules for the schema) in the run directory.
- Every run directory keeps commands.sh (what you ran, in order) and
  a README.md stub (inputs by path, versions, outputs list) so the
  run is reconstructable.
- When results depend on parameters, quote the effective values and
  where they came from (default file vs user override).

## Communicating results
- Lead with the answer/verdict, then evidence (paths, key numbers),
  then caveats. Label anything simulated, partial, or unvalidated -
  never present a mock or fixture result as a real analysis result.
- Ranking/shortlist outputs: distinguish which ranking layer produced
  them and note evidence gaps (missing RNA support, unassessed LOH,
  etc.) when they affect interpretation.

## Destructive operations
Follow the gate protocol in the workspace rules exactly (explain six
items, wait for the card, retry identical). Destructive means:
deleting paths outside your scratch areas, overwriting existing
valuable data, force/system/git-destructive operations. Creating and
overwriting your OWN files inside an authorized run directory is
routine and needs no ceremony.
