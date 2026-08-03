#!/usr/bin/env python3
"""Which drug-typed intervention terms does the resolver not know?

    python graph/unresolved_drugs.py             # top 60
    python graph/unresolved_drugs.py 200

64% of ClinicalTrials.gov trials that name a Drug or Biological resolve to a
Substance. The other 36% - roughly 83,500 trials - are the largest measurable
gap left in this graph, and nothing has looked at what they actually contain.

This extracts the drug-typed arms, strips the label the way the loader does,
and asks the LIVE GRAPH which of the resulting terms it knows. Anything the
graph cannot name is either a compound it genuinely does not hold, or text
that is not a drug name at all - and the two need different fixes, so the
point is to see the split rather than guess it.

Reads the lake and the graph, prints, changes nothing.
"""
from __future__ import annotations

import collections
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import lake                                              # noqa: E402
import trials as T                                       # noqa: E402
from normalise import fold                               # noqa: E402

DRUGGY = ("drug:", "biological:")


def driver():
    from dotenv import load_dotenv
    load_dotenv(pathlib.Path(__file__).resolve().parent.parent
                / "testPipeline" / ".env")
    from neo4j import GraphDatabase
    return GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ.get("NEO4J_USER", "neo4j"),
              os.environ["NEO4J_PASSWORD"]))


def main() -> None:
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 60

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
    print(f"distinct drug-typed terms: {len(terms):,}")

    # Ask the graph which it knows, in batches - one query per term would be
    # thousands of round trips.
    top = [t for t, _ in terms.most_common(4000)]
    known = set()
    drv = driver()
    with drv.session(database=os.getenv("NEO4J_DATABASE", "biolyt")) as s:
        for i in range(0, len(top), 500):
            batch = [fold(t) for t in top[i:i + 500]]
            for r in s.run(
                    "UNWIND $names AS n MATCH (s:Substance {norm_name:n}) "
                    "RETURN DISTINCT n AS hit", names=batch):
                known.add(r["hit"])

    miss = [(t, c) for t, c in terms.most_common(4000) if fold(t) not in known]
    hit_rows = sum(c for t, c in terms.most_common(4000) if fold(t) in known)
    miss_rows = sum(c for _, c in miss)
    print(f"of the 4,000 most common terms:")
    print(f"   the graph knows       {len(top)-len(miss):>6,} terms, "
          f"{hit_rows:>9,} arm mentions")
    print(f"   the graph does not    {len(miss):>6,} terms, "
          f"{miss_rows:>9,} arm mentions")
    print()
    print(f"most common terms the graph cannot name (top {want}):")
    for t, c in miss[:want]:
        print(f"   {c:>6,}  {t[:66]!r}")


if __name__ == "__main__":
    main()
