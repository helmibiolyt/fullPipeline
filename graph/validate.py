#!/usr/bin/env python3
"""Check a build before anyone trusts it.

    python graph/validate.py --dir graph/build

The point of building to files is that every claim about the graph is a claim
about a table, and can be checked without a database. These checks are ordered
by how badly a failure would mislead someone:

  1. referential integrity - an edge to a node that does not exist becomes,
     in Neo4j, a silently missing row rather than an error
  2. label collision    - one key under two labels merges two things
  3. duplicate substance - the same molecule as two nodes is the failure the
     whole resolver exists to prevent, and it is invisible in totals
  4. resolution quality  - what fraction of edges came from a dictionary match
     rather than a stated fact, per source
  5. fan-out outliers    - a node with 100k edges is usually a parse bug
  6. fixtures            - known biology that must survive any refactor

Exit code is non-zero if a FAIL-level check fails, so this can gate an import.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import pathlib
import sys

csv.field_size_limit(2**31 - 1)

FAILS: list[str] = []
WARNS: list[str] = []


def _read(path: pathlib.Path):
    with path.open(encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)


def fail(msg):
    FAILS.append(msg)
    print(f"  FAIL  {msg}")


def warn(msg):
    WARNS.append(msg)
    print(f"  warn  {msg}")


def ok(msg):
    print(f"  ok    {msg}")


# Known biology. If a refactor breaks one of these it broke the graph, whatever
# the totals say. Kept small and specific on purpose.
FIXTURES = [
    ("UNII:C0GEJ5QCSO", "TARGETS", "UNIPROT:P04035",
     "atorvastatin -> HMG-CoA reductase"),
    ("UNII:DPT0O3T46P", "TARGETS", "UNIPROT:Q15116",
     "pembrolizumab -> PD-1"),
    ("UNII:I5I8VB78VT", "TARGETS", "UNIPROT:Q16602",
     "erenumab -> CGRP receptor"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="graph/build")
    a = ap.parse_args()
    d = pathlib.Path(a.dir)
    if not (d / "manifest.json").exists():
        sys.exit(f"no build at {d}")
    man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))

    # ---- load keys -------------------------------------------------------
    keys_by_label: dict[str, set] = {}
    props: dict[str, list] = {}
    for p in sorted((d / "nodes").glob("*.csv")):
        label = p.stem
        ks, rows = set(), []
        for r in _read(p):
            ks.add(r["key"])
            rows.append(r)
        keys_by_label[label] = ks
        props[label] = rows
    all_keys = set().union(*keys_by_label.values()) if keys_by_label else set()
    print(f"\n{sum(len(v) for v in keys_by_label.values()):,} nodes / "
          f"{len(keys_by_label)} labels   run {man.get('run_id')}\n")

    # ---- 1. referential integrity ---------------------------------------
    print("referential integrity")
    total_edges = 0
    for p in sorted((d / "edges").glob("*.csv")):
        missing = collections.Counter()
        n = 0
        for r in _read(p):
            n += 1
            for end in (r["src"], r["dst"]):
                if end not in all_keys:
                    missing[end.split(":", 1)[0]] += 1
        total_edges += n
        if missing:
            fail(f"{p.stem}: {sum(missing.values()):,}/{n:,} dangling endpoints "
                 f"-> {dict(missing.most_common(4))}")
        else:
            ok(f"{p.stem}: {n:,} edges, all endpoints resolve")

    # ---- 2. label collision ---------------------------------------------
    print("\nkey uniqueness across labels")
    owner: dict[str, str] = {}
    clashes = collections.Counter()
    for label, ks in keys_by_label.items():
        for k in ks:
            if k in owner and owner[k] != label:
                clashes[(owner[k], label)] += 1
            else:
                owner[k] = label
    if clashes:
        for (l1, l2), c in clashes.most_common(5):
            # Identifier deliberately shares its key with nothing else; any
            # other pair means two different things collapsed into one node.
            fail(f"{c:,} keys are both {l1} and {l2}")
    else:
        ok("no key is used by two labels")

    # ---- 3. duplicate substances ----------------------------------------
    print("\nsubstance resolution")
    by_norm = collections.defaultdict(set)
    by_method = collections.Counter()
    for r in props.get("Substance", []):
        by_method[r.get("resolved_by") or "?"] += 1
        nn = (r.get("norm_name") or "").strip()
        if nn:
            by_norm[nn].add(r["key"])
    dupes = {k: v for k, v in by_norm.items() if len(v) > 1}
    if dupes:
        sample = list(dupes.items())[:3]
        warn(f"{len(dupes):,} normalised names map to >1 Substance node "
             f"(e.g. {sample[0][0]!r} -> {sorted(sample[0][1])[:3]})")
    else:
        ok("no two Substance nodes share a normalised name")
    tot = sum(by_method.values()) or 1
    prov = by_method.get("provisional", 0)
    print(f"        {dict(by_method.most_common())}")
    if prov / tot > 0.25:
        warn(f"{prov / tot:.1%} of substances are provisional (unresolved names)")
    else:
        ok(f"provisional substances {prov:,}/{tot:,} = {prov / tot:.1%}")

    # ---- 4. match quality per edge type ---------------------------------
    print("\nhow edges were established")
    for p in sorted((d / "edges").glob("*.csv")):
        m = collections.Counter(r.get("match_method") or "?" for r in _read(p))
        n = sum(m.values())
        inferred = sum(c for k, c in m.items()
                       if k in ("name", "symbol", "provisional", "stereo"))
        flag = "  <- mostly inferred" if n and inferred / n > 0.5 else ""
        print(f"        {p.stem:20} {n:>9,}  {dict(m.most_common(4))}{flag}")

    # ---- 5. fan-out outliers --------------------------------------------
    print("\nfan-out outliers")
    worst = []
    for p in sorted((d / "edges").glob("*.csv")):
        deg = collections.Counter()
        for r in _read(p):
            deg[r["src"]] += 1
        if deg:
            k, c = deg.most_common(1)[0]
            worst.append((c, p.stem, k))
    for c, etype, k in sorted(worst, reverse=True)[:6]:
        # A hub is normal (one agency approves every product). A hub on an
        # edge type that should be sparse is a parse bug.
        print(f"        {etype:20} max out-degree {c:>8,}  {k}")

    # ---- 6. fixtures -----------------------------------------------------
    print("\nfixtures")
    for src, etype, dst, label in FIXTURES:
        p = d / "edges" / f"{etype}.csv"
        if not p.exists():
            warn(f"{label}: no {etype} table")
            continue
        hit = any(r["src"] == src and r["dst"] == dst for r in _read(p))
        (ok if hit else fail)(f"{label}")

    # ---- 7. declared sources that were never read ------------------------
    #
    # sources.py is the statement of what feeds the graph. Five files sat in it
    # with no loader for a while and nothing noticed, because a relationship
    # that is never emitted does not look wrong in the totals - it looks like
    # sparse data. IN_CLASS had no Substance source at all and simply appeared
    # small.
    #
    # The manifest records the S3 key of every file actually read, so this is
    # an exact comparison rather than a guess at whether some loader mentions a
    # path somewhere.
    print("\ndeclared sources actually read")
    try:
        import sources as _sources
        declared = {d["file"] for d in _sources.INCLUDED}
        read = set(man.get("files_read", []))
        never = sorted(declared - read)
        # A slice legitimately skips sources it cannot filter - eu_ctr has no
        # substance column - so an unread file is only a failure on a full run.
        report = warn if man.get("mode") == "slice" else fail
        if never:
            report(f"{len(never)}/{len(declared)} declared sources were never read"
                   + ("  (slice mode)" if man.get("mode") == "slice" else ""))
            for f in never[:10]:
                print(f"          {f}")
        else:
            ok(f"all {len(declared)} declared sources were read")
        extra = sorted(read - declared)
        if extra:
            warn(f"{len(extra)} files read but not declared in sources.py: "
                 f"{extra[:3]}")
    except Exception as e:
        warn(f"source coverage check skipped: {type(e).__name__}: {e}")

    # ---- 8. isolated nodes ----------------------------------------------
    print("\nconnectivity")
    touched = set()
    for p in (d / "edges").glob("*.csv"):
        for r in _read(p):
            touched.add(r["src"])
            touched.add(r["dst"])
    for label, ks in sorted(keys_by_label.items()):
        iso = len(ks - touched)
        if iso and iso / len(ks) > 0.5:
            warn(f"{label}: {iso:,}/{len(ks):,} nodes have no edge at all")

    print(f"\n{total_edges:,} edges checked")
    if man.get("stats"):
        print(f"stats: {json.dumps(man['stats'])[:400]}")
    print(f"\n{len(FAILS)} failures, {len(WARNS)} warnings")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
