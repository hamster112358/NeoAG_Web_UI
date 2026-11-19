#!/usr/bin/env python3
"""M11 P0-2: unit battery for the execution gate (the vendored
agent_workspace_assets/.codebuddy/gate.py, which start.sh deploys into
the live agent workspace).

The gate classifies PreToolUse events; decide(ev) returns None (allow),
("deny", ...) or ("confirm", detail, op_payload). Every case below is
either a REAL command observed in a saved chat transcript or a
deliberate bypass attempt. The fd-redirect fix (2>/dev/null must not
read as "destructive write") must not weaken ANY other class: every
deny/confirm path keeps its own regression cases here.

The approval identity (op hash) is computed from the RAW command - a
case here asserts that explicitly, because a stripped hash would let a
different command replay a grant.
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(HERE, "..", "agent_workspace_assets", ".codebuddy",
                    "gate.py")


def load_gate():
    spec = importlib.util.spec_from_file_location("neoag_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ev(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def main():
    g = load_gate()
    ws = g.WS
    fails = []
    # hermetic: never consult live workspace grants/scope files
    import tempfile as _tf
    g.APPROVALS = _tf.mkdtemp(prefix="m11gatescope")

    def check(name, cmd, want):
        """want: None | 'confirm' | 'deny'"""
        d = g.decide(ev(cmd))
        got = None if d is None else d[0]
        ok = got == want
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            fails.append((name, cmd, want, str(d)[:200]))

    # ---- fixed false positives: fd plumbing on READ-ONLY commands ----
    check("fdnull: ls 2>/dev/null (transcript case)",
          f"ls {ws}; echo ---; find {ws} -maxdepth 3 -iname '*poly*' "
          "-o -maxdepth 3 -iname '*novo*' 2>/dev/null | head -40", None)
    check("fdnull: grep 2>/dev/null | head",
          f"grep -l 'POLYSOLVER|novoalign' {ws} 2>/dev/null | head -20",
          None)
    check("fddup: cat 2>&1", "cat /etc/hostname 2>&1", None)
    check("fddup: echo >&2", "python3 -c 'print(1)' >&2", None)
    check("fdnull: &>/dev/null", "curl -s https://example.com &>/dev/null",
          None)
    check("fdnull: >>&/dev/null variant", "ls /etc 2>>/dev/null", None)

    # ---- still allowed: routine scratch reads/writes ----
    check("ws write allowed", f"echo hi > {ws}/jobs/log.txt", None)
    check("tmp write allowed", "touch /tmp/neoag_gate_test_x", None)
    check("tmp redirect allowed", "ls /etc 2>/dev/null > /tmp/list.txt",
          None)
    check("tmp rm allowed", "rm -f /tmp/junk.txt", None)
    check("plan mode allowed",
          f"open-neo run --mode plan --sample-manifest {ws}/m.json", None)

    # ---- unfixed-by-design: redirects to REAL files are writes ----
    check("stderr redirect to outside file still confirms",
          "echo x 2> /data2/caojingqi/err.log", "confirm")
    check("stdout redirect to outside file still confirms",
          "ls /etc > /data2/caojingqi/evil.txt", "confirm")
    check("&> outside file still confirms",
          "make 2>&1 &> /home/someone/build.log", "confirm")

    # ---- write / delete / force classes unchanged ----
    check("cp outside confirms",
          f"cp {ws}/a.txt /data2/caojingqi/out/b.txt", "confirm")
    check("rm outside confirms", "rm /data2/caojingqi/somedir/file",
          "confirm")
    check("rm outside with 2>/dev/null STILL confirms",
          "rm /data2/caojingqi/somedir/file 2>/dev/null", "confirm")
    check("find -delete confirms",
          "find / -name x -delete 2>/dev/null", "confirm")
    check("mv to /dev/null confirms (data destruction)",
          f"mv {ws}/job/out /dev/null", "confirm")
    check("kill confirms", "kill 12345", "confirm")
    check("chmod -R confirms", "chmod -R 777 /data1/neoag", "confirm")

    # ---- deny classes unchanged ----
    check("secret read denies",
          "cat /data1/neoag/secrets/github_askpass.sh 2>/dev/null", "deny")
    check("auth.json mention denies", "grep token auth.json", "deny")
    check("gate self-write denies",
          f"echo pwn > {ws}/.codebuddy/gate.py", "deny")
    check("approvals dir denies", "ls /data1/neoag/agents/neoag_agent_ws/"
          ".neoag-approvals/pending", "deny")
    check("frozen tree write denies",
          "echo x >> /data2/caojingqi/original_OpenNeoAG/neoag/src/x.py",
          "deny")
    check("frozen tree even via 2> redirect denies",
          "make 2> /data2/caojingqi/original_OpenNeoAG/neoag/err.log",
          "deny")
    check("formal run launch denies",
          "open-neo run --sample-manifest m.json --mode execute", "deny")
    check("neoag.cli run-full denies", "python3 -m neoag.cli run-full",
          "deny")
    check("ad-hoc gateway launch denies",
          "python3 -m neoag.controlled_execution.gateway --port 9999",
          "deny")

    # ---- approval identity stays the RAW command (no replay drift) ----
    cmd = "cp /tmp/a /data2/caojingqi/b 2>/dev/null"
    d = g.decide(ev(cmd))
    h_stripped = g.op_hash({"tool": "Bash", "command":
                            g.strip_fd_redirects(cmd)})
    h_raw = g.op_hash({"tool": "Bash", "command": cmd})
    payload = d[2] if d and len(d) > 2 else {}
    ok = (payload.get("command") == cmd and g.op_hash(payload) == h_raw
          and h_stripped != h_raw)
    print(("PASS " if ok else "FAIL ") + "op hash identity uses raw cmd")
    if not ok:
        fails.append(("op-hash identity", cmd, "raw-command hash",
                      json.dumps(payload)[:120]))

    # ---- M11 P0-2: the MUT_TOOLS hole (NameError -> fail-open) ----
    def check_ev(name, event, want):
        d = g.decide(event)
        got = None if d is None else d[0]
        ok = got == want
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            fails.append((name, "event", want, str(d)[:200]))

    check_ev("MUT: Write outside workspace confirms",
             {"tool_name": "Write", "tool_input":
              {"file_path": "/data1/neoag/other_place/x.txt"}},
             "confirm")
    check_ev("MUT: Edit outside workspace confirms",
             {"tool_name": "Edit", "tool_input":
              {"file_path": "/data1/neoag/other_place/x.txt"}},
             "confirm")
    check_ev("MUT: Write inside workspace allows",
             {"tool_name": "Write", "tool_input":
              {"file_path": f"{ws}/jobs/ok.txt"}}, None)
    check_ev("MUT: unrelated tool (WebFetch) allows",
             {"tool_name": "WebFetch", "tool_input": {"url": "x"}},
             None)

    # ---- M11 P0-2: scoped "always allow within this scope" grants ----
    import time as _t
    deep = "/data1/neoag/agents/neoag_agent_runs/case1/out"

    def grants(entries):
        with open(os.path.join(g.APPROVALS, "scope-grants.json"),
                  "w") as fh:
            json.dump(entries, fh)

    check("scope: no grants file -> still confirm",
          f"cp /tmp/f.txt {deep}/x.txt", "confirm")
    grants([{"id": "g1", "roots": [deep],
             "expires": _t.time() + 3600}])
    check("scope: covered write auto-allows",
          f"cp /tmp/f.txt {deep}/x.txt", "scoped")
    check("scope: exact-root mkdir covered",
          f"mkdir -p {deep}", "scoped")
    check("scope: write outside granted root still confirm",
          "cp /tmp/f.txt /data1/neoag/other_place/x.txt", "confirm")
    check("scope: PARTIAL cover still confirm",
          f"cp /tmp/f.txt {deep}/x.txt; cp /tmp/g.txt /etc/x", "confirm")
    check("scope: DELETE under granted root still confirm",
          f"rm -f {deep}/x.txt", "confirm")
    check("scope: force verb under granted root still confirm",
          f"chmod -R 777 {deep}", "confirm")
    check("scope: prefix-lookalike dir NOT covered",
          f"cp /tmp/f.txt {deep}evil/x.txt", "confirm")
    grants([{"id": "g2", "roots": [deep], "expires": _t.time() - 1}])
    check("scope: EXPIRED grant -> confirm",
          f"cp /tmp/f.txt {deep}/x.txt", "confirm")
    grants([{"id": "g3", "roots": [], "expires": _t.time() + 3600}])
    check("scope: empty roots never match",
          f"cp /tmp/f.txt {deep}/x.txt", "confirm")
    grants([{"id": "g4", "roots": [deep], "expires": _t.time() + 3600}])
    d = g.decide({"tool_name": "Write",
                  "tool_input": {"file_path": deep + "/y.txt"}})
    ok = d is not None and d[0] == "scoped"
    print(("PASS " if ok else "FAIL ") + "scope: Write tool covered")
    if not ok:
        fails.append(("scope: Write tool covered", "Write", "scoped",
                      str(d)[:120]))
    ok = os.path.isfile(os.path.join(g.APPROVALS, "scope-audit.jsonl"))
    # main() (not decide()) writes the audit line - exercise the real
    # entrypoint once end to end so the journalling is proven:
    import subprocess
    evf = {"tool_name": "Bash",
           "tool_input": {"command": f"cp /tmp/f.txt {deep}/z.txt"}}
    r = subprocess.run([sys.executable, GATE], input=json.dumps(evf),
                       capture_output=True, text=True,
                       env={**os.environ, "NEOAG_GATE_APPROVALS":
                            g.APPROVALS})
    ok2 = ("allow" in r.stdout) and os.path.isfile(
        os.path.join(g.APPROVALS, "scope-audit.jsonl"))
    print(("PASS " if ok2 else "FAIL ") +
          "scope: main() allows + journals the auto-allow")
    if not ok2:
        fails.append(("scope audit", "subprocess gate",
                      "allow + audit line", r.stdout[:160]))

    print(f"\n{len(fails)} failure(s)")
    for name, cmd, want, got in fails:
        print(f"  {name}: want={want} got={got}  cmd={cmd[:80]}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
