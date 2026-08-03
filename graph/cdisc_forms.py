#!/usr/bin/env python3
"""Would CDISC's Dosage Form codelist canonicalise Product.form?

    python graph/cdisc_forms.py

Product.form holds 378 distinct values across 167,432 products, normalised by
hand: upper-cased, punctuation stripped. That merged the 16 pairs that
differed only in punctuation and did nothing about the rest, because there
was no authority to normalise TOWARDS.

CDISC publishes one - 197 terms in SDTM Terminology, the vocabulary FDA and
EMA submissions actually use. This measures how much of our 378 it covers
before anything is wired to it. Reads the lake and the graph, prints, changes
nothing.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import lake                                              # noqa: E402

SDTM = "Ontologies_Standards/cdisc.org/CDISC/data/SDTM Terminology.csv"
CODELIST = "Dosage Form"
NOT_A_TERM = {"FRM"}          # the codelist's own header row


def canon(s: str) -> str:
    """Punctuation-insensitive comparison key. CDISC writes 'AEROSOL, FOAM'
    and the graph holds 'AEROSOL FOAM' after this morning's normalisation, so
    the two only meet on this form."""
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", (s or "").upper()).split())


def cdisc_forms() -> dict[str, str]:
    out: dict[str, str] = {}
    for r in lake.stream_csv(SDTM):
        if (r.get("Codelist Name") or "").strip() != CODELIST:
            continue
        v = (r.get("CDISC Submission Value") or "").strip()
        if not v or v in NOT_A_TERM:
            continue
        out.setdefault(canon(v), v)
        for syn in (r.get("CDISC Synonym(s)") or "").split(";"):
            if syn.strip():
                out.setdefault(canon(syn), v)
    return out


def graph_forms() -> list[tuple[str, int]]:
    from dotenv import load_dotenv
    load_dotenv(pathlib.Path(__file__).resolve().parent.parent
                / "testPipeline" / ".env")
    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ.get("NEO4J_USER", "neo4j"),
              os.environ["NEO4J_PASSWORD"]))
    with drv.session(database=os.getenv("NEO4J_DATABASE", "biolyt")) as s:
        return [(r["v"], r["n"]) for r in s.run(
            "MATCH (p:Product) WHERE p.form <> '' "
            "RETURN p.form AS v, count(*) AS n ORDER BY n DESC")]


def main() -> None:
    cd = cdisc_forms()
    print(f"CDISC Dosage Form: {len(cd)} lookup keys")
    ours = graph_forms()
    tot = sum(n for _, n in ours)
    print(f"graph:             {len(ours)} distinct forms, {tot:,} products\n")

    hit = [(v, n) for v, n in ours if canon(v) in cd]
    miss = [(v, n) for v, n in ours if canon(v) not in cd]
    hn = sum(n for _, n in hit)
    print(f"in CDISC     {len(hit):>4} forms   {hn:>9,} products  ({hn/tot:.1%})")
    print(f"not in CDISC {len(miss):>4} forms   {tot-hn:>9,} products  "
          f"({(tot-hn)/tot:.1%})\n")
    print("biggest forms CDISC does NOT define:")
    for v, n in miss[:15]:
        print(f"   {n:>7,}  {v[:56]!r}")
    print("\nforms CDISC would RENAME (ours differs from its submission value):")
    shown = 0
    for v, n in hit:
        std = cd[canon(v)]
        if std != v and shown < 12:
            print(f"   {n:>7,}  {v[:38]!r:<40} -> {std!r}")
            shown += 1


if __name__ == "__main__":
    main()
