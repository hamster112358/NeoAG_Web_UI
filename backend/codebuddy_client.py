"""Real-agent integration: client for the CodeBuddy gateway JOBS API
(127.0.0.1:7890), verified live 2026-11-14 against gateway v2.180.0.

Why the Jobs API and not the Runs API
-------------------------------------
The earlier integration used the runs API
(POST /api/v1/runs + GET /api/v1/runs/<id>/stream). Verified limitation:
a run still in the registry holds its SSE stream open WITHOUT response
headers until an event arrives, and once a run leaves the registry its
stream answers 404 with NO replay - so transcript delivery could not be
guaranteed end-to-end. The jobs API has no such gap:

  POST /api/v1/jobs                 dispatch agent instance -> {id,...}
  GET  /api/v1/jobs/<id>            detail under data.job:
                                    state working|blocked|done|failed|
                                    stopped, status busy|waiting|idle,
                                    tempo, settled, alive, output.result,
                                    detail (live progress line)
  GET  /api/v1/jobs/<id>/transcript one-shot ACP replay updates (last
                                    ~1000 lines), read-only, no worker
                                    needed - the reliable content channel
  GET  /api/v1/jobs/<id>/stream     SSE: replays the transcript tail
                                    immediately, then tails live events
                                    (verified: instant 200 + headers)
  POST /api/v1/jobs/<id>/reply      continue the SAME conversation, even
                                    after settle (multi-turn) - verified:
                                    {delivered:true}, result updates
  POST /api/v1/jobs/<id>/stop       stop worker
  DELETE /api/v1/jobs/<id>          remove record
  GET  /api/v1/info                 gateway version/cwd probe (status)

Approval semantics (measured, not assumed)
------------------------------------------
- permissionMode "default" (gateway process default): dispatched jobs
  execute tools autonomously (tool_call pending -> completed with real
  output; no interactive approval exists for headless job workers).
- permissionMode "plan": execution tools are DENIED with a verbatim
  error - "Permission to use Bash has been denied because this tool
  requires approval but permission prompts are not available" - while
  read/inspect behaviour still works. THIS demo therefore dispatches
  its chat agent in plan mode: the real agent answers and proposes, but
  this interface can never execute commands or edit files. Execution,
  if ever wired, must go through the demo's own approval queue.

Gateway quirks (verified):
- JSON responses can embed raw newlines in free-text fields (detail):
  parse with json.loads(..., strict=False).
- Rapid dispatch can return 429: bounded backoff below.
- GET /api/v1/jobs/<name> is NOT supported - only ids (stable, short,
  or sessionId).

The gateway password is read from ~/.codebuddy/settings.json
(gateway.password) at client construction and is NEVER logged or echoed.
"""
from __future__ import annotations

import http.cookiejar
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SETTINGS = Path.home() / ".codebuddy" / "settings.json"
BASE = "http://127.0.0.1:7890"
HDR = {"x-codebuddy-request": "1", "Content-Type": "application/json"}


class CodeBuddyError(RuntimeError):
    pass


def _loads(text: str):
    """Gateway free-text fields can contain raw control characters."""
    return json.loads(text, strict=False)


class CodeBuddyClient:
    """One client = one authenticated gateway session."""

    def __init__(self):
        self._lock = threading.Lock()
        pw = self._read_password()
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        code, body = self._req("POST", "/api/v1/auth/login",
                               {"password": pw})
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
    def info(self) -> dict:
        """Gateway identity probe (version, cwd, uptime) - no job created."""
        code, body = self._req("GET", "/api/v1/info")
        if code != 200:
            raise CodeBuddyError(f"gateway info probe failed (HTTP {code})")
        return _loads(body).get("data", {})

    def dispatch(self, prompt: str, cwd: str, name: str,
                 permission_mode: str | None = None,
                 tries: int = 4, backoff: int = 15) -> dict:
        """Dispatch a job instance. Returns the {id, sessionId, ...} dict."""
        payload = {"prompt": prompt[:16000], "cwd": cwd, "name": name}
        if permission_mode:
            payload["permissionMode"] = permission_mode
        for i in range(tries):
            code, body = self._req("POST", "/api/v1/jobs", payload)
            if code in (200, 201, 202):
                return _loads(body).get("data", {})
            if code == 429 and i < tries - 1:
                time.sleep(backoff)
                continue
            raise CodeBuddyError(
                f"job dispatch failed (HTTP {code}: {body[:120]})")
        raise CodeBuddyError("job dispatch failed (retries exhausted)")

    def job(self, job_id: str) -> dict:
        """Detail dict (state/status/tempo/settled/alive/output/detail)."""
        code, body = self._req("GET", f"/api/v1/jobs/{job_id}")
        if code != 200:
            return {"unknown": True, "http": code}
        return _loads(body).get("data", {}).get("job", {})

    def transcript(self, job_id: str) -> list[dict]:
        """ACP replay updates (append-only window, last ~1000 lines)."""
        code, body = self._req("GET", f"/api/v1/jobs/{job_id}/transcript")
        if code != 200:
            return []
        return _loads(body).get("data", {}).get("updates", []) or []

    def reply(self, job_id: str, text: str) -> dict:
        """Continue the same conversation (works after settle while the
        worker is alive). Returns {delivered, notice} or {error, http}."""
        code, body = self._req("POST", f"/api/v1/jobs/{job_id}/reply",
                               {"text": text[:16000], "bash": False})
        if code == 200:
            return _loads(body).get("data", {})
        return {"error": body[:160], "http": code}

    def stop(self, job_id: str) -> dict:
        code, body = self._req("POST", f"/api/v1/jobs/{job_id}/stop")
        return {"http": code, **(_loads(body).get("data", {})
                                 if code == 200 else {"raw": body[:120]})}

    def delete(self, job_id: str) -> dict:
        code, body = self._req("DELETE", f"/api/v1/jobs/{job_id}")
        return {"http": code, **(_loads(body).get("data", {})
                                 if code == 200 else {"raw": body[:120]})}
