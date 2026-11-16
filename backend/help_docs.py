"""Help / Documentation content for the redesigned UI.

Serves REAL documentation, read-only:
 - the NeoAG repository README and the production-workflow doc from the
   frozen reference tree (never modified by this project), and
 - this app's own guide.

No patient data and no secrets are involved; these are public repo docs.
Files are read on demand and capped in size; unknown names answer 404.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

NEOAG_ROOT = Path("/data2/caojingqi/original_OpenNeoAG/neoag")
MAX_BYTES = 400_000

DOCS = {
    "app-guide": {
        "title": "Using the NeoAG Web workspace",
        "path": None,
    },
    "workflow": {
        "title": "NeoAG production workflow",
        "path": NEOAG_ROOT / "docs" / "PRODUCTION_WORKFLOW.md",
    },
    "repo-readme": {
        "title": "NeoAG repository README",
        "path": NEOAG_ROOT / "README.md",
    },
    "codebuddy": {
        "title": "CodeBuddy agent integration",
        "path": None,
    },
}

CODEBUDDY_DOC = """# CodeBuddy agent integration

## What is connected (verified live, 2026-11-14, gateway v2.180.0)

The Chat pane can run in REAL mode against the server-side CodeBuddy
gateway on 127.0.0.1:7890 through its **Jobs API** (agent instances):

- `POST /api/v1/auth/login` with the gateway password from
  `~/.codebuddy/settings.json` (gateway.password) -> session cookie.
- `POST /api/v1/jobs` (`prompt`, `cwd`, `name`, `permissionMode`) ->
  job instance `{id, state, sessionId, ...}`.
- `GET /api/v1/jobs/<id>` -> `data.job` lifecycle:
  `state` (working/blocked/done/failed/stopped), `status`
  (busy/waiting/idle), `settled`, `alive`, `output.result`, `detail`.
- `GET /api/v1/jobs/<id>/transcript` -> the real ACP conversation
  replay (user/agent/tool events). This is the content channel the
  workspace relays into chat - reliable and poll-safe.
- `POST /api/v1/jobs/<id>/reply` -> continues the SAME conversation,
  even after the turn settled (verified: result updates in place), so
  real chat is genuinely multi-turn.

The earlier integration used the runs API (`POST /api/v1/runs` + SSE
stream). It was replaced because a run's SSE stream is unavailable
after the run leaves the registry (404, no replay) - transcript
delivery could not be guaranteed. The jobs API replays its transcript
on demand, which closes that gap.

## Approval / execution semantics (measured, not assumed)

- Jobs dispatched with the default permission mode execute tools
  autonomously - headless job workers have no interactive approval
  prompt (measured: tool_call pending -> completed with real output).
- Jobs dispatched with `permissionMode: "plan"` get execution tools
  DENIED gateway-side (measured verbatim: "Permission to use Bash has
  been denied because this tool requires approval but permission
  prompts are not available"), while inspecting and answering still
  work. An "ask" decision AUTO-DENIES in headless jobs too, so an
  approval-relay UI cannot be built on gateway permission prompts.
- **This workspace therefore dispatches the chat agent in the gateway
  default mode and enforces safety itself, with a PreToolUse gate
  hook** (see next section). Ordinary work executes; destructive work
  needs a human click. Verified end-to-end by tests/verify_real_agent.py.

## The execution gate (how destructive operations are actually stopped)

Skill/rule instructions alone cannot enforce security, so enforcement
is a technically reliable layer outside the model:

- Chat agents run only inside a dedicated workspace
  (`/data1/neoag/agents/neoag_agent_ws`, deliberately outside any git
  repo, because jobs dispatched inside a repo run in a worktree copy
  where project settings would not apply). The workspace's
  `.codebuddy/settings.json` installs a PreToolUse hook
  (`gate.py`) that fires before EVERY tool call in the dispatched job.
- Classification (unit-tested against the real hook, 29 cases):
  - ALLOW - reads anywhere; writes inside the workspace, /tmp or the
    formal job-output root; routine `rm` inside those same scratch
    roots. No prompt.
  - DENY (no approval path) - credential/config paths (agent settings,
    ssh keys, auth files, secrets dir), the gate's own files, and
    write targets inside frozen operator-invariant trees.
  - CONFIRM - everything destructive or outside the workspace: deletes
    outside scratch roots, writes outside roots, force/system verbs
    (sudo, kill, chmod -R, git push --force, dd, services).
- A CONFIRM denial never runs: the hook answers with
  `NEOAG_CONFIRM_REQUIRED op=<sha256:24>`, this backend turns it into
  a **confirmation card in the chat**, and the agent is instructed to
  explain exact path / what changes / reversibility / backup / why
  necessary / safer alternative before asking.
- Approving the card writes a **single-use grant file** (hash of the
  exact tool+command, TTL 10 min). The gate consumes it on an
  identical retry - one unlock, one operation, then it is gone. A
  paraphrased or different command gets a fresh hash and a fresh card.
- Same-uid limitation, stated honestly: the agent runs as the same OS
  user as the hook, so this stops realistic failure modes (model
  mistakes, misread instructions, ambiguous requests), not a
  deliberately hostile process.

## Task queries from the agent

The chat agent (and any helper script in its workspace) can query
this workspace's own backend through an official read-only task API
instead of poking at databases or writing ad-hoc SQL:

- `GET /api/tasks`, `GET /api/tasks/active?conv_id=...`,
  `GET /api/tasks/<id>`, `GET /api/tasks/<id>/snapshot`,
  `GET /api/research` + `/events` - job summaries, live status, full
  task detail (decisions, artifacts, outcome), and the immutable
  run snapshot.
- Authenticated with a per-install token
  (`X-NeoAG-Task-Token` header, file deployed into the agent
  workspace at startup). The token is scoped to GET on task-query
  routes only - it cannot read the bootstrap or conversations, and
  cannot POST anything. Human UI traffic keeps using Basic auth.
- When several of the operator's conversations could own a task the
  agent is asking about, `/api/tasks/active` answers per conversation
  and never guesses "most recent" across them; a missing conv_id
  honestly returns nothing.

## Documented limits (do not assume otherwise)

- **One in-flight exchange per chat.** While the agent is answering,
  further real-mode messages in that chat are refused with an honest
  notice. Different chats run independently.
- **Rate limits.** Rapid dispatch can return 429; the client backs
  off with bounded retries and surfaces an honest error.
- **Chat job reattach / recovery.** The gateway job id is persisted on
  the conversation. While the job is alive a backend restart reattaches
  (same session, no new dispatch). If the job expired, the next message
  dispatches a fresh job rebuilt from the durable saved history and the
  chat shows an honest "the agent session is new" notice - never fake
  continuity.
- Gateway detail JSON can contain raw newlines - parsed leniently.

## Safety posture

- Real mode is OFF by default and switched only from this UI, audited.
- The gateway password is read from the operator's settings file at
  client construction and never logged, echoed, or stored elsewhere;
  credential paths are unreachable to the agent (deny, no approval
  path) and no secret material was ever observed in a transcript
  (verified).
- Routine diagnostics and workspace work need no approvals - safety
  prompts are reserved for destructive or out-of-scope operations, one
  card per exact operation.
- Real answers come only from the gateway transcript - the workspace
  never fabricates agent output.
"""

APP_GUIDE = """# NeoAG Web workspace - user guide

## What this is

A browser workspace for the NeoAG neoantigen pipeline. Chats live in
the left column (with their history), the agent conversation in the
middle, and the workspace on the right shows analysis jobs, results,
SOTA tool research and documentation. Drag the bar between chat and
workspace to resize.

## Chats

- **New chat** starts an empty conversation; its title is generated
  from your first message and you can rename it any time (pencil /
  double-click the title).
- Chats are saved on the server and survive restarts. Reopening a chat
  restores the full transcript; if its live agent session is still
  alive the conversation continues in it, and if not, a fresh agent
  session is rebuilt from the saved history - the chat always says
  which happened.
- Deleting a chat hides it here; the saved messages stay on the server
  (deletion is soft, for retention and audit).
- Chats and formal analysis runs are different things: conversations
  never appear in Task History, and analysis jobs never appear as chat
  messages.
- A real-mode turn always ends visibly: the "Real agent started"
  line carries the final status (completed, failed, cancelled,
  timed out, connection lost, or interrupted by a server restart).
  Nothing stays "streaming" forever, and the app never invents the
  missing tail of a turn.

## Confirmation cards

In real mode the agent executes ordinary work (reads, diagnostics,
workspace files) directly. If it attempts something destructive or
outside its authorized workspace, the operation is blocked before it
runs and a confirmation card appears in the chat. Read the six facts it
gives you - exact target, what changes, reversibility, backup status,
why it is necessary, and the safer alternative - then approve or deny.
An approval unlocks exactly that one operation, once.

## Demo mode

This build runs in demonstration mode: every analysis job is executed
by a mock engine on synthetic data, clearly badged. No patient data
and no real pipeline execution. The one thing that is genuinely real
is the SOTA Discovery live engine (real public-source retrieval, see
its section) and, when switched on in Settings, the real CodeBuddy
chat agent. The badge in the header (Demo mode) is the single
indicator - it is never repeated inside the conversation.

## Starting an analysis

1. Describe your goal in chat, or open Tasks.
2. Optionally propose an input manifest: describe where sequencing
   data live; the agent proposes tumor/normal/RNA assignments for you
   to confirm or correct. (In demo mode the proposal is synthetic.)
3. Pick a sample, keep the default stage plan or adjust it, choose the
   execution mode and submit.
4. The job enters the approval queue - execute-class work never starts
   without a human click, in either mode.
5. Follow progress in the job view; controlled mode pauses at decision
   points (input mapping, parameter gates, ranking outputs) and asks
   you; autonomous mode resolves them with the policy default of each
   decision and records every choice - including which policy applied
   and each option's honest validation status (policy defaults are
   documented defaults, not scientifically validated ones).

## Reading results

Every formal run writes an output-artifact manifest, and the Results
panel reads the artifacts back from live storage - it never trusts a
cached copy. Each run carries an outcome you can rely on:

- delivered - the requested artifacts exist and can be opened;
- zero candidates - the run completed honestly with an empty table;
- partial - some requested artifact is missing or empty;
- failed generation - the stage that should have produced output
  failed;
- no artifacts - nothing was produced at all.

A completed job's lifecycle state and its delivery outcome are
different things: a run can complete and still be not deliverable,
and the UI says so instead of showing an empty table as success. If
artifacts change after registration (deleted, replaced), Results
detects the drift and downgrades the outcome with a note.

When a run was asked for both ranking outputs, Results shows both
tables (a strategy switcher), and your decision is visible: the
effective configuration shows the gate value or ranking set actually
applied, with a warning when a decision overrode the policy default.
Task detail also exposes the immutable run snapshot: engine version,
policy version, dataset version, seed, stage plan and requested
config exactly as they were at start - written once, never updated.

A skipped essential stage forces production_release=false - the run
never pretends to be production-grade.

## Two execution modes

- Controlled: the run blocks at each decision point and asks.
- Autonomous: each decision's policy default is applied automatically
  and recorded with its policy id/version and validation status;
  bounded recovery means exactly one retry, then an honest stop with
  a reason.

Both modes share the same validity gates and the same decision
records, and in both modes your choices genuinely change the outputs:
choosing weighted-only produces exactly one artifact; choosing both
produces two; relaxing the stability gate changes which candidates
pass and is recorded as a warning on the run.

## Chat controls

- While the agent works you see what it is doing: a working indicator
  with elapsed time next to the composer, and a Stop button that
  cancels the running turn honestly (if nothing is running it says
  so instead of pretending).
- Tool calls appear as collapsed lines (running / result status and
  title); click to see the raw output. Final answers stay prominent.
- Messages carry timestamps with date separators between days.

## Skills

The Skills tab manages reusable agent instructions in three starting
categories: NeoAG Operation, NeoAG Web Awareness, and Scientific
Evidence & Research. You can read, create, edit, enable/disable, and
inspect version history (any version restorable) for each skill.

Two honest limits:

- Activation is per session: a deployed skill applies to NEW agent
  sessions only; each skill shows whether its current saved version
  is the deployed one, so you always know whether a change is live.
- Skills are advisory text for the agent. They can never change
  security gates, execution policies, or thresholds - those are
  enforced outside the model and are not editable through skills.

## SOTA Discovery

The Discovery panel researches tools/evidence for pipeline stages.
Two engines, clearly labelled:

- Live engine (default): genuine retrieval from public sources
  (Europe PMC, PubMed, OpenAlex, GitHub, bio.tools, PyPI - no
  credentials). Every report persists its provenance: the exact
  queries, hit counts, dedup, exclusions with reasons, and failed
  sources. Facts that could not be verified are marked "Not
  verified" - never guessed. If retrieval fails the run reports
  honestly incomplete results, never fabricated numbers.
- Example engine: a fixed offline fixture for UI testing, badged as
  an example on every screen. It never appears inside live reports.

Scope can be a full pipeline review or targeted stages (multiple
targets plus a free-text custom target). Research jobs are durable:
progress steps, cancel, and resume after a restart are all real
operations on the same backend workflow a chat request uses.
"""


def get(name: str) -> dict | None:
    doc = DOCS.get(name)
    if not doc:
        return None
    if doc["path"] is None:
        md = CODEBUDDY_DOC if name == "codebuddy" else APP_GUIDE
        return {"name": name, "title": doc["title"], "markdown": md,
                "source": "app-internal", "bytes": len(md)}
    p = doc["path"]
    if not p.is_file():
        return {"name": name, "title": doc["title"], "markdown":
                f"(source file currently unreadable: {p})",
                "source": str(p), "bytes": 0}
    data = p.read_bytes()[:MAX_BYTES]
    text = data.decode("utf-8", "replace")
    return {"name": name, "title": doc["title"], "markdown": text,
            "source": str(p), "bytes": len(data)}


def list_docs() -> list[dict]:
    return [{"name": k, "title": v["title"],
             "source": str(v["path"]) if v["path"] else "app-internal"}
            for k, v in DOCS.items()]


def retrieved_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
