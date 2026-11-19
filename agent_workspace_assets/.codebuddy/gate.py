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
  DENY-FORMAL   launching a FORMAL NeoAG analysis outside the unified
                job system (open-neo run --mode execute, neoag.cli
                run/run-full/run-demo, direct gateway /open/run posts,
                ad-hoc gateway launches)  - no approval path: the job
                must be REGISTERED instead, via
                .neoag/taskquery.py job submit, so approval, snapshot,
                artifact ledger and outcome taxonomy all apply. Pure
                planning/inspection (open-neo run --mode plan, doctor,
                --help, reads) stays allowed. Each interception is
                audit-logged to .neoag-approvals/routing.jsonl.
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

# FORMAL-JOB routing (Milestone 5 PART 1: enforceable, not skill prose).
# These patterns launch NeoAG's real execution engine straight from the
# agent's shell, which would bypass the unified job system (approval
# gate, run snapshot, artifact ledger, outcome taxonomy, Task History).
# The sanctioned path registers the same work through the task API:
#   python3 <workspace>/.neoag/taskquery.py job submit '<json>'
# Pure planning/inspection (open-neo run --mode plan / doctor / --help)
# is deliberately NOT matched: it executes nothing (verified against
# the deployed planner: plan mode writes a plan + input QC only).
FORMAL_RUN_PATTERNS = (
    # the lookahead (CLI flags follow the subcommand) keeps prose/docs
    # greps like `grep 'open-neo run' f.md` out, while quoted wrappers
    # (bash -c "open-neo run --mode ...") still match - no cheap bypass.
    (re.compile(r"\bopen-neo\s+run\b(?=[\s;&|]+--|[\s;&|]*$)", re.I),
     "open-neo run"),
    (re.compile(r"-m\s+neoag\.open_neo\.cli\s+run\b", re.I),
     "neoag.open_neo.cli run"),
    (re.compile(r"-m\s+neoag\.cli\s+(?:run|run-full|run-demo|"
                r"sv-run-full\S*)\b", re.I), "neoag.cli run*"),
    (re.compile(r"-m\s+neoag\.pipeline\b", re.I), "neoag.pipeline import"),
    (re.compile(r"(?:curl|wget)\b[^|;&]*/open/run\b|"
                r"/open/run\b[^|;&]*(?:curl|wget)", re.I),
     "direct gateway /open/run post"),
    (re.compile(r"neoag\.controlled_execution\.gateway\b", re.I),
     "ad-hoc gateway launch"),
)
# planning/inspection escape hatch - only meaningful for the CLI
# entries above; `--mode plan` executes nothing.
PLAN_MODE_RE = re.compile(r"--mode[\s=]+plan\b", re.I)

# DATASET CONSENT (Milestone 5 PART 3, consent-enforced). Reading
# literature stays free; pulling a DATASET is an explicit resource
# event. A download-class command that targets dataset sources asks
# once through a structured confirmation card (dataset, operation,
# resource scope, destination); the approval ALSO writes a bounded
# scope grant (dataset + destination, TTL-capped) so a multi-file
# download never degrades into per-file prompting. Blanket "allow all
# downloads" does not exist: every scope names its destination.
# Known limitation: a bespoke python script that downloads internally
# is not classifiable from its command line (same-uid honesty stated
# in the header); command-line download tools are covered.
DOWNLOAD_TOOL_RE = re.compile(
    r"(?:^|[\s;&|(])(?:curl|wget|aria2c|gdown|axel|rsync|scp|aws\s+s3\s+"
    r"(?:cp|sync)|gsutil\s+(?:cp|rsync))\b", re.I)
PAPER_READ_RE = re.compile(
    r"(arxiv\.org|biorxiv|medrxiv|doi\.org|pubmed|ncbi\.nlm\.nih\.gov|"
    r"europepmc|nature\.com|science\.org|cell\.com|springer|"
    r"acm\.org|ieee\.org|openreview|aclanthology|pnas|jmir|jacionet)", re.I)
DATASET_SOURCE_RE = re.compile(
    r"(ftp\.ncbi\.nlm\.nih\.gov|trace\.ncbi\.nlm\.nih\.gov|sra\.|"
    r"dbgap|dbGaP|ega\.eu-c|cancer\.sanger|gdac\.|gdc\.cancer\.gov|"
    r"portal\.gdc|cancerstorage\.nci\.tcga|tcga|"
    r"geo\.ncbi|10xgenomics\.com|cf\.10xgenomics|cbioportal|"
    r"hgdownload[.\w]*\.ucsc\.edu|refdata\.genome\.ucsc|"
    r"ftp\.ensembl|download\.ensembl|refseq\.|genome\.ucsc|"
    r"zenodo\.org|figshare|huggingface\.co|hf-mirror|"
    r"hcb\.illumina\.com|icgc\.or\.kr|gtexportal|opengap)", re.I)
DATASET_EXT_RE = re.compile(
    r"\.(fastq|fq|fasta|fa|fna|vcf|bcf|bam|cram|sra|maf|bed.gz|gtf|"
    r"counts|tpm|h5|hd5|10x|rds|rdata|npy|npz|pkl|h5ad)(?:\.(?:gz|bz2|"
    r"zst|tar))?(?=$|[\s\"'])|\bdataset[s]?[/_.]|/references/", re.I)
SCOPE_TTL_SECONDS = 72 * 3600


def _src_key(u: str) -> str:
    """Source family key: host + first path segments. Approving one
    zenodo record's file does NOT match a different record, but does
    cover every file of the SAME record - the multi-file download
    stays one consented scope instead of per-file prompting."""
    u = re.sub(r"^https?://", "", (u or "")).strip("/")
    return "/".join(u.split("/")[:3])


def dataset_scope_ok(urlish: str, dest: str) -> str:
    """Bounded scope grant lookup: returns the approval id of a live
    (source family, destination root) scope, else ''. TTL-capped,
    destination-naming - never a blanket download permit."""
    try:
        with open(os.path.join(APPROVALS, "dataset-scope.json")) as fh:
            scopes = json.load(fh)
    except Exception:
        return ""
    np = norm(dest or "")
    for sc in scopes if isinstance(scopes, list) else []:
        try:
            src = str(sc.get("dataset") or "")
            dst = str(sc.get("destination_root")
                      or sc.get("destination") or "")
            exp = float(sc.get("expires", 0))
        except Exception:
            continue
        if not src or not dst or time.time() > exp:
            continue
        sk, pk = _src_key(src), _src_key(urlish or "")
        nd = norm(dst)
        dest_ok = np == nd or np.startswith(nd.rstrip("/") + "/")
        if (sk and (sk == pk or sk in pk or pk in sk)) and dest_ok:
            return str(sc.get("approval_id") or "scoped")
    return ""


def dataset_intent(cmd: str):
    """Return (source_label, destination, why) when the command is a
    dataset download; None for literature reads and plain diagnostics."""
    if not DOWNLOAD_TOOL_RE.search(cmd):
        return None
    if PAPER_READ_RE.search(cmd) and not DATASET_EXT_RE.search(cmd):
        return None                       # reading a paper is not a dataset
    if not (DATASET_SOURCE_RE.search(cmd) or DATASET_EXT_RE.search(cmd)):
        return None                       # ordinary small fetch/diagnostic
    urls = re.findall(r"https?://[^\s'\"]+", cmd)
    src = urls[0] if urls else ""
    dest = ""
    m = re.search(r"(?:-o|--output-document|--output-dir|-P|-O)\s+(\S+)",
                  cmd)
    if m:
        dest = m.group(1).strip("\"'")
    if not dest:
        # output path may be a -P dir or trailing path token
        m2 = REDIR_RE.findall(cmd)
        if m2:
            dest = m2[0][1]
        else:
            toks = [t for t in PATH_TOKEN_RE.findall(cmd) if "/" in t
                    and not t.startswith("http")]
            dest = toks[-1] if toks else os.getcwd()
    label = src or DATASET_SOURCE_RE.search(cmd).group(0)
    return label, dest, ("known dataset source"
                         if DATASET_SOURCE_RE.search(cmd)
                         else "dataset-format file/destination")
READ_TOOLS = {"Read", "Glob", "Grep", "LS", "list_dir", "NotebookRead"}
TOOL_PATH_KEYS = ("file_path", "notebook_path", "path", "file")
# path-like shell tokens: absolute (/... or ~/...), or relative tokens
# that contain a slash (captured WHOLE, so 'jobs/old.txt' never yields a
# bogus '/old.txt'). Bare filenames are not treated as paths.
PATH_TOKEN_RE = re.compile(
    r"(?<![\w])/[^\s'\"`;|&)<>]*|~[^\s'\"`;|&)<>]*"
    r"|[\w.-]*/[^\s'\"`;|&)<>]*")
REDIR_RE = re.compile(r">>?\s*([\"']?)(~?/[^\s'\"`;|&<>]+)\1")

# M11 P0-2: file-descriptor plumbing is NOT data writing.
# `ls /path 2>/dev/null`, `cmd 2>&1`, `cmd >&2`, `cmd &>/dev/null` are
# read-only diagnostics wired to /dev/null or between descriptors; they
# used to match the redirect-write pattern and raise a DESTRUCTIVE
# confirmation for pure reads. Only these two exact shapes are removed,
# and only from the write-classification view (the original command
# stays the approval identity):
#   - any `N>`, `N>>`, `&>` / `&>>` whose target is exactly /dev/null
#   - fd dups: `N>&M`, `N>&>M` (e.g. 2>&1, >&2, 1>&0)
# A redirect to any other path (`2>err.log`, `&>out.txt`) keeps
# counting as a write exactly as before - nothing else is weakened.
FD_NULL_REDIRECT_RE = re.compile(
    r"(?:^|[\s;&|(])(?:\d*|&)\s*>{1,2}\s*[\"']?/dev/null\b[\"']?")
FD_DUP_REDIRECT_RE = re.compile(
    r"(?:^|[\s;&|(])(?:\d+|&)\s*>\s*&>?\s*\d+\b")


def strip_fd_redirects(cmd: str) -> str:
    """Command text with fd plumbing removed, for WRITE CLASSIFICATION
    only. Redirects to real files are untouched."""
    cmd = FD_NULL_REDIRECT_RE.sub(" ", cmd)
    cmd = FD_DUP_REDIRECT_RE.sub(" ", cmd)
    return cmd


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
        for pat, label in FORMAL_RUN_PATTERNS:
            if pat.search(cmd):
                if label in ("open-neo run", "neoag.open_neo.cli run") \
                        and PLAN_MODE_RE.search(cmd):
                    continue      # planning/inspection: executes nothing
                return ("deny",
                        f"NEOAG_FORMAL_JOB_REQUIRED :: this command "
                        f"launches a formal NeoAG analysis ({label}) "
                        "outside the unified job system. Formal runs "
                        "MUST be registered through the task API so "
                        "approval, the run snapshot, the artifact "
                        "ledger and the outcome taxonomy all apply: "
                        "run python3 .neoag/taskquery.py job submit "
                        "'<json body>' (GET details with taskquery.py "
                        "job status <id>). The registration appears "
                        "as the same task in the interface and starts "
                        "only after the human approval card. There is "
                        "no approval path for raw launches and you "
                        "must not paraphrase around this rule. Pure "
                        "planning is fine: open-neo run --mode plan.",
                        "NeoAG gate: formal run must go through the "
                        "task API (job submit)")
        ds = dataset_intent(cmd)
        if ds:
            label, dest, why = ds
            if dataset_scope_ok(label, dest):
                return None    # inside an approved bounded download scope
            return ("confirm",
                    "DATASET DOWNLOAD requires explicit consent :: "
                    f"dataset={label[:160]} | operation=download | "
                    f"reason={why} | destination={dest} | resource "
                    "scope=THIS source into THIS destination only, 72h. "
                    "Approving lets further files from the same source "
                    "into the same destination through without asking "
                    "again; it NEVER allows downloads anywhere else.",
                    {"tool": "Bash", "command": cmd,
                     "class": "dataset_download", "dataset": label,
                     "operation": "download", "destination": dest})
        # write CLASSIFICATION runs on the fd-plumbing-stripped command
        # (strip_fd_redirects: 2>/dev/null / 2>&1 are reads, not writes);
        # dataset detection and deny checks above stay on the RAW cmd
        cmd_w = strip_fd_redirects(cmd)
        dele = bool(DELETE_RE.search(cmd_w))
        force = bool(FORCE_RE.search(cmd_w))
        mut = bool(WRITE_BASH_RE.search(cmd_w))
        if not (dele or force or mut):
            return None                    # read-only diagnostics
        wt = write_targets(cmd_w)
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
            bad = [p for p in PATH_TOKEN_RE.findall(cmd_w)
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
        if "NEOAG_FORMAL_JOB_REQUIRED" in d[1]:
            # auditable classification: every routing interception is
            # logged (self-protected dir) for the review reports.
            try:
                os.makedirs(APPROVALS, exist_ok=True)
                ti = ev.get("tool_input") or {}
                with open(os.path.join(APPROVALS, "routing.jsonl"),
                          "a") as fh:
                    fh.write(json.dumps({
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "class": "FORMAL_JOB",
                        "reason": d[1].split("::")[-1][:200],
                        "command": str(ti.get("command") or "")[:400],
                        "session_id": ev.get("session_id") or "",
                    }, ensure_ascii=False) + "\n")
            except OSError:
                pass
        emit("deny", d[1], d[2])
        return
    detail, payload = d[1], d[2]            # ("confirm", detail, payload)
    h = op_hash(payload)
    if grant_valid(h):
        emit("allow", detail,
             system_msg=f"NeoAG gate: one-time user approval applied "
                        f"({detail[:80]})")
        return
    if payload.get("class") == "dataset_download":
        # structured consent card: the backend reads this sidecar to
        # show dataset / operation / scope / destination fields, and to
        # write the BOUNDED scope grant on approval.
        try:
            os.makedirs(os.path.join(APPROVALS, "pending"), exist_ok=True)
            fn = os.path.join(APPROVALS, "pending", h + ".json")
            with open(fn, "w") as fh:
                json.dump(payload, fh)
            os.chmod(fn, 0o600)
        except OSError:
            pass
    confirm(detail, payload)


if __name__ == "__main__":
    main()
