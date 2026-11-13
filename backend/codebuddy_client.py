"""Real-agent integration: minimal client for the CodeBuddy Web UI gateway
(127.0.0.1:7890), verified live 2026-11-13:

  POST /api/v1/auth/login          {"password": ...} -> session cookie+token
  POST /api/v1/runs                Gateway Protocol message:
                                   {"id","type","payload":{"text"},"sender"}
                                   + header x-codebuddy-request  -> 202 {runId}
  GET  /api/v1/runs/<runId>        {"runId","active"}
  POST /api/v1/runs/<runId>/cancel -> {"status":"cancelled"}
  GET  /api/v1/runs/<runId>/stream  SSE (text/event-stream)

Notes / honesty:
 - The access password is read from ~/.codebuddy/settings.json
   (gateway.password) at client construction and is NEVER logged or echoed.
 - The gateway rate-limits run creation (HTTP 429) - the client backs off
   with a bounded retry and refuses to start a second run while one is
   active (observed server-side concurrency cap of 1).
 - Tool approvals (permission-mode default) are NOT relayable through the
   runs API today: any server-side tool action that needs approval must be
   approved in the operator's own CodeBuddy Web UI. Until an approval-relay
   endpoint exists, real-agent prompts are restricted to READ-ONLY
   inspection tasks, and this restriction is stated in the UI.
 - Conversation context across runs is NOT guaranteed by the gateway
   (each message parses as its own conversation), so the client sends a
   rolling context snippet with every run instead of assuming memory.
"""
from __future__ import annotations

import http.cookiejar
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SETTINGS = Path.home() / ".codebuddy" / "settings.json"
BASE = "http://127.0.0.1:7890"
HDR = {"x-codebuddy-request": "1", "Content-Type": "application/json"}


class CodeBuddyError(RuntimeError):
    pass


class CodeBuddyClient:
    """One client = one authenticated session; runs are serialized."""

    def __init__(self, sender: str = "neoag-web-demo"):
        self.sender_id = sender
        self._lock = threading.Lock()
        self.active_run: str | None = None
        pw = self._read_password()
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        code, body = self._req("POST", "/api/v1/auth/login", {"password": pw})
        if code != 200:
            raise CodeBuddyError(f"CodeBuddy login failed (HTTP {code})")

    @staticmethod
    def _read_password() -> str:
        try:
            data = json.loads(SETTINGS.read_text())
            pw = data["gateway"]["password"]
            if not pw:
                raise KeyError("empty")
            return pw
        except Exception as e:  # never include file contents in the error
            raise CodeBuddyError(
                "cannot read CodeBuddy gateway password from "
                f"{SETTINGS} ({type(e).__name__})") from None

    def _req(self, method: str, path: str, body: dict | None = None,
             timeout: int = 25):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(BASE + path, data=data, method=method)
        for k, v in HDR.items():
            r.add_header(k, v)
        try:
            with self.op.open(r, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            return -1, str(e)

    # ------------------------------------------------------------------
    def create_run(self, text: str, context: list[str] | None = None,
                   tries: int = 4, backoff: int = 20) -> str:
        with self._lock:
            if self.active_run:
                raise CodeBuddyError(
                    "a real-agent run is already active - cancel it first "
                    "(one concurrent run is allowed by the gateway)")
            # rolling context: the gateway treats each message as its own
            # conversation, so recent turns ride along in the prompt.
            prompt = text
            if context:
                tail = "\n".join(f"- {c}" for c in context[-6:])
                prompt = f"Recent conversation summary:\n{tail}\n\nUser: {text}"
            payload = {"id": str(uuid.uuid4()), "type": "message",
                       "payload": {"text": prompt},
                       "sender": {"id": self.sender_id, "name": "NeoAG Web UI"}}
            for i in range(tries):
                code, body = self._req("POST", "/api/v1/runs", payload)
                if code == 202:
                    run_id = json.loads(body)["data"]["runId"]
                    self.active_run = run_id
                    return run_id
                if code == 429 and i < tries - 1:
                    time.sleep(backoff)
                    continue
                raise CodeBuddyError(f"run creation failed (HTTP {code})")
        raise CodeBuddyError("run creation failed (retries exhausted)")

    def status(self, run_id: str) -> dict:
        code, body = self._req("GET", f"/api/v1/runs/{run_id}")
        if code != 200:
            return {"runId": run_id, "active": False, "unknown": True}
        return json.loads(body)["data"]

    def cancel(self, run_id: str) -> dict:
        code, body = self._req("POST", f"/api/v1/runs/{run_id}/cancel")
        if code == 200:
            return json.loads(body)["data"]
        return {"error": body[:120], "http": code}

    def stream(self, run_id: str, max_seconds: int = 900, max_events: int = 400,
               probe_seconds: int = 20):
        """Yield parsed SSE events; ends on completion or limits.

        Gateway behaviour verified live 2026-11-13:
        - a run still in the registry holds the stream open WITHOUT
          response headers until an event arrives (so a plain long read
          can block for the whole window), while
        - a run that left the registry (completed/dropped) answers the
          stream with an immediate 404 - no replay.
        Therefore: short probe timeouts + status re-polls between
        attempts, a distinct end signal on stream-404, and an explicit
        idle-timeout signal - never an indefinite block."""
        url = f"{BASE}/api/v1/runs/{run_id}/stream"
        # The gateway rejects gateway-protocol requests that lack the
        # x-codebuddy-request header (same requirement as every _req call),
        # so the stream request must carry it too - a cookie alone is not
        # enough (observed live 2026-11-13: without the header -> HTTP 403).
        hdrs = {"Accept": "text/event-stream",
                "x-codebuddy-request": "1",
                "Cache-Control": "no-cache"}
        t0 = time.time()
        n = 0
        try:
            while time.time() - t0 < max_seconds:
                st = self.status(run_id)
                if st.get("unknown"):
                    yield {"run_left_registry": True,
                           "detail": "the status endpoint no longer knows "
                                     "this run (completed/dropped); the "
                                     "gateway does not replay transcripts"}
                    return
                if not st.get("active", True):
                    return  # run known but inactive: end quietly; the
                    # relay's generic fallback covers this accurately
                try:
                    with self.op.open(urllib.request.Request(
                            url, headers=dict(hdrs)),
                            timeout=probe_seconds) as resp:
                        for raw in resp:
                            line = raw.decode("utf-8", "replace").strip()
                            if not line:
                                continue
                            if line.startswith("data:"):
                                n += 1
                                try:
                                    yield json.loads(line[5:].strip())
                                except Exception:
                                    yield {"raw": line[5:200]}
                            if n >= max_events or \
                                    time.time() - t0 > max_seconds:
                                return
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        yield {"run_left_registry": True,
                               "detail": "stream answered 404 - the run is "
                                         "no longer active and the gateway "
                                         "does not replay transcripts"}
                        return
                    yield {"stream_error":
                           f"HTTP Error {e.code}: {e.reason}"[:160]}
                    return
                except Exception:
                    pass  # open/read timeout -> re-poll status, retry
            yield {"stream_idle_timeout": max_seconds}
        finally:
            with self._lock:
                if self.active_run == run_id:
                    self.active_run = None
