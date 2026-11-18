#!/usr/bin/env python3
"""NeoAG quick-tunnel supervisor brain (health probes + recovery + status).

Why this exists: a Cloudflare Quick Tunnel whose random hostname expires on
the edge (2026-11-17 incident: cloudflared logged "Unauthorized: Tunnel not
found" in a permanent retry loop for days) keeps its process alive, so a
process-only watchdog - and systemd's Restart= - can never notice. Recovery
requires a FRESH cloudflared registration, which rotates the public URL;
this script owns that transition: verify the new URL for real, only then
publish it in the state file, and keep the old one in history as expired.

Layers checked per tunnel (recover only the failing layer):
  unit    - the systemd unit is active with a MainPID
  app     - the local upstream answers (a 401 from a Basic-auth app proves
            the app is up; it is NOT treated as an outage)
  network - the host itself can reach the Internet (a short local network
            blip must never burn a public URL)
  conn    - cloudflared has an established TCP/7844 connection to Cloudflare
  public  - the published URL resolves and returns an expected status

Streaks + grace: public failures must reach TUNNEL_HEALTH_FAIL_THRESHOLD
consecutive cycles (default 3, one cycle per minute) before the tunnel is
replaced; "Tunnel not found" in a fresh log line is conclusive and acts
immediately. Restarts are gated by exponential backoff (2 min doubling to
1 h max) on CONSECUTIVE recovery events, reset once the tunnel is healthy.

Commands:
  health-cycle   one supervised recovery cycle (run by the systemd timer)
  status         human report from the state file (--json for raw)
  probe NAME     one-shot layered probe dump for NAME (web|gw) - debug/tests

State file: /data1/neoag/agents/remote_access/tunnels/state.json (0600,
atomic writes under flock). It is OUTSIDE the repo: quick URLs are
rotating secrets-ish and must not be committed.
"""
import argparse
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

STATE_DIR = "/data1/neoag/agents/remote_access/tunnels"
STATE_PATH = os.path.join(STATE_DIR, "state.json")
LOCK_PATH = os.path.join(STATE_DIR, "lock")

# The hostname of the live quick tunnel. Note: the registration FAILURE
# line printed by cloudflared embeds https://api.trycloudflare.com/tunnel -
# that host is the registration API, never a tunnel hostname, so it is
# excluded here (a mid-registration status read once mistook it for the
# URL on 2026-11-18).
URL_RE = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
# Cloudflare edge error codes meaning "the request never reached your app":
CF_ERROR_CODES = (502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530)
# Anycast edge addresses used for the host-level network probe. This host's
# firewall opens outbound TCP/7844 to the Cloudflare edge (which is exactly
# the transport quick tunnels need) but may block other arbitrary 443
# destinations, so probing an edge socket is both the right test and the
# one most relevant to tunnel health.
CF_EDGE_IPS = ("198.41.200.13", "198.41.192.47", "198.41.200.9")

# 2026-11-18: the gateway tunnel (gw) was RETIRED after dependency audit:
# NeoAG Web reaches the gateway over loopback, the frontend makes no
# external calls, no config/script/cron references any gateway public
# hostname, and the operator uses CodeBuddy via SSH/tmux (local PTY, never
# touches :7890). units/neoag-tunnel-gw.service is kept for reference; to
# re-adopt, re-add a TUNNELS entry and reinstall (README_TUNNELS.md).
TUNNELS = {
    "web": {
        "unit": "neoag-tunnel-web.service",
        "target": "http://127.0.0.1:8712",
        # App layer = "the local process answers HTTP at all": any status
        # code proves the app is up (401 on a protected endpoint is proof,
        # not an outage); only a transport failure (0) means it is down.
        # Rotating a public URL cannot fix a broken app.
        "upstream_probe": "http://127.0.0.1:8712/api/bootstrap",
        # Public unauthenticated probe expects the same app-level answer:
        # a 401 THROUGH the tunnel proves route + authentication alive.
        "public_ok": (200, 401),
        "log": os.path.join(STATE_DIR, "web.log"),
        "label": "NeoAG Web",
        "client": "operator browser (Basic-auth protected app)",
    },
}

PUB_THRESHOLD = int(os.environ.get("TUNNEL_HEALTH_FAIL_THRESHOLD", "3"))
CONN_THRESHOLD = 2
MAX_BACKOFF = 3600
URL_TRIES = 3          # inline verification attempts for a new quick URL
URL_TRY_SLEEP = 5      # seconds between them (new hostnames can take ~1 min
                       # to resolve through this host's resolvers; what does
                       # not verify inline is retried by later cycles)


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ts_iso(epoch):
    return datetime.fromtimestamp(epoch,
                                  timezone.utc).astimezone().isoformat(
                                      timespec="seconds")


def parse_iso(s):
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def log(msg):
    print(f"[{now_iso()}] {msg}", flush=True)


# --------------------------------------------------------------------------
# probes

def http_code(url, timeout=10):
    """GET url -> HTTP status; 0 = transport failure (DNS/refused/TLS)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "neoag-tunnelctl/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code          # 401/403 are EXPECTED for protected probes
    except Exception:
        return 0


def dns_ok(url):
    host = url.split("//", 1)[-1].split("/", 1)[0]
    try:
        socket.getaddrinfo(host, 443)
        return True
    except Exception:
        return False


def unit_props(unit):
    out = subprocess.run(
        ["systemctl", "--user", "show", unit,
         "-p", "ActiveState", "-p", "SubState", "-p", "MainPID",
         "-p", "ExecMainStartTimestamp"],
        capture_output=True, text=True).stdout
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k] = v
    return d


def edge_conns(pid):
    """Established TCP/7844 (Cloudflare edge) sockets owned by pid."""
    if not pid:
        return 0
    out = subprocess.run(
        ["ss", "-tnp", "state", "established", "( dport = :7844 )"],
        capture_output=True, text=True).stdout
    return out.count(f"pid={pid},")


def log_tail_has(path, needle, max_age_s=900):
    """needle present in the last 64 KiB of a recently-written log."""
    try:
        if time.time() - os.path.getmtime(path) > max_age_s:
            return False
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            n = min(f.tell(), 65536)
            f.seek(-n, os.SEEK_END)
            return needle in f.read().decode("utf-8", "replace")
    except Exception:
        return False


def last_url_in_log(path):
    try:
        with open(path, "r", errors="replace") as f:
            urls = URL_RE.findall(f.read())
        return urls[-1] if urls else None
    except Exception:
        return None


def public_reach(name, url):
    """Does the published URL actually reach the app? -> (ok, detail)."""
    if not url:
        return False, "no url"
    if not dns_ok(url):
        return False, "dns-fail"
    code = http_code(url)
    if code == 0:
        return False, "connect-fail"
    if code in CF_ERROR_CODES:
        return False, f"edge-{code}"
    if code in TUNNELS[name]["public_ok"]:
        return True, f"HTTP {code}"
    # any other app-level code still proves the route reached the origin;
    # treat as reachable but note the surprise
    return True, f"HTTP {code} (unexpected)"


def network_up():
    """Can this host still reach the Cloudflare edge on TCP/7844?"""
    for ip in CF_EDGE_IPS:
        try:
            socket.create_connection((ip, 7844), timeout=5).close()
            return True
        except Exception:
            continue
    return False


def foreign_tunnels(name, main_pid):
    """cloudflared processes for the same target not owned by our unit."""
    pat = rf"cloudflared.*--url {TUNNELS[name]['target']}"
    out = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
    pids = [int(x) for x in out.stdout.split()]
    return [p for p in pids if p != main_pid]


# --------------------------------------------------------------------------
# state file

def default_state():
    def one(name):
        c = TUNNELS[name]
        return {"unit": c["unit"], "upstream": c["target"],
                "label": c["label"], "client": c["client"],
                "public_url": None, "url_state": "none",
                "pid": 0, "started_at": None, "status": "unknown",
                "last_public_ok": None, "last_failure": None,
                "recoveries": 0, "history": [],
                "counters": {"public_fail": 0, "conn_fail": 0,
                             "fail_streak": 0, "backoff_until": None,
                             "pending_url": None}}
    return {"version": 1, "updated_at": None,
            "tunnels": {n: one(n) for n in TUNNELS}}


def load_state():
    try:
        with open(STATE_PATH) as f:
            st = json.load(f)
        # forward-compat: fill in new tunnel keys; also PRUNE tunnels that
        # are no longer supervised (e.g. the retired gateway entry)
        base = default_state()
        st["tunnels"] = {n: st["tunnels"].get(n) or t
                         for n, t in base["tunnels"].items()}
        return st
    except FileNotFoundError:
        return default_state()
    except Exception as e:
        bad = STATE_PATH + ".bad"
        try:
            os.replace(STATE_PATH, bad)
            log(f"state file unreadable ({e}); preserved at {bad}")
        except Exception:
            pass
        return default_state()


def save_state(st):
    st["updated_at"] = now_iso()
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_PATH)


def activate_url(t, url):
    """Publish a VERIFIED url; demote the previous one to history."""
    old = t.get("public_url")
    if old and old != url:
        t["history"] = ([{"url": old, "expired_at": now_iso()}]
                        + t["history"])[:20]
    t["public_url"] = url
    t["url_state"] = "active"


def verify_url_new(name, url, tries=URL_TRIES):
    """A hostname is only adopted after the APP behind it answers with an
    expected code (200/401). Any other HTTP answer - even a reachable
    one - is not evidence: on 2026-11-18 a mid-registration status read
    matched https://api.trycloudflare.com (registration API, replies
    405) from a failure log line and adopted it as the tunnel URL."""
    detail = ""
    for i in range(tries):
        code = http_code(url, timeout=15) if dns_ok(url) else 0
        if code in TUNNELS[name]["public_ok"]:
            return True, f"HTTP {code}"
        detail = f"HTTP {code}" if code else "unreachable"
        if i < tries - 1:
            time.sleep(URL_TRY_SLEEP)
    return False, detail


# --------------------------------------------------------------------------
# recovery

def restart_tunnel(name, st, reason):
    t = st["tunnels"][name]
    k = t["counters"]
    bu = k.get("backoff_until")
    if bu and time.time() < parse_iso(bu):
        t["status"] = "recovering"
        log(f"{name}: restart DEFERRED until {bu} (reason now: {reason})")
        return
    unit = TUNNELS[name]["unit"]
    subprocess.run(["systemctl", "--user", "reset-failed", unit],
                   capture_output=True)
    r = subprocess.run(["systemctl", "--user", "restart", unit],
                       capture_output=True, text=True)
    k["fail_streak"] += 1
    delay = min(120 * 2 ** (k["fail_streak"] - 1), MAX_BACKOFF)
    k["backoff_until"] = ts_iso(time.time() + delay)
    k["public_fail"] = 0
    k["conn_fail"] = 0
    t["recoveries"] = int(t.get("recoveries", 0)) + 1
    t["last_failure"] = {"time": now_iso(), "reason": reason}
    # old hostname is dead by definition of this path: retire it now, publish
    # the replacement only after a real connectivity check succeeds
    if t["public_url"]:
        t["history"] = ([{"url": t["public_url"], "expired_at": now_iso()}]
                        + t["history"])[:20]
        t["public_url"] = None
        t["url_state"] = "pending"
    k["pending_url"] = None
    t["status"] = "recovering"
    if r.returncode == 0:
        log(f"{name}: RESTARTED {unit} (reason: {reason}); "
            f"next recovery cooldown {delay}s; new URL will be verified "
            f"before publication")
    else:
        log(f"{name}: restart of {unit} FAILED rc={r.returncode} "
            f"{r.stderr.strip()[:160]}")


def check_tunnel(name, st, net_ok):
    c = TUNNELS[name]
    t = st["tunnels"][name]
    k = t["counters"]
    prev_pid = int(t.get("pid") or 0)
    u = unit_props(c["unit"])
    t["pid"] = int(u.get("MainPID") or 0)
    t["started_at"] = (u.get("ExecMainStartTimestamp") or "").strip() or None

    foreign = foreign_tunnels(name, t["pid"])
    if foreign:
        log(f"{name}: WARNING unmanaged cloudflared for {c['target']}: "
            f"{foreign} (only pid {t['pid']} is supervised)")

    # -- out-of-band process restart check, BEFORE URL pickup: a quick
    # hostname dies with its cloudflared process, so if MainPID changed
    # (systemd Restart=, manual restart) the published URL is stale by
    # definition. One failed probe confirms it - no failure streak needed.
    if (t["url_state"] == "active" and t["public_url"] and prev_pid
            and t["pid"] != prev_pid
            and u.get("ActiveState") == "active"):
        ok, why = public_reach(name, t["public_url"])
        if not ok:
            t["history"] = ([{"url": t["public_url"], "expired_at": now_iso()}]
                            + t["history"])[:20]
            log(f"{name}: process restarted ({prev_pid} -> {t['pid']}), "
                f"old URL dead ({why}) - retired, awaiting replacement")
            t["public_url"] = None
            t["url_state"] = "pending"
            k["pending_url"] = None

    # -- pick up a fresh quick URL from the log if we are waiting for one --
    if t["url_state"] != "active":
        retired = {h["url"] for h in t["history"]}
        cand = k.get("pending_url")
        if cand and cand in retired:      # stale pending from an old epoch
            cand = None
            k["pending_url"] = None
        if not cand:
            cand = last_url_in_log(c["log"])
        if cand and cand != t["public_url"] and cand not in retired:
            ok, why = verify_url_new(name, cand)
            if ok:
                activate_url(t, cand)
                k["pending_url"] = None
                t["last_public_ok"] = now_iso()
                log(f"{name}: NEW URL VERIFIED ({why}): {cand}")
            else:
                k["pending_url"] = cand
                t["url_state"] = "pending"
                log(f"{name}: new URL {cand} not serving yet ({why}); "
                    f"will re-check next cycle")

    # -- layer 1: the unit itself --
    if u.get("ActiveState") != "active" or t["pid"] == 0:
        t["status"] = "unit-down"
        restart_tunnel(name, st,
                       f"unit {u.get('ActiveState')}/{u.get('SubState')}")
        return

    # -- layer 2: local app must answer (401 counts as answering) --
    app = http_code(c["upstream_probe"], timeout=8)
    if app == 0:
        t["status"] = "upstream-down"
        t["last_failure"] = {"time": now_iso(),
                             "reason": f"upstream {c['target']} not answering"}
        log(f"{name}: UPSTREAM DOWN ({c['target']}) - not rotating a URL "
            f"that could not serve anything")
        return

    # -- layer 3: host Internet (do not burn URLs during local outages) --
    if not net_ok:
        t["status"] = "network-down"
        t["last_failure"] = {"time": now_iso(),
                             "reason": "host has no outbound connectivity"}
        log(f"{name}: host outbound network down - no tunnel action taken")
        return

    # -- layer 4: edge connection --
    conns = edge_conns(t["pid"])
    k["conn_fail"] = 0 if conns > 0 else k["conn_fail"] + 1

    # -- layer 5: public route --
    if t["url_state"] != "active" or not t["public_url"]:
        t["status"] = "url-pending"
        return
    ok, why = public_reach(name, t["public_url"])
    if ok:
        t["status"] = "healthy"
        t["last_public_ok"] = now_iso()
        k["public_fail"] = 0
        if k["conn_fail"] < CONN_THRESHOLD and k["fail_streak"]:
            k["fail_streak"] = 0
            k["backoff_until"] = None
        return

    k["public_fail"] += 1
    t["status"] = "degraded"
    t["last_failure"] = {"time": now_iso(), "reason": f"public: {why}"}
    sig = log_tail_has(c["log"], "Tunnel not found")
    reasons = []
    if sig:
        reasons.append("edge says Tunnel-not-found")
    if k["public_fail"] >= PUB_THRESHOLD:
        reasons.append(f"public failed {k['public_fail']} cycles")
    if k["conn_fail"] >= CONN_THRESHOLD:
        reasons.append(f"no edge conn {k['conn_fail']} cycles")
    if reasons:
        restart_tunnel(name, st, f"{why}; " + "; ".join(reasons))
    else:
        log(f"{name}: public check failed ({why}) "
            f"{k['public_fail']}/{PUB_THRESHOLD} - grace, no action yet")


def cmd_health_cycle():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    lk = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another health cycle is running; exiting")
        return 0
    st = load_state()
    # one host-level network probe for the whole cycle
    net_ok = network_up()
    for name in TUNNELS:
        try:
            check_tunnel(name, st, net_ok)
        except Exception as e:            # one tunnel's probe must not kill
            log(f"{name}: probe error {type(e).__name__}: {e}")  # the cycle
    save_state(st)
    return 0


# --------------------------------------------------------------------------
# status report

def cmd_status(as_json):
    st = load_state()
    if as_json:
        print(json.dumps(st, indent=1))
        return 0 if all(t["status"] == "healthy"
                        for t in st["tunnels"].values()) else 2
    print(f"NeoAG quick tunnels - state {STATE_PATH}")
    print(f"updated {st['updated_at']}")
    bad = 0
    for name, t in st["tunnels"].items():
        print()
        print(f"[{name}] {t['label']}  status: {t['status']}")
        print(f"  URL      : {t['public_url'] or ('(waiting for a new '
               'URL)' if t['url_state'] == 'pending' else '(none)')}")
        print(f"  upstream : {t['upstream']}   unit: {t['unit']}"
              f"   pid: {t['pid'] or '-'}")
        if t.get("started_at"):
            print(f"  since    : {t['started_at']}")
        print(f"  last public check OK: {t['last_public_ok'] or 'never'}")
        lf = t.get("last_failure")
        if lf:
            print(f"  last failure: {lf['time']}  {lf['reason']}")
        if t.get("recoveries"):
            print(f"  recoveries : {t['recoveries']}")
        if t["counters"].get("backoff_until"):
            print(f"  recovery cooldown until: {t['counters']['backoff_until']}")
        for h in t["history"][:2]:
            print(f"  expired URL: {h['url']} (expired {h['expired_at']})")
        if t["status"] != "healthy":
            bad += 1
    return 0 if bad == 0 else 2


def cmd_probe(name):
    c = TUNNELS[name]
    st = load_state()
    t = st["tunnels"][name]
    u = unit_props(c["unit"])
    print(f"== {name} layered probe ==")
    print(f"unit     : {u}")
    print(f"app      : HTTP {http_code(c['upstream_probe'], 8)} "
          f"(0 = app down; any code = responding)")
    print(f"network  : edge TCP/7844 reachable: {network_up()}")
    pid = int(u.get("MainPID") or 0)
    print(f"edgeconn : {edge_conns(pid)} established dport-7844 for pid {pid}")
    url = t.get("public_url")
    if url:
        print(f"public   : {url} -> {public_reach(name, url)}")
    else:
        print("public   : (no active url in state)")
    print(f"log-url  : last url in {c['log']}: {last_url_in_log(c['log'])}")
    print(f"notfound-sig : {log_tail_has(c['log'], 'Tunnel not found')}")
    print(f"foreign  : {foreign_tunnels(name, pid)}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health-cycle")
    sp = sub.add_parser("status")
    sp.add_argument("--json", action="store_true")
    pp = sub.add_parser("probe")
    pp.add_argument("name", choices=sorted(TUNNELS))
    a = ap.parse_args()
    if a.cmd == "health-cycle":
        sys.exit(cmd_health_cycle())
    if a.cmd == "status":
        sys.exit(cmd_status(a.json))
    if a.cmd == "probe":
        sys.exit(cmd_probe(a.name))


if __name__ == "__main__":
    main()
