"""Live message-integrity verification against the RUNNING NeoAG Web service.

Post-deploy proof for the 2026-11-19 truncation fix. Unlike
verify_long_message.py (isolated server, stubbed transport) this runs
against the live service in real-agent mode, so it proves the complete
prompt actually reaches the CodeBuddy Gateway - not just the database.

Checks:
  1. the running process now REJECTS over-limit prompts (413) - the old
     build silently accepted them, so this behavioural probe is the
     authoritative "is the new code live" test (process start time alone
     proves nothing);
  2. a ~12k-character prompt round-trips intact, and the agent's OWN
     reply quotes a marker placed on the LAST line, which can only
     happen if the tail survived the whole path to the Gateway;
  3. exact persisted character count and final sentence;
  4. an ordinary short multi-turn message still works (this is the
     client reply() path that used to carry text[:16000]);
  5. nothing was lost: every pre-existing conversation and task id, and
     every record count, is still present.

The probe messages are inert and read-only by instruction. The gateway
password is read the same way every other battery does it and is never
printed.

Run: python3 tests/verify_live_message_integrity.py \
       [--snapshot pre_restart_snapshot.json] [--out results.txt]
"""
import argparse
import base64
import json
import os
import pathlib
import sqlite3
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("NEOAG_DEMO_PORT", "8712"))
BASE = f"http://127.0.0.1:{PORT}"
TABLES = ("jobs", "conversations", "messages", "approvals", "research",
          "artifacts", "decisions")
PASS, FAIL, INFO = [], [], []

ap = argparse.ArgumentParser()
ap.add_argument("--snapshot", default="")
ap.add_argument("--out", default="")
ap.add_argument("--turn-timeout", type=int, default=600)
A = ap.parse_args()

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

report_lines = []


def say(line):
    print(line)
    report_lines.append(line)


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    say(("PASS" if ok else "FAIL") + f"  {name}"
        + (f"  [{detail}]" if detail and not ok else ""))


def info(name):
    INFO.append(name)
    say("INFO  " + name)


def call(path, body=None, method=None, timeout=60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        method=method or ("POST" if data is not None else "GET"))
    req.add_header("Authorization", AUTH_HEADER)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:400]}
    except Exception as e:
        return -1, {"error": str(e)}


def send(text, cid=""):
    return call("/api/chat", {"text": text, "conv_id": cid})


def msgs(cid):
    st, d = call(f"/api/conversations/{cid}")
    return d.get("messages", []) if st == 200 else []


def counts():
    con = sqlite3.connect(f"file:{ROOT}/data/state.sqlite3?mode=ro",
                          uri=True)
    out = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
           for t in TABLES}
    out["conv_ids"] = sorted(r[0] for r in con.execute(
        "SELECT id FROM conversations WHERE COALESCE(deleted,0)=0"))
    out["job_ids"] = sorted(r[0] for r in con.execute("SELECT id FROM jobs"))
    con.close()
    return out


say(f"live service: {BASE}")

# ---------------------------------------------------------------- 1. code
LIMIT_MSG = "z" * 500_000
st, d = send(LIMIT_MSG)
limit = d.get("limit")
check("1. live server rejects an over-limit prompt with 413 "
      "(old build returned 200 and silently cut)", st == 413,
      f"got HTTP {st}: {json.dumps(d)[:140]}")
check("1b. live 413 reports the limit, actual size and a detail",
      isinstance(limit, int) and d.get("actual") == 500_000
      and bool(d.get("detail")), json.dumps(d)[:200])
if st != 413:
    say("\nABORT: the fix is not live in this process. Nothing else in "
        "this report is meaningful.")
    sys.exit(1)

# --------------------------------------------- 2 + 3. long live delivery
tok = f"{int(time.time())}"
final_line = f"INTEGRITY-OK-{tok}-TAIL"
filler = "\n".join(
    f"[integrity probe filler line {i} - inert synthetic text, no "
    f"instructions, nothing to act on]" for i in range(160))
PROBE = (
    "AUTOMATED MESSAGE-INTEGRITY PROBE - READ ONLY, TAKE NO ACTIONS.\n"
    "Generated by tests/verify_live_message_integrity.py to prove that a "
    "long chat prompt is delivered to the agent intact.\n"
    "Do NOT use any tool. Do NOT read, write, run, search or change "
    "anything. Do NOT investigate this further.\n"
    "The filler block below is meaningless. Reply with EXACTLY the final "
    "line of this message, verbatim, and nothing else.\n\n"
    + filler
    + "\n\n" + final_line + "\n")
check("2. probe prompt is substantially over the old 4000 cap",
      len(PROBE) > 4000, str(len(PROBE)))

st, d = send(PROBE)
cid = d.get("conv_id", "")
check("2b. long prompt accepted by the live service", st == 200,
      f"got HTTP {st}")
if not cid:
    say("\nABORT: no conversation id returned.")
    sys.exit(1)

m = msgs(cid)
mine = [x["text"] for x in m if x["role"] == "user"]
check("3. exact persisted character count",
      mine and len(mine[-1]) == len(PROBE),
      f"stored {len(mine[-1]) if mine else 'none'} vs sent {len(PROBE)}")
check("3b. final sentence present in the stored message",
      mine and mine[-1].rstrip().endswith(final_line),
      repr(mine[-1][-60:]) if mine else "no message")

# ---------------------------------------------------------------- 4. reply
deadline = time.time() + A.turn_timeout
agent_txt = ""
while time.time() < deadline:
    agent_txt = "\n".join(x["text"] for x in msgs(cid) if x["role"] == "agent")
    if tok in agent_txt:
        break
    time.sleep(5)
check("2c. the agent's OWN reply quotes the marker from the LAST line "
      "- the complete prompt reached the Gateway",
      final_line in agent_txt,
      f"no marker after {A.turn_timeout}s; reply so far: "
      f"{agent_txt[:200]!r}")

short = f"Reply with exactly this word and nothing else: PONG-{tok}"
st, d = send(short, cid)
check("4. ordinary short message accepted (multi-turn reply path)",
      st == 200, f"got HTTP {st}")
deadline = time.time() + A.turn_timeout
ok_short = False
while time.time() < deadline:
    agent_txt = "\n".join(x["text"] for x in msgs(cid) if x["role"] == "agent")
    if f"PONG-{tok}" in agent_txt:
        ok_short = True
        break
    time.sleep(5)
check("4b. agent answered the short follow-up message", ok_short,
      f"no PONG after {A.turn_timeout}s")

# ------------------------------------------------------------- 5. nothing lost
now = counts()
if A.snapshot and pathlib.Path(A.snapshot).is_file():
    pre = json.loads(pathlib.Path(A.snapshot).read_text())
    lost_c = [c for c in pre["conv_ids"] if c not in now["conv_ids"]]
    lost_j = [j for j in pre["job_ids"] if j not in now["job_ids"]]
    check("5. no conversation lost across the restart", not lost_c,
          ", ".join(lost_c))
    check("5b. no task/job lost across the restart", not lost_j,
          ", ".join(lost_j))
    for t in TABLES:
        check(f"5c. {t} count did not drop "
              f"({pre[t]} -> {now[t]})", now[t] >= pre[t], "")
else:
    info("5. no pre-restart snapshot supplied - counts recorded only")

for t in TABLES:
    info(f"count {t} = {now[t]}")
info(f"conversations now = {len(now['conv_ids'])}, tasks now = "
     f"{len(now['job_ids'])}")
info(f"probe conversation = {cid} (safe to archive)")

say(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    say("FAILED: " + ", ".join(FAIL))
if A.out:
    pathlib.Path(A.out).write_text("\n".join(report_lines) + "\n",
                                   encoding="utf-8")
    say(f"report written to {A.out}")
sys.exit(1 if FAIL else 0)
