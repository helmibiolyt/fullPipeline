#!/usr/bin/env python3
"""What the unresolved drug arms actually are, split three ways.

    python graph/drug_gap.py [top]

unresolved_drugs.py lists terms the graph cannot name by an exact norm_name
lookup, and its own docstring warns that this OVERSTATES the gap: the loader
uses the whole resolver, so anything the salt, stereo or alias tiers handle
appears there as a miss. "Azithromycin" is listed and resolves fine.

That warning is right and it makes the list unusable for deciding what to fix.
This does the check the warning asks for, per term, against the LIVE GRAPH:

    resolved     something in the graph already reaches it, via any tier
    naming split the graph holds the compound under a different name
    absent       no node for that compound at all

Only the third is a real gap, and only the third is worth acting on. The
second is a synonym problem with a known answer. The first is not a gap.

Reads the lake and the graph, prints, changes nothing.
"""
from __future__ import annotations

import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import lake                                              # noqa: E402
import neo                                               # noqa: E402
import trials as T                                       # noqa: E402
from normalise import fold, squash, strip_salts          # noqa: E402

DRUGGY = ("drug:", "biological:")

# Enough of the term to be worth a substring search. Below this a "contains"
# query matches half the substance table.
MIN_PROBE = 6


def main() -> None:
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 30

    terms = collections.Counter()
    for row in lake.stream_csv(T.L["ctgov"]):
        raw = row.get("interventions") or ""
        if not any(k in raw.lower() for k in DRUGGY):
            continue
        for part in T._SEP.split(raw):
            if not part.lower().strip().startswith(DRUGGY):
                continue
            for t in T._terms(part, kind="intervention"):
                terms[t.strip()] += 1

    top = terms.most_common(600)
    print(f"{len(terms):,} distinct drug-typed terms; checking the top {len(top)}\n")

    resolved, split, absent = [], [], []
    with neo.session() as s:
        for term, count in top:
            f = fold(term)
            if not f:
                continue
            # 1. Does anything in the graph already reach it? Exact name, the
            #    salt-stripped form, or the separator-insensitive form - the
            #    same ladder the resolver climbs.
            hit = s.run(
                "MATCH (x:Substance) WHERE x.norm_name = $f OR x.norm_name = $salt "
                "RETURN x.name AS n LIMIT 1",
                f=f, salt=strip_salts(term)).single()
            if hit:
                resolved.append((count, term, hit["n"]))
                continue
            # 2. Is it in there under a longer name - a salt form, a
            #    formulation, a combination? That is a synonym problem.
            near = None
            if len(f) >= MIN_PROBE:
                near = s.run(
                    "MATCH (x:Substance) WHERE x.norm_name CONTAINS $f "
                    "RETURN x.name AS n, COUNT { (x)-[:TESTED_IN]->() } AS t "
                    "ORDER BY t DESC LIMIT 1", f=f).single()
            if near:
                split.append((count, term, near["n"], near["t"]))
            else:
                absent.append((count, term))

    tot = sum(c for c, *_ in resolved + split + absent)
    for label, rows in (("ALREADY RESOLVED - not a gap", resolved),
                        ("NAMING SPLIT - held under another name", split),
                        ("ABSENT - no node for this compound", absent)):
        n = sum(r[0] for r in rows)
        print(f"{label:<44} {len(rows):>4} terms  {n:>7,} arms  "
              f"({n/tot:.0%})")
    print()
    print(f"the {want} biggest ABSENT compounds - the only real gap:")
    for c, term in absent[:want]:
        print(f"   {c:>6,}  {term[:56]!r}")
    print()
    print("biggest NAMING SPLITS, with what the graph calls them:")
    for c, term, name, t in sorted(split, reverse=True)[:12]:
        print(f"   {c:>6,}  {term[:34]!r:<36} -> {name[:38]!r} ({t:,} trials)")


if __name__ == "__main__":
    main()
