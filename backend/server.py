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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import skills as skills_mod
from .agent import DemoAgent, ensure_task_gateway
from .help_docs import DOCS as HELP_DOCS, get as help_get
from .inventory import load_inventory, targets_for_ui
from .jobs import JobManager, DECISION_POINTS as RUN_DECISION_POINTS
from .mock_data import SAMPLES, STAGES, DEMO_DATASET_VERSION
from .neoag_params import (PARAM_GROUPS, STAGE_GRAPH, DECISION_POINTS,
                           HUMAN_REQUIRED)
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


def job_public(job: dict) -> dict:
    j = {k: job[k] for k in ("id", "created_at", "updated_at", "started_at",
                             "finished_at", "state", "kind", "sample_id",
                             "stage", "progress", "error", "approval_id",
                             "conv_id", "outcome")}
    j["spec"] = json.loads(job.get("spec_json") or "{}")
    return j




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
    evs = store.event_list(job["id"], 0, 1000)
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
    TASK_QUERY_PREFIXES = ("/api/tasks", "/api/results/", "/api/research")

    def _task_token_ok(self) -> bool:
        tok = store.setting("task_token", "")
        given = self.headers.get("X-NeoAG-Task-Token", "")
        return bool(tok) and bool(given) and hmac.compare_digest(given, tok)

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
                "jobs": [job_public(j) for j in store.job_list(50)],
                "approvals": store.approval_list(),
                "decisions": store.decision_list(),
                "research": store.research_list(),
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
            return self._json(200, {"groups": PARAM_GROUPS,
                                   "stage_graph": STAGE_GRAPH,
                                   "decision_points": DECISION_POINTS,
                                   "human_required": HUMAN_REQUIRED})
        if path == "/api/audit":
            return self._json(200, store.audit_list(300))
        if path == "/api/conversations":
            return self._json(200, {"conversations": store.conv_list()})
        if path.startswith("/api/conversations/"):
            cid = path.strip("/").split("/")[2]
            conv = store.conv_get(cid)
            if not conv or conv.get("deleted"):
                return self._json(404, {"error": "no such conversation"})
            return self._json(200, {"conversation": conv,
                                    "messages": store.msg_list(400, cid)})
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
            rows = store.job_list(
                lim, conv_id=cid,
                kinds=[kinds] if kinds else None)
            return self._json(200, {
                "tasks": [{"id": j["id"], "kind": j["kind"],
                           "state": j["state"], "outcome": j["outcome"],
                           "sample_id": j["sample_id"],
                           "created_at": j["created_at"],
                           "finished_at": j["finished_at"],
                           "conv_id": j["conv_id"],
                           "detail_url": f"/api/tasks/{j['id']}"}
                          for j in rows]})
        if path == "/api/tasks/active":
            # B2: which run belongs to THIS conversation? Never guess the
            # most-recent job: no conv link -> empty list, not a fallback.
            cid = (q.get("conv_id") or [""])[0]
            rows = [j for j in store.job_list(200, conv_id=cid or None)
                    if j["state"] in ("queued", "running", "paused",
                                      "blocked")]
            return self._json(200, {"conv_id": cid,
                                    "tasks": [job_public(j)
                                              for j in rows]})
        if path.startswith("/api/tasks/"):
            parts = path.strip("/").split("/")  # api/tasks/<id>[/snapshot]
            jid = parts[2]
            job = store.job_get(jid)
            if not job:
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
            return self._json(200, {"research": store.research_list()})
        if path.startswith("/api/research/"):
            parts = path.strip("/").split("/")
            rid = parts[2]
            if len(parts) > 3 and parts[3] == "events":
                after = int((q.get("after") or ["0"])[0])
                return self._json(200, {"events": store.research_events(
                    rid, after)})
            r = store.research_get(rid)
            if not r:
                return self._json(404, {"error": "no such report"})
            return self._json(200, r)
        if path.startswith("/api/jobs/"):
            jid = path.strip("/").split("/")[2]  # api/jobs/<id>[/events]
            after = int((q.get("after") or ["0"])[0])
            return self._json(200, store.event_list(jid, after, 1000))
        if path.startswith("/api/results/"):
            # A3: Results are read from the ARTIFACT MANIFEST - the live
            # artifact rows are authoritative, never a stored claim.
            jid = path.split("/")[-1]
            job = store.job_get(jid)
            if not job:
                return self._json(404, {"error": "no such job"})
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
        if not self._authorized():
            return self._unauthorized()
        path = urlparse(self.path).path.rstrip("/")
        try:
            body = self._body()
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        try:
            if path == "/api/chat":
                text = str(body.get("text", ""))[:4000]
                cid = str(body.get("conv_id", "") or "")
                conv = store.conv_get(cid) if cid else None
                if not conv or conv.get("deleted"):
                    conv = store.conv_create("")  # first message starts a chat
                    cid = conv["id"]
                if not (conv.get("title") or "").strip():
                    store.conv_set_title(cid, agent.title_from(text))
                store.audit("user", "chat.send", cid, {"text": text[:120]})
                msgs = [store.msg_add("user", text, {}, cid)]
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
                stage_ids = body.get("stage_ids") or [
                    s["id"] for s in STAGES if s["required"]]
                kind = str(body.get("kind", "analysis"))
                if kind not in ("analysis", "discovery", "diagnostic"):
                    return self._json(400, {"error": "invalid kind"})
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
                                              or ""))
                return self._json(202, res)
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
            if path.startswith("/api/approvals/"):
                aid = path.split("/")[3]
                approve = bool(body.get("approve"))
                appr = store.approval_decide(aid, approve, "user")
                if not appr:
                    return self._json(409, {"error": "approval not pending"})
                store.audit("user", f"approval.{appr['state']}", aid,
                            {"job": appr["job_id"],
                             "action": appr["action"]})
                if appr["action"] == "gate_op":
                    # real-agent destructive-op confirmation card: write
                    # the single-use grant and wake the agent session
                    agent.on_gate_decision(appr, approve)
                else:
                    jobs.on_approval_decided(aid, approve)
                publish({"type": "approval", "id": aid})
                return self._json(200, appr)
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
                            if k in ("mode", "stages", "simulate_failure", "top_n")})
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
                slug = str(body.get("slug", "")).strip()
                errs = skills_mod.validate_skill(
                    slug, str(body.get("content", "")),
                    str(body.get("title", "")))
                if errs:
                    return self._json(400, {"errors": errs})
                cat = str(body.get("category", ""))
                if cat not in skills_mod.CATEGORIES:
                    return self._json(400, {"error": "category must be one "
                                       "of " + ", ".join(
                                           skills_mod.CATEGORIES)})
                if store.skill_get(slug, by_slug=True):
                    return self._json(409, {"error": "slug already exists"})
                s = store.skill_create(slug, slug,
                                       str(body.get("title", slug)), cat,
                                       str(body.get("content", "")))
                store.audit("user", "skill.create", slug, {"category": cat})
                publish({"type": "skills_changed"})
                return self._json(201, s)
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
                    content = str(body.get("content", ""))
                    errs = skills_mod.validate_skill(slug, content,
                                                     str(body.get("title",
                                                                 "")))
                    if errs:
                        return self._json(400, {"errors": errs})
                    cat = body.get("category")
                    if cat is not None and cat not in \
                            skills_mod.CATEGORIES:
                        return self._json(400, {"error": "bad category"})
                    s2 = store.skill_update(
                        s["id"], content, "edited in Skills tab",
                        title=str(body.get("title", "")) or None,
                        category=cat)
                    store.audit("user", "skill.update", slug,
                                {"version": s2["version"]})
                    publish({"type": "skills_changed"})
                    return self._json(200, s2)
                if act in ("enable", "disable"):
                    s2 = store.skill_set_enabled(s["id"], act == "enable")
                    store.audit("user", f"skill.{act}", slug, {})
                    publish({"type": "skills_changed"})
                    return self._json(200, s2)
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
                    store.audit("user", "skill.restore", slug,
                                {"from_version": ver})
                    publish({"type": "skills_changed"})
                    return self._json(200, s2)
                if act == "deploy":
                    res = skills_mod.deploy(store, slug)
                    publish({"type": "skills_changed"})
                    return self._json(200 if res.get("ok") else 404, res)
                if act == "test":
                    # C1 "test": static validation + rendered check, honest
                    # about what it does NOT check (no agent session run)
                    errs = skills_mod.validate_skill(
                        slug, s["content"], s["title"])
                    return self._json(200, {
                        "slug": slug, "valid": not errs,
                        "errors": errs,
                        "render_check": skills_mod.render_check(
                            s["content"])[:12],
                        "note": ("structural validation only: frontmatter, "
                                 "slug, size. It does NOT run an agent "
                                 "session; behaviour check = start a new "
                                 "chat after deploy and ask the agent."),
                    })
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
    swept = store.msg_sweep_stale_streaming()
    if swept:
        print(f"marked {swept} abandoned 'streaming' message(s) as interrupted")
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
