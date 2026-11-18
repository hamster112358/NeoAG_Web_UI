# Purity/CNV review, improved (milestone 8 deliverable 4)

Re-run of the purity/CNV tool review under the milestone-8 scientific
engine, answering the six acceptance questions A-F. Every claim names
the run, document or file:line it came from; nothing here is asserted
from memory.

Engine: local tree at the milestone-8 commits; live service on port
8712. Acceptance runs (targeted scope, purity_cnv module, live
engine, persisted in `data/state.sqlite3`):

- before: `res-20261117-133053-bd0205` (milestone-7 engine) -
  falsely DECISION-READY; analysed in
  `docs/SCIENTIFIC_ROOT_CAUSE_2026-11-17.md`;
- after (final): `res-20261118-092034-96774e` (2026-11-18, engine
  at commits through `50ef8f4`) - the review below;
- intermediate post-fix runs `res-20261117-190518-90c9f5`,
  `res-20261117-194927-45dc58` and `res-20261117-210101-dc8da8` are
  kept verbatim and are cited where a finding was first measured
  there. One honest incident belongs in this list: resuming
  `…-dc8da8` (to clear two transient Bioconductor timeouts) flipped
  its status to "completed" while the resumed pass concluded from a
  starved evidence base - skipped queries kept their skip but not
  their records, so the literature-derived candidates vanished
  (10 -> 8). Found by inspecting the resumed report, fixed in
  `50ef8f4` (resume now restores persisted records and re-runs any
  done query whose records are missing), anchored by four E2E
  checks. The final run above used the fixed code.

## A. What NeoAG currently uses for purity/CNV

The deployment's purity/CNV stage names four tools
(`backend/inventory.py:72`: `["ascat", "sequenza", "facets",
"purple"]`). These are reported to the engine as the baseline, each
carrying its own evidence pointer (workflow config / install
manifest), and every run lists them as "current baseline (not an
alternative)" - visible in every run listed above.

Registry-verified current versions (run 45dc58, S5 read-only probe,
bioconda preferred because the deployment installs from bioconda;
final run 96774e re-checked on 2026-11-18 and confirmed ascat =
3.2.0 "stable; bioconda current version", while sequenza/purple/
facets honestly print "Not verified" until their identity resolves
- never a borrowed number):

- ascat: 3.2.0 - "stable; bioconda current version; checked
  2026-11-17". This corrects the before-run, which reported the PyPI
  `ascat` package (2.8.1) as the tool's version - the same-name
  collision class from root cause 1.4;
- sequenza, facets, purple: "Not verified" in run 45dc58 - the
  engine refuses to transfer a number it could not corroborate for
  the resolved project (root cause 1.4 fix: absence is printed as
  absence).

## B. Relevant alternatives, and why none is adopted

Relevance is decided on positive evidence only (S4): a candidate is
never "irrelevant" for lacking vocabulary - the before-run's "PureCN:
irrelevant" verdict is structurally impossible now, and indeed PureCN
reads "direct alternative" in run 45dc58.

What the review found among retrieved candidates (run 45dc58, full
rows in the run's JSON):

- CNVkit - direct alternative; identity resolved (bioconda/PyPI
  records whose project home is github.com/etal/cnvkit merged with
  the GitHub description axis); current version 0.9.14 verified;
  published measurements verified on an ERBB2 targeted-panel
  benchmark (section D). NOT adopted: no verified measurement on the
  deployment's comparison basis (WGS, matched-normal, ASCAT 3.2.0
  pinned) shows CNVkit ahead; the panel-benchmark rows are recorded
  but their comparability against the deployed setting is the open
  question the follow-up names.
- PureCN - direct alternative; its Bioconductor record
  (bioconductor_package: version 2.18.0, repo_url
  github.com/lima1/PureCN) is what merges the registry axis with the
  GitHub project. NOT adopted: the only published measurements
  retrieved for it are from targeted-panel/low-purity stress work
  where its own numbers are unfavourable (16.9% sensitivity at
  absCN>=14 in the diagnostics paper, section D) - which argues
  AGAINST replacement on this evidence, not for exclusion. It stays
  researched, not exiled.
- HapASeg - unresolved. No identity merge and no published
  measurement claim survived; the run keeps recorded follow-up
  queries instead of a verdict. Absence of evidence is printed as
  absence (non-negotiable 6).
- STGBench, AneuFinder - direct alternatives with thin evidence
  (1-3 verified rows, no comparison pair against the deployed
  baseline context); reported as such.
- CNVs, CellSNP - the first is the plural acronym minted from review
  prose, the second a name whose domain evidence did not resolve;
  both stay out of comparisons. The milestone-8 identity fix for
  URL-grafting (2026-11-17) stopped "CNVs" from inheriting another
  tool's availability link and being falsely resolved.

Why this answers "relevant alternatives" rather than listing tools:
each row above states the POSITIVE evidence it rests on and the
comparison fact that decides adoption; exclusion is only ever
"comparability does not license a decision yet", never keyword
absence.

## C. Canonical identities, with the collisions the name alone gives

From the runs' identity records (considered-project lists are stored
verbatim in each run JSON):

- PURPLE - the purity/ploidy caller distributed with hmftools. The
  before-era retrievals instead hit: PyPI `purple` (colour palette
  generation), `BootstrapDash/PurpleAdmin-Free-Admin-Template`,
  `meta-llama/PurpleLlama`. The engine now refuses to merge them
  (collision -> attribution blocked), so none of their
  versions/descriptions bind to PURPLE (run 90c9f5 identity
  considers list; regression anchor "S3 same-name collision ... does
  not resolve").
- ASCAT - the allele-specific copy-number tool maintained by the
  Cancer Genetics Group (bioconda `ascat`, the 3.2.x "stable" line).
  Retrieved name-exact look-alikes: `VanLoo-lab/ascat`, PyPI
  `ascat`; near-name: `cancerit/ascatNgs`. None of those projects'
  numbers or versions were transferred to ASCAT (run 90c9f5
  considered list).
- FACETS (faCets) - the Bioconductor/CRAN R package "Filter And
  Cluster Tumor Sequences". Retrieved collisions: PyPI `facets`
  (Uber's visualisation library), `rubyworks/facets` (a Ruby gem),
  `PAIR-code/facets` (TensorFlow's viz suite). All stayed separate
  projects (run 90c9f5 considered list).
- CNVkit - github.com/etal/cnvkit (bioconda and PyPI records carry
  it as their project home); merged and resolved in run 45dc58.
- PureCN - Bioconductor package with source at github.com/lima1/
  PureCN; the bridge record (version 2.18.0, repo_url) was verified
  read-only from the Bioconductor landing page on 2026-11-17.

## D. Published performance, correctly attributed

Source: run `res-20261118-092034-96774e`, `published_benchmark_evidence`
(17 valued rows). Each row below was read against the row's own
`quote` span; the independent-verification verdict is the run's
`measurement_verification` record, not this document's opinion.
One paper, `pmc:pmc13703794`, reports the three main candidates on
the same cohorts, which is what later licenses DIRECT comparisons
(section E).

pmc:pmc13703794, development cohort, fixed median-bin log2 rule:
- CNVkit sensitivity 76.3% (Wilson 95% CI 64.0-85.3%) - confirm
- CNVkit specificity 91.0% (81.8-95.8%) - confirm
- CNVkit F1 81.8% - confirm
- PureCN sensitivity 16.9% / specificity 95.5% (at absCN >= 14) -
  confirm, confirm
- PURPLE sensitivity 58.6% / specificity 92.3% - confirm, confirm
  (the before-run bound 95.5% to PureCN as a SENSITIVITY and the
  same text's 76.3%/91.0% to other names; here each number sits on
  the tool its sentence names)

pmc:pmc13703794, common patient-grouped decision calibration:
- CNVkit sensitivity 67.8% / specificity 91.0% - confirm, confirm

pmc:pmc13703794, FISH-confirmed cohort (13 positive / 17 negative):
- CNVkit sensitivity 92.3% / specificity 94.1% - confirm, confirm

pmc:pmc13703794, 29 evaluable tumors:
- PURPLE sensitivity 91.7% / specificity 94.1% - confirm, confirm

pmc:pmc13703794, validated tumor-only workflow, independent cohort:
- CNVkit sensitivity 100% / specificity 93% - confirm, confirm
  (carried with context flags; a 100% without documented conditions
  does not license a decision by itself)

pmc:pmc13345378:
- AneuFinder Pearson r 0.855 (simulated DNA data) - confirm
- the 0.7695 row that the before-run presented as "Pearson" for
  AneuFinder is now REJECTED with subject "panel" - the sentence is
  a DNA-vs-RNA panel concordance, not a caller's accuracy (this is
  the independent verification pass refusing a bound number)

Absence printed as absence: STGBench, CNVs and CellSNP carry "names
the tool; no measurement claim survived the independent verification
pass" instead of borrowed numbers. Candidate-level verification
totals from the run: CNVkit 8 confirmed / 26 rejected / 21 foreign;
purple 4 / 26 / 25; PureCN 4 / 26 / 25; AneuFinder 1 / 1 / 1. A
`correct` verdict is exercised too (a specificity the first parser
bound as `0.90` was re-derived), and every reject/foreign row stayed
out of every comparison.

## E. The defensible conclusion

Run 96774e, module purity_cnv, verbatim fields from the stored
report:

- recommendation: `Review incomplete` - decision_ready: false;
  all 8 scientific gates pass, and the label nonetheless stays open
  because `conditions_unmet` names exactly one thing: "every
  identified candidate reached a terminal relevance state with
  follow-ups closed" - specifically CellSNP, whose recorded
  follow-up shows the skip reason `not executed - GitHub budget
  exhausted this hour`. A conclusion written over an open follow-up
  is a guess, and the engine says so.
- comparison question (stored): "Which purity/CNV estimator is more
  accurate on comparable WES/WGS data with matched normals?"
- per-row comparability from the run: CNVkit and PureCN each carry
  "best pair vs purple: direct - same endpoint metric under
  matched, documented conditions" (both sides come from
  pmc:pmc13703794's shared cohorts); STGBench and AneuFinder each
  carry "no baseline measurement shares this candidate's metric",
  so they are never labelled direct.
- what those two direct pairs actually say: sensitivity - CNVkit
  76.3% > PURPLE 58.6% (development cohort); PureCN 16.9% < PURPLE
  58.6% (at absCN >= 14); specificity - PureCN 95.5% > PURPLE
  92.3%, CNVkit 91.0% < 92.3%. The engine prints these pairs and
  the per-dimension verdicts; it does NOT convert them into an
  adoption decision while follow-ups are open and while AneuFinder
  identity stays unresolved (its version/license stay "Not
  verified - attributing this field to a project would be a guess").
- candidate accounting: CNVs is listed under wrappers_integrations
  (not an independent algorithm); excluded_irrelevant is EMPTY -
  the report has no junk alternatives; the only exclusion is
  CellSNP ("insufficient information to classify" with 5 follow-up
  queries recorded, 1 explicitly budget-skipped).
- baseline truth (S5): PureCN's verified current version now reads
  "2.18.0 (stable; Bioconductor release current version; checked
  2026-11-18)" - the Bioconductor registry axis wired in this
  milestone answers through the channel the deployment actually
  installs from, not PyPI.

So the defensible conclusion is the honest one: two verified direct
pairs against the deployed baseline, correctly oriented; no adoption
decision yet; and a named, machine-checkable list of what would
close the review (run CellSNP's follow-ups - the next run's resume
path now restores the evidence to do it).

## F. Traceable evidence chain and how to re-verify it

Nothing above needs to be taken on trust. The chain, all on this
host, read-only:

1. Stored runs: `data/state.sqlite3`, table `research`, ids
   `res-20261117-133053-bd0205` (before),
   `res-20261117-190518-90c9f5`, `res-20261117-194927-45dc58`,
   `res-20261117-210101-dc8da8` (post-fix intermediates),
   `res-20261118-092034-96774e` (final). Each row's `report_json`
   carries the full provenance: `queries` (every issued query with
   source, count, error, timestamp), `coverage.per_source`, and per
   candidate `identity` (axes), `version_chain`,
   `measurement_verification` (confirm/correct/reject log) and
   `published_benchmark_evidence` (every row with its `quote` and
   `doc_cid`).
2. Via the API (the live service on :8712 serves the same rows):
   `GET /api/research/res-20261118-092034-96774e` (operator Basic
   auth). `progress.lit_records` holds the verbatim retrieved
   records the durable-resume path restores.
3. Quote spot-checks (the D-table method): for each valued row, open
   its `doc_cid` document (PMC13703794 / PMC13345378 for the rows
   above) and find the `quote`; the (subject, metric, value[, CI])
   tuple in the row must match the sentence. Run 96774e: 17/17.
4. Re-running the whole thing: `POST /api/research` with
   `{"scope":"targeted","targets":["purity_cnv"],"engine":"live"}`
   performs real retrieval again (GitHub anonymous quota: 60/hour -
   schedule accordingly).
5. The rules are anchored, not just described: `tests/
   verify_scientific.py` (76 checks incl. the four resume-durability
   anchors that fail under the pre-`50ef8f4` code),
   `tests/verify_battery.py` (143), `tests/science_eval.py`
   (independent 17-case corpus; thresholds met).
6. Commit map for the claims in this document: `7410964` (four
   run-1 defects), `953679c` + identity commits (identity axes,
   trailing-period/home-URL merges), `75d1308` + `7c9bf6f`
   (supersession + intra-document dedup), bioconductor wiring
   (registry axis + version/license answers), `dcd475f` (bounded
   retry for transient CDN stalls), `50ef8f4` (resume restores
   evidence).
