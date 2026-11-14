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
  work.
- **This workspace therefore runs its real chat agent as a plan-mode
  job.** The real agent answers questions and proposes exact commands,
  but this interface can never execute them; any denial text the agent
  hits is relayed verbatim. Formal pipeline execution, when wired,
  must go through this workspace's own approval queue - never silently
  through a dispatched job.

## Documented limits (do not assume otherwise)

- **One in-flight exchange.** While the agent is answering, further
  real-mode messages are refused with an honest notice.
- **Rate limits.** Rapid dispatch can return 429; the client backs
  off with bounded retries and surfaces an honest error.
- **Chat job lifetime.** The chat job lives while the demo process
  runs; after a demo restart the next message re-dispatches a job and
  recent turns ride along as a rolling transcript snippet.
- Gateway detail JSON can contain raw newlines - parsed leniently.

## Safety posture

- Real mode is OFF by default and switched only from this UI, audited.
- The gateway password is read from the operator's settings file at
  client construction and never logged, echoed, or stored elsewhere.
- Tool-execution approval controls are preserved: the chat job is
  plan-mode (execution denied gateway-side), and nothing here
  silently enables unrestricted command execution.
- Real answers come only from the gateway transcript - the workspace
  never fabricates agent output.
"""

APP_GUIDE = """# NeoAG Web workspace - user guide

## What this is

A browser workspace for the NeoAG neoantigen pipeline. You talk to the
agent on the left; the workspace on the right shows analysis jobs,
results, SOTA tool research and documentation.

## Demo mode

This build runs in demonstration mode: every job is executed by a
mock engine on synthetic data, clearly badged. No patient data, no real
pipeline, no network calls. The badge in the header (Demo mode) is the
single indicator - it is never repeated inside the conversation.

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
   you; autonomous mode resolves them with validated defaults and
   records every choice in Decision History.

## Reading results

Completed jobs expose candidate tables, evidence columns and a
provenance block (job id, seed, engine, dataset version). A skipped
essential stage forces production_release=false - the run never
pretends to be production-grade.

## Two execution modes

- Controlled: the run blocks at each decision point and asks.
- Autonomous: validated defaults are applied automatically, each
  recorded; bounded recovery means exactly one retry, then an honest
  stop with a reason.

Both modes share the same validity gates and the same decision
records.
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
