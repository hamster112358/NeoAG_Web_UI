# Before/after evaluation (milestone 8 deliverable 5)

Seven dimensions, each with: the definition, the formula actually
computed, the independently reviewed expected answers, the BEFORE
value measured on run `res-20261117-133053-bd0205` (milestone-7
engine), and the AFTER value measured on the final milestone-8 run
`res-20261118-092034-96774e` (completed, 36 retrieval queries 0
failures, 35 unique literature records, 21 citations, 10 candidates;
per-run audit below).

Ground-truth method: for each evidence row I read the row's own
`quote` (the extractor's verbatim span) and check whether (subject,
metric, value) match the sentence. Expected relevance was set from
each tool's published function (its own paper), not from any word in
our vocabularies; the 17-case independent corpus
(`tests/science_eval_corpus.json`) is the scored instrument for the
relevance dimension, engine offline, documents frozen.

Counts below are derived, not asserted: before-side row audits are
reproducible from `data/state.sqlite3` (read-only) with
`/tmp`-style queries shown in the root-cause doc; after-side from the
run JSON.

## 1. Relevant recall

Formula: |candidates the run placed in a relevant class AND that are
independently relevant| / |independently relevant candidates in the
union of both runs' candidate sets|.

Independently relevant set (5): PureCN (purity + CN calling), CNVkit
(panel/WGS CN calling), AneuFinder (allele-specific CN segmentation),
HapASeg (haplotype-aware CN segmentation), STGBench (CNV benchmark -
relevant to the review, not a competing caller; counted relevant
because the review question is relevance-to-task, and a benchmark is
task-relevant evidence).

BEFORE: 1/5 = 0.20. AneuFinder only (labelled "complementary");
PureCN was labelled IRRELEVANT (its own domain vocabulary present -
root cause 1.1), HapASeg "insufficient information" (never
investigated - root cause 1.2), CNVkit and STGBench were not
retrieved at all.
AFTER: 4/5 = 0.80. Run 96774e screened CNVkit, PureCN, STGBench and
AneuFinder, ALL FOUR labelled "direct alternative" - each placed in
a relevant class by a positive-evidence basis (identity-resolved
where two axes agreed). The miss: HapASeg was never retrieved by
any run - a recall gap of the retrieval plan, recorded here rather
than hidden (the union across both runs contains it, so the honest
denominator is 5).

## 2. False-positive rate (irrelevant candidates feeding
   comparisons)

Formula: |comparison pairs whose alternative is independently
irrelevant| / |all comparison pairs|.

BEFORE: the before-run's conclusion ("Retain current tool") was not
built on comparison pairs at all - it was the default when nothing
validated; its evidence section nonetheless carried 19 valued rows,
6 of them bound to PureCN (labelled irrelevant in the same report)
and 4 bound to Clair (an ONT variant caller - independent tool is
ClairS). Pairing was therefore vacuous AND poisoned; the meaningful
rate is 10/19 valued rows misattributed to tools they do not
describe (53%), with 0 valid pairs.
AFTER: 0/4 = 0.00. The comparison table has 4 candidate rows
(CNVkit, PureCN, STGBench, AneuFinder) and every alternative on it
is independently relevant; excluded_irrelevant is EMPTY - no
 Clair-style interlopers, no wrapper feeding a numeric comparison
 (CNVs is parked in wrappers_integrations). The evidence layer that
 feeds those rows: 17 valued rows, 0 bound to the wrong tool
 (audit, dimension 4).

## 3. Identity accuracy

Formula: precision = correct resolutions / resolutions; resolution
rate = resolved / candidates.

BEFORE: the concept did not exist. Consequences measured: baseline
facets carried "1.1.0 (stable; PyPI current version)" - the version
of Uber's visualisation library; ASCAT's version came from the PyPI
package (2.8.1) instead of the bioconda stable line the deployment
installs (3.2.x); PURPLE hit the colour-palette package.
AFTER: precision 3/3 = 1.00; resolution rate 3/6 = 0.50 of the six
non-baseline candidates (the three unresolved say so and carry
"Not verified" instead of borrowed attribution). Run 96774e's
resolutions, with the corroboration axes recorded on each identity
record:
- CNVkit -> github.com/etal/cnvkit: registry (bioconda AND PyPI,
  both 0.9.14) + description-domain axis;
- PureCN -> github.com/lima1/PureCN: registry (Bioconductor PureCN
  2.18.0 - the bridge the bioconda recipe alone could not provide)
  + description-domain axis;
- STGBench -> github.com/icarus200110/stgbench: literature-URL axis
  (PMID 42418828 links the repo) + naming-paper + github-name-hit.
No resolution merged a name-collision: the before-run's failure
modes (facets -> Uber's visualisation library, PURPLE -> the colour
palette package, ASCAT -> PyPI 2.8.1) each appear in run 96774e's
identity `considered`/`rejected` lists and stayed separate projects.

## 4. Metric attribution accuracy

Formula: valued rows whose (subject, metric, value) all match their
quote / all valued rows.

BEFORE: 1/19 correct under the audit above:
- purple: 0/5 (95.5%/91.0% are PureCN's and CNVkit's SPECIFICITIES;
  67.8% belongs to ClinCNV; PURPLE's own sentence gives
  58.6%/92.3% - none of these rows say that);
- PureCN: 0/6 (95.5% is specificity bound as sensitivity; "95%" is
  the Wilson-CI level inside CNVkit's sentence; each value duplicated
  through a second pseudo-citation of the same text);
- AneuFinder: 1 correct unique (r=0.855) emitted 3 times; the
  0.7695 row re-names a DNA-vs-RNA concordance as "Pearson" for
  AneuFinder alone;
- Clair: 0/4 (the quotes are about ClairS; one recall value bound as
  precision; F1 duplicated).
AFTER: 17/17 = 1.00. Every valued row in run 96774e was read
against its own stored `quote`: purple 4/4 (58.6/92.3 and 91.7/94.1
- the "X%/Y%" fraction pair and the second-cohort sentence, both on
the tool the sentence names); CNVkit 8/8 (76.3 with its Wilson CI,
91.0 with 81.8-95.8, F1 81.8, the 67.8 decision-calibration
sentence, the FISH-cohort 92.3/94.1, the tumor-only 100/93); PureCN
4/4 (16.9/95.5 at absCN >= 14 and 19.3/91.2); AneuFinder 1/1
(r=0.855). The three BEFORE grammar classes - metric-value swap,
fraction pairs, CI text as value - are exactly where the independent
verification pass now bites: CNVkit's per-candidate log shows a
`correct` verdict (a `0.90` re-derived by the second parser) and
26 `reject` rows; the before-run's AneuFinder "0.7695 Pearson" is
REJECTED with subject "panel". Rejected and foreign rows never
enter a comparison (CNVkit: confirmed 8 / rejected 26 / foreign 21
- 8 published).

## 5. Comparability accuracy

Formula: direct-labelled pairs whose documented dimensions actually
match under manual review / all direct-labelled pairs; also reported:
count of pairs the engine refused to label direct.

BEFORE: no comparability layer existed - values from a targeted
ERBB2-panel paper and the deployment's WGS setting sat in one
evidence section with no comparison class; direct/indirect
distinction: absent (treated as 0 correct of 0 valid).
AFTER: direct precision 2/2 = 1.00, refusals 2. Run 96774e labels
exactly two pairs direct, both "best pair vs purple: same endpoint
metric under matched, documented conditions", and manual review
confirms both sides of each pair come from pmc:pmc13703794's shared
cohorts (sensitivity 76.3 vs 58.6; the PureCN/PURPLE specificity
pair). STGBench and AneuFinder are REFUSED direct
("no baseline measurement shares this candidate's metric") - the
modality/metric-silence rule did its job; CNVs is not a pair at all
(wrapper lane). No cross-setting pairing of the BEFORE kind exists
anywhere in the report.

## 6. Recommendation validity

Formula: 1 if (module label is one of the five vocabulary labels AND
decision_ready agrees with the non-negotiable rules) per the
independently reviewed expected answer, else 0. Expected answer for
this module given today's evidence: NO adoption ("Review incomplete"
while follow-ups remain open, or "Evidence supports retaining" when
they close) AND decision_ready = false.

BEFORE: label "Retain current tool" with decision_ready = true and
eight constant-true quality gates (root cause 1.5/§2). Invalid.
AFTER: 1/1 valid. Run 96774e: label "Review incomplete" AND
decision_ready = false - the pair the expected answer demands, in
agreement by construction (the E2E check "decision_ready iff all
gates pass AND no module is Review incomplete" pins it; the
run-45dc58 contradiction was the defect fixed in 7410964). All 8
gates pass and the label STILL says not-ready, because
conditions_unmet names the one open item (CellSNP follow-up, skip
reason "GitHub budget exhausted this hour") - gates measure the
evidence, the label additionally measures openness.

## 7. Remaining unresolved (honesty of the open end)

Formula: count of candidates left unresolved/insufficient, each with
recorded follow-up queries; and count of unresolved items silently
dropped (must be 0).

BEFORE: HapASeg "insufficient information to classify" with no
follow-up mechanism in the product; CNVkit and STGBench never
surfaced and not recorded as coverage gaps in a machine-checkable
form. Dropped-without-trace items: 2 of the relevant set (retrieval
misses, unrecorded).
AFTER: 4 unresolved items, 4 with recorded follow-ups or reasons,
0 silently dropped. Run 96774e: AneuFinder (identity unresolved -
version/license print "Not verified", Pearson row kept and
confirmed), CellSNP (insufficient-to-classify with 5 follow-up
queries recorded, one explicitly budget-skipped with its reason),
CNVs (terminal state relevance-established in the wrapper lane),
HapASeg (not retrieved by any run - named as a retrieval-plan gap
in dimension 1 rather than quietly absent). The module's
conditions_unmet names the open follow-up verbatim;
coverage_accounting lists all 12 out-of-scope modules with
"not part of this run's scope" and the 36-query provenance log in
progress.queries accounts for every issued query.

## Cross-cutting scorecard

| dimension | before (bd0205) | after (96774e) | moved by |
|---|---|---|---|
| 1 relevant recall | 1/5 = 0.20 | 4/5 = 0.80 | positive-evidence relevance + executed follow-ups (7410964 and S4 anchors) |
| 2 false-positive rate | 10/19 rows misbound; 0 valid pairs | 0/4 pairs irrelevant; 0/17 rows misbound | S3 identity + S6/S7 verification (7410964, 75d1308) |
| 3 identity accuracy | concept absent (collisions merged) | precision 3/3; rate 3/6, unresolved printed | identity axes + home-URL/period fixes (953679c + identity commits) |
| 4 metric attribution | 1/19 = 0.05 | 17/17 = 1.00 (26 rejects, 1 correct active) | S6 extraction + S7 independent verify (7410964) |
| 5 comparability | absent (cross-setting mixing) | direct 2/2, refusals 2 | S8 comparability classes (M8 engine) |
| 6 recommendation validity | invalid (ready on junk) | valid (not-ready, named reasons) | label/gate contract (7410964) + decision_ready rule |
| 7 honesty of the open end | 2 of the relevant set dropped unrecorded | 4 unresolved, all recorded, 0 dropped | coverage accounting + executed/skipped follow-up ledger |

The run also caught two of its own tooling defects as acceptance
artifacts, both fixed and anchored before the final run: the
Bioconductor CDN stalls (dcd475f) and the starved-resume defect
(50ef8f4) - see docs/PURITY_CNV_REVIEW_IMPROVED_2026-11-17.md
header for the incident report.
