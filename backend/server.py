"""NeoAG web demo server - stdlib only, binds 127.0.0.1, Basic auth on all
routes (credentials live only in data/auth.json, 0600; see README security
notes before any exposure change).

Run:  python3 -m backend.server  (from the project root, or via ../start.sh)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import skills as skills_mod
from . import native as native_mod
from . import readiness as readiness_mod
from . import exports as exports_mod
from . import versions as versions_mod
from .agent import DemoAgent, ensure_task_gateway
from .help_docs import DOCS as HELP_DOCS, get as help_get
from .inventory import load_inventory, targets_for_ui
from .jobs import JobManager, DECISION_POINTS as RUN_DECISION_POINTS
from .mock_data import SAMPLES, STAGES, DEMO_DATASET_VERSION
from .neoag_params import (PARAM_GROUPS, STAGE_GRAPH, DECISION_POINTS,
                           HUMAN_REQUIRED, params_view,
                           SCIENCE_CONTROLS, SCIENCE_INFO,
                           SCIENCE_SECTIONS)
from .policies import POLICIES, POLICY_VERSION
from .research import (TARGETS as EXAMPLE_TARGETS, submit_research,
                       resume_research, sweep_interrupted_research,
                       LIVE_ENGINE_VERSION)
from .store import Store, new_id, now

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
DATA = ROOT / "data"
HOST = "127.0.0.1"
# NEOAG_DEMO_PORT + NEOAG_DEMO_DB let verification batteries run isolated
# servers against fresh databases without touching the live demo state.
PORT = int(os.environ.get("NEOAG_DEMO_PORT", "8712"))

# Hard, EXPLICIT limit for one chat message, counted in Unicode
# characters (never bytes) so Chinese/Japanese prompts get exactly the
# same allowance as ASCII ones. Oversized messages are REJECTED with a
# 413 and the real numbers - they are never silently shortened.
#
# History: this path used to do `str(body["text"])[:4000]`, which
# destroyed a long specification prompt mid-word with no error, no
# audit entry and no way for the user to notice (2026-11-19 defect).
# The limit is set far above any realistic instruction prompt; the
# matching transport bound is codebuddy_client.MAX_PROMPT_CHARS, which
# must stay higher than this plus the dispatch preamble + rolling
# transcript so the limit a user hits is always THIS one.
MAX_CHAT_CHARS = 100_000

# M11 P1-10: file intake. Uploads are documents/reports/tables/images
# only, and the size cap applies to the DECODED bytes (measured after
# base64 decode, so the rejection counts what actually lands). The cap
# is env-overridable because it is an operator policy knob, not a
# science constant. Sequencing files are refused on BOTH intake paths:
# they belong to the formal ingestion (discovery -> confirmed
# manifest), never to a chat attachment.
ATTACH_MAX_BYTES = int(os.environ.get("NEOAG_ATTACH_MAX_BYTES")
                       or 25 * 1024 * 1024)
ATTACH_KINDS = {
    ".txt": "document", ".md": "document", ".pdf": "document",
    ".docx": "document", ".html": "report", ".htm": "report",
    ".csv": "table", ".tsv": "table", ".json": "table",
    ".yaml": "table", ".yml": "table", ".xlsx": "table",
    ".png": "image", ".jpg": "image", ".jpeg": "image",
    ".webp": "image",
}
ATTACH_SEQ_EXT = {".bam", ".cram", ".fastq", ".fq", ".sam", ".vcf",
                  ".bcf", ".2bit", ".ubam"}
ATTACH_MIME_BY_EXT = {
    ".txt": "text/plain", ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument."
             "wordprocessingml.document",
    ".html": "text/html", ".htm": "text/html",
    ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".json": "application/json", ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
    ".xlsx": "application/vnd.openxmlformats-officedocument."
             "spreadsheetml.sheet",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def attach_dir() -> Path:
    d = Path(store.attach_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def attach_public(a: dict) -> dict:
    return {k: a[k] for k in ("id", "conv_id", "created_at", "name",
                              "mime", "kind", "size", "sha256",
                              "path", "source", "origin")}


def attach_kind(name: str) -> tuple[str, str]:
    """(kind, ext) for an allowed attachment name; raises with an
    honest message for anything else."""
    ext = os.path.splitext(name)[1].lower()
    if ext in ATTACH_SEQ_EXT:
        raise ValueError(
            f"{ext} files are sequencing/variant inputs - they belong "
            "to the formal ingestion path (input discovery -> "
            "confirmed manifest), not to a chat attachment")
    if ext not in ATTACH_KINDS:
        raise ValueError(
            f"unsupported attachment type {ext or '(no extension)'} - "
            "documents, reports, tables and images only")
    return ATTACH_KINDS[ext], ext


def under_authorized_root(p: str) -> str:
    """Realpath the given path must live under - or reject with an
    honest error naming the authorized roots (same boundary the
    input-discovery API already enforces)."""
    rp = os.path.realpath(p)
    for root in native_mod.AUTHORIZED_INPUT_ROOTS:
        if rp == root or rp.startswith(root + os.sep):
            return rp
    raise PermissionError(
        "path is outside the authorized input roots: "
        + ", ".join(native_mod.AUTHORIZED_INPUT_ROOTS))


store = Store(DATA)
EVENT_QUEUES: list[queue.Queue] = []
EVENT_LOCK = threading.Lock()

# Application-level authentication: every route (static, API, SSE) requires
# HTTP Basic auth. The generated password lives ONLY in data/auth.json
# (0600, operator-owned) - it is never echoed to chat, logs or reports.
# Delete the file and restart to rotate. Over a Cloudflare tunnel the
# browser->edge hop is TLS; the edge->origin hop is loopback on this host,
# so credentials never cross the public network in cleartext.
AUTH_FILE = DATA / "auth.json"


def _load_or_create_auth() -> dict:
    if AUTH_FILE.exists():
        try:
            rec = json.loads(AUTH_FILE.read_text())
            if isinstance(rec, dict) and rec.get("user") and rec.get("password"):
                return rec
        except Exception:
            pass
    rec = {"user": "operator", "password": secrets.token_urlsafe(15),
           "note": "delete this file and restart the demo to rotate"}
    AUTH_FILE.write_text(json.dumps(rec, indent=1))
    AUTH_FILE.chmod(0o600)
    return rec


AUTH = _load_or_create_auth()


def publish(event: dict) -> None:
    with EVENT_LOCK:
        for q in list(EVENT_QUEUES):
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


jobs = JobManager(store, publish)
agent = DemoAgent(store, jobs)


def task_name_parts(kind: str, spec: dict, sample_id: str,
                    scope: str = "", target: str = "") -> dict:
    """M9 §8: the pieces of a human-readable title, DERIVED from stored
    metadata only (sample/case, kind, scope/target). Nothing is invented;
    with thin metadata the UI falls back to a concise generic name. The
    localized type label lives in the frontend dictionary (name_type is
    a key), so a stored title never freezes one language. The internal
    id stays the authoritative identity everywhere else."""
    subj, ntype = "", "task"
    if kind == "neoag-native-run":
        nparams = spec.get("native_params") or {}
        subj = str(nparams.get("sample_id") or nparams.get("case_id")
                   or sample_id or "")
        ntype = "analysis"
    elif kind in ("analysis", "discovery", "diagnostic", "benchmark"):
        subj, ntype = str(sample_id or ""), kind
    elif scope or target:
        # a job row whose only identity is its scope/target text
        subj, ntype = str(target or "")[:48], (scope or "task")
    return {"name_subject": subj, "name_type": ntype}


def _native_preview_tables(arts, max_tables=2, max_rows=25):
    """Verbatim header + first rows of the real ranked/scoring tables a
    native run produced (served exactly as the file states them - the
    web layer adds no scientific interpretation)."""
    import csv as _csv
    cand = [a for a in arts
            if a.get("status") == "available"
            and (str(a.get("type")) in ("ranking", "scoring", "table")
                 or str(a.get("name", "")).startswith(("ranking/",
                                                       "scoring/")))
            and str(a.get("name", "")).endswith((".tsv", ".csv", ".txt"))]
    cand.sort(key=lambda a: (0 if str(a["name"]).startswith("ranking/")
                             else 1, str(a["name"])))
    out = []
    for a in cand[:max_tables]:
        p = str(a.get("path") or "")
        if not p or not os.path.isfile(p):
            continue
        try:
            with open(p, newline="", encoding="utf-8",
                      errors="replace") as f:
                head = f.read(65536)
            delim = "\t" if p.endswith(".tsv") else ","
            rdr = list(_csv.reader(head.splitlines(), delimiter=delim))
            if not rdr:
                continue
            out.append({"artifact_id": a.get("id"), "name": a["name"],
                        "kind": a.get("type"),
                        "header": rdr[0][:16],
                        "rows": [r[:16] for r in rdr[1:1 + max_rows]],
                        "truncated": True})
        except OSError:
            continue
    return out


def job_public(job: dict) -> dict:
    j = {k: job[k] for k in ("id", "created_at", "updated_at", "started_at",
                             "finished_at", "state", "kind", "sample_id",
                             "stage", "progress", "error", "approval_id",
                             "conv_id", "outcome")}
    j["spec"] = json.loads(job.get("spec_json") or "{}")
    # M9 §8 presentation fields (derived; id/sample_id stay canonical)
    j["display_title"] = str(job.get("display_title") or "")
    # M11 P1-9 task-management flags (record metadata; files and the
    # audit trail are outside their reach by construction)
    j["pinned"] = int(job.get("pinned") or 0)
    j["locked"] = int(job.get("locked") or 0)
    j["trashed"] = int(job.get("trashed") or 0)
    j["trashed_at"] = str(job.get("trashed_at") or "")
    j.update(task_name_parts(job.get("kind") or "task", j["spec"],
                             job.get("sample_id") or ""))
    return j




def research_public_rows(store) -> list[dict]:
    """Research rows for the ordinary UI: the stored row plus the same
    derived display parts job rows get (M9 §8). The manual
    display_title always wins; derived parts never invent metadata."""
    out = []
    for r in store.research_list():
        out.append({**r,
                    "name_subject": str(r.get("target") or "")[:48],
                    "name_type": "review"})
    return out


def research_task_rows(store) -> list[dict]:
    """Milestone 7 §1/§9: a research run IS a formal background task.
    These rows are DERIVED live from the research table (never a second
    copy of the truth), so the unified task list, the official task
    query API and the agent all see research with the same fields as
    analyses."""
    out = []
    for r in store.research_list():
        try:
            prog = json.loads(r.get("progress_json") or "{}")
        except ValueError:
            prog = {}
        state = {"queued": "queued", "running": "running",
                 "completed": "completed",
                 "cancelled": "cancelled"}.get(r["status"], "failed")
        steps = prog.get("steps") or []
        done = len(prog.get("done_queries") or [])
        total = len(prog.get("queries") or [])
        if state == "completed":
            pct = 100
        elif state == "queued":
            pct = 0
        elif total:
            pct = min(95, int(90 * done / total))
        else:
            pct = min(30, 8 * len(steps))
        outcome = ""
        if state == "completed":
            full = store.research_get(r["id"]) or {}
            rep = full.get("report") or {}
            outcome = ("decision_ready" if rep.get("decision_ready")
                       else "preliminary")
        err = ""
        if state in ("failed", "cancelled"):
            err = str(prog.get("note") or r["status"])[:120]
        out.append({"id": r["id"], "created_at": r["created_at"],
                    "updated_at": r.get("finished_at") or r["created_at"],
                    "started_at": r["created_at"],
                    "finished_at": r.get("finished_at"),
                    "state": state, "kind": "discovery",
                    "sample_id": f"research ({r.get('engine') or '?'})",
                    "stage": f"{r['scope']} · "
                             + (r.get("target") or "")[:48],
                    "progress": pct, "error": err,
                    "approval_id": None,
                    "conv_id": r.get("conv_id") or "",
                    "outcome": outcome, "spec": {},
                    # P1-9: research rows are not job records - the
                    # management flags do not apply to them (the API
                    # refuses them by id), the UI hides the controls.
                    "pinned": 0, "locked": 0, "trashed": 0,
                    "trashed_at": "",
                    # M9 §8: research rows carry their scope/target as
                    # the display metadata (engine names stay internal)
                    "display_title": str(r.get("display_title") or ""),
                    "name_subject": str(r.get("target") or "")[:48],
                    "name_type": "review"})
    return out


def unified_tasks(store, limit: int = 50, trash: str = "normal") -> list[dict]:
    """One chronological list: analyses + partial stages + research,
    regardless of which interface started them (§1.2). P1-9: trashed
    records are excluded by default; the trash view asks explicitly.
    Research rows are never trashed, so they appear in the normal
    listing only."""
    rows = [job_public(j) for j in store.job_list(200, trash=trash)]
    if trash != "only":
        rows += research_task_rows(store)
    rows.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return rows[:limit]


ACTIVE_TASK_STATES = ("queued", "running", "paused", "blocked")


def apply_job_flags(jid: str, body: dict) -> tuple[int, dict]:
    """M11 P1-9: the ONE flag rule, used by the single-task endpoint
    and the bulk endpoint alike (bulk must never be laxer than
    individual). pinned/locked are plain record metadata; trashing is
    refused while the task is ACTIVE (cancel first - validated, not
    implicit) and while LOCKED (that is the lock's whole purpose).
    None of this touches files; every accepted change is audited."""
    j = store.job_get(jid)
    if not j:
        return 404, {"error": "no such job"}
    fields = {}
    if "pinned" in body:
        fields["pinned"] = 1 if body.get("pinned") else 0
    if "locked" in body:
        fields["locked"] = 1 if body.get("locked") else 0
    if "trashed" in body:
        want = 1 if body.get("trashed") else 0
        if want:
            if fields.get("locked", int(j.get("locked") or 0)):
                return 409, {"error": "locked: unlock the task before "
                                      "moving it to trash"}
            if j["state"] in ACTIVE_TASK_STATES:
                return 409, {"error": "active task: cancel it before "
                                      "moving it to trash"}
            fields["trashed"] = 1
            fields["trashed_at"] = now()
        else:
            fields["trashed"] = 0
            fields["trashed_at"] = ""
    if not fields:
        return 400, {"error": "no flags given"}
    store.job_set(jid, **fields)
    store.audit("user", "task.flags.set", jid, dict(fields))
    publish({"type": "job", "id": jid})
    fresh = store.job_get(jid) or {}
    return 200, {"ok": True, "id": jid,
                 "flags": {"pinned": int(fresh.get("pinned") or 0),
                           "locked": int(fresh.get("locked") or 0),
                           "trashed": int(fresh.get("trashed") or 0),
                           "trashed_at": str(fresh.get("trashed_at")
                                             or "")}}


def task_detail(store, job: dict) -> dict:
    """Official task-query view of one formal run (B1): everything a user
    or the agent needs to answer 'what did this run do and what did it
    actually produce' - without ad-hoc SQL against the store."""
    t = job_public(job)
    t["outcome_note"] = ""
    res = json.loads(job.get("result_json") or "{}")
    if res:
        t["outcome_note"] = res.get("outcome_note", "")
        t["delivery_claim"] = res.get("delivery_claim", "")
        t["warnings"] = res.get("warnings", [])
        # M10 §11: the structured failure record (humanized reason,
        # phase, next step) travels with the task detail everywhere.
        if res.get("failure"):
            t["failure"] = res["failure"]
            t["completed_before_failure"] = res.get(
                "completed_before_failure", [])
            t["partial_artifacts"] = res.get("partial_artifacts", 0)
        t["effective_config"] = (res.get("manifest") or {}).get(
            "effective_config", {})
        t["requested_config"] = (res.get("manifest") or {}).get(
            "requested_config", {})
        t["versions"] = (res.get("manifest") or {}).get("versions", {})
    ds = []
    for d in store.decision_list(job["id"]):
        ds.append({x: d.get(x) for x in (
            "id", "key", "category", "stage", "decision_type", "trigger",
            "question", "options", "chosen", "rationale", "policy_id",
            "policy_version", "decided_by", "status", "mandatory",
            "effect_class", "downstream", "ts", "decided_at")})
    t["decisions"] = ds
    t["artifacts"] = store.artifact_list(job["id"])
    if job.get("kind") == "neoag-native-run":
        spec = json.loads(job.get("spec_json") or "{}")
        t["native"] = {
            "engine": "native",
            "engine_version": res.get("engine_version"),
            "profile": spec.get("native_profile"),
            "gateway_job_id": (job.get("gw_job_id")
                               or res.get("gateway_job_id") or ""),
            "gateway_cancel_supported": False,
            "native_params": spec.get("native_params") or {},
            "plan": json.loads(job.get("native_plan_json") or "null"),
            "failure_code": res.get("failure_code"),
            "failure_detail": res.get("failure_detail"),
            "outdir": res.get("outdir"),
        }
    evs = store.event_list(job["id"], 0, 1000)
    t["events"] = evs
    t["failures"] = [e for e in evs if "fail" in str(e.get("kind")) or
                     "error" in str(e.get("kind"))]
    t["events_url"] = f"/api/jobs/{job['id']}/events"
    t["snapshot_url"] = f"/api/tasks/{job['id']}/snapshot"
    t["results_url"] = f"/api/results/{job['id']}"
    return t


class Handler(BaseHTTPRequestHandler):
    server_version = "NeoAgWebDemo/0.1"

    # ---------- helpers ----------
    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-NeoAG-Demo", "mock-data-only")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            raise ValueError("invalid JSON body")
        return data if isinstance(data, dict) else {}

    def log_message(self, fmt, *args):
        pass

    # ---------- authentication ----------
    # M10 PART 3 decision, recorded honestly: the login stays HTTP Basic
    # Auth. Replacing it with app-level sessions (cookie + server-side
    # session store) was NOT adopted because the secure-session
    # prerequisites could not be verified on this deployment: the app is
    # reached through operator-managed tunnels where TLS termination and
    # cookie confidentiality are outside this server's control, so a
    # session cookie would travel over a channel this code cannot prove
    # is confidential, and logout/expiry semantics would be unenforceable
    # behind those tunnels. Basic Auth is per-request, has no bearer
    # artifact to leak or expire, and its boundary is the tunnel policy
    # the operator already controls. Revisit when the deployment serves
    # the app over an end-to-end HTTPS origin this server owns.
    def _unauthorized(self) -> None:
        body = json.dumps({"error": "authentication required"}).encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate",
                         'Basic realm="NeoAG Web Demo", charset="UTF-8"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-NeoAG-Demo", "mock-data-only")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, pwd = base64.b64decode(header[6:].strip()).decode(
                "utf-8").partition(":")
        except Exception:
            return False
        return hmac.compare_digest(user, AUTH["user"]) and \
            hmac.compare_digest(pwd, AUTH["password"])

    # ---------- task-query token (GET-only, official agent pathway) ----
    # The agent in the workspace authenticates with a read-only token
    # (deployed by agent.ensure_task_gateway) instead of the operator's
    # Basic credentials. It ONLY opens the task/research query routes and
    # ONLY for GET - it can never mutate, read chats, or reach admin.
    # This includes the persisted export/bundle downloads under those
    # prefixes (M5 PART 7): they serve the same read-only content the
    # token already sees through /api/results and /api/research.
    TASK_QUERY_PREFIXES = ("/api/tasks", "/api/results/", "/api/research")

    def _task_token_ok(self) -> bool:
        tok = store.setting("task_token", "")
        given = self.headers.get("X-NeoAG-Task-Token", "")
        return bool(tok) and bool(given) and hmac.compare_digest(given, tok)

    # ---------- exported files (M5 PART 7) ----------
    def _file(self, data: bytes, filename: str, ctype: str,
              inline: bool = False, extra: dict | None = None) -> None:
        """Serves persisted export bytes (never regenerated per request).
        The sha256 header lets any client verify that re-downloads return
        byte-identical files."""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         ("inline" if inline else "attachment") +
                         f'; filename="{filename}"')
        self.send_header("X-Export-Sha256",
                         hashlib.sha256(data).hexdigest())
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(data)

    def _serve_export(self, kind: str, oid: str, fmt: str, q) -> None:
        """Render-once-serve-forever: the first request for a (object,
        format) persists the file pinned to the stored content hash;
        every later request serves those exact bytes."""
        fmt = fmt.lower()
        if fmt not in exports_mod.FORMATS:
            return self._json(404, {"error": "unknown export format",
                                    "available": sorted(
                                        exports_mod.FORMATS)})
        try:
            if kind == "research":
                row = exports_mod.ensure_research_export(store, oid, fmt)
            else:
                row = exports_mod.ensure_job_bundle(store, oid)
            data = exports_mod.read_export(store, row)
        except LookupError as e:
            return self._json(404, {"error": str(e)})
        except exports_mod.Conflict as e:
            return self._json(409, {"error": str(e)})
        self._file(data, row["filename"], exports_mod.FORMATS[fmt][1],
                   inline=(q.get("inline") or ["0"])[0] == "1",
                   extra={"X-Export-Version": row["version"]})

    def _research_action(self, rid: str, action: str, q) -> None:
        """M5 PART 6 discovery follow-up actions - read-only derivations
        from the STORED report (no retrieval, no execution, no mutation).
        A candidate that is not in the report is a 404, never an
        invented row. Adoption is a HUMAN action: this endpoint produces
        a plan and explicitly applies nothing."""
        cand = (q.get("candidate") or [""])[0].strip()
        tid = (q.get("target") or [""])[0].strip()
        r = store.research_get(rid)
        if not r or not r.get("report"):
            return self._json(404, {"error": "no stored report to act on"})
        rep = r["report"]
        match = [c for c in rep.get("candidates") or []
                 if str(c.get("name", "")).lower() == cand.lower()]
        if not cand or not match:
            return self._json(
                404, {"error": f"candidate '{cand}' is not a row of this "
                               "report - this app will not plan for a "
                               "tool it never retrieved"})
        c = match[0]
        stage = next((s for s in (rep.get("inventory") or {}).get(
            "stages") or [] if s.get("id") == tid), None)
        cur = (stage or {}).get("current_tools") or []
        if action == "compatibility":
            probe = versions_mod.installed_version(str(c.get("name")))
            cur_probe = {t: versions_mod.installed_version(t) for t in cur}
            return self._json(200, {
                "candidate": c.get("name"), "target": tid or None,
                "artifact_class": c.get("artifact_class"),
                "reported": {
                    "version": c.get("verified_version"),
                    "license": c.get("license"),
                    "maintenance": c.get("maintenance")},
                "on_this_host": {
                    "installed_version_probe": probe,
                    "note": ("read-only probe of the deployed tools "
                             "layout; a value here means the app SAW a "
                             "version record - it did not run anything")},
                "current_stage": {
                    "stage": tid or None, "configured_tools": cur,
                    "installed": cur_probe,
                    "config_provenance": (stage or {}).get("provenance")},
                "interface": ("Not verified - this check covers PRESENCE, "
                              "VERSION RECORD and LICENSE visibility only; "
                              "I/O compatibility with the NeoAG workflow "
                              "was not assessed and requires the "
                              "adoption plan's manual verification"),
                "license_caution": (
                    "license terms were NOT legally reviewed by this app"
                    if str(c.get("license", "")).lower() not in
                    ("mit", "bsd", "apache-2.0", "not verified") else
                    "no known restriction among permissive OSS ids; "
                    "still verify the actual LICENSE file")})
        # adoption plan
        supported = bool(stage and cur)
        plan = {
            "candidate": c.get("name"), "target": tid or None,
            "applied": False,
            "statement": (
                "Adoption is executed by a HUMAN on the configured repo. "
                "This app never patches production config, never "
                "installs tools, and records no silent substitution. "
                + ("The stage HAS a configurable tool slot in the "
                   "workflow config - the plan below points at it."
                   if supported else
                   "The stage has NO evidenced tool slot in the workflow "
                   "config - replacement is therefore a CODE/INTEGRATION "
                   "change by an engineer with a review, not a config "
                   "switch; this app will not pretend otherwise.")),
            "current_stage": {
                "stage": tid or None, "configured_tools": cur,
                "config_provenance": (stage or {}).get("provenance")},
            "evidence": {
                "report": rid,
                "conclusion": (rep.get("module_conclusions") or {}).get(
                    tid, {}).get("conclusion"),
                "performance_summary": c.get("performance_summary"),
                "evidence_gaps": c.get("evidence_gaps")},
            "steps": [
                f"1. Review the stored evidence (report {rid}) - note "
                "that NO winner is asserted without paired numbers.",
                "2. Propose the benchmark as a FORMAL job (kind=benchmark "
                "through the unified API) with the candidate vs the "
                "configured tool and explicit metrics; a human approves "
                "the card before anything runs.",
                f"3. Verify licensing for the intended use "
                f"(candidate reports '{c.get('license')}') - legal review "
                "is a human responsibility, not this app's.",
                "4. If the candidate must be installed: that is an "
                "authorized operator action (production tool installs "
                "are out of scope for this app); pin the exact version.",
                "5. Run the deployment's own validation fixture through "
                "both configurations; compare ranking artifacts and the "
                "stated metrics on identical inputs.",
                "6. Record the outcome as a decision in this app's "
                "decision history; adoption applies only after that.",
                "7. Keep the previous configuration recoverable until "
                "the verification above passes (rollback = restore "
                "config)."],
            "verification_gate": ("adopt ONLY if the benchmark on THIS "
                                  "deployment's inputs improves the "
                                  "stated metrics - published numbers "
                                  "from other versions do not count")}
        return self._json(200, plan)

    # ---------- GET ----------
    def do_GET(self) -> None:
        # an unhandled exception in any GET route must surface as an
        # honest JSON 500, never as a dropped connection
        try:
            self._route_get()
        except Exception as e:
            try:
                self._json(500, {"error": str(e)})
            except Exception:
                pass

    def _route_get(self) -> None:
        u = urlparse(self.path)
        path, q = u.path.rstrip("/") or "/", parse_qs(u.query)
        task_route = path.startswith(self.TASK_QUERY_PREFIXES)
        if not (self._authorized() or
                (task_route and self._task_token_ok())):
            return self._unauthorized()
        if path in ("", "/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/api/bootstrap":
            convs = store.conv_list()
            cid = (q.get("conv_id") or [""])[0]
            if cid and not store.conv_get(cid):
                cid = ""
            if not cid and convs:
                cid = convs[0]["id"]  # most recently updated chat
            return self._json(200, {
                "demo": True, "dataset_version": DEMO_DATASET_VERSION,
                "samples": SAMPLES, "stages": STAGES, "policies": POLICIES,
                "conversations": convs, "active_conv": cid,
                "messages": store.msg_list(400, cid) if cid else [],
                "jobs": unified_tasks(store, 50),
                "approvals": store.approval_list(),
                "scope_grants": store.scope_grants_active(),
                "decisions": store.decision_list(),
                "research": research_public_rows(store),
                "manifests": store.manifest_list(),
                "agent_mode": store.setting("agent_mode", "mock"),
                "params_meta": [
                    {"group": g["group"], "expert": g["expert"],
                     "items": len(g["items"])} for g in PARAM_GROUPS],
                "stage_graph": STAGE_GRAPH,
                # richer runtime definitions (the ones the engine really
                # applies): keys, categories, triggers, policy ids,
                # option effects, mandatory flags, honest validation status
                "decision_points": RUN_DECISION_POINTS,
                "policy_version": POLICY_VERSION,
                "skills": skills_mod.activation_state(store),
                "discovery": {
                    "live_engine_version": LIVE_ENGINE_VERSION,
                    "live_targets": targets_for_ui(),
                    "example_targets": [
                        {"id": k, "label": v.get("stage") or k}
                        for k, v in EXAMPLE_TARGETS.items()],
                },
                "help": [
                    {"name": k, "title": v["title"],
                     "source": str(v["path"]) if v["path"] else "app-internal"}
                    for k, v in HELP_DOCS.items()],
                "settings": store.setting("ui", {"mode": "auto"}),
            })
        if path == "/api/params":
            groups = params_view()
            # science-module tools are DERIVED here from the code-grounded
            # inventory (never stored in the params module itself), so the
            # configuration view can never drift from the configured repo.
            inv = load_inventory()
            by_stage = {s["id"]: s for s in inv["stages"]}
            for g in groups:
                mod = g.get("module")
                if not mod:
                    continue
                tools, prov = [], []
                for sid in mod.get("stages") or []:
                    s = by_stage.get(sid)
                    if s:
                        tools += s.get("current_tools") or []
                        prov.append(s["provenance"])
                mod["tools"] = sorted(set(tools)) or [
                    "not evidenced in the workflow TOML - the UI does "
                    "not invent tools here"]
                mod["tools_provenance"] = prov or [
                    "no [stages.*] entry matched in the workflow TOML"]
            return self._json(200, {"groups": groups,
                                   "stage_graph": STAGE_GRAPH,
                                   "decision_points": DECISION_POINTS,
                                   "human_required": HUMAN_REQUIRED,
                                   # M6 PART E: the curated structured
                                   # science panel (every control verified
                                   # to change native execution) + the
                                   # code-decided list with citations.
                                   "science": SCIENCE_CONTROLS,
                                   "science_info": SCIENCE_INFO,
                                   "science_sections": SCIENCE_SECTIONS,
                                   "inventory_captured_at":
                                       inv["captured_at"]})
        if path == "/api/audit":
            return self._json(200, store.audit_list(300))
        if path == "/api/conversations":
            return self._json(200, {"conversations": store.conv_list()})
        if path.startswith("/api/conversations/"):
            cid = path.strip("/").split("/")[2]
            conv = store.conv_get(cid)
            if not conv or conv.get("deleted"):
                return self._json(404, {"error": "no such conversation"})
            # M11 P0-1: the conversation the user reads vs the execution
            # record behind it. ?include=audit is the optional technical
            # view; the default payload is the conversation only.
            want_audit = "audit" in (q.get("include") or [])
            return self._json(200, {
                "conversation": conv,
                "messages": store.msg_list(
                    1000 if want_audit else 400, cid,
                    visibility=None if want_audit else "chat")})
        if path == "/api/help":
            return self._json(200, {"docs": [
                {"name": k, "title": v["title"],
                 "source": str(v["path"]) if v["path"] else "app-internal"}
                for k, v in HELP_DOCS.items()]})
        if path.startswith("/api/help/"):
            name = path.strip("/").split("/")[2]
            doc = help_get(name)
            if not doc:
                return self._json(404, {"error": "no such doc"})
            return self._json(200, doc)
        if path.startswith("/api/manifests/"):
            mid = path.strip("/").split("/")[2]
            m = store.manifest_get(mid)
            if not m:
                return self._json(404, {"error": "no such manifest"})
            return self._json(200, m)
        if path == "/api/decisions":
            jid = (q.get("job_id") or [None])[0]
            ds = store.decision_list(jid)
            return self._json(200, {"decisions": ds})
        if path == "/api/scopegrants":
            # M11 P0-2: the live 'always allow within this scope'
            # grants - visible, dated, revocable (revoke re-materialises
            # the file gate.py enforces).
            return self._json(200, {"grants": store.scope_grants_active()})
        if path == "/api/codebuddy/status":
            mode = store.setting("agent_mode", "mock")
            info = {"mode": mode, "connected": False, "transport": "jobs",
                    "chat_jobs": [], "agent_workspace": agent.AGENT_WS,
                    "permission_mode": "default (tools auto-execute in "
                                        "headless jobs)",
                    "enforcement": "workspace PreToolUse gate: destructive "
                                   "or out-of-workspace operations are "
                                   "blocked until a single-use human grant",
                    "gateway_version": None, "detail": ""}
            try:
                client = agent._ensure_client()  # login probe, no dispatch
                gw = client.info()
                info["connected"] = True
                info["gateway_version"] = gw.get("version")
                info["detail"] = (f"gateway v{gw.get('version')} via the "
                                  "jobs API; one agent job per chat, "
                                  "gate + single-use grants for destructive "
                                  "operations")
                for cid, st in list(agent._conv.items()):
                    if not st.get("job"):
                        continue
                    d = client.job(st["job"])
                    info["chat_jobs"].append(
                        {"conv_id": cid, "id": st["job"],
                         "busy": bool(st.get("busy")),
                         "state": d.get("state"),
                         "status": d.get("status"),
                         "settled": d.get("settled"),
                         "alive": d.get("alive")})
            except Exception as e:
                info["detail"] = str(e)[:200]
            return self._json(200, info)
        if path == "/api/stream":
            return self._sse()
        # ---------- official task-query API (B1: no ad-hoc SQL) --------
        if path == "/api/tasks":
            cid = (q.get("conv_id") or [None])[0]
            kinds = (q.get("kind") or [None])[0]
            lim = min(int((q.get("limit") or ["50"])[0]), 200)
            # §1/§9: research runs are part of the official task query
            # too; the 'research' filter name is an alias for their
            # 'discovery' kind.
            rows = unified_tasks(store, 200)
            if cid:
                rows = [j for j in rows if j.get("conv_id") == cid]
            if kinds:
                want = {"research": "discovery"}.get(kinds, kinds)
                rows = [j for j in rows if j["kind"] == want]
            return self._json(200, {
                "tasks": [{"id": j["id"], "kind": j["kind"],
                           "state": j["state"], "outcome": j["outcome"],
                           "sample_id": j["sample_id"],
                           "created_at": j["created_at"],
                           "finished_at": j["finished_at"],
                           "conv_id": j["conv_id"],
                           "detail_url": f"/api/tasks/{j['id']}"}
                          for j in rows[:lim]]})
        if path == "/api/jobs":
            # P1-9: the jobs list with the trash as an explicit
            # listing dimension (?trashed=1 shows only trashed
            # records; the default never does). Research rows have no
            # trash state and are not job records - this endpoint
            # lists jobs only.
            tv = (q.get("trashed") or ["0"])[0]
            trash = {"1": "only", "all": "all"}.get(tv, "normal")
            rows = [job_public(j) for j in store.job_list(200,
                                                          trash=trash)]
            return self._json(200, {"jobs": rows})
        if path == "/api/attachments":
            cid = (q.get("conv_id") or [None])[0]
            return self._json(200, {"attachments":
                                    [attach_public(a) for a in
                                     store.att_list(cid)],
                                    "max_bytes": ATTACH_MAX_BYTES,
                                    "kinds": sorted(set(
                                        ATTACH_KINDS.values()))})
        if path.startswith("/api/attachments/") and \
                path.endswith("/download"):
            a = store.att_get(path.split("/")[3])
            if not a:
                return self._json(404, {"error": "no such attachment"})
            try:
                with open(a["path"], "rb") as f:
                    data = f.read()
            except OSError as e:
                return self._json(410, {"error": "stored file is "
                                                 "unreadable: " +
                                                 str(e)[:160]})
            got = hashlib.sha256(data).hexdigest()
            # the download must BE what was registered - a stored file
            # that no longer matches its row is refused, not served.
            # Server picks register no checksum (hashing a multi-GB
            # original on pick is not the operator's ask), so only
            # uploads are byte-pinned here.
            if a["sha256"] and got != a["sha256"]:
                return self._json(500, {"error": "stored bytes no "
                                                 "longer match the "
                                                 "registered checksum"})
            return self._file(data, a["name"],
                              a["mime"] or "application/octet-stream",
                              extra={"X-Attachment-Id": a["id"]})
        if path == "/api/files":
            # P1-10 intake path 2: a PICKER over the authorized input
            # roots. It lists one directory level, refuses anything
            # outside the roots, and follows nothing across a root
            # boundary (every entry is realpath-checked).
            try:
                d = (q.get("dir") or [""])[0]
                if d:
                    rp = under_authorized_root(d)
                    if not os.path.isdir(rp):
                        return self._json(404, {"error": "no such "
                                                         "directory"})
                else:
                    rp = ""
            except PermissionError as e:
                return self._json(403, {"error": str(e)})
            if not rp:
                roots = [{"name": r, "path": r, "dir": True}
                         for r in native_mod.AUTHORIZED_INPUT_ROOTS
                         if os.path.isdir(r)]
                return self._json(200, {"dir": "", "roots": True,
                                        "entries": roots})
            entries, skipped = [], 0
            try:
                names = sorted(os.listdir(rp))
            except OSError as e:
                return self._json(403, {"error": str(e)[:200]})
            for n in names:
                if n.startswith(".") or len(entries) >= 500:
                    skipped += 1
                    continue
                fp = os.path.join(rp, n)
                try:
                    frp = os.path.realpath(fp)
                    under_authorized_root(frp)
                except PermissionError:
                    skipped += 1
                    continue
                try:
                    st_ = os.stat(frp)
                except OSError:
                    continue
                ext = os.path.splitext(n)[1].lower()
                entries.append({"name": n, "path": frp,
                                "dir": os.path.isdir(frp),
                                "size": st_.st_size,
                                "seq": ext in ATTACH_SEQ_EXT,
                                "allowed": (not os.path.isdir(frp))
                                and ext in ATTACH_KINDS})
            return self._json(200, {"dir": rp, "roots": False,
                                    "entries": entries,
                                    "skipped": skipped})
        if path == "/api/tasks/active":
            # B2: which run belongs to THIS conversation? Never guess the
            # most-recent job: no conv link -> empty list, not a fallback.
            cid = (q.get("conv_id") or [""])[0]
            rows = [j for j in unified_tasks(store, 200)
                    if j["state"] in ("queued", "running", "paused",
                                      "blocked")
                    and ((j.get("conv_id") or None) == (cid or None))]
            return self._json(200, {"conv_id": cid,
                                    "tasks": [job_public(j)
                                              for j in rows]})
        if path.startswith("/api/tasks/"):
            parts = path.strip("/").split("/")  # api/tasks/<id>[/snapshot]
            jid = parts[2]
            job = store.job_get(jid)
            if not job:
                rr = store.research_get(jid)
                if rr:
                    # research task detail: derived, honest about where
                    # the full report and the event trail live
                    row = next((x for x in research_task_rows(store)
                                if x["id"] == jid), None) or {}
                    prog = rr.get("progress") or {}
                    return self._json(200, {
                        **{k: row.get(k) for k in
                           ("id", "kind", "state", "outcome", "sample_id",
                            "created_at", "finished_at", "conv_id")},
                        "research": True,
                        "scope": rr["scope"],
                        "engine": rr.get("engine"),
                        "targets": prog.get("targets") or [],
                        "steps": (prog.get("steps") or [])[-20:],
                        "status_url": f"/api/research/{jid}",
                        "events_url": f"/api/research/{jid}/events",
                        "has_report": bool(rr.get("report"))})
                return self._json(404, {"error": "no such task"})
            if len(parts) > 3 and parts[3] == "snapshot":
                snap = store.snapshot_get(jid)
                if not snap:
                    return self._json(404, {"error": "no snapshot for "
                                                     "this task"})
                return self._json(200, snap)
            return self._json(200, task_detail(store, job))
        if path == "/api/inventory":
            return self._json(200, load_inventory())
        if path == "/api/native/tree":
            # M9 §5.3: real one-level directory listing for the
            # input-review tree (sizes from stat, roles from the native
            # detector only). Confined to authorized input directories.
            ddir = (q.get("data_dir") or [""])[0]
            sub = (q.get("sub") or [""])[0]
            res = native_mod.native_tree(ddir, sub)
            if not res.get("ok"):
                return self._json(400, res)
            return self._json(200, res)
        if path == "/api/native/capability":
            return self._json(200, native_mod.probe(force=True))
        if path == "/api/native/gateway":
            return self._json(200, native_mod.gateway_status())
        # ---------- skills (PART C) ----------
        if path == "/api/skills":
            return self._json(200, {"skills": skills_mod.activation_state(
                store), "categories": skills_mod.CATEGORIES})
        if path.startswith("/api/skills/"):
            parts = path.strip("/").split("/")
            slug = parts[2]
            s = store.skill_get(slug, by_slug=True)
            if not s:
                return self._json(404, {"error": "no such skill"})
            if len(parts) > 3 and parts[3] == "versions":
                return self._json(200, {
                    "skill": {k: s[k] for k in
                              ("id", "slug", "name", "title", "category",
                               "version", "enabled")},
                    "versions": store.skill_versions(s["id"])})
            s["versions"] = store.skill_versions(s["id"])
            s["activation"] = next(
                (a for a in skills_mod.activation_state(store)
                 if a["slug"] == slug), None)
            return self._json(200, s)
        # ---------- research ----------
        if path == "/api/research":
            return self._json(200, {"research": research_public_rows(store)})
        if path.startswith("/api/research/"):
            parts = path.strip("/").split("/")
            rid = parts[2]
            if len(parts) > 3 and parts[3] == "events":
                after = int((q.get("after") or ["0"])[0])
                return self._json(200, {"events": store.research_events(
                    rid, after)})
            if len(parts) > 3 and parts[3] == "exports":
                # what exists on disk for this report, pinned per content
                return self._json(200, {"exports": [
                    {k: e[k] for k in
                     ("id", "kind", "object_id", "format", "filename",
                      "bytes", "sha256", "source_hash", "version",
                      "created_at")}
                    for e in store.export_list("research", rid)]})
            if len(parts) > 4 and parts[3] == "export":
                return self._serve_export("research", rid, parts[4], q)
            if len(parts) > 3 and parts[3] in ("compatibility",
                                               "adoption-plan"):
                return self._research_action(rid, parts[3], q)
            r = store.research_get(rid)
            if not r:
                return self._json(404, {"error": "no such report"})
            # lit_records is the resume-durability store (verbatim
            # retrieved records) - needed by the engine's progress,
            # never displayed; strip it from the polled response
            prog = r.get("progress")
            if isinstance(prog, dict) and "lit_records" in prog:
                prog = dict(prog)
                prog.pop("lit_records")
                r = {**r, "progress": prog}
            return self._json(200, r)
        if path.startswith("/api/jobs/"):
            jid = path.strip("/").split("/")[2]  # api/jobs/<id>[/events]
            if not store.job_get(jid):
                # unknown job id -> honest 404, never an empty 200 list
                # (this masked GET route misses in earlier smoke tests)
                return self._json(404, {"error": "no such job"})
            after = int((q.get("after") or ["0"])[0])
            return self._json(200, store.event_list(jid, after, 1000))
        if path.startswith("/api/results/"):
            # A3: Results are read from the ARTIFACT MANIFEST - the live
            # artifact rows are authoritative, never a stored claim.
            parts = path.strip("/").split("/")  # api/results/<id>[/bundle]
            jid = parts[2]
            if len(parts) > 3 and parts[3] == "bundle":
                # M5 PART 7: scoped results ZIP built from persisted job
                # state; re-download serves the pinned bytes
                return self._serve_export("job", jid, "zip", q)
            if len(parts) > 3 and parts[3] == "exports":
                return self._json(200, {"exports": [
                    {k: e[k] for k in
                     ("id", "kind", "object_id", "format", "filename",
                      "bytes", "sha256", "source_hash", "version",
                      "created_at")}
                    for e in store.export_list("job", jid)]})
            if len(parts) > 4 and parts[3] == "artifact":
                # M10 §14: serve ONE registered artifact's real bytes.
                # The path is whatever the artifact row recorded (never
                # client input) and must live under our scratch root.
                arts = store.artifact_list(jid)
                want = parts[4]
                row = next((a for a in arts if str(a["id"]) == want
                            or a["name"] == want), None)
                if not row or not row.get("path"):
                    return self._json(404, {"error": "no such artifact"})
                p = os.path.realpath(str(row["path"]))
                root = os.path.realpath(native_mod.WORK)
                if not p.startswith(root + os.sep) or not os.path.isfile(p):
                    return self._json(403, {"error": "artifact path is "
                                                     "outside the served "
                                                     "root or missing"})
                try:
                    with open(p, "rb") as f:
                        data = f.read()
                except OSError as e:
                    return self._json(500, {"error": str(e)[:200]})
                ctype = ("text/html" if p.endswith(".html") else
                         "text/tab-separated-values" if p.endswith(".tsv")
                         else "text/plain")
                return self._file(data, os.path.basename(p), ctype)
            job = store.job_get(jid)
            if not job:
                return self._json(404, {"error": "no such job"})
            if job["state"] == "failed":
                # M10 §14: a failed formal run is still a RESULTS subject
                # - its structured failure record + whatever partial
                # artifacts were registered for forensics. No candidates
                # are invented for it.
                res = json.loads(job["result_json"] or "{}")
                return self._json(200, {
                    "job_id": jid, "state": "failed",
                    "outcome": job.get("outcome") or "generation_failed",
                    "failure": res.get("failure") or
                    {"code": res.get("failure_code"),
                     "reason": job.get("error")},
                    "failure_detail": res.get("failure_detail"),
                    "completed_before_failure":
                        res.get("completed_before_failure") or [],
                    "artifacts": store.artifact_list(jid),
                    "outdir": res.get("outdir"),
                    "engine": res.get("engine"),
                    "note": "partial outputs (if any) are registered for "
                            "forensics; nothing here is a delivered "
                            "result"})
            if job["state"] != "completed":
                return self._json(409, {"error": f"job is {job['state']}"})
            res = json.loads(job["result_json"] or "{}")
            arts = store.artifact_list(jid)
            from .jobs import classify_outcome
            # requested-but-absent artifacts count as missing: the run may
            # never claim delivery of what the table no longer shows
            man = res.get("manifest") or {}
            req_strat = (man.get("effective_config") or {}).get(
                "ranking_artifacts") or []
            have = {a.get("strategy") for a in arts}
            gone = [s for s in req_strat if s not in have]
            live_outcome = classify_outcome(
                arts + [{"status": "missing", "name": s} for s in gone])
            res["artifacts"] = arts
            if res.get("manifest") is not None:
                res["manifest"]["artifacts"] = arts
            if (res.get("engine") == "native" and not res.get("candidates")):
                # M10 §14.3: for a NATIVE run the shortlist IS the real
                # ranked table the pipeline produced. We serve its
                # header + first rows VERBATIM (no web-layer
                # reinterpretation of scientific columns).
                res["preview_tables"] = _native_preview_tables(arts)
            if gone:
                res["missing_artifacts"] = gone
            if res.get("outcome") != live_outcome:
                # drift between stored result and artifact table: tell the
                # truth about what exists NOW, and say so
                res["outcome_drift"] = (
                    f"stored outcome was '{res.get('outcome')}' but the "
                    f"artifact table now says '{live_outcome}'"
                    + (f" - requested artifact(s) {gone} are absent"
                       if gone else ""))
                res["outcome"] = live_outcome
            res.setdefault("job_id", jid)
            return self._json(200, res)
        return self._json(404, {"error": "not found", "path": path})

    # ---------- POST ----------
    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        # Milestone 5 formal-job routing (PART 1, enforceable): the
        # task-query token grants exactly ONE write - creating a job
        # via POST /api/jobs. It is never sufficient for any other
        # endpoint (chat, settings, approvals, skills, admin all keep
        # Basic auth only). A token-created job still starts BLOCKED
        # behind the same human approval card as a GUI-submitted one.
        scoped = (path == "/api/jobs" and not self._authorized()
                  and self._task_token_ok())
        if not self._authorized() and not scoped:
            return self._unauthorized()
        try:
            body = self._body()
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        if scoped:
            body["requested_by"] = "agent"
            body["requested_by_kind"] = "task_query_token"
        try:
            if path == "/api/attachments":
                # P1-10 intake path 1: browser upload. The stored copy
                # lives under the data dir, keyed by a stable id; the
                # name is reduced to its basename (no traversal), the
                # size cap counts DECODED bytes, and oversize/unknown
                # types are refused loudly with the reason.
                raw_name = str(body.get("name") or "").replace("\\", "/")
                name = os.path.basename(raw_name).strip()
                if not name or name.startswith("."):
                    return self._json(400, {"error": "a real file "
                                                     "name is required"})
                try:
                    kind, ext = attach_kind(name)
                except ValueError as e:
                    return self._json(415, {"error": str(e)})
                b64 = str(body.get("data_b64") or "")
                if not b64:
                    return self._json(400, {"error": "data_b64 required"})
                if len(b64) > (ATTACH_MAX_BYTES // 3 + 1) * 4 + 1024:
                    return self._json(413, {
                        "error": "file_too_big",
                        "detail": f"the file is over the "
                                  f"{ATTACH_MAX_BYTES}-byte cap",
                        "limit": ATTACH_MAX_BYTES})
                try:
                    data = base64.b64decode(b64, validate=True)
                except Exception:
                    return self._json(400, {"error": "data_b64 is not "
                                                     "valid base64"})
                if not data:
                    return self._json(400, {"error": "empty file"})
                if len(data) > ATTACH_MAX_BYTES:
                    store.audit("user", "attachment.rejected_too_big",
                                "", {"name": name,
                                     "actual": len(data),
                                     "limit": ATTACH_MAX_BYTES})
                    return self._json(413, {
                        "error": "file_too_big",
                        "limit": ATTACH_MAX_BYTES, "actual": len(data),
                        "detail": f"file is {len(data)} bytes, over "
                                  f"the {ATTACH_MAX_BYTES}-byte cap; "
                                  "nothing was stored"})
                cid = str(body.get("conv_id") or "")
                if cid and not store.conv_get(cid):
                    return self._json(404, {"error": "no such "
                                                     "conversation"})
                aid = new_id("att")
                adir = attach_dir() / aid
                adir.mkdir(parents=True)
                fp = adir / name
                fp.write_bytes(data)
                att = {"id": aid, "conv_id": cid, "created_at": now(),
                       "name": name,
                       "mime": ATTACH_MIME_BY_EXT.get(ext, ""),
                       "kind": kind, "size": len(data),
                       "sha256": hashlib.sha256(data).hexdigest(),
                       "path": str(fp), "source": "upload",
                       "origin": ""}
                store.att_add(att)
                store.audit("user", "attachment.upload", aid,
                            {"name": name, "size": len(data),
                             "sha256": att["sha256"], "conv_id": cid})
                return self._json(201, {"attachment":
                                        attach_public(att)})
            if path == "/api/attachments/pick":
                # P1-10 intake path 2: reference an EXISTING file
                # under an authorized root, in place - nothing is
                # copied, the original is never moved and never
                # deleted (even when the attachment row goes away).
                p0 = str(body.get("path") or "")
                try:
                    rp = under_authorized_root(p0)
                except PermissionError as e:
                    return self._json(403, {"error": str(e)})
                if not os.path.isfile(rp):
                    return self._json(404, {"error": "not a readable "
                                                     "file under an "
                                                     "authorized root"})
                nm = os.path.basename(rp)
                try:
                    kind, ext = attach_kind(nm)
                except ValueError as e:
                    return self._json(415, {"error": str(e)})
                cid = str(body.get("conv_id") or "")
                aid = new_id("att")
                att = {"id": aid, "conv_id": cid, "created_at": now(),
                       "name": nm, "mime": ATTACH_MIME_BY_EXT.get(ext, ""),
                       "kind": kind, "size": os.path.getsize(rp),
                       "sha256": "", "path": rp,
                       "source": "server_pick", "origin": rp}
                store.att_add(att)
                store.audit("user", "attachment.pick", aid,
                            {"path": rp, "kind": kind, "conv_id": cid})
                return self._json(201, {"attachment":
                                        attach_public(att)})
            if path.startswith("/api/attachments/") and \
                    path.endswith("/delete"):
                aid = path.split("/")[3]
                a = store.att_get(aid)
                if not a:
                    return self._json(404, {"error": "no such "
                                                     "attachment"})
                store.att_delete(aid)
                note = "original untouched (server pick - never " \
                    "deleted through this door)"
                if a["source"] == "upload":
                    try:
                        os.unlink(a["path"])
                        os.rmdir(os.path.dirname(a["path"]))
                        note = "stored copy deleted"
                    except OSError as e:
                        note = f"stored copy removal failed: {str(e)[:140]}"
                store.audit("user", "attachment.deleted", aid,
                            {"name": a["name"], "source": a["source"],
                             "path": a["path"], "file": note})
                return self._json(200, {"ok": True, "file": note})
            if path == "/api/chat":
                raw = str(body.get("text", ""))
                if len(raw) > MAX_CHAT_CHARS:
                    # Loud rejection, never a silent slice: the previous
                    # behaviour lost the tail of the user's own prompt
                    # while still reporting a successful send. Nothing is
                    # persisted and nothing is dispatched on this path.
                    rcid = str(body.get("conv_id", "") or "")
                    store.audit("user", "chat.rejected_too_long", rcid,
                                {"limit": MAX_CHAT_CHARS,
                                 "actual": len(raw)})
                    return self._json(413, {
                        "error": "message_too_long",
                        "limit": MAX_CHAT_CHARS,
                        "actual": len(raw),
                        "detail": f"Message is {len(raw)} characters, "
                                  f"over the {MAX_CHAT_CHARS}-character "
                                  f"limit. Nothing was saved or sent to "
                                  f"the agent."})
                text = raw
                cid = str(body.get("conv_id", "") or "")
                # P1-10: validate attachments BEFORE anything is
                # created - a bad attachment id must not leave an
                # empty conversation behind.
                att_ids = body.get("attachment_ids") or []
                att_rows = []
                if att_ids:
                    if not isinstance(att_ids, list) or len(att_ids) > 10:
                        return self._json(400, {
                            "error": "attachment_ids must be a list "
                                     "of at most 10 ids"})
                    for x in att_ids:
                        a = store.att_get(str(x))
                        if not a:
                            return self._json(400, {
                                "error": f"no such attachment: {x}"})
                        att_rows.append(a)
                conv = store.conv_get(cid) if cid else None
                if not conv or conv.get("deleted"):
                    conv = store.conv_create("")  # first message starts a chat
                    cid = conv["id"]
                if not (conv.get("title") or "").strip():
                    store.conv_set_title(cid, agent.title_from(text))
                # P1-10: attachments travel as PATHS. The dispatch
                # text gains a block naming the workspace files the
                # agent may read - the file BYTES are never embedded,
                # encoded or forwarded anywhere, and the egress audit
                # records exactly what left (path + identity).
                # (att_rows was validated above, before creation.)
                if att_rows:
                    lines = "\n".join(
                        f"- {a['path']} ({a['name']}, {a['kind']}, "
                        f"{a['size']} bytes)" for a in att_rows)
                    text = ((text + "\n\n") if text else "") + \
                        "[attached files - read these paths; the " \
                        "bytes are NOT inline]\n" + lines
                # log field is an explicitly-named preview; the FULL
                # text is persisted as the message row itself
                store.audit("user", "chat.send", cid,
                            {"text_head": text[:120],
                             "chars": len(text)})
                for a in att_rows:
                    if not a["conv_id"]:
                        store.att_link_conv(a["id"], cid)
                    store.audit("user", "attachment.egress", a["id"],
                                {"path": a["path"], "name": a["name"],
                                 "kind": a["kind"], "conv_id": cid,
                                 "what_left": "path only - file "
                                              "bytes never leave"})
                meta = ({"attachments": [a["id"] for a in att_rows]}
                        if att_rows else {})
                msgs = [store.msg_add("user", text, meta, cid)]
                mode = store.setting("agent_mode", "mock")
                if mode == "real":
                    msgs += agent.handle_real(text, cid)
                else:
                    msgs += agent.handle(text, cid)
                store.conv_touch(cid)
                publish({"type": "chat", "conv_id": cid,
                         "conversations": True})
                return self._json(200, {"messages": msgs, "conv_id": cid,
                                        "conversation": store.conv_get(cid)})
            if path == "/api/chat/stop":
                # B4: user-facing interrupt for the live agent turn
                cid = str(body.get("conv_id", "") or "")
                return self._json(200, agent.stop_turn(cid))
            if path == "/api/conversations":
                conv = store.conv_create(str(body.get("title", ""))[:120])
                store.audit("user", "conv.create", conv["id"], {})
                publish({"type": "conversations"})
                return self._json(201, {"conversation": conv})
            if path.startswith("/api/conversations/"):
                parts = path.strip("/").split("/")  # api/conversations/<id>/<act>
                cid, act = parts[2], (parts[3] if len(parts) > 3 else "")
                conv = store.conv_get(cid)
                if not conv or conv.get("deleted"):
                    return self._json(404, {"error": "no such conversation"})
                if act == "rename":
                    title = str(body.get("title", "")).strip()[:120]
                    if not title:
                        return self._json(400, {"error": "title required"})
                    conv = store.conv_set_title(cid, title)
                    store.audit("user", "conv.rename", cid, {"title": title})
                    publish({"type": "conversations"})
                    return self._json(200, {"conversation": conv})
                if act == "delete":  # soft: hidden in UI, rows retained
                    ok = store.conv_delete(cid)
                    store.audit("user", "conv.delete", cid, {"soft": True})
                    publish({"type": "conversations"})
                    return self._json(200, {"ok": ok, "deleted": cid})
                if act == "flag":
                    # M6 PART F4: sidebar menu Rename/Pin/Archive. Only
                    # these two booleans move here; title changes keep
                    # their own audited rename endpoint.
                    body2 = {}
                    for k in ("pinned", "archived"):
                        if k in body:
                            if body[k] not in (True, False, 0, 1):
                                return self._json(400, {
                                    "error": f"{k} must be a boolean"})
                            body2[k] = 1 if body[k] else 0
                    if not body2:
                        return self._json(400, {"error": "no flags given"})
                    conv2 = store.conv_set_flags(cid, **body2)
                    store.audit("user", "conv.flag", cid, body2)
                    publish({"type": "conversations"})
                    return self._json(200, {"conversation": conv2})
                if act == "example":
                    msgs = agent.load_example(cid)
                    if not (conv.get("title") or "").strip():
                        store.conv_set_title(cid, "Example: DEMO-P001")
                    store.conv_touch(cid)
                    publish({"type": "chat", "conv_id": cid,
                             "conversations": True})
                    return self._json(200, {"messages": msgs,
                                            "conv_id": cid})
                return self._json(404, {"error": "not found", "path": path})
            if path == "/api/agent/mode":
                mode = str(body.get("mode", ""))
                if mode not in ("mock", "real"):
                    return self._json(400, {"error": "mode must be mock|real"})
                # M6 PART F5: active operations are protected at the
                # BACKEND (a dropdown is UX; this is the guarantee): no
                # mode switch while a chat turn or an analysis job is
                # in flight, whatever UI asked for it.
                if mode != store.setting("agent_mode", "mock"):
                    act = agent.active_work()
                    if act["busy_chats"] or act["running_jobs"]:
                        return self._json(409, {
                            "error": "an operation is in flight ("
                                     + f"{act['busy_chats']} chat turn(s), "
                                     + f"{act['running_jobs']} running "
                                       "job(s)) - wait for it to finish "
                                     + "or cancel it, then switch"})
                store.setting_set("agent_mode", mode)
                store.audit("user", "agent.mode.set", mode, {})
                publish({"type": "agent_mode", "mode": mode})
                return self._json(200, {"agent_mode": mode})
            if path == "/api/manifests/propose":
                desc = str(body.get("description", ""))[:1000]
                if not desc.strip():
                    return self._json(400, {"error": "description required"})
                proposal = agent.propose_manifest(desc)
                mid = new_id("man")
                m = store.manifest_add(mid, desc, proposal)
                store.audit("user", "manifest.propose", mid,
                            {"sample": proposal["sample_id"]})
                publish({"type": "manifest", "id": mid})
                return self._json(201, m)
            if path.startswith("/api/manifests/") and \
                    path.endswith("/confirm"):
                mid = path.split("/")[3]
                m = store.manifest_get(mid)
                if not m:
                    return self._json(404, {"error": "no such manifest"})
                if m["status"] != "proposed":
                    return self._json(409, {"error": "manifest not proposed"})
                edits = body.get("edits") or {}
                m2 = store.manifest_set(mid, "confirmed", confirmed=edits)
                store.audit("user", "manifest.confirm", mid,
                            {"edits": list(edits.keys())})
                publish({"type": "manifest", "id": mid})
                return self._json(200, m2)
            if path == "/api/jobs":
                if str(body.get("engine", "mock")) == "native":
                    # M6 PART D1/D2: the ordinary entry is ONE flow -
                    # data directory -> discovery -> confirmed manifest
                    # -> this call with manifest_id. The CONFIRMED
                    # manifest is authoritative: hand-typed params
                    # cannot override it. `native_profile` remains only
                    # as the agent/taskquery compatibility path; the UI
                    # never sends it, and `synthetic_demo: true` is the
                    # explicit bundled-fixture request.
                    profile = str(body.get("native_profile", "") or "")
                    nparams = body.get("native_params") or {}
                    if not isinstance(nparams, dict):
                        return self._json(400, {"error": "native_params must "
                                                         "be an object"})
                    mid = str(body.get("manifest_id") or "")
                    if mid:
                        man = store.manifest_get(mid)
                        if not man:
                            return self._json(400, {"error":
                                                    "no such manifest"})
                        if man.get("status") != "confirmed":
                            return self._json(409, {
                                "error": "the manifest is the "
                                         "authoritative input - confirm "
                                         "it before submitting"})
                        nparams = {**nparams,
                                   **native_mod.params_from_manifest(man)}
                    if body.get("synthetic_demo"):
                        profile = "demo"
                    if not profile:
                        profile = "gateway" if nparams else "demo"
                    sid = str(nparams.get("sample_id")
                              or body.get("sample_id")
                              or ("DEMO-NEOAG" if profile == "demo"
                                  else ""))
                    if not sid:
                        return self._json(400, {
                            "error": "native jobs need inputs: confirm a "
                                     "data-directory manifest, pass "
                                     "native_params, or ask for the "
                                     "bundled synthetic demo"})
                    nparams.setdefault("sample_id", sid)
                    # M6 PART E: scientific overrides are validated HERE,
                    # not by trusting the form - a value outside the
                    # deployed code's real options/ranges never reaches a
                    # run, whatever origin (GUI, chat, taskquery) posted it.
                    bad = native_mod.validate_scientific(nparams)
                    if bad:
                        return self._json(400, {
                            "error": "unvalidated scientific configuration: "
                                     + "; ".join(bad)})
                    # M10 §12 (UI path): a confirmed manifest is
                    # SINGLE-USE while its job is still active - a second
                    # Start click (or a second browser tab) gets the live
                    # task's id back, never a duplicate queue entry.
                    if mid:
                        _man = store.manifest_get(mid) or {}
                        _lj = str(_man.get("job_id") or "")
                        if _lj:
                            _j2 = store.job_get(_lj)
                            if _j2 and _j2["state"] in ("queued", "running",
                                                        "paused", "blocked"):
                                return self._json(409, {
                                    "error": "duplicate_active",
                                    "job_id": _lj,
                                    "detail": ("this confirmed input set "
                                               "already has an active run: "
                                               f"{_lj} ({_j2['state']})")})
                    # M10 §12: a task-query (agent) submission may not
                    # stack duplicates of a run that is ALREADY active -
                    # the honest answer is the id of the live task, never
                    # a second queue entry for the same work.
                    if scoped:
                        for j in store.job_list(200):
                            if (j["state"] in ("queued", "running",
                                               "paused", "blocked")
                                    and j["kind"] == "neoag-native-run"
                                    and (j["sample_id"] == sid
                                         or (mid and str(
                                             json.loads(j["spec_json"] or
                                                        "{}").get(
                                                "manifest_id") or "")
                                             == mid))):
                                return self._json(409, {
                                    "error": "duplicate_active",
                                    "job_id": j["id"],
                                    "detail": ("an active native run for "
                                               "this sample already exists: "
                                               f"{j['id']} ({j['state']}) - "
                                               "query it via taskquery "
                                               "instead of re-submitting")})
                    # M10 §9: server-side revalidation at submit - the
                    # cheap live checks (runtime, manifests of record,
                    # input files still present, authorized roots,
                    # writable scratch). A predictable failure is blocked
                    # HERE with ALL of its reasons, not queued to fail.
                    recheck = readiness_mod.assess(
                        store, profile=profile, params=nparams,
                        manifest_id=mid, run_planner=False)
                    if recheck["status"] == "BLOCKED":
                        return self._json(409, {
                            "error": "readiness_blocked",
                            "blockers": recheck["blockers"],
                            "warnings": recheck["warnings"],
                            "detail": "; ".join(recheck["blockers"])})
                    ex_mode = str(body.get("execution_mode")
                                  or "autonomous")
                    if ex_mode not in ("autonomous", "controlled"):
                        return self._json(400, {
                            "error": "execution_mode must be "
                                     "autonomous or controlled"})
                    # M10 §10: the human's explicit Start Analysis click
                    # (Basic-auth session, never the agent token) carries
                    # its authorization THROUGH to the backend record.
                    authorized_start = None
                    if (str(body.get("authorized") or "")
                            == "start_analysis" and not scoped):
                        authorized_start = {
                            "authorized": True,
                            "scope": (f"confirmed manifest {mid} "
                                      f"({sid})" if mid else
                                      f"native params for {sid}")}
                    try:
                        res = jobs.submit_native(
                            sid, profile=profile, native_params=nparams,
                            stub=bool(body.get("stub")),
                            requested_by=str(body.get("requested_by",
                                                      "user")),
                            conv_id=str(body.get("conv_id", "") or ""),
                            execution_mode=ex_mode,
                            authorized_start=authorized_start)
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
                    if mid:
                        store.manifest_set(mid, "linked", job_id=res[
                            "job_id"])
                        store.audit("user", "manifest.linked", mid,
                                    {"job": res["job_id"]})
                    if scoped:
                        store.audit("agent", "job.create.scoped_token",
                                    res.get("job_id", ""),
                                    {"engine": "native",
                                     "profile": profile, "sample_id": sid})
                    return self._json(202, res)
                stage_ids = body.get("stage_ids") or [
                    s["id"] for s in STAGES if s["required"]]
                kind = str(body.get("kind", "analysis"))
                if kind not in ("analysis", "discovery", "diagnostic",
                                "benchmark"):
                    return self._json(400, {"error": "invalid kind"})
                if kind == "benchmark":
                    # M5 PART 6: the brief is validated BEFORE anything is
                    # queued; the job blocks on an approval card that
                    # carries the full experiment. Nothing runs on submit.
                    try:
                        res = jobs.submit_benchmark(
                            body.get("benchmark") or {},
                            sample_id=str(body.get("sample_id")
                                          or "DEMO-BENCH"),
                            requested_by=str(body.get("requested_by",
                                                      "user")),
                            conv_id=str(body.get("conv_id", "") or ""))
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
                    if scoped:
                        store.audit("agent", "job.create.scoped_token",
                                    res.get("job_id", ""),
                                    {"kind": "benchmark"})
                    return self._json(202, res)
                rcfg = body.get("requested_config")
                if rcfg is not None and not isinstance(rcfg, dict):
                    return self._json(400, {"error": "requested_config must be an object"})
                res = jobs.submit(str(body.get("sample_id", "DEMO-P001")),
                                  list(stage_ids),
                                  str(body.get("mode", "auto")),
                                  execution_mode=str(body.get(
                                      "execution_mode", "controlled")),
                                  simulate_failure=bool(body.get(
                                      "simulate_failure")),
                                  kind=kind,
                                  manifest_id=body.get("manifest_id"),
                                  requested_config=rcfg,
                                  conv_id=str(body.get("conv_id", "")
                                              or ""),
                                  requested_by=str(
                                      body.get("requested_by", "user")))
                if scoped:
                    store.audit("agent", "job.create.scoped_token",
                                res.get("job_id", ""),
                                {"engine": "mock", "kind": kind,
                                 "sample_id": body.get("sample_id", "")})
                return self._json(202, res)
            if path == "/api/native/discover":
                # M7 PART 2: ONE authorized directory in, the NATIVE
                # NeoAG detector's answer out (routing.scan_input_directory
                # + inventory + router, run in the native environment).
                # Invalid/unauthorized directories fail clearly (400);
                # nothing is ever fabricated or rerouted to a demo path.
                ddir = str(body.get("data_dir") or "").strip()
                disc = native_mod.discover_inputs(ddir)
                store.audit("user", "native.discover", "",
                            {"data_dir": ddir, "ok": disc.get("ok"),
                             "found": len(disc.get("inputs") or []),
                             "ambiguous": len(disc.get("ambiguous") or [])})
                if not disc.get("ok"):
                    return self._json(400, disc)
                return self._json(200, disc)
            if path == "/api/native/manifest":
                # the human's confirmation step: write the CONFIRMED
                # manifest in the native open-neo-sample-v1 schema.
                # Ambiguities are resolved by PICKING among the detected
                # candidates (validated inside the scanned directory),
                # never by free-path typing.
                ddir = str(body.get("data_dir") or "").strip()
                picked = body.get("picked") or {}
                if not isinstance(picked, dict):
                    return self._json(400, {"error": "picked must be an "
                                                     "object"})
                try:
                    proposal = native_mod.write_native_manifest(
                        ddir, str(body.get("sample_id") or ""),
                        picked={str(k): str(v) for k, v in picked.items()})
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                mid = new_id("man")
                m = store.manifest_add(mid, proposal[
                    "source_description"], proposal)
                # M7 §2: THIS call is the human's confirmation step - the
                # ambiguities above were resolved by picking real files.
                # Recording it confirmed here is what makes the manifest
                # the single source of truth: planning and execution
                # receive this same record, never a re-entered copy.
                m = store.manifest_set(mid, "confirmed", confirmed={
                    "sample_id": proposal["sample_id"],
                    "picked": picked,
                    "method": "native discovery + human picks"}) or m
                store.audit("user", "manifest.confirm.native", mid,
                            {"data_dir": ddir,
                             "inputs": len(proposal["inputs"]),
                             "picked": len(picked)})
                publish({"type": "manifest", "id": mid})
                return self._json(201, m)
            if path == "/api/jobs/native/readiness":
                # M10 §9: ONE comprehensive pre-submission check of the
                # ENTIRE selected workflow. Runs the real native planner
                # over the resolved configuration (read-only) and merges
                # its verdict with the deployment-side checks; ALL
                # blockers come back together. Nothing is faked: every
                # check reports the state it actually measured.
                nparams = body.get("native_params") or {}
                if not isinstance(nparams, dict):
                    return self._json(400, {"error": "native_params must be "
                                                     "an object"})
                mid = str(body.get("manifest_id") or "")
                if mid:
                    man = store.manifest_get(mid)
                    if not man:
                        return self._json(400, {"error": "no such manifest"})
                    nparams = {**nparams,
                               **native_mod.params_from_manifest(man)}
                profile = str(body.get("native_profile", "") or "")
                if body.get("synthetic_demo"):
                    profile = "demo"
                if not profile:
                    profile = "gateway" if nparams else "demo"
                sid = str(nparams.get("sample_id")
                          or nparams.get("case_id") or "")
                if not sid and profile == "gateway":
                    return self._json(400, {"error": "nothing to assess: "
                                                     "no sample/inputs"})
                nparams.setdefault("sample_id", sid or "DEMO-NEOAG")
                nparams.setdefault("case_id", sid or "DEMO-NEOAG")
                out = readiness_mod.assess(store, profile=profile,
                                           params=nparams,
                                           manifest_id=mid)
                store.audit("user", "native.readiness", "",
                            {"status": out["status"],
                             "blockers": len(out["blockers"]),
                             "sample": sid})
                return self._json(200, out)
            if path == "/api/jobs/native/preview":
                # Real native planning/preflight WITHOUT executing: the
                # planner decides routes; we only display them. Read-only
                # on inputs; writes only into the scratch preview dir.
                nparams = body.get("native_params") or {}
                if not isinstance(nparams, dict):
                    return self._json(400, {"error": "native_params must be "
                                                     "an object"})
                if body.get("manifest_id"):
                    man = store.manifest_get(str(body["manifest_id"]))
                    if not man:
                        return self._json(400, {"error": "no such manifest"})
                    nparams = {**nparams,
                               **native_mod.params_from_manifest(man)}
                sid = str(nparams.get("sample_id")
                          or nparams.get("case_id") or "")
                if not sid:
                    return self._json(400, {"error": "native_params.sample_id "
                                                     "required"})
                nparams.setdefault("sample_id", sid)
                nparams.setdefault("case_id", sid)
                bad = native_mod.validate_scientific(nparams)
                if bad:
                    return self._json(400, {
                        "error": "unvalidated scientific configuration: "
                                 + "; ".join(bad)})
                if not native_mod.probe()["available"]:
                    return self._json(200, {
                        "ok": False, "status": "NATIVE_UNAVAILABLE",
                        "detail": "native NeoAG not importable on this "
                                  "host - preview is honestly unavailable, "
                                  "not faked"})
                outdir = native_mod.job_outdir(
                    "preview-" + time.strftime("%Y%m%d%H%M%S"))
                plan = native_mod.run_plan(nparams, outdir)
                plan["outdir"] = outdir
                return self._json(200, plan)
            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                jid = path.split("/")[3]
                ok = jobs.request_cancel(jid)
                return self._json(200 if ok else 409,
                                  {"ok": ok, **({"error": "job not cancellable"} if not ok else {})})
            if path.startswith("/api/jobs/") and path.endswith("/pause"):
                jid = path.split("/")[3]
                ok = jobs.request_pause(jid)
                return self._json(200 if ok else 409,
                                  {"ok": ok, **({"error": "job not running"} if not ok else {})})
            if path.startswith("/api/jobs/") and path.endswith("/resume"):
                jid = path.split("/")[3]
                ok = jobs.request_resume(jid)
                return self._json(200 if ok else 409,
                                  {"ok": ok, **({"error": "job not paused"} if not ok else {})})
            if path.startswith("/api/jobs/") and path.endswith("/mode"):
                jid = path.split("/")[3]
                res = jobs.request_mode(jid, str(body.get("mode", "")))
                if not res:
                    return self._json(409, {"error": "mode switch not possible"})
                return self._json(200, res)
            if path == "/api/jobs/flags/bulk":
                # P1-9: bulk is the SAME rule per id, never a laxer
                # path - the caller gets one honest result line per
                # requested id (partial success is visible, never
                # smoothed over).
                ids = body.get("ids")
                if not isinstance(ids, list) or not ids:
                    return self._json(400, {"error": "ids required"})
                if len(ids) > 100:
                    return self._json(400, {"error": "too many ids "
                                                     "(max 100)"})
                flags_body = {k: v for k, v in body.items()
                              if k in ("pinned", "locked", "trashed")}
                if not flags_body:
                    return self._json(400, {"error": "no flags given"})
                results = []
                for i_ in ids[:100]:
                    st_, pay = apply_job_flags(str(i_), flags_body)
                    results.append({"id": str(i_), "ok": st_ == 200,
                                    "status": st_,
                                    **({} if st_ == 200 else
                                       {"error": pay.get("error", "")})})
                return self._json(200, {"results": results})
            if path.startswith("/api/jobs/") and path.endswith("/flags"):
                jid = path.split("/")[3]
                st_, pay = apply_job_flags(jid, body)
                return self._json(st_, pay)
            if path.startswith("/api/jobs/") and path.endswith("/purge"):
                # P1-9: permanent RECORD deletion behind an explicit
                # confirmation (confirm must repeat the job id - a
                # bare {confirm:true} is refused). Locked and active
                # jobs are refused; FILES ARE NEVER TOUCHED, so a
                # shared artifact keeps its file and its other
                # references keep working no matter what is purged.
                jid = path.split("/")[3]
                j = store.job_get(jid)
                if not j:
                    return self._json(404, {"error": "no such job"})
                if int(j.get("locked") or 0):
                    return self._json(409, {"error": "locked task: "
                                                     "unlock first"})
                if j["state"] in ACTIVE_TASK_STATES:
                    return self._json(409, {"error": "active task: "
                                                     "cancel first"})
                if str(body.get("confirm") or "") != jid:
                    return self._json(400, {"error": "type the task id "
                                                     "to confirm; record "
                                                     "deletion only - "
                                                     "files are never "
                                                     "removed"})
                snap = store.job_purge(jid)
                if not snap:
                    return self._json(404, {"error": "no such job"})
                store.audit("user", "task.purged", jid,
                            {**snap, "files_touched": False})
                publish({"type": "job", "id": jid})
                return self._json(200, {"ok": True, "id": jid,
                                        "removed": snap["removed_rows"],
                                        "files_touched": False})
            if path.startswith("/api/jobs/") and path.endswith("/title"):
                # M9 §8: rename the DISPLAY title only. The job id,
                # sample_id, spec and audit trail are untouched - this
                # is presentation metadata, stored beside the record.
                jid = path.split("/")[3]
                title = str(body.get("title") or "").strip()
                if not title:
                    return self._json(400, {"error": "title required"})
                if not store.job_get(jid):
                    return self._json(404, {"error": "no such job"})
                store.job_set(jid, display_title=title[:200])
                store.audit("user", "task.title.set", jid,
                            {"title": title[:200]})
                publish({"type": "job", "id": jid})
                return self._json(200, {"ok": True, "id": jid,
                                        "display_title": title[:200]})
            if path.startswith("/api/approvals/"):
                aid = path.split("/")[3]
                # M6 PART C: consent cards may be answered four ways -
                # approve / reject / limit_scope / literature_only.
                # `decision` is the canonical field; the older
                # {approve: bool} body stays accepted for compatibility.
                decision = str(body.get("decision") or "").strip()
                legacy = body.get("approve")
                if decision in ("", "true", "false"):
                    decision = "approve" if legacy else "reject"
                if decision not in ("approve", "approve_scope", "reject",
                                    "limit_scope", "literature_only"):
                    return self._json(400, {
                        "error": "decision must be approve, approve_scope, "
                                 "reject, limit_scope or literature_only"})
                if decision == "limit_scope" and not str(
                        body.get("scope") or "").strip():
                    return self._json(400, {
                        "error": "limit_scope requires a 'scope' note "
                                 "saying what the user will allow"})
                if decision == "approve_scope":
                    cur = store.approval_get(aid)
                    if not cur or cur["state"] != "pending":
                        return self._json(409, {
                            "error": "approval not pending"})
                    if cur["action"] != "gate_op":
                        # formal-run / job cards are NEVER scope-
                        # approvable: each run stays an independent
                        # human decision (M11 P0-2 risk rule)
                        return self._json(400, {
                            "error": "scoped approval applies only to "
                                     "gate confirmations"})
                approve = decision in ("approve", "approve_scope")
                scope_note = str(body.get("scope") or "").strip()
                appr = store.approval_decide(aid, approve, "user")
                if not appr:
                    return self._json(409, {"error": "approval not pending"})
                store.audit("user", f"approval.{decision}", aid,
                            {"job": appr["job_id"],
                             "action": appr["action"],
                             "scope": scope_note[:300]})
                if appr["action"] == "gate_op":
                    # real-agent destructive-op confirmation card: write
                    # the single-use grant and wake the agent session
                    agent.on_gate_decision(appr, approve, decision,
                                           scope_note)
                else:
                    jobs.on_approval_decided(aid, approve, decision,
                                             scope_note)
                publish({"type": "approval", "id": aid})
                return self._json(200, {**appr, "decision": decision})
            if path.startswith("/api/scopegrants/"):
                # POST /api/scopegrants/<id>/revoke (M11 P0-2):
                # revocation is immediate - the file gate.py reads is
                # re-materialised without the revoked root set.
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[3] == "revoke":
                    g = store.scope_grant_revoke(parts[2])
                    if not g:
                        return self._json(404, {"error": "no live grant "
                                                         "with that id"})
                    agent.rewrite_scope_grants()
                    store.audit("user", "scope.revoked", parts[2],
                                {"roots": g["roots"]})
                    publish({"type": "approval", "id": parts[2]})
                    return self._json(200, {"ok": True, "id": parts[2]})
                return self._json(404, {"error": "unknown route"})
            if path == "/api/admin/archive":
                # reversible cleanup of developer/demo records (see store)
                res = store.archive_and_purge()
                publish({"type": "records_changed"})
                return self._json(200, res)
            if path == "/api/admin/restore":
                fp = Path(str(body.get("file", ""))).name
                p = DATA / fp
                if not fp.startswith("archive-") or not p.is_file():
                    return self._json(400, {"error": "no such archive file "
                                                     "in the data dir"})
                n = store.restore_archive(str(p))
                publish({"type": "records_changed"})
                return self._json(200, {"restored_rows": n})
            if path == "/api/settings":
                cur = store.setting("ui", {})
                cur.update({k: v for k, v in body.items()
                            if k in ("mode", "stages", "simulate_failure",
                                     "top_n",
                                     # M9 §3: the chosen UI language
                                     # persists server-side too, so a
                                     # new browser sees the same default
                                     "language",
                                     # P1-9: how long the trash view
                                     # says records are kept; removal
                                     # is ALWAYS a manual action
                                     "trash_retention_days")})
                store.setting_set("ui", cur)
                store.audit("user", "settings.update", "ui", cur)
                return self._json(200, cur)
            if path == "/api/research":
                # E3: the SAME pathway the agent uses for chat research
                # requests (submit_research) - no separate mock branch.
                scope = body.get("scope", "targeted")
                targets = body.get("targets") or []
                if body.get("target") and not targets:
                    targets = [body["target"]]
                if not isinstance(targets, list):
                    return self._json(400, {"error": "targets must be a list"})
                custom = body.get("custom") or []
                if isinstance(custom, str):
                    custom = [custom]
                engine = str(body.get("engine", "live"))
                if engine not in ("live", "example"):
                    return self._json(400, {"error": "engine must be "
                                                     "live|example"})
                try:
                    res = submit_research(
                        store, scope, targets=list(targets),
                        custom=list(custom),
                        conv_id=str(body.get("conv_id", "") or ""),
                        actor="user", engine=engine)
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                publish({"type": "research_changed"})
                return self._json(202, res)
            if path.startswith("/api/research/") and \
                    path.endswith("/title"):
                # M9 §8: display title for a research run (see the job
                # route above - same presentation-only contract).
                rid = path.split("/")[3]
                title = str(body.get("title") or "").strip()
                if not title:
                    return self._json(400, {"error": "title required"})
                if not store.research_get(rid):
                    return self._json(404, {"error": "no such report"})
                store.research_set_title(rid, title)
                store.audit("user", "task.title.set", rid,
                            {"title": title[:200]})
                publish({"type": "research_changed"})
                return self._json(200, {"ok": True, "id": rid,
                                        "display_title": title[:200]})
            if path.startswith("/api/research/") and \
                    path.endswith("/cancel"):
                rid = path.split("/")[3]
                r = store.research_get(rid)
                if not r:
                    return self._json(404, {"error": "no such report"})
                if r["status"] not in ("running", "queued"):
                    return self._json(409, {"error": f"research is "
                                                     f"{r['status']}"})
                store.research_cancel_request(rid)
                store.research_event_add(rid, "cancellation requested - the "
                                         "run stops after the current query")
                return self._json(200, {"ok": True, "status": "cancel "
                                        "requested (cooperative)"})
            if path.startswith("/api/research/") and \
                    path.endswith("/resume"):
                rid = path.split("/")[3]
                if resume_research(store, rid):
                    publish({"type": "research_changed"})
                    return self._json(202, {"ok": True, "status_url":
                                            f"/api/research/{rid}"})
                return self._json(409, {"error": "not resumable (still "
                                                 "running, completed, or "
                                                 "unknown id)"})
            # ---------- skills (PART C: advisory instructions only) ----
            if path == "/api/skills":
                # Milestone 7 §5: the author supplies name + instructions
                # only. ONE Save validates, records a version, and writes
                # the file so NEW chat sessions load it (title/content
                # accepted as compatibility aliases).
                r = skills_mod.save_and_apply(
                    store,
                    str(body.get("name", body.get("title", ""))),
                    str(body.get("instructions", body.get("content", ""))))
                if r.get("errors"):
                    return self._json(400, {"errors": r["errors"]})
                publish({"type": "skills_changed"})
                return self._json(201, r)
            if path.startswith("/api/skills/deploy"):
                res = skills_mod.deploy(store)
                publish({"type": "skills_changed"})
                return self._json(200, res)
            if path.startswith("/api/skills/"):
                parts = path.strip("/").split("/")
                slug = parts[2]
                act = parts[3] if len(parts) > 3 else ""
                s = store.skill_get(slug, by_slug=True)
                if not s:
                    return self._json(404, {"error": "no such skill"})
                if act == "update":
                    # same single-Save chain as creation; the slug is
                    # pinned to this skill so a renamed edit updates in
                    # place instead of creating a second skill
                    r = skills_mod.save_and_apply(
                        store,
                        str(body.get("name", body.get("title", ""))),
                        str(body.get("instructions",
                                     body.get("content", ""))),
                        slug=slug)
                    if r.get("errors"):
                        return self._json(400, {"errors": r["errors"]})
                    publish({"type": "skills_changed"})
                    return self._json(200, r)
                if act in ("enable", "disable"):
                    s2 = store.skill_set_enabled(s["id"], act == "enable")
                    # §5: the toggle is the apply switch - re-run the
                    # deploy for this skill so Enable writes the file and
                    # Disable removes it (a stale file would still load)
                    res = skills_mod.deploy(store, slug)
                    store.audit("user", f"skill.{act}", slug,
                                {"deploy": (res.get("results")
                                            or [{}])[0].get("state")})
                    publish({"type": "skills_changed"})
                    return self._json(200, {"skill": s2,
                                            "deploy": (res.get("results")
                                                       or [{}])[0]})
                if act == "restore":
                    ver = body.get("version")
                    try:
                        ver = int(ver)
                    except (TypeError, ValueError):
                        return self._json(400, {"error": "version (int) "
                                                         "required"})
                    content = store.skill_version_content(s["id"], ver)
                    if content is None:
                        return self._json(404, {"error": f"no stored "
                                                         f"version {ver}"})
                    s2 = store.skill_update(s["id"], content,
                                            f"restored from version {ver}")
                    res = skills_mod.deploy(store, slug)
                    store.audit("user", "skill.restore", slug,
                                {"from_version": ver,
                                 "deploy": (res.get("results")
                                            or [{}])[0].get("state")})
                    publish({"type": "skills_changed"})
                    return self._json(200, {"skill": s2,
                                            "deploy": (res.get("results")
                                                       or [{}])[0]})
                if act == "deploy":
                    # retained for scripted repair only - the normal UI
                    # applies through Save/Enable (§5), no Deploy button
                    res = skills_mod.deploy(store, slug)
                    publish({"type": "skills_changed"})
                    return self._json(200 if res.get("ok") else 404, res)
                return self._json(404, {"error": "not found", "path": path})
            if path.startswith("/api/decisions/"):
                d_id = path.strip("/").split("/")[2]
                res = jobs.resolve_decision(d_id, str(body.get("choice", "")),
                                            str(body.get("note", "")))
                if not res:
                    return self._json(409, {"error": "decision not resolvable"})
                return self._json(200, res)
        except Exception as e:  # prototype: surface errors honestly
            return self._json(500, {"error": str(e)})
        return self._json(404, {"error": "not found", "path": path})

    # ---------- SSE / static ----------
    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q: queue.Queue = queue.Queue(maxsize=200)
        with EVENT_LOCK:
            EVENT_QUEUES.append(q)
        try:
            self.wfile.write(b'data: {"type":"hello"}\n\n')
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(
                        f"data: {json.dumps(ev)}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with EVENT_LOCK:
                if q in EVENT_QUEUES:
                    EVENT_QUEUES.remove(q)

    def _static(self, name: str) -> None:
        f = (FRONTEND / name).resolve()
        if not str(f).startswith(str(FRONTEND.resolve())) or not f.is_file():
            return self._json(404, {"error": "not found"})
        ctype = {".html": "text/html", ".js": "text/javascript",
                 ".css": "text/css", ".svg": "image/svg+xml"}.get(f.suffix, "application/octet-stream")
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    recovered = store.recover_interrupted()
    if recovered:
        print(f"recovered {len(recovered)} interrupted job(s) -> failed (see UI)")
    n = store.msg_purge_legacy_notices()
    if n:
        print(f"purged {n} pre-redesign mock banner(s) from chat history")
    rs = sweep_interrupted_research(store)
    if rs:
        print(f"marked {rs} interrupted research run(s) incomplete "
              "(resume available - persisted queries kept)")
    seeded = skills_mod.ensure_seed_skills(store)
    if seeded:
        dep = skills_mod.deploy(store)
        print(f"seeded skills {seeded}; deploy states: "
              + ", ".join(f"{r.get('slug')}={r.get('state')}"
                          for r in dep.get("results", [])))
    if ensure_task_gateway(store):
        print("task-query gateway ready (read-only token + helper deployed "
              "into the agent workspace)")
    # M11 P0-3 ordering: FIRST try to re-attach streaming turns to
    # jobs the (separate, still-running) gateway is holding, THEN
    # sweep whatever could not be re-attached to an honest
    # interrupted state. The gateway outlives this process; a turn
    # mid-flight across a Web restart is recoverable, not lost.
    reattached = agent.reattach_turns()
    if reattached:
        print(f"re-attached {len(reattached)} streaming turn(s) to live "
              "gateway jobs (results will still arrive)")
    swept = store.msg_sweep_stale_streaming(skip_conv_ids=reattached)
    if swept:
        print(f"marked {swept} abandoned 'streaming' message(s) as "
              "interrupted")
    store.audit("server", "server.start", "-", {"host": HOST, "port": PORT})
    jobs.resume_queued()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"NeoAG web demo (MOCK) on http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.audit("server", "server.stop", "-")


if __name__ == "__main__":
    main()
