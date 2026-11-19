"""Long-message regression battery: a chat prompt is never silently cut.

Defect (2026-11-19): POST /api/chat did `str(body.get("text",""))[:4000]`.
An 18-section specification prompt was therefore cut mid-word, the cut
version was persisted, the caller still got HTTP 200, and the agent
silently worked from an incomplete specification. The client layer had
the same class of bug: dispatch/reply used `[:16000]`, and because the
user's message is appended LAST on the dispatch path (after the preamble
and rolling transcript), that slice would have cut the tail off the
user's own prompt.

Fix contract asserted here:
  - messages up to server.MAX_CHAT_CHARS round-trip byte-exactly;
  - anything larger is REJECTED (413 + limit + actual) and nothing is
    persisted or dispatched;
  - codebuddy_client never returns a shortened copy of a prompt.

Runs against an ISOLATED server (fresh NEOAG_DEMO_DB on port 8797) that
this script spawns itself - the live demo state, its database and the
real CodeBuddy gateway are never touched.

Run:      python3 tests/verify_long_message.py
Optional: NEOAG_LONGMSG_FIXTURE=/path/to/spec.txt round-trips that file
          byte-exactly. Point it at the real Milestone 10 specification
          once re-supplied - this test never reconstructs prompt text.
"""
import atexit
import base64
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _free_port():
    """Ask the OS for an unused port.

    A FIXED port is not safe here: a leftover server from an earlier run
    that still holds it answers instead of the freshly spawned one, and
    the battery then silently verifies stale code (observed 2026-11-19:
    an orphan on 8797 made the 413 checks fail against pre-fix code).
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = int(os.environ.get("NEOAG_LONGMSG_PORT", "0")) or _free_port()
BASE = f"http://127.0.0.1:{PORT}"
DB = "longmsgtest.sqlite3"
PASS, FAIL, SKIP = [], [], []

sys.path.insert(0, str(ROOT))

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

# a SIGKILLed previous run can leave its DB behind: remove it BEFORE
# spawning so "fresh workspace" is genuinely fresh
try:
    (ROOT / "data" / DB).unlink()
except OSError:
    pass

_srv = subprocess.Popen(
    [sys.executable, "-B", "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB=DB, NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-longmsg-skills",
             NEOAG_TASKQ_DIR="/tmp/neoag-longmsg-taskq"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _cleanup():
    _srv.terminate()
    try:
        _srv.wait(timeout=8)
    except Exception:
        _srv.kill()
    try:
        (ROOT / "data" / DB).unlink()
    except OSError:
        pass
    import shutil as _sh
    for _d in ("/tmp/neoag-longmsg-skills", "/tmp/neoag-longmsg-taskq"):
        _sh.rmtree(_d, ignore_errors=True)
    _sh.rmtree(ROOT / "data" / "exports-longmsgtest", ignore_errors=True)


atexit.register(_cleanup)


def _wait_up():
    for _ in range(80):
        try:
            req = urllib.request.Request(
                BASE + "/api/bootstrap",
                headers={"Authorization": AUTH_HEADER})
            urllib.request.urlopen(req, timeout=3)
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("isolated long-message server did not start")


_wait_up()

# The answering server must be OURS. If the child died (port conflict),
# whatever answered /api/bootstrap is somebody else's process running
# unknown code - refuse to report results from it.
if _srv.poll() is not None:
    raise RuntimeError(
        f"isolated server exited (rc={_srv.returncode}); port {PORT} was "
        f"already in use - refusing to test a server this battery did not "
        f"spawn")


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS" if ok else "FAIL") + f"  {name}"
          + (f"  [{detail}]" if detail and not ok else ""))


def skipped(name, why):
    SKIP.append(name)
    print(f"SKIP  {name}  ({why})")


def call(path, body=None, method=None, timeout=30):
    """Returns (status, parsed_body). HTTP errors are parsed, not raised."""
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
            return e.code, {"raw": raw}


def send(text, cid=""):
    return call("/api/chat", {"text": text, "conv_id": cid})


def restored_user_msgs(cid):
    """Read the conversation back the way a page reload does."""
    st, d = call(f"/api/conversations/{cid}")
    if st != 200:
        return []
    return [m["text"] for m in d.get("messages", []) if m["role"] == "user"]


# ---------------------------------------------------------------- limit
# Learned from the API itself, so the test cannot drift from the code.
LIMIT = None
st, d = send("x" * 10)
cid0 = d.get("conv_id", "")
base_n = len(restored_user_msgs(cid0))
st, d = send("y" * 500_000, cid0)
check("over-limit send returns 413", st == 413, f"got {st}")
LIMIT = d.get("limit")
check("413 reports the numeric limit", isinstance(LIMIT, int) and LIMIT > 0,
      json.dumps(d)[:160])
check("413 reports the actual size", d.get("actual") == 500_000,
      json.dumps(d)[:160])
check("413 carries a human-readable detail",
      bool(d.get("detail")) and str(LIMIT) in d.get("detail", ""),
      json.dumps(d)[:200])
check("413 persists NOTHING for the rejected message",
      len(restored_user_msgs(cid0)) == base_n,
      f"{len(restored_user_msgs(cid0))} vs {base_n}")

if not LIMIT:
    print("\nCannot continue without the server-side limit.")
    sys.exit(1)

# ------------------------------------------------- regression: the 4000 cut
LONG = ("## Section\n\n- requirement text\n\n" * 400)  # ~10,800 chars
check("test prompt is substantially over the old 4000 cap",
      len(LONG) > 4000, str(len(LONG)))
LONG = LONG + "\nFINAL-TAIL-MUST-SURVIVE"
st, d = send(LONG)
cid1 = d.get("conv_id", "")
got = restored_user_msgs(cid1)
check("12k-char message accepted", st == 200, f"got {st}")
check("12k-char message persisted at FULL length (not 4000)",
      got and len(got[-1]) == len(LONG),
      f"stored {len(got[-1]) if got else 'none'} vs sent {len(LONG)}")
check("12k-char message stored byte-exactly",
      got and got[-1] == LONG, "")
check("12k-char message tail survives (the exact lost failure)",
      got and got[-1].endswith("FINAL-TAIL-MUST-SURVIVE"),
      repr(got[-1][-40:]) if got else "no message")

# ------------------------------------------------------- at-limit boundary
st, d = send("a" * LIMIT)
cid2 = d.get("conv_id", "")
got = restored_user_msgs(cid2)
check(f"message of exactly the limit ({LIMIT}) is accepted", st == 200,
      f"got {st}")
check("at-limit message round-trips at full length",
      got and len(got[-1]) == LIMIT, f"stored {len(got[-1]) if got else 0}")

st, d = send("a" * (LIMIT + 1), cid2)
check("limit+1 is rejected", st == 413, f"got {st}")
check("limit+1 is not silently truncated and stored",
      all(len(t) <= LIMIT for t in restored_user_msgs(cid2)), "")

# ------------------------------------------------------ Unicode / Chinese
# Counted in characters, so a Chinese prompt gets the SAME allowance as an
# ASCII one. Byte length deliberately differs from char length, so a
# byte-based limit would fail here instead of passing quietly.
ZH = ("# 里程碑说明\n\n本节描述NeoAG流水线的可靠性要求，"
      "包括消息完整性、验证与回归测试。\n\n" * 260)
check("Chinese prompt exceeds 4000 chars", len(ZH) > 4000, str(len(ZH)))
check("Chinese prompt byte length != char length",
      len(ZH.encode("utf-8")) != len(ZH),
      f"bytes={len(ZH.encode('utf-8'))} chars={len(ZH)}")
ZH = ZH + "\n最终句子必须完整保留。"
st, d = send(ZH)
cid3 = d.get("conv_id", "")
got = restored_user_msgs(cid3)
check("long Chinese prompt accepted", st == 200, f"got {st}")
check("long Chinese prompt stored unchanged (no mojibake, no cut)",
      got and got[-1] == ZH,
      f"stored {len(got[-1]) if got else 'none'} vs sent {len(ZH)}")
check("Chinese prompt final sentence survives",
      got and got[-1].endswith("最终句子必须完整保留。"), "")

# ---------------------------------------------------- long Markdown prompt
MD = "\n".join(
    f"### {i}. Heading with `inline code` and **bold**\n\n"
    f"- bullet with a > quote marker\n- another\n\n"
    f"```toml\nkey{i} = \"value {i}\"\n```\n" for i in range(300))
MD += "\n<!-- END OF SPECIFICATION -->"
st, d = send(MD)
cid4 = d.get("conv_id", "")
got = restored_user_msgs(cid4)
check(f"long Markdown prompt ({len(MD)} chars) accepted", st == 200,
      f"got {st}")
check("Markdown prompt stored unchanged", got and got[-1] == MD, "")
check("Markdown fences and comment tail intact",
      got and got[-1].endswith("<!-- END OF SPECIFICATION -->")
      and "```toml" in got[-1], "")

# ------------------------------------------------- client transport layer
# No gateway contact: the client's own _req is stubbed, so this asserts
# what WOULD be posted without dispatching a real agent job.
from backend import codebuddy_client as cb  # noqa: E402

check("transport limit is above the user-facing chat limit "
      "(so the 413 is the limit users ever hit)",
      cb.MAX_PROMPT_CHARS > LIMIT,
      f"transport={cb.MAX_PROMPT_CHARS} chat={LIMIT}")


class _Stub:
    def _req(self, method, path, payload=None):
        self.seen = (method, path, payload)
        return 200, json.dumps({"data": {"id": "J-STUB", "delivered": True}})


s = _Stub()
PROMPT = "preamble\n" + "z" * 60_000 + "\nUSER ACTUAL MESSAGE TAIL"
cb.CodeBuddyClient.dispatch(s, PROMPT, "/tmp", "t")
check("dispatch posts the prompt unchanged (no [:16000] slice)",
      s.seen[2]["prompt"] == PROMPT,
      f"posted {len(s.seen[2]['prompt'])} vs {len(PROMPT)}")
check("dispatch prompt tail survives",
      s.seen[2]["prompt"].endswith("USER ACTUAL MESSAGE TAIL"), "")

s2 = _Stub()
cb.CodeBuddyClient.reply(s2, "J-STUB", "r" * 60_000 + "TAIL")
check("reply posts text unchanged (no [:16000] slice)",
      s2.seen[2]["text"] == "r" * 60_000 + "TAIL",
      f"posted {len(s2.seen[2]['text'])}")

try:
    cb.within_limit("q" * (cb.MAX_PROMPT_CHARS + 1), "prompt")
    check("oversized prompt raises instead of being cut", False, "no raise")
except cb.CodeBuddyError as e:
    check("oversized prompt raises instead of being cut",
          str(cb.MAX_PROMPT_CHARS) in str(e) and "Nothing was sent" in str(e),
          str(e)[:160])

check("within_limit returns the identical object when in range",
      cb.within_limit("abc", "prompt") == "abc", "")

# ------------------------------------------------- user-supplied fixture
FIX = os.environ.get("NEOAG_LONGMSG_FIXTURE", "")
if FIX and pathlib.Path(FIX).is_file():
    spec = pathlib.Path(FIX).read_text(encoding="utf-8")
    st, d = send(spec)
    cidf = d.get("conv_id", "")
    got = restored_user_msgs(cidf)
    check(f"fixture round-trips ({len(spec)} chars)", st == 200, f"got {st}")
    check("fixture stored byte-exactly, first to last character",
          got and got[-1] == spec,
          f"stored {len(got[-1]) if got else 'none'} vs sent {len(spec)}")
else:
    skipped("full Milestone 10 specification round-trip",
            "NEOAG_LONGMSG_FIXTURE not set - awaiting the re-sent "
            "specification; its text is never reconstructed here")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
