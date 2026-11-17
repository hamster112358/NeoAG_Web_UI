# Discovery engine v2 (m8): scientific architecture and data contracts

Engine version: live-retrieval-2026.11-m8-r1.
This is the design contract the Milestone-8 scientific reliability
work implements. It is written first, then enforced by tests; code
that cannot satisfy a contract is wrong, not the contract.

Principles (each traced to the root-cause audit
docs/SCIENTIFIC_ROOT_CAUSE_2026-11-17.md):
- a name is never an identity (1.3);
- absence of a retrieved keyword is never evidence of irrelevance
  (1.1);
- a number without a bound subject/metric/condition is never a
  performance claim (1.5);
- repository presence is never execution, config is never deployment
  (1.4);
- absence of evidence is never superiority or justified retention
  (1.6);
- decision labels are earned by verified claims, not by the query
  pipeline finishing;
- no fix is a name list - every rule is a general scientific or
  syntactic class, and the evaluation corpus (tests/science_eval)
  is built independently of the heuristics it scores.

---------------------------------------------------------------------------
## 1. Pipeline stages and module ownership (PART 9)

Ten stages, each a function boundary with an explicit dict contract
(serializable, versioned by key names). The report renderer only
displays what earlier stages produced - it never invents, repairs or
re-derives evidence.

 S1 retrieval-normalization   sources.py + research._s1_*
    out: Citation = {cid, canonical: {doi|pmcid|pmid|arxiv|title_key},
         title, year, venue, source_first_seen, urls[], abstract,
         retrieved_at}
    - canonical id precedence DOI > PMCID > PMID > arXiv > normalized
      title; merging a later sighting of the SAME canonical id into
      the earlier Citation is mandatory (fixes 1.5-point-5); the
      full-text route attaches text to an existing Citation, it may
      not mint a pseudo-citation.

 S2 task-definition           ontology.py
    out: TaskDef = {target_id, question, endpoints[] (each: name,
         output_quantity, ground_truth_kind, metrics[]), modalities,
         preprocessing_needs, outputs, consumers, domain_re (compiled),
         off_domain_re, complements[]}
    - matchers are compiled regexes with explicit alternation over
      word/punct variants ("copy[ _-]?number"); loader invariant:
      every declared term matches at least one English corpus string
      (kills dead terms like 1.1-point-2 for all tasks);
    - Purity/CNV endpoint inventory (the ontology this task needs):
      tumour purity (MAE/CCC vs truth), ploidy (exact/within-one),
      total copy number per segment, allele-specific CN / minor
      (integer exactness), segmentation accuracy (boundary distance /
      region concordance), LOH events, subclonal CN structure,
      per-variant multiplicity - each mapped to the NeoAG consumers
      that read it (CCF/cellularity, purity gating, HLA-LOH, LOH-
      aware filtering).

 S3 identity-resolution       identity.py
    in : candidate name + TaskDef + Citations + budget/probes
    out: IdentityRecord = {query_name, status: one of
         unresolved|contested|resolved, considered[] (each: project,
         org, kind, axes[] evidence, score), chosen (nullable),
         corroboration_axes[], rejected[] (with reasons),
         checked_at}
    - evidence AXES: literature URL/organization, registry records
      (PyPI, bioconda, Bioconductor, bio.tools), GitHub repo
      (description domain-match), installation docs, naming paper;
    - resolution requires corroboration on >= 2 independent axes,
      and a domain-match of the project description against the
      TaskDef endpoints; the first search hit alone is NEVER enough
      (fixes 1.3);
    - component recognition: literature URLs naming a monorepo +
      contents listing containing the candidate name resolves the
      component as part of that project (PURPLE-within-hmftools is
      the audit instance, the test is a generic analogue);
    - a version, license or performance number may only be attached
      to a project that IS the resolved identity (verifier re-checks
      the chain provenance).

 S4 relevance-assessment      science.assess_relevance (rewritten)
    in : candidate + TaskDef + ALL retrieved text about it (blurbs,
         titles, abstracts, full text) + IdentityRecord
    out: RelevanceDecision = {relationship: one of direct-alternative
         | partial-overlap | complementary | upstream | downstream |
         wrapper | alternative-implementation | unrelated |
         unresolved, basis[] ({claim, text_span, doc_cid}),
         research_state, followups[]}
    - "unrelated" ONLY from a positive mismatch: an off-domain
      description, or a resolved identity whose documented function
      excludes the task's endpoints; keyword absence produces
      "unresolved" + concrete followup queries ("<tool> <endpoint
      quantity>", "<tool> official implementation", "<tool> benchmark
      <peer>") - never a terminal exclusion (fixes 1.1);
    - research states (PART 4): discovered -> identity-verified ->
      relevance-established -> function-characterized ->
      availability-verified -> performance-retrieved -> evidence-
      compared -> compatibility-assessed; transitions carry the
      evidence object that licensed them; naming method paper counts
      as function evidence (fixes 1.2).

 S5 baseline-context          baselines.py (+ inventory.py fixes)
    out: BaselineContext per stage = {tools[]: {tool,
         identity: IdentityRecord, level (see below), evidence[]
         {file/env, detail}, pinned_declared, installed (env probe),
         ran_in_this_run: always false in a literature review}}
    - levels (PART 3): 1 present-in-literature, 2 supported (code
      can run it), 3 declared-config (workflow/config names it),
      4 enabled-profile, 5 planner-selected, 6 executed, 7
      contributed-to-output; intended consensus (e.g. min_successful
      2-of-3) recorded separately from achieved consensus - a demo
      run's honest ceiling is level 3;
    - the conda-pin parser must handle name=X.Y, name==X.Y and
      unpinned forms (1.4-point-1, reproduced); the deployed-env
      probe reads conda-meta/dist-info under the configured env
      roots read-only.

 S6 claim-extraction          claims.py
    out: Measurement = {subject, subject_span, metric, metric_span,
         value, value_span, ci (nullable, span), direction
         (value-first|metric-first|pair), dataset, condition,
         comparator, doc_cid, quote_span, kind: point|range|ci-
         level}
    - CI-coverage numbers ("95% CI", Wilson/Beta spans) are masked
         before matching and never become values (1.5-point-3);
    - value pairs "67.8%/85.1%" bind to ordered metric pairs
         "sensitivity/specificity" positionally (1.5-point-4);
    - a sentence may carry N subjects x M measurements; each
         measurement's subject is bound by nearest-antecedent clause
         attachment with list segmentation on "whereas|while|and
         |, " boundaries; a measurement whose subject cannot be
         bound uniquely is emitted with subject=None,
         ambiguous=True and is EXCLUDED from any comparison while
         staying visible with its span (1.5-points-1/2).

 S7 claim-verification        claimverify.py (independent module,
    different parsing strategy - token scan + clause split - written
    without reusing claims.py regexes)
    out: Verdict per Measurement = {confirm | correct(new binding,
         reason) | reject(reason: subject-ambiguous | metric-mismatch
         | value-is-ci-level | wrong-scale | cross-tool-contamination
         | quoted-from-other-study)}
    - a measurement enters the report as "verified" only with a
      confirm (or an accepted correction) verdict; extraction and
      verification disagreeing on subject/metric/value always
      resolves to reject - the more conservative answer (1.5).

 S8 comparability-assessment  comparability (in science.py)
    out: ComparabilityClass per measurement pair: direct (same
         endpoint, modality, setting class) | indirect (different
         setting, same endpoint quantity) | descriptive (no
         comparable endpoint) | unverified; dimensions compared:
         WES/WGS/panel, tumor-only vs matched, FFPE, coverage,
         purity/ploidy range, ground-truth construction, absolute vs
         relative CN, total vs allele-specific, task focus
         (segmentation vs purity); not-sufficiently-documented
         dimensions keep the pair indirect, they never collapse it
         to direct (PART 6: no over-restriction - indirect evidence
         may still license a "promising candidate" label).

 S9 synthesis                 science.recommend_v2 + gates
    out: ModuleDecision = {label: one of the five (Evidence supports
         retaining current method | No justified change identified |
         Promising candidate for further evaluation | Recommend
         targeted benchmark | Review incomplete), conditions_met[],
         conditions_unmet[], unresolved_items[]}
    - "No justified change identified" additionally requires that
      every identified relevant candidate reached a terminal
      relevance state (fixes the 1.6 vacuum);
    - scientific gates (8 dimensions: identity, relevance, baseline,
      evidence, comparability, recommendation, coverage, provenance
      integrity) are REAL checks over the stage outputs (examples:
      no exclusion whose only basis is absence-of-keyword; no
      decision-ready module with zero verified decisive measurements;
      no citation counted twice under two pseudo-ids; every version
      string traceable to the resolved identity; no superiority text
      without a verified comparable pair). Mechanical checks
      (adapters answered, lists well-typed) stay separate from
      scientific ones. Gate failure demotes the module to "Review
      incomplete" and blocks decision_ready; saving the preliminary
      report stays allowed;
    - gates_passed is fed INTO the ladder (no constant True anywhere
      - 1.6-point-3).

 S10 report-generation        research._live_report + exporter
    - conclusion-first shape from M6/M7 is preserved (non-negotiable
      11: no template redesign); new content flows through existing
      generic lists (assessment.evidence_gaps, quality_gates,
      appendix tables) plus explicit new sections rendered as text
      where a slot exists; research_states and open_investigations
      are data, visible as such.

---------------------------------------------------------------------------
## 2. Type contracts (fixes provenance-integrity defects)

- published_benchmark_evidence: ALWAYS a list (never the string
  "Not verified" - 1.5-point-6); emptiness is [] +
  evidence_gaps entry.
- every registry/version/claim dict carries source url + checked_at;
  a value with no provenance is not emitted.
- citations carry one canonical id; every stage keys observations by
  (canonical_cid, quote_hash) so the same paper via two routes is
  one document with possibly several observations, and observation
  identity includes subject+metric+value spans (fixes duplicate
  independent/not-independent rows).

## 3. Resource intelligence (PART 9)

- query budget priority: identity corroboration for BASELINE tools
  first (the report's claims about what NeoAG uses are the ones the
  deployment will act on), then naming papers for unresolved-
  relevance candidates, then generic sweeps;
- GitHub core budget: measured at start, spent from the highest-
  priority need; when budget hits zero while unresolved identities
  remain, the run records pending steps WITH the reset time, and
  (in interactive research, not in tests) may wait once if the
  reset is within a bounded window;
- every adapter response cached per run keyed by canonical query,
  with retrieved_at - two stages asking the same question get the
  same timestamped record.

## 4. Fact classes the report must keep separate (PART 9)

ESTABLISHED (verified across sources), SOURCE-REPORTED (one source,
quoted with span), INDEPENDENT-COMPARISON (third-party measurement
with bound subject), SYSTEM-INFERENCE (engine derivation, labelled),
UNRESOLVED-HYPOTHESIS (open investigation with its resolving
question). The renderer prints the class tag with every claim.

## 5. What may NOT change (brief's non-negotiables, restated for
implementation)

No hardcoded corrections for PureCN/HapASeg/PURPLE/ASCAT or any
specific tool name; no UI/template redesign; no dataset downloads or
pipeline execution without explicit consent; the production NeoAG
pipeline and the frozen repo stay untouched; the evaluation corpus
may not be built from the same heuristics it measures.

---------------------------------------------------------------------------
## 6. Implementation status (2026-11-17, engine live-retrieval-2026.11-m8-r1)

Stages 1-10 are live in the retrieval path; the mapping to code:

  S1 research.py _canon_cid + citation merge in run_live
  S2 ontology.py     S3 identity.py      S5 baselines.py
  S4 science.assess_relevance (state ladder + follow-ups)
  S6 claims.py       S7 claimverify.py
  S8 science.compare_measurements
  S9 science.recommend_v2 / scientific_gates / decision_ready_v2
  S10 research._live_report / _module_decision / _task_comparisons

The stage prefixes S1..S9 from §1 were not adopted as function
names; the contract lives in the dict shapes and in
tests/verify_scientific.py (62 checks, offline, including a
faked-source end-to-end run of the whole chain).

Honest deltas against this document:

- S1's `_s1_*` prefix: not used (same contracts, plain names).
- §3 "may wait once when the GitHub reset is imminent": NOT
  implemented - budget exhaustion is recorded as a budget-skipped
  step with the reason, and the investigation stays open; no run
  ever blocks on a wall-clock wait.
- §3 query-budget priority ordering: implemented in the weaker form
  - baseline tools resolve identity through the deployment channel
  first (versions.resolve prefer=bioconda) and per-run caches
  (version chain, identity, documents) prevent duplicate spend; the
  full priority scheduler described above is not built.
- §4 fact-class tags: carried inside each evidence row's
  evidence_level string ("author-reported" / "independent claim,
  confirmed|corrected|rejected") rather than as a separate
  renderer-level tag; every comparison row additionally states its
  comparability class and note.
