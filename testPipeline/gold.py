#!/usr/bin/env python3
"""Questions with known answers, so correctness can be scored and not guessed.

    python testPipeline/gold.py                  the agent, as it ships
    python testPipeline/gold.py --arm graph      graph only
    python testPipeline/gold.py --arm docs       documents only
    python testPipeline/gold.py --compare        all three, side by side

Everything else in testPipeline measures whether a store RETURNED something.
That was enough to show the loop beats a fixed plan, and it is not enough for
the question that is left. Asked "is rimegepant FDA approved", the graph
answered "listed as None (Tentative Approval) rather than full approval" and
the documents answered "yes, approved for acute migraine". Both scored as
answered. One was wrong, and nothing in the harness could tell.

THE FACTS HERE DO NOT COME FROM THE GRAPH.

That is the whole point and the easiest thing to get wrong. Deriving expected
answers from the graph would make the graph correct by construction and the
comparison meaningless. These are established pharmacology and regulatory
facts - the kind a reviewer can check without this pipeline - chosen because
they are unambiguous and stable.

Two checks per question, and the second matters more:

    must     a fact the answer has to contain
    must_not a claim that would make the answer WRONG, not merely thin

An answer that omits something is incomplete. An answer that says a marketed
drug is not approved, or names the wrong target, is the failure that makes a
research agent unusable - and it is the one an "did it answer" metric scores
as a pass.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent as AG                                             # noqa: E402

#: (kind, question, must-contain-any-of, must-not-contain-any-of)
#:
#: `must` is a list of alternative spellings - an answer saying "PD-1" and one
#: saying "programmed cell death protein 1" are the same answer, and scoring
#: the phrasing rather than the fact would measure the model's vocabulary.
#:
#: `kind` is what makes this set discriminate, which the first version did not.
#: Eighteen well-known facts scored 18/18 on every arm - true, and useless for
#: choosing a routing strategy, because not one of them was a question a store
#: could fail. A test every configuration passes ranks nothing.
#:
#:   fact   checkable independently; either store might hold it
#:   doc    needs what a LABEL SAYS - contraindications, warnings, wording.
#:          The graph holds no label prose, so a graph-only arm should fail.
#:   graph  needs counting or structure across sources. A corpus of chunks
#:          cannot count trials, so a documents-only arm should fail.
#:
#: The prediction being tested: each single store fails its opposite kind and
#: the loop handles both. If that does not happen, complementarity is an
#: assumption I have been repeating rather than a finding.
GOLD: list[tuple[str, str, list[list[str]], list[str]]] = [

    # ---- approval status: the case that started this -----------------------
    ("fact", "Is rimegepant FDA approved?",
     [["approved", "approval"]],
     ["not approved", "not fda approved", "no fda approval",
      "tentative approval", "is not currently approved"]),

    ("fact", "Is pembrolizumab FDA approved?",
     [["approved", "approval"]],
     ["not approved", "no fda approval", "is not currently approved"]),

    ("fact", "Is atorvastatin approved in the United States?",
     [["approved", "approval"]],
     ["not approved", "no approval", "is not currently approved"]),

    # ---- absence must never be reported as a negative fact -----------------
    #
    # These exist because the graph now says NA in places it used to say
    # nothing, and NA is easy to read as "no". Both are traps built from real
    # properties of this data:
    #
    #   every one of the 38,914 MHRA products carries status NA, because that
    #   agency's column is a row flag and not a status. A UK approval question
    #   therefore finds a product whose status says nothing.
    #
    #   five of the eleven agencies - NHRA, DHA, DOH, MOH-OM, MOPH-QA - hold
    #   no products at all. Nothing was ever published for them.
    #
    # In both cases the honest answer distinguishes "this source does not say"
    # from "the drug is not approved". Getting this wrong is the rimegepant
    # failure again, and it is the failure a "did it answer" metric passes.
    ("trap", "Is atorvastatin approved in the United Kingdom?",
     [["approved", "authorised", "authorized", "available", "mhra"]],
     ["not approved", "no approval", "is not approved", "not authorised",
      "not available in the uk", "no evidence it is approved"]),

    ("trap", "Is atorvastatin approved in Qatar?",
     [["no data", "not available", "does not contain", "no records",
       "not covered", "cannot determine", "no information"]],
     ["it is not approved in qatar", "atorvastatin is not approved",
      "there are no approvals in qatar because none exist"]),

    # ---- prose the graph does not hold -------------------------------------
    # A boxed warning is a paragraph on a label, not a row. If the corpus is
    # doing anything the graph cannot, it should show here.
    ("fact", "Does metformin carry a boxed warning, and what for?",
     [["lactic acidosis"]],
     ["no boxed warning", "does not carry a boxed warning",
      "metformin has no boxed warning"]),

    ("fact", "What is the boxed warning on warfarin?",
     [["bleeding", "haemorrhage", "hemorrhage"]],
     ["no boxed warning", "does not have a boxed warning"]),

    # ---- target and mechanism, the harder ones -----------------------------
    ("fact", "What is the molecular target of trastuzumab?",
     [["her2", "erbb2", "erbb-2"]],
     ["egfr", "pd-1", "cd20", "vegf"]),

    ("fact", "What is the molecular target of rituximab?",
     [["cd20", "ms4a1"]],
     ["her2", "pd-1", "egfr", "cd19"]),

    ("fact", "What does imatinib inhibit?",
     [["bcr-abl", "bcr abl", "abl", "kit", "c-kit", "pdgfr"]],
     ["her2", "pd-1", "cd20", "hmg-coa"]),

    ("fact", "What is the mechanism of action of semaglutide?",
     [["glp-1", "glp 1", "glucagon-like peptide"]],
     ["insulin analogue", "sglt2", "dpp-4 inhibitor", "biguanide"]),

    ("fact", "What is the molecular target of infliximab?",
     [["tnf", "tumor necrosis factor", "tumour necrosis factor"]],
     ["il-6", "cd20", "her2", "pd-1"]),

    # ---- target and mechanism ----------------------------------------------
    ("fact", "What is the molecular target of pembrolizumab?",
     [["pd-1", "pd1", "programmed cell death protein 1", "pdcd1"]],
     ["egfr", "her2", "vegf", "cd20"]),

    ("fact", "What is the mechanism of action of atorvastatin?",
     [["hmg-coa", "hmg coa", "reductase"]],
     ["beta blocker", "ace inhibitor", "calcium channel"]),

    ("fact", "Which enzyme does aspirin inhibit?",
     [["cyclooxygenase", "cox-1", "cox-2", "cox1", "cox2", "prostaglandin"]],
     ["reductase", "kinase inhibitor"]),

    ("fact", "What is the target of trastuzumab?",
     [["her2", "erbb2"]],
     ["pd-1", "egfr", "cd20", "vegf"]),

    ("fact", "What is the target of rituximab?",
     [["cd20", "ms4a1"]],
     ["her2", "pd-1", "egfr"]),

    # ---- drug class --------------------------------------------------------
    ("fact", "What drug class does metformin belong to?",
     [["biguanide", "antidiabetic", "a10", "blood glucose"]],
     ["statin", "beta blocker", "antibiotic"]),

    ("fact", "What class of drug is amoxicillin?",
     [["penicillin", "beta-lactam", "antibacterial", "antibiotic", "j01"]],
     ["statin", "antidepressant", "anticoagulant"]),

    ("fact", "Is sertraline an SSRI?",
     [["ssri", "serotonin reuptake", "serotonin-reuptake"]],
     ["is not an ssri", "not a serotonin"]),

    ("fact", "Which class does warfarin belong to?",
     [["anticoagulant", "vitamin k antagonist", "coumarin", "b01"]],
     ["antibiotic", "statin", "antipsychotic"]),

    # ---- indication --------------------------------------------------------
    ("fact", "What is levetiracetam used to treat?",
     [["epilep", "seizure", "convuls"]],
     ["diabetes", "hypertension", "asthma"]),

    ("fact", "What condition is metformin used for?",
     [["diabet", "glycaem", "glycem", "blood glucose"]],
     ["epilepsy", "asthma", "depression"]),

    ("fact", "What is salbutamol used for?",
     [["asthma", "bronch", "airway", "copd", "obstructive"]],
     ["diabetes", "epilepsy"]),

    # ---- identifiers, where a wrong answer is unmistakable -----------------
    ("fact", "What is the ATC code for atorvastatin?",
     [["c10aa05", "c10aa"]],
     ["a10", "n06a", "j01"]),

    ("fact", "Which gene does the EGFR protein come from?",
     [["egfr", "erbb1", "her1"]],
     ["kras", "tp53", "braf"]),

    # ---- absence, stated correctly -----------------------------------------
    # Not a trick: a drug that does not exist should produce "not found", and
    # an agent that invents a profile for it is worse than one that says so.
    ("fact", "What is the mechanism of action of florbetapinib-zx?",
     [["no", "not", "could not", "unable", "nothing"]],
     ["inhibitor of", "binds to the", "approved for"]),

    # ---- doc: what a LABEL SAYS. The graph holds no label prose, so a
    # graph-only arm should fail these, and that is the point of having them.
    ("doc", "What does the atorvastatin label say about use in pregnancy?",
     [["contraindicat", "not recommend", "should not be used", "avoid"]],
     ["safe in pregnancy", "no restriction"]),

    ("doc", "What are the contraindications listed for warfarin?",
     [["bleed", "haemorrhag", "hemorrhag", "pregnan", "contraindicat"]],
     ["no contraindications"]),

    ("doc", "What does the metformin label say about renal impairment?",
     [["renal", "kidney", "egfr", "creatinine", "lactic acidosis"]],
     ["no renal", "no restriction"]),

    ("doc", "What warnings does the label give for isotretinoin in pregnancy?",
     [["teratogen", "contraindicat", "pregnan", "birth defect", "malformation"]],
     ["safe in pregnancy"]),

    ("doc", "What does the sertraline label say about suicidal thoughts?",
     [["suicid", "behaviour", "behavior", "young adult", "monitor"]],
     ["no warning"]),

    # ---- graph: counting and structure across sources. A corpus of chunks
    # cannot count trials, so a documents-only arm should fail these.
    ("graph", "How many clinical trials in the graph test pembrolizumab?",
     [["trial"]],
     ["cannot count", "unable to count", "no tool", "i don't have access"]),

    ("graph", "Which regulatory agencies have approved products containing "
              "atorvastatin?",
     [["fda", "ema", "mhra", "pmda", "sfda", "health canada"]],
     ["no tool", "i don't have access", "cannot determine"]),

    ("graph", "How many substances in the graph are classified as "
              "anticoagulants?",
     [["anticoagulant"]],
     ["cannot count", "no tool", "i don't have access"]),

    ("graph", "Which drugs target EGFR according to the graph?",
     [["egfr"]],
     ["no tool", "i don't have access", "cannot determine"]),

    ("graph", "How many products contain metformin?",
     [["product", "metformin"]],
     ["cannot count", "no tool", "i don't have access"]),
]


def score(answer: str, must: list[list[str]], must_not: list[str]) -> dict:
    a = (answer or "").lower()
    missing = [alts for alts in must if not any(x in a for x in alts)]
    wrong = [p for p in must_not if p in a]
    return {
        "has_fact": not missing,
        "contradicts": bool(wrong),
        # Correct means both: it said the right thing AND did not say a wrong
        # one. An answer can contain the fact and still be wrong elsewhere.
        "correct": not missing and not wrong,
        "missing": [alts[0] for alts in missing],
        "wrong": wrong,
    }


ARMS = {
    "agent":  dict(allow=None, max_graph=4, max_docs=4),
    "graph":  dict(allow=("graph",), max_graph=8, max_docs=0),
    "docs":   dict(allow=("documents",), max_graph=0, max_docs=8),
}


def run_arm(name: str, k: int) -> list[dict]:
    cfg = ARMS[name]
    out = []
    for kind, q, must, must_not in GOLD:
        try:
            res = AG.run(q, k=k, **cfg)
            ans = res.get("answer") or ""
        except Exception as e:                                 # noqa: BLE001
            ans = ""
            res = {"steps": [], "total_ms": 0, "error": str(e)}
        s = score(ans, must, must_not)
        steps = [x for x in res.get("steps", []) if x.get("tool") in ("graph", "documents")]
        out.append({"arm": name, "kind": kind, "question": q, **s,
                    "seq": "".join(x["tool"][0] for x in steps),
                    "ms": res.get("total_ms", 0),
                    "answer": ans[:400]})
        mark = "ok  " if s["correct"] else ("WRONG" if s["contradicts"] else "thin ")
        print(f"  {mark} {kind:<6} [{out[-1]['seq']:<5}] {q[:50]}", flush=True)
        if not s["correct"]:
            print(f"        missing={s['missing']} wrong={s['wrong']}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="agent", choices=list(ARMS))
    ap.add_argument("--compare", action="store_true", help="run all three arms")
    ap.add_argument("--k", type=int, default=6)
    ap.add_argument("--out", default="testPipeline/gold_results.jsonl")
    a = ap.parse_args()

    arms = list(ARMS) if a.compare else [a.arm]
    allrows = []
    for name in arms:
        print(f"\n=== {name} ===")
        allrows += run_arm(name, a.k)

    with pathlib.Path(a.out).open("w", encoding="utf-8") as fh:
        for r in allrows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n{'arm':<8}{'correct':>9}{'has fact':>10}{'CONTRADICTS':>13}{'sec':>7}")
    for name in arms:
        rs = [r for r in allrows if r["arm"] == name]
        print(f"  {name:<6}{sum(r['correct'] for r in rs):>9}/{len(rs)}"
              f"{sum(r['has_fact'] for r in rs):>9}"
              f"{sum(r['contradicts'] for r in rs):>13}"
              f"{sum(r['ms'] for r in rs)/1000/max(1,len(rs)):>7.0f}")
    print(f"\nwritten to {a.out}")


if __name__ == "__main__":
    main()
