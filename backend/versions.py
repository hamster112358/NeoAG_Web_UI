"""Version verification chain (M5 PART 4.2).

One candidate tool in, one version VERDICT out - and the verdict is
never a lazy "unknown" when any link of the chain produced evidence.
The chain order follows the brief:

  1. official release page (GitHub Releases; the release page of
     GitHub-hosted tools is the official one)
  2. git tags
  3. package registries (PyPI, bioconda/anaconda.org)
  4. official version file inside the repository (contents API)
  5. original papers / benchmark methods (version tokens adjacent to
     the tool name in retrieved abstracts - recorded, never treated
     as the CURRENT version)

Distinguished values (each with source + checked_at):
  latest_stable, latest_release (which may be a pre-release),
  development commit (fallback when no formal release exists),
  registry versions, versions cited by benchmarks.

Honesty rules enforced here:
  - a missing GitHub Release NEVER yields "unknown" by itself: the
    chain continues and every skipped/empty step keeps its reason
    ("rate budget exhausted", "no GitHub identity", "no version-like
    tag" - so a later run can tell why);
  - version values are only ever copied verbatim from a probe result;
    nothing is interpolated between releases.

Unauthenticated GitHub shares 60 core requests/hour; the caller
passes a budget dict so steps are SKIPPED WITH A REASON instead of
burning (and 403-ing on) an exhausted budget.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .store import now

PRE_RE = re.compile(r"(rc[-_.]?\d*|alpha|beta|dev[-_.]?\d*|nightly|"
                    r"snapshot|preview|-pre\d*)", re.I)
TAGVER_RE = re.compile(r"^v?(\d+(?:\.\d+){1,3})[\w.+~-]*$")


def _vkey(text: str):
    """Sortable numeric key for a version-like string, or None."""
    m = TAGVER_RE.match((text or "").strip())
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split("."))


def _is_pre(text: str, gh_prerelease=False) -> bool:
    return bool(gh_prerelease) or bool(PRE_RE.search(text or ""))


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------

def resolve(name: str, *, repo: str | None = None, citations=(),
            probes=None, budget=None, cache=None, prefer=None) -> dict:
    """Run the version chain for one candidate.

    probes  : optional dict {releases_tags, pypi, bioconda, version_file,
              head_commit} -> functions (dependency injection for offline
              tests; defaults are the real live adapters).
    budget  : mutable {"core": int|None} of GitHub core requests left;
              steps decrement it and record 'skipped' with a reason when
              it is exhausted.
    cache   : optional dict keyed by lower(name) - one chain per tool per
              run (registry + GitHub state do not change mid-run).
    prefer  : the registry channel this deployment actually installs
              from (root-cause 1.4: picking PyPI's 2.8.1 while the
              deployment pins bioconda 3.2.0 stated the wrong
              distribution's version as THE stable). When set, the
              registry fallback consults that channel first and the
              verdict names the channel explicitly; other channels'
              values stay recorded in the chain.
    """
    key = (name or "").lower()
    if cache is not None and key in cache:
        return cache[key]
    P = dict(releases_tags=None, pypi=None, bioconda=None,
             bioconductor=None, version_file=None, head_commit=None)
    if probes:
        P.update({k: v for k, v in probes.items() if v})
    if not all(P.values()):
        from . import sources
        for k, fn in (("releases_tags", sources.github_releases_tags),
                      ("pypi", sources.pypi_package),
                      ("bioconda", sources.anaconda_bioconda),
                      ("bioconductor", sources.bioconductor_package),
                      ("version_file", sources.github_version_file),
                      ("head_commit", sources.github_head_commit)):
            if not P[k]:
                P[k] = fn

    def _core_spend(n=1):
        """True when the GitHub core budget allows n more calls."""
        if repo is None:
            return False
        left = (budget or {}).get("core")
        if left is not None and left < n:
            return False
        if budget is not None and left is not None:
            budget["core"] = left - n
        return True

    chain, values = [], {}

    def _step(step, outcome, value=None, reason="", url=""):
        chain.append({"step": step, "outcome": outcome,
                      "value": value, "reason": reason, "url": url,
                      "checked_at": now()})

    # -- 1/2. GitHub Releases + tags (one measured probe call family) --
    rels, tags, head = [], [], {}
    if repo:
        if _core_spend(2):
            r = P["releases_tags"](repo) or {}
            rels, tags, head = (r.get("releases") or [],
                                r.get("tags") or [],
                                r.get("head") or {})
            formal = [x for x in rels if not x.get("prerelease")]
            _step("official release page (GitHub Releases)",
                  "found" if rels else "empty",
                  [f"{x.get('tag')}{' (pre-release)' if x.get('prerelease') else ''}"
                   for x in rels[:5]],
                  "" if rels else "no formal GitHub release exists",
                  f"https://github.com/{repo}/releases")
            _step("git tags", "found" if tags else "empty",
                  tags[:5], "" if tags else "repository exposes no tags",
                  f"https://github.com/{repo}/tags")
        else:
            _step("official release page (GitHub Releases)",
                  "skipped (GitHub API budget exhausted this hour)",
                  reason="unauthenticated GitHub allows 60 core "
                        "requests/hour; see report provenance for the "
                        "reset time")
            _step("git tags", "skipped (GitHub API budget exhausted)",
                  reason="same budget as releases")
    else:
        _step("official release page (GitHub Releases)",
              "skipped (no GitHub identity established for this name)",
              reason="registry/literature retrievals never matched a "
                     "repository named after the candidate")
        _step("git tags", "skipped (no GitHub identity)",
              reason="same as release-page step")

    # -- 3. registries --
    py = P["pypi"]((name.split("/")[-1]).split()[0].lower()) if P.get(
        "pypi") else ([], "not probed", {})
    py_rec = (py[0] or [{}])[0] if py and py[0] else {}
    _step("registry: PyPI", "found" if py_rec.get("version") else
          ("not on PyPI" if py and "not-on-pypi" in str(py[1]) else
           "no version returned"),
          py_rec.get("version"), "" if py_rec.get("version") else
          str(py[1] if py else "not probed")[:120],
          py_rec.get("url") or "")
    bc = P["bioconda"]((name.split("/")[-1]).split()[0].lower()) \
        if P.get("bioconda") else ([], "not probed", {})
    bc_rec = (bc[0] or [{}])[0] if bc and bc[0] else {}
    _step("registry: bioconda (anaconda.org)",
          "found" if bc_rec.get("version") else
          ("not on bioconda" if bc and "not-on-bioconda" in str(bc[1])
           else "no version returned"),
          bc_rec.get("versions") or bc_rec.get("version"),
          "" if bc_rec else str(bc[1] if bc else "not probed")[:120],
          bc_rec.get("url") or "")
    # Bioconductor: the release repository is the authoritative
    # channel for R/Bioconductor tools (many have NO PyPI/bioconda
    # record at all - m8 root-cause 1.1/1.4)
    bcd = P["bioconductor"]((name.split("/")[-1]).split()[0]) \
        if P.get("bioconductor") else ([], "not probed", {})
    bcd_rec = (bcd[0] or [{}])[0] if bcd and bcd[0] else {}
    _step("registry: Bioconductor (release bioc)",
          "found" if bcd_rec.get("version") else
          ("not on Bioconductor bioc repo"
           if bcd and "not-on-bioconductor" in str(bcd[1])
           else "no version returned"),
          bcd_rec.get("version"),
          "" if bcd_rec else str(bcd[1] if bcd else "not probed")[:140],
          bcd_rec.get("url") or "")

    # -- 4. version file inside the repo (only when budget allows and
    #       the higher links came up without a stable version) --
    vf = {}
    stable_candidate = _pick_stable(rels, tags, py_rec, bc_rec,
                                    bcd_rec, prefer)
    if repo and not stable_candidate and _core_spend(1):
        vf = P["version_file"](repo) or {}
        _step("version file in repository",
              "found" if vf.get("value") else "not found",
              {"path": vf.get("path"), "value": vf.get("value")},
              "" if vf.get("value") else
              "no version found in pyproject.toml/setup.py/VERSION/"
              "version.txt",
              vf.get("url") or "")
    elif repo and not stable_candidate:
        _step("version file in repository",
              "skipped (GitHub API budget exhausted)")

    # -- 5. papers: EXPLICIT version tokens (v / version) adjacent to
    #       the name - bare numbers near a name are too noisy to count.
    #       Recorded values are never treated as the current version. --
    bench_versions = []
    esc = re.escape((name.split("/")[-1].split() or [""])[0])
    vtok = r"[vV](?:ersion)?\s*=?\s*:?\s*v?(\d+\.\d+[\w.\-]*)"
    for c in citations or ():
        blob = f"{c.get('title') or ''}. {c.get('abstract') or ''}"
        for m in re.finditer(
                rf"{esc}[^\n]{{0,40}}?{vtok}|{vtok}[^\n]{{0,40}}?{esc}",
                blob):
            v = m.group(1) or m.group(2)
            if v and not any(b["version"] == v and
                             b["where"] == c.get("id")
                             for b in bench_versions):
                bench_versions.append(
                    {"version": v, "where": c.get("id") or c.get("url"),
                     "note": "explicit version token adjacent to the "
                             "name in the retrieved text - the version "
                             "THAT STUDY measured, not the current "
                             "release"})
    _step("papers / benchmark methods",
          "found" if bench_versions else "no explicit version token",
          [b["version"] for b in bench_versions][:6],
          "" if bench_versions else
          "retrieved abstracts state no explicit tested version - the "
          "benchmark-version field stays 'not stated by source'",
          "")

    # -- commit fallback (only when nothing above produced a version) --
    commit = {}
    if repo and not stable_candidate and not head.get("sha") \
            and _core_spend(1):
        head = P["head_commit"](repo) or {}
    if repo and not stable_candidate and head.get("sha"):
        commit = head
        _step("development commit fallback", "found",
              head.get("sha", "")[:10],
              "no formal release, tag or registry version exists - an "
              "identifiable commit stands in (labelled DEVELOPMENT, not "
              "a release)", head.get("url") or "")

    # -- verdict --
    stable = stable_candidate or (
        {"value": (vf or {}).get("value"), "source": f"version file "
         f"{vf.get('path')}", "pre": False} if vf.get("value") else None)
    if commit and not stable:
        stable = {"value": f"commit {commit['sha'][:10]}",
                  "source": "repository HEAD (commit fallback)",
                  "pre": False, "development": True}
    values["latest_stable"] = (
        {"value": stable["value"], "source": stable["source"],
         "checked_at": now(),
         **({"development": True} if stable.get("development") else {})}
        if stable else None)
    newest_rel = _newest_release(rels)
    values["latest_release"] = (
        {"value": newest_rel.get("tag"), "source": "GitHub release "
         f"published {str(newest_rel.get('published') or '')[:10]}",
         "prerelease": _is_pre(newest_rel.get("tag", ""),
                               bool(newest_rel.get("prerelease"))),
         "checked_at": now()} if newest_rel else None)
    values["development_commit"] = (
        {"value": head.get("sha"), "date": head.get("date"),
         "url": head.get("url"), "checked_at": now()}
        if head.get("sha") else None)
    values["registries"] = {k: v for k, v in (
        ("pypi", py_rec.get("version")),
        ("bioconda", bc_rec.get("version")),
        ("bioconductor", (bcd_rec or {}).get("version"))) if v}
    values["benchmark_versions"] = bench_versions

    out = {"candidate": name, "github_repo": repo,
           "display": _display(values),
           "values": values, "chain": chain,
           "all_steps_empty": not any(
               c["outcome"].startswith("found") for c in chain),
           "checked_at": now()}
    if cache is not None:
        cache[key] = out
    return out


def _pick_stable(rels, tags, py_rec, bc_rec, bcd_rec=None, prefer=None):
    for r in sorted((x for x in rels if not x.get("prerelease")
                     and _vkey(x.get("tag", ""))),
                    key=lambda x: _vkey(x.get("tag", "")), reverse=True):
        return {"value": r.get("tag"),
                "source": f"GitHub release (published "
                          f"{str(r.get('published') or '')[:10]})",
                "pre": False}
    tagvers = [t for t in tags if _vkey(t) and not _is_pre(t)]
    if tagvers:
        best = max(tagvers, key=_vkey)
        return {"value": best, "source": "git tag (no formal release "
                "page entry)", "pre": False}
    # registry fallback ORDER is deployment-aware (m8): the channel the
    # deployment installs from wins; without that knowledge PyPI keeps
    # its historical first look, and ALL channel values remain visible
    # in the chain either way.
    chans = [
        ("pypi", py_rec, "PyPI current version"),
        ("bioconda", bc_rec, "bioconda current version"),
        ("bioconductor", bcd_rec, "Bioconductor release current "
                                  "version")]
    if prefer:
        chans.sort(key=lambda c: 0 if c[0] == prefer else 1)
    for _, rec, src in chans:
        v = (rec or {}).get("version")
        if v and _vkey(v) and not _is_pre(v):
            return {"value": v, "source": src, "pre": False}
    return None


def _newest_release(rels):
    cands = [r for r in rels if r.get("tag")]
    if not cands:
        return None
    withkey = [r for r in cands if _vkey(r["tag"])]
    if withkey:
        return max(withkey, key=lambda r: _vkey(r["tag"]))
    return max(cands, key=lambda r: str(r.get("published") or ""))


def _display(values) -> str:
    st = values.get("latest_stable")
    if st:
        tag = "DEVELOPMENT commit - no formal release found" \
            if st.get("development") else "stable"
        return f"{st['value']} ({tag}; {st['source']}; " \
               f"checked {str(st['checked_at'])[:10]})"
    lr = values.get("latest_release")
    if lr:
        return (f"{lr['value']} (latest GitHub release"
                f"{' - PRE-RELEASE, no stable release found' if lr.get('prerelease') else ''}; "
                f"{lr['source']}; checked {str(lr['checked_at'])[:10]})")
    # honest, but the sentinel string stays exactly machine-checkable
    return "Not verified"


# ---------------------------------------------------------------------------
# installed version (what THIS deployment actually runs)
# ---------------------------------------------------------------------------

TOOLS_ROOT_DEFAULT = "/data1/neoag/tools"


def installed_version(name: str, tools_root: str | None = None) -> dict:
    """Read-only, bounded probe: is the tool's installed version visible
    in the deployed tools layout (dist-info / conda-meta / VERSION file)?
    Never executes anything. When the layout does not expose a version
    the honest answer is returned - not a guess."""
    root = Path(tools_root or os.environ.get("NEOAG_TOOLS_ROOT")
                or TOOLS_ROOT_DEFAULT)
    slug = re.sub(r"[_]+", "-", (name or "").lower())
    out = {"value": None, "evidence": None, "checked_at": now()}
    if not name or not root.is_dir():
        out["reason"] = (f"tools root {root} is not readable from this "
                         f"application - installed version not verified")
        return out
    try:
        dirs = [d for d in root.iterdir() if d.is_dir()
                and re.sub(r"[_]+", "-", d.name.lower()) in (
                    slug, slug.replace("-", ""))]
    except OSError as e:
        out["reason"] = f"tools root not listable: {type(e).__name__}"
        return out
    for d in dirs:
        try:
            # dist-info of a python install (bounded glob depth)
            for pat in (f"{slug}*.dist-info", f"{slug.replace('-', '_')}*.dist-info"):
                for di in d.glob(f"**/{pat}"):
                    m = re.match(re.escape(pat.split("*")[0]) +
                                 r"[-_](\d[^-]*)\.dist-info", di.name)
                    if m:
                        return {"value": m.group(1), "evidence":
                                f"python dist-info: {di}",
                                "checked_at": now()}
            for meta in d.glob("conda-meta/*.json"):
                if re.match(re.escape(slug.split()[0]) + r"[-_]",
                            meta.name, re.I):
                    import json as _json
                    try:
                        j = _json.loads(meta.read_text())
                        if j.get("version"):
                            return {"value": j["version"],
                                    "evidence":
                                        f"conda-meta: {meta}",
                                    "checked_at": now()}
                    except Exception:
                        pass
            for vf in ("VERSION", "version.txt"):
                p = d / vf
                if p.is_file():
                    t = p.read_text(errors="replace").strip().splitlines()
                    if t:
                        return {"value": t[0][:40],
                                "evidence": f"version file: {p}",
                                "checked_at": now()}
        except OSError:
            continue
    if dirs:
        out["reason"] = ("the deployed layout exposes no version marker "
                         "for this tool (checked dist-info, conda-meta, "
                         "VERSION files) - installed version NOT "
                         "verified rather than guessed")
    else:
        out["reason"] = ("no directory named after this tool under the "
                         "configured tools root - it is not part of "
                         "this installation")
    return out


# ---------------------------------------------------------------------------
# candidate classification (4.4): algorithm vs wrapper/pipeline/...
# ---------------------------------------------------------------------------

CLASS_SIGNALS = [
    ("workflow wrapper", re.compile(
        r"\b(wrap(s|per|ped)?|interface (?:to|for)|front[\s-]?end"
        r"(?: to| for)?|encapsulat\w+)\b", re.I)),
    ("tool collection", re.compile(
        r"\b(collection of (?:tools|methods|programs)|tool\s?box"
        r"|suite of|bundles? (?:multiple|several) tools)\b", re.I)),
    ("pipeline (multi-stage workflow)", re.compile(
        r"\b(pipeline|workflow)\b", re.I)),
    ("reimplementation", re.compile(
        r"\b(re-?implementation|re-?written? (?:in|port)|port of"
        r"|python (?:port|package) of)\b", re.I)),
    ("model/weights artifact", re.compile(
        r"\b(pre-?trained|model weights|hugging\s?face\s+model"
        r"|checkpoint download)\b", re.I)),
    ("algorithm (method implementation)", re.compile(
        r"\b(predict\w*|classif\w*|score\w*|scan\w*|neural network"
        r"|machine learning|deep learning|convolutional|transformer)\b",
        re.I)),
]


def classify(descriptions, archived=False) -> dict:
    """Classify what KIND of artifact a candidate is, using ONLY the
    description text actually retrieved (GitHub repo description,
    bio.tools summary, PyPI summary). The rule order matters:
    'wrapper for X pipeline' must not be swallowed by the pipeline
    regex. No positive signal -> honestly 'unclassified'. This is a
    labelled heuristic over short registry blurbs - the report says so.
    """
    blob = " ".join(str(d or "") for d in descriptions)
    if archived:
        return {"class": "archived code",
                "signals": ["GitHub reports the repository archived"],
                "basis": "hard fact from the GitHub repository record"}
    for cls, rx in CLASS_SIGNALS:
        m = rx.search(blob)
        if m:
            return {"class": cls,
                    "signals": [f"'{m.group(0)}' in retrieved "
                                f"description"],
                    "basis": "labelled heuristic over retrieved "
                             "description text (GitHub/bio.tools/PyPI "
                             "blurbs) - not a code audit"}
    return {"class": "unclassified (retrieved descriptions carry no "
                     "kind signal)",
            "signals": [],
            "basis": "no positive evidence either way - NOT silently "
                     "grouped with algorithms"}
