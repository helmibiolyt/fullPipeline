#!/usr/bin/env python3
"""Run a battery of questions through the router and report what breaks.

    python testPipeline/probe.py                # everything
    python testPipeline/probe.py --only graph   # one side
    python testPipeline/probe.py -v             # show the queries

Each case declares which store it SHOULD reach and whether it should return
anything. That turns three separate failures into three distinct signals:

    MISROUTED  the router sent it to the wrong store
    EMPTY      right store, query ran, nothing came back
    ERROR      the query was invalid or the service failed

An empty result is the interesting one. It usually means a property the schema
declares is not actually populated, which no amount of prompt tuning fixes -
Target.symbol was found exactly this way.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ask as A                                             # noqa: E402

# (question, expected store, expect results)
CASES = [
    # ---- graph: targets and mechanism
    ("which drugs target EGFR", "graph", True),
    ("what drugs act on PD-1", "graph", True),
    ("what is the mechanism of action of atorvastatin", "graph", True),
    ("which substances are HMG-CoA reductase inhibitors", "graph", True),
    ("what does pembrolizumab target", "graph", True),

    # ---- graph: products and approvals
    ("which agencies approved products containing atorvastatin", "graph", True),
    ("how many products contain metformin", "graph", True),
    ("what products are approved in Saudi Arabia", "graph", True),
    ("which company develops Keytruda", "graph", True),
    ("what patents protect products containing celecoxib", "graph", True),

    # ---- graph: trials
    ("how many clinical trials are registered on clinicaltrials.gov", "graph", True),
    ("how many trials are running in the MENA region", "graph", True),
    ("which companies sponsor the most trials", "graph", True),
    ("find phase 3 trials studying breast cancer", "graph", True),
    ("what trials tested pembrolizumab", "graph", True),

    # ---- graph: safety
    ("what adverse events are reported for ibuprofen", "graph", True),
    ("which drugs have cardiac adverse events reported", "graph", True),
    ("what recalls involve valsartan", "graph", True),

    # ---- graph: genomics and classification
    ("what variants are found in the BRAF gene", "graph", True),
    ("which pathogenic variants are implicated in cystic fibrosis", "graph", True),
    ("what ATC class does metformin belong to", "graph", True),
    ("what diseases are associated with the TP53 gene", "graph", True),

    # ---- graph: literature and identity
    ("what publications mention pembrolizumab", "graph", True),
    ("what is the UNII for atorvastatin", "graph", True),

    # ---- documents
    ("what are the contraindications of sertraline", "vector", True),
    ("what does the label say about ibuprofen in pregnancy", "vector", True),
    ("how should insulin be stored", "vector", True),
    ("what is the recommended dose of amoxicillin for children", "vector", True),
    ("what warnings are given about metformin and kidney function", "vector", True),
    ("what are the undesirable effects listed for atorvastatin", "vector", True),
    ("which medicines interact with warfarin according to the label", "vector", True),
    ("what does the assessment report conclude about efficacy of remdesivir",
     "vector", True),
]


def run(cases, verbose=False):
    results = []
    for q, want, expect in cases:
        rec = {"q": q, "want": want, "expect": expect,
               "store": "", "n": 0, "ms": 0, "status": "", "detail": "",
               "query": ""}
        try:
            r = A.route(q, verbose=True)
            if "error" in r:
                rec["status"] = "ROUTER"
                rec["detail"] = r["error"]
                results.append(rec)
                _line(rec, verbose)
                continue

            tool, args = r["tool"], r["args"]
            rec["store"] = "graph" if tool == "query_graph" else "vector"

            if rec["store"] != want:
                rec["status"] = "MISROUTED"
                rec["detail"] = f"went to {rec['store']}"
                rec["query"] = args.get("cypher") or args.get("query", "")
                results.append(rec)
                _line(rec, verbose)
                continue

            if tool == "query_graph":
                cy = args.get("cypher", "").strip()
                rec["query"] = cy
                bad = A.check_cypher(cy)
                if bad:
                    rec["status"] = "REFUSED"
                    rec["detail"] = bad
                else:
                    rows, ms = A.run_cypher(cy)
                    rec["n"], rec["ms"] = len(rows), ms
                    rec["status"] = "OK" if rows else "EMPTY"
            else:
                sq = args.get("query", "")
                rec["query"] = sq
                res, ms = A.run_search(sq, args.get("section"), 6)
                hits = res.get("results", [])
                rec["n"], rec["ms"] = len(hits), ms
                rec["status"] = "OK" if hits else "EMPTY"
        except Exception as e:                               # noqa: BLE001
            rec["status"] = "ERROR"
            rec["detail"] = f"{type(e).__name__}: {str(e)[:150]}"
        results.append(rec)
        _line(rec, verbose)
    return results


ICON = {"OK": "✓", "EMPTY": "∅", "MISROUTED": "→",
        "ERROR": "✗", "REFUSED": "✗", "ROUTER": "✗"}
COLOR = {"OK": A.green, "EMPTY": A.amber, "MISROUTED": A.amber,
         "ERROR": A.red, "REFUSED": A.red, "ROUTER": A.red}


def _line(r, verbose):
    c = COLOR.get(r["status"], A.dim)
    tag = c(f"{ICON.get(r['status'],'?')} {r['status']:<9}")
    n = f"{r['n']:>3} " if r["status"] in ("OK", "EMPTY") else "    "
    ms = A.dim(f"{r['ms']:>5}ms") if r["ms"] else A.dim("       ")
    print(f"  {tag} {n}{ms}  {r['q'][:62]}")
    if r["detail"]:
        print(A.dim(f"              {r['detail'][:96]}"))
    if verbose and r["query"]:
        print(A.dim(f"              {' '.join(r['query'].split())[:150]}"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["graph", "vector"])
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    cases = [c for c in CASES if not a.only or c[1] == a.only]
    print(A.bold(f"\nprobing {len(cases)} questions\n"))
    t0 = time.time()
    res = run(cases, a.verbose)

    ok = [r for r in res if r["status"] == "OK"]
    print()
    print(A.bold(f"  {len(ok)}/{len(res)} returned data"
                 f"   ({time.time()-t0:.0f}s)"))
    for label in ("EMPTY", "MISROUTED", "ERROR", "REFUSED", "ROUTER"):
        bad = [r for r in res if r["status"] == label]
        if bad:
            print(COLOR[label](f"\n  {label} ({len(bad)})"))
            for r in bad:
                print(f"    · {r['q']}")
                if r["query"]:
                    print(A.dim(f"        {' '.join(r['query'].split())[:140]}"))
    return res


if __name__ == "__main__":
    main()
