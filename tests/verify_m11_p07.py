#!/usr/bin/env python3
"""M11 P0-7 battery: input discovery truth + typed blockers under the
EFFECTIVE workflow.

Pinned behaviour (all measured, none asserted from memory):
  * discovery over the REAL read-only CXLMINI case
    (/data2/caojingqi/neoag_agent_runs/cxl_e2e_mini_20261119/inputs)
    returns role + provenance per file; VCF/table/HLA roles are
    content-based, BAM roles state honestly whether the file's own
    header (BGZF @RG samples) or only the filename token established
    them.
  * "missing" is never one undifferentiated pile: blockers carry one
    kind from {missing_patient_input, missing_server_reference,
    ambiguous, unsupported_format, missing_tool,
    unselected_stage_dependency}, evaluated under the workflow the
    interface actually runs (DNA-SV branch OFF by default since
    P0-6). With the branch off, the sv route's reference_fasta /
    gencode_gtf become ADVISORIES, not blockers; ticking the branch
    makes them real missing_server_reference blockers again.
  * a human pick among the somatic VCF's genotype columns is honored
    in the confirmed manifest (names validated against the VCF - the
    provenance authority); a bogus sample name is refused.
  * the raw native view (ready/missing/routes) is preserved verbatim
    for audit and battery compatibility.
Run: python3 tests/verify_m11_p07.py   (isolated :8786)
"""
import atexit
import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import types
import urllib.request
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 8786
CASE = "/data2/caojingqi/neoag_agent_runs/cxl_e2e_mini_20261119/inputs"
# `or` not a get-default: the variable exists as EMPTY string here,
# and an empty value means "use the deployed default", not "no tool"
SAMTOOLS = os.environ.get("NEOAG_SAMTOOLS") or \
    "/data1/neoag/miniforge3/envs/neoag-tools/bin/samtools"
FIX = pathlib.Path("/tmp/p07_cases")
KNOWN_KINDS = {"missing_patient_input", "missing_server_reference",
               "ambiguous", "unsupported_format", "missing_tool",
               "unselected_stage_dependency", "environment",
               "invalid_config", "not_authorized", "planner_verdict"}
PASS, FAIL = [], []

os.environ["NEOAG_INPUT_ROOTS"] = (
    f"{FIX}:{CASE.rsplit('/', 1)[0].rsplit('/', 1)[0]}:/data1/neoag")

with open(ROOT / "data" / "auth.json") as f:
    _a = json.load(f)
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_a['user']}:{_a['password']}".encode()).decode()

sys.path.insert(0, str(ROOT))
_stub = types.ModuleType("backend.server")
_stub.publish = lambda ev: None
sys.modules.setdefault("backend.server", _stub)
from backend import native, readiness  # noqa: E402


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS" if ok else "FAIL") + f"  {name}"
          + (f"  [{detail}]" if detail and not ok else ""))


def w(p: pathlib.Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def bam(path: pathlib.Path, sm: str = "") -> bool:
    """header-only BGZF BAM through the deployed samtools (byte-honest
    container so the stdlib BGZF reader in _SCAN_SCRIPT can parse it).
    Returns False when samtools is absent (caller degrades honestly)."""
    head = "@HD\tVN:1.6\tSO:coordinate\n"
    if sm:
        head += f"@RG\tID:1\tSM:{sm}\n"
    r = subprocess.run([SAMTOOLS, "view", "-b", "-o", str(path), "-"],
                       input=head.encode(), capture_output=True,
                       timeout=60)
    return r.returncode == 0


# genotype-column sample names carry the tumor/normal evidence (the
# honest pair-read), so the svplus fixture isolates ONE story: the
# sv branch's server-reference advisories, nothing else
_VCF = ("##fileformat=VCFv4.2\n"
        "##contig=<ID=chr1,length=248956422>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t"
        "P07TUM_A\tP07NOR_B\n"
        "chr1\t1234\t.\tA\tG\t99\tPASS\tDP=42\tGT:DP\t0/1:21\t0/0:20\n")
_SVCF = ("##fileformat=VCFv4.2\n"
         '##INFO=<ID=SVTYPE,Number=1,Type=String,Description="type">\n'
         "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t"
         "P07TUM_A\tP07NOR_B\n"
         "chr1\t999\t.\tN\t<DEL>\t99\tPASS\tSVTYPE=DEL\tGT\t0/1\t0/0\n")
_FUS = "Gene1\tGene2\tScore\tSplitReads\tDiscordantPairs\nEML4\tALK\t0.9\t12\t4\n"
_HLA = ("sample\tgene\tallele_1\tallele_2\n"
        "P07\tA\tHLA-A*02:01\tHLA-A*24:02\n"
        "P07\tB\tHLA-B*07:02\tHLA-B*39:06\n")

# ================= fixtures ==========================================
HAVE_SAMTOOLS = os.path.exists(SAMTOOLS)
FIX.mkdir(parents=True, exist_ok=True)
# svplus: RNA routes fine; DNA-SV inputs present so the OFF-by-default
# branch turns the route's server-reference gaps into advisories
w(FIX / "svplus" / "sv_calls.vcf", _SVCF)
w(FIX / "svplus" / "fusion_calls.tsv", _FUS)
w(FIX / "svplus" / "hla_type.txt", _HLA)
w(FIX / "svplus" / "somatic_calls.vcf", _VCF)
# nohla: only a somatic VCF - the HLA result is genuine patient data
w(FIX / "nohla" / "only_somatic.vcf", _VCF)
# emptyvcf: present for the VCF role but unusable (zero bytes)
(FIX / "emptyvcf").mkdir(parents=True, exist_ok=True)
(FIX / "emptyvcf" / "zero.vcf").write_bytes(b"")
# bamhdr: BAMs whose OWN header declares tumor/normal samples
(FIX / "bamhdr").mkdir(parents=True, exist_ok=True)
ok_bams = (HAVE_SAMTOOLS
           and bam(FIX / "bamhdr" / "tumor_a.bam", "P07TUMOUR_A")
           and bam(FIX / "bamhdr" / "normal_b.bam", "P07NORMAL_B"))
if ok_bams:
    w(FIX / "bamhdr" / "somatic_calls.vcf", _VCF)
    w(FIX / "bamhdr" / "hla_type.txt", _HLA)

# ================= server ============================================
srv = subprocess.Popen(
    [sys.executable, "-u", "-m", "backend.server"], cwd=str(ROOT),
    env=dict(os.environ, NEOAG_DEMO_DB="m11p07.sqlite3",
             NEOAG_DEMO_PORT=str(PORT),
             NEOAG_SKILLS_DIR="/tmp/neoag-m11p07-skills",
             NEOAG_TASKQ_DIR="/tmp/neoag-m11p07-taskq",
             NEOAG_GATE_TOKEN_DISABLE="1"),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
_made_manifests = []


def _cleanup():
    srv.terminate()
    try:
        srv.wait(timeout=8)
    except Exception:
        srv.kill()
    for f in _made_manifests:
        try:
            os.unlink(f)
        except OSError:
            pass
    try:
        (ROOT / "data" / "m11p07.sqlite3").unlink()
    except OSError:
        pass
    shutil.rmtree(ROOT / "data" / "exports-m11p07", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p07-skills", ignore_errors=True)
    shutil.rmtree("/tmp/neoag-m11p07-taskq", ignore_errors=True)
    shutil.rmtree(FIX, ignore_errors=True)


atexit.register(_cleanup)


def api(method, path, body=None, timeout=120):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method, headers={"Authorization": AUTH_HEADER})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


up = False
for _ in range(40):
    try:
        s, _ = api("GET", "/api/bootstrap")
        up = s == 200
        if up:
            break
    except Exception:
        time.sleep(0.5)
check("1a server boots (isolated :8786, input roots extended to the "
      "read-only case and /tmp fixtures)", up)

# ================= 2. REAL read-only case ============================
s2, disc = api("POST", "/api/native/discover", {"data_dir": CASE})
rows = disc.get("inputs") or []
byflag = {i["flag"]: i for i in rows}
ready_routes = {r["route"] for r in disc.get("routes") or []
                if r.get("status") == "READY"}
wf = disc.get("workflow") or {}
check("2a discovery over the REAL CXLMINI case (read-only) returns the "
      "native roles and its routes - fusion and splice READY",
      s2 == 200 and disc.get("ok") and len(rows) >= 5
      and {"fusion", "splice"} <= ready_routes,
      f"{s2} routes={[(r['route'], r['status']) for r in disc.get('routes') or []]}")
check("2b every recognized file carries HOW its role was established; "
      "VCF/table roles are content-based; BAM rows honestly say whether "
      "the file header or only the filename decided (real data)",
      rows and all(i.get("basis") in ("content", "filename token")
                   for i in rows)
      and all(byflag[f]["basis"] == "content"
              for f in ("somatic_vcf", "fusion_tsv") if f in byflag)
      and all("BAM header" in byflag[f].get("basis_detail", "")
              or "filename" in byflag[f].get("basis_detail", "")
              for f in ("tumor_dna_bam", "normal_dna_bam",
                        "tumor_rna_bam") if f in byflag),
      str({i["flag"]: i["basis"] for i in rows}))
_svoff = [b for b in wf.get("blockers") or []
          if b["kind"] == "missing_server_reference"]
_adv = wf.get("advisories") or []
check("2c under the EFFECTIVE (DNA-SV OFF by default) workflow the sv "
      "route's server-reference gaps are ADVISORIES, never blockers - "
      "the old UI blocked confirming this real case on them",
      not _svoff
      and {"reference_fasta", "gencode_gtf", "dna_sv"}
      <= {a["field"] for a in _adv}
      and all(a["kind"] == "unselected_stage_dependency" for a in _adv),
      f"blockers={wf.get('blockers')} advisories={_adv}")
check("2d the raw native view is untouched for audit (ready/missing/"
      "routes verbatim) and every typed blocker uses a known kind",
      all(k in disc for k in ("ready", "missing", "routes",
                              "ambiguous", "summary"))
      and all(b["kind"] in KNOWN_KINDS
              for b in (wf.get("blockers") or []) + _adv)
      and wf.get("note"),
      sorted(disc)[:12])
check("2e real case: with sample names that carry no tumor/normal "
      "tokens the pair is NOT invented - it returns as a structured "
      "ambiguous choice over the VCF's real genotype columns",
      any(a.get("field") == "tumor_sample_id"
          and len(a.get("candidates") or []) == 2
          for a in disc.get("ambiguous") or [])
      and any(b.get("kind") == "ambiguous"
              and b.get("field") == "tumor_sample_id"
              for b in wf.get("blockers") or []),
      str(disc.get("ambiguous"))[:200])

# ---- 2f human pick honored (provenance-validated) --------------------
_cands = next((a["candidates"] for a in disc.get("ambiguous") or []
               if a.get("field") == "tumor_sample_id"), [])
if _cands:
    s3, man = api("POST", "/api/native/manifest",
                  {"data_dir": CASE, "sample_id": "P07PICK",
                   "picked": {"tumor_sample_id": _cands[0]}})
    mp = ((man.get("proposal") or {}).get("manifest_path")) or ""
    if mp:
        _made_manifests.append(mp)
    mdata = json.loads(pathlib.Path(mp).read_text()) if mp else {}
    check("2f the human's tumor pick is VALIDATED against the VCF "
          "genotype columns and lands in the confirmed manifest (the "
          "other column becomes normal) - never silently dropped",
          s3 == 201 and mdata.get("tumor_sample_id") == _cands[0]
          and mdata.get("normal_sample_id") == _cands[1]
          and mdata.get("inputs", {}).get("tumor_sample_id")
          == _cands[0],
          f"{s3} tumor={mdata.get('tumor_sample_id')}")
    s3b, err = api("POST", "/api/native/manifest",
                   {"data_dir": CASE, "sample_id": "P07BAD",
                    "picked": {"tumor_sample_id": "NOT_A_REAL_SAMPLE"}})
    check("2b2 a sample pick that is NOT one of the VCF genotype "
          "columns is refused (the VCF is the sample authority; no "
          "free-text identity entry)",
          s3b == 400 and "genotype" in str(err), f"{s3b} {str(err)[:120]}")
else:
    check("2f human pick honored", False, "no tumor_sample_id ambiguity")
    check("2b2 bogus sample pick refused", False, "no candidates")

# ================= 3. content, not filename ==========================
junk = FIX / "junk"
junk.mkdir(parents=True, exist_ok=True)
shutil.copyfile(byflag["somatic_vcf"]["path"], junk / "zzz_qq9.vcf")
shutil.copyfile(byflag["fusion_tsv"]["path"], junk / "aaa_123.tsv")
if HAVE_SAMTOOLS:
    bam(junk / "q7k9x.bam")
s4, dj = api("POST", "/api/native/discover", {"data_dir": str(junk)})
jflags = {i["flag"] for i in dj.get("inputs") or []}
check("3a the same files UNDER GARBAGE NAMES keep their roles - "
      "classification is content (VCF header / table headers), not "
      "filename matching (proven on copies of the real case's files)",
      s4 == 200 and {"somatic_vcf", "fusion_tsv"} <= jflags,
      f"{s4} {jflags}")
check("3b honest limitation stated: a BAM with NO name tokens and no "
      "sample declaration is left UNCLASSIFIED rather than assigned a "
      "role by guess (it appears as unmatched, not as a role)",
      "tumor_dna_bam" not in jflags and any(
          u.endswith("q7k9x.bam") for u in dj.get("unmatched") or [])
      if HAVE_SAMTOOLS else True,
      f"flags={jflags} unmatched={dj.get('unmatched')}")
if ok_bams:
    s4b, db = api("POST", "/api/native/discover",
                  {"data_dir": str(FIX / "bamhdr")})
    bmap = {i["flag"]: i for i in db.get("inputs") or []}
    check("3c when the BAM's OWN header declares the samples, the "
          "tumor/normal roles become content-based and the evidence "
          "(@RG SM names) is shown (stdlib BGZF read, no tool run)",
          s4b == 200
          and bmap.get("tumor_dna_bam", {}).get("basis") == "content"
          and "P07TUMOUR_A" in bmap.get("tumor_dna_bam", {}).get(
              "basis_detail", "")
          and "P07NORMAL_B" in bmap.get("normal_dna_bam", {}).get(
              "basis_detail", ""),
          str({k: (v.get("basis"), v.get("basis_detail", "")[:60])
               for k, v in bmap.items()})[:200])
else:
    check("3c BAM header content evidence", False,
          "samtools unavailable - header-bearing fixtures skipped")

# ================= 4. typed blockers, effective workflow =============
s5, dsv = api("POST", "/api/native/discover",
              {"data_dir": str(FIX / "svplus")})
wfd = dsv.get("workflow") or {}
check("4a DNA-SV branch OFF (the interface default): an sv route "
      "blocked ONLY on server reference assets yields ZERO blockers "
      "and typed advisories - the workflow actually selected can run",
      s5 == 200 and wfd.get("ready") is True
      and {a["field"] for a in wfd.get("advisories") or []}
      >= {"reference_fasta", "gencode_gtf", "dna_sv"},
      f"{s5} wf={json.dumps(wfd)[:220]}")
wf_on = readiness.workflow_assessment(dsv, {"skip_dna_sv": False})
check("4b the SAME discovery assessed with the branch ticked ON: those "
      "assets become real missing_server_reference blockers - the "
      "classification follows the EFFECTIVE workflow in both "
      "directions (nothing weakened, nothing hidden)",
      wf_on["ready"] is False
      and {"reference_fasta", "gencode_gtf"}
      <= {b["field"] for b in wf_on["blockers"]
          if b["kind"] == "missing_server_reference"}
      and not [a for a in wf_on["advisories"]
               if a["field"] in ("reference_fasta", "gencode_gtf")],
      json.dumps(wf_on["blockers"])[:220])
s5c, dnh = api("POST", "/api/native/discover",
               {"data_dir": str(FIX / "nohla")})
wfn = dnh.get("workflow") or {}
check("4c a route that needs an HLA typing result the case does not "
      "carry is a missing_patient_input blocker (the user's to fix), "
      "kept apart from any server-side kind",
      s5c == 200 and not wfn.get("ready")
      and any(b["kind"] == "missing_patient_input"
              and b["field"] == "hla_alleles_or_hla_file"
              for b in wfn.get("blockers") or []),
      json.dumps(wfn.get("blockers"))[:200])
s5d, dev = api("POST", "/api/native/discover",
               {"data_dir": str(FIX / "emptyvcf")})
wfe = dev.get("workflow") or {}
check("4d a zero-byte file presented for the VCF role is an "
      "unsupported_format blocker (present-but-unusable), not a "
      "silent skip and not a fake OK",
      s5d == 200 and any(b["kind"] == "unsupported_format"
                         and b["field"] == "somatic_vcf"
                         for b in wfe.get("blockers") or []),
      json.dumps(wfe.get("blockers"))[:200])

# ================= 5. readiness endpoint kinds =======================
s6, rd = api("POST", "/api/jobs/native/readiness",
             {"synthetic_demo": True})
checks6 = rd.get("checks") or []
check("5a every readiness check carries a kind from the declared "
      "vocabulary; runtime/tool rows say missing_tool, so a user can "
      "tell WHO must fix it (server admin vs operator vs themselves)",
      s6 == 200 and checks6
      and all(c.get("kind") in KNOWN_KINDS for c in checks6)
      and any(c["id"] == "native_runtime" and c["kind"] == "missing_tool"
              for c in checks6),
      f"{s6} {str([(c['id'], c.get('kind')) for c in checks6])[:200]}")

# ================= 6. frontend contracts =============================
appjs = (ROOT / "frontend" / "app.js").read_text()
css = (ROOT / "frontend" / "styles.css").read_text()
check("6a the confirm gate and the red list are driven by the typed "
      "assessment (hardBlocks + unresolved picks), never the raw "
      "missing pile; the raw native view stays available under the "
      "technical view",
      "hardBlocks" in appjs and "const blocked = hardBlocks.length"
      in appjs and "native raw missing (pre-workflow view)" in appjs
      and 'missing.length ? "disabled"' not in appjs)
check("6b role rows show the basis chip (content vs filename "
      "heuristic) with the evidence in the tooltip",
      "basisChip" in appjs and 'class="rr-basis' in appjs
      and "i.basis_detail" in appjs)
check("6c readiness rows carry the blocker-kind chip", "kindLabel" in
      appjs and 'class="rk"' in appjs)
_keys = ["inv.advisories", "kind.missing_patient_input",
         "kind.missing_server_reference", "kind.ambiguous",
         "kind.unsupported_format", "kind.missing_tool",
         "kind.unselected_stage_dependency", "kind.invalid_config",
         "kind.not_authorized", "kind.planner_verdict",
         "kind.environment", "basis.content", "basis.filename"]
check("6d i18n: EVERY new kind/basis string exists in BOTH "
      "dictionaries (no untranslated jargon for either language)",
      all(appjs.count(f'"{k}"') >= 2 for k in _keys),
      str([k for k in _keys if appjs.count(f'"{k}"') < 2]))
check("6e the two new chips have real CSS (they are UI, not debug "
      "output)", ".rr-basis" in css and ".rdy-row .rk" in css)

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
