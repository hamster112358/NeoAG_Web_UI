# Verified-evidence examples (milestone 8 deliverable 3)

Concrete before/after examples of what one evidence row looked like
before the milestone-8 work and what it looks like now, plus the
re-verification recipe for anyone who wants to check them without
trusting this document.

Sources (all read-only reproducible):
- before: run `res-20261117-133053-bd0205`, table `research` in
  `data/state.sqlite3`;
- after: run `res-20261117-194927-45dc58` (all examples below were
  checked row-by-row against their own `quote` spans), with the
  final run `res-20261118-092034-96774e` superseding it for row
  COUNTS - the same-paper / same-claim dedup rules landed after
  45dc58: 96774e reports 35 unique literature records -> 21
  citations kept (0 duplicate records removed by the cross-source
  rule on this sample) and per-candidate verification logs of
  confirm/correct/reject rows (CNVkit 8/0/26, purple 4/0/26,
  PureCN 4/0/26, AneuFinder 1/0/1).

## 1. The metric-value swap, before and after

BEFORE (bd0205, candidate PureCN, verbatim row):
  metric "sensitivity", value "95.5%", dataset "not stated by
  source", quote "Native standalone PureCN yielded 16.9% sensitivity
  and 95.5% specificity at absCN >= 14;"
The quote says 95.5% is the SPECIFICITY; the row bound it as
sensitivity, and the same row appeared twice (the same text was
cited twice - once "independent", once not).

AFTER (45dc58, same paper, same sentence):
  sensitivity 16.9%  verified "confirm"  doc 10.3390/diagnostics16203348
  specificity 95.5%  verified "confirm"  doc 10.3390/diagnostics16203348
  quote (verbatim, spans stored): "Native standalone PureCN yielded
  16.9% sensitivity and 95.5% specificity at absCN >= 14;"
Metric and value bound by position-in-phrase, not by "first metric
word before first number". The "Wilson 95% CI" text in a neighbouring
CNVkit sentence no longer produces a sensitivity of "95%" anywhere.

## 2. The fraction-pair sentence, before and after

BEFORE: no concept of a "sensitivity/specificity" value pair. The
sentence "PURPLE 58.6%/92.3%" contributed its numbers to other
candidates' rows (67.8% -> purple, while 67.8% belongs to ClinCNV's
clause) and PURPLE's own pair appeared nowhere as a pair.

AFTER (45dc58):
  quote "native PureCN 16.9%/95.5% at absCN >= 14," -> sensitivity
  16.9% + specificity 95.5% bound to PureCN;
  quote "PURPLE 58.6%/92.3%." -> sensitivity 58.6% + specificity
  92.3% bound to purple, verified "confirm";
  quote "PURPLE achieved 91.7% sensitivity and 94.1% specificity
  among 29 evaluable tumors" -> both rows, same tool, correct order.

## 3. Multi-tool, multi-metric sentence - how one paper's numbers
   split across tools (the case the acceptance brief asked to see)

The diagnostics paper (DOI 10.3390/diagnostics16203348, "ERBB2
amplification" panel benchmark) states results for several callers in
adjacent sentences. Rows in run 45dc58, each with its own quote and
byte spans (doc-absolute), verified by independent re-parse:

  CNVkit   sensitivity 76.3%  ci "Wilson 95% CI, 64.0-85.3%"
           quote: "On the development cohort, the fixed CNVkit
           median-bin log2 rule yielded 76.3% sensitivity (Wilson 95%
           CI, 64.0-85.3%) and 91.0% specificity (81.8-95.8%), with
           F1 81.8% ..."
  CNVkit   specificity 91.0%, F1 81.8%  (same quote)
  CNVkit   sensitivity 67.8%, specificity 91.0%
           quote: "Under the common patient-grouped decision-
           calibration procedure, CNVkit yielded 67.8% sensitivity
           and 91.0% specificity, whereas ..."
  PureCN   sensitivity 16.9%/19.3%, specificity 95.5%/91.2%
           (four rows, §1 and §2 quotes)
  purple   sensitivity 58.6%, 91.7%; specificity 92.3%, 94.1%
           (§2 quotes)
  purple   one MENTION-ONLY placeholder row for a second document
           that names the tool but yielded no surviving measurement:
           "names the tool; no measurement claim survived the
           independent verification pass" - recorded, never dressed
           up as data.

Every (tool, number) pairing above was checked by hand against the
printed quote; the engine's own verdict field for each is
"confirmed" via an independent re-parse of the stored span, with
"corrected" and "rejected" outcomes preserved in the run's
excluded/log section (root-cause 1.5: attribution errors die at the
verifier, not in the report).

## 4. What a row now carries (contract)

metric, value, ci (kept SEPARATE from the metric value), quote
(verbatim), quote_span + value_span (doc-absolute byte offsets),
doc_cid (canonical: DOI > PMCID > PMID > ...), binding ("value-first"
/ ...), verified (confirm/correct/reject verdict of the independent
re-parse) + verify_note, subject, citation (title/year/url/source),
context {modality, sample_design, ground_truth, coverage, ...} +
context_established flag, tested_version, evidence_level,
from_fulltext, quoted_metrics, version_caveat.

Guarantees enforced by tests (tests/verify_scientific.py, 72
offline checks): spans are doc-absolute across multi-sentence docs;
a CI level never becomes a metric value; foreign-tool subjects get
rejected rows, not silent drops; the same paper's abstract cannot
double-count against its own full text; a repeated claim inside one
document yields one row with the duplicate logged.

## 5. Re-verification recipe

  sqlite3 "file:data/state.sqlite3?mode=ro" \
    "select report_json from research where id like '%45dc58';"
then for any row: open its `doc_cid` document (citation.url), jump
to quote_span[0]:quote_span[1], compare with the printed quote, and
check that the sentence's subject and metric order match the row's
subject/metric/value. The 45dc58 paper resolves via
https://doi.org/10.3390/diagnostics16203348 (Europe PMC record
42938189; PMC deposit PMC13703794).

## 6. Honest residuals

- "dataset: not stated by source" on several rows: the extraction
  did not find a dataset identifier in the quoted context; the
  engine prints absence instead of inventing one (comparability
  rules then refuse to call such pairs direct unless other decisive
  dimensions match);
- the 100% values that appear on some alternative rows are carried
  with their context flags; a 100% without documented conditions
  does not license a decision by itself;
- run 45dc58 still shows the pre-dedup duplication (two entries of
  the same quote); the same intra-document rule that fixed it
  counted 14 -> 7 in run dc8da8, and in the final run 96774e every
  published row appears exactly once (17 valued rows, CNVkit 8,
  purple 4, PureCN 4, AneuFinder 1).
