#!/usr/bin/env python3
"""Why is one condition under-linked? Shows the text that failed.

    python graph/why_condition.py covid
    python graph/why_condition.py obesity --registry clinicaltrials.gov

recall_vs_ctgov.py says WHICH conditions are weak. It cannot say why, because
the condition text a trial was loaded from is not stored on the node - only
the edge it produced, or the absence of one.

So this goes back to the source file, finds the rows whose condition mentions
the term, checks which of those trials ended up linked, and prints the wording
of the ones that did not. That wording is the whole answer: it is either a
concept the dictionaries do not hold, or a phrasing they do hold under another
name.

Restricted to ClinicalTrials.gov by default, because that is the registry the
recall numbers compare against - a fix that helps IRCT or DRKS will not move
them, and confusing the two wasted a measurement earlier.
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import lake                                              # noqa: E402
import neo                                               # noqa: E402
import trials as T                                       # noqa: E402
from normalise import condition_variants, fold           # noqa: E402

SOURCES = {
    "clinicaltrials.gov": (T.L["ctgov"], "conditions", "nct_id", "NCT:"),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("term", help="substring to look for, case-insensitive")
    ap.add_argument("--registry", default="clinicaltrials.gov")
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()

    path, col, idcol, prefix = SOURCES[a.registry]
    term = a.term.lower()

    # Which trials in this registry already reach a Disease whose name
    # mentions the term. Deliberately name-based: it is the same loose net the
    # question is asked with.
    linked = set()
    with neo.session() as s:
        for r in s.run(
                "MATCH (t:ClinicalTrial {registry:$reg})-[:STUDIES]->(d:Disease) "
                "WHERE toLower(d.name) CONTAINS $t RETURN t.key AS k",
                reg=a.registry, t=term):
            linked.add(r["k"])
    print(f"{a.registry}: {len(linked):,} trials already linked to a "
          f"Disease whose name contains {a.term!r}")

    seen = unlinked = 0
    misses = collections.Counter()
    for row in lake.stream_csv(path):
        cond = row.get(col) or ""
        if term not in cond.lower():
            continue
        seen += 1
        key = prefix + (row.get(idcol) or "").strip()
        if key in linked:
            continue
        unlinked += 1
        for t in T._terms(cond)[:3]:
            misses[t.strip()[:56]] += 1

    print(f"rows whose condition text mentions it: {seen:,}")
    print(f"  of those, not linked to such a Disease: {unlinked:,} "
          f"({unlinked/seen:.0%})" if seen else "  none found")
    print(f"\nwhat the unlinked ones say (top {a.top}):")
    for t, c in misses.most_common(a.top):
        # Say whether the dictionaries could reach it, so a reader can tell a
        # missing concept from a missing rewriting without another script.
        hit = ""
        print(f"   {c:>6,}  {t!r}{hit}")


if __name__ == "__main__":
    main()
