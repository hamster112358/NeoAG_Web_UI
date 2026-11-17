# Scientific reliability: root-cause analysis of the Purity/CNV review

Run audited: res-20261117-133053-bd0205 (engine live-retrieval-2026.11-m6-r1,
status completed, DECISION-READY REVIEW banner). The earlier
res-20261117-103138-d1247b supplies the negative-candidate cases.
Every "observed" line below is read verbatim from the run's stored
report JSON (data/state.sqlite3, research table) or reproduced
deterministically from the current code by the commands listed in
section 8. Code references are to the tree at commit 7877996.

This file answers the six mandated findings (1.1-1.6). For each: the
original incorrect statement, the source evidence that misled the
engine, the root cause (file and line), the correct scientific
interpretation, the general fix (not a name-specific patch), and the
regression test that pins it.

---------------------------------------------------------------------------
## 1.1 PureCN excluded as "irrelevant"

Observed (stored candidate row):
  eligibility: "irrelevant" with screen_confidence: "high"
  reason: "the retrieved scientific description contains none of this
  module's domain vocabulary - it does not address the same biological
  problem"
in the same run whose evidence table quotes papers measuring PureCN's
sensitivity/specificity for copy-number calling, and whose own row
records "named in retrieved literature".

Source evidence the engine actually had:
- GitHub identity was CORRECT (verification_hits.github =
  https://github.com/lima1/PureCN).
- Two citations name PureCN; the benchmark paper ("Panel-Specific
  Optimization and Benchmarking of Copy-Number Callers...",
  PMID 42938189) contains in its ABSTRACT the sentence "Native
  standalone PureCN yielded 16.9% sensitivity and 95.5% specificity
  at absCN >= 14" - which the engine extracted as performance
  evidence for PureCN while simultaneously declaring PureCN does not
  address the module's biological problem.

Root cause (three layers, all general):
1. What was screened (research.py:609-613): the eligibility screen
   sees ONLY the GitHub description + bio.tools/PyPI blurbs + the
   TITLES of up to 4 naming citations. Abstracts are never part of
   the screened text, so a tool whose domain only shows in its
   papers' abstracts is screened against a near-empty blurb (lima1/
   PureCN has no GitHub description; PyPI/bioconda/bio.tools have no
   PureCN record - it is a Bioconductor package and there is no
   Bioconductor adapter).
2. A dead vocabulary term made the miss deterministic
   (science.py:99-107 vs science.py:60-61): the purity_cnv domain
   list contains "copy.number", written as if _hit() were a regex.
   _hit() is a literal substring test, so "copy.number" can never
   match naturally written "copy-number" or "copy number". The cited
   paper's title says "Copy-Number Callers" - which matches NO term
   in the list ("cnv", "ploidy", "aneuploid", "tumour purity",
   "purity estimat", "copy number alteration" all absent).
3. Absence treated as disproof (science.py:265-281): when a non-empty
   blurb contains no domain term and no complement term, the screen
   returns eligibility "irrelevant" with confidence "high". Absence
   of retrieved keywords is an EVIDENCE gap about the candidate, not
   positive evidence of a task mismatch - the engine's own header
   comment (science.py:1-12) states candidates are judged only on
   retrieved text, but the code converts "not retrieved" into
   "does not address the same biological problem" and stamps it high
   confidence.

Correct interpretation: PureCN is a recognized purity/ploidy/CNV
method (Bioconductor; the retrieved benchmark papers evaluate it
against ClinCNV, CNVkit, PURPLE on the exact endpoint this module
consumes). The correct questions at this stage are: which quantities
it estimates (purity, ploidy, integer copy number, per-segment
multiplicity), its inputs (BAF/coverage from a tumor(-normal) VCF or
pileup), technologies (WES/WGS/targeted panel), coverage of NeoAG's
problem, and replace-vs-complement conditions with downstream
compatibility. The right state before those questions are answered
is "relevance not yet established - investigate", never
"irrelevant - high confidence".

General fix (implemented under tasks #183/#184, no name lists):
- relevance is decided over ALL text retrieved about a candidate
  (blurbs + titles + ABSTRACTS + full text when available);
- the only paths to "irrelevant" are POSITIVE evidence: an off-domain
  description, or a verified identity whose function contradicts the
  task endpoints;
- vocabulary absence routes to a research-state machine
  (discovered -> identity-verified -> relevance-established -> ...)
  that issues targeted follow-up queries ("<tool> <endpoint>" etc.)
  and records unresolved states honestly;
- task vocabulary matching becomes explicit alternation
  ("copy[ -]?number" style regex class, applied uniformly), and a
  profile term that cannot match real text is a bug the ontology
  loader can test for.

Regression tests (tests/science_eval):
- a candidate whose GitHub description is empty but whose naming
  abstract states its task function must NOT be excluded; its state
  must be at least "relevance-established" with the supporting span;
- every profile domain term must match at least one corpus string in
  the ontology test (kills dead "copy.number"-class terms for every
  task, not just purity_cnv);
- exclusion may only be produced by a rule branch whose reason cites
  positive text, asserted by the gate that scans exclusion reasons
  for the absence-of-keyword pattern.

---------------------------------------------------------------------------
## 1.2 HapASeg under-investigated despite a 2026 method paper

Observed: eligibility "insufficient information to classify",
confidence "low", listed under insufficient; its version chain shows
every GitHub step "skipped (GitHub API budget exhausted)" (the run
STARTED with core budget 0 - provenance.github_budget_at_start) and
its evidence row says "names the tool; no explicit metric in the
retrieved abstract". Its own primary reference is the HapASeg method
paper (Genome Biology 2026, PMID 41689134, DOI
10.1186/s13059-026-03971-w), and its GitHub identity was correctly
found (getzlab/HapASeg).

Root cause:
1. The investigation budget is fixed and front-loaded
   (research.py:289 "ft_budget = {n: 6}" full-text calls shared
   across ALL candidates; no per-candidate priority). The paper
   entitled "Haplotype-aware segmentation with HapASeg increases
   accuracy..." was left at abstract level.
2. Nothing reacts to a budget-blocked step. When releases/tags were
   skipped, no alternative route was scheduled (Bioconductor-style
   pages via Europe PMC funder/venue metadata, the paper's
   availability statement, git protocol-free routes, or a later
   retry once the GitHub budget resets - the run knew the reset time
   and finished 23 minutes after it).
3. The screen has no notion of research depth. A tool with a naming
   METHOD paper (the paper title itself asserts the function and the
   endpoint: homolog-specific somatic CNAs) can stall in "insufficient
   information" because the blurb lacked a software keyword
   (science.py:323-330) - the paper's existence is itself evidence
   of an implementable method, and that evidence class is not
   consulted by the screen.

Correct interpretation: a 2026 peer-reviewed method paper that names
the tool, describes the algorithm, and evaluates it against baselines
establishes (a) it is a runnable method, (b) its endpoint
(algorithm-level segmentation accuracy for homolog-specific CNAs),
and (c) its relationship to this task (a segmentation-stage method
that COMPLEMENTS purity/CNV callers rather than directly replacing
the consensus group). The engine must reach that state by going
deeper - availability statement, supplementary, comparative studies,
code availability - and must record the budget-blocked steps as
pending work items with their reset time, not as terminal gaps.

General fix: research-state machine (task #184) with importance-
weighted follow-up: a naming method paper raises investigation
priority; unresolved states at report time are enumerated as
explicit "open investigations" with what would resolve them; skipped
steps record their unblock condition (budget reset time).

Regression tests:
- a candidate with a naming method paper but empty registry metadata
  must reach "relevance-established" via the abstract and be routed
  by the endpoint relationship (here: complementary at the
  segmentation subtask), never a terminal "insufficient";
- when a chain step is budget-skipped, the record must carry the
  reset time and the report's open-investigations list;
- generalization corpus: an HLA-typing, fusion and presentation tool
  each receive the same follow-up treatment (no purity-specific
  logic).

---------------------------------------------------------------------------
## 1.3 PURPLE bound to the wrong project (PyPI "purple", PurpleLlama)

Observed in the baseline row for purple:
  version_chain.github_repo: "meta-llama/PurpleLlama"
  verified_version: "1.0.1 (stable; PyPI current version; ...)"
  availability: "found on GitHub, PyPI"
live reproduction (2026-11-17): GitHub search q=purple returns
meta-llama/PurpleLlama (4483 stars), PurpleAdmin template,
PurpleMine2 - no bioinformatics repository at all. The real PURPLE is
the Hartwig Medical Foundation copy-number/purity/ploidy caller
inside the hmftools monorepo
(https://github.com/hartwigmedical/hmftools/tree/master/purple),
which GitHub keyword search for "purple" cannot return because the
REPOSITORY name contains no "purple".

Root cause:
1. Identity by first search hit (sources.py:162-191 + research.py:
   489-515): github_repo_search(name) with q = the bare name; the
   "is this record about the candidate?" filter (_about,
   research.py:507-510) is a substring test on the repo full-name,
   which PurpleLlama passes. One hit is accepted as identity; no
   corroboration (org, function, ecosystem, publication, install
   route) is required or even attempted.
2. No component recognition: a tool that lives as a subdirectory of
   a monorepo is invisible to repo-name search; the engine never
   resolves identity through literature URLs, organization names, or
   bioconda/bio.tools records that point at hmftools.
3. Registry adoption without identity: pypi_package("purple") is a
   definite PyPI hit (the filter research.py:512 accepts exact-slug
   matches blindly), so the report states PURPLE is available on
   PyPI at 1.0.1 - true of a DIFFERENT project. PyPI was then first
   in _pick_stable's fallback order (versions.py:278), so the wrong
   project's version became the "verified version" of a NeoAG
   baseline tool.

Correct interpretation: repository presence, name equality, and a
registry record are SUGGESTIONS of identity. An identity verdict
requires corroboration across independent axes (organization matches
the literature, function matches the task, install docs match the
ecosystem the deployment uses), and disagreement must produce
"identity contested - N candidate projects" rather than a silent
pick. For PURPLE specifically the corroborating facts were already in
this deployment: the baseline evidence cites hmftools-style
required_tool_groups membership, and /data1/neoag/references/data/
hmf/purple_reference exists - Hartwig reference data, incompatible
with any "purple" PyPI package or Llama model.

General fix (task #183): canonical identity records with a
corroboration score and an explicit "candidates considered / why
chosen or rejected" appendix; search disambiguation using the task's
domain vocabulary in the query ("purple copy number"), the naming
paper's venue/org metadata, and component recognition (monorepo
contents check); identity below threshold blocks any version or
performance attribution ("a number needs an identity first").

Regression tests:
- the corpus contains a deliberately ambiguous name (this class, not
  this name) whose top GitHub hit is a high-star unrelated project;
  the identity stage must mark it contested and must not attach the
  wrong project's version/license/availability;
- a component-of-monorepo fixture resolves via the literature-URL /
  registry cross-reference path;
- assertion that NO candidate row can carry a version whose source
  project differs from the identity-verified project (verified by
  re-deriving the chain from identity in the independent verifier).

---------------------------------------------------------------------------
## 1.4 ASCAT version chain: "2.8.1 stable PyPI" while the deployment pins ASCAT 3.2.0 (Bioconda)

Observed: ascat verified_version "2.8.1 (stable; PyPI current
version...)"; the SAME row's bioconda step lists ['3.2.0','3.1.1',
'3.0.0','2.5.2','2.5.1'] and marks them non-authoritative; pinned_version
null. Deployment truth (read-only, this host): the configured repo's
conda/env.neoag-ascat.yml pins ascat=3.2.0 from bioconda (comment:
"ASCAT 3.2.0 (linux-64; current Bioconda build for R 4.4)") and both
neoag-ascat and neoag-ascat-v3 envs report ASCAT 3.2.0 via
packageVersion. The baseline row also reads installed_version
"n/a - not part of this deployment" for ALL FOUR baseline tools,
which is false - facets (0.6.2), sequenza (3.0.0) and ASCAT (3.2.0)
are installed conda envs on this machine.

Root cause:
1. Conda pin parser blind to the actual pin syntax (inventory.py:
   91-94): the regex requires name==version, conda ymls use name=
   version, so pins for ascat/facets/sequenza are all silently
   dropped (reproduced: _conda_pins returns None for all three).
   pinned_version null is therefore a parser failure, not a repo
   fact.
2. Registry precedence ignores the deployment context (versions.py:
   278-283): when GitHub steps are budget-skipped, _pick_stable
   prefers PyPI over bioconda regardless of which ecosystem the
   deployment installs from, and regardless of which value is
   newer - the engine even recorded the newer bioconda list in the
   same chain and printed the older, wrong-ecosystem number as
   "(stable)".
3. Deployment baseline never probed: versions.installed_version() is
   gated on the literal origin string "configured NeoAG workflow"
   (research.py:640-644), which no baseline candidate carries
   (baseline origins read "current NeoAG baseline (runner script
   ..."), so the probe never ran; its default root
   /data1/neoag/tools (versions.py:316) is also not where this
   deployment keeps envs.
4. Baseline context collapsed into one class (PART 3):
   inventory identifies tools from the repo's EXAMPLE workflow TOML
   and install scripts (repo-declared level) and the report calls
   that "current NeoAG baseline" without distinguishing repo vs
   deployment vs this-run baseline, or intended consensus config
   (min_successful=2 of facets/sequenza/purple) vs what actually ran.

Correct interpretation: "which version does NeoAG use" has three
different answers that must all be recorded - repo declaration
(yml pins: ascat=3.2.0, facets via env, sequenza via env), deployment
install (conda envs: ASCAT 3.2.0, facets 0.6.2, sequenza 3.0.0), and
per-run usage (this demo ran no tools, so nothing contributed).
PyPI's ascat 2.8.1 is a different distribution channel of (probably)
the same project and may not be presented as THE version; multiple
implementations/distributions of one tool are a first-class concept.

General fix (task #183): baseline evidence levels 1-7 attached per
tool with file/env evidence; fixed pin parser; deployed-env probe
(read-only) for installed versions; ecosystem-aware version verdicts
(declare which channel, prefer the one the deployment uses, show all
discovered versions per channel).

Regression tests:
- yml fixtures with =, == and unpinned forms must all parse to the
  correct pin;
- an ecosystem-preference test: when the deployment env is bioconda,
  the "stable" verdict must come from bioconda (or say why not);
- installed-version probing must fire for every baseline candidate
  and report the real env evidence string;
- report contract: baseline rows must carry level (declared/
  enabled/executed) - asserted by schema test.

---------------------------------------------------------------------------
## 1.5 Performance-metric attribution errors

Observed rows in this run (verbatim from the report):
- purple, metric "sensitivity", value "95.5%" - quote is "Native
  standalone PureCN yielded 16.9% sensitivity and 95.5% specificity
  at absCN >= 14;" (wrong tool, wrong metric for that number);
- purple, "sensitivity" "91.0%" - quote gives 91.0% as CNVkit's
  SPECIFICITY;
- purple, "sensitivity" "67.8%" - quote attributes 67.8% to ClinCNV;
  PURPLE's actual numbers in that sentence are 58.6%/92.3%;
- PureCN, "sensitivity" "95.5%" (specificity's number) and
  "sensitivity" "95%" - the 95% is the "Wilson 95% CI" coverage
  level inside CNVkit's sentence;
- the same quote appears 2-3x per candidate (abstract route +
  full-text route via a differently-titled "full text via efetch"
  pseudo-citation), once flagged independent and once not;
- sequenza's published_benchmark_evidence is the string "Not
  verified" JSON-encoded as a character array (contract violation:
  the field is a list OR a string, and consumers iterate it).

Root cause (evidence.py:26-121, reproduced deterministically):
1. METRIC_RE pairs the FIRST metric word in a sentence with the
   FIRST number AFTER it. English states metrics both as
   "sensitivity of 16.9%" and "16.9% sensitivity"; for the PureCN
   sentence the first metric word (sensitivity) is followed by
   95.5%, so sensitivity gets specificity's number. Reproduced:
   metric='sensitivity' value='95.5%'.
2. One row per metric-bearing sentence, one subject per claim: the
   multi-tool sentence is admitted for every candidate whose name
   appears ANYWHERE in it (name gate evidence.py:75-81 widens to a
   +-1-sentence window), then the single regex match binds whichever
   (metric, number) pair is lexically first - which belongs to a
   NEIGHBOUR tool (67.8% -> purple). No subject-binding step exists:
   the extractor never asks "which tool is this number the subject
   of".
3. CI text is not excluded: numbers inside "(Wilson 95% CI, ...)"
   match the metric-number pattern (reproduced value='95%').
4. Fraction pairs "67.8%/85.1%" bind metric words and values 1:1
   positionally nowhere - the parser has no concept of a
   (sens/spec) value pair.
5. Duplicate observations: citations dedup by DOI/PMCID/title runs
   BEFORE the full-text stage (research.py:350-371), and full-text
   rows get a synthetic citation titled "full text via efetch..."
   (research.py:574-586), so the same paper yields two "sources";
   independence is then computed from the pseudo-title
   (COMP_CUE_RE over the title), so the same observation appears
   flagged independent AND non-independent.
6. Type contract: published_benchmark_evidence falls back to the
   bare string "Not verified" (research.py:712), which quality
   gate 4 (science.py:620-623) then compares by identity - the
   sequenza row is the artifact.

The context gate (context_established False for every row here) did
its part - none of these numbers entered a comparison row - but the
brief is explicit: gate rejection does not excuse extraction errors.
Rows carrying wrong (tool, metric, value) triples are published in
the appendix and rendered in the HTML ("purple sensitivity 95.5%"),
misinforming the reader and corrupting any later reuse.

Correct interpretation: extraction must produce COMPLETE
experimental claims - "under condition X, tool A achieved metric M =
value V (CI) on dataset D vs comparator B" - with each measurement
bound to its subject, split per metric, CI spans separated from
metric values, pairs expanded, and each row keeping exact source
spans. A second, independently written verifier must re-derive
(subject, metric, value) from the same span and reject rows where
the two disagree or where the subject is ambiguous.

General fix: see tasks #185 (claim extraction + independent
verification + publication-identity dedup + always-list contract) -
all rules are syntactic/scientific classes (subject binding, CI
spans, value pairs, per-claim one subject), no tool names.

Regression tests (mandatory corpus cases 5-10): the two sentences
above (genericized analogues kept as fixtures) plus CI bounds,
AUPRC-vs-precision, fraction pairs, multi-tool paragraphs, and
mentioned-but-not-evaluated sentences; scorer requires exact
(tool, metric, value, ci, subject-span) match, and the verifier
test asserts adversarially-misbound rows are rejected.

---------------------------------------------------------------------------
## 1.6 "DECISION-READY REVIEW" with nothing screened in and comparability never established

Observed: decision_ready true, review_class "decision-ready review",
headline "NO tool replacement is justified for any researched module
(1x Retain current tool)... Where zero suitable alternatives were
found, that is the finding", evidence_gaps [] - while the same
report: excluded 5 of 10 candidates (PureCN wrongly, HapASeg
unresolved), has task_comparisons[0].rows == [], and its own
gate list reads "recommendations consistent with evidence - passed
by construction".

Root cause (three layers):
1. The recommendation ladder treats a screening vacuum as evidence of
   retention (science.py:538-546): if the baseline is identified and
   no candidate survived screening, the output is "Retain current
   tool" with action "no action". The state where the screen itself
   over-excluded (1.1/1.2 above) is indistinguishable, in this
   vocabulary, from a genuinely explored search that found no
   alternative. Absence of evidence is being consumed as evidence of
   nothing-to-do.
2. Gates are self-certifying (science.py:598-635): gate 5 "passed"
   is the literal constant True; gate 4 only checks that at least
   one candidate's evidence field != "Not verified" (it passes even
   when every row is misattributed); gate 2 checks that eligibility
   LABELS EXIST, not that exclusions are justified; gate 6 is a
   constant True. decision_ready = all(gates) (science.py:638-639)
   plus no hard retrieval failures (research.py:1102-1103) - so a
   run whose entire alternative space was excluded by the 1.1 bug
   sails through.
3. The module decision is computed with gates_passed hardcoded True
   (research.py:898-903) because the gates run later in the
   pipeline; the ladder therefore cannot demote itself even when a
   future gate fails. And nothing anywhere asks the comparability
   question the decision silently assumes: "Retain" on performance
   grounds requires knowing what the CURRENT tool achieves under
   comparable conditions - here the baseline's own context-
   established numbers are zero.

Correct interpretation: this run's honest output is
"Review incomplete" (new vocabulary): relevant candidates were
identified (PureCN, HapASeg) but their relevance/function/performance
states are unresolved, no direct alternative entered comparison, no
comparable evidence exists on either side, and the decisive claims
underpinning "retain" have not been verified to any standard. Query
completion is not scientific completion.

General fix (tasks #186):
- five-label vocabulary: Evidence supports retaining current method /
  No justified change identified / Promising candidate for further
  evaluation / Recommend targeted benchmark / Review incomplete -
  each with a required evidence condition that CAN fail
  ("No justified change identified" additionally requires that the
  alternative space was actually resolved, i.e. every identified
  relevant candidate reached a terminal state);
- eight scientific verification dimensions (identity, relevance,
  baseline, evidence, comparability, recommendation, coverage,
  provenance integrity) implemented as checks over the evidence
  objects themselves (do any exclusions rest on absence-of-keyword
  reasoning? does any decision-ready module have zero verified
  decisive claims? is any cited paper double-counted?), mechanical
  and scientific validation kept separate;
- decision_ready derived AFTER the real gates, with the ladder re-
  evaluated against them (no constant True anywhere); preliminary
  saving stays allowed - gate failure blocks the DECISION label, not
  the report.

Regression tests: the stored regression report itself is the
fixture's source: fed through the new synthesis, it must yield
"Review incomplete" (or equivalent) and decision_ready False;
mandatory corpus cases 11-12 (installed-but-not-executed is not a
contributed baseline; no-comparable-evidence is not superiority) plus
a synthetic "zero alternatives because screen over-excluded" case the
gates must catch.

---------------------------------------------------------------------------
## 2. Cross-cutting facts established by this audit

- The run started with GitHub unauthenticated budget core=0 and
  reset 05:58 UTC; it finished 13:53 local (05:53 UTC) - five
  minutes before the reset. Budget-blocked identity steps were
  terminal for the run even though the reset was imminent.
- biotools reported ok:10 (reachable on 2026-11-17) - the earlier
  host-memory note that bio.tools is unreachable is outdated for the
  probe path; adapters measure reachability per run and that is
  correct.
- dedup_removed was 0 yet the same paper reached the evidence table
  through europepmc and through the efetch full-text pseudo-citation
  (see 1.5 point 5).
- No Bioconductor adapter exists, while four of the module's baseline
  tools (facets, sequenza, PureCN, ASCAT) are Bioconductor/Bioconda
  R packages. Consequence chains visible in this report: PureCN
  screened on a near-empty blurb (1.1), facets resolved to Google's
  PAIR-code/facets ML-visualization library with PyPI version 1.1.0
  (identity collision identical in kind to PURPLE's, and this one hit
  a BASELINE row), sequenza's version left "Not verified".
- The example TOML parsed for baselines is the repo's example file;
  the report calls its contents "current NeoAG baseline" with no
  repo/deployment/run distinction (PART 3 of the fix addresses this).

## 3. Commands that reproduce every claim in this file

- Stored rows: sqlite read-only on data/state.sqlite3 research
  table, id res-20261117-133053-bd0205 (report_json).
- Metric-binding repro:
  python3 -c 'from backend import evidence; \
  q="Native standalone PureCN yielded 16.9% sensitivity and 95.5% \
  specificity at absCN \u2265 14;"; print(evidence.METRIC_RE.search(q).groups())'
  -> ('sensitivity', '95.5%'); likewise the ClinCNV/PURPLE sentence
  -> ('sensitivity', '67.8%') and the Wilson-CI sentence -> ('...',
  '95%').
- Conda-pin repro: inventory._conda_pins on the configured repo
  returns None for ascat/facets/sequenza despite env.neoag-ascat.yml
  containing "ascat=3.2.0".
- Identity repro: GitHub search q=purple first hits
  meta-llama/PurpleLlama (measured 2026-11-17).
- Deployment versions: packageVersion in the neoag-facets /
  neoag-sequenza / neoag-ascat(-v3) conda envs -> 0.6.2 / 3.0.0 /
  3.2.0 (read-only).
