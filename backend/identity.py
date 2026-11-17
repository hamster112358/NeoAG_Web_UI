"""S3 canonical software identity (root-cause 1.3: a name is not an
identity).

resolve_identity() turns one candidate NAME into one IdentityRecord
by collecting evidence about the name across independent AXES and
requiring corroboration, never accepting the top search hit:

  axis "literature-url"    a retrieved paper's text links to the
                           project (URL with org/repo, registry page)
  axis "naming-paper"      the candidate was introduced by a naming
                           publication (intro-cue evidence from S1)
  axis "registry"          a registry record whose NAME IS the
                           candidate (exact slug) on a plausible
                           ecosystem (PyPI/bioconda/Bioconductor/
                           bio.tools)
  axis "description"       a project record whose own description
                           matches the task's domain vocabulary
  axis "deployment"        evidence from THIS deployment (the caller
                           passes it: config membership, reference
                           data paths, install scripts naming the
                           project)

Resolution rules:
  - resolved  : >= 2 axes AND the axes are not all "same-source"
                (a GitHub name-search hit plus its own registry page
                is weaker than a literature URL plus a registry plus
                a description match); the winning project is the one
                with the most axes; ties across DIFFERENT projects
                with >= 2 axes each -> contested.
  - contested : multiple projects tie on corroboration - the report
                must show the candidates and why unresolved; NO
                version/license/number may be attached (the caller
                keeps fields 'identity contested').
  - unresolved: < 2 axes.

Component recognition: a project whose repository NAME differs from
the tool name is not rejected when the tool name appears as a
subdirectory of the repository contents (PURPLE-inside-hmftools
class) - this is probed only when budget allows and always records
its evidence path.

The module is network-pluggable (probes dict) for offline tests.
"""
from __future__ import annotations

import re

from . import ontology
from .store import now

MIN_AXES = 2

_URL_RE = re.compile(r"https?://(?:www\.)?(github\.com/[\w.\-]+/[\w.\-]+"
                     r"|bio\.tools/[\w.\-]+|pypi\.org/project/[\w.\-]+"
                     r"|anaconda\.org/[\w\-]+/[\w\-]+"
                     r"|bioconductor\.org/[\w./\-]+)", re.I)


def _norm_key(kind: str, ident: str) -> str:
    """Stable project key so the same project found via several routes
    (literature URL 'github.com/x/y', registry link, github search)
    merges into ONE considered entry."""
    ident = (ident or "").strip().rstrip("/.,;:").lower()
    if ident.startswith("github.com/"):
        return "gh:" + ident[len("github.com/"):].split("#")[0]
    if re.fullmatch(r"[a-z0-9-]+/[a-z0-9._~+-]+", ident):
        # BARE owner/repo (the id a GitHub search record carries) is
        # the same project as a registry's github.com/owner/repo link
        # - without this the two routes never merge and a genuine
        # two-axis identity reads as two one-axis projects
        return "gh:" + ident.split("#")[0]
    if ident.startswith("pypi.org/project/"):
        return "pypi:" + ident.split("/project/", 1)[1]
    if ident.startswith("anaconda.org/"):
        return "bioconda:" + ident.split("anaconda.org/", 1)[1]
    if ident.startswith("bioconductor.org/"):
        return "bioc:" + ident
    return f"{kind}:{ident}"


class _Considered:
    """Evidence accumulator for one candidate PROJECT."""

    def __init__(self, project: str, origin: str):
        self.project = project            # 'owner/repo', 'pypi:sig', ...
        self.origin = origin              # where first seen
        self.axes: dict[str, list[str]] = {}
        self.url = ""
        self.description = ""
        self.extra: dict = {}             # license, stars, versions...

    def add(self, axis: str, detail: str):
        self.axes.setdefault(axis, [])
        if detail not in self.axes[axis]:
            self.axes[axis].append(detail)

    def as_row(self) -> dict:
        return {"project": self.project, "origin": self.origin,
                "axes": {k: v[:4] for k, v in self.axes.items()},
                "n_axes": len(self.axes), "url": self.url,
                "description": (self.description or "")[:200],
                **({"license": self.extra["license"]}
                   if self.extra.get("license") else {})}


def resolve_identity(name: str, task: dict, citations=(),
                     *, registry_hits=(), github_hits=(),
                     extra_evidence=(), component_probe=None,
                     budget=None, cache=None,
                     github_contents=None) -> dict:
    """One candidate name in, one IdentityRecord out.

    registry_hits / github_hits are the records ALREADY retrieved for
    this name by the caller (no duplicate network calls); each must
    carry the keys its source guarantees (id/url/title or
    full_name/description...).
    component_probe: optional callable(project, tool_name)->bool|None
    checking whether the tool name exists inside the project
    (repository contents) - None means 'not probed'.
    github_contents: raw list of top-level entry names for the
    probed repo (recorded verbatim into the evidence detail).
    budget: {"core": int|None} - component probing spends 1 call and
    is recorded as skipped when exhausted, never faked.
    """
    key = (name or "").lower()
    if cache is not None and key in cache:
        return cache[key]
    name_l = (name or "").lower()
    considered: dict[str, _Considered] = {}

    def _get(project: str, origin: str) -> _Considered:
        k = _norm_key("proj", project)
        if k not in considered:
            considered[k] = _Considered(project, origin)
        return considered[k]

    # ---- axis: literature URLs inside naming citations ----
    lit_projects: set[str] = set()
    name_rx = (re.compile(r"\b" + re.escape(name_l) + r"\b")
               if name_l else None)
    for c in citations or ():
        blob = f"{c.get('title') or ''} {c.get('abstract') or ''}"
        if name_l and name_l not in blob.lower():
            continue
        # a URL corroborates THIS tool only when the SENTENCE carrying
        # it also names the tool: an abstract that merely contains the
        # name-like word (the plural acronym "CNVs" occurs in most CNV
        # abstracts) or discusses other tools must not graft its
        # availability link onto this candidate (measured 2026-11-17:
        # candidates "CNVs"/"AneuFinder" resolved to STGBench's repo
        # through STGBench's own availability sentence)
        for sent in re.split(r"(?<=[.!?;])\s+|\n+", blob.lower()):
            if not sent or (name_rx and not name_rx.search(sent)):
                continue
            for m in _URL_RE.finditer(sent):
                # sentence-final punctuation is not part of the URL
                u = m.group(1).rstrip(".,;:!?")
                cnd = _get(u, f"URL in naming paper "
                              f"{c.get('id') or c.get('title', '')[:40]}")
                cnd.url = cnd.url or f"https://{u}"
                cnd.add("literature-url", f"{c.get('id') or 'paper'} "
                                          f"links {u}")
                lit_projects.add(cnd.project)
        # org-style mentions: "available at github.com/org/repo"
    # ---- axis: naming paper ----
    for e in extra_evidence or ():
        if e.get("axis") == "naming-paper":
            # naming papers corroborate the CANDIDATE, not a project
            # directly; attach to any literature-url project, else to
            # a virtual 'name-only' project
            if lit_projects:
                for k in considered:
                    if considered[k].project in lit_projects:
                        considered[k].add("naming-paper", e["detail"])
            else:
                _get(f"name:{name_l}", "naming literature").add(
                    "naming-paper", e["detail"])
        else:
            # caller-provided deployment/config evidence attaches to
            # a project when the detail names it, else records a
            # name-level fact (an ecosystem/deployment axis)
            det = str(e.get("detail") or "")
            hit = None
            for m in _URL_RE.finditer(det):
                hit = _get(m.group(1).rstrip(".,;:!?"),
                           e.get("source", "deployment"))
                break
            if hit is None:
                hit = _get(f"name:{name_l}",
                           e.get("source", "deployment"))
            hit.add(e.get("axis", "deployment"), det[:160])
    # ---- axis: registries ----
    for src, recs in (("registry", registry_hits),):
        for r in recs or ():
            slug = str(r.get("id") or "")
            title = str(r.get("title") or "")
            if not (slug == name_l or name_l == title.lower()):
                continue  # not an exact-name record: not corroboration
            url = str(r.get("url") or "")
            # key on the record's PROJECT HOME when it has one: a
            # bioconda/PyPI package and its GitHub repository are ONE
            # project seen on two independent axes. Without this the
            # anaconda.org/pypi.org URL forms its own one-axis project
            # and genuine two-axis corroboration never merges
            # (measured 2026-11-17: CNVkit, PureCN stuck unresolved).
            home = str(r.get("home") or "")
            key_src = (home or url).replace("https://", "").replace(
                "http://", "")
            cnd = _get(key_src or f"registry:{slug}", src)
            cnd.url = cnd.url or home or url
            cnd.description = cnd.description or str(
                r.get("summary") or "")
            if r.get("license"):
                cnd.extra.setdefault("license", r["license"])
            cnd.add("registry", f"{r.get('source', 'registry')}: {slug}"
                      + (f" (version {r.get('version')})"
                         if r.get("version") else ""))
            if r.get("version"):
                cnd.extra.setdefault("registry_version",
                                     r["version"])
    # ---- GitHub name-search hits: description axis (and a WEAK
    #      presence axis - alone never enough) ----
    domain_rx = task.get("domain_rx")
    gh_search_hits = 0
    for h in github_hits or ():
        full = str(h.get("id") or h.get("full_name") or "")
        if not full:
            continue
        gh_search_hits += 1
        cnd = _get(full, "GitHub repository search")
        cnd.url = cnd.url or str(h.get("url") or
                                 f"https://github.com/{full}")
        desc = str(h.get("description") or "")
        if len(desc) > len(cnd.description or ""):
            cnd.description = desc
        cnd.extra.setdefault("stars", h.get("stars"))
        cnd.extra.setdefault("license", h.get("license"))
        cnd.extra.setdefault("archived", bool(h.get("archived")))
        if domain_rx and desc and domain_rx.search(desc):
            cnd.add("description", "repository description matches the "
                                   "task's domain vocabulary")
        elif name_l in full.lower():
            # name match without domain corroboration is presence only
            cnd.add("github-name-hit",
                    f"repository name contains {name!r} (top-search-hit "
                    f"class evidence - presence, not identity)")
        elif domain_rx and not desc:
            pass  # empty-description hit: nothing positive either way
    # ---- component recognition: tool inside a differently-named
    #      repository (a literature-URL or org-named repo whose name
    #      lacks the tool name) ----
    if component_probe is not None:
        for cnd in list(considered.values()):
            if not cnd.project.startswith("github.com/") or \
                    name_l in cnd.project.lower():
                continue
            left = (budget or {}).get("core")
            if left is not None and left < 1:
                cnd.add("component", "contents probe skipped (GitHub "
                            "API budget exhausted this hour)")
                continue
            if budget is not None and left is not None:
                budget["core"] = left - 1
            r = component_probe(cnd.project, name)
            if r is True:
                cnd.add("component", f"repository contents list "
                            f"contains a {name!r} component")
                cnd.extra["component"] = True
            elif r is False:
                cnd.add("component-absent",
                        "tool name not found in repository top-level "
                        "contents")
    # ---- verdict ----
    rows = sorted(considered.values(),
                  key=lambda c: (-len(c.axes), c.project))
    strong = [c for c in rows if len(c.axes) >= MIN_AXES
              and "github-name-hit" not in (set(c.axes) - {"description"})]
    # a "strong" project must have >=2 axes and those axes must not be
    # only presence: count axes other than presence-only ones
    def _weight(c: _Considered) -> int:
        return sum(1 for a in c.axes if a not in ("github-name-hit",))

    strong = [c for c in rows if _weight(c) >= MIN_AXES]
    chosen = None
    rejected = [c.as_row() for c in rows]
    status = "unresolved"
    if strong:
        top = _weight(strong[0])
        topset = [c for c in strong if _weight(c) == top]
        if len(topset) == 1:
            chosen = topset[0]
            status = "resolved"
        elif all(_is_same_project(c, topset[0]) for c in topset):
            chosen = topset[0]
            status = "resolved"
        else:
            status = "contested"
    record = {
        "query_name": name,
        "status": status,
        "resolved_project": (chosen.url or chosen.project) if chosen
        else None,
        "axes": ({k: v[:4] for k, v in chosen.axes.items()} if chosen
                 else []),
        "n_axes": _weight(chosen) if chosen else 0,
        "corroboration": ([f"{a}: {d}" for a, ds in chosen.axes.items()
                           for d in ds[:3]] if chosen else []),
        "considered": rejected[:8],
        "rejected": [c.as_row() for c in rows if c is not chosen][:8],
        "gh_search_hits": gh_search_hits,
        "note": "",
        "checked_at": now(),
    }
    if status == "contested":
        record["note"] = (
            "several projects are corroborated equally - identity not "
            "resolved; versions/licenses/performance may NOT be "
            "attributed to any of them until corroborating evidence "
            "disambiguates (a targeted benchmark of the LITERATURE, "
            "not of the tools, is what resolves this)")
    elif status == "unresolved":
        record["note"] = (
            "fewer than two independent evidence axes for any project "
            "behind this name - the candidate's identity is unknown; "
            "no version, license or availability claim may be attached")
    if cache is not None:
        cache[key] = record
    return record


def _is_same_project(a: _Considered, b: _Considered) -> bool:
    """Two considered entries that resolve to the same underlying
    project (github repo X and the Bioconductor page that only mirrors
    X) - conservative: only when one url literally contains the other
    project's repo path."""
    for x, y in ((a, b), (b, a)):
        if x.project.startswith("github.com/") and x.project in y.url:
            return True
    return False


def blocks_attribution(record: dict | None) -> str | None:
    """Gate used by the evidence/version stages: a project-scoped fact
    may only be recorded for a resolved identity. Returns a reason
    string when attribution is blocked."""
    if record is None:
        return "no identity record"
    if record["status"] == "resolved":
        return None
    return (f"identity is {record['status']} - attributing this "
            f"field to a project would be a guess")
