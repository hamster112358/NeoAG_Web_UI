"""Milestone-8 PART 8: independent evaluation of the relevance engine.

Scores science.assess_relevance (S4) against an INDEPENDENT corpus
(tests/science_eval_corpus.json): 17 cases whose ground truth was set
from each tool's published function vs the module task, never from any
engine output, deliberately disjoint from the development-regression
tools. Reports a confusion matrix, PER-CLASS PRECISION AND RECALL, and
an error analysis (every mismatch prints the engine's own recorded
basis/reasons so failures are diagnosable, not just countable).

Thresholds enforced as regression floors:
  * precision of 'irrelevant' calls == 1.0 - the single non-negotiable
    property: a candidate may only be dismissed on POSITIVE mismatch
    evidence, and every corpus irrelevant case carries such evidence;
  * recall of relevant (direct+complementary+wrapper) >= 0.80;
  * precision of relevant >= 0.80.

Run: python3 tests/science_eval.py
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend import ontology, science  # noqa: E402

CORPUS = json.loads((ROOT / "tests" / "science_eval_corpus.json")
                    .read_text())["cases"]

# engine relationship -> scorer class
CLASS_OF = {"direct-alternative": "direct", "complementary":
            "complementary", "wrapper": "wrapper", "partial-overlap":
            "complementary", "unrelated": "irrelevant", "unresolved":
            "unresolved"}
RELEVANT = ("direct", "complementary", "wrapper")


def cls_of(r):
    return CLASS_OF.get(r.get("relationship"), "unresolved")


results = []
for c in CORPUS:
    task = ontology.task_for(c["task_id"])
    r = science.assess_relevance(c["tool"], c["docs"], task=task,
                                 task_id=c["task_id"],
                                 **(c.get("kw") or {}))
    pred = cls_of(r)
    results.append({"case": c, "out": r, "pred": pred,
                    "ok": pred == c["truth"],
                    "subtype_ok": pred == c["truth"]})


def pr(truth_cls, pred_cls, name):
    tp = sum(1 for x in results if x["case"]["truth"] == truth_cls
             and x["pred"] == pred_cls)
    n_truth = sum(1 for x in results if x["case"]["truth"] == truth_cls)
    n_pred = sum(1 for x in results if x["pred"] == pred_cls)
    prec = tp / n_pred if n_pred else None
    rec = tp / n_truth if n_truth else None
    print(f"{name}: TP={tp} truth-n={n_truth} pred-n={n_pred} "
          f"precision={'-' if prec is None else format(prec, '.3f')} "
          f"recall={'-' if rec is None else format(rec, '.3f')}")
    return prec, rec


print(f"corpus: {len(CORPUS)} cases")
print("\n--- confusion (truth -> predicted class) ---")
classes = ["direct", "complementary", "wrapper", "irrelevant",
           "unresolved"]
cm = {(x["case"]["truth"], x["pred"]) for x in results}
for t in classes:
    row = {p: sum(1 for x in results if x["case"]["truth"] == t
                  and x["pred"] == p) for p in classes}
    if sum(row.values()):
        print(f"  {t:14s} " + " ".join(f"{p}:{row[p]}" for p in classes
                                       if row[p]))

# group metrics: relevant (any subtype) vs irrelevant vs unresolved
res_tp = sum(1 for x in results if x["case"]["truth"] in RELEVANT
             and x["pred"] in RELEVANT)
res_n = sum(1 for x in results if x["case"]["truth"] in RELEVANT)
res_p = sum(1 for x in results if x["pred"] in RELEVANT)
prec_rel = res_tp / res_p if res_p else 0.0
rec_rel = res_tp / res_n if res_n else 0.0
prec_irr, rec_irr = pr("irrelevant", "irrelevant", "irrelevant")
prec_un, rec_un = pr("unresolved", "unresolved", "unresolved")
print(f"relevant(any): TP={res_tp} truth-n={res_n} pred-n={res_p} "
      f"precision={prec_rel:.3f} recall={rec_rel:.3f}")
strict = sum(1 for x in results if x["subtype_ok"])
print(f"exact-class accuracy: {strict}/{len(results)}")

print("\n--- error analysis (mismatches with the engine's own basis) ---")
errors = []
for x in results:
    if x["ok"]:
        continue
    errors.append(x)
    r = x["out"]
    print(f"MISMATCH {x['case']['id']}: truth={x['case']['truth']} "
          f"pred={x['pred']} rel={r.get('relationship')} "
          f"conf={r.get('confidence')}")
    print(f"   truth-why : {x['case']['why']}")
    print(f"   basis     : {json.dumps(r.get('basis'))[:200]}")
    for why in (r.get("reasons") or [])[:3]:
        print(f"   reason    : {why[:160]}")
if not errors:
    print("   none")

# case-level table
print("\n--- per-case ---")
for x in results:
    print(f"{'ok ' if x['ok'] else 'BAD'} {x['case']['id']:20s} "
          f"{x['case']['task_id']:16s} truth={x['case']['truth']:13s} "
          f"pred={x['pred']:13s} state={x['out'].get('research_state')}")

fails = []
if prec_irr is not None and prec_irr < 1.0:
    fails.append(f"precision(irrelevant)={prec_irr:.3f} < 1.0")
if rec_rel < 0.80:
    fails.append(f"recall(relevant)={rec_rel:.3f} < 0.80")
if prec_rel < 0.80:
    fails.append(f"precision(relevant)={prec_rel:.3f} < 0.80")
print("\nRESULT:", "ALL THRESHOLDS MET" if not fails else
      f"THRESHOLD FAILURES: {fails}")
sys.exit(1 if fails else 0)
