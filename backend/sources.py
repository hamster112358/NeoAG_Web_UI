"""Literature / software source adapters for REAL discovery (PART D).

Every adapter performs live HTTPS retrieval with stdlib urllib and
returns (records, error). A failed source NEVER fabricates records -
the research engine persists the failure honestly in its provenance
section and reflects it in coverage. Reachability of all adapters was
measured from this A100 host on 2026-11-16 (raw.githubusercontent.com
was NOT reachable and is deliberately not used).

No API keys: these five sources are publicly queryable unauthenticated
(PubMed E-utilities, Europe PMC REST, OpenAlex, GitHub search,
bio.tools, PyPI). Requests stay modest (small page sizes, one call per
query, 15 s timeout) out of politeness to shared endpoints.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

UA = ("NeoAGWebDemo/1.0 (research adapter on a research server; "
      "reads public APIs only)")
TIMEOUT = 15


def _http_json(url: str, timeout: int = TIMEOUT, retries: int = 2):
    """M6 A8: transient failures (Europe PMC read timeouts, GitHub's
    10/min search budget) are RETRIED with the endpoint's own advice
    (X-RateLimit-Reset) before being recorded as failures - one 403
    must not silently make a whole candidate batch 'unverified'.
    A final failure is still returned as an error, never as fake data."""
    import time
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Accept":
                                                   "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            # PubMed/PyPI are strict JSON; Europe PMC too. Tolerate
            # control chars.
            return json.loads(raw, strict=False)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 403:
                reset = e.headers.get("X-RateLimit-Reset")
                wait = 20
                if reset:
                    try:
                        wait = max(5, min(65, int(reset) - int(time.time())
                                          + 1))
                    except ValueError:
                        pass
            elif e.code in (429, 500, 502, 503, 504):
                wait = 10
            else:
                raise
        except (TimeoutError, urllib.error.URLError) as e:
            last, wait = e, 8
        if attempt < retries:
            time.sleep(wait)
    raise last


def _rec(source: str, rid: str, url: str, title: str, year=None,
         **extra) -> dict:
    return {"source": source, "id": str(rid or ""), "url": url or "",
            "title": (title or "").strip(), "year": year, **extra}


# ---------------- literature ----------------

def pubmed_search(query: str, max_results: int = 8):
    """PubMed E-utilities: esearch -> esummary."""
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    q = urllib.parse.quote(query)
    try:
        j = _http_json(f"{base}esearch.fcgi?db=pubmed&retmode=json"
                       f"&retmax={max_results}&term={q}")
        ids = (j.get("esearchresult") or {}).get("idlist") or []
        total = (j.get("esearchresult") or {}).get("count")
        if not ids:
            return [], None, {"total": total}
        s = _http_json(f"{base}esummary.fcgi?db=pubmed&retmode=json"
                       f"&id={','.join(ids)}")
        out = []
        for pid in ids:
            r = (s.get("result") or {}).get(pid) or {}
            doi = next((a.get("value") for a in r.get("articleids", [])
                        if a.get("idtype") == "doi"), "")
            pmc = next((a.get("value") for a in r.get("articleids", [])
                        if a.get("idtype") == "pmc"), "")
            authors = ", ".join(a.get("name", "")
                                for a in (r.get("authors") or [])[:3])
            out.append(_rec(
                "pubmed", f"PMID:{pid}",
                f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
                r.get("title"), (r.get("pubdate") or "")[:4],
                authors=authors, venue=r.get("fulljournalname") or "",
                doi=doi, pmcid=pmc))
        return out, None, {"total": total}
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:180]}", {}


def europepmc_search(query: str, max_results: int = 8):
    """Europe PMC REST search (core resultType = includes abstracts -
    used for abstract-level benchmark-evidence extraction)."""
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
           + urllib.parse.urlencode(
               {"query": query, "format": "json",
                "resultType": "core", "pageSize": max_results}))
    try:
        j = _http_json(url)
        out = []
        for r in (j.get("resultList") or {}).get("result") or []:
            out.append(_rec(
                "europepmc", r.get("id"),
                f"https://europepmc.org/article/{(r.get('source') or '').lower()}/{r.get('id')}",
                r.get("title"), r.get("pubYear"),
                authors=(r.get("authorString") or "")[:160],
                venue=r.get("journalTitle") or "",
                doi=r.get("doi") or "",
                pmcid=r.get("pmcid") or "",
                open_access=(r.get("isOpenAccess") == "Y"),
                abstract=r.get("abstractText") or ""))
        return out, None, {"total": (j.get("hitCount"))}
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:180]}", {}


def openalex_search(query: str, max_results: int = 8):
    """OpenAlex works search (unauthenticated shared budget)."""
    url = ("https://api.openalex.org/works?"
           + urllib.parse.urlencode({"search": query,
                                     "per-page": max_results}))
    try:
        j = _http_json(url)
        out = []
        for w in j.get("results") or []:
            auth = ", ".join((a.get("author") or {}).get("display_name", "")
                             for a in (w.get("authorships") or [])[:3])
            loc = ((w.get("primary_location") or {}).get("landing_page_url")
                   or w.get("doi") or "")
            out.append(_rec(
                "openalex", (w.get("id") or "").rsplit("/", 1)[-1], loc,
                w.get("display_name"), w.get("publication_year"),
                authors=auth,
                venue=(((w.get("primary_location") or {})
                        .get("source") or {}).get("display_name") or ""),
                cited_by=w.get("cited_by_count")))
        return out, None, {"total": j.get("meta", {}).get("count")}
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:180]}", {}


# ---------------- software ----------------

def github_repo_search(name: str, max_results: int = 3):
    """GitHub repository search + (first hit) latest release probe."""
    try:
        j = _http_json("https://api.github.com/search/repositories?"
                       + urllib.parse.urlencode(
                           {"q": name, "per_page": max_results}),
                       timeout=12)
        out = []
        for r in j.get("items") or []:
            out.append(_rec(
                "github", r.get("full_name"), r.get("html_url"),
                r.get("full_name"), (r.get("pushed_at") or "")[:10],
                description=(r.get("description") or "")[:300],
                stars=r.get("stargazers_count"),
                license=((r.get("license") or {}).get("spdx_id")
                         or "none-declared"),
                archived=bool(r.get("archived")),
                pushed_at=r.get("pushed_at"),
                language=r.get("language")))
        if out:
            try:
                rel = _http_json(
                    "https://api.github.com/repos/"
                    f"{out[0]['id']}/releases/latest", timeout=12)
                out[0]["latest_release"] = rel.get("tag_name")
                out[0]["latest_release_date"] = (
                    rel.get("published_at") or "")[:10]
            except Exception:
                out[0]["latest_release"] = None
        return out, None, {"total": j.get("total_count")}
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:180]}", {}


# -------- version-chain probes (M5 PART 4.2) --------
# These use GitHub CORE requests (unauthenticated budget 60/hour per
# host), so versions.py spends them through a measured budget and
# records 'skipped (budget)' - never a fake 'unknown' - when the
# budget is exhausted. The rate_limit probe itself is free.

def github_rate_limit():
    """Free probe of the unauthenticated core budget (does not count
    against it). Returns {'core': int|None, 'reset_utc': 'HH:MM',
    'checked_at': ...}."""
    from .store import now as _now
    out = {"core": None, "reset_utc": None, "checked_at": _now()}
    try:
        j = _http_json("https://api.github.com/rate_limit", timeout=10)
        core = (j.get("resources") or {}).get("core") or {}
        out["core"] = core.get("remaining")
        ts = core.get("reset")
        if ts:
            import time as _t
            out["reset_utc"] = _t.strftime(
                "%Y-%m-%d %H:%M UTC", _t.gmtime(int(ts)))
    except Exception:
        pass
    return out


def github_releases_tags(full_name: str) -> dict:
    """Releases list (INCLUDING pre-releases, unlike /releases/latest)
    + tag list for one identified repository."""
    out = {"releases": [], "tags": [], "error": None}
    try:
        rels = _http_json(
            "https://api.github.com/repos/"
            f"{full_name}/releases?per_page=10", timeout=12)
        for r in rels if isinstance(rels, list) else []:
            out["releases"].append({
                "tag": r.get("tag_name"),
                "prerelease": bool(r.get("prerelease")),
                "published": r.get("published_at")})
    except urllib.error.HTTPError as e:
        # 404 on /releases means the repo has no releases AT ALL
        if e.code != 404:
            out["error"] = f"releases HTTP {e.code}"
    except Exception as e:
        out["error"] = f"releases {type(e).__name__}"
    try:
        tags = _http_json(
            "https://api.github.com/repos/"
            f"{full_name}/tags?per_page=10", timeout=12)
        out["tags"] = [t.get("name") for t in
                       (tags if isinstance(tags, list) else [])]
    except Exception as e:
        out["error"] = (out["error"] or "") + f" tags {type(e).__name__}"
    return out


def github_head_commit(full_name: str) -> dict:
    try:
        j = _http_json("https://api.github.com/repos/"
                       f"{full_name}/commits?per_page=1", timeout=12)
        c = (j or [{}])[0]
        return {"sha": c.get("sha"),
                "date": ((c.get("commit") or {}).get("committer")
                         or {}).get("date"),
                "url": c.get("html_url")}
    except Exception:
        return {}


_VERSION_FILES = ("pyproject.toml", "setup.py", "VERSION", "version.txt")


def github_version_file(full_name: str) -> dict:
    """Read candidate version files through the contents API (the only
    repository-file route reachable from this host - raw github was
    measured NOT reachable). Returns the first file whose content
    exposes a version, with the exact path as provenance."""
    import base64
    for fn in _VERSION_FILES:
        try:
            j = _http_json(
                "https://api.github.com/repos/"
                f"{full_name}/contents/{fn}", timeout=12)
            raw = base64.b64decode(j.get("content") or "").decode(
                "utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            return {"error": f"contents HTTP {e.code}"}
        except Exception:
            continue
        m = (re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', raw,
                       re.M)
             or re.search(r'^\s*__version__\s*=\s*["\']([^"\']+)["\']',
                          raw, re.M)
             or re.search(r"^\s*version:\s*[\"']?([\w.\-]+)", raw,
                          re.M)
             or (raw.strip().splitlines() or [""])[0].strip()
             if fn in ("VERSION", "version.txt") else None)
        if isinstance(m, str):
            m = re.match(r"[\w.\-]+", m)
        if m:
            return {"path": fn, "value": (m.group(1)
                                          if hasattr(m, "group")
                                          else str(m)).strip(),
                    "url": f"https://github.com/{full_name}/blob/HEAD/{fn}"}
    return {}


def anaconda_bioconda(name: str, max_results: int = 1):
    """bioconda channel via anaconda.org JSON API (measured reachable
    2026-11-16). Many CLI tools ship only here, not on PyPI."""
    slug = name.strip().lower().replace("_", "-")
    try:
        j = _http_json(f"https://api.anaconda.org/package/bioconda/"
                       f"{urllib.parse.quote(slug)}", timeout=12)
        versions = sorted((j.get("versions") or []), reverse=True)
        return [_rec("bioconda", slug,
                     f"https://anaconda.org/bioconda/{slug}",
                     j.get("name"), None,
                     version=j.get("latest_version") or
                             (versions[0] if versions else None),
                     versions=versions[:5],
                     license=j.get("license") or "Not verified")], \
            None, {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return [], "not-on-bioconda", {}
        return [], f"HTTP {e.code}", {}
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:180]}", {}


def pmc_fulltext(pmcid: str, max_bytes: int = 600_000):
    """Full-text XML via NCBI efetch db=pmc (open-access subset).
    Chosen because Europe PMC's fullTextXML sub-endpoint is NOT
    reachable from this host (measured: 404 even for the
    documentation's canonical example). Returns (text, error)."""
    pid = re.sub(r"(?i)^pmc", "", str(pmcid or ""))
    if not pid.isdigit():
        return None, "no PMCID on the citation"
    try:
        req = urllib.request.Request(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
            f"efetch.fcgi?db=pmc&id={pid}&retmode=xml",
            headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(max_bytes).decode("utf-8", "replace")
        if "<article" not in raw and "No PMC Records Found" in raw:
            return None, "full text not in the open-access subset"
        if "<article" not in raw:
            return None, "efetch returned no article XML for this PMCID"
        return raw, None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:150]}"


def pypi_package(name: str, max_results: int = 1):
    """PyPI JSON API: real current version + license fields."""
    slug = name.strip().lower().replace(" ", "-").replace("_", "-")
    try:
        j = _http_json(f"https://pypi.org/pypi/{urllib.parse.quote(slug)}"
                       "/json", timeout=12)
        info = j.get("info") or {}
        lic = info.get("license") or ""
        if not lic or lic in ("UNKNOWN", "None"):
            lic = next((c.split("::")[-1].strip()
                        for c in info.get("classifiers", [])
                        if c.startswith("License ::")), "")
        rels = sorted((j.get("releases") or {}).keys(), reverse=True)
        return [_rec("pypi", slug, f"https://pypi.org/project/{slug}/",
                     info.get("name"), None,
                     version=info.get("version"),
                     license=lic or "Not verified",
                     requires_python=info.get("requires_python"),
                     summary=(info.get("summary") or "")[:300],
                     releases=len(rels))], None, {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return [], "not-on-pypi", {}
        return [], f"HTTP {e.code}", {}
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:180]}", {}


def biotools_search(name: str, max_results: int = 3):
    """bio.tools registry search: existence + official link + license."""
    try:
        j = _http_json("https://bio.tools/api/t?"
                       + urllib.parse.urlencode(
                           {"q": name, "format": "json",
                            "page_size": max_results}), timeout=12)
        out = []
        for r in j.get("list") or []:
            if not (r.get("toolID") or "").strip():
                # a registry record without an id is not evidence of
                # a tool (measured 2026-11-17: bio.tools returned an
                # id-less record that made the virus string "CoV-2"
                # look "found on bio.tools" + registry-verified)
                continue
            lic = ((r.get("licence") or {}) if isinstance(
                r.get("licence"), dict) else {})
            out.append(_rec(
                "biotools", r.get("toolID"), r.get("url")
                or f"https://bio.tools/{r.get('toolID')}",
                r.get("name"), None,
                summary=(r.get("summary") or "")[:300],
                license=lic.get("name") or "Not verified"))
        return out, None, {"total": j.get("total")}
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:180]}", {}


# ---------------- reachability probe (reported, never guessed) -------

ALL_ADAPTERS = {
    "pubmed": pubmed_search,
    "europepmc": europepmc_search,
    "openalex": openalex_search,
    "github": github_repo_search,
    "pypi": pypi_package,
    "biotools": biotools_search,
}

_PROBE_QUERY = {
    "pubmed": "neoantigen prediction",
    "europepmc": "neoantigen prediction",
    "openalex": "neoantigen prediction",
    "github": "MHCflurry",
    "pypi": "mhcnuggets",
    "biotools": "netmhcpan",
}


def probe(timeout: int = 10) -> dict:
    """One cheap live request per source; returns MEASURED availability
    (never assumed)."""
    import time as _t
    from .store import now as _now
    out = {}
    for name, fn in ALL_ADAPTERS.items():
        t0 = _t.time()
        try:
            recs, err, _meta = fn(_PROBE_QUERY[name], 1)
        except Exception as e:
            recs, err = [], f"{type(e).__name__}: {str(e)[:180]}"
        out[name] = {"reachable": bool(recs),
                     "error": err,
                     "latency_ms": int((_t.time() - t0) * 1000),
                     "sample_records": len(recs),
                     "checked_at": _now()}
    return out
