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

## What is connected (verified live)

The Chat pane can run in REAL mode against the server-side CodeBuddy
gateway on 127.0.0.1:7890. Verified request/response shapes:

- `POST /api/v1/auth/login` with the gateway password from
  `~/.codebuddy/settings.json` (gateway.password) -> session cookie +
  token. Verified 200.
- `POST /api/v1/runs` with a Gateway Protocol message
  (`id`, `type`, `payload.text`, `sender`) plus the
  `x-codebuddy-request` header -> 202 with a runId. Verified live.
- `GET /api/v1/runs/<runId>` -> `{runId, active}`. Verified.
- `POST /api/v1/runs/<runId>/cancel` and
  `GET /api/v1/runs/<runId>/stream` (SSE) exist; streaming requires the
  session cookie.

## Documented limits (do not assume otherwise)

- **Approval relay: not implemented.** With the gateway's default
  permission mode, tool actions that need approval prompt in the
  operator's own CodeBuddy Web UI; no endpoint is known that approves
  them via the runs API. Real-agent prompts are therefore restricted
  to read-only inspection tasks.
- **One active run.** Creating a second run while one is active is
  rejected (observed 429 concurrency cap).
- **Rate limits.** Rapid run creation returns 429; the client backs
  off with a bounded retry and surfaces an honest error.
- **Context is not guaranteed.** Each message parses as its own
  conversation, so the client sends a rolling context snippet with
  every run instead of assuming server-side memory.

## Safety posture

- Real mode is OFF by default and switched only from this UI, audited.
- The gateway password is read from the operator's settings file at
  client construction and never logged, echoed, or stored elsewhere.
- Tool-execution approval controls are preserved: nothing in this
  integration silently enables unrestricted command execution.
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
