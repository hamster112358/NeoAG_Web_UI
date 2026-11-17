#!/usr/bin/env python3
"""M7 §6 unit verification: real-agent delivery repair.

Hermetic by design: a scripted fake gateway client drives the REAL
DemoAgent code paths (handle_real + _turn), so the session-restore
recovery is exercised without dispatching actual CodeBuddy jobs. The
whole run uses a throwaway NEOAG_DEMO_DB - nothing here touches the
live data directory or the real gateway.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="m7recov-"))
os.environ["NEOAG_DEMO_DB"] = str(TMP / "state.sqlite3")
os.environ["NEOAG_TASKQ_DIR"] = str(TMP / "taskq")
os.environ["NEOAG_SKILLS_DIR"] = str(TMP / "skills")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import server  # noqa: E402  (env set: scratch DB)

store = server.store
agent = server.agent


class FakeClient:
    _seq = 0  # GLOBAL across scenarios: relay threads from a finished
              # scenario keep polling, and re-used job ids would let
              # them act on a LATER scenario's jobs (cross-talk)

    def __init__(self):
        self.details = {}
        self.transcripts = {}
        self.reply_results = {}
        self.dispatched = []
        self.replied = []
        self.n = 0

    def job(self, jid):
        return self.details.get(jid, {"unknown": True, "http": 404})

    def transcript(self, jid):
        return list(self.transcripts.get(jid, []))

    def reply(self, jid, text):
        self.replied.append((jid, text))
        return self.reply_results.get(jid, {"delivered": True})

    def dispatch(self, prompt, cwd, name, **kw):
        self.n += 1
        jid = f"fake-{self.n}"
        self.dispatched.append(prompt)
        self.details.setdefault(jid, {"alive": True, "state": "working"})
        self.transcripts.setdefault(jid, [])
        return {"id": jid}


FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def wait(fn, timeout=25, step=0.25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(step)
    return False


def new_conv(title):
    c = store.conv_create(title)
    return c["id"]


def texts(cid):
    return [m["text"] for m in store.msg_list(500)
            if m.get("conv_id") == cid]


def mk():
    fc = FakeClient()
    agent._client = fc
    agent._conv.clear()
    return fc


# bounded turn lifetime: every leaked relay thread dies on its own
# within this window (scenario chains themselves finish in <8s)
agent.TURN_MAX_SECONDS = 15


def drain(fc):
    """End-of-scenario hygiene: push every scripted job to a terminal
    state (relay threads observing it exit honestly; each message still
    gets AT MOST its one recovery dispatch), then wait past the relay
    sleep so no thread from this scenario is alive during the next one
    (    a live thread re-reads self._client in handle_real and would act
    on the NEXT scenario's client)."""
    if fc is None:
        return
    for _ in range(2):
        for jid in list(fc.details):
            fc.details[jid] = {"alive": False, "state": "done"}
        time.sleep(4)


fc = None

# ---------------------------------------------------------------- S1
# settled session ("done", alive false): a reply must NEVER be
# attempted; the message goes straight to session restore.
drain(fc)
fc = mk()
cid = new_conv("s1")
store.conv_set_job(cid, "old-job", "done")
fc.details["old-job"] = {"alive": False, "state": "done",
                         "settled": True}
msgs = agent.handle_real("hello?", cid)
check("S1 no reply to settled session", fc.replied == [],
      str(fc.replied))
check("S1 re-dispatched from history", fc.n == 1
      and fc.dispatched[0].endswith("hello?"))
check("S1 honest re-session notice",
      any("had ended" in m["text"] for m in msgs))

# ---------------------------------------------------------------- S1b
# settled but STILL ALIVE worker: this is legitimate multi-turn -
# the reply must continue the SAME session (verified gateway
# behaviour: replies work after settle while the worker is alive).
drain(fc)
fc = mk()
cid = new_conv("s1b")
store.conv_set_job(cid, "settled-job", "done")
fc.details["settled-job"] = {"alive": True, "state": "done",
                             "settled": True}
agent.handle_real("follow-up question", cid)
check("S1b alive+settled continues same session",
      fc.replied and fc.replied[0][0] == "settled-job" and fc.n == 0,
      f"replied={fc.replied} n={fc.n}")

# ---------------------------------------------------------------- S2
# live worker whose reply gets REFUSED after the liveness check
# (race): must recover via re-dispatch, not dead-end error.
drain(fc)
fc = mk()
cid = new_conv("s2")
store.conv_set_job(cid, "live-job", "working")
fc.details["live-job"] = {"alive": True, "state": "working"}
fc.reply_results["live-job"] = {"error": "worker gone", "http": 410}
msgs = agent.handle_real("second question", cid)
check("S2 reply attempted once", fc.replied
      and fc.replied[0][0] == "live-job")
check("S2 refused -> re-dispatched", fc.n == 1
      and fc.dispatched[0].endswith("second question"))
check("S2 no dead-end error surfaced",
      not any("Could not reach" in m["text"] for m in msgs),
      str([m["text"] for m in msgs]))

# ---------------------------------------------------------------- S3
# THE delivered:false case: dispatch succeeds, worker then settles
# "done" WITHOUT ever answering -> the relay must restore the session
# exactly once and deliver the eventual reply.
drain(fc)
fc = mk()
cid = new_conv("s3")
agent.handle_real("third question", cid)
j1 = f"fake-{fc.n}"
time.sleep(0.4)
fc.details[j1] = {"alive": False, "state": "done"}
ok = wait(lambda: fc.n == 2)
check("S3 silent settle triggers one recovery", ok
      and fc.dispatched[-1].endswith("third question"),
      f"n={fc.n}")
j2 = f"fake-{fc.n}"
fc.transcripts[j2] = [
    {"sessionUpdate": "agent_message_chunk", "messageId": "m1",
     "content": {"text": "Here is the real answer."}}]
fc.details[j2] = {"alive": False, "state": "done"}
ok = wait(lambda: any("Here is the real answer." in t
                      for t in texts(cid)))
check("S3 reply delivered after recovery", ok)
check("S3 exactly one recovery", fc.n == 2, f"n={fc.n}")

# ---------------------------------------------------------------- S4
# recovery must not loop: if the restored session ALSO settles without
# answering, the turn ends honestly ("no reply recorded"), no 3rd
# dispatch.
drain(fc)
fc = mk()
cid = new_conv("s4")
agent.handle_real("fourth question", cid)
j1 = f"fake-{fc.n}"
time.sleep(0.4)
fc.details[j1] = {"alive": False, "state": "done"}
wait(lambda: fc.n == 2)
j2 = f"fake-{fc.n}"
fc.details[j2] = {"alive": False, "state": "done"}
ok = wait(lambda: any("No assistant reply text was recorded" in t
                      for t in texts(cid)))
check("S4 honest terminal after failed recovery", ok)
time.sleep(3)
check("S4 no recovery loop", fc.n == 2, f"n={fc.n}")

# ---------------------------------------------------------------- S5
# zombie guard: worker gone while the record still says "working" -
# the turn must recover instead of waiting out the 15-minute timeout.
drain(fc)
fc = mk()
cid = new_conv("s5")
agent.handle_real("fifth question", cid)
time.sleep(0.4)
fc.details["fake-1"] = {"alive": False, "state": "working"}
ok = wait(lambda: fc.n == 2, timeout=30)
check("S5 zombie record triggers recovery", ok, f"n={fc.n}")
# settle the recovered session so the thread exits cleanly
fc.transcripts["fake-2"] = [
    {"sessionUpdate": "agent_message_chunk", "messageId": "m9",
     "content": {"text": "Recovered answer."}}]
fc.details["fake-2"] = {"alive": False, "state": "done"}
wait(lambda: any("Recovered answer." in t for t in texts(cid)),
     timeout=20)

# ---------------------------------------------------------------- S6
# deliberate cancel (R7 field case): a job STOPPED from outside is a
# recorded outcome, not a lost session - the turn must end honestly
# ("cancelled") and must NOT re-dispatch the message (that would
# duplicate work someone ended on purpose).
drain(fc)
fc = mk()
cid = new_conv("s6")
agent.handle_real("sixth question", cid)
j1 = f"fake-{fc.n}"
time.sleep(0.4)
fc.details[j1] = {"alive": False, "state": "stopped"}
ok = wait(lambda: any("No assistant reply text was recorded" in t
                      for t in texts(cid)), timeout=25)
check("S6 stopped job -> honest terminal, no auto re-dispatch",
      ok and fc.n == 1, f"n={fc.n}")

print("RESULT:", "ALL PASS" if not FAILS else f"FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
