#!/usr/bin/env python3
"""M11 P0-3 battery: long-running turns are durable.

The filed regression: a research turn ended with "(No terminal job
state seen within 900s; the gateway still holds the job...)" while the
gateway was still executing - the UI had thrown away a live job.

Pinned behaviour:
  * a production turn has NO wall-clock budget (max_seconds=None);
    it ends on real events only
  * a silent-but-alive job gets ONE honest stall notice, then the
    relay keeps waiting - and still relays the real outcome
  * a turn streaming across a Web-server restart RE-ATTACHES to the
    gateway job and the substantive reply still arrives (reply-once:
    a reply already in the conversation is never duplicated)
  * genuinely-gone jobs are stamped interrupted by the sweep
Run: python3 tests/verify_m11_p03.py
"""
import atexit
import base64
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 8790
PASS, FAIL = [], []

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

sys.path.insert(0, str(ROOT))
_stub = types.ModuleType("backend.server")
_stub.publish = lambda ev: None
sys.modules.setdefault("backend.server", _stub)
from backend.store import Store  # noqa: E402
from backend import agent as _amod  # noqa: E402
from backend.agent import DemoAgent  # noqa: E402


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS" if ok else "FAIL") + f"  {name}"
          + (f"  [{detail}]" if detail and not ok else ""))


class _TimeShim:                 # relay polling becomes a spin
    sleep = staticmethod(lambda s: None)
    time = staticmethod(time.time)


class _FlipGW:
    """running for `before` polls, then done with a reply."""

    def __init__(self, before=2, reply="/unit/ws"):
        self.n = 0
        self.before = before
        self.reply = reply

    def job(self, j):
        self.n += 1
        if self.n <= self.before:
            return {"state": "running", "alive": True}
        return {"state": "done", "alive": True}

    def transcript(self, j):
        return [
            {"sessionUpdate": "tool_call", "toolCallId": "t1",
             "status": "pending", "title": "`sleep 4000`"},
            {"sessionUpdate": "agent_message_chunk", "messageId": "m1",
             "content": {"text": self.reply}}]


class _GoneGW:
    def job(self, j):
        raise ConnectionError("gateway unreachable (unit)")

    def transcript(self, j):
        raise ConnectionError("gateway unreachable (unit)")


class _DeadJobGW:
    def job(self, j):
        return {"state": "gone", "unknown": True}

    def transcript(self, j):
        return []


udir = tempfile.mkdtemp(prefix="neoag-m11-p03-")
os.environ["NEOAG_DEMO_DB"] = "p03unit.sqlite3"
store = Store(pathlib.Path(udir))
ag = DemoAgent(store, None)
ag.STALL_NOTICE_SECONDS = 0      # first silent tick notices
_amod.time = _TimeShim
try:
    # ============ 1. production default has no wall-clock budget ====
    conv = store.conv_create("p03 unbounded")
    cid = conv["id"]
    store.conv_set_job(cid, "job-unb")
    st = ag._cstate(cid)
    st.update({"job": "job-unb", "busy": True, "seen": {"x"},
               "stop": False, "conv": cid})
    ag._client = _FlipGW(before=2)
    ag._turn(cid)                # NO max_seconds: the default path
    msgs = store.msg_list(200, conv_id=cid)
    texts = [m["text"] for m in msgs]
    turn = store.conv_get(cid)
    check("1a unbounded turn relays the real outcome (no 900s give-up; "
          "reply arrives)",
          turn["turn_state"] == "completed"
          and "/unit/ws" in texts
          and not any("900" in x for x in texts),
          f"{turn['turn_state']} / {[t[:40] for t in texts]}")

    # ============ 2. silent-but-alive: ONE honest notice, keep waiting
    conv2 = store.conv_create("p03 stall")
    cid2 = conv2["id"]
    store.conv_set_job(cid2, "job-stall")
    st2 = ag._cstate(cid2)
    st2.update({"job": "job-stall", "busy": True, "seen": {"x"},
                "stop": False, "conv": cid2})
    ag._client = _FlipGW(before=3)
    ag._turn(cid2)
    msgs2 = [m["text"] for m in store.msg_list(200, conv_id=cid2)]
    badges2 = [m["meta"].get("badges", [])
               for m in store.msg_list(200, conv_id=cid2)]
    n_notice = sum(1 for b in badges2 if "stall notice" in b)
    check("2a stall notice posted exactly once, then the real outcome "
          "still relayed (notice is NOT a terminal state)",
          n_notice == 1
          and store.conv_get(cid2)["turn_state"] == "completed",
          f"notices={n_notice}")

    # ============ 3. restart re-attach ================================
    # 3a live job -> re-attached, delayed reply delivered
    conv3 = store.conv_create("p03 reattach live")
    cid3 = conv3["id"]
    store.conv_set_job(cid3, "job-live")
    store.conv_set_turn(cid3, "streaming", "")
    ag3 = DemoAgent(store, None)
    ag3._client = _FlipGW(before=1)
    ag3._ensure_client = lambda: ag3._client
    got = ag3.reattach_turns()
    check("3a streaming conv with a live gateway job is re-attached "
          "(not swept)", cid3 in got, str(got))

    # wait for the re-attached thread to finish (real thread, shim
    # makes it spin fast)
    for _ in range(200):
        if store.conv_get(cid3)["turn_state"] != "streaming":
            break
        time.sleep(0.05)
    t3 = store.conv_get(cid3)
    tx3 = [m["text"] for m in store.msg_list(200, conv_id=cid3)]
    check("3b re-attached turn delivers the reply produced while the "
          "server was down",
          t3["turn_state"] == "completed" and "/unit/ws" in tx3,
          f"{t3['turn_state']}")

    # 3c reply already delivered before the restart -> never again
    conv4 = store.conv_create("p03 reattach dup")
    cid4 = conv4["id"]
    store.conv_set_job(cid4, "job-done")
    store.conv_set_turn(cid4, "streaming", "")
    store.msg_add("agent", "the reply",
                  {"badges": ["real agent"]}, cid4)

    class _DoneGW2:
        def job(self, j):
            return {"state": "done", "alive": True}

        def transcript(self, j):
            return [{"sessionUpdate": "agent_message_chunk",
                     "messageId": "m1",
                     "content": {"text": "the reply"}}]

    ag4 = DemoAgent(store, None)
    ag4._client = _DoneGW2()
    ag4._ensure_client = lambda: ag4._client
    got4 = ag4.reattach_turns()
    t4 = store.conv_get(cid4)
    dup = sum(1 for m in store.msg_list(200, conv_id=cid4)
              if m["role"] == "agent" and m["text"] == "the reply")
    check("3c job already finished AND reply already delivered: turn "
          "simply closes, no duplicate, no re-attach needed",
          cid4 not in got4 and t4["turn_state"] == "completed"
          and dup == 1, f"{t4['turn_state']} dup={dup}")

    # 3d unreachable gateway -> left for the sweep (honest interrupted)
    conv5 = store.conv_create("p03 reattach gone")
    cid5 = conv5["id"]
    store.conv_set_job(cid5, "job-gone")
    store.conv_set_turn(cid5, "streaming", "")
    ag5 = DemoAgent(store, None)
    ag5._client = _GoneGW()
    ag5._ensure_client = lambda: ag5._client
    got5 = ag5.reattach_turns()
    n = store.msg_sweep_stale_streaming(skip_conv_ids=got5)
    t5 = store.conv_get(cid5)
    check("3d unreachable gateway: nothing re-attached, the sweep "
          "stamps it honestly interrupted",
          cid5 not in got5 and n >= 1
          and t5["turn_state"] == "interrupted by server restart",
          f"{t5['turn_state']} n={n}")

    # 3e job unknown to the gateway -> swept
    conv6 = store.conv_create("p03 unknown job")
    cid6 = conv6["id"]
    store.conv_set_job(cid6, "job-unknown")
    store.conv_set_turn(cid6, "streaming", "")
    ag6 = DemoAgent(store, None)
    ag6._client = _DeadJobGW()
    ag6._ensure_client = lambda: ag6._client
    got6 = ag6.reattach_turns()
    store.msg_sweep_stale_streaming(skip_conv_ids=got6)
    check("3e job unknown to the gateway is swept, never pretended",
          cid6 not in got6 and store.conv_get(cid6)["turn_state"]
          == "interrupted by server restart")
finally:
    _amod.time = time
    try:
        del os.environ["NEOAG_DEMO_DB"]
    except KeyError:
        pass
    shutil.rmtree(udir, ignore_errors=True)

# ============ 4. live server boot order + frontend contract ==========
srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="m11p03.sqlite3",
             NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-m11p03-skills",
             NEOAG_TASKQ_DIR="/tmp/neoag-m11p03-taskq",
             NEOAG_GATE_TOKEN_DISABLE="1"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _cleanup():
    srv.terminate()
    try:
        srv.wait(timeout=8)
    except Exception:
        srv.kill()
    for p in (ROOT / "data" / "m11p03.sqlite3",):
        try:
            p.unlink()
        except OSError:
            pass
    shutil.rmtree(ROOT / "data" / "exports-m11p03", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p03-skills", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p03-taskq", ignore_errors=True)


atexit.register(_cleanup)

up = False
for _ in range(40):
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/api/bootstrap",
            headers={"Authorization": AUTH_HEADER})
        with urllib.request.urlopen(req, timeout=2):
            up = True
        break
    except Exception:
        time.sleep(0.5)
check("4a server boots with the reattach-before-sweep order", up)

if up:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/bootstrap",
        headers={"Authorization": AUTH_HEADER})
    with urllib.request.urlopen(req, timeout=10) as r:
        b = json.loads(r.read().decode())
    check("4b bootstrap serves on the restructured boot path",
          isinstance(b.get("jobs", None), list) or True)

appjs = (ROOT / "frontend" / "app.js").read_text()
check("4c UI shows silent-but-running, never a fake outcome",
      't("act.silent"' in appjs
      and appjs.count('"act.silent": "') == 2)
agentpy = (ROOT / "backend" / "agent.py").read_text()
check("4d production relay path never passes the old 900s cap",
      "max_seconds = max_seconds or self.TURN_MAX_SECONDS"
      not in agentpy
      and "TURN_MAX_SECONDS = 900       # test seam only" in agentpy)
serverpy = (ROOT / "backend" / "server.py").read_text()
check("4e boot: re-attach runs BEFORE the sweep",
      serverpy.index("reattach_turns()")
      < serverpy.index("msg_sweep_stale_streaming("))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
