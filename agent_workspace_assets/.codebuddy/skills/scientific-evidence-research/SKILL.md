---
name: scientific-evidence-research
description: Rules for tool/literature research in this interface - evidence-level separation, verifiable citations, license/version verification, no cross-comparable-metric ranking, honest coverage. Use for ANY SOTA/tool/benchmark/literature question.
---

# Scientific evidence & research discipline

Research must come from actual retrieval (the interface's live
Discovery engine or your own read-only fetches), never from memory of
plausible-sounding numbers.

## Evidence levels - always separate, never blend
1. author-reported: the tool's own paper claims.
2. independent published: a DIFFERENT group's benchmark/comparison -
   cite it explicitly, and note whether you saw the full text or only
   an abstract.
3. our own experiments: only numbers actually run in this deployment
   with a recorded run directory. If none exist, say so.

## Hard rules
- Never rank tools by comparing AUCs from different datasets, label
  definitions or peptide sets. If metrics are incomparable, present
  them side-by-side and say they are not rankable.
- Quote metric values only with their benchmark task, dataset and
  evaluation labels attached; if a paper's abstract does not state
  them, write "metric context not verified" rather than guessing.
- Verify existence/version/license from primary sources (repository,
  package index, official docs); anything unverifiable is marked
  "Not verified", never filled with a plausible estimate.
- Every citation needs a resolvable identifier (DOI/PMID/arXiv/URL)
  plus the source you actually retrieved it from and the date.
- A report must state what was searched, what failed, and its
  limitations; never promise exhaustiveness.
- No execution of candidate tools without an approved run through
  the normal job-control pathway; literature review and independent
  benchmarking are different claims and must not be merged.
