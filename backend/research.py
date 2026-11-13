"""SOTA tool discovery & benchmarking module - MOCK research results.

This prototype does NOT perform live literature search, does NOT download
anything and does NOT benchmark anything. The digests below are static,
hand-written EXAMPLE content about real, publicly documented tools, clearly
labelled as mock. When a real research adapter is connected (API task list
in README), the same report shape will be filled by live sources with
citations; the UI separation between literature-derived claims and
independently benchmarked results is already enforced here:
 - 'basis: literature-derived (mock)' rows cite sources but carry EXAMPLE
   numbers only;
 - 'basis: independently benchmarked' rows require a validated dataset run -
   there are NONE yet and the UI shows that explicitly.
"""
from __future__ import annotations

import random
import time

from .mock_data import DEMO_DATASET_VERSION
from .store import Store, now

AVAILABILITY = ("open-source | freely available with restrictions | "
                "license-required | commercial | unavailable | unclear")

TARGETS = {
    "presentation": {"stage": "Presentation (MHC-I EL / ligand prediction)",
                     "current": "NetMHCpan 4.1b + MHCflurry 2.x",
                     "candidates": [
        {"tool": "NetMHCpan 4.1b", "version": "4.1b",
         "availability_class": "license-required (free academic license)",
         "license": "academic free / commercial paid (DTU)",
         "source": "Reynisson et al. 2020, NAR (mock citation example)",
         "publication_year": 2020, "evidence_quality": "high (widely used)",
         "accuracy_example": "example EL AUC ~0.94 on mock benchmark extract",
         "perf_example": "~10k peptides/min/core (mock figure)",
         "io_compat": "peptide+HLA in, binding scores out; pVACtools-integrated",
         "maintenance": "active, DTU Health Tech site (mock)",
         "notes": "current NeoAG tool - baseline for comparisons"},
        {"tool": "MHCflurry 2.x", "version": "2.0.x",
         "availability_class": "open-source (Apache-2.0)",
         "license": "Apache-2.0", "source": "O'Donnell et al. 2020, Cell (mock)",
         "publication_year": 2020, "evidence_quality": "high",
         "accuracy_example": "example EL AUC ~0.93 on mock benchmark extract",
         "perf_example": "GPU-friendly, ~50k peptides/s on GPU (mock figure)",
         "io_compat": "Python API; pVACtools-integrated",
         "maintenance": "active on GitHub (mock)", "notes": "current tool"},
        {"tool": "MHCnuggets 2.x", "version": "2.3.x",
         "availability_class": "open-source (MIT)",
         "license": "MIT", "source": "Shao et al. 2020, Immunology (mock)",
         "publication_year": 2020, "evidence_quality": "moderate (older model)",
         "accuracy_example": "example EL AUC ~0.89 on mock benchmark extract",
         "perf_example": "CPU hours for cohort scale (mock figure)",
         "io_compat": "Python API; no pVACtools plugin maintained",
         "maintenance": "low activity (mock)", "notes": "literature alternative"},
        {"tool": "BigMHC", "version": "1.x",
         "availability_class": "open-source",
         "license": "GPL-compatible (mock - verify before adopt)",
         "source": "Wang et al. 2023, NMI (mock)", "publication_year": 2023,
         "evidence_quality": "moderate (single-group benchmark)",
         "accuracy_example": "example immunogenicity AUC ~0.76 (mock)",
         "perf_example": "seconds per peptide list (mock)",
         "io_compat": "standalone scripts; wrapper needed",
         "maintenance": "limited (mock)", "notes": "candidate for EL+IM readout"},
    ]},
    "stability": {"stage": "Peptide-MHC stability",
                  "current": "NetMHCstabpan 1.0",
                  "candidates": [
        {"tool": "NetMHCstabpan 1.0", "version": "1.0",
         "availability_class": "license-required (free academic license)",
         "license": "DTU academic license",
         "source": "Rasmussen et al. 2016, JI (mock)", "publication_year": 2016,
         "evidence_quality": "high (production gate dependency)",
         "accuracy_example": "example Spearman ~0.79 on mock extract",
         "perf_example": "bulk web/API (mock)", "io_compat": "pVACtools-integrated",
         "maintenance": "stable, rarely updated (mock)",
         "notes": "REQUIRED by POL-001 for production ranking"},
        {"tool": "MHCStab-EL retraining on TransferLearned pan", "version": "-",
         "availability_class": "unclear (no maintained release)",
         "license": "research code only", "source": "literature only (mock)",
         "publication_year": 2021, "evidence_quality": "low (no runnable release)",
         "accuracy_example": "no example (not independently benchmarked)",
         "perf_example": "n/a", "io_compat": "incompatible as-is",
         "maintenance": "none (mock)", "notes": "availability risk example"},
    ]},
    "fusion": {"stage": "Fusion discovery",
               "current": "EasyFuse + STAR-Fusion (+Arriba optional)",
               "candidates": [
        {"tool": "EasyFuse 1.x", "version": "1.3.x",
         "availability_class": "freely available with restrictions (non-commercial)",
         "license": "non-commercial use (mock - verify)",
         "source": "Weber et al. 2021, Nat Commun (mock)", "publication_year": 2021,
         "evidence_quality": "high (published head-to-head)",
         "accuracy_example": "example F1 ~0.85 on mock extract",
         "perf_example": "~4 h/case RNA (mock)", "io_compat": "custom container",
         "maintenance": "active (mock)", "notes": "current tool"},
        {"tool": "STAR-Fusion 1.x", "version": "1.13",
         "availability_class": "open-source (GPL-3)",
         "license": "GPL-3", "source": "Haas et al. 2019 (mock)",
         "publication_year": 2019, "evidence_quality": "high",
         "accuracy_example": "example F1 ~0.80 on mock extract",
         "perf_example": "~2 h/case RNA (mock)", "io_compat": "standard STAR stack",
         "maintenance": "active (mock)", "notes": "current secondary caller"},
        {"tool": "Arriba 2.x", "version": "2.4",
         "availability_class": "freely available with restrictions (academic)",
         "license": "academic free; commercial paid", "source": "Uhlmann 2022 (mock)",
         "publication_year": 2022, "evidence_quality": "high",
         "accuracy_example": "example F1 ~0.83 on mock extract",
         "perf_example": "<1 h/case (mock)", "io_compat": "STAR + ChimericSAM",
         "maintenance": "active (mock)", "notes": "fast, but license review needed"},
    ]},
    "hla_typing": {"stage": "HLA typing",
                   "current": "OptiType + SpecHLA/HLA-LA",
                   "candidates": [
        {"tool": "OptiType 1.3.x", "version": "1.3.2+",
         "availability_class": "freely available with restrictions (academic)",
         "license": "academic", "source": "Szolek 2014 (mock)",
         "publication_year": 2014, "evidence_quality": "high",
         "accuracy_example": "example accuracy ~97% on mock extract",
         "perf_example": "minutes/case (mock)", "io_compat": "RNA/DNA BAM in",
         "maintenance": "stable (mock)", "notes": "current tool"},
        {"tool": "specHLA", "version": "1.0+",
         "availability_class": "open-source", "license": "GPL (mock - verify)",
         "source": "2024 preprint (mock)", "publication_year": 2024,
         "evidence_quality": "moderate (single paper)",
         "accuracy_example": "example accuracy ~98% on mock extract",
         "perf_example": "minutes/case, long-read aware (mock)",
         "io_compat": "BAM in; requires its container",
         "maintenance": "active (mock)", "notes": "also powers HLA LOH in NeoAG"},
    ]},
}

FULL_PIPELINE_TARGETS = list(TARGETS.keys())


def _recommendation(scope: str, target: str) -> dict:
    return {
        "verdict": ("No replacement recommended from this mock digest - the "
                    "current NeoAG tool set is already at or above the "
                    "mock literature baseline; adoption is NOT concluded."),
        "experiments_before_adoption": [
            "1. reproduce the candidate's published metric on NeoAG's own "
            "validation fixture (validated dataset, not literature numbers)",
            "2. run current vs candidate on the same 3-5 demo cases and "
            "record overlap/precision on identical inputs",
            "3. review the exact license text for commercial use and "
            "container redistribution (availability classes can change)",
            "4. pin tool version + container digest in the tools manifest "
            "before any production run",
            "5. confirm output-schema compatibility with raw_events/"
            "raw_peptides normalization (pVACtools-format mapping)",
        ],
    }


def build_report(scope: str, target: str) -> dict:
    if scope == "full":
        keys = FULL_PIPELINE_TARGETS
    else:
        if target not in TARGETS:
            raise ValueError("unknown target")
        keys = [target]
    comparison = []
    for k in keys:
        meta = TARGETS[k]
        for c in meta["candidates"]:
            comparison.append({
                "stage": meta["stage"],
                "current_in_neoag": c["tool"] == meta["current"].split(" +")[0]
                or c["tool"] in meta["current"],
                "tool": c["tool"], "version": c["version"],
                "availability_class": c["availability_class"],
                "license": c["license"],
                "source_citation": c["source"],
                "publication_year": c["publication_year"],
                "evidence_quality": c["evidence_quality"],
                "accuracy_example": c["accuracy_example"],
                "perf_example": c["perf_example"],
                "io_compat": c["io_compat"],
                "maintenance": c["maintenance"],
                "notes": c["notes"],
                "basis": "literature-derived (MOCK digest - numbers are "
                         "illustrative placeholders)",
                "independently_benchmarked": False,
            })
    return {
        "disclaimer": ("MOCK RESEARCH DIGEST - example content only. No live "
                       "literature search, no downloads, no benchmarks were "
                       "performed. Numbers are illustrative placeholders."),
        "scope": scope,
        "target": target if scope != "full" else "full pipeline",
        "availability_classes_legend": AVAILABILITY,
        "comparison": comparison,
        "recommendation": _recommendation(scope, target),
        "provenance": {"engine": "mock-research", "dataset_version":
                       DEMO_DATASET_VERSION,
                       "live_sources_connected": False},
    }


RESEARCH_STEPS = [
    "scanning configured literature sources (MOCK - no network access)",
    "reading tool documentation for current versions (MOCK)",
    "checking repositories and release dates (MOCK)",
    "classifying availability and licensing (MOCK)",
    "extracting example accuracy/performance figures from mock digest (MOCK)",
    "separating literature-derived claims from independent benchmarks (MOCK)",
    "compiling recommendation + pre-adoption experiment list (MOCK)",
]


def run_research(rid: str, scope: str, target: str, store: Store,
                 publish) -> None:
    for line in RESEARCH_STEPS:
        with store.lock:
            store.db.execute(
                "INSERT INTO research_events(research_id,ts,text) VALUES(?,?,?)",
                (rid, now(), line))
            store.db.commit()
        publish({"type": "research", "id": rid})
        time.sleep(0.55)
    report = build_report(scope, target)
    store.research_set(rid, "completed", report, finished=True)
    store.audit("engine", "research.completed", rid,
                {"scope": scope, "target": target})
    publish({"type": "research", "id": rid})
