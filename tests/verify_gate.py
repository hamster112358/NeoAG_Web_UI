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

    print(f"\n{len(fails)} failure(s)")
    for name, cmd, want, got in fails:
        print(f"  {name}: want={want} got={got}  cmd={cmd[:80]}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
