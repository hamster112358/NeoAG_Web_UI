#!/usr/bin/env python3
"""PostToolUse provenance hook: appends one compact audit line per
executed tool call to <workspace>/audit/exec.jsonl. Read-only w.r.t.
everything else; never blocks. Secrets are never written (path/command
kept only; that is what the operator reviews)."""
import json
import os
import time

WS = "/data1/neoag/agents/neoag_agent_ws"
LOG = os.path.join(WS, "audit", "exec.jsonl")

try:
    ev = json.load(__import__("sys").stdin)
except Exception:
    raise SystemExit(0)

ti = ev.get("tool_input") or {}
brief = ""
if isinstance(ti, dict):
    brief = (str(ti.get("command") or ti.get("file_path")
               or ti.get("pattern") or ti.get("path") or ""))[:400]
resp = ev.get("tool_response")
status = "unknown"
if isinstance(resp, dict):
    if "isError" in resp:
        status = "error" if resp.get("isError") else "ok"
    elif "success" in resp:
        status = "ok" if resp.get("success") else "error"
os.makedirs(os.path.dirname(LOG), exist_ok=True)
rec = {"ts": round(time.time(), 2),
       "session": str(ev.get("session_id") or "")[:8],
       "tool": str(ev.get("tool_name") or ""),
       "status": status,
       "brief": brief}
with open(LOG, "a") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
