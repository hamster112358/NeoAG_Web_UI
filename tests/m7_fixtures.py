#!/usr/bin/env python3
"""Reproducible M7 input fixtures for the discovery/manifest journeys.

Both verification suites (verify_battery.py, verify_m7_acceptance.py)
exercise the REAL NeoAG scanner, inventory and planner over input
directories that must live under an authorized input root
(/data1/neoag - see backend/native.py AUTHORIZED_INPUT_ROOTS). This
module (re)creates those directories from inline content so any run is
self-contained: the tables are genuine text files the native inventory
parses for real; the BAMs are written with the native environment's
samtools when present (a real BGZF container + BAI index, byte-honest
for the planner) and fall back to header-only stand-ins otherwise
(enough for the stat-only discovery inventory).

Nothing here touches patient data: every file is synthetic.
"""
import os
import pathlib
import subprocess

AGENT_WS = pathlib.Path(os.environ.get(
    "NEOAG_AGENT_WS", "/data1/neoag/agents/neoag_agent_ws"))
CLEAN = AGENT_WS / "test_data"
AMBIG = AGENT_WS / "test_data_ambiguous"
SAMTOOLS = os.environ.get(
    "NEOAG_SAMTOOLS", "/data1/neoag/miniforge3/envs/neoag-tools/bin/samtools")

# A somatic VCF whose GENOTYPE COLUMNS are the tumor/normal evidence:
# the discovery layer must read the pair from here, not from filenames.
_VCF001 = (
    "##fileformat=VCFv4.2\n"
    "##source=synthetic-test-fixture\n"
    "##contig=<ID=chr1,length=248956422>\n"
    "##contig=<ID=chr2,length=242193529>\n"
    '##FILTER=<ID=PASS,Description="all filters passed">\n'
    '##INFO=<ID=DP,Number=1,Type=Integer,Description="depth">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t"
    "TEST001_T\tTEST001_N\n"
    "chr1\t1234\t.\tA\tG\t99\tPASS\tDP=42\tGT:DP\t0/1:21\t0/0:20\n"
    "chr2\t5678\t.\tC\tT\t88\tPASS\tDP=36\tGT:DP\t1/1:18\t0/0:19\n")
_VCF002 = (
    "##fileformat=VCFv4.2\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t"
    "TEST002_T\tTEST002_N\n"
    "chr1\t999\t.\tG\tA\t99\tPASS\t.\tGT\t0/1\t0/0\n")
_HLA = ("sample\tgene\tallele_1\tallele_2\n"
        "TEST001\tA\tHLA-A*02:01\tHLA-A*24:02\n"
        "TEST001\tB\tHLA-B*07:02\tHLA-B*39:06\n"
        "TEST001\tC\tHLA-C*07:02\tHLA-C*07:02\n")
_FUSION = ("Gene1\tGene2\tScore\tSplitReads\tDiscordantPairs\n"
           "EML4\tALK\t0.91\t12\t4\n")
_SPLICE = "chrom\tstart\tend\tstrand\tread_count\nchr1\t1000\t1300\t+\t7\n"
_EXPR = "GeneName\tTPM\nEML4\t12.5\nALK\t0.3\nGAPDH\t850.0\n"


def _bam(path: pathlib.Path) -> None:
    if os.path.exists(SAMTOOLS):
        head = b"@HD\tVN:1.6\tSO:coordinate\n"
        r = subprocess.run([SAMTOOLS, "view", "-b", "-o", str(path), "-"],
                           input=head, capture_output=True, timeout=60)
        r2 = subprocess.run([SAMTOOLS, "index", str(path)],
                            capture_output=True, timeout=60)
        if r.returncode == 0 and r2.returncode == 0 and path.exists():
            return
    # header-only stand-in: the discovery inventory only stats the file
    # and looks for an index; it never parses alignment content here.
    path.write_bytes(b"BAM\x01" + b"\x00" * 300)
    path.with_name(path.name + ".bai").write_bytes(b"BAI\x01" + b"\x00" * 150)


def ensure() -> tuple:
    """(Re)create both fixture directories; return (clean, ambiguous)."""
    CLEAN.mkdir(parents=True, exist_ok=True)
    AMBIG.mkdir(parents=True, exist_ok=True)

    def w(p, s):
        pathlib.Path(p).write_text(s)

    w(CLEAN / "test001_somatic_filtered.vcf", _VCF001)
    w(CLEAN / "test001_hla_typing.tsv", _HLA)
    w(CLEAN / "test001_fusions.tsv", _FUSION)
    w(CLEAN / "test001_splicejunctions.tsv", _SPLICE)
    w(CLEAN / "test001_expression.tsv", _EXPR)
    w(AMBIG / "test002_somatic.vcf", _VCF002)
    for n in ("test001_tumor_dna", "test001_normal_dna", "test001_rna"):
        _bam(CLEAN / (n + ".bam"))
    for n in ("test002_tumor_dna", "test002_normal_dna_run1",
              "test002_normal_dna_run2"):
        _bam(AMBIG / (n + ".bam"))
    return CLEAN, AMBIG


if __name__ == "__main__":
    c, a = ensure()
    print("fixtures ready:", c, a)
