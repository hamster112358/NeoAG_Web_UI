#!/usr/bin/env python3
"""NeoAG Web execution gate - PreToolUse hook for agent jobs dispatched
against this workspace (project-level .codebuddy/settings.json).

Purpose: let the real CodeBuddy agent execute ordinary work without
approval prompts, while making destructive or high-impact operations
impossible without an explicit, single-use human approval issued by the
NeoAG Web backend (the /api/approvals confirm flow writes a grant file
into .neoag-approvals/ named <op-hash>.json).

Decision classes (hook semantics verified live 2026-11-14, gateway
v2.180.0: project hooks fire in headless jobs; deny blocks before
execution; "ask" auto-denies headless, so confirmations are routed
through the demo's approval cards instead):

  DENY-SECRET   protected credentials/config           - no approval path
  DENY-SELF     this enforcement layer itself          - no approval path
  DENY-FROZEN   operator-invariant immutable trees     - no approval path
                (reads/copies FROM them are fine; writes INTO them are not)
  CONFIRM       destructive / outside-workspace writes - a grant file
                matching the exact op-hash unlocks ONE retry
  ALLOW         everything else: routine reads, and routine writes /
                temp files / logs inside allowed roots (no prompts)

Allowed roots = the agent workspace, /tmp, and the formal job output
root. Deleting files inside those scratch areas is routine work;
deleting or force-changing anything else needs a human click.

Same-uid limitation (stated honestly): the agent runs as the same OS
user as this hook, so a deliberately malicious process could rewrite
the hook. This layer targets realistic failure modes - model mistakes,
misread instructions, ambiguous requests - not hostile code.

Protocol: stdin PreToolUse JSON -> stdout decision JSON, exit 0.
"""
import hashlib
import json
import os
import re
import sys
import time

WS = "/data1/neoag/agents/neoag_agent_ws"
APPROVALS = os.path.join(WS, ".neoag-approvals")
GRANT_TTL_SECONDS = 600

ALLOWED_ROOTS = (WS + "/", "/tmp/", "/var/tmp/",
                 "/data2/caojingqi/neoag_agent_runs/")

DENY_SECRET_RE = re.compile(
    r"(~/\.codebuddy/|/home/[^/\s'\"]*/\.codebuddy/|"
    r"\$HOME/\.(?:codebuddy|aws|ssh|config)|\$\{HOME\}/\.(?:codebuddy|aws|ssh|config)|"
    r"/root/\.(?:codebuddy|aws|ssh|config)|"
    r"/data1/neoag/secrets/|\bauth\.json\b|id_rsa|id_ed25519|"
    r"\.pem\b|\.key\b|/etc/shadow|gateway\.password)", re.I)
DENY_SELF_RE = re.compile(
    r"(gate\.py|post\.py|\.neoag-approvals\b|"
    r"agent_workspace/\.codebuddy/settings\.json)", re.I)
DENY_FROZEN_RE = re.compile(
    r"(/data2/caojingqi/original_OpenNeoAG|/data2/caojingqi/neoag_control|"
    r"/data2/caojingqi/mmod_20261104|/data1/neoag/install_logs|"
    r"/data1/neoag/agents/remote_access|/data1/neoag/agents/cloudflared|"
    r"/data1/neoag/agents/neoag-web-demo/data)(?![\w.-])", re.I)

DELETE_RE = re.compile(
    r"(?:^|[\s;&|(])(?:rm|rmdir|unlink|shred|srm)\b|"
    r"\bfind\b[^|;&]*\s-delete\b", re.I)
FORCE_RE = re.compile(
    r"(?:^|[\s;&|(])(?:sudo|mkfs(?:\.\w+)?|shutdown|reboot|halt)\b|"
    r"\bchmod\b[^|;&]*\s-[a-zA-Z]*R|\bchown\b[^|;&]*\s-[a-zA-Z]*R|"
    r"\bdd\b[^|;&]*\bof=|\btruncate\b|"
    r"\b(?:systemctl|service)\s|\b(?:kill|pkill|killall)\b|"
    r"\bgit\b[^|;&]*\bpush\b[^|;&]*(?:--force|-f)\b|"
    r"\bgit\b[^|;&]*\breset\s+--hard\b|\bgit\b[^|;&]*\bclean\b[^|;&]*-f",
    re.I)
COPY_MOVE_RE = re.compile(r"\b(?:cp|mv|rsync|ln)\b", re.I)
WRITE_BASH_RE = re.compile(
    r"\b(?:cp|mv|rsync|ln|touch|mkdir|tee)\b|\bsed\b[^|;&]*\s-i|"
    r"\bgit\b[^|;&]*\b(?:apply|checkout|restore)\b|>>?\s*[\"'~/.]", re.I)

MUT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Glob", "Grep", "LS", "list_dir", "NotebookRead"}
TOOL_PATH_KEYS = ("file_path", "notebook_path", "path", "file")
# path-like shell tokens: absolute (/... or ~/...), or relative tokens
# that contain a slash (captured WHOLE, so 'jobs/old.txt' never yields a
# bogus '/old.txt'). Bare filenames are not treated as paths.
PATH_TOKEN_RE = re.compile(
    r"(?<![\w])/[^\s'\"`;|&)<>]*|~[^\s'\"`;|&)<>]*"
    r"|[\w.-]*/[^\s'\"`;|&)<>]*")
REDIR_RE = re.compile(r">>?\s*([\"']?)(~?/[^\s'\"`;|&<>]+)\1")


def norm(path: str) -> str:
    p = os.path.expanduser(path)
    if not p.startswith("/"):
        p = os.path.join(WS, p)
    return os.path.normpath(p)


def in_allowed(path: str) -> bool:
    np = norm(path)
    return any(np == r.rstrip("/") or np.startswith(r)
               for r in ALLOWED_ROOTS)


def op_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:24]


def grant_valid(h: str) -> bool:
    """Single-use: consumed (unlinked) on first successful check."""
    try:
        f = os.path.join(APPROVALS, h + ".json")
        with open(f) as fh:
            g = json.load(fh)
        if time.time() - float(g.get("granted_at", 0)) > GRANT_TTL_SECONDS:
            try:
                os.unlink(f)
            except OSError:
                pass
            return False
        os.unlink(f)
        return True
    except Exception:
        return False


def emit(decision: str, reason: str, system_msg: str = ""):
    out = {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}
    if system_msg:
        out["systemMessage"] = system_msg
    print(json.dumps(out, ensure_ascii=False))


def confirm(detail: str, payload: dict):
    h = op_hash(payload)
    emit("deny",
         f"NEOAG_CONFIRM_REQUIRED op={h} :: {detail} This operation is "
         "classified as destructive or outside the authorized workspace "
         "by the NeoAG Web execution gate. If the user has NOT confirmed "
         "it yet: state (1) the exact target path, (2) what will change "
         "or be deleted, (3) reversibility, (4) backup status, (5) why "
         "this is necessary, and (6) a non-destructive alternative if "
         "one exists - then ask them to confirm; a confirmation card "
         "appears in the interface. After the user approves the card, "
         "retry the IDENTICAL call: the approval unlocks exactly this "
         "call once. Do not paraphrase the command; never self-approve.",
         system_msg=f"NeoAG gate: user confirmation required ({detail[:80]})")


def write_targets(cmd: str) -> list:
    """Paths a Bash command would WRITE. Redirection targets always;
    cp/mv/rsync/ln use their LAST path argument as destination; the
    other write verbs treat every path token as a target."""
    tgts = [m[1] for m in REDIR_RE.findall(cmd)]
    tokens = PATH_TOKEN_RE.findall(cmd)
    if COPY_MOVE_RE.search(cmd) and tokens:
        tgts.append(tokens[-1])
    if re.search(r"\b(?:touch|mkdir|tee)\b|\bsed\b[^|;&]*\s-i", cmd, re.I):
        tgts.extend(tokens)
    if re.search(r"\bgit\b[^|;&]*\b(?:apply|checkout|restore)\b", cmd, re.I):
        tgts.extend(tokens)      # conservative: git tree-mutating verbs
    return tgts


def decide(ev: dict):
    """Return None to allow, ("deny", reason, user_msg), or
    ("confirm", detail, op_payload)."""
    tool = str(ev.get("tool_name") or "")
    ti = ev.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    cmd = str(ti.get("command") or "")
    paths = [str(ti[k]) for k in TOOL_PATH_KEYS if ti.get(k)]
    text_all = cmd + " " + " ".join(paths)

    # secrets are unreadable and unwritable, no approval path
    if DENY_SECRET_RE.search(text_all):
        return ("deny",
                "Blocked by the NeoAG Web gate: this touches credential "
                "or agent-configuration paths the application must never "
                "read or write. There is no approval path. Do not retry; "
                "tell the user the operation is not permitted by design.",
                "NeoAG gate: protected credential/config path")
    if DENY_SELF_RE.search(text_all):
        return ("deny",
                "Blocked by the NeoAG Web gate: the approval mechanism "
                "and its files are self-protected; the agent can never "
                "grant itself an approval. Request user confirmation "
                "through the confirmation card instead.",
                "NeoAG gate: approval mechanism is self-protected")

    if tool in READ_TOOLS:
        return None

    if tool == "Bash" and cmd:
        dele = bool(DELETE_RE.search(cmd))
        force = bool(FORCE_RE.search(cmd))
        mut = bool(WRITE_BASH_RE.search(cmd))
        if not (dele or force or mut):
            return None                    # read-only diagnostics
        wt = write_targets(cmd)
        # frozen trees: never a write destination (reads/copies-out OK)
        if any(DENY_FROZEN_RE.search(norm(p)) for p in wt):
            return ("deny",
                    "Blocked by the NeoAG Web gate: frozen pipeline "
                    "trees, control copies, report mirrors and "
                    "infrastructure directories are immutable by "
                    "operator invariant - no approval path exists. "
                    "Working on a copy inside the authorized workspace "
                    "is fine and needs no approval.",
                    "NeoAG gate: immutable-by-invariant write target")
        if dele:
            bad = [p for p in PATH_TOKEN_RE.findall(cmd)
                   if not in_allowed(p)]
            if bad:
                return ("confirm",
                        "Bash command deletes paths outside the "
                        "authorized job workspace.",
                        {"tool": "Bash", "command": cmd})
        if force:
            return ("confirm",
                    "Bash command performs a force/system-level change "
                    "(recursive permission change, process kill, git "
                    "force/clean/hard-reset, disk image, or service "
                    "control).", {"tool": "Bash", "command": cmd})
        if mut and any(not in_allowed(p) for p in wt):
            return ("confirm",
                    "Bash command writes, overwrites or moves files "
                    "into a location outside the authorized job "
                    "workspace.", {"tool": "Bash", "command": cmd})
        return None                        # routine workspace mutation

    if tool in MUT_TOOLS:
        bad = [p for p in paths if not in_allowed(p)]
        if bad:
            return ("confirm",
                    f"Tool {tool} writes outside the authorized job "
                    f"workspace ({bad[0]}).",
                    {"tool": tool, "file_path": bad[0]})
        return None
    return None


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return
    d = decide(ev)
    if d is None:
        return                              # allow (default-mode grant)
    if d[0] == "deny":                      # ("deny", reason, user_msg)
        emit("deny", d[1], d[2])
        return
    detail, payload = d[1], d[2]            # ("confirm", detail, payload)
    h = op_hash(payload)
    if grant_valid(h):
        emit("allow", detail,
             system_msg=f"NeoAG gate: one-time user approval applied "
                        f"({detail[:80]})")
        return
    confirm(detail, payload)


if __name__ == "__main__":
    main()
