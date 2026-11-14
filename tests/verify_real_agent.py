#!/usr/bin/env python3
"""Real-mode E2E through the demo API: pure-chat delivery, multi-turn
same-session continuity, honest status. Prints PASS/FAIL lines only."""
import base64, json, time, urllib.request, urllib.error

AUTH = json.load(open("/data1/neoag/agents/neoag-web-demo/data/auth.json"))
B64 = base64.b64encode(f"{AUTH['user']}:{AUTH['password']}".encode()).decode()
BASE = "http://127.0.0.1:8712"

def call(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data,
                               method="POST" if body is not None else "GET")
    r.add_header("Authorization", "Basic " + B64)
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=30) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"), strict=False)

P = F = 0
def check(name, ok, extra=""):
    global P, F
    print(("PASS " if ok else "FAIL ") + name + (f"  [{extra}]" if extra and not ok else ""))
    if ok: P += 1
    else: F += 1

def wait_reply(after_idx, marker=None, limit=150):
    """Poll bootstrap until an agent msg with badges ['real agent'] (final)
    appears past after_idx, or timeout. Returns (msgs, final_text|None)."""
    t0 = time.time()
    while time.time() - t0 < limit:
        time.sleep(4)
        _, b = call("/api/bootstrap")
        msgs = b["messages"]
        for m in msgs[after_idx:]:
            if m["role"] == "agent" and \
               (m.get("meta") or {}).get("badges") == ["real agent"]:
                return msgs, m
    return call("/api/bootstrap")[1]["messages"], None

call("/api/agent/mode", {"mode": "real"})
_, b0 = call("/api/bootstrap")
n0 = len(b0["messages"])
st, r0 = call("/api/chat", {"text": "Reply with exactly E2E-STREAM-OK and nothing else."})
check("E2E-1 chat accepted in real mode", st == 200 and
      any("job" in str((m.get("meta") or {})) or "Real agent started" in m.get("text","")
          for m in r0.get("messages", [])[1:]), str(st))
msgs1, final1 = wait_reply(n0, limit=150)
check("E2E-2 real agent reply streamed to chat",
      final1 is not None and "E2E-STREAM-OK" in (final1.get("text") or ""),
      (final1 or {}).get("text", "")[:120])
n1 = len(msgs1)
st, _ = call("/api/chat", {"text": "Without using tools: what exact token did I ask you to reply with in my previous message? Answer with just that token."})
msgs2, final2 = wait_reply(n1, limit=150)
check("E2E-3 multi-turn same-session memory",
      final2 is not None and "E2E-STREAM-OK" in (final2.get("text") or ""),
      (final2 or {}).get("text", "")[:120])
_, cb = call("/api/codebuddy/status")
check("E2E-4 status honest via jobs transport",
      cb.get("connected") is True and cb.get("transport") == "jobs"
      and cb.get("chat_permission_mode") == "plan"
      and (cb.get("chat_job") or {}).get("id"), json.dumps(cb)[:200])
check("E2E-5 gateway version reported", bool(cb.get("gateway_version")),
      str(cb.get("gateway_version")))
call("/api/agent/mode", {"mode": "mock"})
print(f"\nRESULT: {P} passed, {F} failed")
raise SystemExit(0 if F == 0 else 1)
