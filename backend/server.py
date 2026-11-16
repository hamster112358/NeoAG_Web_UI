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

from .agent import DemoAgent
from .help_docs import DOCS as HELP_DOCS, get as help_get
from .jobs import JobManager
from .mock_data import SAMPLES, STAGES, DEMO_DATASET_VERSION
from .neoag_params import (PARAM_GROUPS, STAGE_GRAPH, DECISION_POINTS,
                           HUMAN_REQUIRED)
from .policies import POLICIES
from .research import TARGETS, run_research
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
                             "finished_at", "state", "kind", "sample_id", "stage",
                             "progress", "error", "approval_id")}
    j["spec"] = json.loads(job.get("spec_json") or "{}")
    return j


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

    # ---------- GET ----------
    def do_GET(self) -> None:
        if not self._authorized():
            return self._unauthorized()
        u = urlparse(self.path)
        path, q = u.path.rstrip("/") or "/", parse_qs(u.query)
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
                "decision_points": DECISION_POINTS,
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
        if path.startswith("/api/research/"):
            rid = path.strip("/").split("/")[2]
            r = store.research_get(rid)
            if not r:
                return self._json(404, {"error": "no such report"})
            return self._json(200, r)
        if path.startswith("/api/jobs/"):
            jid = path.strip("/").split("/")[2]  # api/jobs/<id>[/events]
            after = int((q.get("after") or ["0"])[0])
            return self._json(200, store.event_list(jid, after, 1000))
        if path.startswith("/api/results/"):
            jid = path.split("/")[-1]
            job = store.job_get(jid)
            if not job:
                return self._json(404, {"error": "no such job"})
            if job["state"] != "completed":
                return self._json(409, {"error": f"job is {job['state']}"})
            return self._json(200, json.loads(job["result_json"] or "{}"))
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
                                  requested_config=rcfg)
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
                scope = body.get("scope", "full")
                target = body.get("target", "presentation")
                rid = new_id("res")
                store.research_add(rid, scope, target)
                threading.Thread(target=run_research,
                                 args=(rid, scope, target, store, publish),
                                 daemon=True).start()
                return self._json(202, {"research_id": rid, "status_url":
                                       f"/api/research/{rid}"})
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
