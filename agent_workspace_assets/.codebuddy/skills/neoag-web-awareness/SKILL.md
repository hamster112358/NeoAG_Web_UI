---
name: neoag-web-awareness
description: How to query the NeoAG Web backend authoritatively (formal tasks, artifacts, decisions, snapshots, research reports) instead of guessing from chat memory. Use for ANY question about tasks/analyses/reports this interface has run.
---

# NeoAG Web awareness

You are the agent inside the NeoAG Web interface. The interface's
persisted records - not your conversation memory - are authoritative
for what has run and what it produced.

## Task query API (official; read-only)
A helper is deployed at `.neoag/taskquery.py` (inside your workspace):
- `python3 .neoag/taskquery.py list [n]` - recent formal tasks
  (id, kind, state, outcome, sample, conv link, timestamps)
- `python3 .neoag/taskquery.py get <job_id>` - full detail: effective
  config, decision history with applied effects, artifact manifest
  with availability statuses, failure info
- `python3 .neoag/taskquery.py snapshot <job_id>` - the immutable
  per-run configuration snapshot
- `python3 .neoag/taskquery.py research [n]` /
  `research get <id>` - discovery/research reports and their coverage
It authenticates with the workspace task token and only performs GET
requests. Use it whenever asked about runs; never answer from chat
memory alone and never query the database directly.

## Resolving "the analysis I just ran"
Match candidate tasks by conversation link first, then recency, then
sample id. If MORE THAN ONE task plausibly matches, ask the user ONE
focused question naming the candidates (ids + sample + state + time).
Never silently assume the most recent run is the one they mean.

## Honesty about interface state
A completed task may have outcome delivered / zero_candidates /
partial / generation_failed / no_artifacts - report exactly what the
manifest says, including empty tables, and never describe an artifact
as delivered when its status is not 'available'.
