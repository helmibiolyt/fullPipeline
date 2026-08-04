#!/usr/bin/env python3
"""How complete is this graph's trial-to-disease linkage, against the registry?

    python graph/recall_vs_ctgov.py
    python graph/recall_vs_ctgov.py --conditions "eczema,asthma"

Every other coverage number in this repo is measured against ourselves: what
fraction of trials carry a link, what fraction of conditions matched. None of
those can tell you whether the LINKS ARE THE RIGHT ONES or whether the ones
that are missing matter, because the denominator comes from the same pipeline
as the numerator.

ClinicalTrials.gov is the registry itself. Asking it how many trials exist for
a condition, and asking the graph the same question restricted to the trials
it took FROM that registry, gives a recall figure that owes nothing to this
codebase.

Two caveats, both making the real figure BETTER than what prints:

  * ct.gov matches its query against title and description text as well as the
    condition field. The graph only ever links on the condition field, so
    ct.gov's total is an upper bound rather than an exact target.
  * ct.gov expands synonyms server-side, and some of what it returns is a
    trial that merely mentions the term.

So treat a number here as a floor on recall, and treat a big drop between runs
as the signal - that is what it is for.

Uses the public v2 API, no key, no MCP: this has to be runnable from the graph
host and from CI, not only from a session that happens to have a connector.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import neo                                               # noqa: E402

API = "https://clinicaltrials.gov/api/v2/studies"

# (what to ask ct.gov, the MeSH heading the graph files it under).
#
# The pairing is the point: they differ for most rows, and every one of those
# differences is a case where a naive name lookup would have scored zero and
# reported it as missing data.
PAIRS = [
    ("eczema",                      "Dermatitis, Atopic"),
    ("non-small cell lung cancer",  "Carcinoma, Non-Small-Cell Lung"),
    ("type 2 diabetes",             "Diabetes Mellitus, Type 2"),
    ("breast cancer",               "Breast Neoplasms"),
    ("Alzheimer disease",           "Alzheimer Disease"),
    ("Parkinson disease",           "Parkinson Disease"),
    ("multiple sclerosis",          "Multiple Sclerosis"),
    ("rheumatoid arthritis",        "Arthritis, Rheumatoid"),
    ("asthma",                      "Asthma"),
    ("heart failure",               "Heart Failure"),
    ("colorectal cancer",           "Colorectal Neoplasms"),
    ("epilepsy",                    "Epilepsy"),
    ("psoriasis",                   "Psoriasis"),
    ("COVID-19",                    "COVID-19"),
    ("obesity",                     "Obesity"),
]


def ctgov_total(condition: str, retries: int = 3) -> int | None:
    q = urllib.parse.urlencode({
        "query.cond": condition,
        "countTotal": "true",
        "pageSize": "1",
        "fields": "NCTId",
    })
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(f"{API}?{q}", timeout=60) as r:
                return json.loads(r.read()).get("totalCount")
        except Exception as exc:                          # noqa: BLE001
            if attempt == retries - 1:
                print(f"   ({condition}: {type(exc).__name__})")
                return None
            time.sleep(2 * (attempt + 1))
    return None




# Restricted to trials this graph took FROM ct.gov, so the comparison is
# like-for-like. Counting all 22 registries against ct.gov's total would
# produce a recall above 100% and mean nothing.
GRAPH_Q = """
MATCH (d:Disease {name: $mesh})
OPTIONAL MATCH (t:ClinicalTrial {registry:'clinicaltrials.gov'})-[:STUDIES]->(x)
WHERE x = d OR (x)-[:SUBTYPE_OF*1..3]->(d)
RETURN count(DISTINCT t) AS n
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conditions", help="comma-separated, overrides the list")
    a = ap.parse_args()

    pairs = PAIRS
    if a.conditions:
        pairs = [(c.strip(), c.strip()) for c in a.conditions.split(",")]

    drv = neo.driver()
    print(f"{'condition':<30}{'ct.gov':>9}{'graph':>9}{'recall':>9}")
    print("-" * 57)
    tot_reg = tot_graph = 0
    rows = []
    with drv.session(database=neo.config()[3]) as s:
        for cond, mesh in pairs:
            reg = ctgov_total(cond)
            if reg is None:
                continue
            got = s.run(GRAPH_Q, mesh=mesh).single()["n"]
            tot_reg += reg
            tot_graph += got
            rows.append((cond, reg, got))
            pct = f"{got/reg:.0%}" if reg else "-"
            print(f"{cond[:29]:<30}{reg:>9,}{got:>9,}{pct:>9}")
    print("-" * 57)
    if tot_reg:
        print(f"{'WEIGHTED':<30}{tot_reg:>9,}{tot_graph:>9,}"
              f"{tot_graph/tot_reg:>9.0%}")
    weak = [r for r in rows if r[1] and r[2] / r[1] < 0.5]
    if weak:
        print("\nUnder 50% - worth looking at the condition text for these:")
        for cond, reg, got in weak:
            print(f"   {cond:<30} {got:,} of {reg:,}")


if __name__ == "__main__":
    main()
